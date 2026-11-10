"""Cliente EventSub via WebSocket."""
import asyncio, json, logging
import aiohttp
from . import const

log = logging.getLogger("eventsub")
GRACE = 5
TIPO = "channel.channel_points_custom_reward_redemption.add"
TIPO_FOLLOW = "channel.follow"


def parse_mensagem(raw: str) -> tuple[str, dict, dict]:
    m = json.loads(raw)
    meta = m.get("metadata", {})
    return meta.get("message_type", ""), meta, m.get("payload", {})


class EventSub:
    def __init__(self, auth, on_resgate, on_follow=None):
        self.auth = auth
        self.on_resgate = on_resgate  # async callable(evento: dict)
        self.on_follow = on_follow  # async callable(evento: dict) | None
        self.conectado = False
        self.inscrito = False
        self.erro = None
        self.vistos: list[str] = []
        self.recarregar_url = None

    def status(self):
        return {"conectado": self.conectado, "inscrito": self.inscrito, "erro": self.erro}

    async def run(self):
        backoff = 1
        while True:
            await self.auth.on_login.wait()
            try:
                await self._sessao()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.erro = f"{type(e).__name__}: {e}"
                log.warning("Conexão EventSub caiu: %s (nova tentativa em %ss)", self.erro, backoff)
            self.conectado = self.inscrito = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _sessao(self):
        url, reconectando = self.recarregar_url or const.EVENTSUB_URL, bool(self.recarregar_url)
        self.recarregar_url = None
        async with self.auth.session.ws_connect(url, heartbeat=None) as ws:
            timeout = 15
            while True:
                try:
                    msg = await ws.receive(timeout=timeout)
                except asyncio.TimeoutError:
                    raise ConnectionError(f"sem keepalive por {timeout}s")
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                    raise ConnectionError(f"websocket fechado ({msg.type.name}, code={ws.close_code})")
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                tipo, meta, payload = parse_mensagem(msg.data)
                if meta.get("message_id") in self.vistos:
                    continue
                self.vistos = (self.vistos + [meta.get("message_id")])[-100:]
                if tipo == "session_welcome":
                    sess = payload["session"]
                    timeout = (sess.get("keepalive_timeout_seconds") or 10) + GRACE
                    self.conectado, self.erro = True, None
                    log.info("EventSub conectado (sessão %s, keepalive %ss)", sess["id"], timeout - GRACE)
                    if reconectando:
                        self.inscrito = True  # assinaturas migram no session_reconnect
                    else:
                        await self._assinar(sess["id"])
                elif tipo == "session_keepalive":
                    pass
                elif tipo == "session_reconnect":
                    self.recarregar_url = payload["session"]["reconnect_url"]
                    log.info("Twitch pediu reconexão")
                    return
                elif tipo == "notification":
                    if meta.get("subscription_type") == TIPO:
                        await self.on_resgate(payload["event"])
                    elif meta.get("subscription_type") == TIPO_FOLLOW and self.on_follow:
                        await self.on_follow(payload["event"])
                elif tipo == "revocation":
                    sub = payload.get("subscription", {})
                    self.inscrito = False
                    self.erro = f"assinatura revogada: {sub.get('status')}"
                    log.error("Assinatura revogada (%s)", sub.get("status"))
                    if sub.get("status") == "authorization_revoked":
                        self.auth.logout()
                    return
                else:
                    log.debug("Mensagem ignorada: %s", tipo)

    async def _assinar(self, session_id: str):
        uid = self.auth.user_id
        if not uid:
            await self.auth.validar(forcar=True)
            uid = self.auth.user_id
        if not uid:
            raise RuntimeError("id do canal desconhecido (validacao do token falhou)")
        body = {"type": TIPO, "version": "1", "condition": {"broadcaster_user_id": uid},
                "transport": {"method": "websocket", "session_id": session_id}}
        status, j = await self.auth.api("POST", "/eventsub/subscriptions", json=body)
        if status in (200, 202):
            self.inscrito = True
            log.info("Assinatura criada: %s", TIPO)
        else:
            raise RuntimeError(f"falha ao assinar ({status}): {j}")
        esc = await self.auth.escopos()
        if esc is not None and const.ESCOPO_SEGUIDORES not in esc:
            log.warning("Token sem o escopo %s: follows não serão assinados (entre de novo)", const.ESCOPO_SEGUIDORES)
            return
        body = {"type": TIPO_FOLLOW, "version": "2", "condition": {"broadcaster_user_id": uid, "moderator_user_id": uid},
                "transport": {"method": "websocket", "session_id": session_id}}
        status, j = await self.auth.api("POST", "/eventsub/subscriptions", json=body)
        if status in (200, 202):
            log.info("Assinatura criada: %s", TIPO_FOLLOW)
        else:
            log.warning("Falha ao assinar %s (%s): %s", TIPO_FOLLOW, status, j)
