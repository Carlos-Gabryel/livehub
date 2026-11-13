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
        self.rt = None
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
        self.rt = rt
        rt.atualizador.marcar_ok()
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

    def pasta_imagens(icon=None, item=None):
        os.startfile(paths.imagens_dir())

    def ver_log(icon=None, item=None):
        os.startfile(paths.log_file())

    frozen = getattr(sys, "frozen", False)

    def alternar_auto(icon, item):
        try:
            startup.set_autostart(not startup.autostart_enabled())
        except OSError:
            log.exception("nao consegui alterar 'Iniciar com o Windows'")

    def atu():
        return thread.rt.atualizador if thread.rt else None

    def sair(icon, item):
        a = atu()
        if a and a._cfg().get("modo") == "ao_sair" and a.estado()["estado"] == "pronto":
            a.preparar_instalacao()
        encerrar(icon, thread)

    def sair_pedido():
        # chamado da thread asyncio (rota /api/update/instalar): encerrar() faz join do runtime, entao roda em thread propria
        threading.Thread(target=encerrar, args=(icon, thread), name="sair", daemon=True).start()

    def verificar(icon, item):
        def tarefa():
            a = atu()
            if not a or not thread.loop:
                return
            try:
                e = asyncio.run_coroutine_threadsafe(a.verificar(), thread.loop).result(60)
            except Exception:  # noqa: BLE001
                log.exception("verificar atualizacoes")
                return
            if e["estado"] == "erro" and not e["disponivel"]:
                msg = e["erro"] or "Não consegui verificar atualizações."
            elif e["disponivel"]:
                msg = f"Versão {e['disponivel']} disponível — abra as configurações para instalar"
            else:
                msg = f"Você está na versão mais recente ({e['atual']})"
            icon.notify(msg, "LiveHub")
        threading.Thread(target=tarefa, name="verificar-update", daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem("Abrir configurações", abrir("/config"), default=True),
        pystray.MenuItem("Abrir chat", abrir("/dock")),
        pystray.MenuItem("Abrir feed", abrir("/eventos")),
        pystray.MenuItem("Abrir recompensas", abrir("/recompensas")),
        pystray.MenuItem("Abrir alertas (overlay)", abrir("/alertas?preview=1")),
        pystray.MenuItem("Pasta de sons", pasta_sons),
        pystray.MenuItem("Pasta de imagens", pasta_imagens),
        pystray.MenuItem("Ver log", ver_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Verificar atualizações", verificar),
        pystray.MenuItem("Iniciar com o Windows", alternar_auto,
                         checked=lambda item: startup.autostart_enabled() if frozen else False,
                         enabled=frozen),
        pystray.MenuItem("Sair", sair),
    )
    icon = pystray.Icon("LiveHub", make_icon(64), "LiveHub", menu)
    thread.on_death = icon.stop
    if thread.rt:
        thread.rt.pedir_saida = sair_pedido
        thread.rt.atualizador.on_pronto = lambda v: icon.notify(
            f"LiveHub {v} disponível", "Abra as configurações para atualizar." if thread.rt.atualizador._cfg().get("modo") != "ao_sair"
            else "Será instalada quando você sair do LiveHub.")

    def setup(ic):
        ic.visible = True
        if abrir_config:
            webbrowser.open(base_url + "/config")

    icon.run(setup=setup)
