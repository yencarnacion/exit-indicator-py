python3 -m venv .venv
source .venv/bin/activate
pip install -r server_py/requirements.txt

## Development & Tests

These tests run **completely offline** (no IB/TWS, no market data). They stub the IB client so you
can validate core logic and HTTP/WebSocket behavior before committing.

### 1) Create a virtualenv and install deps

```bash
python3 -m venv .venv
source .venv/bin/activate

# App runtime deps
pip install -r server_py/requirements.txt

# Test-only deps
pip install -r server_py/requirements-dev.txt
```

### 2) Run the test suite

```bash
# from the repo root
pytest -q
```

**Tip:** If a test fails, copy‑paste the failure block (assertion message + "got: …" details)
back to ChatGPT. The assertions were written to be diagnostic and easy to act on.

### 3) What the tests exercise

- `server_py/config.py` validation (strict invariants).
- `server_py/state.py` transitions and cooldown logic.
- `server_py/depth.py` aggregation (top-10 per side), alert emission, and best bid/ask selection.
- REST endpoints for health/config and YAML normalizers.
- WebSocket handshake for initial status (no real data feed required).
- T&S helpers (`_fmt_amount`, `_classify_trade`) and `broadcast_trade` dollar filters.
- Sound file hashing and URL cache-busting.

### 4) Offline guarantee

The test harness replaces the live IB/TWS manager with a `DummyManager`. Startup still runs,
but no real sockets are opened and no network is required.

---

## Why this setup will catch breakage early

- **Contract tests** on every public endpoint/data shape (including YAML normalization) will fail fast if schemas drift.
- **Aggregation/alert unit tests** guard your domain math (top‑10, cooldown, side‑specific alerts).
- **T&S tests** ensure audio/visual classification continues to match bid/ask logic and dollar filters.
- **WebSocket smoke test** verifies initial status framing without depending on a live feed.
- Diagnostic **assert messages** provide concrete "expected vs got" output you can paste back to me.