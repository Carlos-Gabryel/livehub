"""Hub central: guarda historico, distribui para o dock e para clientes (emulacao Streamer.bot)."""
import collections, itertools, json, logging, time

log = logging.getLogger("hub")


class Hub:
    def __init__(self):
        self.history = collections.deque(maxlen=300)
        self.dock_clients = set()
        self.interacoes = collections.deque(maxlen=300)
        self.feed_clients = set()
        self.ouvintes = []  # fn(ev) sincronas e leves, chamadas a cada interacao nova
        self._ids = collections.deque(maxlen=1000)
        self.sb_clients = {}  # ws -> {source: set(types)}
        self.status = {"twitch": "iniciando", "kick": "iniciando", "youtube": "aguardando", "tiktok": "aguardando",
                       "streamelements": "sem token"}
        self._seq = itertools.count(1)

    # ---- status ----
    async def set_status(self, platform, text):
        if self.status.get(platform) == text:
            return
        self.status[platform] = text
        log.info("%s: %s", platform, text)
        await self._dock({"type": "status", "status": self.status})
        await self._feed({"type": "status", "status": self.status})

    # ---- feed de interacoes ----
    async def interacao(self, platform, tipo, user, detalhe="", valor="", id=None):
        if id:
            key = (platform, str(id))
            if key in self._ids:
                return None
            self._ids.append(key)
        ev = {"platform": platform, "tipo": tipo, "user": user or "?", "detalhe": detalhe or "", "valor": valor or "",
              "id": id, "ts": time.time(), "seq": next(self._seq)}
        self.interacoes.append(ev)
        for fn in list(self.ouvintes):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001
                log.exception("ouvinte de interacao falhou")
        await self._feed({"type": "ev", "ev": ev})
        return ev

    async def _feed(self, payload):
        text = json.dumps(payload, ensure_ascii=False)
        for ws in list(self.feed_clients):
            try:
                await ws.send_str(text)
            except Exception:
                self.feed_clients.discard(ws)

    # ---- publicacao ----
    async def publish(self, dock_msg=None, sb_events=()):
        """dock_msg: dict normalizado; sb_events: [(source, type, data)]."""
        if dock_msg:
            dock_msg.setdefault("ts", time.time())
            dock_msg["seq"] = next(self._seq)
            self.history.append(dock_msg)
            await self._dock({"type": "msg", "msg": dock_msg})
        for source, typ, data in sb_events:
            await self._sb(source, typ, data)

    async def delete(self, platform, msg_id=None, user=None):
        """Remove do dock mensagens apagadas/banidas."""
        keep = [m for m in self.history if not (m["platform"] == platform and (
            (msg_id and m.get("id") == msg_id) or (user and m.get("user_id") == user)))]
        self.history.clear()
        self.history.extend(keep)
        await self._dock({"type": "delete", "platform": platform, "id": msg_id, "user_id": user})

    async def clear(self, platform):
        keep = [m for m in self.history if m["platform"] != platform]
        self.history.clear()
        self.history.extend(keep)
        await self._dock({"type": "clear", "platform": platform})

    async def aparencia(self, alvo, valores):
        """Avisa as paginas do chat (dock/overlay) que a aparencia mudou; elas ignoram tipos desconhecidos."""
        await self._dock({"tipo": "aparencia", "alvo": alvo, "valores": valores})

    async def _dock(self, payload):
        text = json.dumps(payload, ensure_ascii=False)
        for ws in list(self.dock_clients):
            try:
                await ws.send_str(text)
            except Exception:
                self.dock_clients.discard(ws)

    async def _sb(self, source, typ, data):
        text = json.dumps({
            "timeStamp": time.strftime("%Y-%m-%dT%H:%M:%S.000000%z"),
            "event": {"source": source, "type": typ},
            "data": data,
        }, ensure_ascii=False)
        for ws, subs in list(self.sb_clients.items()):
            if typ in subs.get(source, ()):
                try:
                    await ws.send_str(text)
                except Exception:
                    self.sb_clients.pop(ws, None)
