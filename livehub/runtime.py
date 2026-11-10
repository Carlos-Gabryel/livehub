"""Processo unico: um loop asyncio, um app web, tarefas de plataforma supervisionadas e reload()."""
import asyncio, logging, time
import aiohttp
from aiohttp import web

from . import paths, settings
from .chat import dock, sbserver
from .chat.hub import Hub
from .chat.kick import Kick
from .chat.streamelements import StreamElements
from .chat.tiktok import TikTok
from .chat.twitch import Twitch
from .chat.youtube import YouTube
from .resgates import servico
from .web import WEB_PORT, seguranca

log = logging.getLogger("runtime")

# plataforma -> (classe, funcao que diz se ha o que iniciar)
PLATAFORMAS = {
    "twitch": (Twitch, lambda c: (c["twitch"].get("channel") or "").strip()),
    "kick": (Kick, lambda c: (c["kick"].get("channel") or "").strip()),
    "youtube": (YouTube, lambda c: (c["youtube"].get("handle") or c["youtube"].get("channel_id")
                                    or c["youtube"].get("video_id") or "").strip()),
    "tiktok": (TikTok, lambda c: (c["tiktok"].get("username") or "").strip()),
    "streamelements": (StreamElements, lambda c: (c["streamelements"].get("jwt") or "").strip()),
}
STATUS_INATIVO = {"streamelements": "sem token"}


async def supervisionar(nome, fabrica, base=2.0, teto=60.0):
    """Roda fabrica() e reinicia com backoff se cair. Retorno normal = encerrado de proposito."""
    atraso = base
    while True:
        inicio = time.monotonic()
        try:
            await fabrica()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s caiu; reiniciando em %.0fs", nome, atraso)
        if time.monotonic() - inicio > 60:
            atraso = base
        await asyncio.sleep(atraso)
        atraso = min(atraso * 2, teto)


class Runtime:
    def __init__(self):
        self.hub = Hub()
        self.session = None
        self.svc = None
        self.cfg = None
        self.tasks: dict[str, asyncio.Task] = {}
        self.runner = None
        self.sb_runner = None
        self._sb_key = None
        self._lock = asyncio.Lock()
        self.supervisor_base = 2.0

    # ---- web ----
    def make_web_app(self) -> web.Application:
        app = web.Application(middlewares=[seguranca])
        app[WEB_PORT] = self.cfg["web_port"]
        dock.add_routes(app, self.hub, on_token=self.reload)
        servico.add_routes(app, self.svc)
        return app

    async def start(self):
        try:
            await self._start()
        except BaseException:
            await self.stop()
            raise

    async def _start(self):
        paths.sons_dir()  # garante data_dir()/sons
        self.cfg = settings.runtime_config()
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=15, sock_read=60))
        self.svc = servico.Servico(self.session, self.hub)
        self.svc.player.start()
        self.runner = web.AppRunner(self.make_web_app(), access_log=None)
        await self.runner.setup()
        port = self.cfg["web_port"]
        await web.TCPSite(self.runner, "127.0.0.1", port).start()
        log.info("LiveHub em http://127.0.0.1:%s/", port)
        async with self._lock:
            await self._sync_sb()
            self._start_tasks()

    # ---- plataformas ----
    def _start_tasks(self):
        cfg = self.cfg
        for nome, (cls, ativo) in PLATAFORMAS.items():
            if not ativo(cfg):
                log.info("%s: nao configurado, nao sera iniciado", nome)
                self._status_inativo(nome)
                continue
            self._spawn(nome, cls(cfg, self.hub, self.session).run)
        self._spawn("eventsub", self.svc.eventsub_main)

    def _status_inativo(self, nome):
        self.hub.status[nome] = STATUS_INATIVO.get(nome, "desativado")

    def _spawn(self, nome, fabrica):
        self.tasks[nome] = asyncio.create_task(
            supervisionar(nome, fabrica, base=self.supervisor_base), name=nome)

    async def _stop_tasks(self):
        tasks, self.tasks = list(self.tasks.values()), {}
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _sync_sb(self):
        """Emulacao do Streamer.bot: so enquanto streamerbot_compat.enabled."""
        sb = self.cfg["streamerbot_compat"]
        chave = (sb["port"],) if sb["enabled"] else None
        if chave == self._sb_key and (self.sb_runner is not None) == bool(chave):
            return
        if self.sb_runner:
            r, self.sb_runner = self.sb_runner, None
            await r.cleanup()
        self._sb_key = chave
        if chave:
            r = web.AppRunner(sbserver.make_app(self.hub, self.cfg), access_log=None)
            await r.setup()
            try:
                await web.TCPSite(r, "127.0.0.1", sb["port"]).start()
            except OSError as e:
                log.error("Nao consegui abrir a porta %s do Streamer.bot compat: %s", sb["port"], e)
                await r.cleanup()
                self._sb_key = None
                return
            self.sb_runner = r
            log.info("Streamer.bot compat em 127.0.0.1:%s", sb["port"])

    async def reload(self):
        """Releitura de config/secrets: recria as tarefas de plataforma e o EventSub."""
        async with self._lock:
            await self._stop_tasks()
            novo = settings.runtime_config()
            if novo["web_port"] != self.cfg["web_port"]:
                log.warning("web_port mudou; so vale depois de reiniciar o LiveHub")
                novo["web_port"] = self.cfg["web_port"]
            self.cfg = novo
            await self._sync_sb()
            self._start_tasks()
            log.info("configuracoes recarregadas")

    async def stop(self):
        async with self._lock:
            await self._stop_tasks()
        if self.svc:
            await self.svc.player.stop()
        for r in (self.sb_runner, self.runner):
            if r:
                await r.cleanup()
        self.sb_runner = self.runner = None
        if self.session:
            await self.session.close()
