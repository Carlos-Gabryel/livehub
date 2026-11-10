"""Servico de resgates: EventSub -> fila de sons + feed de interacoes (em processo) e rotas da UI."""
import collections, logging, time
from aiohttp import web

from .. import paths, settings
from ..web import seguro
from . import const, sons
from .auth import Auth
from .eventsub import EventSub
from .player import Player

log = logging.getLogger("resgates")


class Servico:
    def __init__(self, session, hub):
        self.hub, self.session = hub, session
        self.auth = Auth(session)
        self.player = Player(lambda: settings.load_config()["audio"]["output_device"])
        self.eventsub = EventSub(self.auth, self.tratar_resgate, self.tratar_follow)
        self.resgates = collections.deque(maxlen=50)

    async def _feed(self, **ev):
        try:
            await self.hub.interacao(**ev)
        except Exception as e:  # o feed nunca derruba os resgates
            log.warning("Falha ao publicar no feed: %r", e)

    async def tratar_follow(self, ev: dict):
        quem = ev.get("user_name") or ev.get("user_login", "?")
        log.info("Follow: %s", quem)
        await self._feed(platform="twitch", tipo="follow", user=quem, detalhe="", valor="",
                         id=f"follow-{ev.get('user_id')}-{ev.get('followed_at')}")

    async def tratar_resgate(self, ev: dict):
        rid, titulo = ev["reward"]["id"], ev["reward"]["title"]
        quem = ev.get("user_name") or ev.get("user_login", "?")
        m = settings.load_mapeamento().get(rid)
        status = "sem mapeamento"
        if m and m.get("som"):
            p = sons.resolve_som(m["som"])
            if p:
                self.player.enfileirar(p, m.get("volume", 100), f"{titulo} ({quem})")
                status = "enfileirado"
            else:
                status = "som não encontrado"
                log.error("Som do mapeamento não encontrado: %s", m["som"])
        log.info("Resgate: '%s' por %s (reward_id=%s) -> %s", titulo, quem, rid, status)
        self.resgates.appendleft({"hora": time.strftime("%H:%M:%S"), "titulo": titulo, "usuario": quem,
                                  "reward_id": rid, "status": status})
        await self._feed(platform="twitch", tipo="resgate", user=quem, detalhe=titulo,
                         valor=f"{ev['reward'].get('cost', '')} pts", id=ev.get("id"))

    async def eventsub_main(self):
        """Valida o token (guarda user_id/login) e roda o EventSub; fica ocioso enquanto nao logado."""
        if self.auth.logado:
            await self.auth.validar(forcar=True)
        await self.eventsub.run()


def add_routes(app: web.Application, svc: Servico):
    r = web.RouteTableDef()

    @r.get("/recompensas")
    async def index(req):
        return web.FileResponse(paths.resource_dir() / "resgates" / "index.html")

    @r.get("/api/status")
    async def status(req):
        esc = await svc.auth.validar() if svc.auth.logado else None
        return web.json_response({
            "logado": svc.auth.logado, **svc.eventsub.status(), "canal": svc.auth.login,
            "configurado": svc.auth.configurado, "erro_login": svc.auth.ultimo_erro,
            "modo": svc.auth.modo, "login_expirado": svc.auth.login_expirado,
            "pode_gerenciar": None if esc is None else const.ESCOPO_GERENCIAR in esc,
            "pode_seguidores": None if esc is None else const.ESCOPO_SEGUIDORES in esc,
            "player": svc.player.estado()})

    @r.get("/login")
    async def login(req):
        try:
            url = await svc.auth.iniciar_login()
        except RuntimeError as e:
            return web.Response(status=400, text=str(e))
        except OSError as e:
            return web.Response(status=500, text=f"Não consegui abrir a porta {const.AUTH_PORT}: {e}")
        raise web.HTTPFound(url)

    def _proibido():
        return web.json_response({"erro": "Origem não permitida."}, status=403)

    @r.post("/api/twitch/dispositivo/iniciar")
    async def disp_iniciar(req):
        if not seguro(req):
            return _proibido()
        try:
            return web.json_response(await svc.auth.iniciar_dispositivo())
        except Exception as e:
            log.warning("Login por codigo nao iniciou: %r", type(e).__name__)
            return web.json_response({"erro": "Não consegui falar com a Twitch. Tente de novo."}, status=502)

    @r.get("/api/twitch/dispositivo/estado")
    async def disp_estado(req):
        return web.json_response(svc.auth.estado_dispositivo())

    @r.post("/api/twitch/dispositivo/cancelar")
    async def disp_cancelar(req):
        if not seguro(req):
            return _proibido()
        await svc.auth.cancelar_dispositivo()
        return web.json_response({"ok": True})

    @r.post("/api/twitch/modo")
    async def twitch_modo(req):
        if not seguro(req):
            return _proibido()
        try:
            b = await req.json()
            modo = b.get("modo") if isinstance(b, dict) else None
            mudou = await svc.auth.definir_modo(modo)
        except (ValueError, RuntimeError) as e:
            return web.json_response({"erro": str(e)}, status=400)
        except Exception:
            return web.json_response({"erro": "JSON inválido."}, status=400)
        if mudou and svc.auth.on_reload:
            await svc.auth.on_reload()
        return web.json_response({"ok": True, "modo": svc.auth.modo})

    @r.post("/api/logout")
    async def logout(req):
        svc.auth.logout()
        return web.json_response({"ok": True})

    @r.get("/api/recompensas")
    async def recompensas(req):
        if not svc.auth.logado:
            return web.json_response({"erro": "não logado"}, status=401)
        if not svc.auth.user_id:
            await svc.auth.validar(forcar=True)
        st, j = await svc.auth.api("GET", "/channel_points/custom_rewards",
                                   params={"broadcaster_id": svc.auth.user_id})
        if st == 403:  # canal sem Afiliado/Parceiro
            return web.json_response({"erro": "não é afiliado", "nao_afiliado": True}, status=403)
        if st != 200:
            return web.json_response({"erro": j.get("message", str(j))}, status=502)
        mapa = settings.load_mapeamento()
        return web.json_response([{"id": x["id"], "titulo": x["title"], "custo": x["cost"],
                                   "ativa": x.get("is_enabled"), "mapa": mapa.get(x["id"])} for x in j["data"]])

    @r.get("/api/sons")
    async def lista_sons(req):
        return web.json_response(sons.listar_sons())

    @r.post("/api/mapeamento")
    async def salvar(req):
        b = await req.json()
        rid, som, vol = b.get("reward_id"), b.get("som"), b.get("volume", 100)
        mapa = settings.load_mapeamento()
        if not rid:
            return web.json_response({"erro": "reward_id ausente"}, status=400)
        if not som:  # sem som = remove o mapeamento
            mapa.pop(rid, None)
        else:
            if not sons.resolve_som(som):
                return web.json_response({"erro": "som inválido"}, status=400)
            mapa[rid] = {"titulo": b.get("titulo", ""), "som": som, "volume": max(0, min(100, int(vol)))}
        settings.save_mapeamento(mapa)
        return web.json_response({"ok": True})

    @r.post("/api/testar")
    async def testar(req):
        b = await req.json()
        p = sons.resolve_som(b.get("som", ""))
        if not p:
            return web.json_response({"erro": "som inválido"}, status=400)
        svc.player.enfileirar(p, b.get("volume", 100), "teste")
        return web.json_response({"ok": True})

    @r.post("/api/parar")
    async def parar(req):
        await svc.player.parar()
        return web.json_response({"ok": True})

    @r.get("/api/resgates")
    async def resgates(req):
        return web.json_response(list(svc.resgates))

    app.add_routes(r)
