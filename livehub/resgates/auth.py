"""Login da Twitch: Device Code (client publico do LiveHub) ou authorization code (app proprio), com refresh automatico."""
import asyncio, logging, secrets, time
from urllib.parse import urlencode
import aiohttp
from aiohttp import web
from .. import settings
from . import const

log = logging.getLogger("auth")


class Auth:
    _VAZIO = {"access_token": "", "refresh_token": "", "expires_at": 0}
    _INTERVALO = 5

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.on_login = asyncio.Event()  # sinalizado quando há token válido
        self._lock = asyncio.Lock()
        self._state = None
        self._cb_runner = None
        self._cb_timeout = None
        self.ultimo_erro = None
        self._esc_cache = (None, None)
        self.on_reload = None  # coroutine chamada apos login por codigo (recarrega EventSub/chat)
        self._sleep = asyncio.sleep
        self._disp_task = None
        self._disp = {"estado": "inativo"}
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
    def modo(self) -> str:
        return self.seg.get("modo") or "dispositivo"

    @property
    def login_expirado(self) -> bool:
        return bool(self.seg.get("login_expirado"))

    @property
    def configurado(self) -> bool:
        s = self.seg
        return self.modo == "dispositivo" or bool(s.get("client_id") and s.get("client_secret"))

    def _cred(self):
        """(client_id, client_secret); no modo dispositivo usa o client publico e secret vazio."""
        s = self.seg
        if s.get("modo", "dispositivo") == "dispositivo":
            return const.LIVEHUB_CLIENT_ID, ""
        if not (s.get("client_id") and s.get("client_secret")):
            raise RuntimeError("client_id/client_secret da Twitch nao configurados")
        return s["client_id"], s["client_secret"]

    async def token_usuario(self) -> str:
        """Token de usuario atual (renovado se preciso), para outros modulos (badges do chat)."""
        return await self.access_token()

    async def definir_modo(self, modo: str) -> bool:
        """Troca o modo de login; ao mudar, apaga tokens/usuario. True se mudou."""
        if modo not in settings.MODOS_TWITCH:
            raise ValueError("modo invalido")
        if modo == "app_proprio":
            s = self.seg
            if not (s.get("client_id") and s.get("client_secret")):
                raise RuntimeError("Salve o Client ID e o Client Secret primeiro.")
        await self.cancelar_dispositivo()
        if modo == self.modo:
            return False
        self._salvar(modo=modo, **self._VAZIO, user_id="", login="", login_expirado=False)
        self._esc_cache = (None, None)
        self.on_login.clear()
        return True

    # ---- device code flow ----
    @property
    def _disp_ativo(self) -> bool:
        return bool(self._disp_task and not self._disp_task.done())

    async def iniciar_dispositivo(self) -> dict:
        await self.definir_modo("dispositivo")  # tambem cancela qualquer fluxo anterior
        async with self.session.post(const.TWITCH_DEVICE, data={
                "client_id": const.LIVEHUB_CLIENT_ID, "scopes": const.SCOPES}) as r:
            j = await r.json(content_type=None)
            if r.status != 200 or not isinstance(j, dict) or not j.get("device_code"):
                msg = j.get("message", "resposta inesperada") if isinstance(j, dict) else "resposta inesperada"
                raise RuntimeError(f"{r.status} {msg}")
        exp = int(j.get("expires_in") or 1800)
        self._disp = {"estado": "aguardando", "device_code": j["device_code"],
                      "intervalo": max(1, int(j.get("interval") or self._INTERVALO)),
                      "expira": time.time() + exp}
        self.ultimo_erro = None
        self._disp_task = asyncio.create_task(self._poll_dispositivo(self._disp))
        return {"user_code": j.get("user_code", ""), "verification_uri": j.get("verification_uri", ""),
                "expires_in": exp}

    async def cancelar_dispositivo(self):
        t, self._disp_task = self._disp_task, None
        if t and not t.done() and t is not asyncio.current_task():
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
        if self._disp.get("estado") == "aguardando":
            self._disp = {"estado": "inativo"}

    def estado_dispositivo(self) -> dict:
        est = self._disp.get("estado", "inativo")
        if est == "inativo" and self.logado:
            est = "conectado"
        out = {"estado": est}
        if est == "conectado":
            out["login"] = self.login
        return out

    async def _poll_dispositivo(self, d: dict):
        falhas = 0
        while True:
            await self._sleep(d["intervalo"])
            if time.time() > d["expira"]:
                d["estado"] = "expirado"
                return
            try:
                async with self.session.post(f"{const.TWITCH_ID}/oauth2/token", data={
                        "client_id": const.LIVEHUB_CLIENT_ID, "scopes": const.SCOPES,
                        "device_code": d["device_code"], "grant_type": const.DEVICE_GRANT}) as r:
                    j = await r.json(content_type=None)
                    status = r.status
                falhas = 0
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                falhas += 1
                log.warning("Falha ao consultar login por codigo (%s)", type(e).__name__)
                if falhas >= 5:
                    d["estado"], self.ultimo_erro = "erro", "Sem conexão com a Twitch."
                    return
                continue
            if status == 200:
                try:
                    self._guardar(j)
                    self._salvar(login_expirado=False)
                    await self.validar(forcar=True)
                except Exception as e:
                    log.error("Falha ao concluir o login: %s", type(e).__name__)
                    d["estado"], self.ultimo_erro = "erro", "Não consegui concluir o login."
                    return
                self.ultimo_erro = None
                d["estado"] = "conectado"
                d.pop("device_code", None)
                if self.on_reload:
                    try:
                        await self.on_reload()
                    except Exception as e:
                        log.warning("Recarga apos login falhou: %r", e)
                return
            msg = str(j.get("message", "")).lower() if isinstance(j, dict) else ""
            if "pending" in msg:
                continue
            if "slow" in msg:
                d["intervalo"] += 5
                continue
            if "denied" in msg:
                d["estado"] = "negado"
                return
            if "expired" in msg or "invalid device" in msg or "invalid_device" in msg:
                d["estado"] = "expirado"
                return
            log.warning("Login por codigo falhou: %s %s", status, msg)
            d["estado"], self.ultimo_erro = "erro", "A Twitch recusou o login."
            return

    # ---- fluxo de login (app proprio) ----
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
            self._salvar(login_expirado=False)
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
        data = {"client_id": cid, **({"client_secret": csec} if csec else {}), **params}
        async with self.session.post(f"{const.TWITCH_ID}/oauth2/token", data=data) as r:
            j = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"{r.status} {j.get('message', j) if isinstance(j, dict) else j}")
        self._guardar(j)

    def _guardar(self, j: dict):
        """Persiste de uma vez os tokens recebidos (o refresh token e de uso unico)."""
        self._salvar(access_token=j["access_token"], refresh_token=j["refresh_token"],
                     expires_at=time.time() + j.get("expires_in", 3600))
        self._esc_cache = (None, None)
        self.on_login.set()
        log.info("Token salvo (expira em %ss)", j.get("expires_in"))

    async def refresh(self):
        """Renova o token. Chamar com self._lock tomado (refresh token e de uso unico)."""
        s = self.seg
        try:
            await self._token({"grant_type": "refresh_token", "refresh_token": s["refresh_token"]})
        except RuntimeError as e:
            if str(e).startswith(("400", "401")):  # refresh inválido/expirado: precisa logar de novo
                log.error("Refresh token inválido, é preciso entrar de novo: %s", e)
                self._salvar(**self._VAZIO, login_expirado=True)
                self._esc_cache = (None, None)
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
                        if self.seg.get("access_token") == tok:  # ninguem renovou ainda
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
        if self._disp_ativo:
            self._disp_task.cancel()
        self._disp = {"estado": "inativo"}
        self._salvar(**self._VAZIO, user_id="", login="", login_expirado=False)
        self._esc_cache = (None, None)
        self.on_login.clear()
