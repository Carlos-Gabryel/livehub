"""Guardas comuns de Host/Origin (anti DNS-rebinding/CSRF) usando a porta configurada."""
from aiohttp import web

WEB_PORT = web.AppKey("web_port", int)


def hosts_locais(porta: int) -> set:
    return {f"127.0.0.1:{porta}", f"localhost:{porta}"}


def origem_local(request) -> bool:
    """Remote local, Host do proprio servico e Origin (se houver) do proprio servico."""
    hosts = hosts_locais(request.app[WEB_PORT])
    if request.remote not in ("127.0.0.1", "::1") or request.headers.get("Host") not in hosts:
        return False
    org = request.headers.get("Origin")
    return org is None or org in {"http://" + h for h in hosts}


def seguro(request, tipos=("application/json",)) -> bool:
    """POST so do proprio PC: origem local e content-type esperado."""
    return origem_local(request) and request.content_type in tipos


@web.middleware
async def seguranca(request, handler):
    """Barra Host/Origin estranhos em toda a aplicacao; POST em /api exige JSON."""
    if not origem_local(request):
        raise web.HTTPForbidden(text="Host/Origin nao permitido")
    if request.method == "POST" and request.path.startswith("/api/"):
        tipo = "application/octet-stream" if request.path == "/api/midia/enviar" else "application/json"
        if request.content_type != tipo:
            raise web.HTTPUnsupportedMediaType(text="Use Content-Type: " + tipo)
    return await handler(request)
