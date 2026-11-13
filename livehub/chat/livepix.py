"""LivePix: doacoes via polling da API v2 (OAuth2 client credentials, somente leitura, escopo messages:read)."""
import asyncio, collections, logging, random, time
from datetime import datetime, timezone
import aiohttp

log = logging.getLogger("livepix")
TOKEN_URL = "https://oauth.livepix.gg/oauth2/token"
API_URL = "https://api.livepix.gg/v2/messages"
LIMITE, PAGINAS, MAX_SEMEAR, TIMEOUT = 100, 5, 50, aiohttp.ClientTimeout(total=15)
MIN_LIMITE = 30  # espera minima (s) ao tomar 429
_TOKENS = {}  # (client_id, client_secret) -> (token, expira em time.monotonic())


class AuthInvalida(Exception):
    pass


class Limite(Exception):
    def __init__(self, espera):
        self.espera = espera


def _espera(r):
    try:
        s = float(r.headers.get("X-RateLimit-Reset")) - time.time()
    except (TypeError, ValueError):
        s = 0
    return min(max(s, MIN_LIMITE), 3600)


async def obter_token(session, cid, sec, forcar=False):
    """Token em cache ate expires_in-60s; so pede outro ao expirar ou com forcar=True (apos 401)."""
    chave = (cid, sec)
    t = _TOKENS.get(chave)
    if t and not forcar and time.monotonic() < t[1]:
        return t[0]
    _TOKENS.pop(chave, None)
    data = {"grant_type": "client_credentials", "client_id": cid, "client_secret": sec, "scope": "messages:read"}
    async with session.post(TOKEN_URL, data=data, timeout=TIMEOUT) as r:
        if r.status in (400, 401):
            raise AuthInvalida()
        if r.status == 429:
            raise Limite(_espera(r))
        if r.status != 200:
            raise RuntimeError(f"token http {r.status}")
        j = await r.json(content_type=None)
    tok = j["access_token"]
    _TOKENS[chave] = (tok, time.monotonic() + max(60, int(j.get("expires_in") or 3600)) - 60)
    return tok


async def buscar(session, cid, sec, pagina=1, limite=LIMITE):
    """Lista de mensagens de uma pagina. Um 401 renova o token uma unica vez."""
    for tentativa in (0, 1):
        tok = await obter_token(session, cid, sec, forcar=tentativa == 1)
        async with session.get(API_URL, params={"page": pagina, "limit": limite}, timeout=TIMEOUT,
                               headers={"Authorization": f"Bearer {tok}"}) as r:
            if r.status == 401:
                if tentativa:
                    raise AuthInvalida()
                continue
            if r.status == 429:
                raise Limite(_espera(r))
            if r.status != 200:
                raise RuntimeError(f"messages http {r.status}")
            j = await r.json(content_type=None)
        d = j.get("data") if isinstance(j, dict) else None
        return [m for m in d if isinstance(m, dict)] if isinstance(d, list) else []


async def testar(session, cid, sec):
    """Uma consulta so para validar as credenciais; devolve quantas mensagens vieram."""
    return len(await buscar(session, cid, sec, 1, LIMITE))


def valor(m):
    cur = str(m.get("currency") or "BRL").upper()
    try:
        c = int(m.get("amount"))
    except (TypeError, ValueError):
        return str(m.get("amount") or "")
    r, ct = divmod(abs(c), 100)
    sinal = "-" if c < 0 else ""
    if cur == "BRL":
        return f"R$ {sinal}{r:,}".replace(",", ".") + f",{ct:02d}"
    return f"{sinal}{r}.{ct:02d} {cur}"


def _quando(m):
    try:
        d = datetime.fromisoformat(str(m.get("createdAt")).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return 0.0


def normalize(m):
    """mensagem do LivePix -> kwargs de hub.interacao (ou None sem id)."""
    if not isinstance(m, dict) or not m.get("id"):
        return None
    det = "" if m.get("flagged") else str(m.get("message") or "")
    return dict(platform="livepix", tipo="doação", user=str(m.get("username") or "").strip() or "Anônimo",
                detalhe=det, valor=valor(m), id=str(m["id"]))


class LivePix:
    def __init__(self, cfg, sec, hub, session):
        self.cfg, self.sec, self.hub, self.session = cfg, sec, hub, session
        self.wake = asyncio.Event()
        self.vistos, self._ord = set(), collections.deque()
        self.ordem, self._pagina = None, None  # ordem da API: None (ainda desconhecida, assume desc) | desc | asc

    def set_credentials(self, cid, secret):
        self.sec["client_id"], self.sec["client_secret"] = cid, secret
        self.wake.set()

    def _cred(self):
        return (self.sec.get("client_id") or "").strip(), (self.sec.get("client_secret") or "").strip()

    def _intervalo(self):
        try:
            return min(60, max(5, int(self.cfg.get("intervalo") or 8)))
        except (TypeError, ValueError):
            return 8

    async def _wait(self, secs):
        try:
            await asyncio.wait_for(self.wake.wait(), secs)
        except asyncio.TimeoutError:
            pass
        self.wake.clear()

    def _marcar(self, i):
        if i in self.vistos:
            return
        self.vistos.add(i)
        self._ord.append(i)
        if len(self._ord) > 5000:
            self.vistos.discard(self._ord.popleft())

    def _detectar(self, lote):
        """Descobre a ordem da API pelos createdAt (uma vez so)."""
        if self.ordem:
            return
        ts = [_quando(m) for m in lote]
        if len(ts) >= 2 and ts[0] != ts[-1]:
            self.ordem = "asc" if ts[0] < ts[-1] else "desc"
            log.info("ordem das mensagens do LivePix: %s", self.ordem)

    async def _pag(self, cid, sec, p):
        return await buscar(self.session, cid, sec, p, LIMITE)

    async def _semear(self, cid, sec, lote1):
        """asc: percorre as paginas ate a ultima (parcial); _pagina = onde estao as mais novas."""
        out, lote, p = list(lote1), lote1, 1
        while len(lote) >= LIMITE and p < MAX_SEMEAR:
            p += 1
            lote = await self._pag(cid, sec, p)
            out += lote
        if len(lote) >= LIMITE:
            log.warning("LivePix: mais de %d paginas de doacoes; usando a pagina %d", MAX_SEMEAR, MAX_SEMEAR)
        elif not lote and p > 1:
            p -= 1  # pagina final vazia (total multiplo do limite)
        self._pagina = p
        return out

    async def _coletar(self, cid, sec, primeiro):
        if self.ordem == "asc" and self._pagina:
            p, out, ultima = self._pagina, [], None
            for n in range(PAGINAS):
                lote = await self._pag(cid, sec, p + n)
                if lote:
                    out += lote
                    ultima = p + n
                if len(lote) < LIMITE:
                    break
            if ultima:
                self._pagina = ultima
            elif p > 1:
                self._pagina = p - 1  # pagina vazia: volta uma
            return out
        out = lote = await self._pag(cid, sec, 1)
        self._detectar(lote)
        if self.ordem == "asc":
            return await self._semear(cid, sec, lote)
        for p in range(2, PAGINAS + 1):
            # so pagina adiante se a pagina inteira for nova (rajada)
            if primeiro or len(lote) < LIMITE or any(str(m.get("id")) in self.vistos for m in lote):
                break
            lote = await self._pag(cid, sec, p)
            out = out + lote
        return out

    async def run(self):
        delay, primeiro = 2, True
        while True:
            cid, sec = self._cred()
            if not cid or not sec:
                await self.hub.set_status("livepix", "sem credenciais")
                primeiro = True
                await self._wait(30)
                continue
            try:
                msgs = await self._coletar(cid, sec, primeiro)
            except asyncio.CancelledError:
                raise
            except AuthInvalida:
                _TOKENS.pop((cid, sec), None)
                await self.hub.set_status("livepix", "credenciais inválidas")
                await self._wait(300)
                continue
            except Limite as e:
                await self.hub.set_status("livepix", "limite de requisições")
                await self._wait(e.espera)
                continue
            except Exception as e:
                log.warning("consulta falhou: %r", e)
                await self.hub.set_status("livepix", "reconectando")
                await self._wait(delay + random.random())
                delay = min(delay * 2, 60)
                continue
            delay = 2
            await self.hub.set_status("livepix", "conectado")
            if primeiro:
                for m in msgs:
                    self._marcar(str(m.get("id")))
                primeiro = False
            else:
                novos = {str(m["id"]): m for m in msgs if m.get("id") and str(m["id"]) not in self.vistos}
                for m in sorted(novos.values(), key=_quando):
                    self._marcar(str(m["id"]))
                    try:
                        kw = normalize(m)
                        if kw:
                            await self.hub.interacao(**kw)
                    except Exception as e:
                        log.info("doacao LivePix malformada (%r): %s", e, str(m)[:300])
            await self._wait(self._intervalo())
