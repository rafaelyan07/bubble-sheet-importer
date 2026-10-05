"""Download do arquivo hospedado pelo Bubble."""

from __future__ import annotations

import logging
from urllib.parse import unquote, urlparse

import httpx

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 5


class ErroArquivo(ValueError):
    pass


def normalizar_url(url: str) -> str:
    """O Bubble envia "//s3.amazonaws.com/..." sem esquema; completa com https."""
    url = (url or "").strip()
    # O Bubble manda "null" quando o parâmetro do API Connector está sem valor
    if not url or url.lower() in {"null", "undefined"}:
        raise ErroArquivo("file_url vazio: confira o valor do parâmetro no Bubble")
    if url.startswith("//"):
        return "https:" + url
    if url.lower().startswith("http://"):
        return "https://" + url[len("http://"):]
    if not url.lower().startswith("https://"):
        return "https://" + url
    return url


def validar_host(url: str, hosts_permitidos: list[str]) -> None:
    """Evita que a API seja usada para baixar URLs arbitrárias (SSRF). Só aceita HTTPS."""
    partes = urlparse(url)
    if partes.scheme != "https":
        raise ErroArquivo(f"Só são aceitas URLs HTTPS: {url}")
    host = (partes.hostname or "").lower()
    if not hosts_permitidos:
        return
    if not any(host == h or host.endswith("." + h) for h in hosts_permitidos):
        raise ErroArquivo(f"Host não permitido para download: {host}")


def nome_do_arquivo(url: str) -> str:
    return unquote(urlparse(url).path.rsplit("/", 1)[-1])


def baixar_arquivo(
    url: str,
    hosts_permitidos: list[str],
    max_mb: int = 25,
    http_client: httpx.Client | None = None,
) -> tuple[bytes, str]:
    """Baixa o arquivo e retorna (conteúdo, nome do arquivo)."""
    url = normalizar_url(url)
    limite = max_mb * 1024 * 1024

    # Redirecionamentos seguidos à mão para validar o host de cada salto
    # (senão um host permitido poderia redirecionar para um endereço interno).
    cliente = http_client or httpx.Client(timeout=60, follow_redirects=False)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            validar_host(url, hosts_permitidos)
            with cliente.stream("GET", url) as resp:
                if resp.is_redirect and resp.next_request is not None:
                    url = str(resp.next_request.url)
                    continue
                if resp.status_code != 200:
                    raise ErroArquivo(f"Falha ao baixar arquivo: HTTP {resp.status_code}")
                partes, tamanho = [], 0
                for parte in resp.iter_bytes():
                    tamanho += len(parte)
                    if tamanho > limite:
                        raise ErroArquivo(f"Arquivo maior que o limite de {max_mb} MB")
                    partes.append(parte)
                break
        else:
            raise ErroArquivo("Redirecionamentos demais ao baixar o arquivo")
    except httpx.HTTPError as exc:
        raise ErroArquivo(f"Erro ao baixar arquivo: {exc}") from exc
    finally:
        if http_client is None:
            cliente.close()

    nome = nome_do_arquivo(url)
    logger.info("arquivo baixado", extra={"etapa": "download", "arquivo": nome, "bytes": tamanho})
    return b"".join(partes), nome
