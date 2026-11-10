import html, logging
from aiohttp import web, WSMsgType

from .. import paths, settings
from ..web import origem_local, seguro

log = logging.getLogger("dock")
PLATAFORMAS = {"twitch", "youtube", "kick", "tiktok", "streamelements"}


def static_dir():
    return paths.resource_dir() / "chat" / "static"


def _ler(nome):
    return (static_dir() / nome).read_text(encoding="utf-8")


CONFIG_HTML = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>StreamElements</title><style>body{margin:0;padding:20px;background:#0e0e10;color:#efeff1;font:14px/1.5 system-ui,sans-serif;max-width:560px}
input{width:100%;padding:8px;background:#18181b;color:#efeff1;border:1px solid #333;border-radius:4px;margin:8px 0}
button{background:#9146ff;color:#fff;border:0;border-radius:4px;padding:8px 16px;font-weight:600;cursor:pointer}
.m{color:#3ddc84;font-weight:600}.d{color:#898990}a{color:#9146ff}</style></head><body>
<h3>Token do StreamElements</h3>
<p class="d">StreamElements &rarr; Minha conta (Account) &rarr; Canais (Channels) &rarr; Mostrar segredos (Show secrets) &rarr; JWT Token</p>
<p class="m">@@MSG@@</p>
<form method="post" action="/eventos/config"><input type="password" name="jwt" placeholder="JWT Token" autocomplete="off"><button>Salvar</button></form>
<p class="d">Status atual: <b>@@ST@@</b></p><p><a href="/eventos">&larr; voltar aos eventos</a></p></body></html>"""


def add_routes(app, hub, on_token=None):
    """Rotas do chat/feed. on_token: async callable chamado apos salvar o JWT do StreamElements."""

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

    def pagina_config(msg=""):
        st = html.escape(hub.status.get("streamelements", ""))
        return web.Response(content_type="text/html", text=CONFIG_HTML.replace("@@MSG@@", html.escape(msg)).replace("@@ST@@", st))

    async def config_get(request):
        return pagina_config()

    async def config_post(request):
        if not seguro(request, ("application/x-www-form-urlencoded",)):
            return web.Response(status=403)
        jwt = ((await request.post()).get("jwt") or "").strip()
        if not jwt or len(jwt) > 4000 or any(c.isspace() for c in jwt):
            return pagina_config("Token vazio ou invalido.")
        try:
            sec = settings.load_secrets()
            sec["streamelements"]["jwt"] = jwt
            settings.save_secrets(sec)
        except Exception as e:
            log.warning("falha ao salvar secrets.json: %r", type(e).__name__)
            return pagina_config("Falha ao salvar o token.")
        if on_token:
            await on_token()
        return pagina_config("Token salvo.")

    app.router.add_get("/eventos", eventos)
    app.router.add_get("/eventos/ws", eventos_ws)
    app.router.add_post("/eventos/novo", eventos_novo)
    app.router.add_get("/eventos/config", config_get)
    app.router.add_post("/eventos/config", config_post)
    app.router.add_get("/dock", page)
    app.router.add_get("/", page)
    app.router.add_get("/neutro", neutro)
    app.router.add_static("/fonts", static_dir() / "fonts")
    app.router.add_get("/dock/ws", ws_handler)
    app.router.add_get("/status", status)
