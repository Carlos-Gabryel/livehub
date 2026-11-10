"""StreamElements: doacoes (tips) via realtime socket.io (somente leitura, JWT do canal)."""
import asyncio, logging, random
import socketio

log = logging.getLogger("streamelements")
URL = "https://realtime.streamelements.com"


def normalize(msg, teste=False):
    """evento do SE -> kwargs de hub.interacao (ou None se nao for tip valida)."""
    if not isinstance(msg, dict) or msg.get("type") != "tip":
        return None
    d = msg.get("data")
    if not isinstance(d, dict):
        return None
    user = str(d.get("username") or d.get("name") or "?")
    try:
        amt = float(d.get("amount"))
        amt = f"{amt:.0f}" if amt == int(amt) else f"{amt:.2f}"
    except (TypeError, ValueError):
        amt = str(d.get("amount") or "")
    valor = f"{amt} {d.get('currency') or ''}".strip()
    det = str(d.get("message") or "")
    if teste:
        det = "(teste) " + det
    return dict(platform="streamelements", tipo="doação", user=user, detalhe=det, valor=valor,
                id=None if teste else (msg.get("_id") or msg.get("id")))


class StreamElements:
    def __init__(self, cfg, hub, session):
        self.cfg, self.hub = cfg, hub
        self.wake = asyncio.Event()

    def set_token(self, jwt):
        self.cfg.setdefault("streamelements", {})["jwt"] = jwt
        self.wake.set()

    def _jwt(self):
        return (self.cfg.get("streamelements", {}).get("jwt") or "").strip()

    async def _wait(self, secs):
        try:
            await asyncio.wait_for(self.wake.wait(), secs)
        except asyncio.TimeoutError:
            pass
        self.wake.clear()

    async def run(self):
        delay = 2
        while True:
            jwt = self._jwt()
            if not jwt:
                await self.hub.set_status("streamelements", "sem token")
                await self._wait(30)
                continue
            try:
                invalido = await self._session(jwt)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("conexao caiu: %r", e)
                invalido = False
            if invalido:
                await self.hub.set_status("streamelements", "token inválido")
                await self._wait(300)
                continue
            await self.hub.set_status("streamelements", "reconectando")
            await self._wait(delay + random.random())
            delay = min(delay * 2, 60)

    async def _session(self, jwt):
        sio = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
        fim, invalido = asyncio.Event(), []

        @sio.event
        async def connect():
            await sio.emit("authenticate", {"method": "jwt", "token": jwt})

        @sio.event
        async def authenticated(*a):
            await self.hub.set_status("streamelements", "conectado")

        @sio.event
        async def unauthorized(*a):
            invalido.append(1)
            fim.set()

        @sio.event
        async def disconnect(*a):
            fim.set()

        @sio.on("connect_error")
        async def _err(*a):
            fim.set()

        async def evento(msg, teste):
            try:
                kw = normalize(msg, teste)
                if kw:
                    await self.hub.interacao(**kw)
                else:
                    log.info("evento SE ignorado: %s", str(msg)[:300])
            except Exception as e:
                log.info("evento SE malformado (%r): %s", e, str(msg)[:300])

        @sio.on("event")
        async def _ev(msg=None, *a):
            await evento(msg, False)

        @sio.on("event:test")
        async def _evt(msg=None, *a):
            await evento(msg, True)

        try:
            await sio.connect(URL, transports=["websocket"], wait_timeout=15)
            await fim.wait()
        finally:
            try:
                await sio.disconnect()
            except Exception:
                pass
        return bool(invalido)
