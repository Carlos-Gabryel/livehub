import asyncio
import aiohttp
import pytest

from livehub import settings
from livehub.runtime import Runtime, supervisionar
from conftest import porta_livre


@pytest.fixture
async def rt(data_dir):
    cfg = settings.load_config()
    cfg["web_port"] = porta_livre()
    settings.save_config(cfg)
    r = Runtime()
    await r.start()
    yield r
    await r.stop()


def base(rt):
    return f"http://127.0.0.1:{rt.cfg['web_port']}"


async def test_app_unificado_serve_tudo(rt):
    async with aiohttp.ClientSession() as s:
        for rota in ("/", "/dock", "/eventos", "/neutro", "/recompensas", "/api/status", "/status",
                     "/api/sons", "/eventos/config", "/fonts/FiraCode-Bold.ttf"):
            async with s.get(base(rt) + rota) as r:
                assert r.status == 200, rota
        async with s.get(base(rt) + "/api/status") as r:
            j = await r.json()
            assert j["logado"] is False and j["canal"] == "" and j["configurado"] is False
        async with s.get(base(rt) + "/recompensas") as r:
            assert "Resgates" in await r.text()
        # Host estranho e barrado em qualquer rota
        for rota in ("/dock", "/recompensas", "/api/status"):
            async with s.get(base(rt) + rota, headers={"Host": "evil.com"}) as r:
                assert r.status == 403, rota
        async with s.get(base(rt) + "/api/status", headers={"Host": f"localhost:{rt.cfg['web_port']}"}) as r:
            assert r.status == 200
        async with s.get(base(rt) + "/api/recompensas") as r:  # nao logado
            assert r.status == 401


async def test_plataformas_vazias_nao_iniciam(rt):
    assert set(rt.tasks) == {"eventsub"}  # nada configurado: so o EventSub, ocioso
    assert rt.hub.status["twitch"] == "desativado" and rt.hub.status["streamelements"] == "sem token"
    assert rt.sb_runner is None  # compat do Streamer.bot desligada por padrao


class Falso:
    instancias = []

    def __init__(self, cfg, hub, session):
        Falso.instancias.append(self)

    async def run(self):
        await asyncio.sleep(3600)


async def test_reload_recria_tarefas(rt, monkeypatch):
    from livehub import runtime
    monkeypatch.setitem(runtime.PLATAFORMAS, "twitch", (Falso, runtime.PLATAFORMAS["twitch"][1]))
    antigo_eventsub = rt.tasks["eventsub"]
    cfg = settings.load_config()
    cfg["twitch"]["channel"] = "canal"
    cfg["streamerbot_compat"] = {"enabled": True, "port": porta_livre()}
    settings.save_config(cfg)
    await rt.reload()
    assert set(rt.tasks) == {"twitch", "eventsub"} and len(Falso.instancias) == 1
    assert antigo_eventsub.cancelled() or antigo_eventsub.done()
    assert rt.tasks["eventsub"] is not antigo_eventsub
    assert rt.sb_runner is not None  # compat ligada pelo reload
    # desligar tudo de novo
    cfg["twitch"]["channel"] = ""
    cfg["streamerbot_compat"]["enabled"] = False
    settings.save_config(cfg)
    await rt.reload()
    assert set(rt.tasks) == {"eventsub"} and rt.sb_runner is None


async def test_reload_via_jwt_do_streamelements(rt, monkeypatch):
    from livehub import runtime
    monkeypatch.setitem(runtime.PLATAFORMAS, "streamelements", (Falso, runtime.PLATAFORMAS["streamelements"][1]))
    async with aiohttp.ClientSession() as s:
        h = {"Content-Type": "application/x-www-form-urlencoded"}
        async with s.post(base(rt) + "/eventos/config", data="jwt=abc123", headers=h) as r:
            assert r.status == 200 and "Token salvo" in await r.text()
    assert "streamelements" in rt.tasks
    assert settings.load_secrets()["streamelements"]["jwt"] == "abc123"


async def test_supervisor_reinicia_com_backoff():
    n = []

    async def instavel():
        n.append(1)
        if len(n) < 3:
            raise RuntimeError("caiu")

    await asyncio.wait_for(supervisionar("x", instavel, base=0.01, teto=0.02), 5)
    assert len(n) == 3  # reiniciou duas vezes e terminou normalmente
