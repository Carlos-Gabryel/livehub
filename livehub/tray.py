"""Icone na bandeja (pystray no thread principal) + Runtime asyncio em thread de fundo."""
import asyncio, logging, os, sys, threading, webbrowser

from . import paths, startup

log = logging.getLogger("tray")
ROXO = (145, 70, 255, 255)


def make_icon(size=64):
    from PIL import Image, ImageDraw
    S = 512
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((16, 16, S - 16, S - 16), radius=110, fill=ROXO)
    branco = (255, 255, 255, 255)
    d.rounded_rectangle((90, 110, 422, 340), radius=60, fill=branco)           # balao
    d.polygon([(150, 320), (150, 430), (250, 330)], fill=branco)               # cauda
    for cx in (176, 256, 336):                                                 # reticencias
        d.ellipse((cx - 26, 200, cx + 26, 252), fill=ROXO)
    return im.resize((size, size), Image.LANCZOS)


def save_ico(path):
    make_icon(256).save(path, format="ICO", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])


class RuntimeThread(threading.Thread):
    """Roda Runtime num loop proprio. `ready` e setado quando sobe (ok) ou falha (error)."""

    def __init__(self):
        super().__init__(name="runtime", daemon=True)
        self.ready = threading.Event()
        self.error = None
        self.loop = None
        self._stop_ev = None
        self.on_death = None  # chamado se morrer com excecao

    def run(self):
        try:
            asyncio.run(self._main())
        except BaseException as e:  # noqa: BLE001
            self.error = e
            log.exception("runtime caiu")
        finally:
            self.ready.set()
            if self.error and self.on_death:
                self.on_death()

    async def _main(self):
        from .runtime import Runtime
        self.loop = asyncio.get_running_loop()
        self._stop_ev = asyncio.Event()
        rt = Runtime()
        await rt.start()
        self.ready.set()
        try:
            await self._stop_ev.wait()
        finally:
            await rt.stop()

    def request_stop(self):
        if self.loop and self._stop_ev:
            self.loop.call_soon_threadsafe(self._stop_ev.set)


def encerrar(icon, thread, espera=8):
    """Acao do menu Sair: some da bandeja ja, pede o stop, espera limitado e fecha o loop do pystray."""
    try:
        icon.visible = False
    except Exception:  # noqa: BLE001
        log.exception("nao consegui esconder o icone")
    thread.request_stop()
    thread.join(espera)
    if thread.is_alive():
        log.warning("runtime nao encerrou em %ss; saindo mesmo assim", espera)
    icon.stop()


def run_tray(thread: RuntimeThread, base_url: str, abrir_config: bool):
    import pystray

    def abrir(caminho):
        return lambda icon=None, item=None: webbrowser.open(base_url + caminho)

    def pasta_sons(icon=None, item=None):
        os.startfile(paths.sons_dir())

    def ver_log(icon=None, item=None):
        os.startfile(paths.log_file())

    frozen = getattr(sys, "frozen", False)

    def alternar_auto(icon, item):
        try:
            startup.set_autostart(not startup.autostart_enabled())
        except OSError:
            log.exception("nao consegui alterar 'Iniciar com o Windows'")

    def sair(icon, item):
        encerrar(icon, thread)

    menu = pystray.Menu(
        pystray.MenuItem("Abrir configurações", abrir("/config"), default=True),
        pystray.MenuItem("Abrir chat", abrir("/dock")),
        pystray.MenuItem("Abrir feed", abrir("/eventos")),
        pystray.MenuItem("Abrir recompensas", abrir("/recompensas")),
        pystray.MenuItem("Pasta de sons", pasta_sons),
        pystray.MenuItem("Ver log", ver_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Iniciar com o Windows", alternar_auto,
                         checked=lambda item: startup.autostart_enabled() if frozen else False,
                         enabled=frozen),
        pystray.MenuItem("Sair", sair),
    )
    icon = pystray.Icon("LiveHub", make_icon(64), "LiveHub", menu)
    thread.on_death = icon.stop

    def setup(ic):
        ic.visible = True
        if abrir_config:
            webbrowser.open(base_url + "/config")

    icon.run(setup=setup)
