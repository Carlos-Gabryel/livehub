import asyncio, json, types

import aiohttp
import pytest
from yarl import URL

from livehub import settings
from livehub.alertas import regras, tts
from livehub.alertas.servico import Alertas
from livehub.chat.hub import Hub
from livehub.runtime import Runtime
from conftest import porta_livre


# ---------- regras ----------
CASOS = {
    "follow": ["follow"], "sub": ["sub", "assinatura", "membro"], "presente": ["presente de sub", "presente de membro"],
    "raid": ["raid", "host"], "cheer": ["bits", "kicks", "presente", "super sticker"], "doacao": ["doação", "super chat"],
}


def test_categoria_todos_os_tipos():
    for cat, tipos in CASOS.items():
        for t in tipos:
            assert regras.categoria({"tipo": t}) == cat, t
    for t in ("resgate", "outro", "", None):
        assert regras.categoria({"tipo": t}) is None


def test_numero():
    assert regras.numero("500 bits") == 500
    assert regras.numero("R$ 10,00") == 10
    assert regras.numero("1.234,56") == 1234.56
    assert regras.numero("$5.50") == 5.5
    assert regras.numero("1,234.50") == 1234.5
    assert regras.numero("1.234") == 1234
    assert regras.numero("") is None and regras.numero(None) is None and regras.numero("sem numero") is None


def test_texto_e_mensagem():
    ev = {"user": "Ana", "valor": "100 bits", "detalhe": "oi gente", "platform": "twitch", "tipo": "bits"}
    assert regras.texto("{user} mandou {valor}! {mensagem} [{plataforma}] {detalhe}", ev) == "Ana mandou 100 bits! oi gente [twitch] oi gente"
    assert regras.mensagem(ev) == "oi gente"
    for t in ("kicks", "super chat", "doação"):
        assert regras.mensagem({"tipo": t, "detalhe": "x"}) == "x"
    for t in ("sub", "follow", "presente", "presente de sub"):
        assert regras.mensagem({"tipo": t, "detalhe": "Tier 1"}) == ""


# ---------- TTS ----------
CFG_TTS = {"motor": "neural", "voz_neural": "pt-BR-FranciscaNeural", "voz_windows": "", "volume": 90, "minimo": 0,
           "max_caracteres": 200, "ler_nome": True, "remover_links": True, "palavras_bloqueadas": []}


def test_preparar_texto():
    c = dict(CFG_TTS)
    assert tts.preparar("veja https://x.com/a?b=1 e www.site.com.br agora", c) == "veja e agora"
    assert tts.preparar("veja https://x.com", {**c, "remover_links": False}) == "veja https://x.com"
    assert tts.preparar("isso é Feio e feioso", {**c, "palavras_bloqueadas": ["feio"]}) == "isso é e feioso"
    assert tts.preparar("a" * 300, {**c, "max_caracteres": 50}) == "a" * 50
    assert tts.preparar("oi   tudo\n bem", c, "Ana") == "Ana disse: oi tudo bem"
    assert tts.preparar("oi", {**c, "ler_nome": False}, "Ana") == "oi"
    assert tts.preparar("https://x.com", c, "Ana") == ""
    assert tts.preparar("   ", c) == ""


async def test_neural_falha_cai_para_windows(monkeypatch):
    chamadas = []

    async def neural(texto, voz, destino):
        chamadas.append("neural")
        raise RuntimeError("sem rede")

    async def windows(texto, voz, destino):
        chamadas.append("windows")
        destino.write_bytes(b"RIFFxxxx")
    monkeypatch.setattr(tts, "_neural", neural)
    monkeypatch.setattr(tts, "_windows", windows)
    p = await tts.gerar("oi", dict(CFG_TTS), "Ana")
    assert chamadas == ["neural", "windows"] and p.suffix == ".wav" and p.parent == tts.tts_dir()


async def test_neural_ok_e_tudo_falhando(monkeypatch):
    async def neural(texto, voz, destino):
        destino.write_bytes(b"ID3xxxx")

    async def quebra(*a):
        raise RuntimeError("x")
    monkeypatch.setattr(tts, "_neural", neural)
    assert (await tts.gerar("oi", dict(CFG_TTS))).suffix == ".mp3"
    monkeypatch.setattr(tts, "_neural", quebra)
    monkeypatch.setattr(tts, "_windows", quebra)
    assert await tts.gerar("oi", dict(CFG_TTS)) is None  # nunca levanta
    assert await tts.gerar("   ", dict(CFG_TTS)) is None


async def test_windows_nao_interpola_texto_no_comando(monkeypatch, tmp_path):
    visto = {}

    class Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

        def kill(self):
            pass

    async def fake_exec(*args, **kw):
        visto["args"], visto["env"] = args, kw.get("env", {})
        return Proc()
    monkeypatch.setattr(tts.asyncio, "create_subprocess_exec", fake_exec)
    perigo = "'; Remove-Item C:\\ -Recurse; '"
    await tts._windows(perigo, "Maria", tmp_path / "o.wav")
    assert not any(perigo in str(a) for a in visto["args"])
    assert visto["env"]["LH_TEXTO"] == perigo and visto["env"]["LH_VOZ"] == "Maria"
    assert visto["env"]["LH_SAIDA"].endswith("o.wav")
    assert "$env:LH_TEXTO" in visto["args"][-1]


def test_limpa_tts_mantem_50():
    d = tts.tts_dir()
    for i in range(60):
        (d / f"{i:02d}.mp3").write_bytes(b"x")
        import os
        os.utime(d / f"{i:02d}.mp3", (1000 + i, 1000 + i))
    tts._limpar()
    nomes = sorted(p.name for p in d.iterdir())
    assert len(nomes) == 50 and "59.mp3" in nomes and "09.mp3" not in nomes


# ---------- fila ----------
class WS:
    def __init__(self):
        self.msgs = []

    async def send_str(self, t):
        self.msgs.append(json.loads(t))

    def alertas(self):
        return [m["alerta"] for m in self.msgs if m["type"] == "alerta"]


async def esperar(cond, t=6.0):
    fim = asyncio.get_running_loop().time() + t
    while asyncio.get_running_loop().time() < fim:
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condicao nao ocorreu")


@pytest.fixture
async def svc():
    cfg = settings.load_config()
    for c in cfg["alertas"]["categorias"].values():
        c["duracao"] = 2
    rt = types.SimpleNamespace(hub=Hub(), cfg=cfg)
    s = Alertas(rt)
    rt.alertas = s
    s.iniciar()
    s.ws = WS()
    yield s
    await s.parar()


async def ev(s, tipo, user="u", valor="", detalhe="", plat="twitch", id=None):
    return await s.rt.hub.interacao(plat, tipo, user, detalhe, valor, id)


async def test_ordem_e_fim_avanca(svc):
    svc.clientes.add(svc.ws)
    await ev(svc, "follow", "a")
    await ev(svc, "sub", "b")
    await ev(svc, "raid", "c", "9 viewers")
    await esperar(lambda: len(svc.ws.alertas()) == 1)
    assert svc.ws.alertas()[0]["texto"] == "a seguiu!" and svc.estado()["atual"]["texto"] == "a seguiu!"
    await asyncio.sleep(0.05)
    assert len(svc.ws.alertas()) == 1  # espera o fim
    svc.fim_do_cliente("outro-id")  # id errado nao avanca
    await asyncio.sleep(0.05)
    assert len(svc.ws.alertas()) == 1
    svc.fim_do_cliente(svc.ws.alertas()[0]["id"])
    await esperar(lambda: len(svc.ws.alertas()) == 2)
    svc.fim_do_cliente(svc.ws.alertas()[1]["id"])
    await esperar(lambda: len(svc.ws.alertas()) == 3)
    assert [a["categoria"] for a in svc.ws.alertas()] == ["follow", "sub", "raid"]
    assert svc.ws.alertas()[2]["texto"] == "c chegou com 9 viewers!"


async def test_follow_descartado_com_fila_grande(svc):
    svc.clientes.add(svc.ws)
    svc.pausar()
    for i in range(15):
        await ev(svc, "follow", f"f{i}", id=f"f{i}")
    assert len(svc.fila) == 11
    await ev(svc, "sub", "s")
    assert len(svc.fila) == 12
    for i in range(60):
        await ev(svc, "sub", f"x{i}")
    assert len(svc.fila) == 50  # teto


async def test_sem_cliente_descarta(svc):
    await ev(svc, "sub", "a")
    await esperar(lambda: not svc.fila)
    assert svc.estado()["atual"] is None and svc.ws.msgs == []


async def test_timeout_avanca(svc):
    svc.clientes.add(svc.ws)
    svc.extra_timeout = 0
    for c in svc.rt.cfg["alertas"]["categorias"].values():
        c["duracao"] = 0.05
    await ev(svc, "sub", "a")
    await ev(svc, "sub", "b")
    await esperar(lambda: len(svc.ws.alertas()) == 2)


async def test_pular(svc):
    svc.clientes.add(svc.ws)
    await ev(svc, "sub", "a")
    await ev(svc, "sub", "b")
    await esperar(lambda: len(svc.ws.alertas()) == 1)
    await svc.pular()
    await esperar(lambda: len(svc.ws.alertas()) == 2)
    assert {"type": "pular"} in svc.ws.msgs


async def test_pausar_e_retomar(svc):
    svc.clientes.add(svc.ws)
    assert svc.pausar() is True
    await ev(svc, "sub", "a")
    await asyncio.sleep(0.1)
    assert svc.ws.alertas() == [] and len(svc.fila) == 1
    assert svc.pausar() is False
    await esperar(lambda: len(svc.ws.alertas()) == 1)


async def test_repetir_por_seq(svc):
    svc.clientes.add(svc.ws)
    svc.pausar()
    e = await ev(svc, "doação", "Zé", "R$ 1,00", "oi")
    r = await ev(svc, "resgate", "Zé")
    svc.fila.clear()
    svc.rt.cfg["alertas"]["categorias"]["doacao"]["minimo"] = 50  # repetir ignora o minimo
    svc.pausar()
    assert svc.repetir(e["seq"]) is True
    await esperar(lambda: len(svc.ws.alertas()) == 1)
    assert svc.ws.alertas()[0]["texto"] == "Zé doou R$ 1,00!" and svc.ws.alertas()[0]["seq"] == e["seq"]
    with pytest.raises(KeyError):
        svc.repetir(99999)
    with pytest.raises(ValueError):
        svc.repetir(r["seq"])


async def test_testar_amostras(svc):
    svc.clientes.add(svc.ws)
    with pytest.raises(ValueError):
        svc.testar("nada")
    svc.testar("cheer")
    await esperar(lambda: len(svc.ws.alertas()) == 1)
    a = svc.ws.alertas()[0]
    assert a["user"] == "Teste" and "100 bits" in a["texto"] and a["mensagem"] == "Mensagem de teste do LiveHub"


async def test_minimo_inativo_resgate(svc):
    svc.clientes.add(svc.ws)
    svc.pausar()
    cats = svc.rt.cfg["alertas"]["categorias"]
    cats["cheer"]["minimo"] = 100
    await ev(svc, "bits", "a", "99 bits")
    assert len(svc.fila) == 0
    await ev(svc, "bits", "a", "100 bits")
    assert len(svc.fila) == 1
    cats["raid"]["ativo"] = False
    await ev(svc, "raid", "a", "5 viewers")
    assert len(svc.fila) == 1
    await ev(svc, "resgate", "a", "Hidratar")
    assert len(svc.fila) == 1
    svc.rt.cfg["alertas"]["ativo"] = False
    await ev(svc, "sub", "a")
    assert len(svc.fila) == 1


async def test_tts_antes_de_enfileirar_mantem_ordem(svc, monkeypatch):
    svc.clientes.add(svc.ws)
    gerados = []

    async def gerar(texto, cfg, user=""):
        gerados.append((texto, user))
        await asyncio.sleep(0.2)
        return tts.tts_dir() / "x.mp3"
    monkeypatch.setattr(tts, "gerar", gerar)
    await ev(svc, "bits", "Ana", "100 bits", "ola mundo")
    await ev(svc, "follow", "Bia")
    await esperar(lambda: len(svc.ws.alertas()) == 1)
    a = svc.ws.alertas()[0]
    assert a["categoria"] == "cheer" and a["tts_url"] == "/alertas/tts/x.mp3" and gerados == [("ola mundo", "Ana")]
    svc.fim_do_cliente(a["id"])
    await esperar(lambda: len(svc.ws.alertas()) == 2)
    assert svc.ws.alertas()[1]["categoria"] == "follow" and svc.ws.alertas()[1]["tts_url"] == ""


async def test_tts_minimo_e_sem_mensagem(svc, monkeypatch):
    svc.clientes.add(svc.ws)
    gerados = []

    async def gerar(texto, cfg, user=""):
        gerados.append(texto)
        return None
    monkeypatch.setattr(tts, "gerar", gerar)
    svc.rt.cfg["alertas"]["tts"]["minimo"] = 50
    await ev(svc, "bits", "a", "10 bits", "baixo")
    await ev(svc, "bits", "a", "60 bits", "alto")
    await ev(svc, "bits", "a", "60 bits", "")
    await esperar(lambda: len(svc.ws.alertas()) >= 1)
    assert gerados == ["alto"]


# ---------- rotas ----------
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


async def test_posts_sem_origem_local_dao_403(rt):
    hdr = {"Origin": "http://evil.example"}
    async with aiohttp.ClientSession() as s:
        for rota, corpo in (("testar", {"categoria": "follow"}), ("pular", {}), ("pausar", {}), ("repetir", {"seq": 1}),
                            ("abrir_imagens", {})):
            async with s.post(f"{base(rt)}/api/alertas/{rota}", json=corpo, headers=hdr) as r:
                assert r.status == 403, rota
        async with s.post(f"{base(rt)}/api/alertas/pausar", json={}) as r:
            assert r.status == 200 and (await r.json())["pausado"] is True
        assert rt.alertas.pausado is True


async def test_rotas_basicas(rt):
    async with aiohttp.ClientSession() as s:
        async with s.get(base(rt) + "/alertas") as r:
            assert r.status == 200 and "alertas/ws" in await r.text()
        async with s.get(base(rt) + "/api/alertas/estado") as r:
            assert await r.json() == {"pausado": False, "fila": 0, "atual": None, "clientes": 0}
        async with s.post(base(rt) + "/api/alertas/testar", json={"categoria": "xx"}) as r:
            assert r.status == 400
        async with s.post(base(rt) + "/api/alertas/repetir", json={"seq": 12345}) as r:
            assert r.status == 404
        async with s.post(base(rt) + "/api/alertas/repetir", json={"seq": "a"}) as r:
            assert r.status == 400
        from livehub import paths
        (paths.sons_dir() / "a.mp3").write_bytes(b"x")
        (paths.imagens_dir() / "i.png").write_bytes(b"x")
        async with s.get(base(rt) + "/api/alertas/midia") as r:
            j = await r.json()
            assert j["sons"] == ["a.mp3"] and j["imagens"] == ["i.png"] and "pt-BR-FranciscaNeural" in j["vozes_neural"]
            assert isinstance(j["vozes_windows"], list)
        for rota in ("/alertas/midia/som/a.mp3", "/alertas/midia/img/i.png"):
            async with s.get(base(rt) + rota) as r:
                assert r.status == 200


async def test_midia_rejeita_traversal(rt, data_dir):
    from livehub import paths
    (data_dir / "segredo.png").write_bytes(b"x")
    (data_dir / "segredo.mp3").write_bytes(b"x")
    (tts.tts_dir() / "ok.mp3").write_bytes(b"x")
    alvos = ["/alertas/midia/img/..%2Fsegredo.png", "/alertas/midia/img/%2e%2e%2fsegredo.png",
             "/alertas/midia/som/..%2Fsegredo.mp3", "/alertas/tts/..%2Fsegredo.mp3",
             "/alertas/midia/img/" + str(data_dir / "segredo.png").replace("\\", "/").replace(":", "%3A").replace("/", "%2F"),
             "/alertas/midia/som/..%5Csegredo.mp3", "/alertas/midia/img/config.json", "/alertas/midia/img/nao-existe.png"]
    (data_dir / "config.json").exists()
    async with aiohttp.ClientSession() as s:
        for a in alvos:
            async with s.get(URL(base(rt) + a, encoded=True)) as r:
                assert r.status == 404, a
        async with s.get(base(rt) + "/alertas/tts/ok.mp3") as r:
            assert r.status == 200


async def test_ws_fim_e_config_ao_vivo(rt):
    rt.cfg["alertas"]["categorias"]["follow"]["duracao"] = 2
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(base(rt) + "/alertas/ws") as ws:
            async with s.post(base(rt) + "/api/alertas/testar", json={"categoria": "follow"}) as r:
                assert (await r.json())["ok"] is True
            m = json.loads((await ws.receive(timeout=3)).data)
            assert m["type"] == "alerta" and m["alerta"]["texto"] == "Teste seguiu!"
            assert rt.alertas.estado()["atual"]["id"] == m["alerta"]["id"]
            await ws.send_json({"type": "fim", "id": m["alerta"]["id"]})
            await esperar(lambda: rt.alertas.estado()["atual"] is None)
        await esperar(lambda: rt.alertas.estado()["clientes"] == 0)


async def test_config_alertas_validacao_e_persistencia(rt, data_dir):
    from livehub import paths
    (paths.sons_dir() / "ding.wav").write_bytes(b"x")
    async with aiohttp.ClientSession() as s:
        async def post(corpo):
            async with s.post(base(rt) + "/api/config", json={"alertas": corpo}) as r:
                return r.status, await r.json()
        st, j = await post({"categorias": {"follow": {"texto": "{user} chegou!", "som": "ding.wav", "duracao": 9, "volume": 50}},
                            "estilo": {"animacao": "bounce", "tamanho": 60}, "tts": {"palavras_bloqueadas": ["a", " b "]}})
        assert st == 200, j
        c = settings.load_config()["alertas"]
        assert c["categorias"]["follow"]["texto"] == "{user} chegou!" and c["categorias"]["follow"]["som"] == "ding.wav"
        assert c["estilo"]["animacao"] == "bounce" and c["tts"]["palavras_bloqueadas"] == ["a", "b"]
        assert rt.cfg["alertas"]["categorias"]["follow"]["duracao"] == 9  # reload aplicou
        for ruim in ({"categorias": {"follow": {"duracao": 1}}}, {"categorias": {"follow": {"duracao": 31}}},
                     {"categorias": {"follow": {"volume": 101}}}, {"categorias": {"follow": {"som": "nao.wav"}}},
                     {"categorias": {"follow": {"som": "../x.wav"}}}, {"categorias": {"follow": {"imagem": "x.png"}}},
                     {"categorias": {"cheer": {"minimo": -1}}}, {"categorias": {"xx": {}}},
                     {"estilo": {"tamanho": 11}}, {"estilo": {"animacao": "zoom"}}, {"estilo": {"cor_texto": "red"}},
                     {"tts": {"max_caracteres": 10}}, {"tts": {"max_caracteres": 501}}, {"tts": {"motor": "x"}},
                     {"tts": {"voz_neural": "x; rm"}}, {"tts": {"palavras_bloqueadas": ["x"] * 201}},
                     {"tts": {"palavras_bloqueadas": "x"}}, {"ativo": "sim"}):
            st, j = await post(ruim)
            assert st == 400 and j["campo"].startswith("alertas"), ruim
