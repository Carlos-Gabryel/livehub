import aiohttp
import pytest

from livehub import settings
from livehub.chat.kick import Kick
from livehub.resgates import audio
from livehub.runtime import Runtime
from conftest import porta_livre

SEGREDO, TOKEN, JWT = "segredo-xyz-123", "tok-acesso-abc", "jwt-secreto-777"


@pytest.fixture
async def rt(data_dir, monkeypatch):
    cfg = settings.load_config()
    cfg["web_port"] = porta_livre()
    settings.save_config(cfg)
    sec = settings.load_secrets()
    sec["twitch"].update(client_id="cid1", client_secret=SEGREDO, access_token=TOKEN, refresh_token="refresh-qqq",
                         user_id="42", login="ana")
    sec["streamelements"]["jwt"] = JWT
    settings.save_secrets(sec)
    monkeypatch.setattr(audio, "listar_saidas", lambda: ["Alto-falante", "Fone"])
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


async def post(rt, corpo, **kw):
    async with aiohttp.ClientSession() as s:
        async with s.post(b(rt) + "/api/config", json=corpo, **kw) as r:
            return r.status, await r.json()


async def test_pagina_e_redirects(rt):
    async with aiohttp.ClientSession() as s:
        async with s.get(b(rt) + "/config") as r:
            assert r.status == 200 and "text/html" in r.headers["Content-Type"]
            assert "Configurações" in await r.text()
        async with s.get(b(rt) + "/", allow_redirects=False) as r:
            assert r.status == 302 and r.headers["Location"] == "/config"
        async with s.get(b(rt) + "/eventos/config", allow_redirects=False) as r:
            assert r.status == 302 and r.headers["Location"] == "/config#streamelements"


async def test_get_nunca_vaza_segredos(rt):
    async with aiohttp.ClientSession() as s:
        async with s.get(b(rt) + "/api/config") as r:
            txt = await r.text()
            j = await r.json()
    for v in (SEGREDO, TOKEN, JWT, "refresh-qqq"):
        assert v not in txt
    assert j["twitch"] == {"client_id": "cid1", "has_secret": True, "login": "ana", "user_id": "42", "connected": True,
                    "assinaturas": {"follow": "inativo", "resgates": "inativo"}}
    assert j["streamelements"]["has_jwt"] is True
    assert j["audio"]["saidas"] == ["Alto-falante", "Fone"]
    assert j["urls"]["dock"].endswith(f":{rt.cfg['web_port']}/dock")


async def test_post_nao_vaza_e_recarrega_uma_vez(rt):
    st, j = await post(rt, {"twitch": {"channel": "https://twitch.tv/MeuCanal"}})
    assert st == 200 and j["config"]["twitch"]["channel"] == "meucanal"
    assert SEGREDO not in str(j) and JWT not in str(j) and TOKEN not in str(j)
    assert rt.reload_n == 1
    st, _ = await post(rt, {"web_port": 80})
    assert st == 400 and rt.reload_n == 1


@pytest.mark.parametrize("corpo,campo", [
    ({"web_port": 80}, "web_port"),
    ({"web_port": 70000}, "web_port"),
    ({"web_port": "abc"}, "web_port"),
    ({"streamerbot_compat": {"port": 99999}}, "streamerbot_compat.port"),
    ({"kick": {"channel": "a b"}}, "kick.channel"),
    ({"twitch": {"channel": "x" * 201}}, "twitch.channel"),
    ({"tiktok": {"username": "a/b"}}, "tiktok.username"),
    ({"youtube": {"check_interval": 14}}, "youtube.check_interval"),
    ({"tiktok": {"check_interval": 601}}, "tiktok.check_interval"),
    ({"kick": {"chatroom_id": -3}}, "kick.chatroom_id"),
])
async def test_validacoes(rt, corpo, campo):
    st, j = await post(rt, corpo)
    assert st == 400 and j["campo"] == campo and j["erro"]
    assert rt.reload_n == 0


async def test_portas_iguais(rt):
    p = rt.cfg["web_port"]
    st, j = await post(rt, {"streamerbot_compat": {"port": p}})
    assert st == 400 and j["campo"] == "streamerbot_compat.port"


async def test_normalizacao(rt, monkeypatch):
    async def ids(self):
        self.chatroom_id, self.channel_id = 11, 22
    monkeypatch.setattr(Kick, "resolve_ids", ids)
    st, j = await post(rt, {"kick": {"channel": "https://kick.com/Fulano/"},
                            "youtube": {"handle": "https://www.youtube.com/@Canal.X"},
                            "tiktok": {"username": "@user_1"}})
    c = j["config"]
    assert st == 200
    assert c["kick"] == {"channel": "fulano", "chatroom_id": 11, "channel_id": 22}
    assert j["kick"]["resolvido"] is True
    assert c["youtube"]["handle"] == "Canal.X" and c["tiktok"]["username"] == "user_1"
    st, j = await post(rt, {"youtube": {"handle": "@abc"}})
    assert j["config"]["youtube"]["handle"] == "abc"
    st, j = await post(rt, {"youtube": {"handle": "youtube.com/channel/UCxyz"}})
    assert st == 400


async def test_kick_falha_e_troca_de_slug_limpa_ids(rt, monkeypatch):
    async def falha(self):
        raise RuntimeError("x")
    monkeypatch.setattr(Kick, "resolve_ids", falha)
    st, j = await post(rt, {"kick": {"channel": "novo"}})
    assert st == 200 and j["kick"] == {"resolvido": False, "precisa_manual": True}
    st, j = await post(rt, {"kick": {"channel": "novo", "chatroom_id": 5, "channel_id": "6"}})
    assert j["kick"]["precisa_manual"] is False and j["config"]["kick"]["chatroom_id"] == 5
    assert j["config"]["kick"]["channel_id"] == 6
    st, j = await post(rt, {"kick": {"channel": "outro"}})  # slug mudou: ids antigos somem
    assert j["config"]["kick"]["chatroom_id"] is None and j["kick"]["precisa_manual"] is True


async def test_segredo_vazio_mantem_e_client_id_limpa_tokens(rt):
    st, _ = await post(rt, {"twitch": {"client_id": "cid1", "client_secret": ""}})
    t = settings.load_secrets()["twitch"]
    assert st == 200 and t["client_secret"] == SEGREDO and t["refresh_token"] == "refresh-qqq"
    st, _ = await post(rt, {"twitch": {"client_id": "novo-id"}})
    t = settings.load_secrets()["twitch"]
    assert t["client_id"] == "novo-id" and t["client_secret"] == SEGREDO
    assert t["access_token"] == t["refresh_token"] == t["user_id"] == t["login"] == ""
    st, j = await post(rt, {"twitch": {"client_secret": "outro"}})
    assert j["twitch"]["connected"] is False and settings.load_secrets()["twitch"]["client_secret"] == "outro"


async def test_jwt_mantem_e_remove(rt):
    await post(rt, {"streamelements": {"jwt": ""}})
    assert settings.load_secrets()["streamelements"]["jwt"] == JWT
    st, j = await post(rt, {"streamelements": {"remover": True}})
    assert settings.load_secrets()["streamelements"]["jwt"] == "" and j["streamelements"]["has_jwt"] is False


async def test_web_port_restart(rt):
    nova = porta_livre()
    st, j = await post(rt, {"web_port": nova})
    assert st == 200 and j["restart_required"] is True and settings.load_config()["web_port"] == nova


async def test_audio_testar_e_sons(rt, monkeypatch):
    chamadas = []
    monkeypatch.setattr(audio, "tocar_dados", lambda d, t, v, dev: chamadas.append((len(d), t, v, dev)))
    aberto = []
    monkeypatch.setattr("os.startfile", aberto.append, raising=False)
    async with aiohttp.ClientSession() as s:
        async with s.post(b(rt) + "/api/audio/testar", json={"device": "Fone"}) as r:
            assert r.status == 200
        async with s.get(b(rt) + "/api/audio/saidas") as r:
            assert (await r.json())["saidas"] == ["Alto-falante", "Fone"]
        async with s.post(b(rt) + "/api/abrir-sons", json={}) as r:
            assert r.status in (200, 501)
    assert chamadas == [(int(44100 * 0.4), 44100, 0.3, "Fone")]


async def test_origem_estranha_403(rt):
    for url in ("/api/config", "/api/audio/testar", "/api/abrir-sons"):
        async with aiohttp.ClientSession() as s:
            async with s.post(b(rt) + url, json={}, headers={"Origin": "http://evil.com"}) as r:
                assert r.status == 403, url
    assert rt.reload_n == 0


async def test_recompensas_tem_seletor_de_saida(rt):
    async with aiohttp.ClientSession() as s:
        async with s.get(b(rt) + "/recompensas") as r:
            t = await r.text()
        async with s.get(b(rt) + "/config") as r:
            c = await r.text()
    assert 'id="saida"' in t and "/api/audio/testar" in t and "/config#twitch" in t
    assert "Saída de som das recompensas" in t and "Saída de som das recompensas" in c
    assert "Opção 1" in c and "Opção 2" in c and "Sem login você perde:" in c and "adges da Twitch no chat" in c
    assert "Só é necessário se você quiser usar as recompensas" not in c
    assert "Seu canal ainda não é Afiliado/Parceiro" in t


async def test_saida_alterada_vale_no_proximo_play(rt, monkeypatch):
    import asyncio
    chamadas = []
    monkeypatch.setattr(audio, "tocar", lambda caminho, vol, dev: chamadas.append(dev))
    st, _ = await post(rt, {"audio": {"output_device": "Fone"}})
    assert st == 200
    rt.svc.player.enfileirar("x.wav", 50)
    for _ in range(50):
        if chamadas:
            break
        await asyncio.sleep(0.05)
    assert chamadas == ["Fone"]


@pytest.mark.parametrize("entrada", ["MeuCanal", "@MeuCanal", "https://www.twitch.tv/MeuCanal", "twitch.tv/meucanal/", "https://twitch.tv/MeuCanal?x=1"])
async def test_twitch_canal_normaliza(rt, entrada):
    st, j = await post(rt, {"twitch": {"channel": entrada}})
    assert st == 200 and j["config"]["twitch"]["channel"] == "meucanal"


async def test_twitch_canal_invalido(rt):
    st, j = await post(rt, {"twitch": {"channel": "a b"}})
    assert st == 400 and j["campo"] == "twitch.channel"
