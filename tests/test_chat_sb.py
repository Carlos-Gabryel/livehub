"""Emulacao do Streamer.bot: handshake do @streamerbot/client usado pelo overlay (ex-sb_client_test)."""
import asyncio, json
import aiohttp
from aiohttp import web

from livehub.chat import sbserver
from livehub.chat.hub import Hub
from conftest import porta_livre

WANT = {"Twitch": ["ChatMessage", "Sub"], "Kick": ["ChatMessage", "Follow"]}


async def test_handshake_e_eventos():
    hub = Hub()
    cfg = {"twitch": {"channel": "canal"}, "youtube": {}, "kick": {}}
    porta = porta_livre()
    runner = web.AppRunner(sbserver.make_app(hub, cfg), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", porta).start()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"ws://127.0.0.1:{porta}/") as ws:
                hello = await ws.receive_json()
                assert hello["request"] == "Hello" and not hello.get("authentication")
                assert hello["info"]["os"] == "windows"

                async def req(**kw):
                    await ws.send_json({**kw, "id": "i1"})
                    return await asyncio.wait_for(ws.receive_json(), 5)

                assert (await req(request="GetInfo"))["status"] == "ok"
                assert "Twitch" in (await req(request="GetEvents"))["events"]
                assert (await req(request="Subscribe", events=WANT))["status"] == "ok"
                b = await req(request="GetBroadcaster")
                assert list(b["platforms"]) == ["twitch"] and b["platforms"]["twitch"]["broadcastUser"] == "canal"
                assert (await req(request="GetUserPronouns", platform="twitch", userLogin="x"))["pronoun"] == {"userFound": False}
                await hub.publish(None, [("Twitch", "ChatMessage", {"text": "oi", "user": {"name": "Ana"}}),
                                         ("Twitch", "Raid", {})])  # Raid nao assinado: nao chega
                d = json.loads((await asyncio.wait_for(ws.receive(), 3)).data)
                assert d["event"] == {"source": "Twitch", "type": "ChatMessage"} and d["data"]["text"] == "oi"
            try:
                async with s.ws_connect(f"ws://127.0.0.1:{porta}/", headers={"Origin": "http://evil.com"}):
                    raise AssertionError("deveria recusar")
            except aiohttp.WSServerHandshakeError as e:
                assert e.status == 403
    finally:
        await runner.cleanup()
