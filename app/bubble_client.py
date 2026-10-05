"""Cliente do endpoint bulk da Data API do Bubble."""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

STATUS_RETRY = {429, 500, 502, 503, 504}


class ErroBubble(RuntimeError):
    def __init__(self, mensagem: str, status: int | None = None):
        super().__init__(mensagem)
        self.status = status


@dataclass
class ResultadoGravacao:
    sucesso: int = 0
    ids: list[str] = field(default_factory=list)
    erros: list[dict[str, Any]] = field(default_factory=list)


class BubbleClient:
    def __init__(
        self,
        bulk_url: str,
        token: str,
        batch_size: int = 1000,
        max_retries: int = 5,
        timeout: float = 300.0,
        backoff_base: float = 1.0,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.bulk_url = bulk_url
        self.batch_size = min(batch_size, 1000)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        self._http = http_client or httpx.Client(timeout=timeout)
        self._auth = {"Authorization": f"Bearer {token}"}

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "BubbleClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ API pública

    def gravar(self, itens: list[tuple[int, dict[str, Any]]]) -> ResultadoGravacao:
        """Grava os registros em lotes. `itens` = [(linha_origem, registro), ...]."""
        resultado = ResultadoGravacao()
        total_lotes = (len(itens) + self.batch_size - 1) // self.batch_size

        for numero, inicio in enumerate(range(0, len(itens), self.batch_size), start=1):
            lote = itens[inicio : inicio + self.batch_size]
            inicio_t = time.monotonic()
            try:
                resposta = self._enviar_lote(lote)
                sucesso_antes, erros_antes = resultado.sucesso, len(resultado.erros)
                self._processar_resposta(lote, resposta, resultado)
                logger.info(
                    "lote gravado",
                    extra={
                        "etapa": "bubble_bulk",
                        "lote": numero,
                        "total_lotes": total_lotes,
                        "registros": len(lote),
                        "sucesso": resultado.sucesso - sucesso_antes,
                        "erros": len(resultado.erros) - erros_antes,
                        "duracao_s": round(time.monotonic() - inicio_t, 2),
                    },
                )
            except ErroBubble as exc:
                # Lote inteiro falhou: marca todas as linhas como erro e segue para o próximo
                logger.error(
                    "falha no lote",
                    extra={"etapa": "bubble_bulk", "lote": numero, "status": exc.status, "erro": str(exc)},
                )
                resultado.erros.extend({"linha": linha, "mensagem": str(exc)} for linha, _ in lote)

        return resultado

    def atualizar(self, url: str, campos: dict[str, Any]) -> None:
        """PATCH em um registro da Data API (ex.: .../obj/importacao/<id>). Levanta ErroBubble."""
        self._requisitar("PATCH", url, json=campos, headers=self._auth)

    # ------------------------------------------------------------------ internos

    @staticmethod
    def montar_corpo(registros: list[dict[str, Any]]) -> bytes:
        """Um objeto JSON por linha, UTF-8, sem escapar acentos."""
        return "\n".join(json.dumps(r, ensure_ascii=False) for r in registros).encode("utf-8")

    def _enviar_lote(self, lote: list[tuple[int, dict[str, Any]]]) -> str:
        corpo = self.montar_corpo([registro for _, registro in lote])
        headers = {**self._auth, "Content-Type": "text/plain; charset=utf-8"}
        return self._requisitar("POST", self.bulk_url, content=corpo, headers=headers).text

    def _requisitar(self, metodo: str, url: str, **kwargs: Any) -> httpx.Response:
        """Faz a requisição com retry/backoff em erros de conexão, 429 e 5xx."""
        for tentativa in range(self.max_retries + 1):
            try:
                resp = self._http.request(metodo, url, **kwargs)
            except httpx.TransportError as exc:
                if tentativa >= self.max_retries:
                    raise ErroBubble(f"Erro de conexão com o Bubble: {exc}") from exc
                self._esperar(tentativa, None, motivo=f"transporte: {exc}")
                continue

            if resp.status_code in STATUS_RETRY and tentativa < self.max_retries:
                self._esperar(tentativa, resp.headers.get("Retry-After"), motivo=f"HTTP {resp.status_code}")
                continue
            if resp.status_code >= 400:
                raise ErroBubble(
                    f"Bubble respondeu HTTP {resp.status_code}: {resp.text[:500]}", resp.status_code
                )
            return resp

        raise ErroBubble("Número máximo de tentativas excedido")  # pragma: no cover

    def _esperar(self, tentativa: int, retry_after: str | None, motivo: str) -> None:
        espera = self.backoff_base * (2**tentativa) + random.uniform(0, self.backoff_base)
        if retry_after:
            try:
                espera = max(espera, float(retry_after))
            except ValueError:
                pass
        logger.warning(
            "retry no Bubble",
            extra={"etapa": "bubble_bulk", "tentativa": tentativa + 1, "espera_s": round(espera, 2), "motivo": motivo},
        )
        self._sleep(espera)

    @staticmethod
    def _processar_resposta(
        lote: list[tuple[int, dict[str, Any]]], texto: str, resultado: ResultadoGravacao
    ) -> None:
        """A resposta tem uma linha JSON por registro, na mesma ordem do envio."""
        linhas_resp = [linha for linha in texto.splitlines() if linha.strip()]

        for i, (linha_origem, _) in enumerate(lote):
            if i >= len(linhas_resp):
                resultado.erros.append({"linha": linha_origem, "mensagem": "Sem resposta do Bubble para este registro"})
                continue
            try:
                item = json.loads(linhas_resp[i])
            except json.JSONDecodeError:
                resultado.erros.append({"linha": linha_origem, "mensagem": f"Resposta inválida: {linhas_resp[i][:200]}"})
                continue

            if item.get("status") == "success":
                resultado.sucesso += 1
                if item.get("id"):
                    resultado.ids.append(item["id"])
            else:
                mensagem = item.get("message") or item.get("body", {}).get("message") or json.dumps(item, ensure_ascii=False)
                resultado.erros.append({"linha": linha_origem, "mensagem": mensagem})
