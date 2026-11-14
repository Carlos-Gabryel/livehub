import asyncio, json, types

import aiohttp
import pytest
from yarl import URL

from livehub import paths, settings
from livehub.alertas import regras, tts
from livehub.alertas.servico import Alertas, MENSAGENS_TESTE, USUARIOS_TESTE

MENSAGENS_TESTE_CORTADAS = {regras.cortar(m, 120) for m in MENSAGENS_TESTE}
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
    assert tts.preparar("oi   tudo\n bem", c, "Ana doou R$ 10,00!") == "Ana doou 10 reais! oi tudo bem"
    assert tts.preparar("oi", c, "Ana mandou 100 bits") == "Ana mandou 100 bits. oi"
    assert tts.preparar("oi", {**c, "ler_nome": False}, "Ana doou R$ 10,00!") == "oi"
    assert tts.preparar("https://x.com", c, "Ana doou!") == ""  # sem mensagem do viewer nao fala nada
    assert tts.preparar("   ", c, "Ana doou!") == ""


def test_falar_valores():
    f = tts._falar_valores
    assert f("R$ 10,00") == "10 reais" and f("R$10") == "10 reais" and f("R$ 1,00") == "1 real"
    assert f("R$ 10,50") == "10 reais e 50 centavos" and f("R$ 1.234,56") == "1234 reais e 56 centavos"
    assert f("100 bits de 5 pessoas") == "100 bits de 5 pessoas"


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
    cfg["alertas"]["categorias"]["cheer"]["tts"] = True  # padrao e desligado (opt-in); os testes de TTS precisam dele
    rt = types.SimpleNamespace(hub=Hub(), cfg=cfg)
    s = Alertas(rt)
    rt.alertas = s
    s.iniciar()
    s.ws = WS()
    yield s
    await s.parar()


async def ev(s, tipo, user="u", valor="", detalhe="", plat="twitch", id=None, mensagem=""):
    return await s.rt.hub.interacao(plat, tipo, user, detalhe, valor, id, mensagem)


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
        await svc.testar("nada")
    assert (await svc.testar("cheer"))["ok"] is True
    await esperar(lambda: len(svc.ws.alertas()) == 1)
    a = svc.ws.alertas()[0]
    assert a["user"] in USUARIOS_TESTE and a["texto"].endswith(" bits!") and a["mensagem"] in MENSAGENS_TESTE_CORTADAS


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

    async def gerar(texto, cfg, anuncio=""):
        gerados.append((texto, anuncio))
        await asyncio.sleep(0.2)
        return tts.tts_dir() / "x.mp3"
    monkeypatch.setattr(tts, "gerar", gerar)
    await ev(svc, "bits", "Ana", "100 bits", "ola mundo")
    await ev(svc, "follow", "Bia")
    await esperar(lambda: len(svc.ws.alertas()) == 1)
    a = svc.ws.alertas()[0]
    assert a["categoria"] == "cheer" and a["tts_url"] == "/alertas/tts/x.mp3" and gerados == [("ola mundo", "Ana mandou 100 bits!")]
    svc.fim_do_cliente(a["id"])
    await esperar(lambda: len(svc.ws.alertas()) == 2)
    assert svc.ws.alertas()[1]["categoria"] == "follow" and svc.ws.alertas()[1]["tts_url"] == ""


async def test_tts_minimo_e_sem_mensagem(svc, monkeypatch):
    svc.clientes.add(svc.ws)
    gerados = []

    async def gerar(texto, cfg, anuncio=""):
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


async def test_ws_fim_e_config_ao_vivo(rt, monkeypatch):
    async def gerar(texto, cfg, anuncio=""):
        return None
    monkeypatch.setattr(tts, "gerar", gerar)
    rt.cfg["alertas"]["categorias"]["follow"]["duracao"] = 2
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(base(rt) + "/alertas/ws") as ws:
            async with s.post(base(rt) + "/api/alertas/testar", json={"categoria": "follow"}) as r:
                assert (await r.json())["ok"] is True
            m = json.loads((await ws.receive(timeout=3)).data)
            assert m["type"] == "alerta" and m["alerta"]["texto"].endswith(" seguiu!")
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
                            "estilo": {"texto_entrada": "zoom", "tamanho": 60, "mensagem_max": 80}, "tts": {"palavras_bloqueadas": ["a", " b "]}})
        assert st == 200, j
        c = settings.load_config()["alertas"]
        assert c["categorias"]["follow"]["texto"] == "{user} chegou!" and c["categorias"]["follow"]["som"] == "ding.wav"
        assert c["estilo"]["texto_entrada"] == "zoom" and c["estilo"]["mensagem_max"] == 80 and c["tts"]["palavras_bloqueadas"] == ["a", "b"]
        assert rt.cfg["alertas"]["categorias"]["follow"]["duracao"] == 9  # reload aplicou
        for ruim in ({"categorias": {"follow": {"duracao": 1}}}, {"categorias": {"follow": {"duracao": 31}}},
                     {"categorias": {"follow": {"volume": 101}}}, {"categorias": {"follow": {"som": "nao.wav"}}},
                     {"categorias": {"follow": {"som": "../x.wav"}}}, {"categorias": {"follow": {"imagem": "x.png"}}},
                     {"categorias": {"cheer": {"minimo": -1}}}, {"categorias": {"xx": {}}},
                     {"estilo": {"tamanho": 11}}, {"estilo": {"texto_entrada": "girar"}}, {"estilo": {"midia_entrada": "suave"}},
                     {"estilo": {"texto_saida": "pop"}}, {"estilo": {"midia_saida": "zoom"}},
                     {"estilo": {"posicao_texto": "meio"}}, {"estilo": {"texto_x": 401}}, {"estilo": {"texto_y": -401}},
                     {"estilo": {"mensagem_max": 19}}, {"estilo": {"mensagem_max": 501}}, {"estilo": {"texto_atraso": 5001}}, {"estilo": {"texto_atraso": -1}},
                     {"categorias": {"follow": {"video": "nao.mp4"}}}, {"estilo": {"cor_texto": "red"}},
                     {"tts": {"max_caracteres": 10}}, {"tts": {"max_caracteres": 501}}, {"tts": {"motor": "x"}},
                     {"tts": {"voz_neural": "x; rm"}}, {"tts": {"palavras_bloqueadas": ["x"] * 201}},
                     {"tts": {"palavras_bloqueadas": "x"}}, {"ativo": "sim"}):
            st, j = await post(ruim)
            assert st == 400 and j["campo"].startswith("alertas"), ruim


# ---------- controles do feed: pausa explicita, difusao, guarda do pular, repetir resgate ----------
async def _pausar(s, rt, corpo):
    async with s.post(f"{base(rt)}/api/alertas/pausar", json=corpo) as r:
        return r.status, await r.json()


async def test_pausar_explicito_idempotente_e_toggle(rt):
    async with aiohttp.ClientSession() as s:
        assert (await _pausar(s, rt, {"pausado": True}))[1]["pausado"] is True
        assert (await _pausar(s, rt, {"pausado": True}))[1]["pausado"] is True
        assert (await _pausar(s, rt, {"pausado": False}))[1]["pausado"] is False
        assert (await _pausar(s, rt, {"pausado": False}))[1]["pausado"] is False
        assert (await _pausar(s, rt, {}))[1]["pausado"] is True  # toggle (compat)
        assert (await _pausar(s, rt, {}))[1]["pausado"] is False
        assert (await _pausar(s, rt, {"pausado": "sim"}))[0] == 400
        assert rt.alertas.pausado is False


async def test_estado_e_difundido_no_ws_do_feed(rt):
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(base(rt) + "/eventos/ws")
        await ws.receive_json()  # history
        await _pausar(s, rt, {"pausado": True})
        m = await asyncio.wait_for(ws.receive_json(), 3)
        assert m["type"] == "alertas" and m["pausado"] is True and m["fila"] == 0 and m["atual"] is None
        await rt.hub.interacao("twitch", "follow", "Ana", id="f1")  # entra na fila (pausado) -> fila 1
        msgs = [await asyncio.wait_for(ws.receive_json(), 3) for _ in range(2)]
        assert any(x["type"] == "alertas" and x["fila"] == 1 for x in msgs)


async def test_pular_duas_vezes_em_3s_recusa_a_segunda(rt, monkeypatch):
    from livehub.alertas import servico
    t = [1000.0]
    monkeypatch.setattr(servico.time, "monotonic", lambda: t[0])
    async with aiohttp.ClientSession() as s:
        async def pular():
            async with s.post(f"{base(rt)}/api/alertas/pular", json={}) as r:
                return r.status, await r.json()
        assert await pular() == (200, {"ok": True})
        t[0] += 2.9
        assert await pular() == (429, {"ok": False, "motivo": "aguarde"})
        t[0] += 0.2
        assert await pular() == (200, {"ok": True})


async def test_resgates_repetir(rt, monkeypatch):
    (paths.sons_dir() / "boom.wav").write_bytes(b"x")
    settings.save_mapeamento({"R1": {"titulo": "Boom", "som": "boom.wav", "volume": 40}})
    tocados = []
    monkeypatch.setattr(rt.svc.player, "enfileirar", lambda p, v, r="": tocados.append((str(p), v, r)))
    await rt.svc.tratar_resgate({"id": "x1", "user_name": "Ana", "reward": {"id": "R1", "title": "Boom", "cost": 5}})
    await rt.hub.interacao("twitch", "resgate", "Bia", "Boom", id="x2")  # sem reward_id guardado: casa pelo titulo
    await rt.hub.interacao("twitch", "resgate", "Cia", "Outro", id="x3")  # sem mapeamento
    assert len(tocados) == 1  # o resgate original tocou uma vez
    seqs = [e["seq"] for e in rt.hub.interacoes]
    hdr = {"Origin": "http://evil.example"}
    async with aiohttp.ClientSession() as s:
        url = base(rt) + "/api/resgates/repetir"
        async with s.post(url, json={"seq": seqs[0]}, headers=hdr) as r:
            assert r.status == 403
        for seq in seqs[:2]:
            async with s.post(url, json={"seq": seq}) as r:
                assert r.status == 200 and (await r.json())["ok"] is True
        assert len(tocados) == 3 and all(t[0].endswith("boom.wav") and t[1] == 40 for t in tocados)
        async with s.post(url, json={"seq": seqs[2]}) as r:
            assert r.status == 404 and "som" in (await r.json())["erro"]
        async with s.post(url, json={"seq": 99999}) as r:
            assert r.status == 404
        async with s.post(url, json={"seq": "a"}) as r:
            assert r.status == 400
        async with s.post(url, data="{}", headers={"Content-Type": "text/plain"}) as r:
            assert r.status in (403, 415)
    assert len(tocados) == 3


# ---------- video, estilo novo, testar com TTS, upload ----------
async def test_migracao_v2_animacao(data_dir):
    settings.config_path().write_text(json.dumps({"settings_version": 1, "alertas": {"estilo": {"animacao": "bounce"}}}),
                                      encoding="utf-8")
    cfg = settings.load_config()
    e = cfg["alertas"]["estilo"]
    assert e["texto_entrada"] == e["midia_entrada"] == "bounce" and "animacao" not in e and cfg["settings_version"] == 2
    assert "animacao" not in json.loads(settings.config_path().read_text(encoding="utf-8"))["alertas"]["estilo"]


async def test_config_estilo_novo_e_video(rt):
    from livehub import paths
    (paths.videos_dir() / "v.mp4").write_bytes(b"x")
    async with aiohttp.ClientSession() as s:
        async with s.post(base(rt) + "/api/config", json={"alertas": {
                "estilo": {"texto_entrada": "zoom", "midia_entrada": "baixo", "texto_saida": "encolher", "midia_saida": "cima",
                           "posicao_texto": "sobre_meio", "texto_x": -400, "texto_y": 400, "texto_atraso": 5000},
                "categorias": {"follow": {"video": "v.mp4"}}}}) as r:
            assert r.status == 200, await r.text()
    c = settings.load_config()["alertas"]
    assert c["estilo"]["texto_saida"] == "encolher" and c["estilo"]["posicao_texto"] == "sobre_meio"
    assert c["estilo"]["texto_x"] == -400 and c["estilo"]["texto_atraso"] == 5000 and c["categorias"]["follow"]["video"] == "v.mp4"


async def test_video_enfileirar_e_rotas(rt):
    from livehub import paths
    (paths.videos_dir() / "v.webm").write_bytes(b"x")
    (paths.imagens_dir() / "i.png").write_bytes(b"x")
    c = rt.cfg["alertas"]["categorias"]["follow"]
    c["video"], c["imagem"] = "v.webm", "i.png"
    rt.alertas.pausar(True)
    assert rt.alertas.enfileirar({"platform": "twitch", "tipo": "follow", "user": "a", "detalhe": "", "valor": "", "id": None, "seq": None}, "follow")
    a = rt.alertas.fila[0]["alerta"]
    assert a["video_url"] == "/alertas/midia/video/v.webm" and a["imagem_url"] == ""
    async with aiohttp.ClientSession() as s:
        async with s.get(base(rt) + a["video_url"]) as r:
            assert r.status == 200
        async with s.get(base(rt) + "/alertas/midia/video/nao.webm") as r:
            assert r.status == 404
        async with s.get(base(rt) + "/api/alertas/midia") as r:
            assert (await r.json())["videos"] == ["v.webm"]


EV_FOLLOW = {"platform": "twitch", "tipo": "follow", "user": "a", "detalhe": "", "valor": "", "id": None, "seq": None}


async def test_testar_tts_resultado_e_texto(svc, monkeypatch):
    falas = []

    async def gerar(texto, cfg, anuncio=""):
        falas.append(texto)
        return tts.tts_dir() / "t.mp3"
    monkeypatch.setattr(tts, "gerar", gerar)
    assert (await svc.testar("cheer")).items() >= {"ok": True, "tts": True}.items()
    assert await svc.testar("follow") == {"ok": True, "tts": None}  # sem mensagem de viewer: nao fala
    assert len(falas) == 1 and falas[0] in MENSAGENS_TESTE

    async def vazio(texto, cfg, anuncio=""):
        return None
    monkeypatch.setattr(tts, "gerar", vazio)
    assert (await svc.testar("cheer")).items() >= {"ok": True, "tts": False}.items()

    async def quebra(texto, cfg, anuncio=""):
        raise RuntimeError("x")
    monkeypatch.setattr(tts, "gerar", quebra)
    assert (await svc.testar("cheer")).items() >= {"ok": True, "tts": False}.items()


async def _up(s, rt, nome, dados=b"abc", ct="application/octet-stream"):
    return await s.post(base(rt) + "/api/midia/enviar", params={"nome": nome}, data=dados, headers={"Content-Type": ct})


async def test_upload_misto_colisao_e_traversal(rt, monkeypatch):
    from livehub import paths
    async with aiohttp.ClientSession() as s:
        for nome, tipo in (("x.mp3", "som"), ("y.PNG", "imagem"), ("z.webm", "video")):
            async with await _up(s, rt, nome) as r:
                assert r.status == 200 and await r.json() == {"ok": True, "tipo": tipo, "nome": nome}
        async with await _up(s, rt, "y.PNG") as r:
            assert (await r.json())["nome"] == "y (2).PNG"
        async with await _up(s, rt, "..\\..\\evil.png") as r:
            assert (await r.json())["nome"] == "evil.png"
        async with await _up(s, rt, "../../evil2.png") as r:
            assert (await r.json())["nome"] == "evil2.png"
        big = b"z" * (2 * 1024 * 1024)
        async with await _up(s, rt, "grande.mp4", big) as r:
            assert r.status == 200
    assert (paths.sons_dir() / "x.mp3").is_file() and (paths.imagens_dir() / "y (2).PNG").is_file()
    assert (paths.imagens_dir() / "evil.png").is_file() and (paths.videos_dir() / "z.webm").is_file()
    assert (paths.videos_dir() / "grande.mp4").stat().st_size == len(big)
    assert not (paths.data_dir().parent / "evil.png").exists()


async def test_upload_erros(rt, monkeypatch):
    from livehub import midia, paths
    async with aiohttp.ClientSession() as s:
        async with await _up(s, rt, "a.txt") as r:
            assert r.status == 415
        for ruim in ("CON.png", "...", "nul.mp3", ""):
            async with await _up(s, rt, ruim) as r:
                assert r.status == 400, ruim
        async with await _up(s, rt, "a.png", ct="application/json") as r:
            assert r.status in (403, 415)
        async with s.post(base(rt) + "/api/midia/enviar?nome=a.png", data=b"x", headers={"Content-Type": "application/octet-stream", "Origin": "http://evil.example"}) as r:
            assert r.status == 403
        monkeypatch.setitem(midia.TIPOS, ".png", ("imagem", paths.imagens_dir, 1000))
        async with await _up(s, rt, "g.png", b"x" * 5000) as r:
            assert r.status == 413 and "grande" in (await r.json())["erro"]
    assert not list(paths.imagens_dir().glob("*"))


async def test_testar_doacao_anuncio_e_mensagem_aleatoria(svc, monkeypatch):
    chamadas = []

    async def gerar(texto, cfg, anuncio=""):
        chamadas.append((texto, anuncio))
        return tts.tts_dir() / "t.mp3"
    monkeypatch.setattr(tts, "gerar", gerar)
    assert (await svc.testar("doacao")).items() >= {"ok": True, "tts": True}.items()
    texto, anuncio = chamadas[0]
    assert texto in MENSAGENS_TESTE and "doou" in anuncio and texto not in anuncio
    seq = iter(MENSAGENS_TESTE[:3])
    monkeypatch.setattr("livehub.alertas.servico.random.choice", lambda l: next(seq) if l is MENSAGENS_TESTE else l[0])
    await svc.testar("doacao")
    await svc.testar("doacao")
    assert chamadas[1][0] != chamadas[2][0]
    assert (await svc.testar("sub")).items() >= {"ok": True, "tts": True}.items()
    for cat in ("follow", "presente", "raid"):
        n = len(chamadas)
        assert await svc.testar(cat) == {"ok": True, "tts": None} and len(chamadas) == n


async def test_mensagem_cortada_na_tela_e_completa_no_tts(svc, monkeypatch):
    svc.clientes.add(svc.ws)
    falas = []

    async def gerar(texto, cfg, anuncio=""):
        falas.append((texto, anuncio))
        return None
    monkeypatch.setattr(tts, "gerar", gerar)
    svc.rt.cfg["alertas"]["estilo"]["mensagem_max"] = 30
    svc.rt.cfg["alertas"]["categorias"]["doacao"]["tts"] = True
    svc.rt.cfg["alertas"]["categorias"]["doacao"]["texto"] = "{user} doou {valor}! {mensagem}"
    longa = "palavra " * 20
    await ev(svc, "doação", "Ana", "R$ 10,00", longa.strip())
    await esperar(lambda: len(svc.ws.alertas()) == 1)
    a = svc.ws.alertas()[0]
    assert len(a["mensagem"]) <= 30 and a["mensagem"].endswith("…") and a["texto"].endswith(a["mensagem"])
    assert falas == [(longa.strip(), "Ana doou R$ 10,00!")]


async def test_sub_com_mensagem_de_resub(svc, monkeypatch):
    svc.clientes.add(svc.ws)
    falas = []

    async def gerar(texto, cfg, anuncio=""):
        falas.append((texto, anuncio))
        return None
    monkeypatch.setattr(tts, "gerar", gerar)
    svc.rt.cfg["alertas"]["categorias"]["sub"]["tts"] = True
    e = await ev(svc, "sub", "Ana", "", "Tier 1, 3 meses", mensagem="amo essa live")
    assert e["mensagem"] == "amo essa live" and regras.mensagem(e) == "amo essa live"
    await esperar(lambda: len(svc.ws.alertas()) == 1)
    assert falas == [("amo essa live", "Ana assinou! Tier 1, 3 meses")]


def test_regras_texto_sem_mensagem():
    e = {"tipo": "doação", "user": "A", "valor": "R$ 1,00", "detalhe": "oi"}
    assert regras.texto("{user} doou {valor}! {mensagem}", e, sem_mensagem=True) == "A doou R$ 1,00!"
    assert regras.texto("{user} {detalhe}", e, sem_mensagem=True) == "A"
    e = {"tipo": "sub", "user": "A", "detalhe": "Tier 1", "mensagem": "oi"}
    assert regras.texto("{user} {detalhe} {mensagem}", e, sem_mensagem=True) == "A Tier 1"


def test_twitch_resub_mensagem_e_youtube_membro():
    from livehub.chat import twitch, youtube
    r = twitch.interacao_usernotice({"msg-id": "resub", "display-name": "Ana", "msg-param-cumulative-months": "3",
                                     "msg-param-sub-plan": "1000", "system-msg": "Ana subscribed"}, " tmj demais ")
    assert r["mensagem"] == "tmj demais" and regras.mensagem(r) == "tmj demais"
    item = {"liveChatMembershipItemRenderer": {"id": "m1", "authorName": {"simpleText": "Bia"},
            "headerSubtext": {"simpleText": "Membro há 6 meses"}, "message": {"runs": [{"text": "feliz com o canal"}]}}}
    y = youtube.interacao_youtube(item)
    assert y["mensagem"] == "feliz com o canal" and y["tipo"] == "membro"


async def test_testar_avisa_quando_voz_desligada_na_live(svc, monkeypatch):
    async def gerar(texto, cfg, anuncio=""):
        return tts.tts_dir() / "t.mp3"
    monkeypatch.setattr(tts, "gerar", gerar)
    svc.rt.cfg["alertas"]["categorias"]["doacao"]["tts"] = False
    svc.rt.cfg["alertas"]["tts"]["minimo"] = 5
    r = await svc.testar("doacao")
    assert r["tts"] is True and r["tts_na_live"] is False and r["tts_minimo"] == 5
    svc.rt.cfg["alertas"]["categorias"]["doacao"]["tts"] = True
    assert (await svc.testar("doacao"))["tts_na_live"] is True
