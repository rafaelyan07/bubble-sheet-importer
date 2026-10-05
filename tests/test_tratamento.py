from datetime import datetime

import pandas as pd
import pytest

from app.tratamento import (
    ErroPlanilha,
    cpf_digito_valido,
    ler_planilha,
    mes_conciliacao,
    normalizar_averbacao,
    normalizar_cpf,
    normalizar_data,
    normalizar_texto,
    normalizar_tipo_cobranca,
    normalizar_valor,
    padronizar_colunas,
    tratar_dataframe,
)


@pytest.fixture
def df_exemplo() -> pd.DataFrame:
    planilha = pd.DataFrame(
        {
            "Nome do Servidor": ["  joão   da silva ", "Maria Souza", "", "Carlos", "João da Silva", None],
            "CPF do Servidor": ["52998224725", 1234567891.0, "11144477735", "123", "529.982.247-25", None],
            "Mês da Conciliação": [datetime(2026, 1, 31), "01/2026", "02/2026", "03/2026", "31/01/2026", None],
            "Descr. Motivo da Conciliação": ["desconto  ok", None, "x", "y", "Desconto OK", None],
            "Descrição da Espécie": ["mensalidade", "seguro", "seguro", "seguro", "Mensalidade", None],
            "Valor Averbado": [150.5, "1.234,56", "10", "abc", "150,50", None],
            "Número da Averbação": [" a2017090500000872 ", 123456.0, "x", "y", "z", None],
        }
    )
    return padronizar_colunas(planilha)


# ------------------------------------------------------------------ normalizadores


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("  joão   da  silva ", "JOAO DA SILVA"),
        ("Débito Efetuado", "DEBITO EFETUADO"),
        ("Conceição Ação Pão", "CONCEICAO ACAO PAO"),
        ("", None),
        (None, None),
        (float("nan"), None),
    ],
)
def test_normalizar_texto(entrada, esperado):
    assert normalizar_texto(entrada) == esperado


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("52998224725", "529.982.247-25"),
        ("529.982.247-25", "529.982.247-25"),
        (52998224725, "529.982.247-25"),
        (1234567890.0, "012.345.678-90"),  # Excel comeu o zero à esquerda
        ("123456789012", None),
        ("123", None),
        ("", None),
        (None, None),
    ],
)
def test_normalizar_cpf(entrada, esperado):
    assert normalizar_cpf(entrada) == esperado


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("Assistência Odontológica", "CONVENIO ODONTOLOGICO"),
        ("ASSISTENCIA ODONTOLOGICA", "CONVENIO ODONTOLOGICO"),
        ("Assistência Médica", "CONVENIO MEDICO"),
        ("  assistencia   medica hospitalar ", "CONVENIO MEDICO"),
        ("Seguro de Vida", "SEGURO"),
        ("SEGURO DE VIDA EM GRUPO", "SEGURO"),
        ("Mensalidade", "MENSALIDADE"),
        ("", None),
    ],
)
def test_normalizar_tipo_cobranca(entrada, esperado):
    assert normalizar_tipo_cobranca(entrada) == esperado


def test_cpf_digito_valido():
    assert cpf_digito_valido("529.982.247-25")
    assert not cpf_digito_valido("529.982.247-26")
    assert not cpf_digito_valido("111.111.111-11")


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        (datetime(2026, 1, 31, 15, 30), "2026-01-31T00:00:00Z"),
        (pd.Timestamp("2026-01-31"), "2026-01-31T00:00:00Z"),
        ("31/01/2026", "2026-01-31T00:00:00Z"),
        ("01/2026", "2026-01-01T00:00:00Z"),
        ("2026-01", "2026-01-01T00:00:00Z"),
        ("2026-01-31 00:00:00", "2026-01-31T00:00:00Z"),
        ("jan/2026", "2026-01-01T00:00:00Z"),
        ("Janeiro de 2026", "2026-01-01T00:00:00Z"),
        (46053, "2026-01-31T00:00:00Z"),  # serial do Excel
        ("31/02/2026", None),
        ("abc", None),
        (None, None),
    ],
)
def test_normalizar_data(entrada, esperado):
    assert normalizar_data(entrada) == esperado


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        (150.5, 150.5),
        (10, 10.0),
        ("1.234,56", 1234.56),
        ("R$ 1.234,56", 1234.56),
        ("1,234.56", 1234.56),
        ("150,50", 150.5),
        ("1234.5", 1234.5),
        ("1.234.567", 1234567.0),
        ("-10,00", -10.0),
        ("abc", None),
        ("", None),
        (float("nan"), None),
    ],
)
def test_normalizar_valor(entrada, esperado):
    assert normalizar_valor(entrada) == esperado


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("A20170905000008724929", "A20170905000008724929"),
        (" a2017 ", "A2017"),
        (123456.0, "123456"),
        (123456, "123456"),
        (None, None),
        (float("nan"), None),
    ],
)
def test_normalizar_averbacao(entrada, esperado):
    assert normalizar_averbacao(entrada) == esperado


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("08/2026", "08/2026"),
        ("8/2026", "08/2026"),
        ("31/01/2026", "01/2026"),
        ("jan/2026", "01/2026"),
        (datetime(2026, 12, 5), "12/2026"),
        (46054, "02/2026"),  # serial do Excel = 01/02/2026
    ],
)
def test_mes_conciliacao(entrada, esperado):
    assert mes_conciliacao(normalizar_data(entrada)) == esperado


def test_mes_conciliacao_vazio():
    assert mes_conciliacao(None) is None


# ------------------------------------------------------------------ colunas


def test_padronizar_colunas_tolera_acento_e_caixa():
    df = pd.DataFrame(columns=[
        "NOME DO SERVIDOR", "cpf do servidor", "Mes da Conciliacao",
        "Descr.  Motivo da Conciliação", "Descrição da Espécie", "Valor Averbado", "numero da averbacao", "Extra",
    ])
    assert list(padronizar_colunas(df).columns) == [
        "nome", "cpf", "data", "motivo", "tipo_cobranca", "valor", "averbacao",
    ]


def test_padronizar_colunas_faltando():
    with pytest.raises(ErroPlanilha, match="Valor Averbado"):
        padronizar_colunas(pd.DataFrame(columns=["Nome do Servidor"]))


def test_ler_csv_com_ponto_e_virgula_e_latin1():
    csv = (
        "Nome do Servidor;CPF do Servidor;Mês da Conciliação;Descr. Motivo da Conciliação;"
        "Descrição da Espécie;Valor Averbado;Número da Averbação\n"
        "José;01234567890;01/2026;ok;seguro;10,00;A1\n"
    ).encode("latin-1")
    df = ler_planilha(csv, "arquivo.csv")
    assert df.loc[0, "nome"] == "José"
    assert df.loc[0, "cpf"] == "01234567890"  # zeros à esquerda preservados


def test_ler_xlsx(tmp_path):
    caminho = tmp_path / "p.xlsx"
    pd.DataFrame(
        {
            "Nome do Servidor": ["Ana"],
            "CPF do Servidor": ["52998224725"],
            "Mês da Conciliação": [datetime(2026, 1, 31)],
            "Descr. Motivo da Conciliação": ["ok"],
            "Descrição da Espécie": ["seguro"],
            "Valor Averbado": [99.9],
            "Número da Averbação": ["A1"],
        }
    ).to_excel(caminho, index=False)
    df = ler_planilha(caminho.read_bytes(), "p.xlsx")
    assert len(df) == 1 and df.loc[0, "valor"] == 99.9


# ------------------------------------------------------------------ pipeline


def test_tratar_dataframe(df_exemplo):
    r = tratar_dataframe(df_exemplo, importacao_id="imp123")

    # linha totalmente vazia (a última) é ignorada
    assert r.total_lidas == 5
    assert len(r.registros) == 2
    assert len(r.descartes) == 3

    linha, primeiro = r.registros[0]
    assert linha == 2
    assert primeiro == {
        "nome": "JOAO DA SILVA",
        "cpf": "529.982.247-25",
        "data": "2026-01-31T00:00:00Z",
        "mes_conciliacao": "01/2026",
        "motivo": "DESCONTO OK",
        "tipo_cobranca": "MENSALIDADE",
        "valor": 150.5,
        "averbacao": "A2017090500000872",
        "importacao": "imp123",
    }

    # motivo vazio é omitido, não enviado como null
    _, segundo = r.registros[1]
    assert "motivo" not in segundo
    assert segundo["cpf"] == "012.345.678-91"
    assert segundo["valor"] == 1234.56
    assert segundo["averbacao"] == "123456"

    motivos = {d["linha"]: d["motivo"] for d in r.descartes}
    assert motivos[4] == "nome vazio"
    assert "cpf inválido" in motivos[5] and "valor inválido" in motivos[5]
    assert motivos[6] == "duplicada da linha 2"


def test_tratar_dataframe_sem_importacao(df_exemplo):
    r = tratar_dataframe(df_exemplo, importacao_id=None)
    assert all("importacao" not in reg for _, reg in r.registros)


def test_tratar_dataframe_valida_digito_cpf(df_exemplo):
    r = tratar_dataframe(df_exemplo, validar_digito_cpf=True)
    motivos = {d["linha"]: d["motivo"] for d in r.descartes}
    assert "dígito verificador" in motivos[3]  # 012.345.678-91 é inválido
