"""Fila de reproducao: um som por vez, em ordem (FIFO)."""
import asyncio, logging, time

from . import audio

log = logging.getLogger("player")


class Player:
    def __init__(self, device_fn=None):
        self.fila: asyncio.Queue = asyncio.Queue()
        self.atual = None  # dict do item tocando
        self.tocados = []  # historico curto (testes/diagnostico)
        self._task = None
        self._device_fn = device_fn or (lambda: "")

    def start(self):
        self._task = asyncio.create_task(self._worker())

    async def stop(self):
        if self._task:
            self._task.cancel()
            audio.parar()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def enfileirar(self, caminho, volume: int, rotulo: str = ""):
        self.fila.put_nowait({"caminho": str(caminho), "volume": max(0, min(100, int(volume))), "rotulo": rotulo})
        log.info("Enfileirado: %s (vol %s) [fila=%d]", rotulo or caminho, volume, self.fila.qsize())

    def estado(self):
        return {"tocando": self.atual["rotulo"] if self.atual else None, "fila": self.fila.qsize()}

    async def parar(self):
        n = 0
        while not self.fila.empty():
            self.fila.get_nowait()
            n += 1
        audio.parar()
        log.info("Parado; %d itens removidos da fila", n)

    async def _worker(self):
        loop = asyncio.get_running_loop()
        while True:
            item = await self.fila.get()
            self.atual = item
            rc = 0
            try:
                log.info("Tocando: %s", item["rotulo"] or item["caminho"])
                await loop.run_in_executor(None, audio.tocar, item["caminho"], item["volume"] / 100,
                                           self._device_fn())
            except asyncio.CancelledError:
                raise
            except Exception:
                rc = 1
                log.exception("Falha ao tocar %s", item["caminho"])
            finally:
                self.tocados.append({**item, "rc": rc, "t": time.time()})
                self.tocados = self.tocados[-50:]
                self.atual = None
