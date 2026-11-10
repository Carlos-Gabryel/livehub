"""TikTok LIVE: chat via TikTokLive (somente leitura, anonimo, sem login)."""
import asyncio, collections, logging, random
from TikTokLive import TikTokLiveClient
from TikTokLive.client.errors import UserOfflineError, UserNotFoundError
from TikTokLive.events import CommentEvent, ConnectEvent, FollowEvent, GiftEvent, SubNotifyEvent

log = logging.getLogger("tiktok")
logging.getLogger("httpx").setLevel(logging.WARNING)


def normalize(ev, owner_id=""):
    """CommentEvent -> mensagem do dock (ou None se vazia)."""
    u = ev.user
    text = ev.content or ""
    uid = getattr(u, "unique_id", None) or ""
    name = getattr(u, "nickname", "") or uid or "?"
    emotes = sorted(((e.index, e.emote.image.url_list[0], e.emote.emote_id) for e in (ev.emotes or [])
                     if e.emote and e.emote.image and e.emote.image.url_list
                     and e.emote.image.url_list[0].startswith("https://")), key=lambda x: x[0])
    parts, cur = [], 0
    for idx, url, eid in emotes:
        idx = max(cur, min(idx, len(text)))
        if idx > cur:
            parts.append({"t": "text", "v": text[cur:idx]})
        parts.append({"t": "emote", "url": url, "name": eid})
        cur = idx
    if cur < len(text):
        parts.append({"t": "text", "v": text[cur:]})
    if not parts:
        return None
    mod = bool(getattr(u, "is_moderator", False))
    owner = bool(owner_id) and uid.lower() == owner_id.lower()
    badges = [{"title": "broadcaster"}] if owner else [{"title": "moderator"}] if mod else []
    mid = getattr(getattr(ev, "common", None), "msg_id", None)
    d = {"platform": "tiktok", "id": str(mid) if mid else None, "user_id": uid or str(getattr(u, "id", "")),
         "user": name, "parts": parts, "badges": badges}
    if mod:
        d["mod"] = True
    if owner:
        d["owner"] = True
    return d


def _nome(u):
    return getattr(u, "nickname", "") or getattr(u, "unique_id", "") or "?"


def interacao_follow(ev):
    return dict(platform="tiktok", tipo="follow", user=_nome(ev.user), detalhe="", valor="",
                id=str(getattr(getattr(ev, "common", None), "msg_id", "") or "") or None)


def interacao_sub(ev):
    m = getattr(ev, "sub_month", 0)
    return dict(platform="tiktok", tipo="assinatura", user=_nome(ev.user), detalhe=f"{m} meses" if m else "", valor="",
                id=str(getattr(getattr(ev, "common", None), "msg_id", "") or "") or None)


def interacao_gift(ev):
    """None enquanto a sequencia (streak) de um presente esta em andamento; so o evento final publica."""
    if getattr(ev, "streaking", False):
        return None
    g = ev.gift
    nome = getattr(g, "name", "") or "presente"
    qtd = max(int(getattr(ev, "repeat_count", 1) or 1), 1)
    dia = int(getattr(g, "diamond_count", 0) or 0) * qtd
    valor = f"{qtd}x {nome}" + (f" ({dia} 💎)" if dia else "")
    return dict(platform="tiktok", tipo="presente", user=_nome(ev.user), detalhe="", valor=valor,
                id=str(getattr(getattr(ev, "common", None), "msg_id", "") or "") or None)


class TikTok:
    def __init__(self, cfg, hub, session):
        t = cfg["tiktok"]
        self.user = (t.get("username") or "").lstrip("@")
        self.interval = t.get("check_interval", 60)
        self.hub = hub
        self.seen = collections.deque(maxlen=500)

    async def run(self):
        if not self.user:
            await self.hub.set_status("tiktok", "desativado")
            return
        delay = 2
        while True:
            try:
                client = TikTokLiveClient(unique_id="@" + self.user)
                try:
                    live = await client.is_live()
                    if live:
                        await self._session(client)
                finally:
                    try:
                        await client.web.close()  # cada cliente abre um httpx.AsyncClient; client.close() quebra num loop ativo
                    except Exception:
                        pass
                if not live:
                    await self.hub.set_status("tiktok", "fora do ar, aguardando")
                    await asyncio.sleep(self.interval)
                    continue
                delay = 2
                await self.hub.set_status("tiktok", "live encerrada, aguardando")
                await asyncio.sleep(self.interval)
                continue
            except asyncio.CancelledError:
                raise
            except (UserOfflineError, UserNotFoundError) as e:
                log.info("tiktok: %r", e)
                await self.hub.set_status("tiktok", "fora do ar, aguardando")
                await asyncio.sleep(self.interval)
                continue
            except Exception as e:
                log.warning("conexao caiu: %r", e)
            await self.hub.set_status("tiktok", "reconectando")
            await asyncio.sleep(delay + random.random())
            delay = min(delay * 2, 60)

    async def _session(self, client):
        @client.on(ConnectEvent)
        async def _on_connect(ev):
            await self.hub.set_status("tiktok", f"conectado (@{self.user})")

        @client.on(CommentEvent)
        async def _on_comment(ev):
            d = normalize(ev, self.user)
            if not d:
                return
            if d["id"]:
                if d["id"] in self.seen:
                    return
                self.seen.append(d["id"])
            await self.hub.publish(d, ())

        async def _feed(fn, ev):
            try:
                kw = fn(ev)
                if kw:
                    await self.hub.interacao(**kw)
            except Exception as e:
                log.info("evento tiktok malformado (%r): %s %s", e, type(ev).__name__, str(ev)[:300])

        @client.on(FollowEvent)
        async def _on_follow(ev):
            await _feed(interacao_follow, ev)

        @client.on(SubNotifyEvent)
        async def _on_sub(ev):
            await _feed(interacao_sub, ev)

        @client.on(GiftEvent)
        async def _on_gift(ev):
            await _feed(interacao_gift, ev)

        task = await client.start()
        try:
            await task
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
