import asyncio, logging, threading, time
import aiohttp
import pytest

from livehub import __main__ as entrada, settings, tray
from livehub import runtime as runtime_mod
from livehub.runtime import Runtime
from conftest import porta_livre


def _cfg_porta():
    cfg = settings.load_config()
    cfg["web_port"] = porta_livre()
    settings.save_config(cfg)
    return cfg["web_port"]


async def test_stop_com_cliente_ws_conectado():
    porta = _cfg_porta()
    rt = Runtime()
    await rt.start()
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(f"http://127.0.0.1:{porta}/dock/ws")
        feed = await s.ws_connect(f"http://127.0.0.1:{porta}/eventos/ws")
        await asyncio.sleep(0.2)
        t0 = time.monotonic()
        await rt.stop()
        assert time.monotonic() - t0 < 5
        msg = await asyncio.wait_for(ws.receive(), 3)
        while msg.type == aiohttp.WSMsgType.TEXT:
            msg = await asyncio.wait_for(ws.receive(), 3)
        assert msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
        assert ws.closed and ws.close_code in (1001, 1006)
        await feed.close()


async def test_stop_com_tarefa_que_ignora_cancelamento(caplog, monkeypatch):
    _cfg_porta()
    rt = Runtime()
    await rt.start()

    async def teimosa():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(30)

    rt.tasks["teimosa"] = asyncio.create_task(teimosa())
    await asyncio.sleep(0.1)
    caplog.set_level(logging.INFO, logger="runtime")
    t0 = time.monotonic()
    await rt.stop()
    assert time.monotonic() - t0 < 12
    assert any(r.levelno == logging.WARNING and "encerrando" in r.getMessage() for r in caplog.records)
    rt.tasks.clear()


def test_runtime_thread_encerra_com_ws_conectado():
    porta = _cfg_porta()
    th = tray.RuntimeThread()
    th.start()
    assert th.ready.wait(20) and th.error is None

    async def cliente():
        async with aiohttp.ClientSession() as s:
            ws = await s.ws_connect(f"http://127.0.0.1:{porta}/dock/ws")
            await asyncio.sleep(0.2)
            t0 = time.monotonic()
            th.request_stop()
            await asyncio.get_running_loop().run_in_executor(None, th.join, 10)
            return time.monotonic() - t0, ws

    dt, _ = asyncio.run(cliente())
    assert not th.is_alive() and dt < 8


class FakeIcon:
    def __init__(self, ordem):
        self.ordem, self._v = ordem, True

    @property
    def visible(self):
        return self._v

    @visible.setter
    def visible(self, v):
        self._v = v
        self.ordem.append(f"visible={v}")

    def stop(self):
        self.ordem.append("icon.stop")


class FakeThread:
    def __init__(self, ordem, vivo=False):
        self.ordem, self.vivo, self.espera = ordem, vivo, None

    def request_stop(self):
        self.ordem.append("request_stop")

    def join(self, t):
        self.espera = t
        self.ordem.append("join")

    def is_alive(self):
        return self.vivo


@pytest.mark.parametrize("vivo", [False, True])
def test_sair_oculta_icone_primeiro_e_espera_limitado(vivo):
    ordem = []
    th = FakeThread(ordem, vivo)
    tray.encerrar(FakeIcon(ordem), th)
    assert ordem == ["visible=False", "request_stop", "join", "icon.stop"]
    assert th.espera is not None and th.espera <= 8


def test_main_tray_termina_com_os_exit(monkeypatch):
    saidas = []
    monkeypatch.setattr(entrada.os, "_exit", lambda c: saidas.append(c))
    monkeypatch.setattr(entrada, "_main_tray", lambda: 0)
    entrada.main_tray()
    assert saidas == [0]

    def falha():
        raise RuntimeError("x")
    monkeypatch.setattr(entrada, "_main_tray", falha)
    entrada.main_tray()
    assert saidas == [0, 1]
