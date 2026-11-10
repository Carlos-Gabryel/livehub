"""Twitch: IRC anonimo (somente leitura) via WebSocket + Helix (app token) para icones de badges."""
import asyncio, logging, random, re, time
import aiohttp

log = logging.getLogger("twitch")
IRC_URL = "wss://irc-ws.chat.twitch.tv:443"
ROLE = {"broadcaster": 4, "moderator": 3, "vip": 2}


class Helix:
    def __init__(self, session, client_id, secret):
        self.s, self.cid, self.secret = session, client_id, secret
        self.token, self.exp = None, 0
        self.badges = {}  # (set_id, version) -> url

    @property
    def enabled(self):
        return bool(self.cid and self.secret)

    async def _token(self):
        if self.token and time.time() < self.exp - 60:
            return self.token
        async with self.s.post("https://id.twitch.tv/oauth2/token", data={
                "client_id": self.cid, "client_secret": self.secret, "grant_type": "client_credentials"}) as r:
            d = await r.json()
        self.token, self.exp = d["access_token"], time.time() + d.get("expires_in", 3600)
        return self.token

    async def get(self, path, **params):
        h = {"Client-Id": self.cid, "Authorization": f"Bearer {await self._token()}"}
        async with self.s.get("https://api.twitch.tv/helix/" + path, params=params, headers=h) as r:
            r.raise_for_status()
            return (await r.json())["data"]

    async def load_badges(self, login):
        try:
            self.badges.clear()
            for sets in [await self.get("chat/badges/global")]:
                self._add(sets)
            users = await self.get("users", login=login)
            if users:
                self._add(await self.get("chat/badges", broadcaster_id=users[0]["id"]))
            log.info("badges carregados: %d", len(self.badges))
        except Exception as e:
            log.warning("falha ao carregar badges (seguindo sem icones): %s", e)

    def _add(self, sets):
        for st in sets:
            for v in st["versions"]:
                self.badges[(st["set_id"], v["id"])] = v.get("image_url_2x") or v["image_url_1x"]


def _unescape(v):
    out, i = [], 0
    while i < len(v):
        c = v[i]
        if c == "\\" and i + 1 < len(v):
            i += 1
            out.append({"s": " ", ":": ";", "\\": "\\", "r": "\r", "n": "\n"}.get(v[i], v[i]))
        else:
            out.append(c)
        i += 1
    return "".join(out)


def parse_irc(line):
    tags = {}
    if line.startswith("@"):
        raw, line = line[1:].split(" ", 1)
        for kv in raw.split(";"):
            k, _, v = kv.partition("=")
            tags[k] = _unescape(v)
    prefix = ""
    if line.startswith(":"):
        prefix, line = line[1:].split(" ", 1)
    trailing = None
    if " :" in line:
        line, trailing = line.split(" :", 1)
    parts = line.split()
    return tags, prefix, (parts[0] if parts else ""), parts[1:], trailing


def split_emotes(text, emotes_tag):
    """-> [(texto, emote_id|None)] respeitando indices em code points."""
    spans = []
    for grp in filter(None, emotes_tag.split("/")):
        eid, _, rngs = grp.partition(":")
        for r in rngs.split(","):
            a, _, b = r.partition("-")
            spans.append((int(a), int(b), eid))
    spans.sort()
    out, cur = [], 0
    for a, b, eid in spans:
        if a < cur or b >= len(text):
            continue
        if a > cur:
            out.append((text[cur:a], None))
        out.append((text[a:b + 1], eid))
        cur = b + 1
    if cur < len(text):
        out.append((text[cur:], None))
    return out


def emote_url(eid):
    return f"https://static-cdn.jtvnw.net/emoticons/v2/{eid}/default/dark/2.0"


def _int(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


def interacao_usernotice(tags, text=""):
    """USERNOTICE -> kwargs de hub.interacao (ou None se nao e interacao / deve ser ignorado)."""
    mid = tags.get("msg-id", "")
    nome = tags.get("display-name") or tags.get("login") or "?"
    base = {"platform": "twitch", "id": tags.get("id") or None}
    if mid in ("sub", "resub"):
        plano = tags.get("msg-param-sub-plan", "")
        meses = _int(tags.get("msg-param-cumulative-months"), 1)
        if plano == "Prime":
            det = "Prime"
        else:
            det = "Tier " + {"1000": "1", "2000": "2", "3000": "3"}.get(plano, plano or "1")
        if mid == "resub" or meses > 1:
            det += f", {meses} meses"
        return dict(base, tipo="sub", user=nome, detalhe=det, valor="")
    if mid == "subgift":
        if tags.get("msg-param-community-gift-id"):
            return None
        rec = tags.get("msg-param-recipient-display-name") or tags.get("msg-param-recipient-user-name") or "?"
        return dict(base, tipo="presente de sub", user=nome, detalhe=f"para {rec}", valor="")
    if mid == "submysterygift":
        return dict(base, tipo="presente de sub", user=nome, detalhe="",
                    valor=f"{_int(tags.get('msg-param-mass-gift-count'), 1)} subs")
    if mid == "raid":
        return dict(base, tipo="raid", user=tags.get("msg-param-displayName") or tags.get("msg-param-login") or nome,
                    detalhe="", valor=f"{_int(tags.get('msg-param-viewerCount'))} viewers")
    return None


def interacao_bits(tags, text):
    bits = _int(tags.get("bits"))
    if bits <= 0:
        return None
    limpo = " ".join(w for w in text.split() if not re.fullmatch(r"[A-Za-z]+\d+", w))
    return dict(platform="twitch", tipo="bits", user=tags.get("display-name") or "?", detalhe=limpo or text,
                valor=f"{bits} bits", id=tags.get("id") or None)


class Twitch:
    def __init__(self, cfg, hub, session):
        self.channel = (cfg["twitch"].get("channel") or "").lower().lstrip("#")
        self.hub, self.s = hub, session
        self.helix = Helix(session, cfg["twitch"].get("client_id"), cfg["twitch"].get("client_secret"))
        self.room_id = None
        self.third = {}  # nome -> url (7TV + BTTV)

    async def _json(self, url):
        async with self.s.get(url, headers={"User-Agent": "LiveHub/1.0"}) as r:
            r.raise_for_status()
            return await r.json(content_type=None)

    async def load_third_party(self):
        """Emotes 7TV e BTTV (globais + do canal). Falhas sao toleradas."""
        third, uid = {}, None
        try:
            if self.helix.enabled:
                u = await self.helix.get("users", login=self.channel)
                uid = u[0]["id"] if u else None
        except Exception as e:
            log.warning("helix users falhou: %s", e)
        jobs = [("7tv", "https://7tv.io/v3/emote-sets/global"), ("bttv", "https://api.betterttv.net/3/cached/emotes/global")]
        if uid:
            jobs += [("7tv", f"https://7tv.io/v3/users/twitch/{uid}"), ("bttv", f"https://api.betterttv.net/3/cached/users/twitch/{uid}")]
        for kind, url in jobs:
            try:
                d = await self._json(url)
                if kind == "7tv":
                    for e in (d.get("emote_set", d) or {}).get("emotes", []) or []:
                        third[e["name"]] = "https:" + e["data"]["host"]["url"] + "/2x.webp"
                else:
                    lst = d if isinstance(d, list) else d.get("channelEmotes", []) + d.get("sharedEmotes", [])
                    for e in lst:
                        third[e["code"]] = f"https://cdn.betterttv.net/emote/{e['id']}/2x.webp"
            except Exception as e:
                log.warning("emotes de terceiros (%s) falharam: %s", url, e)
        self.third = third
        log.info("emotes 7TV/BTTV carregados: %d", len(third))

    async def _refresh_loop(self):
        while True:
            if self.helix.enabled:
                await self.helix.load_badges(self.channel)
            await self.load_third_party()
            await asyncio.sleep(3600)

    async def run(self):
        if not self.channel:
            await self.hub.set_status("twitch", "desativado")
            return
        refresh = asyncio.create_task(self._refresh_loop())
        delay = 2
        try:
            while True:
                try:
                    await self._session()
                    delay = 2
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("conexao caiu: %r", e)
                await self.hub.set_status("twitch", "reconectando")
                await asyncio.sleep(delay + random.random())
                delay = min(delay * 2, 30)
        finally:
            refresh.cancel()

    async def _session(self):
        async with self.s.ws_connect(IRC_URL, heartbeat=None, receive_timeout=420) as ws:
            await ws.send_str("CAP REQ :twitch.tv/tags twitch.tv/commands")
            await ws.send_str("PASS SCHMOOPIIE")
            await ws.send_str(f"NICK justinfan{random.randint(10000, 99999)}")
            await ws.send_str(f"JOIN #{self.channel}")
            async for m in ws:
                if m.type != aiohttp.WSMsgType.TEXT:
                    break
                for line in m.data.split("\r\n"):
                    if line:
                        await self._line(ws, line)

    async def _line(self, ws, line):
        tags, prefix, cmd, params, trailing = parse_irc(line)
        if cmd == "PING":
            await ws.send_str("PONG :" + (trailing or "tmi.twitch.tv"))
        elif cmd == "RECONNECT":
            await ws.close()
        elif cmd == "JOIN":
            await self.hub.set_status("twitch", f"conectado (#{self.channel})")
        elif cmd == "ROOMSTATE":
            self.room_id = tags.get("room-id", self.room_id)
        elif cmd == "PRIVMSG":
            await self._privmsg(tags, prefix, trailing or "")
        elif cmd == "USERNOTICE":
            await self._usernotice(tags, prefix, trailing or "")
        elif cmd == "CLEARMSG":
            await self.hub.delete("twitch", msg_id=tags.get("target-msg-id"))
            await self.hub._sb("Twitch", "ChatMessageDeleted", {"messageId": tags.get("target-msg-id")})
        elif cmd == "CLEARCHAT":
            if trailing:
                uid = tags.get("target-user-id")
                await self.hub.delete("twitch", user=uid)
                typ = "UserTimedOut" if "ban-duration" in tags else "UserBanned"
                await self.hub._sb("Twitch", typ, {"targetUser": {"id": uid, "login": trailing, "name": trailing}})
            else:
                await self.hub.clear("twitch")
                await self.hub._sb("Twitch", "ChatCleared", {})

    # ---------- construcao de mensagens ----------
    def _badges(self, tags):
        out = []
        for b in filter(None, tags.get("badges", "").split(",")):
            sid, _, ver = b.partition("/")
            out.append({"name": sid, "version": ver, "imageUrl": self.helix.badges.get((sid, ver), "")})
        return [b for b in out if b["imageUrl"]], [b.partition("/")[0] for b in filter(None, tags.get("badges", "").split(","))]

    def _user(self, tags, prefix):
        login = prefix.split("!")[0]
        badges, names = self._badges(tags)
        role = max([ROLE.get(n, 1) for n in names] + [1])
        if tags.get("mod") == "1":
            role = max(role, 3)
        return {"id": tags.get("user-id", ""), "login": login, "name": tags.get("display-name") or login,
                "color": tags.get("color") or _default_color(login), "role": role,
                "subscribed": tags.get("subscriber") == "1" or "subscriber" in names or "founder" in names,
                "badges": badges}

    def _parts(self, text, tags):
        parts, dock = [], []
        for chunk, eid in split_emotes(text, tags.get("emotes", "")):
            if eid:
                parts.append({"type": "emote", "text": chunk, "imageUrl": emote_url(eid)})
                dock.append({"t": "emote", "url": emote_url(eid), "name": chunk})
            else:
                for tok in re.split(r"(\s+)", chunk):
                    if not tok:
                        continue
                    url = self.third.get(tok)
                    if url:
                        parts.append({"type": "emote", "text": tok, "imageUrl": url})
                        dock.append({"t": "emote", "url": url, "name": tok})
                    else:
                        parts.append({"type": "text", "text": tok})
                        dock.append({"t": "text", "v": tok})
        return parts, dock

    async def _privmsg(self, tags, prefix, text):
        is_me = text.startswith("\x01ACTION ")
        if is_me:
            text = text[8:].rstrip("\x01")
        src = tags.get("source-room-id")
        shared_guest = bool(src and self.room_id and src != self.room_id)
        user = self._user(tags, prefix)
        parts, dock_parts = self._parts(text, tags)
        if tags.get("bits") and not shared_guest:
            await self._interacao(interacao_bits, tags, text)
        data = {"messageId": tags.get("id", ""), "text": text, "parts": parts, "user": user,
                "meta": {"firstMessage": tags.get("first-msg") == "1", "isMe": is_me},
                "isFromSharedChatGuest": shared_guest, "isReply": "reply-parent-msg-id" in tags,
                "bits": int(tags.get("bits", 0) or 0), "role": user["role"]}
        if shared_guest:
            data["sharedChatSource"] = {"name": src, "login": src}
        if data["isReply"]:
            data["reply"] = {"userName": tags.get("reply-parent-display-name", ""),
                             "msgBody": tags.get("reply-parent-msg-body", "")}
        dock = {"platform": "twitch", "id": data["messageId"], "user_id": user["id"], "user": user["name"],
                "color": user["color"], "parts": dock_parts, "me": is_me,
                "badges": [{"url": b["imageUrl"], "title": b["name"]} for b in user["badges"]],
                "first": data["meta"]["firstMessage"]}
        if shared_guest:
            return  # chat compartilhado de outro canal: nao mostrar no dock
        await self.hub.publish(dock, [("Twitch", "ChatMessage", data)])

    async def _interacao(self, fn, tags, text):
        src = tags.get("source-room-id")
        if src and self.room_id and src != self.room_id:
            return  # chat compartilhado de outro canal
        try:
            kw = fn(tags, text)
            if kw:
                await self.hub.interacao(**kw)
        except Exception as e:
            log.info("evento twitch malformado (%r): %s %s", e, tags.get("msg-id"), str(tags)[:300])

    async def _usernotice(self, tags, prefix, text):
        mid = tags.get("msg-id", "")
        await self._interacao(interacao_usernotice, tags, text)
        login = tags.get("login", "")
        user = self._user(tags, f"{login}!")
        user["name"] = tags.get("display-name") or login
        parts, dock_parts = self._parts(text, tags)
        sysmsg = tags.get("system-msg", "")
        ev = None
        if mid == "announcement":
            ev = ("Announcement", {"messageId": tags.get("id", ""), "text": text, "parts": parts, "user": user,
                                   "announcementColor": (tags.get("msg-param-color") or "PRIMARY").upper()})
        elif mid in ("sub", "resub"):
            ev = ("Sub" if mid == "sub" else "ReSub", {
                "messageId": tags.get("id", ""), "text": text, "parts": parts, "user": user,
                "subTier": {"Prime": "prime"}.get(tags.get("msg-param-sub-plan", ""), tags.get("msg-param-sub-plan", "1000")),
                "isPrime": tags.get("msg-param-sub-plan") == "Prime",
                "cumulativeMonths": int(tags.get("msg-param-cumulative-months", 1) or 1)})
        elif mid == "raid":
            ev = ("Raid", {"messageId": tags.get("id", ""),
                           "from_broadcaster_user_name": tags.get("msg-param-displayName", login),
                           "from_broadcaster_user_login": tags.get("msg-param-login", login),
                           "viewers": int(tags.get("msg-param-viewerCount", 0) or 0)})
        if ev is None:
            return
        dock = {"platform": "twitch", "id": tags.get("id", ""), "user_id": user["id"], "user": user["name"],
                "color": user["color"], "badges": [], "event": True,
                "parts": [{"t": "text", "v": (sysmsg + (" - " if text else "") + text) if mid != "announcement" else text}]}
        await self.hub.publish(dock, [("Twitch", ev[0], ev[1])])


def _default_color(login):
    cols = ["#FF0000", "#0000FF", "#008000", "#B22222", "#FF7F50", "#9ACD32", "#FF4500", "#2E8B57",
            "#DAA520", "#D2691E", "#5F9EA0", "#1E90FF", "#FF69B4", "#8A2BE2", "#00FF7F"]
    return cols[sum(map(ord, login)) % len(cols)]
