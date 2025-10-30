import asyncio
import json
import pytest


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, txt: str):
        self.sent.append(txt)


@pytest.mark.asyncio
async def test_broadcast_sends_to_ws_clients(app_module):
    ws = FakeWS()
    app_module.ws_clients.add(ws)
    await app_module.broadcast({"type": "status", "data": {"connected": True, "symbol": "AAPL", "side": "ASK"}})
    assert ws.sent, "No message delivered to fake websocket"
    payload = json.loads(ws.sent[-1])
    assert payload["type"] == "status" and payload["data"]["connected"] is True, f"Wrong payload: {payload}"
