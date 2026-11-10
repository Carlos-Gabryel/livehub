"""Entrada: python -m livehub [--console | --tray]; no exe (congelado) o padrao e a bandeja."""
import asyncio, logging, os, signal, sys, webbrowser

from . import paths, settings, startup


async def amain():
    from .runtime import Runtime
    rt = Runtime()
    await rt.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows nao tem add_signal_handler
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    try:
        await stop.wait()
    finally:
        await rt.stop()


def _abrir(url):
    if not os.environ.get("LIVEHUB_NO_BROWSER"):  # usado nos testes de fumaca
        webbrowser.open(url)


def main_tray():
    log = logging.getLogger("livehub")
    mutex = startup.acquire_singleton()
    if mutex is None:
        _abrir(f"http://127.0.0.1:{startup.porta_configurada()}/config")
        return 0
    primeira_vez = not settings.config_path().exists()
    cfg = settings.load_config()  # cria config.json com padroes se faltar
    porta = cfg["web_port"]
    escolhida = startup.escolher_porta(porta, primeira_vez=primeira_vez)
    if escolhida is None:
        startup.msgbox(
            f"Outro programa já está usando a porta {porta}.\n\n"
            "Feche esse programa e abra o LiveHub de novo, ou troque o valor de "
            f'"web_port" no arquivo:\n{settings.config_path()}')
        return 1
    if escolhida != porta:
        cfg["web_port"] = escolhida
        settings.save_config(cfg)
        log.info("porta %s ocupada; usando %s", porta, escolhida)
    from . import tray
    th = tray.RuntimeThread()
    th.start()
    th.ready.wait(60)
    if th.error is not None:
        startup.msgbox(f"O LiveHub não conseguiu iniciar:\n\n{th.error}\n\nVeja o log em:\n{paths.log_file()}")
        return 1
    url = f"http://127.0.0.1:{escolhida}"
    abrir_config = primeira_vez and not os.environ.get("LIVEHUB_NO_BROWSER")
    tray.run_tray(th, url, abrir_config)
    if th.error is not None:
        startup.msgbox(f"O LiveHub parou por um erro:\n\n{th.error}\n\nVeja o log em:\n{paths.log_file()}")
        return 1
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    startup.force_utf8()
    startup.setup_logging()
    frozen = getattr(sys, "frozen", False)
    tray = "--tray" in argv or (frozen and "--console" not in argv)
    try:
        if tray:
            return main_tray()
        asyncio.run(amain())
    except OSError as e:
        logging.getLogger("livehub").error("falha ao iniciar: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
