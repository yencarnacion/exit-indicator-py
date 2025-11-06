python3 -m venv .venv
source .venv/bin/activate
pip install -r server_py/requirements.txt

## Recording & Replay

The server supports recording live market data streams to compressed NDJSON files (`.ndjson.gz`) and replaying them later—perfect for backtesting, debugging, or development when markets are closed.

### Recording Live Data

Record 5+ minutes of market activity (e.g., AAPL):

```bash
# 1) Run your server in live mode with recording enabled
EI_RECORD_TO=./captures/aapl-5min-2025-11-06.ndjson.gz ./go.sh

# 2) In the web UI, subscribe to AAPL, let it run 5+ minutes, then stop the server
```

The recorded file captures:
- Order book depth snapshots (up to 10 levels per side)
- Real-time quote updates (bid/ask)
- Time & Sales trade events
- Accurate timestamps for replay

### Replaying Recorded Data

Replay any captured session (works even when markets are closed):

```bash
# Swap to PlaybackManager and replay at normal speed
EI_REPLAY_FROM=./captures/aapl-5min-2025-11-06.ndjson.gz \
EI_REPLAY_RATE=1.0 \
./go.sh
```

### Playback Options

Control playback behavior with environment variables:

- `EI_REPLAY_FROM` — Path to the `.ndjson.gz` recording file
- `EI_REPLAY_RATE` — Playback speed multiplier (default: `1.0`)
  - `2.0` = 2x faster
  - `0.5` = half speed (slow motion)
- `EI_REPLAY_LOOP` — Loop the recording continuously (default: `0`)
  - Set to `1`, `true`, `yes`, or `on` to enable

**Example: Fast replay with looping**

```bash
EI_REPLAY_FROM=./captures/aapl-5min.ndjson.gz \
EI_REPLAY_RATE=5.0 \
EI_REPLAY_LOOP=1 \
./go.sh
```

### Recording While Replaying

You can even record from a replay session (useful for testing recording logic):

```bash
EI_REPLAY_FROM=./old-session.ndjson.gz \
EI_RECORD_TO=./new-session.ndjson.gz \
./go.sh
```

### File Format

Recordings use NDJSON (newline-delimited JSON) with gzip compression:
- First line: metadata header with format version and timestamp
- Subsequent lines: timestamped events (`depth`, `quote`, `trade`)
- Timestamps are relative milliseconds from recording start

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

### 4) Regression tests with recorded data

The test suite includes **replay regression tests** that use recorded market data ("golden datasets") to verify your aggregation logic, alert generation, and OBI calculations haven't changed unexpectedly.

**Test file:** `tests/test_replay_regression.py`

These tests:
- Replay a short recorded tape at 10x speed
- Verify expected message types are broadcast (book, quote, trade, alert)
- Validate book structure (asks, bids, stats, OBI fields)
- Ensure **deterministic output** by replaying the same tape twice and comparing results
- Catch non-deterministic bugs in aggregation or state management

**Fixture:** `tests/fixtures/aapl-1sec.ndjson.gz`

This is a small (~1 second) recording of AAPL market data with:
- 3 depth snapshots
- 2 quote updates
- 3 trade events

**Running only regression tests:**

```bash
pytest tests/test_replay_regression.py -v
```

**Creating your own golden datasets:**

1. Record a session with interesting market conditions:
   ```bash
   EI_RECORD_TO=./tests/fixtures/new-scenario.ndjson.gz ./go.sh
   ```

2. Subscribe to a symbol and let it run until you capture the behavior you want to test

3. Create a new test in `test_replay_regression.py` that replays your fixture and asserts expected outputs

**Why these tests are valuable:**

- **Regression protection:** Catches unintended changes to aggregation math, alert thresholds, or OBI calculations
- **Reproducibility:** Market conditions are frozen in time—tests run identically on every machine
- **Fast feedback:** No need to wait for live market data; tests complete in seconds
- **Works offline:** No IB/TWS connection required

### 5) Offline guarantee

The test harness replaces the live IB/TWS manager with a `DummyManager`. Startup still runs,
but no real sockets are opened and no network is required.

---

## Why this setup will catch breakage early

- **Contract tests** on every public endpoint/data shape (including YAML normalization) will fail fast if schemas drift.
- **Aggregation/alert unit tests** guard your domain math (top‑10, cooldown, side‑specific alerts).
- **T&S tests** ensure audio/visual classification continues to match bid/ask logic and dollar filters.
- **WebSocket smoke test** verifies initial status framing without depending on a live feed.
- **Replay regression tests** verify aggregation, alerts, and OBI calculations using frozen market conditions (golden datasets).
- Diagnostic **assert messages** provide concrete "expected vs got" output you can paste back to me.