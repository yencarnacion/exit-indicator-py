import json


def test_websocket_initial_status(client):
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_text()
        msg = json.loads(first)
        assert msg["type"] == "status" and "data" in msg, f"Unexpected WS message: {msg}"
        assert "connected" in msg["data"] and "symbol" in msg["data"] and "side" in msg["data"], \
            f"Status payload missing fields: {msg}"
        # Allow server loop to progress once, then close gracefully
        ws.send_text("ping")
