"""LivePix: polling com servidor aiohttp local (sem rede real)."""
import asyncio, time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from livehub import settings
from livehub.alertas import regras
from livehub.chat import livepix
from livehub.chat.hub import Hub
from livehub.chat.livepix import LivePix
from livehub.runtime import Runtime
from conftest import porta_livre

SEC = {"client_id": "cid", "client_secret": "csec"}


def msg(i, quando="2026-01-01T10:00:00-03:00", **kw):
    return {"id": str(i), "username": "Ana", "message": "oi", "amount": 1000, "currency": "BRL",
            "flagged": False, "createdAt": quando, **kw}


class Fake:
    """Servidor falso: pages = lista de paginas (lista de msgs); scripts por requisicao."""
    def __init__(self):
        self.items, self.token_hits, self.msg_hits, self.page_reqs = [[]], 0, 0, []
        self.token_status, self.msg_codes, self.headers, self.expires = 200, [], {}, 3600
        app = web.Application()
        app.router.add_post("/token", self.token)
        app.router.add_get("/messages", self.msgs)
        self.server = TestServer(app)

    @property
    def pages(self):
        return self.items

    @pages.setter
    def pages(self, v):  # lista de "paginas" de teste -> lista plana na ordem do servidor
        self.items = [m for pg in v for m in pg]

    async def token(self, req):
        self.token_hits += 1
        if self.token_status != 200:
            return web.json_response({"error": "x"}, status=self.token_status)
        return web.json_response({"access_token": f"tok{self.token_hits}", "expires_in": self.expires, "token_type": "bearer"})

    async def msgs(self, req):
        self.msg_hits += 1
        p = int(req.query.get("page", 1))
        self.page_reqs.append(p)
        if self.msg_codes:
            c = self.msg_codes.pop(0)
            if c != 200:
                return web.Response(status=c, headers=self.headers)
        n = int(req.query.get("limit", 20))
        data = self.items[(p - 1) * n: p * n]
        return web.json_response({"data": data})


@pytest.fixture
async def fake(monkeypatch):
    f = Fake()
    await f.server.start_server()
    monkeypatch.setattr(livepix, "TOKEN_URL", str(f.server.make_url("/token")))
    monkeypatch.setattr(livepix, "API_URL", str(f.server.make_url("/messages")))
    monkeypatch.setattr(livepix, "MIN_LIMITE", 0)
    livepix._TOKENS.clear()
    yield f
    await f.server.close()
    livepix._TOKENS.clear()


class Driver:
    """Roda LivePix.run() com _wait controlado: cada passo() libera mais um ciclo de polling."""
    def __init__(self, fake, sec=SEC, cfg=None):
        self.hub, self.waits, self.go = Hub(), [], asyncio.Queue()
        self.lp = LivePix(cfg or {"intervalo": 8}, dict(sec), self.hub, None)

        async def esperar(secs):
            self.waits.append(secs)
            await self.go.get()
        self.lp._wait = esperar
        self.fake = fake

    async def start(self):
        self.session = aiohttp.ClientSession()
        self.lp.session = self.session
        self.task = asyncio.create_task(self.lp.run())
        await self._ate(1)
        return self

    async def _ate(self, n):
        t0 = time.monotonic()
        while len(self.waits) < n:
            assert time.monotonic() - t0 < 5, "run() travou"
            await asyncio.sleep(0.005)

    async def passo(self):
        n = len(self.waits)
        self.go.put_nowait(1)
        await self._ate(n + 1)

    async def fim(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.session.close()

    @property
    def evs(self):
        return list(self.hub.interacoes)


@pytest.fixture
async def drv(fake):
    ds = []

    async def mk(**kw):
        d = Driver(fake, **kw)
        ds.append(d)
        return await d.start()
    yield mk
    for d in ds:
        await d.fim()


async def test_token_em_cache_e_primeira_consulta_silenciosa(fake, drv):
    fake.pages = [[msg(1), msg(2)]]
    d = await drv()
    assert d.evs == [] and d.hub.status["livepix"] == "conectado"
    for _ in range(5):
        await d.passo()
    assert fake.token_hits == 1 and fake.msg_hits == 6 and d.waits[0] == 8


async def test_novos_ordenados_sem_duplicar_e_campos(fake, drv):
    fake.pages = [[msg(1)]]
    d = await drv()
    fake.pages = [[msg(4, "2026-01-01T10:03:00-03:00", username="", message="x"),
                   msg(2, "2026-01-01T10:01:00-03:00", flagged=True, message="feio"),
                   msg(3, "2026-01-01T13:02:00+00:00", username="Bia", message="ola", amount=123456), msg(1)]]
    await d.passo()
    assert [e["id"] for e in d.evs] == ["2", "3", "4"]  # 13:02Z = 10:02-03
    a, b, c = d.evs
    assert a["detalhe"] == "" and a["valor"] == "R$ 10,00" and a["tipo"] == "doação" and a["platform"] == "livepix"
    assert b["user"] == "Bia" and b["detalhe"] == "ola" and b["valor"] == "R$ 1.234,56"
    assert c["user"] == "Anônimo"
    await d.passo()
    assert len(d.evs) == 3


def test_formato_valor():
    assert livepix.valor({"amount": 5, "currency": "BRL"}) == "R$ 0,05"
    assert livepix.valor({"amount": 100000000, "currency": "BRL"}) == "R$ 1.000.000,00"
    assert livepix.valor({"amount": 1000, "currency": "usd"}) == "10.00 USD"
    assert livepix.valor({"amount": "abc", "currency": "BRL"}) == "abc"


async def test_rajada_pagina_ate_achar_vistos(fake, drv):
    fake.pages = [[msg(i) for i in range(1, 21)]]
    d = await drv()
    assert fake.page_reqs == [1]
    p1 = [msg(1000 + i) for i in range(100)]
    p2 = [msg(2000 + i) for i in range(100)]
    p3 = [msg(300), msg(1)]  # tem um visto: para aqui
    fake.pages, fake.page_reqs = [p1, p2, p3], []
    await d.passo()
    assert fake.page_reqs == [1, 2, 3] and len(d.evs) == 201
    # no maximo 5 paginas
    fake.pages = [[msg(5000 + 100 * p + i) for i in range(100)] for p in range(8)]
    fake.page_reqs = []
    await d.passo()
    assert fake.page_reqs == [1, 2, 3, 4, 5]


async def test_401_renova_token_uma_vez(fake, drv):
    fake.pages = [[msg(1)]]
    d = await drv()
    fake.msg_codes = [401]
    fake.pages = [[msg(1), msg(2)]]
    await d.passo()
    assert fake.token_hits == 2 and [e["id"] for e in d.evs] == ["2"]
    await d.passo()
    assert fake.token_hits == 2


async def test_401_persistente_vira_credenciais_invalidas(fake, drv):
    d = await drv()
    fake.msg_codes = [401, 401]
    await d.passo()
    assert d.hub.status["livepix"] == "credenciais inválidas" and d.waits[-1] == 300
    assert fake.token_hits == 2  # no maximo um re-auth


async def test_credenciais_invalidas_sem_loop(fake, drv):
    fake.token_status = 401
    d = await drv()
    assert d.hub.status["livepix"] == "credenciais inválidas" and d.waits == [300]
    assert fake.token_hits == 1 and fake.msg_hits == 0
    fake.token_status = 400
    await d.passo()
    assert fake.token_hits == 2 and d.hub.status["livepix"] == "credenciais inválidas"


async def test_sem_credenciais(fake, drv):
    d = await drv(sec={"client_id": "", "client_secret": ""})
    assert d.hub.status["livepix"] == "sem credenciais" and d.waits == [30]
    assert fake.token_hits == 0


async def test_erro_de_rede_reconectando_com_backoff(fake, drv):
    d = await drv()
    fake.msg_codes = [500, 500, 500]
    for _ in range(3):
        await d.passo()
        assert d.hub.status["livepix"] == "reconectando"
    assert 2 <= d.waits[1] < 3 and 4 <= d.waits[2] < 5 and 8 <= d.waits[3] < 9
    await d.passo()
    assert d.hub.status["livepix"] == "conectado"


async def test_429_respeita_reset(fake, drv, monkeypatch):
    monkeypatch.setattr(livepix, "MIN_LIMITE", 30)
    d = await drv()
    fake.msg_codes = [429]
    fake.headers = {"X-RateLimit-Reset": str(int(time.time()) + 100), "X-RateLimit-Remaining": "0"}
    await d.passo()
    assert d.hub.status["livepix"] == "limite de requisições" and 95 <= d.waits[-1] <= 101
    fake.msg_codes = [429]
    fake.headers = {"X-RateLimit-Reset": str(int(time.time()) + 1)}
    await d.passo()
    assert d.waits[-1] == 30  # minimo
    await d.passo()
    assert d.hub.status["livepix"] == "conectado"


async def test_token_expirado_pede_novo(fake, drv):
    fake.expires = 60  # expira na hora (expires_in-60)
    d = await drv()
    await d.passo()
    assert fake.token_hits == 2


# ---------- config / runtime / regras ----------
@pytest.fixture
async def rt(data_dir):
    cfg = settings.load_config()
    cfg["web_port"] = porta_livre()
    settings.save_config(cfg)
    r = Runtime()
    await r.start()
    yield r
    await r.stop()


async def post(rt, corpo, url="/api/config"):
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://127.0.0.1:{rt.cfg['web_port']}{url}", json=corpo) as r:
            return r.status, await r.json()


async def test_config_validacao_e_mascara(rt, fake):
    fake.token_status = 401  # a tarefa de fundo nao toca a rede real
    st, j = await post(rt, {"livepix": {"intervalo": 4}})
    assert st == 400 and j["campo"] == "livepix.intervalo"
    st, j = await post(rt, {"livepix": {"intervalo": 61}})
    assert st == 400
    st, j = await post(rt, {"livepix": {"client_id": "a b"}})
    assert st == 400 and j["campo"] == "livepix.client_id"
    st, j = await post(rt, {"livepix": {"client_id": "idzao", "client_secret": "segredao", "intervalo": 10}})
    assert st == 200 and j["config"]["livepix"]["intervalo"] == 10
    assert j["livepix"] == {"has_client_id": True, "has_secret": True}
    assert "idzao" not in str(j) and "segredao" not in str(j)
    assert settings.load_secrets()["livepix"] == {"client_id": "idzao", "client_secret": "segredao"}
    assert "livepix" in rt.tasks  # reload iniciou a tarefa
    st, j = await post(rt, {"livepix": {"client_id": "", "client_secret": ""}})
    assert settings.load_secrets()["livepix"]["client_id"] == "idzao"  # vazio = manter
    st, j = await post(rt, {"livepix": {"remover": True}})
    assert j["livepix"] == {"has_client_id": False, "has_secret": False} and "livepix" not in rt.tasks
    assert rt.hub.status["livepix"] == "sem credenciais"


async def test_botao_testar_conexao(rt, fake):
    st, j = await post(rt, {}, "/api/livepix/testar")
    assert st == 400
    fake.pages = [[msg(1), msg(2)]]
    st, j = await post(rt, {"client_id": "x", "client_secret": "y"}, "/api/livepix/testar")
    assert st == 200 and j == {"ok": True, "mensagens": 2}
    assert not rt.hub.interacoes
    # segunda chamada reaproveita o token
    await post(rt, {"client_id": "x", "client_secret": "y"}, "/api/livepix/testar")
    assert fake.token_hits == 1
    fake.token_status = 401
    livepix._TOKENS.clear()
    st, j = await post(rt, {"client_id": "x", "client_secret": "y"}, "/api/livepix/testar")
    assert st == 400 and "inválidas" in j["erro"]


def test_regras_doacao_livepix():
    ev = {"platform": "livepix", "tipo": "doação", "detalhe": "valeu!", "valor": "R$ 10,00"}
    assert regras.categoria(ev) == "doacao" and regras.mensagem(ev) == "valeu!"


def _asc(n, ini=0):
    return [msg(ini + i, f"2026-01-01T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}+00:00") for i in range(n)]


async def test_ordem_asc_semeia_e_acompanha_ultima_pagina(fake, drv):
    fake.items = _asc(250, 1)  # paginas de 100: 100 + 100 + 50
    d = await drv()
    assert d.evs == [] and d.lp.ordem == "asc" and d.lp._pagina == 3
    assert fake.page_reqs == [1, 2, 3]
    fake.page_reqs = []
    fake.items += [msg(9001, "2026-02-01T00:00:00+00:00")]
    await d.passo()
    assert [e["id"] for e in d.evs] == ["9001"] and fake.page_reqs == [3]
    await d.passo()
    assert len(d.evs) == 1
    # 60 novas: enche a pagina 3 (100) e transborda para a 4
    fake.items += [msg(9100 + i, f"2026-02-02T00:00:{i:02d}+00:00") for i in range(60)]
    fake.page_reqs = []
    await d.passo()
    assert len(d.evs) == 61 and fake.page_reqs == [3, 4] and d.lp._pagina == 4
    await d.passo()
    assert len(d.evs) == 61


async def test_ordem_asc_total_multiplo_do_limite(fake, drv):
    fake.items = _asc(200, 1)
    d = await drv()
    assert d.lp._pagina == 2 and d.evs == []
    fake.items += [msg(9001, "2026-02-01T00:00:00+00:00")]
    await d.passo()
    assert [e["id"] for e in d.evs] == ["9001"] and d.lp._pagina == 3


async def test_ordem_desc_detectada(fake, drv):
    fake.items = list(reversed(_asc(30, 1)))
    d = await drv()
    assert d.lp.ordem == "desc" and fake.page_reqs == [1]


async def test_createdat_iguais_cai_em_desc(fake, drv):
    fake.items = [msg(i) for i in range(1, 6)]
    d = await drv()
    assert d.lp.ordem is None and fake.page_reqs == [1] and d.evs == []
    fake.items = [msg(9)] + fake.items
    await d.passo()
    assert [e["id"] for e in d.evs] == ["9"]
