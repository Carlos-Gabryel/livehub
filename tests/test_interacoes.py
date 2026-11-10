"""Normalizacao de interacoes (ex-interacoes_test)."""
from types import SimpleNamespace as NS
from livehub.chat.twitch import interacao_usernotice, interacao_bits
from livehub.chat.youtube import interacao_youtube
from livehub.chat.tiktok import interacao_gift
from livehub.chat.kick import interacao_kick
from livehub.chat.streamelements import normalize


def test_interacoes():

    # Twitch: bomba de 10 = 1 linha so
    bomba = {"msg-id": "submysterygift", "display-name": "Zed", "msg-param-mass-gift-count": "10", "id": "a"}
    filho = {"msg-id": "subgift", "display-name": "Zed", "msg-param-recipient-display-name": "Bob",
             "msg-param-community-gift-id": "123", "id": "b"}
    solto = {"msg-id": "subgift", "display-name": "Zed", "msg-param-recipient-display-name": "Bob", "id": "c"}
    r = interacao_usernotice(bomba)
    assert r["tipo"] == "presente de sub" and r["valor"] == "10 subs" and r["user"] == "Zed", r
    assert interacao_usernotice(filho) is None
    assert interacao_usernotice(solto)["detalhe"] == "para Bob"
    r = interacao_usernotice({"msg-id": "resub", "display-name": "Ana", "msg-param-sub-plan": "1000", "msg-param-cumulative-months": "5"})
    assert r["detalhe"] == "Tier 1, 5 meses", r
    assert interacao_usernotice({"msg-id": "sub", "display-name": "Ana", "msg-param-sub-plan": "Prime"})["detalhe"] == "Prime"
    r = interacao_usernotice({"msg-id": "raid", "msg-param-displayName": "Raider", "msg-param-viewerCount": "42"})
    assert r["tipo"] == "raid" and r["valor"] == "42 viewers"
    assert interacao_usernotice({"msg-id": "ritual"}) is None
    r = interacao_bits({"bits": "100", "display-name": "Ana", "id": "z"}, "Cheer100 boa live")
    assert r["valor"] == "100 bits" and r["detalhe"] == "boa live", r
    assert interacao_bits({"display-name": "A"}, "oi") is None

    # YouTube: membro
    item = {"liveChatMembershipItemRenderer": {"id": "m1", "authorName": {"simpleText": "Carol"},
            "headerSubtext": {"runs": [{"text": "Bem-vindo a "}, {"text": "Canal!"}]}}}
    r = interacao_youtube(item)
    assert r["tipo"] == "membro" and r["user"] == "Carol" and r["detalhe"] == "Bem-vindo a Canal!", r
    r = interacao_youtube({"liveChatPaidMessageRenderer": {"id": "p", "authorName": {"simpleText": "D"},
                           "purchaseAmountText": {"simpleText": "R$ 10,00"}, "message": {"runs": [{"text": "oi"}]}}})
    assert r["tipo"] == "super chat" and r["valor"] == "R$ 10,00" and r["detalhe"] == "oi", r
    assert interacao_youtube({"liveChatTextMessageRenderer": {}}) is None

    # TikTok: streak so publica no fim
    def gift(streaking, n):
        return NS(streaking=streaking, repeat_count=n, gift=NS(name="Rosa", diamond_count=1), user=NS(nickname="Eva", unique_id="eva"),
                  common=NS(msg_id=n))
    assert interacao_gift(gift(True, 3)) is None
    r = interacao_gift(gift(False, 5))
    assert r["valor"] == "5x Rosa (5 💎)" and r["user"] == "Eva", r

    # Kick
    r = interacao_kick("App\\Events\\GiftedSubscriptionsEvent", {"gifter_username": "G", "gifted_usernames": ["a", "b"]})
    assert r["valor"] == "2 subs" and r["user"] == "G", r
    assert interacao_kick("App\\Events\\FollowersUpdated", {"followed": False, "username": "x"}) is None
    assert interacao_kick("App\\Events\\FollowersUpdated", {"followed": True, "username": "x"})["tipo"] == "follow"

    # StreamElements
    r = normalize({"type": "tip", "data": {"username": "Dono", "amount": 5, "currency": "BRL", "message": "valeu"}}, True)
    assert r["valor"] == "5 BRL" and r["detalhe"] == "(teste) valeu" and r["tipo"] == "doação", r
    assert normalize({"type": "follow", "data": {}}) is None and normalize("lixo") is None
