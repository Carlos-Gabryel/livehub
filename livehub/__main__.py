"""Entrada: python -m livehub"""
import asyncio, logging, logging.handlers, signal, sys

from . import paths


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    raiz = logging.getLogger()
    raiz.setLevel(logging.INFO)
    for h in list(raiz.handlers):
        raiz.removeHandler(h)
    arq = logging.handlers.RotatingFileHandler(paths.log_file(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    err = logging.StreamHandler(sys.stderr)
    for h in (arq, err):
        h.setFormatter(fmt)
        raiz.addHandler(h)


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


def main():
    setup_logging()
    try:
        asyncio.run(amain())
    except OSError as e:
        logging.getLogger("livehub").error("falha ao iniciar: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
