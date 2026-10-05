import io
from datetime import datetime

import httpx
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import main
from app.arquivo import ErroArquivo, baixar_arquivo, normalizar_url, validar_host
from app.bubble_client import ResultadoGravacao
from app.config import Settings


class FakeBubble:
    """Simula o BubbleClient e guarda o que foi gravado e os PATCHes na Importacao."""

    def __init__(self, erros_gravacao=None, falhar=False):
        self.itens = []
        self.patches = []
        self.erros_gravacao = erros_gravacao or []
        self.falhar = falhar

    def __call__(self):  # a rota recebe uma fábrica; a própria instância serve
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def gravar(self, itens):
        if self.falhar:
            raise RuntimeError("boom")
        self.itens = itens
        return ResultadoGravacao(sucesso=len(itens) - len(self.erros_gravacao), erros=self.erros_gravacao)

    def atualizar(self, url, campos):
        self.patches.append((url, campos))


def _planilha(nomes):
    planilha = io.BytesIO()
    pd.DataFrame(
        {
            "Nome do Servidor": nomes,
            "CPF do Servidor": ["52998224725"] * len(nomes),
            "Mês da Conciliação": [datetime(2026, 1, 31)] * len(nomes),
            "Descr. Motivo da Conciliação": ["ok"] * len(nomes),
            "Descrição da Espécie": ["seguro"] * len(nomes),
            "Valor Averbado": list(range(10, 10 * (len(nomes) + 1), 10)),
            "Número da Averbação": [f"A{i}" for i in range(len(nomes))],
        }
    ).to_excel(planilha, index=False)
    return planilha.getvalue()


@pytest.fixture
def montar(monkeypatch):
    settings = Settings(api_key="segredo", bubble_base_url="https://x/api/1.1/obj", bubble_api_token="t")

    def _montar(nomes=("Ana", ""), arquivo=None, **fake_kwargs):
        if arquivo is None:
            conteudo = _planilha(list(nomes))
            monkeypatch.setattr(main, "baixar_arquivo", lambda *a, **k: (conteudo, "p.xlsx"))
        else:
            monkeypatch.setattr(main, "baixar_arquivo", arquivo)
        fake = FakeBubble(**fake_kwargs)
        main.app.dependency_overrides[main.settings_dep] = lambda: settings
        main.app.dependency_overrides[main.bubble_factory_dep] = lambda: fake
        return TestClient(main.app), fake

    yield _montar
    main.app.dependency_overrides.clear()


def _post(client, importacao_id="1791222659057x452330323960370400"):
    corpo = {"file_url": "//s3.amazonaws.com/a.xlsx"}
    if importacao_id is not None:
        corpo["importacao_id"] = importacao_id
    return client.post("/importar", json=corpo, headers={"X-API-Key": "segredo"})


def test_sem_api_key(montar):
    client, _ = montar()
    r = client.post("/importar", json={"file_url": "//s3.amazonaws.com/a.xlsx", "importacao_id": "1791222659057x452330323960370400"})
    assert r.status_code == 401


def test_importacao_id_obrigatorio(montar):
    client, _ = montar()
    assert _post(client, importacao_id=None).status_code == 422


@pytest.mark.parametrize("invalido", ["", "imp1", "../conciliacao/123x456", "123x456/extra", "123x456?x=1"])
def test_importacao_id_com_formato_invalido(montar, invalido):
    client, fake = montar()
    assert _post(client, importacao_id=invalido).status_code == 422
    assert fake.patches == [] and fake.itens == []


def test_importar_responde_202_e_grava_em_segundo_plano(montar):
    client, fake = montar()
    r = _post(client)

    assert r.status_code == 202
    dados = r.json()
    assert dados["status"] == "processando"
    assert dados["total_lidas"] == 2
    assert dados["validas"] == 1
    assert dados["descartadas"] == 1
    assert dados["descartes"] == [{"linha": 3, "motivo": "nome vazio"}]

    assert len(fake.itens) == 1
    assert fake.itens[0][1]["importacao"] == "1791222659057x452330323960370400"

    url, inicio = fake.patches[0]
    assert url == "https://x/api/1.1/obj/importacao/1791222659057x452330323960370400"
    assert inicio == {"status": "processando", "total_lidas": 2, "validas": 1, "descartadas": 1}
    assert fake.patches[-1][1] == {"status": "concluida", "gravadas_sucesso": 1, "gravadas_erro": 0}


def test_importar_com_erros_de_gravacao(montar):
    client, fake = montar(nomes=("Ana", "Bia"), erros_gravacao=[{"linha": 3, "mensagem": "campo inválido"}])
    assert _post(client).status_code == 202

    final = fake.patches[-1][1]
    assert final["status"] == "concluida_com_erros"
    assert final["gravadas_sucesso"] == 1 and final["gravadas_erro"] == 1
    assert final["mensagem"] == "linha 3: campo inválido"


def test_importar_falha_inesperada_marca_erro(montar):
    client, fake = montar(falhar=True)
    assert _post(client).status_code == 202
    assert fake.patches[-1][1] == {"status": "erro", "mensagem": "Falha na gravação: boom"}


def test_importar_sem_registros_validos_conclui_sem_gravar(montar):
    client, fake = montar(nomes=("",))
    r = _post(client)
    assert r.status_code == 202 and r.json()["status"] == "concluida"
    assert fake.itens == []
    assert fake.patches == [
        ("https://x/api/1.1/obj/importacao/1791222659057x452330323960370400",
         {"status": "concluida", "total_lidas": 1, "validas": 0, "descartadas": 1, "gravadas_sucesso": 0, "gravadas_erro": 0}),
    ]


def test_arquivo_invalido_responde_422_e_marca_erro(montar):
    def falha(*a, **k):
        raise ErroArquivo("Host não permitido para download: evil.com")

    client, fake = montar(arquivo=falha)
    r = _post(client)
    assert r.status_code == 422
    assert fake.patches == [
        ("https://x/api/1.1/obj/importacao/1791222659057x452330323960370400",
         {"status": "erro", "mensagem": "Host não permitido para download: evil.com"}),
    ]


def test_resumir_erros_limita_tamanho():
    erros = [{"linha": i, "mensagem": "x"} for i in range(main.MAX_ERROS_MENSAGEM + 5)]
    texto = main.resumir_erros(erros)
    assert texto.count("\n") == main.MAX_ERROS_MENSAGEM
    assert texto.endswith("... e mais 5 erro(s)")


def test_normalizar_url():
    assert normalizar_url("//s3.amazonaws.com/a/b.xlsx") == "https://s3.amazonaws.com/a/b.xlsx"
    assert normalizar_url("https://x.com/a") == "https://x.com/a"
    assert normalizar_url("http://x.com/a") == "https://x.com/a"  # nunca baixa sem TLS
    for vazio in ("", "  ", "null", "NULL", "undefined"):
        with pytest.raises(ErroArquivo, match="file_url vazio"):
            normalizar_url(vazio)


def test_validar_host():
    validar_host("https://s3.amazonaws.com/a", ["amazonaws.com"])
    validar_host("https://abc.cdn.bubble.io/a", ["cdn.bubble.io"])
    with pytest.raises(ErroArquivo):
        validar_host("https://evil.com/a", ["amazonaws.com"])
    with pytest.raises(ErroArquivo):
        validar_host("https://amazonaws.com.evil.com/a", ["amazonaws.com"])
    with pytest.raises(ErroArquivo, match="HTTPS"):
        validar_host("http://s3.amazonaws.com/a", ["amazonaws.com"])


def _cliente_http(rotas):
    """httpx.Client falso: rotas = {url: httpx.Response}."""
    def handler(req):
        return rotas[str(req.url)]
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_baixar_arquivo_segue_redirect_para_host_permitido():
    cliente = _cliente_http({
        "https://a.cdn.bubble.io/f/p.xlsx": httpx.Response(302, headers={"Location": "https://s3.amazonaws.com/b/p.xlsx"}),
        "https://s3.amazonaws.com/b/p.xlsx": httpx.Response(200, content=b"ok"),
    })
    conteudo, nome = baixar_arquivo("//a.cdn.bubble.io/f/p.xlsx", ["cdn.bubble.io", "amazonaws.com"], http_client=cliente)
    assert conteudo == b"ok" and nome == "p.xlsx"


def test_baixar_arquivo_bloqueia_redirect_para_host_proibido():
    cliente = _cliente_http({
        "https://a.cdn.bubble.io/f/p.xlsx": httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"}),
    })
    with pytest.raises(ErroArquivo):
        baixar_arquivo("//a.cdn.bubble.io/f/p.xlsx", ["cdn.bubble.io"], http_client=cliente)


def test_baixar_arquivo_limita_redirects():
    cliente = _cliente_http({
        "https://a.cdn.bubble.io/loop": httpx.Response(302, headers={"Location": "https://a.cdn.bubble.io/loop"}),
    })
    with pytest.raises(ErroArquivo, match="Redirecionamentos demais"):
        baixar_arquivo("//a.cdn.bubble.io/loop", ["cdn.bubble.io"], http_client=cliente)


def test_baixar_arquivo_limita_tamanho():
    cliente = _cliente_http({"https://a.cdn.bubble.io/g": httpx.Response(200, content=b"x" * (1024 * 1024 + 1))})
    with pytest.raises(ErroArquivo, match="limite"):
        baixar_arquivo("//a.cdn.bubble.io/g", ["cdn.bubble.io"], max_mb=1, http_client=cliente)


def test_docs_desligado_por_padrao(monkeypatch):
    monkeypatch.delenv("EXPOR_DOCS", raising=False)
    assert Settings(api_key="k", bubble_base_url="https://x", bubble_api_token="t").expor_docs is False
