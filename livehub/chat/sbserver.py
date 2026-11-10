"""Emulacao minima do servidor WebSocket do Streamer.bot (subconjunto usado pelo overlay do nutty.gg)."""
import json, logging, uuid
from aiohttp import web, WSMsgType

log = logging.getLogger("sb")
ORIGENS = {"https://widgets.nutty.gg"}  # overlay que usa esta emulacao; sem Origin = cliente local

EVENTS = {
    "Twitch": ["ChatMessage", "Cheer", "AutomaticRewardRedemption", "Announcement", "Follow", "Sub", "ReSub",
               "GiftSub", "RewardRedemption", "CustomPowerUpRedemption", "Raid", "WatchStreak",
               "ChatMessageDeleted", "UserBanned", "UserTimedOut", "ChatCleared", "SharedChatMessageDeleted",
               "SharedChatUserBanned", "SharedChatUserTimedout"],
    "YouTube": ["Message", "SuperChat", "SuperSticker", "JewelsGifted", "NewSubscriber", "NewSponsor",
                "GiftMembershipReceived"],
    "Kick": ["ChatMessage", "Follow", "Subscription", "Resubscription", "GiftSubscription", "RewardRedemption"],
    "Streamlabs": ["Donation"], "StreamElements": ["Tip"], "Patreon": ["PledgeCreated"],
    "Kofi": ["Donation", "Subscription", "Resubscription", "ShopOrder"], "TipeeeStream": ["Donation"],
    "Fourthwall": ["OrderPlaced", "Donation", "SubscriptionPurchased", "GiftPurchase", "GiftDrawStarted",
                   "GiftDrawEnded"],
}
INSTANCE = str(uuid.uuid4())


def _info():
    return {"instanceId": INSTANCE, "name": "LiveHub (emulacao Streamer.bot)", "version": "1.0.0",
            "os": "windows", "osVersion": "", "server": "127.0.0.1", "time": ""}


def make_app(hub, cfg):
    async def handler(request):
        if request.headers.get("Upgrade", "").lower() != "websocket":
            return web.Response(text="LiveHub: emulacao Streamer.bot WebSocket\n")
        org = request.headers.get("Origin")
        if request.remote not in ("127.0.0.1", "::1") or (org is not None and org not in ORIGENS):
            return web.Response(status=403)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        hub.sb_clients[ws] = {}
        await ws.send_json({"request": "Hello", "info": _info()})
        try:
            async for m in ws:
                if m.type != WSMsgType.TEXT:
                    continue
                try:
                    req = json.loads(m.data)
                except ValueError:
                    continue
                await _handle(ws, req, hub, cfg)
        finally:
            hub.sb_clients.pop(ws, None)
        return ws

    app = web.Application()
    app.router.add_get("/", handler)
    return app


async def _handle(ws, req, hub, cfg):
    rid, name = req.get("id"), req.get("request")
    ok = {"id": rid, "status": "ok"}
    if name == "Subscribe":
        subs = hub.sb_clients[ws]
        for src, types in (req.get("events") or {}).items():
            subs.setdefault(src, set()).update(types)
        ok["events"] = req.get("events") or {}
    elif name == "UnSubscribe":
        subs = hub.sb_clients[ws]
        for src, types in (req.get("events") or {}).items():
            subs.get(src, set()).difference_update(types)
    elif name == "GetEvents":
        ok["events"] = EVENTS
    elif name == "GetInfo":
        ok["info"] = _info()
    elif name == "GetBroadcaster":
        tw, yt, kk = cfg.get("twitch", {}), cfg.get("youtube", {}), cfg.get("kick", {})
        plat = {}
        if tw.get("channel"):
            plat["twitch"] = {"broadcastUser": tw["channel"], "broadcastUserName": tw["channel"],
                              "broadcastUserId": "", "broadcastIsAffiliate": False, "broadcastIsPartner": False}
        if yt.get("handle"):
            plat["youtube"] = {"broadcastUser": yt["handle"], "broadcastUserName": yt["handle"],
                               "broadcastUserId": ""}
        if kk.get("channel"):
            plat["kick"] = {"broadcasterLogin": kk["channel"], "broadcasterName": kk["channel"]}
        ok["platforms"] = plat
        ok["connected"] = list(plat)
    elif name == "GetUserPronouns":
        ok["pronoun"] = {"userFound": False}
    elif name == "GetActiveViewers":
        ok["viewers"] = []
    elif name in ("TwitchGetEmotes", "YouTubeGetEmotes"):
        ok["emotes"] = []
    # qualquer outro pedido: responde ok vazio (nao envia nada a lugar nenhum)
    await ws.send_json(ok)
