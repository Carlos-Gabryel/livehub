"""Login da Twitch por Device Code (client publico, sem segredo)."""
import asyncio, time

import aiohttp
import pytest
from aiohttp import web

from livehub import settings
from livehub.chat.twitch import Twitch
from livehub.resgates import const
from livehub.resgates.auth import Auth
from livehub.runtime import Runtime
from conftest import porta_livre

DC = "DEVICE-CODE-SECRETO"


class Fake:
    """Servidor falso do id.twitch.tv."""

    def __init__(self):
        self.token_respostas = []  # (status, json) consumidas em ordem pelo /oauth2/token do device
        self.refresh_respostas = []
        self.hits_device, self.token_posts, self.refresh_posts = 0, [], []
        self.refresh_delay = 0
        app = web.Application()
        app.router.add_post("/oauth2/device", self.device)
        app.router.add_post("/oauth2/token", self.token)
        app.router.add_get("/oauth2/validate", self.validate)
        self.app = app

    async def device(self, req):
        self.hits_device += 1
        d = dict(await req.post())
        assert d["client_id"] == const.LIVEHUB_CLIENT_ID and "channel:read:redemptions" in d["scopes"]
        return web.json_response({"device_code": DC, "expires_in": 1800, "interval": 5,
                                  "user_code": "ABCDEFGH",
                                  "verification_uri": "https://www.twitch.tv/activate?public=true&device-code=ABCDEFGH"})

    async def token(self, req):
        d = dict(await req.post())
        if d["grant_type"] == "refresh_token":
            self.refresh_posts.append(d)
            await asyncio.sleep(self.refresh_delay)
            st, j = self.refresh_respostas.pop(0)
            return web.json_response(j, status=st)
        self.token_posts.append(d)
        st, j = self.token_respostas.pop(0) if self.token_respostas else (400, {"message": "authorization_pending"})
        return web.json_response(j, status=st)

    async def validate(self, req):
        return web.json_response({"user_id": "77", "login": "amigo",
                                  "scopes": const.SCOPES.split()})


OK = (200, {"access_token": "AT-1", "refresh_token": "RT-1", "expires_in": 14000, "scope": []})


@pytest.fixture
async def amb(monkeypatch):
    f = Fake()
    runner = web.AppRunner(f.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    monkeypatch.setattr(const, "TWITCH_ID", base)
    monkeypatch.setattr(const, "TWITCH_DEVICE", base + "/oauth2/device")
    async with aiohttp.ClientSession() as s:
        a = Auth(s)
        a.dormidas = []

        async def dormir(t):
            a.dormidas.append(t)
            await asyncio.sleep(0)
        a._sleep = dormir
        a.reloads = 0

        async def rec():
            a.reloads += 1
        a.on_reload = rec
        yield f, a
    await runner.cleanup()


async def espera(cond, t=5):
    fim = time.time() + t
    while time.time() < fim:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timeout")


def seg():
    return settings.load_secrets()["twitch"]


async def test_inicio_devolve_campos_sem_device_code(amb):
    f, a = amb
    r = await a.iniciar_dispositivo()
    assert r["user_code"] == "ABCDEFGH" and r["expires_in"] == 1800 and "activate" in r["verification_uri"]
    assert DC not in str(r) and DC not in str(a.estado_dispositivo())
    await a.cancelar_dispositivo()


async def test_poll_pendente_depois_sucesso(amb):
    f, a = amb
    f.token_respostas = [(400, {"message": "authorization_pending"}), OK]
    await a.iniciar_dispositivo()
    await espera(lambda: a.estado_dispositivo()["estado"] == "conectado")
    assert a.estado_dispositivo() == {"estado": "conectado", "login": "amigo"}
    s = seg()
    assert s["access_token"] == "AT-1" and s["refresh_token"] == "RT-1" and s["user_id"] == "77" and s["modo"] == "dispositivo"
    assert a.reloads == 1 and a.on_login.is_set()
    assert all("client_secret" not in p for p in f.token_posts)
    assert f.token_posts[0]["device_code"] == DC and f.token_posts[0]["grant_type"] == const.DEVICE_GRANT


async def test_slow_down_aumenta_intervalo(amb):
    f, a = amb
    f.token_respostas = [(400, {"message": "slow_down"}), OK]
    await a.iniciar_dispositivo()
    await espera(lambda: a.estado_dispositivo()["estado"] == "conectado")
    assert a.dormidas[:2] == [5, 10]


@pytest.mark.parametrize("msg,estado", [("invalid device code", "expirado"), ("access_denied", "negado"),
                                         ("algo estranho", "erro")])
async def test_estados_finais(amb, msg, estado):
    f, a = amb
    f.token_respostas = [(400, {"message": msg})]
    await a.iniciar_dispositivo()
    await espera(lambda: a.estado_dispositivo()["estado"] == estado)
    assert not a.logado


async def test_expira_pelo_relogio(amb):
    f, a = amb
    await a.iniciar_dispositivo()
    a._disp["expira"] = time.time() - 1
    await espera(lambda: a.estado_dispositivo()["estado"] == "expirado")


async def test_so_um_fluxo_ativo(amb):
    f, a = amb
    await a.iniciar_dispositivo()
    t1 = a._disp_task
    await a.iniciar_dispositivo()
    assert t1.done() and a._disp_task is not t1 and not a._disp_task.done()
    assert f.hits_device == 2
    await a.cancelar_dispositivo()
    assert a.estado_dispositivo()["estado"] == "inativo" and a._disp_task is None


async def test_refresh_dispositivo_sem_secret_e_rotaciona(amb):
    f, a = amb
    sec = settings.load_secrets()
    sec["twitch"].update(modo="dispositivo", access_token="velho", refresh_token="RT-0",
                         expires_at=time.time() - 5, user_id="77", login="amigo")
    settings.save_secrets(sec)
    f.refresh_respostas = [(200, {"access_token": "AT-2", "refresh_token": "RT-2", "expires_in": 14000})]
    assert await a.access_token() == "AT-2"
    p = f.refresh_posts[0]
    assert "client_secret" not in p and p["client_id"] == const.LIVEHUB_CLIENT_ID and p["refresh_token"] == "RT-0"
    assert seg()["refresh_token"] == "RT-2"


async def test_refresh_concorrente_bate_uma_vez(amb):
    f, a = amb
    sec = settings.load_secrets()
    sec["twitch"].update(modo="dispositivo", access_token="", refresh_token="RT-0", expires_at=0)
    settings.save_secrets(sec)
    f.refresh_delay = 0.1
    f.refresh_respostas = [(200, {"access_token": "AT-2", "refresh_token": "RT-2", "expires_in": 14000})]
    r = await asyncio.gather(*(a.access_token() for _ in range(5)))
    assert r == ["AT-2"] * 5 and len(f.refresh_posts) == 1


async def test_refresh_invalido_limpa_e_marca_expirado(amb):
    f, a = amb
    sec = settings.load_secrets()
    sec["twitch"].update(modo="dispositivo", access_token="", refresh_token="RT-0", expires_at=0,
                         user_id="77", login="amigo")
    settings.save_secrets(sec)
    f.refresh_respostas = [(400, {"message": "Invalid refresh token"})]
    with pytest.raises(RuntimeError):
        await a.access_token()
    s = seg()
    assert s["refresh_token"] == "" and s["access_token"] == "" and s["login_expirado"] is True
    assert not a.logado and a.login_expirado
    f.token_respostas = [OK]
    await a.iniciar_dispositivo()
    await espera(lambda: a.estado_dispositivo()["estado"] == "conectado")
    assert seg()["login_expirado"] is False


def test_migracao_modo(data_dir):
    import json
    (data_dir / "secrets.json").write_text(json.dumps(
        {"twitch": {"client_id": "x", "client_secret": "y"}}), encoding="utf-8")
    assert settings.load_secrets()["twitch"]["modo"] == "app_proprio"
    (data_dir / "secrets.json").write_text(json.dumps({"twitch": {"client_id": "x"}}), encoding="utf-8")
    assert settings.load_secrets()["twitch"]["modo"] == "dispositivo"
    (data_dir / "secrets.json").write_text(json.dumps(
        {"twitch": {"client_id": "x", "client_secret": "y", "modo": "dispositivo"}}), encoding="utf-8")
    assert settings.load_secrets()["twitch"]["modo"] == "dispositivo"
    rc = settings.runtime_config()["twitch"]
    assert rc["client_id"] == const.LIVEHUB_CLIENT_ID and rc["client_secret"] == ""


async def test_troca_de_modo_limpa_tokens(amb):
    f, a = amb
    sec = settings.load_secrets()
    sec["twitch"].update(modo="app_proprio", client_id="c", client_secret="s", access_token="A",
                         refresh_token="R", expires_at=time.time() + 99, user_id="1", login="x")
    settings.save_secrets(sec)
    assert await a.definir_modo("app_proprio") is False and a.logado
    assert await a.definir_modo("dispositivo") is True
    s = seg()
    assert s["modo"] == "dispositivo" and s["refresh_token"] == "" and s["user_id"] == "" and s["login"] == ""
    assert s["client_id"] == "c"  # credenciais do app proprio ficam guardadas
    with pytest.raises(ValueError):
        await a.definir_modo("xyz")


# ---- badges do chat ----
class SessBadges:
    def __init__(self):
        self.reqs = []

    def get(self, url, **kw):
        self.reqs.append((url, kw.get("headers", {})))

        class R:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *e):
                return False

            def raise_for_status(s):
                pass

            async def json(s, content_type=None):
                return {"data": []}
        return R()


async def test_badges_usam_token_do_usuario_no_modo_dispositivo():
    s, n = SessBadges(), {"v": 0}

    async def tok():
        n["v"] += 1
        return f"USER-TOK-{n['v']}"
    cfg = {"twitch": {"channel": "x", "client_id": const.LIVEHUB_CLIENT_ID, "client_secret": "", "token_fn": tok}}
    tw = Twitch(cfg, None, s)
    assert tw.helix.enabled
    await tw.helix.load_badges("x")
    auths = [h["Authorization"] for _, h in s.reqs]
    assert auths[0] == "Bearer USER-TOK-1" and auths[-1] != auths[0]  # token atual a cada chamada
    assert all(h["Client-Id"] == const.LIVEHUB_CLIENT_ID for _, h in s.reqs)
    assert not Twitch({"twitch": {"channel": "x", "client_id": const.LIVEHUB_CLIENT_ID, "client_secret": ""}},
                      None, s).helix.enabled


# ---- API ----
@pytest.fixture
async def rt(data_dir, monkeypatch):
    f = Fake()
    runner = web.AppRunner(f.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    monkeypatch.setattr(const, "TWITCH_ID", base)
    monkeypatch.setattr(const, "TWITCH_DEVICE", base + "/oauth2/device")
    cfg = settings.load_config()
    cfg["web_port"] = porta_livre()
    settings.save_config(cfg)
    r = Runtime()
    await r.start()
    r.fake = f
    yield r
    await r.stop()
    await runner.cleanup()


def b(rt):
    return f"http://127.0.0.1:{rt.cfg['web_port']}"


async def test_api_fluxo_e_sem_vazamento(rt):
    rt.fake.token_respostas = [OK]
    async with aiohttp.ClientSession() as s:
        async with s.post(b(rt) + "/api/twitch/dispositivo/iniciar", json={}) as r:
            assert r.status == 200
            txt1 = await r.text()
        assert "ABCDEFGH" in txt1 and DC not in txt1
        rt.svc.auth._sleep = lambda t: asyncio.sleep(0)
        for _ in range(200):
            async with s.get(b(rt) + "/api/twitch/dispositivo/estado") as r:
                j = await r.json()
                txt2 = await r.text()
            if j["estado"] == "conectado":
                break
            await asyncio.sleep(0.05)
        assert j == {"estado": "conectado", "login": "amigo"}
        async with s.get(b(rt) + "/api/config") as r:
            cfg = await r.json()
            txt3 = await r.text()
        async with s.get(b(rt) + "/api/status") as r:
            txt4 = await r.text()
    assert cfg["twitch"]["modo"] == "dispositivo" and cfg["twitch"]["login_expirado"] is False
    for t in (txt2, txt3, txt4):
        for v in (DC, "AT-1", "RT-1"):
            assert v not in t


async def test_api_modo_e_origem(rt):
    async with aiohttp.ClientSession() as s:
        async with s.post(b(rt) + "/api/twitch/modo", json={"modo": "app_proprio"}) as r:
            assert r.status == 400  # sem client_id/secret salvos
        async with s.post(b(rt) + "/api/twitch/modo", json={"modo": "dispositivo"}) as r:
            assert r.status == 200 and (await r.json())["modo"] == "dispositivo"
        for u in ("/api/twitch/dispositivo/iniciar", "/api/twitch/dispositivo/cancelar", "/api/twitch/modo"):
            async with s.post(b(rt) + u, json={"modo": "dispositivo"}, headers={"Origin": "http://evil.com"}) as r:
                assert r.status == 403
        async with s.post(b(rt) + "/api/twitch/dispositivo/cancelar", json={}) as r:
            assert r.status == 200
    assert rt.fake.hits_device == 0
