"""Configuração carregada de variáveis de ambiente (.env via python-dotenv)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    pass


def _bool(valor: str | None, padrao: bool = False) -> bool:
    if valor is None or valor.strip() == "":
        return padrao
    return valor.strip().lower() in {"1", "true", "sim", "yes", "on"}


def _lista(valor: str | None) -> list[str]:
    if not valor:
        return []
    return [item.strip() for item in valor.split(",") if item.strip()]


def _obrigatoria(nome: str) -> str:
    valor = os.getenv(nome, "").strip()
    if not valor:
        raise ConfigError(f"Variável de ambiente obrigatória ausente: {nome}")
    return valor


def montar_base_url(app_url: str, ambiente: str) -> str:
    """Monta a URL da Data API a partir do domínio e do ambiente (test/live)."""
    app_url = app_url.rstrip("/")
    ambiente = ambiente.strip().lower()
    if ambiente in {"test", "dev", "version-test"}:
        return f"{app_url}/version-test/api/1.1/obj"
    if ambiente in {"live", "prod", "production"}:
        return f"{app_url}/api/1.1/obj"
    raise ConfigError(f"BUBBLE_ENV inválido: {ambiente!r} (use 'test' ou 'live')")


@dataclass(frozen=True)
class Settings:
    api_key: str
    bubble_base_url: str
    bubble_api_token: str
    bubble_data_type: str = "conciliacao"
    bubble_importacao_type: str = "importacao"
    bubble_batch_size: int = 1000
    bubble_max_retries: int = 5
    bubble_timeout: float = 300.0
    campo_importacao: str = "importacao"
    chave_duplicidade: list[str] = field(
        default_factory=lambda: ["cpf", "data", "tipo_cobranca", "valor"]
    )
    validar_digito_cpf: bool = False
    allowed_file_hosts: list[str] = field(
        default_factory=lambda: ["amazonaws.com", "cdn.bubble.io"]
    )
    max_file_mb: int = 25
    log_level: str = "INFO"
    expor_docs: bool = False

    @property
    def bulk_url(self) -> str:
        return f"{self.bubble_base_url.rstrip('/')}/{self.bubble_data_type}/bulk"

    def importacao_url(self, importacao_id: str) -> str:
        return f"{self.bubble_base_url.rstrip('/')}/{self.bubble_importacao_type}/{importacao_id}"


@lru_cache
def get_settings() -> Settings:
    base_url = os.getenv("BUBBLE_BASE_URL", "").strip() or montar_base_url(
        _obrigatoria("BUBBLE_APP_URL"), os.getenv("BUBBLE_ENV", "test")
    )
    batch = int(os.getenv("BUBBLE_BATCH_SIZE", "1000"))
    if not 1 <= batch <= 1000:
        raise ConfigError("BUBBLE_BATCH_SIZE deve estar entre 1 e 1000")

    return Settings(
        api_key=_obrigatoria("API_KEY"),
        bubble_base_url=base_url,
        bubble_api_token=_obrigatoria("BUBBLE_API_TOKEN"),
        bubble_data_type=os.getenv("BUBBLE_DATA_TYPE", "conciliacao").strip(),
        bubble_importacao_type=os.getenv("BUBBLE_IMPORTACAO_TYPE", "importacao").strip(),
        bubble_batch_size=batch,
        bubble_max_retries=int(os.getenv("BUBBLE_MAX_RETRIES", "5")),
        bubble_timeout=float(os.getenv("BUBBLE_TIMEOUT", "300")),
        campo_importacao=os.getenv("CAMPO_IMPORTACAO", "importacao").strip(),
        chave_duplicidade=_lista(os.getenv("CHAVE_DUPLICIDADE"))
        or ["cpf", "data", "tipo_cobranca", "valor"],
        validar_digito_cpf=_bool(os.getenv("VALIDAR_DIGITO_CPF"), False),
        allowed_file_hosts=_lista(os.getenv("ALLOWED_FILE_HOSTS"))
        or ["amazonaws.com", "cdn.bubble.io"],
        max_file_mb=int(os.getenv("MAX_FILE_MB", "25")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        expor_docs=_bool(os.getenv("EXPOR_DOCS"), False),
    )
