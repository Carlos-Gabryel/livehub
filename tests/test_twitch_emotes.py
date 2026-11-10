import pytest

from livehub.chat.twitch import Twitch


class Resp:
    def __init__(self, data, falha=False):
        self.data, self.falha = data, falha

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        if self.falha:
            raise RuntimeError("http")

    async def json(self, content_type=None):
        return self.data


class Sess:
    def __init__(self, ivr_falha=False):
        self.urls, self.ivr_falha = [], ivr_falha

    def get(self, url, **kw):
        self.urls.append(url)
        if "ivr.fi" in url:
            return Resp([{"id": "777"}], self.ivr_falha)
        if "7tv.io/v3/users/twitch/" in url:
            return Resp({"emote_set": {"emotes": [{"name": "Canal7", "data": {"host": {"url": "//cdn/x"}}}]}})
        return Resp({"emotes": []} if "7tv" in url else [])


def mk(sess, creds):
    cfg = {"twitch": {"channel": "canal", "client_id": "a" if creds else "", "client_secret": "b" if creds else ""}}
    return Twitch(cfg, None, sess)


async def test_com_credenciais_usa_helix(monkeypatch):
    t, sess = mk(Sess(), True), None
    sess = t.s

    async def get(path, **p):
        return [{"id": "555"}]
    monkeypatch.setattr(t.helix, "get", get)
    await t.load_third_party()
    assert not any("ivr.fi" in u for u in sess.urls)
    assert any(u.endswith("/users/twitch/555") for u in sess.urls) and "Canal7" in t.third


async def test_sem_credenciais_usa_ivr():
    t = mk(Sess(), False)
    await t.load_third_party()
    assert any("ivr.fi" in u for u in t.s.urls)
    assert any(u.endswith("/users/twitch/777") for u in t.s.urls) and "Canal7" in t.third
    n = len(t.s.urls)
    await t.load_third_party()  # cache: ivr nao e chamado de novo
    assert sum("ivr.fi" in u for u in t.s.urls) == 1 and len(t.s.urls) > n


async def test_ivr_falha_sem_emotes_de_canal():
    t = mk(Sess(ivr_falha=True), False)
    await t.load_third_party()
    assert "Canal7" not in t.third
    assert not any("/users/twitch/" in u for u in t.s.urls)
