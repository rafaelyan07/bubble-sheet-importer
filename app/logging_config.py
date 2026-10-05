"""Logging estruturado em JSON (uma linha por evento), pronto para Render/Railway/Cloud Run."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

# Atributos padrão do LogRecord; o resto (passado via extra=) vira campo do JSON
_PADRAO = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        dados = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for chave, valor in record.__dict__.items():
            if chave not in _PADRAO and not chave.startswith("_"):
                dados[chave] = valor
        if record.exc_info:
            dados["exc"] = self.formatException(record.exc_info)
        return json.dumps(dados, ensure_ascii=False, default=str)


def configurar_logging(nivel: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    raiz = logging.getLogger()
    raiz.handlers[:] = [handler]
    raiz.setLevel(nivel)
    # httpx loga cada request em INFO; deixa só avisos
    logging.getLogger("httpx").setLevel(logging.WARNING)
