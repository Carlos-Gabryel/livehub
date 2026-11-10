"""Feed de interacoes e dock via rotas do chat + guardas unificadas (ex-feed_test e dock_listen_test)."""
import asyncio
import aiohttp
from aiohttp import web

from livehub.chat import dock
from livehub.chat.hub import Hub
from livehub.web import WEB_PORT, seguranca
from conftest import porta_livre


async def subir(hub):
    porta = porta_livre()
    app = web.Application(middlewares=[seguranca])
    app[WEB_PORT] = porta
    dock.add_routes(app, hub)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", porta).start()
    return runner, f"http://127.0.0.1:{porta}"


async def test_eventos_novo_e_ws():
    hub = Hub()
    runner, base = await subir(hub)
    ev = {"platform": "twitch", "tipo": "resgate", "user": "ana", "detalhe": "oi", "valor": "100", "id": "x1"}
    try:
        async with aiohttp.ClientSession() as s:
            ws = await s.ws_connect(base + "/eventos/ws")
            h = await ws.receive_json()
            assert h["type"] == "history" and h["eventos"] == [] and "streamelements" in h["status"], h
            async with s.post(base + "/eventos/novo", json=ev) as r:
                assert r.status == 204
            m = await asyncio.wait_for(ws.receive_json(), 3)
            assert m["type"] == "ev" and m["ev"]["user"] == "ana" and m["ev"]["tipo"] == "resgate", m
            async with s.post(base + "/eventos/novo", json=ev) as r:  # id repetido
                assert r.status == 204
            assert len(hub.interacoes) == 1
            for hdr, kw in (({"Host": "evil.com"}, {"json": ev}), ({"Origin": "http://evil.com"}, {"json": ev}),
                            ({}, {"data": "x=1"}), ({"Content-Type": "text/plain"}, {"data": "{}"})):
                async with s.post(base + "/eventos/novo", headers=hdr, **kw) as r:
                    assert r.status == 403, (hdr, r.status)
            for bad in ({**ev, "platform": "x"}, {**ev, "user": "a" * 201}, {**ev, "tipo": 5}):
                async with s.post(base + "/eventos/novo", json=bad) as r:
                    assert r.status == 400, (bad, r.status)
            assert len(hub.interacoes) == 1
            async with s.get(base + "/eventos") as r:
                assert r.status == 200
            await hub.set_status("streamelements", "conectado")
            st = await asyncio.wait_for(ws.receive_json(), 3)
            assert st["type"] == "status" and st["status"]["streamelements"] == "conectado"
    finally:
        await runner.cleanup()


async def test_dock_ws_historico_e_mensagem():
    hub = Hub()
    await hub.publish({"platform": "twitch", "id": "1", "user": "ana", "parts": [{"t": "text", "v": "oi"}], "badges": []})
    runner, base = await subir(hub)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(base + "/dock/ws") as ws:
                h = await ws.receive_json()
                assert h["type"] == "history" and len(h["messages"]) == 1 and "twitch" in h["status"]
                await hub.publish({"platform": "kick", "id": "2", "user": "bob",
                                   "parts": [{"t": "emote", "url": "u", "name": "e"}], "badges": []})
                m = await asyncio.wait_for(ws.receive_json(), 3)
                assert m["type"] == "msg" and m["msg"]["user"] == "bob" and m["msg"]["platform"] == "kick"
            try:
                async with s.ws_connect(base + "/dock/ws", headers={"Origin": "http://evil.com"}):
                    raise AssertionError("deveria recusar")
            except aiohttp.WSServerHandshakeError as e:
                assert e.status == 403
    finally:
        await runner.cleanup()
