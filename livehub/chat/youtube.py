"""YouTube: deteccao de live + chat ao vivo via InnerTube (sem API key do usuario, sem login)."""
import asyncio, collections, json, logging, re, time
import aiohttp

log = logging.getLogger("youtube")
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
COOKIES = {"CONSENT": "YES+1", "SOCS": "CAI"}


KNOWN = {"liveChatTextMessageRenderer", "liveChatPaidMessageRenderer", "liveChatViewerEngagementMessageRenderer",
         "liveChatPlaceholderItemRenderer", "liveChatSponsorshipsGiftRedemptionAnnouncementRenderer",
         "liveChatModeChangeMessageRenderer", "liveChatTickerPaidMessageItemRenderer"}


def _runs(runs):
    """-> (texto_plano, parts_sb, parts_dock)"""
    plain, sb, dock = [], [], []
    for r in runs or []:
        if "text" in r:
            plain.append(r["text"])
            sb.append({"type": "text", "text": r["text"]})
            dock.append({"t": "text", "v": r["text"]})
        elif "emoji" in r:
            e = r["emoji"]
            label = (e.get("shortcuts") or [e.get("emojiId", "")])[0]
            thumbs = (e.get("image") or {}).get("thumbnails") or []
            if e.get("isCustomEmoji") and thumbs:
                url = thumbs[-1]["url"]
                plain.append(label)
                sb.append({"emoji": True, "image": url, "text": label})
                dock.append({"t": "emote", "url": url, "name": label})
            else:
                ch = e.get("emojiId", label)
                plain.append(ch)
                sb.append({"type": "text", "text": ch})
                dock.append({"t": "text", "v": ch})
    return "".join(plain), sb, dock


def _txt(o):
    """simpleText ou runs -> texto plano."""
    if not isinstance(o, dict):
        return ""
    return o.get("simpleText") or "".join(r.get("text", "") for r in o.get("runs") or [] if isinstance(r, dict))


def interacao_youtube(item):
    """item do chat -> kwargs de hub.interacao (ou None). Super chat/sticker/membro/presente de membro."""
    for chave, tipo in (("liveChatPaidMessageRenderer", "super chat"), ("liveChatPaidStickerRenderer", "super sticker"),
                        ("liveChatMembershipItemRenderer", "membro"),
                        ("liveChatSponsorshipsGiftPurchaseAnnouncementRenderer", "presente de membro")):
        r = item.get(chave)
        if not isinstance(r, dict):
            continue
        base = {"platform": "youtube", "id": r.get("id")}
        if tipo == "presente de membro":
            h = ((r.get("header") or {}).get("liveChatSponsorshipsHeaderRenderer")) or {}
            user = _txt(h.get("authorName")) or "?"
            m = re.search(r"(\d+)", _txt(h.get("primaryText")))
            return dict(base, tipo=tipo, user=user, detalhe="", valor=f"{m.group(1)} membros" if m else "")
        user = _txt(r.get("authorName")) or "?"
        if tipo == "membro":
            det = _txt(r.get("headerSubtext")) or _txt(r.get("headerPrimaryText"))
            return dict(base, tipo=tipo, user=user, detalhe=det, valor="", mensagem=_txt(r.get("message")))
        return dict(base, tipo=tipo, user=user, detalhe=_txt(r.get("message")), valor=_txt(r.get("purchaseAmountText")))
    return None


class YouTube:
    def __init__(self, cfg, hub, session):
        y = cfg["youtube"]
        self.handle = (y.get("handle") or "").lstrip("@")
        self.channel_id = y.get("channel_id")
        self.interval = y.get("check_interval", 60)
        self.video_id_override = y.get("video_id")  # util para testes
        self.hub, self.s = hub, session
        self.seen = collections.deque(maxlen=1000)

    async def run(self):
        if not (self.handle or self.channel_id or self.video_id_override):
            await self.hub.set_status("youtube", "desativado")
            return
        while True:
            try:
                vid = self.video_id_override or await self.find_live()
                if vid:
                    await self.hub.set_status("youtube", f"ao vivo, conectando ({vid})")
                    await self.chat(vid)
                    await self.hub.set_status("youtube", "live encerrada, aguardando")
                else:
                    await self.hub.set_status("youtube", "canal fora do ar, aguardando")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("erro: %r", e)
                await self.hub.set_status("youtube", "erro, tentando de novo")
            await asyncio.sleep(self.interval)

    async def _get(self, url):
        async with self.s.get(url, headers=HEADERS, cookies=COOKIES) as r:
            return await r.text()

    async def find_live(self):
        base = f"https://www.youtube.com/channel/{self.channel_id}" if self.channel_id else f"https://www.youtube.com/@{self.handle}"
        html = await self._get(base + "/live")
        m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', html)
        if m and '"isLive":true' in html:
            return m.group(1)
        return None

    async def chat(self, vid):
        html = await self._get(f"https://www.youtube.com/live_chat?is_popout=1&v={vid}")
        key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
        ver = re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', html)
        m = re.search(r'(?:window\["ytInitialData"\]|var ytInitialData)\s*=\s*(\{.*?\});\s*</script>', html, re.S)
        if not (key and m):
            raise RuntimeError("pagina do chat sem ytInitialData/chave (chat desativado?)")
        ver = ver.group(1) if ver else "2.20240101.00.00"
        lcr = json.loads(m.group(1)).get("contents", {}).get("liveChatRenderer")
        if not lcr:
            raise RuntimeError("liveChatRenderer ausente")
        for a in lcr.get("actions", []):  # historico inicial: marca como visto, nao exibe
            it = (a.get("addChatItemAction") or {}).get("item") or {}
            for r in it.values():
                if isinstance(r, dict) and r.get("id"):
                    self.seen.append(r["id"])
        cont = self._next(lcr.get("continuations"))[0]
        start_us = (time.time() - 3) * 1e6
        await self.hub.set_status("youtube", f"conectado (ao vivo {vid})")
        fails = 0
        url = f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={key.group(1)}&prettyPrint=false"
        while cont:
            body = {"context": {"client": {"clientName": "WEB", "clientVersion": ver, "hl": "pt", "gl": "BR"}},
                    "continuation": cont}
            try:
                async with self.s.post(url, json=body, headers=HEADERS, cookies=COOKIES) as r:
                    r.raise_for_status()
                    d = await r.json(content_type=None)
                fails = 0
            except aiohttp.ClientError as e:
                fails += 1
                if fails > 5:
                    raise
                await asyncio.sleep(3)
                continue
            lc = (d.get("continuationContents") or {}).get("liveChatContinuation")
            if not lc:
                return  # chat terminou
            for a in lc.get("actions", []):
                await self._action(a, start_us)
            cont, wait = self._next(lc.get("continuations"))
            await asyncio.sleep(max(0.5, min(wait / 1000, 5)))

    @staticmethod
    def _next(conts):
        for c in conts or []:
            for k in ("invalidationContinuationData", "timedContinuationData", "reloadContinuationData"):
                if k in c:
                    return c[k].get("continuation"), c[k].get("timeoutMs", 2000)
        return None, 2000

    async def _action(self, a, start_us):
        if "markChatItemsByAuthorAsDeletedAction" in a:
            cid = a["markChatItemsByAuthorAsDeletedAction"].get("externalChannelId")
            await self.hub.delete("youtube", user=cid)
            return
        if "removeChatItemAction" in a:
            await self.hub.delete("youtube", msg_id=a["removeChatItemAction"].get("targetItemId"))
            return
        item = (a.get("addChatItemAction") or {}).get("item") or {}
        try:
            kw = interacao_youtube(item)
            ts = int(next((v.get("timestampUsec", 0) for v in item.values() if isinstance(v, dict)), 0) or 0)
            if kw and ts >= start_us:
                await self.hub.interacao(**kw)
            elif not kw and any(k.endswith("Renderer") and k not in KNOWN for k in item):
                log.info("item yt desconhecido: %s", str(item)[:300])
        except Exception as e:
            log.info("item yt malformado (%r): %s", e, str(item)[:300])
        r = item.get("liveChatTextMessageRenderer")
        paid = item.get("liveChatPaidMessageRenderer")
        r = r or paid
        if not r or r.get("id") in self.seen:
            return
        self.seen.append(r["id"])
        if int(r.get("timestampUsec", 0)) < start_us:
            return
        text, sb_parts, dock_parts = _runs((r.get("message") or {}).get("runs"))
        name = (r.get("authorName") or {}).get("simpleText", "?")
        photos = (r.get("authorPhoto") or {}).get("thumbnails") or []
        icons = {(b.get("liveChatAuthorBadgeRenderer") or {}).get("icon", {}).get("iconType") for b in r.get("authorBadges", [])}
        member = any("customThumbnail" in (b.get("liveChatAuthorBadgeRenderer") or {}) for b in r.get("authorBadges", []))
        uid = r.get("authorExternalChannelId", "")
        user = {"id": uid, "name": name, "profileImageUrl": photos[-1]["url"] if photos else "",
                "isOwner": "OWNER" in icons, "isModerator": "MODERATOR" in icons,
                "isSponsor": member, "isVerified": "VERIFIED" in icons}
        dock = {"platform": "youtube", "id": r["id"], "user_id": uid, "user": name, "color": None,
                "parts": dock_parts, "badges": [], "owner": user["isOwner"], "mod": user["isModerator"]}
        if paid:
            amount = (paid.get("purchaseAmountText") or {}).get("simpleText", "")
            dock["event"] = True
            dock["parts"] = [{"t": "text", "v": f"Super Chat {amount}" + (f": {text}" if text else "")}]
            ev = ("SuperChat", {"eventId": r["id"], "user": user, "amount": amount, "message": text})
        else:
            ev = ("Message", {"eventId": r["id"], "message": text, "parts": sb_parts, "user": user})
        await self.hub.publish(dock, [("YouTube", *ev)])
