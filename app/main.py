"""API de importação de planilhas para o Bubble.

Modo assíncrono: o POST /importar baixa e trata a planilha (~1 s), responde 202 com o resumo
e grava no Bubble em segundo plano. O andamento e o resultado final são escritos no registro
Importacao (campo status: processando → concluida | concluida_com_erros | erro).
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any, Callable

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.arquivo import ErroArquivo, baixar_arquivo
from app.bubble_client import BubbleClient, ErroBubble
from app.config import ConfigError, Settings, get_settings
from app.logging_config import configurar_logging
from app.tratamento import ErroPlanilha, ler_planilha, tratar_dataframe

try:
    _settings = get_settings()
    configurar_logging(_settings.log_level)
    _expor_docs = _settings.expor_docs
except ConfigError:
    configurar_logging("INFO")
    _expor_docs = False

logger = logging.getLogger("app")

# /docs fica desligado por padrão em produção (EXPOR_DOCS=true para ligar)
app = FastAPI(
    title="Bubble Sheet Importer",
    version="2.0.0",
    docs_url="/docs" if _expor_docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _expor_docs else None,
)

# Formato do unique id do Bubble (ex.: 1791222659057x452330323960370400). Validar evita que o id
# seja usado para montar outra URL na Data API (ex.: "../conciliacao/...").
PADRAO_ID_BUBBLE = r"^\d+x\d+$"

# Quantos erros entram no campo "mensagem" da Importacao
MAX_ERROS_MENSAGEM = 20


# --------------------------------------------------------------------------- modelos


class ImportarRequest(BaseModel):
    file_url: str = Field(..., description="URL do arquivo no Bubble (aceita '//s3.amazonaws.com/...')")
    importacao_id: str = Field(..., pattern=PADRAO_ID_BUBBLE, description="Unique id da Importacao no Bubble")


class Descarte(BaseModel):
    linha: int
    motivo: str


class ImportarResponse(BaseModel):
    importacao_id: str
    status: str
    total_lidas: int
    validas: int
    descartadas: int
    descartes: list[Descarte]


# --------------------------------------------------------------------------- dependências


def settings_dep() -> Settings:
    try:
        return get_settings()
    except ConfigError as exc:
        logger.error("configuração inválida", extra={"erro": str(exc)})
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "API mal configurada") from exc


def verificar_api_key(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    settings: Settings = Depends(settings_dep),
) -> None:
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "X-API-Key inválida")


def bubble_factory_dep(settings: Settings = Depends(settings_dep)) -> Callable[[], BubbleClient]:
    """Fábrica (e não o cliente) porque a gravação roda depois da resposta, fora do ciclo da dependência."""

    def criar() -> BubbleClient:
        return BubbleClient(
            bulk_url=settings.bulk_url,
            token=settings.bubble_api_token,
            batch_size=settings.bubble_batch_size,
            max_retries=settings.bubble_max_retries,
            timeout=settings.bubble_timeout,
        )

    return criar


# --------------------------------------------------------------------------- Importacao


def atualizar_importacao(
    bubble: BubbleClient, settings: Settings, importacao_id: str, campos: dict[str, Any]
) -> None:
    """Atualiza o registro Importacao. Falha aqui só é logada: não deve derrubar a importação."""
    try:
        bubble.atualizar(settings.importacao_url(importacao_id), campos)
    except ErroBubble as exc:
        logger.error(
            "falha ao atualizar Importacao",
            extra={"importacao_id": importacao_id, "etapa": "importacao", "status": exc.status, "erro": str(exc)},
        )


def resumir_erros(erros: list[dict[str, Any]]) -> str:
    linhas = [f"linha {e['linha']}: {e['mensagem']}" for e in erros[:MAX_ERROS_MENSAGEM]]
    if len(erros) > MAX_ERROS_MENSAGEM:
        linhas.append(f"... e mais {len(erros) - MAX_ERROS_MENSAGEM} erro(s)")
    return "\n".join(linhas)


def gravar_em_segundo_plano(
    criar_bubble: Callable[[], BubbleClient],
    settings: Settings,
    importacao_id: str,
    registros: list[tuple[int, dict[str, Any]]],
    inicio: float,
) -> None:
    contexto = {"importacao_id": importacao_id}
    with criar_bubble() as bubble:
        try:
            gravacao = bubble.gravar(registros)
        except Exception as exc:  # noqa: BLE001 - qualquer falha precisa chegar ao Bubble como status=erro
            logger.exception("falha na gravação", extra={**contexto, "etapa": "fim"})
            atualizar_importacao(
                bubble, settings, importacao_id, {"status": "erro", "mensagem": f"Falha na gravação: {exc}"}
            )
            return

        if not gravacao.erros:
            situacao = "concluida"
        elif gravacao.sucesso:
            situacao = "concluida_com_erros"
        else:
            situacao = "erro"

        campos: dict[str, Any] = {
            "status": situacao,
            "gravadas_sucesso": gravacao.sucesso,
            "gravadas_erro": len(gravacao.erros),
        }
        if gravacao.erros:
            campos["mensagem"] = resumir_erros(gravacao.erros)
        atualizar_importacao(bubble, settings, importacao_id, campos)

    logger.info(
        "importação concluída",
        extra={
            **contexto,
            "etapa": "fim",
            "status": situacao,
            "gravadas_sucesso": gravacao.sucesso,
            "gravadas_erro": len(gravacao.erros),
            "duracao_s": round(time.monotonic() - inicio, 2),
        },
    )


# --------------------------------------------------------------------------- rotas


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# def síncrono: o FastAPI roda em threadpool, sem bloquear o event loop
@app.post(
    "/importar",
    response_model=ImportarResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verificar_api_key)],
)
def importar(
    body: ImportarRequest,
    background: BackgroundTasks,
    settings: Settings = Depends(settings_dep),
    criar_bubble: Callable[[], BubbleClient] = Depends(bubble_factory_dep),
) -> ImportarResponse:
    inicio = time.monotonic()
    contexto = {"importacao_id": body.importacao_id}
    logger.info("importação iniciada", extra={**contexto, "etapa": "inicio", "file_url": body.file_url})

    try:
        conteudo, nome = baixar_arquivo(body.file_url, settings.allowed_file_hosts, settings.max_file_mb)
        df = ler_planilha(conteudo, nome)
    except (ErroArquivo, ErroPlanilha) as exc:
        logger.warning("arquivo rejeitado", extra={**contexto, "etapa": "leitura", "erro": str(exc)})
        with criar_bubble() as bubble:
            atualizar_importacao(bubble, settings, body.importacao_id, {"status": "erro", "mensagem": str(exc)})
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    logger.info("planilha lida", extra={**contexto, "etapa": "leitura", "linhas": len(df)})

    tratado = tratar_dataframe(
        df,
        importacao_id=body.importacao_id,
        campo_importacao=settings.campo_importacao,
        chave_duplicidade=settings.chave_duplicidade,
        validar_digito_cpf=settings.validar_digito_cpf,
    )

    resposta = ImportarResponse(
        importacao_id=body.importacao_id,
        status="processando" if tratado.registros else "concluida",
        total_lidas=tratado.total_lidas,
        validas=len(tratado.registros),
        descartadas=len(tratado.descartes),
        descartes=tratado.descartes,
    )

    campos: dict[str, Any] = {
        "status": resposta.status,
        "total_lidas": resposta.total_lidas,
        "validas": resposta.validas,
        "descartadas": resposta.descartadas,
    }
    if not tratado.registros:
        campos.update(gravadas_sucesso=0, gravadas_erro=0)
    with criar_bubble() as bubble:
        atualizar_importacao(bubble, settings, body.importacao_id, campos)

    if tratado.registros:
        background.add_task(
            gravar_em_segundo_plano, criar_bubble, settings, body.importacao_id, tratado.registros, inicio
        )

    logger.info(
        "importação aceita",
        extra={**contexto, "etapa": "aceita", **resposta.model_dump(exclude={"descartes"})},
    )
    return resposta
