"""Leitura da planilha e tratamento dos dados.

Todas as funções aqui são puras (não fazem I/O de rede) para serem testadas isoladamente.
"""

from __future__ import annotations

import io
import logging
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# coluna da planilha -> campo no Bubble
COLUNAS: dict[str, str] = {
    "Nome do Servidor": "nome",
    "CPF do Servidor": "cpf",
    "Mês da Conciliação": "data",
    "Descr. Motivo da Conciliação": "motivo",
    "Descrição da Espécie": "tipo_cobranca",
    "Valor Averbado": "valor",
    "Número da Averbação": "averbacao",
}

# Campos sem os quais a linha é descartada
CAMPOS_OBRIGATORIOS = ("nome", "cpf", "data", "valor")

MESES = {
    "JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12,
}

# Agrupamento da "Descrição da Espécie": (regex sobre o texto já normalizado, valor final).
# A primeira regra que casar vence; o que não casar mantém o texto original.
REGRAS_TIPO_COBRANCA: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ODONTO"), "CONVENIO ODONTOLOGICO"),
    (re.compile(r"ASSIST.*MEDIC|MEDIC.*ASSIST"), "CONVENIO MEDICO"),
    (re.compile(r"SEGURO.*VIDA"), "SEGURO"),
]


class ErroPlanilha(ValueError):
    """Planilha ilegível ou sem as colunas esperadas."""


@dataclass
class ResultadoTratamento:
    total_lidas: int
    # (linha_origem, registro pronto para o Bubble)
    registros: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    descartes: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- leitura


def ler_planilha(conteudo: bytes, nome_arquivo: str = "") -> pd.DataFrame:
    """Lê .xlsx ou .csv a partir dos bytes. Tudo vem como object para não perder zeros do CPF."""
    nome = nome_arquivo.lower()
    eh_xlsx = nome.endswith((".xlsx", ".xlsm")) or conteudo[:2] == b"PK"

    try:
        if eh_xlsx:
            df = pd.read_excel(io.BytesIO(conteudo), dtype=object, engine="openpyxl")
        else:
            df = _ler_csv(conteudo)
    except ErroPlanilha:
        raise
    except Exception as exc:  # noqa: BLE001 - qualquer falha de parse vira erro de planilha
        raise ErroPlanilha(f"Não foi possível ler o arquivo: {exc}") from exc

    return padronizar_colunas(df)


def _ler_csv(conteudo: bytes) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            texto = conteudo.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 decodifica qualquer byte
        raise ErroPlanilha("Encoding do CSV não suportado")

    # sep=None faz o pandas detectar ; ou , automaticamente
    return pd.read_csv(io.StringIO(texto), sep=None, engine="python", dtype=str)


def _chave_coluna(nome: str) -> str:
    """Normaliza o nome da coluna: sem acento, minúsculo, espaços únicos."""
    sem_acento = unicodedata.normalize("NFKD", str(nome)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", sem_acento).strip().lower()


def padronizar_colunas(df: pd.DataFrame) -> pd.DataFrame:
    """Renomeia as colunas da planilha para os campos do Bubble, tolerando acento/caixa/espaços."""
    mapa = {_chave_coluna(origem): destino for origem, destino in COLUNAS.items()}
    renomear = {col: mapa[_chave_coluna(col)] for col in df.columns if _chave_coluna(col) in mapa}

    faltando = [origem for origem, destino in COLUNAS.items() if destino not in renomear.values()]
    if faltando:
        raise ErroPlanilha(f"Colunas ausentes na planilha: {', '.join(faltando)}")

    return df.rename(columns=renomear)[list(COLUNAS.values())]


# --------------------------------------------------------------------------- normalizadores


def _vazio(valor: Any) -> bool:
    if valor is None:
        return True
    if isinstance(valor, float) and math.isnan(valor):
        return True
    if valor is pd.NaT:
        return True
    return isinstance(valor, str) and valor.strip() == ""


def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()


def normalizar_texto(valor: Any) -> str | None:
    """Remove acentos e espaços extras e coloca em maiúsculas. Vazio vira None."""
    if _vazio(valor):
        return None
    texto = re.sub(r"\s+", " ", _sem_acento(str(valor))).strip().upper()
    return texto or None


def normalizar_averbacao(valor: Any) -> str | None:
    """Número da averbação como texto; desfaz o float do Excel (12345.0 -> "12345")."""
    if isinstance(valor, float) and not math.isnan(valor) and valor.is_integer():
        valor = int(valor)
    return normalizar_texto(valor)


def normalizar_tipo_cobranca(valor: Any) -> str | None:
    """Agrupa as espécies: seguro de vida → SEGURO, assistência médica → CONVENIO MEDICO,
    assistência odontológica → CONVENIO ODONTOLOGICO. As demais só são padronizadas."""
    texto = normalizar_texto(valor)
    if texto is None:
        return None
    for padrao, destino in REGRAS_TIPO_COBRANCA:
        if padrao.search(texto):
            return destino
    return texto


def normalizar_cpf(valor: Any) -> str | None:
    """Retorna o CPF no formato xxx.xxx.xxx-xx, ou None se não tiver 11 dígitos.

    CPFs que perderam até 2 zeros à esquerda (Excel tratando como número) são completados.
    """
    if _vazio(valor):
        return None
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    digitos = re.sub(r"\D", "", str(valor))
    if not 9 <= len(digitos) <= 11:
        return None
    digitos = digitos.zfill(11)
    return f"{digitos[:3]}.{digitos[3:6]}.{digitos[6:9]}-{digitos[9:]}"


def cpf_digito_valido(cpf: str) -> bool:
    digitos = re.sub(r"\D", "", cpf)
    if len(digitos) != 11 or digitos == digitos[0] * 11:
        return False
    for tamanho in (9, 10):
        soma = sum(int(d) * peso for d, peso in zip(digitos[:tamanho], range(tamanho + 1, 1, -1)))
        dv = (soma * 10) % 11 % 10
        if dv != int(digitos[tamanho]):
            return False
    return True


def _iso(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}T00:00:00Z"


def normalizar_data(valor: Any) -> str | None:
    """Converte a data para ISO 8601 aceito pelo Bubble ("2026-01-31T00:00:00Z").

    Aceita datetime, número serial do Excel, "dd/mm/aaaa", "mm/aaaa", "aaaa-mm",
    "aaaa-mm-dd", "JAN/2026", "janeiro/2026". Quando só há mês/ano, usa o dia 1.
    """
    if _vazio(valor):
        return None
    if isinstance(valor, (pd.Timestamp, datetime)):
        return _iso(valor.date())
    if isinstance(valor, date):
        return _iso(valor)
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        if 1 <= valor < 100000:  # serial do Excel
            return _iso(date(1899, 12, 30) + timedelta(days=int(valor)))
        return None

    texto = str(valor).strip().upper()
    try:
        if m := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", texto):
            return _iso(date(int(m[3]), int(m[2]), int(m[1])))
        if m := re.fullmatch(r"(\d{1,2})[/.-](\d{4})", texto):
            return _iso(date(int(m[2]), int(m[1]), 1))
        if m := re.fullmatch(r"(\d{4})-(\d{1,2})", texto):
            return _iso(date(int(m[1]), int(m[2]), 1))
        if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})([ T].*)?", texto):
            return _iso(date(int(m[1]), int(m[2]), int(m[3])))
        if m := re.fullmatch(r"([A-ZÇ]{3})[A-ZÇ]*[/ .-]+(?:DE[ ]+)?(\d{2}|\d{4})", texto):
            mes = MESES.get(m[1])
            if mes:
                ano = int(m[2]) + (2000 if len(m[2]) == 2 else 0)
                return _iso(date(ano, mes, 1))
    except ValueError:  # ex.: 31/02/2026
        return None
    return None


def normalizar_valor(valor: Any) -> float | None:
    """Converte valor monetário (número, "1.234,56", "R$ 1234.56", "1,234.56") para float."""
    if _vazio(valor):
        return None
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return None if math.isnan(valor) or math.isinf(valor) else round(float(valor), 2)

    texto = re.sub(r"[R$\s]", "", str(valor))
    negativo = texto.startswith("(") and texto.endswith(")")
    texto = texto.strip("()")
    if not re.fullmatch(r"-?[\d.,]+", texto):
        return None

    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):  # 1.234,56
            texto = texto.replace(".", "").replace(",", ".")
        else:  # 1,234.56
            texto = texto.replace(",", "")
    elif "," in texto:
        texto = texto.replace(",", ".") if texto.count(",") == 1 else texto.replace(",", "")
    elif texto.count(".") > 1:  # 1.234.567
        texto = texto.replace(".", "")

    try:
        numero = round(float(texto), 2)
    except ValueError:
        return None
    return -numero if negativo else numero


# --------------------------------------------------------------------------- pipeline


def _linha_vazia(linha: pd.Series) -> bool:
    return all(_vazio(v) for v in linha.tolist())


def tratar_linha(
    linha: pd.Series, validar_digito_cpf: bool = False
) -> tuple[dict[str, Any], list[str]]:
    """Normaliza uma linha. Retorna (registro sem campos vazios, lista de motivos de descarte)."""
    motivos: list[str] = []
    registro: dict[str, Any] = {
        "nome": normalizar_texto(linha.get("nome")),
        "cpf": normalizar_cpf(linha.get("cpf")),
        "data": normalizar_data(linha.get("data")),
        "motivo": normalizar_texto(linha.get("motivo")),
        "tipo_cobranca": normalizar_tipo_cobranca(linha.get("tipo_cobranca")),
        "valor": normalizar_valor(linha.get("valor")),
        "averbacao": normalizar_averbacao(linha.get("averbacao")),
    }

    for campo in CAMPOS_OBRIGATORIOS:
        if registro[campo] is None:
            original = linha.get(campo)
            if _vazio(original):
                motivos.append(f"{campo} vazio")
            else:
                motivos.append(f"{campo} inválido: {str(original).strip()!r}")

    if validar_digito_cpf and registro["cpf"] and not cpf_digito_valido(registro["cpf"]):
        motivos.append(f"cpf com dígito verificador inválido: {registro['cpf']}")

    # Valores vazios são omitidos (o Bubble não deve receber null)
    return {k: v for k, v in registro.items() if v is not None}, motivos


def tratar_dataframe(
    df: pd.DataFrame,
    importacao_id: str | None = None,
    campo_importacao: str = "importacao",
    chave_duplicidade: list[str] | tuple[str, ...] = ("cpf", "data", "tipo_cobranca", "valor"),
    validar_digito_cpf: bool = False,
) -> ResultadoTratamento:
    """Aplica normalização, validação e remoção de duplicadas.

    O DataFrame deve ter as colunas já padronizadas (ver padronizar_colunas).
    A linha de origem é a linha do Excel (cabeçalho = 1, primeiro dado = 2).
    Linhas totalmente vazias são ignoradas e não entram na contagem.
    """
    resultado = ResultadoTratamento(total_lidas=0)
    vistos: dict[tuple, int] = {}

    for posicao, (_, linha) in enumerate(df.iterrows()):
        linha_origem = posicao + 2
        if _linha_vazia(linha):
            continue
        resultado.total_lidas += 1

        registro, motivos = tratar_linha(linha, validar_digito_cpf)
        if motivos:
            resultado.descartes.append({"linha": linha_origem, "motivo": "; ".join(motivos)})
            continue

        chave = tuple(registro.get(campo) for campo in chave_duplicidade)
        if chave in vistos:
            resultado.descartes.append(
                {"linha": linha_origem, "motivo": f"duplicada da linha {vistos[chave]}"}
            )
            continue
        vistos[chave] = linha_origem

        if importacao_id and campo_importacao:
            registro[campo_importacao] = importacao_id
        resultado.registros.append((linha_origem, registro))

    logger.info(
        "tratamento concluído",
        extra={
            "etapa": "tratamento",
            "importacao_id": importacao_id,
            "lidas": resultado.total_lidas,
            "validas": len(resultado.registros),
            "descartadas": len(resultado.descartes),
        },
    )
    return resultado
