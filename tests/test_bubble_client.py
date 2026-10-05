import json

import httpx

from app.bubble_client import BubbleClient

URL = "https://app.test/version-test/api/1.1/obj/conciliacao/bulk"


def _cliente(handler, **kwargs) -> tuple[BubbleClient, list[float]]:
    esperas: list[float] = []
    cliente = BubbleClient(
        URL,
        token="tok",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=esperas.append,
        backoff_base=0.01,
        **kwargs,
    )
    return cliente, esperas


def test_montar_corpo_ndjson_utf8():
    corpo = BubbleClient.montar_corpo([{"nome": "JOÃO"}, {"valor": 1.5}])
    assert corpo.decode("utf-8") == '{"nome": "JOÃO"}\n{"valor": 1.5}'


def test_gravar_em_lotes_e_parse_da_resposta():
    chamadas = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "Bearer tok"
        assert req.headers["Content-Type"].startswith("text/plain")
        linhas = req.content.decode("utf-8").split("\n")
        chamadas.append(len(linhas))
        resp = []
        for linha in linhas:
            reg = json.loads(linha)
            if reg["n"] == 3:
                resp.append(json.dumps({"status": "error", "message": "campo inválido"}))
            else:
                resp.append(json.dumps({"status": "success", "id": f"id{reg['n']}"}))
        return httpx.Response(200, text="\n".join(resp))

    cliente, _ = _cliente(handler, batch_size=2)
    itens = [(i + 2, {"n": i}) for i in range(5)]
    r = cliente.gravar(itens)

    assert chamadas == [2, 2, 1]
    assert r.sucesso == 4
    assert r.erros == [{"linha": 5, "mensagem": "campo inválido"}]
    assert "id0" in r.ids


def test_retry_em_429_e_5xx():
    respostas = iter([
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(503),
        httpx.Response(200, text='{"status":"success","id":"a"}'),
    ])
    cliente, esperas = _cliente(lambda req: next(respostas))
    r = cliente.gravar([(2, {"n": 1})])

    assert r.sucesso == 1
    assert len(esperas) == 2
    assert esperas[0] >= 2  # respeita Retry-After


def test_erro_4xx_nao_faz_retry_e_marca_lote():
    cliente, esperas = _cliente(lambda req: httpx.Response(401, text="unauthorized"))
    r = cliente.gravar([(2, {"n": 1}), (3, {"n": 2})])

    assert esperas == []
    assert r.sucesso == 0
    assert [e["linha"] for e in r.erros] == [2, 3]
    assert "401" in r.erros[0]["mensagem"]


def test_retries_esgotados():
    cliente, esperas = _cliente(lambda req: httpx.Response(500), max_retries=2)
    r = cliente.gravar([(2, {"n": 1})])
    assert len(esperas) == 2
    assert r.erros[0]["linha"] == 2


def test_resposta_com_menos_linhas():
    cliente, _ = _cliente(lambda req: httpx.Response(200, text='{"status":"success","id":"a"}'))
    r = cliente.gravar([(2, {"n": 1}), (3, {"n": 2})])
    assert r.sucesso == 1
    assert r.erros[0]["linha"] == 3


def test_atualizar_faz_patch_json_com_retry():
    chamadas = []

    def handler(req: httpx.Request) -> httpx.Response:
        chamadas.append(req)
        if len(chamadas) == 1:
            return httpx.Response(503)
        return httpx.Response(204)

    cliente, esperas = _cliente(handler)
    cliente.atualizar("https://app.test/api/1.1/obj/importacao/imp1", {"status": "concluida"})

    assert len(esperas) == 1
    req = chamadas[-1]
    assert req.method == "PATCH"
    assert req.headers["Authorization"] == "Bearer tok"
    assert req.headers["Content-Type"] == "application/json"
    assert json.loads(req.content) == {"status": "concluida"}


def test_atualizar_erro_4xx_levanta():
    import pytest

    from app.bubble_client import ErroBubble

    cliente, _ = _cliente(lambda req: httpx.Response(404, text="not found"))
    with pytest.raises(ErroBubble, match="404"):
        cliente.atualizar("https://app.test/api/1.1/obj/importacao/x", {"status": "erro"})
