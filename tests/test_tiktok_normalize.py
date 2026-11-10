"""Normalizacao de comentarios do TikTok (ex-tiktok_normalize_test)."""
from types import SimpleNamespace as NS
from livehub.chat.tiktok import normalize


def ev(content, uid="viewer1", nick="Viewer", mod=False, emotes=(), mid=123):
    em = [NS(index=i, emote=NS(emote_id=n, image=NS(url_list=[u]))) for i, n, u in emotes]
    return NS(content=content, user=NS(unique_id=uid, nickname=nick, id=42, is_moderator=mod),
              emotes=em, common=NS(msg_id=mid))



def test_normalize():
    d = normalize(ev("oi pessoal"))
    assert d == {"platform": "tiktok", "id": "123", "user_id": "viewer1", "user": "Viewer",
                 "parts": [{"t": "text", "v": "oi pessoal"}], "badges": []}, d

    d = normalize(ev("oi ", emotes=[(3, "e1", "https://x/e1.png")]))
    assert d["parts"] == [{"t": "text", "v": "oi "}, {"t": "emote", "url": "https://x/e1.png", "name": "e1"}], d

    d = normalize(ev("a", emotes=[(0, "e1", "http://x/e1.png")]))
    assert d["parts"] == [{"t": "text", "v": "a"}], d  # emote nao-https ignorado

    d = normalize(ev("x", mod=True))
    assert d["mod"] and d["badges"] == [{"title": "moderator"}]

    d = normalize(ev("x", uid="STREAMER"), owner_id="streamer")
    assert d["owner"] and d["badges"] == [{"title": "broadcaster"}]

    d = normalize(ev("x", uid="", nick=""))
    assert d["user"] == "?" and d["user_id"] == "42"
    assert normalize(ev("")) is None
