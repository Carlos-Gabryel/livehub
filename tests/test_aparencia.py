import json

import aiohttp
import pytest

from livehub import settings
from livehub.runtime import Runtime
from conftest import porta_livre


@pytest.fixture
async def rt(data_dir):
    cfg = settings.load_config()
    cfg["web_port"] = porta_livre()
    settings.save_config(cfg)
    r = Runtime()
    await r.start()
    r.reload_n = 0
    orig = r.reload

    async def contado():
        r.reload_n += 1
        await orig()
    r.reload = contado
    yield r
    await r.stop()


def b(rt):
    return f"http://127.0.0.1:{rt.cfg['web_port']}"


async def post(rt, rota, corpo, **kw):
    async with aiohttp.ClientSession() as s:
        async with s.post(b(rt) + rota, json=corpo, **kw) as r:
            return r.status, await r.json(content_type=None) if r.status != 403 else None


async def get_json(rt, rota="/api/aparencia"):
    async with aiohttp.ClientSession() as s:
        async with s.get(b(rt) + rota) as r:
            return await r.json()


async def test_padroes_e_css(rt):
    j = await get_json(rt)
    assert j["dock"] == settings.DEFAULT_APARENCIA["dock"] == j["defaults"]["dock"]
    assert j["neutro"]["fonte"] == "Fira Code" and j["neutro"]["max"] == 15
    assert "Fira Code" in j["fontes_sugeridas"]
    async with aiohttp.ClientSession() as s:
        for alvo in ("dock", "neutro"):
            async with s.get(b(rt) + f"/aparencia/{alvo}.css") as r:
                assert r.status == 200 and "text/css" in r.headers["Content-Type"]
                assert r.headers["Cache-Control"] == "no-store"
                css = await r.text()
                for var in ("--fonte:", "--tam:", "--fundo:", "--msg-bg:", "--texto:", "--esp:", "--raio:", "--sombra:"):
                    assert var in css
        async with s.get(b(rt) + "/aparencia/dock.css") as r:
            assert "--tam:14px" in await r.text()
        async with s.get(b(rt) + "/aparencia/neutro.css") as r:
            css = await r.text()
            assert "--tam:22px" in css and "--fundo:rgba(220,235,255,1.00)" in css and ".ic{display:none" in css


async def test_alvo_desconhecido_404(rt):
    async with aiohttp.ClientSession() as s:
        async with s.get(b(rt) + "/aparencia/outro.css") as r:
            assert r.status == 404


async def test_previa_por_query(rt):
    async with aiohttp.ClientSession() as s:
        async with s.get(b(rt) + "/aparencia/dock.css", params={"v": json.dumps({"tamanho": 30})}) as r:
            assert r.status == 200 and "--tam:30px" in await r.text()
        async with s.get(b(rt) + "/aparencia/dock.css", params={"v": json.dumps({"tamanho": 3})}) as r:
            assert r.status == 400
    assert settings.load_config()["aparencia"]["dock"]["tamanho"] == 14


@pytest.mark.parametrize("valores,campo", [
    ({"cor_fundo": "red"}, "cor_fundo"),
    ({"cor_texto": "#12345"}, "cor_texto"),
    ({"opacidade_fundo": 101}, "opacidade_fundo"),
    ({"tamanho": 9}, "tamanho"),
    ({"tamanho": 49}, "tamanho"),
    ({"contorno": "neon"}, "contorno"),
    ({"animacao": "pular"}, "animacao"),
    ({"cor_nome_modo": "x"}, "cor_nome_modo"),
    ({"fonte": "Arial;}body{x:y"}, "fonte"),
    ({"fonte": ""}, "fonte"),
    ({"negrito": "sim"}, "negrito"),
    ({"max": 301}, "max"),
    ({"sumir": 601}, "sumir"),
    ({"espacamento": 21}, "espacamento"),
    ({"raio": 25}, "raio"),
    ({"inexistente": 1}, "inexistente"),
])
async def test_validacao_400(rt, valores, campo):
    st, j = await post(rt, "/api/aparencia", {"alvo": "dock", "valores": valores})
    assert st == 400 and j["campo"] == campo and j["erro"]
    assert settings.load_config()["aparencia"]["dock"] == settings.DEFAULT_APARENCIA["dock"]


async def test_alvo_invalido_no_post(rt):
    st, _ = await post(rt, "/api/aparencia", {"alvo": "x", "valores": {}})
    assert st == 400


async def test_atualizacao_parcial_persiste(rt):
    st, j = await post(rt, "/api/aparencia", {"alvo": "neutro", "valores": {"tamanho": 30, "cor_texto": "#FFFFFF"}})
    assert st == 200 and j["neutro"]["tamanho"] == 30
    cfg = settings.load_config()["aparencia"]
    assert cfg["neutro"]["tamanho"] == 30 and cfg["neutro"]["cor_texto"] == "#ffffff"
    assert cfg["neutro"]["fonte"] == "Fira Code" and cfg["neutro"]["max"] == 15
    assert cfg["dock"] == settings.DEFAULT_APARENCIA["dock"]
    st, j = await post(rt, "/api/aparencia", {"alvo": "neutro", "valores": {"raio": "8"}})
    assert st == 200
    cfg = settings.load_config()["aparencia"]["neutro"]
    assert cfg["tamanho"] == 30 and cfg["raio"] == 8


async def test_reset(rt):
    await post(rt, "/api/aparencia", {"alvo": "dock", "valores": {"tamanho": 40, "contorno": "sombra"}})
    st, j = await post(rt, "/api/aparencia/reset", {"alvo": "dock"})
    assert st == 200 and j["dock"] == settings.DEFAULT_APARENCIA["dock"]
    assert settings.load_config()["aparencia"]["dock"] == settings.DEFAULT_APARENCIA["dock"]


async def test_broadcast_sem_reload(rt):
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(b(rt) + "/dock/ws") as ws:
            assert (await ws.receive_json())["type"] == "history"
            st, _ = await post(rt, "/api/aparencia", {"alvo": "neutro", "valores": {"tamanho": 26}})
            assert st == 200
            m = await ws.receive_json()
            assert m["tipo"] == "aparencia" and m["alvo"] == "neutro" and m["valores"]["tamanho"] == 26
            await post(rt, "/api/aparencia/reset", {"alvo": "neutro"})
            m = await ws.receive_json()
            assert m["tipo"] == "aparencia" and m["valores"]["tamanho"] == 22
            # mensagens de chat normais continuam chegando no mesmo canal
            await rt.hub.publish({"platform": "twitch", "user": "ana", "parts": [{"t": "text", "v": "oi"}]})
            m = await ws.receive_json()
            assert m["type"] == "msg" and m["msg"]["user"] == "ana"
    assert rt.reload_n == 0


async def test_origem_estranha_403(rt):
    st, _ = await post(rt, "/api/aparencia", {"alvo": "dock", "valores": {"tamanho": 20}},
                       headers={"Origin": "http://evil.example"})
    assert st == 403
    st, _ = await post(rt, "/api/aparencia/reset", {"alvo": "dock"}, headers={"Origin": "http://evil.example"})
    assert st == 403
    assert settings.load_config()["aparencia"]["dock"]["tamanho"] == 14


async def test_paginas_servem_demo_e_css(rt):
    async with aiohttp.ClientSession() as s:
        for alvo in ("dock", "neutro"):
            async with s.get(b(rt) + f"/{alvo}", params={"demo": "1"}) as r:
                t = await r.text()
                assert r.status == 200 and f"/aparencia/{alvo}.css" in t and "tipo:'preview'" not in t and "demo" in t
