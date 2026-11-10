"""Kick: chat publico via Pusher WebSocket (somente leitura)."""
import asyncio, collections, json, logging, random, re
import aiohttp

log = logging.getLogger("kick")
PUSHER_KEYS = ["34bf7a0ff419a2a775b9", "32cbd69e4b950bf97679"]
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
EMOTE_RE = re.compile(r"\[emote:(\d+):([^\]]+)\]")


def _dados(d):
    v = d.get("data")
    if isinstance(v, str):
        v = json.loads(v)
    return v if isinstance(v, dict) else {}


def _nome(x):
    if isinstance(x, dict):
        return x.get("username") or x.get("name") or x.get("slug") or ""
    return str(x) if isinstance(x, str) else ""


def interacao_kick(ev, p):
    """evento Pusher do Kick -> kwargs de hub.interacao (ou None). Formatos nao verificados ao vivo."""
    base = {"platform": "kick", "id": None}
    if ev.endswith("FollowersUpdated"):
        user = _nome(p.get("username")) or _nome(p.get("user"))
        if p.get("followed") is True and user:
            return dict(base, tipo="follow", user=user, detalhe="", valor="")
        return None
    if ev.endswith("SubscriptionEvent"):
        user = _nome(p.get("username")) or _nome(p.get("user"))
        meses = p.get("months")
        return dict(base, tipo="sub", user=user or "?", detalhe=f"{meses} meses" if meses else "", valor="")
    if ev.endswith("GiftedSubscriptionsEvent"):
        lista = p.get("gifted_usernames") or []
        return dict(base, tipo="presente de sub", user=_nome(p.get("gifter_username")) or _nome(p.get("gifter")) or "?",
                    detalhe="", valor=f"{len(lista)} subs")
    if ev.endswith("StreamHostEvent"):
        return dict(base, tipo="host", user=_nome(p.get("host_username")) or _nome(p.get("hosted")) or _nome(p.get("user")) or "?",
                    detalhe="", valor=f"{p.get('number_viewers')} viewers" if p.get("number_viewers") is not None else "")
    if "Kicks" in ev or "Gift" in ev:
        g = p.get("gift") if isinstance(p.get("gift"), dict) else {}
        user = _nome(p.get("sender")) or _nome(p.get("username")) or _nome(p.get("user")) or "?"
        qtd = p.get("amount") or p.get("kicks") or g.get("amount")
        return dict(base, tipo="kicks", user=user, detalhe=str(p.get("message") or g.get("name") or "")[:200],
                    valor=f"{qtd} kicks" if qtd else "", id=str(p.get("id") or p.get("gift_transaction_id") or "") or None)
    return None


class Kick:
    def __init__(self, cfg, hub, session):
        k = cfg["kick"]
        self.channel = (k.get("channel") or "").lower()
        self.chatroom_id, self.channel_id = k.get("chatroom_id"), k.get("channel_id")
        self.hub, self.s = hub, session
        self.seen = collections.deque(maxlen=500)
        self.key_idx = 0
        self.nomes_vistos = set()
        self.recentes = collections.deque(maxlen=100)

    async def resolve_ids(self):
        if self.chatroom_id:
            return
        for url in (f"https://kick.com/api/v2/channels/{self.channel}", f"https://kick.com/api/v1/channels/{self.channel}"):
            try:
                async with self.s.get(url, headers={"User-Agent": UA, "Accept": "application/json"}) as r:
                    r.raise_for_status()
                    d = await r.json(content_type=None)
                self.chatroom_id, self.channel_id = d["chatroom"]["id"], d["id"]
                log.info("kick ids resolvidos via API: chatroom=%s channel=%s", self.chatroom_id, self.channel_id)
                return
            except Exception as e:
                log.warning("API do Kick falhou (%s): %s", url, e)
        raise RuntimeError("nao foi possivel descobrir o chatroom id do Kick (defina kick.chatroom_id e kick.channel_id nas configuracoes do LiveHub)")

    async def run(self):
        if not self.channel:
            await self.hub.set_status("kick", "desativado")
            return
        delay = 2
        while True:
            try:
                await self.resolve_ids()
                await self._session()
                delay = 2
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("conexao caiu: %r", e)
                self.key_idx = (self.key_idx + 1) % len(PUSHER_KEYS)
            await self.hub.set_status("kick", "reconectando")
            await asyncio.sleep(delay + random.random())
            delay = min(delay * 2, 30)

    async def _session(self):
        url = (f"wss://ws-us2.pusher.com/app/{PUSHER_KEYS[self.key_idx]}"
               "?protocol=7&client=js&version=8.4.0&flash=false")
        async with self.s.ws_connect(url, heartbeat=None, receive_timeout=180, headers={"User-Agent": UA}) as ws:
            async for m in ws:
                if m.type != aiohttp.WSMsgType.TEXT:
                    break
                d = json.loads(m.data)
                ev = d.get("event", "")
                if ev == "pusher:connection_established":
                    chans = [f"chatrooms.{self.chatroom_id}.v2", f"chatroom_{self.chatroom_id}"]
                    if self.channel_id:
                        chans += [f"channel.{self.channel_id}", f"channel_{self.channel_id}"]
                    for ch in chans:
                        await ws.send_json({"event": "pusher:subscribe", "data": {"channel": ch}})
                elif ev == "pusher:ping":
                    await ws.send_json({"event": "pusher:pong", "data": {}})
                elif ev == "pusher_internal:subscription_succeeded":
                    await self.hub.set_status("kick", f"conectado ({self.channel})")
                elif ev == "pusher:error":
                    raise RuntimeError("pusher: " + str(d.get("data")))
                elif ev.endswith("ChatMessageEvent"):
                    await self._chat(json.loads(d["data"]) if isinstance(d["data"], str) else d["data"])
                elif ev.endswith("MessageDeletedEvent"):
                    dd = json.loads(d["data"]) if isinstance(d["data"], str) else d["data"]
                    mid = (dd.get("message") or {}).get("id")
                    await self.hub.delete("kick", msg_id=mid)
                elif ev.startswith("App\\"):
                    await self._evento(ev, d)

    async def _evento(self, ev, d):
        if ev.endswith("ChatMessageEvent") or ev.endswith("MessageDeletedEvent"):
            return
        try:
            chave = (ev, str(d.get("data")))
            if chave in self.recentes:  # mesmo evento replicado em canais diferentes
                return
            self.recentes.append(chave)
            p = _dados(d)
            kw = interacao_kick(ev, p)
            if kw:
                await self.hub.interacao(**kw)
            elif ev not in self.nomes_vistos:
                self.nomes_vistos.add(ev)
                log.info("evento kick nao tratado: %s %s", ev, str(d.get("data"))[:300])
        except Exception as e:
            log.info("evento kick malformado (%r): %s %s", e, ev, str(d.get("data"))[:300])

    async def _chat(self, d):
        mid = d.get("id")
        if mid in self.seen:
            return
        self.seen.append(mid)
        sender = d.get("sender") or {}
        ident = sender.get("identity") or {}
        content = d.get("content") or ""
        parts, dock_parts, cur = [], [], 0
        for m in EMOTE_RE.finditer(content):
            if m.start() > cur:
                t = content[cur:m.start()]
                parts.append({"type": "text", "text": t})
                dock_parts.append({"t": "text", "v": t})
            url = f"https://files.kick.com/emotes/{m.group(1)}/fullsize"
            parts.append({"type": "emote", "text": m.group(2), "imageUrl": url})
            dock_parts.append({"t": "emote", "url": url, "name": m.group(2)})
            cur = m.end()
        if cur < len(content):
            parts.append({"type": "text", "text": content[cur:]})
            dock_parts.append({"t": "text", "v": content[cur:]})
        badges = [{"id": b.get("type"), "count": b.get("count", 0), "type": b.get("type")} for b in ident.get("badges", [])]
        name = sender.get("username", "?")
        color = ident.get("color") or "#53FC18"
        data = {"messageId": mid, "text": re.sub(r"\[emote:\d+:([^\]]+)\]", r"\1", content), "parts": parts,
                "user": {"id": str(sender.get("id", "")), "name": name, "login": sender.get("slug", name),
                         "color": color, "badges": badges}, "isReply": False}
        meta = d.get("metadata") or {}
        if d.get("type") == "reply" and meta.get("original_sender"):
            data["isReply"] = True
            data["reply"] = {"userName": meta["original_sender"].get("username", ""),
                             "msgBody": (meta.get("original_message") or {}).get("content", "")}
        dock = {"platform": "kick", "id": mid, "user_id": str(sender.get("id", "")), "user": name, "color": color,
                "parts": dock_parts, "badges": [{"title": b["type"]} for b in badges if b["type"] in ("broadcaster", "moderator", "vip", "subscriber", "founder", "og")]}
        await self.hub.publish(dock, [("Kick", "ChatMessage", data)])
