import asyncio
import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


class DummyManager:
    """
    Offline stub that mimics the bits of IBDepthManager the app uses.
    Prevents any real sockets or TWS connections during tests.
    """
    def __init__(self):
        self.sym = ""
        self._last = None
        self._vol = None

    async def run(self):
        # Keep it as an awaitable so startup doesn't crash.
        await asyncio.sleep(0)

    async def stop(self):
        return

    async def subscribe_symbol(self, sym: str):
        self.sym = (sym or "").upper()

    async def unsubscribe(self):
        self.sym = ""

    def current_quote(self):
        return self._last, self._vol


@pytest.fixture(scope="session")
def app_module():
    """
    Import the FastAPI app once and replace the manager with a dummy stub.
    """
    import server_py.app as app
    app.manager = DummyManager()  # swap to offline dummy
    return app


@pytest.fixture(autouse=True)
def reset_app_state(app_module):
    """
    Ensure each test starts from a clean application state.
    """
    # Reset State
    s = app_module.state
    s.symbol = ""
    s.side = "ASK"
    s.threshold = s.default_threshold
    s.connected = False
    s._last_alert.clear()
    s.dollar_threshold = 0
    s.big_dollar_threshold = 0
    s.silent = False

    # Reset WS
    app_module.ws_clients.clear()

    yield


@pytest.fixture
def client(app_module):
    """
    Starlette TestClient that runs startup/shutdown hooks but uses DummyManager.
    """
    return TestClient(app_module.app)


@pytest.fixture
def set_config_dir(app_module, tmp_path, monkeypatch):
    """
    Helper to redirect the /api/yaml/* endpoints to a temp config-data directory.
    """
    cfgdir = tmp_path / "config-data"
    cfgdir.mkdir()
    monkeypatch.setattr(app_module, "CONFIG_DATA_DIR", cfgdir)
    return cfgdir


@pytest.fixture
def capture_broadcast(app_module, monkeypatch):
    """
    Capture app.broadcast(payload) calls (used by T&S + DOM broadcasts).
    """
    messages = []

    async def fake_broadcast(payload: dict):
        messages.append(payload)

    monkeypatch.setattr(app_module, "broadcast", fake_broadcast)
    return messages
