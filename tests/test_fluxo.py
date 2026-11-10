import asyncio, json, threading, time
import aiohttp, pytest
from aiohttp import web

from conftest import porta_livre
from livehub import paths, settings
from livehub.chat.hub import Hub
from livehub.resgates import audio, const, eventsub
from livehub.resgates.auth import Auth
from livehub.resgates.player import Player
from livehub.resgates.servico import Servico, add_routes
from livehub.web import WEB_PORT, seguranca

SOM = "vine-boom.mp3"


class FakeTwitch:
    """Fake de id.twitch.tv + helix + EventSub WebSocket."""
    def __init__(self):
        self.token_calls, self.subs, self.ws_conexoes, self.api_401 = [], [], 0, 0
        self.script = None  # async fn(ws, n_conexao, path)
        self.app = web.Application()
        self.app.router.add_post("/oauth2/token", self.token)
        self.app.router.add_post("/helix/eventsub/subscriptions", self.sub)
        self.app.router.add_get("/oauth2/validate", self.validate)
        self.app.router.add_get("/helix/channel_points/custom_rewards", self.lista)
        self.escopos = ["channel:read:redemptions", "channel:manage:redemptions", "moderator:read:followers"]
        self.rewards = [{"id": "EX1", "title": "vai la !!", "cost": 1}]
        self.app.router.add_get("/ws", self.ws)
        self.app.router.add_get("/ws2", self.ws)

    async def token(self, req):
        d = await req.post()
        self.token_calls.append(dict(d))
        n = len(self.token_calls)
        return web.json_response({"access_token": f"AT{n}", "refresh_token": f"RT{n}", "expires_in": 3600})

    async def validate(self, req):
        return web.json_response({"scopes": self.escopos, "user_id": "U42", "login": "streamer"})

    async def lista(self, req):
        return web.json_response({"data": self.rewards})

    async def sub(self, req):
        if self.api_401:
            self.api_401 -= 1
            return web.json_response({"message": "Invalid OAuth token"}, status=401)
        if getattr(self, "sem_resgates", False):
            corpo = await req.json()
            if corpo["type"] == eventsub.TIPO:
                return web.json_response({"message": "must be partner or affiliate"}, status=403)
        self.subs.append({"auth": req.headers["Authorization"], "client": req.headers["Client-Id"],
                          "body": await req.json()})
        return web.json_response({"data": []}, status=202)

    async def ws(self, req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        self.ws_conexoes += 1
        await self.script(ws, self.ws_conexoes, req.path)
        return ws


def msg(tipo, payload, **meta):
    return json.dumps({"metadata": {"message_id": f"m{time.time_ns()}", "message_type": tipo, **meta},
                       "payload": payload})


def resgate(rid, titulo="Teste", user="fulano"):
    return msg("notification", {"event": {"id": "x", "user_name": user, "reward": {"id": rid, "title": titulo, "cost": 100}}},
               subscription_type=eventsub.TIPO)


def welcome(sid, ka=1):
    return msg("session_welcome", {"session": {"id": sid, "keepalive_timeout_seconds": ka}})


class FakeAudio:
    """Substitui livehub.resgates.audio.tocar/parar (sem placa de som)."""
    def __init__(self):
        self.chamadas, self.bloquear, self.liberar = [], False, threading.Event()

    def tocar(self, caminho, volume=1.0, device_name=""):
        self.chamadas.append((str(caminho), volume, device_name))
        if self.bloquear:
            self.liberar.wait(30)

    def parar(self):
        self.liberar.set()


@pytest.fixture
def faudio(monkeypatch):
    f = FakeAudio()
    monkeypatch.setattr(audio, "tocar", f.tocar)
    monkeypatch.setattr(audio, "parar", f.parar)
    return f


@pytest.fixture
async def amb(monkeypatch, faudio):
    fake = FakeTwitch()
    runner = web.AppRunner(fake.app, shutdown_timeout=0.2)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    porta = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{porta}"
    sec = settings.load_secrets()
    sec["twitch"].update(client_id="CID", client_secret="SEC", access_token="AT0", refresh_token="RT0",
                         expires_at=time.time() + 3600, user_id="36599", login="streamer")
    settings.save_secrets(sec)
    (paths.sons_dir() / SOM).write_bytes(b"x")
    settings.save_mapeamento({"R1": {"titulo": "Boom", "som": SOM, "volume": 0},
                              "R2": {"titulo": "Boom2", "som": SOM, "volume": 0}})
    monkeypatch.setattr(const, "TWITCH_ID", base)
    monkeypatch.setattr(const, "TWITCH_API", base + "/helix")
    monkeypatch.setattr(const, "EVENTSUB_URL", f"ws://127.0.0.1:{porta}/ws")
    monkeypatch.setattr(const, "AUTH_PORT", 38765)
    monkeypatch.setattr(eventsub, "GRACE", 0.5)
    fake.base, fake.audio = base, faudio
    async with aiohttp.ClientSession() as s:
        yield fake, s
    await runner.cleanup()


def twitch_secrets():
    return settings.load_secrets()["twitch"]


def so_cliente():
    """Mantem client_id/secret e apaga os tokens (estado 'nao logado')."""
    sec = settings.load_secrets()
    sec["twitch"].update(access_token="", refresh_token="", expires_at=0, user_id="", login="")
    settings.save_secrets(sec)


async def espera(cond, t=10):
    fim = time.time() + t
    while time.time() < fim:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timeout esperando condição")


def test_parser():
    t, meta, p = eventsub.parse_mensagem(resgate("R1"))
    assert t == "notification" and meta["subscription_type"] == eventsub.TIPO
    assert p["event"]["reward"]["id"] == "R1"


async def test_refresh_expirado(amb):
    fake, s = amb
    sec = settings.load_secrets()
    sec["twitch"]["expires_at"] = time.time() - 10
    settings.save_secrets(sec)
    a = Auth(s)
    assert await a.access_token() == "AT1"
    c = fake.token_calls[0]
    assert c["grant_type"] == "refresh_token" and c["refresh_token"] == "RT0" and c["client_secret"] == "SEC"
    seg = twitch_secrets()
    assert seg["access_token"] == "AT1" and seg["refresh_token"] == "RT1" and seg["client_id"] == "CID"
    assert await a.access_token() == "AT1" and len(fake.token_calls) == 1  # válido: sem novo refresh


async def test_refresh_em_401(amb):
    fake, s = amb
    fake.api_401 = 1
    a = Auth(s)
    st, _ = await a.api("POST", "/eventsub/subscriptions", json={"x": 1})
    assert st == 202 and len(fake.token_calls) == 1
    assert fake.subs[0]["auth"] == "Bearer AT1"


async def test_login_code_exchange(amb):
    fake, s = amb
    so_cliente()
    a = Auth(s)
    assert not a.logado
    url = await a.iniciar_login()
    assert "scope=channel%3Aread%3Aredemptions" in url and "redirect_uri=http%3A%2F%2Flocalhost%3A3000%2Fauth" in url
    async with s.get("http://localhost:38765/auth?code=abc&state=errado") as r:
        assert r.status == 400
    async with s.get(f"http://localhost:38765/auth?code=abc&state={a._state}") as r:
        assert r.status == 200
        assert "/recompensas" in await r.text()
    assert fake.token_calls[0]["grant_type"] == "authorization_code" and fake.token_calls[0]["code"] == "abc"
    assert a.logado and a.on_login.is_set()
    assert twitch_secrets()["user_id"] == "U42" and twitch_secrets()["login"] == "streamer"  # validate pós-login
    await asyncio.sleep(0.2)
    assert a._cb_runner is None  # servidor de callback encerrado


async def test_login_sem_credenciais(amb):
    fake, s = amb
    sec = settings.load_secrets()
    sec["twitch"].update(client_id="", client_secret="")
    settings.save_secrets(sec)
    with pytest.raises(RuntimeError, match="nao configurados"):
        await Auth(s).iniciar_login()


async def test_broadcaster_id_vem_do_validate(amb):
    fake, s = amb
    sec = settings.load_secrets()
    sec["twitch"].update(user_id="", login="")
    settings.save_secrets(sec)
    a = Auth(s)
    assert a.user_id == ""
    await a.validar(forcar=True)
    assert a.user_id == "U42" and a.login == "streamer"


async def test_fluxo_eventsub(amb):
    fake, s = amb

    async def script(ws, n, path):
        if n == 1:
            await ws.send_str(welcome("S1"))
            await asyncio.sleep(0.3)
            await ws.send_str(msg("session_keepalive", {}))
            await ws.send_str(resgate("R1", "Boom", "ana"))
            await ws.send_str(resgate("R1", "Boom", "bia"))
            await ws.send_str(resgate("NAOMAPEADO", "Outro", "caio"))
            await ws.send_str(msg("session_reconnect", {"session": {"id": "S1", "reconnect_url": fake.base.replace("http", "ws") + "/ws2"}}))
            await asyncio.sleep(0.3)
        elif n == 2:  # via reconnect_url: não deve reassinar
            await ws.send_str(welcome("S1"))
            await ws.send_str(resgate("R2", "Boom2", "dani"))
            await asyncio.sleep(5)  # silêncio -> estoura keepalive (1s + 0.5s)
        else:  # reconexão após timeout: reassina
            await ws.send_str(welcome("S3"))
            await asyncio.sleep(30)

    fake.script = script
    svc = Servico(s, Hub())
    svc.player.start()
    tarefa = asyncio.create_task(svc.eventsub.run())
    try:
        await espera(lambda: len(svc.resgates) == 4)
        await espera(lambda: len(svc.player.tocados) == 3)
        # assinatura correta, com o id do canal vindo dos segredos (não de constante)
        b = fake.subs[0]["body"]
        assert b == {"type": "channel.channel_points_custom_reward_redemption.add", "version": "1",
                     "condition": {"broadcaster_user_id": "36599"},
                     "transport": {"method": "websocket", "session_id": "S1"}}
        assert fake.subs[0]["auth"] == "Bearer AT0" and fake.subs[0]["client"] == "CID"
        assert len(fake.subs) == 2  # (resgate + follow) reconnect_url não reassina
        # mapeados tocaram na ordem, não mapeado só logado
        r = list(reversed(svc.resgates))
        assert [x["status"] for x in r] == ["enfileirado", "enfileirado", "sem mapeamento", "enfileirado"]
        assert [x["usuario"] for x in svc.resgates][0] == "dani"
        assert [t["rotulo"] for t in svc.player.tocados] == ["Boom (ana)", "Boom (bia)", "Boom2 (dani)"]
        assert all(t["rc"] == 0 and t["volume"] == 0 for t in svc.player.tocados)
        assert [c[1] for c in fake.audio.chamadas] == [0, 0, 0]
        # keepalive estourou -> reconectou e reassinou (sessão S3)
        await espera(lambda: len(fake.subs) == 4 and fake.ws_conexoes == 3, 15)
        assert fake.subs[2]["body"]["transport"]["session_id"] == "S3"
    finally:
        tarefa.cancel()
        await svc.player.stop()


async def test_fila_parar(amb):
    fake, s = amb
    fake.audio.bloquear = True
    p = Player()
    p.start()
    for i in range(3):
        p.enfileirar("x", 0, f"s{i}")
    await espera(lambda: p.atual is not None)
    await p.parar()
    await espera(lambda: p.atual is None and p.fila.empty(), 5)
    await asyncio.sleep(0.3)
    assert p.atual is None and len(p.tocados) == 1  # só o atual foi interrompido; fila limpa
    assert len(fake.audio.chamadas) == 1
    await p.stop()


async def test_fifo_e_dispositivo(amb):
    fake, s = amb
    settings.save_config({"audio": {"output_device": "Meu Fone"}})
    p = Player(lambda: settings.load_config()["audio"]["output_device"])
    p.start()
    for i in range(3):
        p.enfileirar(f"a{i}", 50, f"s{i}")
    await espera(lambda: len(p.tocados) == 3)
    assert [c[0] for c in fake.audio.chamadas] == ["a0", "a1", "a2"]
    assert all(c[1] == 0.5 and c[2] == "Meu Fone" for c in fake.audio.chamadas)
    await p.stop()


async def test_callback_state(amb):
    fake, s = amb
    so_cliente()
    a = Auth(s)
    cb = "http://localhost:38765/auth"
    await a.iniciar_login()
    async with s.get(f"{cb}?error=access_denied&state=errado") as r:
        assert r.status == 400
    assert a.ultimo_erro is None and a._cb_runner is not None  # ignorado, callback segue de pé
    async with s.get(f"{cb}?code=abc&state={a._state}") as r:
        assert r.status == 200
    assert a._state is None
    await a.iniciar_login()  # reabre; state antigo (já usado) não vale
    velho = a._state
    async with s.get(f"{cb}?error=access_denied&state={velho}") as r:
        assert r.status == 400
    assert a.ultimo_erro == "access_denied" and a._state is None
    await asyncio.sleep(0.2)
    n = len(fake.token_calls)
    try:  # state reutilizado: ou 400, ou o callback já fechou
        async with s.get(f"{cb}?code=abc&state={velho}") as r:
            assert r.status == 400
    except aiohttp.ClientConnectorError:
        pass
    assert len(fake.token_calls) == n


async def test_middleware_seguranca(amb):
    fake, s = amb
    porta = porta_livre()
    app = web.Application(middlewares=[seguranca])
    app[WEB_PORT] = porta
    add_routes(app, Servico(s, Hub()))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", porta).start()
    u = f"http://127.0.0.1:{porta}"
    J = {"Content-Type": "application/json"}
    try:
        async with s.get(u + "/api/status") as r:
            assert r.status == 200
        async with s.get(u + "/api/status", headers={"Host": "evil.com"}) as r:
            assert r.status == 403
        async with s.post(u + "/api/parar", data="{}", headers=J) as r:
            assert r.status == 200
        async with s.post(u + "/api/parar", data="{}", headers={**J, "Origin": "http://evil.com"}) as r:
            assert r.status == 403
        async with s.post(u + "/api/parar", data="{}", headers={**J, "Origin": f"http://localhost:{porta}",
                                                                "Host": f"localhost:{porta}"}) as r:
            assert r.status == 200
        async with s.post(u + "/api/parar", data="x=1", headers={"Content-Type": "text/plain"}) as r:
            assert r.status == 415
        async with s.post(u + "/api/parar", data="{}", headers={"Content-Type": "application/x-www-form-urlencoded"}) as r:
            assert r.status == 415
    finally:
        await runner.cleanup()


def follow(uid="42", nome="novato", quando="2026-10-30T12:00:00Z"):
    return msg("notification", {"event": {"user_id": uid, "user_name": nome, "followed_at": quando}},
               subscription_type=eventsub.TIPO_FOLLOW)


async def test_follow_e_resgate_para_feed(amb):
    fake, s = amb

    async def script(ws, n, path):
        await ws.send_str(welcome("S1", 30))
        await ws.send_str(follow())
        await ws.send_str(resgate("R1", "Boom", "ana"))
        await asyncio.sleep(30)

    fake.script = script
    hub = Hub()
    svc = Servico(s, hub)
    svc.player.start()
    tarefa = asyncio.create_task(svc.eventsub.run())
    try:
        await espera(lambda: len(hub.interacoes) == 2 and len(svc.player.tocados) == 1)
        evs = [{k: e[k] for k in ("platform", "tipo", "user", "detalhe", "valor", "id")} for e in hub.interacoes]
        assert {"platform": "twitch", "tipo": "follow", "user": "novato", "detalhe": "", "valor": "",
                "id": "follow-42-2026-10-30T12:00:00Z"} in evs
        assert {"platform": "twitch", "tipo": "resgate", "user": "ana", "detalhe": "Boom", "valor": "100 pts",
                "id": "x"} in evs
        assert svc.player.tocados[0]["rotulo"] == "Boom (ana)"
        b = fake.subs[1]["body"]
        assert b == {"type": "channel.follow", "version": "2",
                     "condition": {"broadcaster_user_id": "36599", "moderator_user_id": "36599"},
                     "transport": {"method": "websocket", "session_id": "S1"}}
    finally:
        tarefa.cancel()
        await svc.player.stop()


async def test_feed_falha_nao_quebra(amb):
    fake, s = amb

    class HubQuebrado(Hub):
        async def interacao(self, *a, **k):
            raise RuntimeError("boom")

    svc = Servico(s, HubQuebrado())
    svc.player.start()
    await svc.tratar_resgate({"id": "a", "user_name": "ana", "reward": {"id": "R1", "title": "Boom", "cost": 1}})
    await svc.tratar_follow({"user_id": "1", "user_name": "x", "followed_at": "t"})
    await espera(lambda: len(svc.player.tocados) == 1)
    assert len(svc.resgates) == 1
    await svc.player.stop()


async def test_follow_sem_escopo_pula(amb):
    fake, s = amb
    fake.escopos = ["channel:read:redemptions", "channel:manage:redemptions"]

    async def script(ws, n, path):
        await ws.send_str(welcome("S1", 30))
        await asyncio.sleep(30)

    fake.script = script
    svc = Servico(s, Hub())
    tarefa = asyncio.create_task(svc.eventsub.run())
    try:
        await espera(lambda: svc.eventsub.inscrito)
        await asyncio.sleep(0.3)
        assert [x["body"]["type"] for x in fake.subs] == [eventsub.TIPO]
    finally:
        tarefa.cancel()


async def test_nao_afiliado_follow_continua(amb):
    fake, s = amb
    fake.sem_resgates = True

    async def script(ws, n, path):
        await ws.send_str(welcome("S1", 30))
        await ws.send_str(follow())
        await asyncio.sleep(30)

    fake.script = script
    hub = Hub()
    svc = Servico(s, hub)
    tarefa = asyncio.create_task(svc.eventsub.run())
    try:
        await espera(lambda: len(hub.interacoes) == 1)
        assert hub.interacoes[0]["tipo"] == "follow"
        assert svc.eventsub.assinaturas == {"follow": "ativo", "resgates": "indisponivel_afiliado"}
        assert svc.eventsub.inscrito and svc.eventsub.status()["assinaturas"]["follow"] == "ativo"
        await asyncio.sleep(1.5)
        assert fake.ws_conexoes == 1 and svc.eventsub.conectado  # sem loop de reconexao
    finally:
        tarefa.cancel()


async def test_ambas_assinaturas_ativas(amb):
    fake, s = amb

    async def script(ws, n, path):
        await ws.send_str(welcome("S1", 30))
        await asyncio.sleep(30)

    fake.script = script
    svc = Servico(s, Hub())
    tarefa = asyncio.create_task(svc.eventsub.run())
    try:
        await espera(lambda: svc.eventsub.inscrito)
        assert svc.eventsub.assinaturas == {"follow": "ativo", "resgates": "ativo"}
    finally:
        tarefa.cancel()
