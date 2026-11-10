"""OAuth (authorization code) da Twitch com refresh automático."""
import asyncio, logging, secrets, time
from urllib.parse import urlencode
import aiohttp
from aiohttp import web
from .. import settings
from . import const

log = logging.getLogger("auth")


class Auth:
    _VAZIO = {"access_token": "", "refresh_token": "", "expires_at": 0}

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.on_login = asyncio.Event()  # sinalizado quando há token válido
        self._lock = asyncio.Lock()
        self._state = None
        self._cb_runner = None
        self._cb_timeout = None
        self.ultimo_erro = None
        self._esc_cache = (None, None)
        if self.logado:
            self.on_login.set()

    @property
    def seg(self) -> dict:
        """Segredos da Twitch (secrets.json -> twitch), lidos do disco a cada acesso."""
        return settings.load_secrets()["twitch"]

    def _salvar(self, **campos):
        sec = settings.load_secrets()
        sec["twitch"].update(campos)
        settings.save_secrets(sec)

    @property
    def logado(self) -> bool:
        return bool(self.seg.get("refresh_token"))

    @property
    def user_id(self) -> str:
        return str(self.seg.get("user_id") or "")

    @property
    def login(self) -> str:
        return self.seg.get("login") or ""

    @property
    def configurado(self) -> bool:
        s = self.seg
        return bool(s.get("client_id") and s.get("client_secret"))

    def _cred(self):
        s = self.seg
        if not (s.get("client_id") and s.get("client_secret")):
            raise RuntimeError("client_id/client_secret da Twitch nao configurados")
        return s["client_id"], s["client_secret"]

    # ---- fluxo de login ----
    async def iniciar_login(self) -> str:
        await self._parar_callback()
        self._cred()  # falha cedo se nao houver credenciais
        self._state = secrets.token_urlsafe(16)
        app = web.Application()
        app.router.add_get("/auth", self._callback)
        self._cb_runner = web.AppRunner(app)
        await self._cb_runner.setup()
        await web.TCPSite(self._cb_runner, "localhost", const.AUTH_PORT).start()
        self._cb_timeout = asyncio.create_task(self._expirar())
        cid, _ = self._cred()
        q = urlencode({"client_id": cid, "redirect_uri": const.REDIRECT_URI, "response_type": "code",
                       "scope": const.SCOPES, "state": self._state, "force_verify": "true"})
        return f"{const.TWITCH_ID}/oauth2/authorize?{q}"

    async def _expirar(self):
        await asyncio.sleep(300)
        log.info("Login expirou; fechando servidor de callback")
        await self._parar_callback()

    async def _parar_callback(self):
        if self._cb_timeout and self._cb_timeout is not asyncio.current_task():
            self._cb_timeout.cancel()
        self._cb_timeout = None
        if self._cb_runner:
            r, self._cb_runner = self._cb_runner, None
            await r.cleanup()

    async def _callback(self, req: web.Request):
        if not self._state or req.query.get("state") != self._state:
            return web.Response(text="Requisição inválida.", status=400)
        self._state = None  # uso único
        if req.query.get("error"):
            self.ultimo_erro = req.query.get("error_description", req.query["error"])
            asyncio.create_task(self._parar_callback())
            return web.Response(text=f"Login recusado: {self.ultimo_erro}", status=400)
        if not req.query.get("code"):
            asyncio.create_task(self._parar_callback())
            return web.Response(text="Requisição inválida.", status=400)
        try:
            await self._token({"grant_type": "authorization_code", "code": req.query["code"],
                               "redirect_uri": const.REDIRECT_URI})
            await self.validar(forcar=True)
            self.ultimo_erro = None
        except Exception as e:
            log.error("Falha na troca do código: %s", e)
            self.ultimo_erro = str(e)
            asyncio.create_task(self._parar_callback())
            return web.Response(text=f"Falha no login: {e}", status=500)
        asyncio.create_task(self._parar_callback())
        return web.Response(content_type="text/html", charset="utf-8",
                            text='<body style="background:#111;color:#eee;font-family:sans-serif">'
                                 '<h2>Login feito!</h2><p>Pode fechar esta aba e voltar a '
                                 f'<a style="color:#9b7bff" href="http://127.0.0.1:{settings.load_config()["web_port"]}/recompensas">página de resgates</a>.</p>')

    # ---- tokens ----
    async def _token(self, params: dict):
        cid, csec = self._cred()
        data = {"client_id": cid, "client_secret": csec, **params}
        async with self.session.post(f"{const.TWITCH_ID}/oauth2/token", data=data) as r:
            j = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"{r.status} {j.get('message', j)}")
        self._salvar(access_token=j["access_token"], refresh_token=j["refresh_token"],
                     expires_at=time.time() + j.get("expires_in", 3600))
        self._esc_cache = (None, None)
        self.on_login.set()
        log.info("Token salvo (expira em %ss)", j.get("expires_in"))

    async def refresh(self):
        s = self.seg
        try:
            await self._token({"grant_type": "refresh_token", "refresh_token": s["refresh_token"]})
        except RuntimeError as e:
            if str(e).startswith(("400", "401")):  # refresh inválido: precisa logar de novo
                log.error("Refresh token inválido, é preciso entrar de novo: %s", e)
                self._salvar(**self._VAZIO)
                self.on_login.clear()
            raise

    async def access_token(self) -> str:
        async with self._lock:
            s = self.seg
            if not s.get("refresh_token"):
                raise RuntimeError("não logado")
            if not s.get("access_token") or s.get("expires_at", 0) - time.time() < 120:
                await self.refresh()
            return self.seg["access_token"]

    async def api(self, method: str, path: str, **kw):
        """Chamada Helix autenticada; renova o token e tenta de novo em 401."""
        for tentativa in (1, 2):
            tok = await self.access_token()
            h = {"Authorization": f"Bearer {tok}", "Client-Id": self._cred()[0]}
            async with self.session.request(method, f"{const.TWITCH_API}{path}", headers=h, **kw) as r:
                body = await r.json(content_type=None)
                if r.status == 401 and tentativa == 1:
                    async with self._lock:
                        await self.refresh()
                    continue
                return r.status, body

    async def validar(self, forcar=False):
        """GET /oauth2/validate: guarda user_id/login em secrets.twitch e devolve os escopos (None se falhar)."""
        if not self.logado:
            return None
        try:
            tok = await self.access_token()
            if not forcar and self._esc_cache[0] == tok:
                return self._esc_cache[1]
            async with self.session.get(f"{const.TWITCH_ID}/oauth2/validate",
                                        headers={"Authorization": f"OAuth {tok}"}) as r:
                if r.status != 200:
                    return None
                j = await r.json(content_type=None)
            esc = j.get("scopes", [])
            self._esc_cache = (tok, esc)
            uid, login = str(j.get("user_id") or ""), j.get("login") or ""
            if uid and (self.user_id != uid or self.login != login):
                self._salvar(user_id=uid, login=login)
            return esc
        except Exception as e:
            log.warning("Não consegui validar o token: %s", e)
            return None

    escopos = validar

    def logout(self):
        self._salvar(**self._VAZIO, user_id="", login="")
        self._esc_cache = (None, None)
        self.on_login.clear()
