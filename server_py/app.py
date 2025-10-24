from __future__ import annotations
import asyncio
import time as _time
import json
import os
from pathlib import Path
from typing import Dict, Set, Optional
from math import isfinite
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response, Request
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
from pydantic import BaseModel
import yaml as _yaml
from .config import Config
from .state import State
from .sound import sound_info
from .depth import aggregate_top10, aggregate_both_top10, AggregatedLevel, AlertEvent, DepthLevel
from .ib_client import IBConfig, IBDepthManager

# Debug flag: Set to True to enable detailed debug logging
DEBUG = False

CONFIG_PATH = os.environ.get("CONFIG_PATH", "./config.tws.yaml")
cfg = Config.load(CONFIG_PATH)
app = FastAPI()

# --- T&S focused debug switch (env or config) ---
def _is_true(x) -> bool:
    s = str(x).strip().lower()
    return s in ("1", "true", "yes", "on", "debug")
TNS_DEBUG = _is_true(os.getenv("EI_TNS_DEBUG", "")) or _is_true(os.getenv("EI_DEBUG", "")) \
            or (str(getattr(cfg, "log_level", "")).lower() == "debug")
def tns_log(msg: str):
    """Thread-safe debug logger that works in both the main loop and AnyIO worker threads."""
    if not TNS_DEBUG:
        return
    try:
        ts = asyncio.get_running_loop().time()   # fast, monotonic, event-loop time
    except RuntimeError:
        ts = _time.perf_counter()                # fallback in threadpool
    print(f"[TNS {ts:.3f}] {msg}", flush=True)

# --- state & wiring ---
state = State(cooldown_seconds=cfg.cooldown_seconds, default_threshold=cfg.default_threshold_shares)
ws_clients: Set[WebSocket] = set()
ws_lock = asyncio.Lock()
# Sound
_snd = sound_info(cfg.sound_file)
# IB manager
manager = IBDepthManager(
    IBConfig(host=cfg.ib_host, port=cfg.ib_port, client_id=cfg.ib_client_id, smart_depth=cfg.smart_depth),
    on_status=lambda c: asyncio.create_task(broadcast_status(c)),
    on_snapshot=lambda sym, asks, bids: asyncio.create_task(on_dom_snapshot(sym, asks, bids)),
    on_error=lambda msg: asyncio.create_task(broadcast_error(msg)),
    on_tape_quote=lambda b,a: asyncio.create_task(broadcast_quote(b,a)),
    on_tape_trade=lambda ev: asyncio.create_task(broadcast_trade(ev)),
)
# --- lifecycle ---
@app.on_event("startup")
async def _startup():
    asyncio.create_task(manager.run())
@app.on_event("shutdown")
async def _shutdown():
    await manager.stop()
# --- static assets (serve existing ./web) ---
WEB_DIR = Path("web")
@app.get("/", include_in_schema=False)
def _index():
    return FileResponse(WEB_DIR / "index.html")
@app.get("/index.html", include_in_schema=False)
def _index2():
    return FileResponse(WEB_DIR / "index.html")
@app.get("/app.js", include_in_schema=False)
def _appjs():
    return FileResponse(WEB_DIR / "app.js")
@app.get("/styles.css", include_in_schema=False)
def _css():
    return FileResponse(WEB_DIR / "styles.css")
@app.get("/sounds/{filename}", include_in_schema=False)
def _sound(filename: str):
    path = WEB_DIR / "sounds" / filename
    if not path.exists():
        return PlainTextResponse("not found", status_code=404)
    ext = path.suffix.lower()
    if ext in (".wav", ".wave"):
        media = "audio/wav"
    elif ext in (".mp3", ".mpeg"):
        media = "audio/mpeg"
    else:
        media = "application/octet-stream"
    headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    return FileResponse(path, headers=headers, media_type=media)

# Service worker for sound caching (cache-first on /sounds/*)
@app.get("/sw.js", include_in_schema=False)
def _sw():
    p = WEB_DIR / "sw.js"
    if not p.exists():
        return PlainTextResponse("// no service worker", media_type="application/javascript")
    # always revalidate SW
    headers = {"Cache-Control": "no-cache"}
    return FileResponse(p, headers=headers, media_type="application/javascript")
# --- YAML endpoints ---
CONFIG_DATA_DIR = Path("./config-data")

def _read_yaml_or_default(filename: str, default_text: str) -> str:
    try:
        if CONFIG_DATA_DIR.exists():
            p = CONFIG_DATA_DIR / filename
            if p.exists() and p.is_file():
                return p.read_text(encoding="utf-8")
    except Exception:
        pass
    return default_text

@app.get("/api/yaml/watchlist", include_in_schema=False)
def yaml_watchlist():
    """
    Returns YAML for the ticker combobox.
    Expected structure (example):
      watchlist:
        - symbol: "AAPL"
        - symbol: "MSFT"
    """
    default_ = "watchlist: []\n"
    txt = _read_yaml_or_default("watchlist.yaml", default_)
    # 'text/yaml' is fine; many clients also use 'application/x-yaml'
    return PlainTextResponse(txt, media_type="text/yaml")

@app.get("/api/yaml/thresholds", include_in_schema=False)
def yaml_thresholds():
    """
    Returns YAML for the threshold combobox.
    Normalizes your on-disk root:
      thresholds.yaml:
        thresholds:
          - threshold: 5000
          - threshold: 10000
    ...into the UI-consumed shape:
      watchlist:
        - threshold: 5000
        - threshold: 10000
    """
    default_ = "watchlist: []\n"
    txt = _read_yaml_or_default("thresholds.yaml", default_)
    try:
        data = _yaml.safe_load(txt) or {}
        # accept either `watchlist:` or your chosen `thresholds:` root
        arr = data.get("watchlist")
        if not isinstance(arr, list):
            arr = data.get("thresholds", [])
        if not isinstance(arr, list):
            arr = []
        txt = _yaml.safe_dump({"watchlist": arr}, sort_keys=False)
    except Exception:
        # fall through with raw txt if something odd happens
        pass
    return PlainTextResponse(txt, media_type="text/yaml")

@app.get("/api/yaml/dollar-values", include_in_schema=False)
def yaml_dollar_values():
    """
    Returns YAML for the Dollar value combobox.
    Normalizes your on-disk root:
      dollar-value.yaml:
        dollarvalue:
          - label: "$10"
            threshold: 1000
            big_threshold: 10000
    ...into the UI-consumed shape:
      watchlist:
        - label: "$10"
          threshold: 1000
          big_threshold: 10000
    """
    default_ = "watchlist: []\n"
    txt = _read_yaml_or_default("dollar-value.yaml", default_)
    try:
        data = _yaml.safe_load(txt) or {}
        # accept either `watchlist:` or your chosen `dollarvalue:` root
        arr = data.get("watchlist")
        if not isinstance(arr, list):
            arr = data.get("dollarvalue", [])
        if not isinstance(arr, list):
            arr = []
        txt = _yaml.safe_dump({"watchlist": arr}, sort_keys=False)
    except Exception:
        pass
    return PlainTextResponse(txt, media_type="text/yaml")
# --- API models ---
class StartReq(BaseModel):
    symbol: str
    threshold: Optional[int] = None
    side: Optional[str] = None
    # T&S specific
    dollar: Optional[int] = None
    bigDollar: Optional[int] = None
    silent: Optional[bool] = None
class ThresholdReq(BaseModel):
    threshold: int
class SideReq(BaseModel):
    side: str
class SilentReq(BaseModel):
    silent: bool
# --- API routes ---
@app.get("/api/health")
def api_health():
    return {"ok": True, "connected": state.connected}
@app.get("/api/config")
def api_config():
    tns_log("GET /api/config")
    return {
        "defaultThresholdShares": cfg.default_threshold_shares,
        "currentThresholdShares": state.threshold,
        "cooldownSeconds": cfg.cooldown_seconds,
        "levelsToScan": cfg.levels_to_scan,
        "priceReference": cfg.price_reference,
        "smartDepth": cfg.smart_depth,
        "soundAvailable": _snd.available,
        "soundURL": _snd.url,
        "currentSide": state.side,
        # T&S config/state
        "silent": state.silent,
        "dollarThreshold": state.dollar_threshold,
        "bigDollarThreshold": state.big_dollar_threshold,
        "soundsPath": "/sounds/",  # base for ticksonic wavs
    }
@app.post("/api/start")
async def api_start(req: StartReq):
    sym = state.set_symbol(req.symbol)
    if req.threshold is not None and req.threshold > 0:
        state.set_threshold(req.threshold)
    if req.side:
        state.set_side(req.side)
    # T&S thresholds + mute
    state.set_tape_thresholds(req.dollar, req.bigDollar)
    if req.silent is not None:
        state.set_silent(req.silent)
    tns_log(f"POST /api/start sym={state.symbol} side={state.side} "
            f"thrShares={state.threshold} $thr={state.dollar_threshold} $big={state.big_dollar_threshold} "
            f"silent={state.silent}")
    # Allow starts even if not yet connected, to match dev affordance
    await manager.subscribe_symbol(sym)
    await broadcast_status(state.connected)
    return {"ok": True, "symbol": state.symbol, "threshold": state.threshold, "side": state.side}
@app.post("/api/stop")
async def api_stop():
    tns_log("POST /api/stop")
    state.set_symbol("")
    await manager.unsubscribe()
    await broadcast_status(state.connected)
    return {"ok": True}
@app.post("/api/threshold")
async def api_threshold(req: ThresholdReq):
    if req.threshold < 1:
        return PlainTextResponse("threshold must be >=1", status_code=400)
    state.set_threshold(req.threshold)
    tns_log(f"POST /api/threshold => {state.threshold}")
    return {"ok": True, "threshold": state.threshold}
@app.post("/api/side")
async def api_side(req: SideReq):
    s = state.set_side(req.side)
    tns_log(f"POST /api/side => {s}")
    return {"ok": True, "side": s}

@app.post("/api/silent")
async def api_silent(req: SilentReq):
    state.set_silent(req.silent)
    tns_log(f"POST /api/silent => {state.silent}")
    return {"ok": True, "silent": state.silent}
# --- WebSocket ---
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    async with ws_lock:
        ws_clients.add(ws)
    try:
        await send_json(ws, {"type": "status", "data": {"connected": state.connected, "symbol": state.symbol, "side": state.side}})
        while True:
            # we only use server → client; just keep connection alive
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with ws_lock:
            ws_clients.discard(ws)
# --- Broadcast helpers ---
async def send_json(ws: WebSocket, payload: Dict):
    await ws.send_text(json.dumps(payload, separators=(",", ":")))
async def broadcast(payload: Dict):
    stale = []
    async with ws_lock:
        if TNS_DEBUG:
            try:
                _t = payload.get("type", "")
                if _t in ("trade", "quote"):
                    tns_log(f"broadcast {_t} -> {len(ws_clients)} client(s)")
            except Exception:
                pass
        for ws in ws_clients:
            try:
                await ws.send_text(json.dumps(payload, separators=(",", ":")))
            except Exception:
                stale.append(ws)
        for ws in stale:
            ws_clients.discard(ws)
async def broadcast_status(connected: bool):
    state.set_connected(connected)
    await broadcast({"type": "status", "data": {"connected": connected, "symbol": state.symbol, "side": state.side}})
async def broadcast_book(levels: list[AggregatedLevel], side: str):
    # (Deprecated single-side broadcaster retained for back-compat)
    data = [{"price": float(l.price), "sumShares": l.sumShares, "rank": l.rank} for l in levels]
    await broadcast({"type": "book", "data": {"levels": data, "asks": data, "side": side}})

async def broadcast_book_full(
    asks: list[AggregatedLevel], bids: list[AggregatedLevel],
    best_ask, best_bid, last, volume
):
    tolist = lambda arr: [{"price": float(l.price), "sumShares": l.sumShares, "rank": l.rank} for l in arr]
    stats = {
        "bestBid": float(best_bid) if best_bid is not None else None,
        "bestAsk": float(best_ask) if best_ask is not None else None,
        "spread": (float(best_ask - best_bid) if (best_ask is not None and best_bid is not None) else None),
        "last": (float(last) if last is not None else None),
        "volume": int(volume) if volume is not None else None,
    }
    await broadcast({
        "type": "book",
        "data": {
            "asks": tolist(asks),
            "bids": tolist(bids),
            "side": state.side,
            "stats": stats
        }
    })
async def broadcast_alert(a: AlertEvent):
    await broadcast({"type": "alert", "data": {
        "side": a.side, "symbol": a.symbol, "price": float(a.price),
        "sumShares": a.sumShares, "timeISO": a.timeISO
    }})
async def broadcast_error(msg: str):
    # NEW: Ignore harmless Error 310
    if "Error 310" not in msg:
        await broadcast({"type": "error", "data": {"message": msg}})
# --- DOM → aggregation glue ---
async def on_dom_snapshot(symbol: str, asks: list[DepthLevel], bids: list[DepthLevel]):
    if DEBUG:
        print(f"DEBUG: on_dom_snapshot received data. Symbol: {symbol}, Current state symbol: {state.symbol}")
    # Ignore snapshots for stale symbols
    if symbol != state.symbol:
        if DEBUG:
            print("DEBUG: Symbol mismatch, discarding snapshot.")
        return
    ask_book, bid_book, alerts, best_ask, best_bid = aggregate_both_top10(state, asks, bids)
    if DEBUG:
        print(f"DEBUG: Aggregated both books. Asks: {len(ask_book)}, Bids: {len(bid_book)}, Alerts: {len(alerts)}")
    # Pull last/volume from IB manager
    last, volume = manager.current_quote()
    if ask_book or bid_book:
        await broadcast_book_full(ask_book, bid_book, best_ask, best_bid, last, volume)
    for a in alerts:
        await broadcast_alert(a)

# --- T&S broadcasting (TickSonic-compatible payloads) ---

def _fmt_amount(amount: float) -> tuple[str, bool]:
    # returns (label, is_big_label) — label mirrors TickSonic style
    if amount >= 1_000_000:
        m = amount / 1_000_000
        if abs(m - round(m)) < 1e-9:
            return (f"{m:.0f} million", True)
        return (f"{m:.1f} million", True)
    if amount >= 1_000:
        k = amount / 1_000
        if abs(k - round(k)) < 1e-9:
            return (f"{k:.0f}K", False)
        return (f"{k:.1f}K", False)
    return (f"{amount:.2f}", False)

def _classify_trade(price: float, bid: Optional[float], ask: Optional[float]) -> tuple[str, str]:
    eps = 1e-3
    if not (isfinite(price) and (bid is None or isfinite(bid)) and (ask is None or isfinite(ask))):
        return ("between_mid", "white")
    b = bid or 0.0
    a = ask or 0.0
    if b == 0.0 or a == 0.0:
        return ("between_mid", "white")
    if abs(price - a) < eps: return ("at_ask", "green")
    if abs(price - b) < eps: return ("at_bid", "red")
    if price > a + eps:      return ("above_ask", "yellow")
    if price < b - eps:      return ("below_bid", "magenta")
    da = abs(price - a); db = abs(price - b)
    if abs(da - db) < 1e-9:  return ("between_mid", "white")
    return ("between_ask", "white") if da < db else ("between_bid", "white")

# Keep most recent bid/ask seen (from tick-by-tick)
_last_bid: Optional[float] = None
_last_ask: Optional[float] = None

async def broadcast_quote(bid: float | None, ask: float | None):
    global _last_bid, _last_ask
    if bid is not None: _last_bid = bid
    if ask is not None: _last_ask = ask
    tns_log(f"QUOTE bid={bid} ask={ask} (last_bid={_last_bid} last_ask={_last_ask})")
    await broadcast({"type": "quote", "bid": bid, "ask": ask, "timeISO": None})

async def broadcast_trade(ev: dict):
    # Pull inputs
    sym = state.symbol or ev.get("sym") or ""
    price = float(ev.get("price") or 0.0)
    size  = int(ev.get("size") or 0)
    amount = price * size
    # Threshold filter (T&S only)
    if state.dollar_threshold and amount < state.dollar_threshold:
        tns_log(f"DROP trade (below $ threshold): sym={sym} px={price:.4f} sz={size} "
                f"amt={amount:.2f} < $thr={state.dollar_threshold}")
        return
    # Classify vs last seen bid/ask
    side, color = _classify_trade(price, _last_bid, _last_ask)
    big = bool(state.big_dollar_threshold and amount >= state.big_dollar_threshold)
    amountStr, _ = _fmt_amount(amount)
    tns_log(f"EMIT trade: sym={sym} px={price:.4f} sz={size} amt={amount:.2f} "
            f"bid={_last_bid} ask={_last_ask} side={side} big={big} "
            f"$thr={state.dollar_threshold} $big={state.big_dollar_threshold}")
    payload = {
        "type": "trade",
        "sym": sym, "price": price, "size": size,
        "amount": amount, "amountStr": amountStr,
        "timeISO": None,
        "side": side, "color": color, "big": big,
        "bid": _last_bid, "ask": _last_ask,
        "silent": state.silent,
    }
    await broadcast(payload)
