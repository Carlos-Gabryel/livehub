import logging
from aiohttp import web, WSMsgType

from .. import paths
from ..web import origem_local, seguro

log = logging.getLogger("dock")
PLATAFORMAS = {"twitch", "youtube", "kick", "tiktok", "streamelements"}


def static_dir():
    return paths.resource_dir() / "chat" / "static"


def _ler(nome):
    return (static_dir() / nome).read_text(encoding="utf-8")


def add_routes(app, hub):
    """Rotas do chat/feed."""

    async def page(request):
        return web.Response(text=_ler("dock.html"), content_type="text/html")

    async def neutro(request):
        return web.Response(text=_ler("neutro.html"), content_type="text/html")

    async def ws_handler(request):
        if not origem_local(request):
            return web.Response(status=403)
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)
        await ws.send_json({"type": "history", "messages": list(hub.history), "status": hub.status})
        hub.dock_clients.add(ws)
        try:
            async for m in ws:
                if m.type == WSMsgType.ERROR:
                    break
        finally:
            hub.dock_clients.discard(ws)
        return ws

    async def status(request):
        return web.json_response({"status": hub.status, "dock_clients": len(hub.dock_clients),
                                  "sb_clients": len(hub.sb_clients), "history": len(hub.history)})

    async def eventos(request):
        return web.Response(text=_ler("eventos.html"), content_type="text/html")

    async def eventos_ws(request):
        if not origem_local(request):
            return web.Response(status=403)
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)
        await ws.send_json({"type": "history", "eventos": list(hub.interacoes), "status": hub.status})
        hub.feed_clients.add(ws)
        try:
            async for m in ws:
                if m.type == WSMsgType.ERROR:
                    break
        finally:
            hub.feed_clients.discard(ws)
        return ws

    async def eventos_novo(request):
        if not seguro(request):
            return web.Response(status=403)
        try:
            d = await request.json()
        except Exception:
            return web.Response(status=400)
        if not isinstance(d, dict) or d.get("platform") not in PLATAFORMAS:
            return web.Response(status=400)
        campos = {}
        for k in ("tipo", "user", "detalhe", "valor", "id"):
            v = d.get(k)
            if v is None and k in ("detalhe", "valor", "id"):
                campos[k] = None if k == "id" else ""
            elif isinstance(v, str) and len(v) <= 200 and (v or k not in ("tipo", "user")):
                campos[k] = v
            else:
                return web.Response(status=400)
        await hub.interacao(d["platform"], **campos)
        return web.Response(status=204)

    async def para_config(request):
        raise web.HTTPFound("/config")

    async def para_config_se(request):
        raise web.HTTPFound("/config#streamelements")

    app.router.add_get("/eventos", eventos)
    app.router.add_get("/eventos/ws", eventos_ws)
    app.router.add_post("/eventos/novo", eventos_novo)
    app.router.add_get("/eventos/config", para_config_se)
    app.router.add_get("/dock", page)
    app.router.add_get("/", para_config)
    app.router.add_get("/neutro", neutro)
    app.router.add_static("/fonts", static_dir() / "fonts")
    app.router.add_get("/dock/ws", ws_handler)
    app.router.add_get("/status", status)
