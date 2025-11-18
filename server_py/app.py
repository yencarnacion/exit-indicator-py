from __future__ import annotations
import asyncio
import time as _time
import json
import os
import math
import contextlib
from pathlib import Path
from typing import Dict, Set, Optional
from math import isfinite
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response, Request
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
from pydantic import BaseModel
import yaml as _yaml
from .config import Config
from .state import State
from .sound import sound_info
from .depth import aggregate_top10, aggregate_both_top10, AggregatedLevel, AlertEvent, DepthLevel
from decimal import Decimal
from .ib_client import IBConfig, IBDepthManager
from .obi import compute_obi, choose_alpha_heuristic
from .recording import NDJSONRecorder
from .replay import PlaybackManager, ReplayConfig

# Debug flag: Set to True to enable detailed debug logging
DEBUG = False

CONFIG_PATH = os.environ.get("CONFIG_PATH", "./config.tws.yaml")
cfg = Config.load(CONFIG_PATH)

# --- DOM outlier clamp (recommended) -----------------------------------------
def _get_anchor_price() -> float | None:
    """Midpoint of last tick-by-tick bid/ask when available; else last trade."""
    try:
        if _last_bid is not None and _last_ask is not None:
            b = float(_last_bid); a = float(_last_ask)
            if isfinite(b) and isfinite(a) and b > 0 and a > 0:
                return (a + b) * 0.5
    except Exception:
        pass
    try:
        last, _ = manager.current_quote()
        if last is not None and isfinite(float(last)) and float(last) > 0:
            return float(last)
    except Exception:
        pass
    return None

def _filter_dom_outliers(asks: list[DepthLevel], bids: list[DepthLevel]) -> tuple[list[DepthLevel], list[DepthLevel]]:
    anchor = _get_anchor_price()
    if anchor is None or anchor <= 0:
        return asks, bids
    try:
        pct = float(os.getenv("EI_L2_BAND_PCT", "0.20") or "0.20")
    except Exception:
        pct = 0.20
    pct = max(0.05, min(pct, 0.50))  # clamp to 5–50%
    lo = anchor * (1.0 - pct)
    hi = anchor * (1.0 + pct)
    def keep(row: DepthLevel) -> bool:
        try:
            p = float(row.price)
            return (p >= lo) and (p <= hi)
        except Exception:
            return False
    A = [r for r in asks if keep(r)]
    B = [r for r in bids if keep(r)]
    # If filtering nuked a side entirely (e.g., at session start), keep originals
    return (A or asks), (B or bids)

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

# Recording setup
REC_PATH = os.getenv("EI_RECORD_TO", "").strip()
REPLAY_FROM = os.getenv("EI_REPLAY_FROM", "").strip()
REPLAY_RATE = float(os.getenv("EI_REPLAY_RATE", "1.0"))
REPLAY_LOOP = os.getenv("EI_REPLAY_LOOP", "0").lower() in ("1","true","yes","on")

recorder: NDJSONRecorder | None = None
if REC_PATH:
    recorder = NDJSONRecorder(REC_PATH, meta={"started_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())})

# Manager: choose live vs playback
if REPLAY_FROM:
    manager = PlaybackManager(
        ReplayConfig(path=REPLAY_FROM, rate=REPLAY_RATE, loop=REPLAY_LOOP),
        on_status=lambda c: asyncio.create_task(broadcast_status(c)),
        on_snapshot=lambda sym, asks, bids: asyncio.create_task(on_dom_snapshot(sym, asks, bids)),
        on_error=lambda msg: asyncio.create_task(broadcast_error(msg)),
        on_tape_quote=lambda b,a: asyncio.create_task(broadcast_quote(b,a)),
        on_tape_trade=lambda ev: asyncio.create_task(broadcast_trade(ev)),
    )
else:
    manager = IBDepthManager(
        IBConfig(host=cfg.ib_host, port=cfg.ib_port, client_id=cfg.ib_client_id, smart_depth=cfg.smart_depth),
        on_status=lambda c: asyncio.create_task(broadcast_status(c)),
        on_snapshot=lambda sym, asks, bids: asyncio.create_task(on_dom_snapshot(sym, asks, bids)),
        on_error=lambda msg: asyncio.create_task(broadcast_error(msg)),
        on_tape_quote=lambda b,a: asyncio.create_task(broadcast_quote(b,a)),
        on_tape_trade=lambda ev: asyncio.create_task(broadcast_trade(ev)),
    )

# --- periodic stats heartbeat (default: every 1.0s; override via EI_STATS_HEARTBEAT_SEC) ---
HEARTBEAT_SECONDS = float(os.getenv("EI_STATS_HEARTBEAT_SEC", "1.0") or "1.0")

async def _stats_heartbeat():
    """
    Push a tiny 'stats' frame with last/volume at a fixed cadence so the UI
    refreshes even when DOM/quotes are quiet.
    """
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            if not state.symbol:
                continue
            last, volume = manager.current_quote()
            if last is None and volume is None:
                continue
            await broadcast({"type": "stats", "data": {"last": last, "volume": volume}})
    except asyncio.CancelledError:
        pass

# --- lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    mgr_task = asyncio.create_task(manager.run())
    hb_task = asyncio.create_task(_stats_heartbeat())
    yield
    hb_task.cancel()
    with contextlib.suppress(Exception):
        await hb_task
    await manager.stop()
    if recorder:
        await recorder.close()

app = FastAPI(lifespan=lifespan)
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

class MicroVWAPReq(BaseModel):
    minutes: float
    band_k: float
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
        # OBI indicator config
        "obi": {
            "enabled": bool(getattr(cfg, "obi_enabled", True)),
            "alpha": getattr(cfg, "obi_alpha", None),
            "levelsMax": getattr(cfg, "obi_levels_max", 3),
        },
        # Micro VWAP config (supports both DummyManager and IBDepthManager)
        "microVWAPConfig": {
            "minutes": (
                getattr(manager, "_micro_window_minutes", None)
                if getattr(manager, "_micro_window_minutes", None) is not None
                else (
                    getattr(manager, "_micro_window_sec", None) / 60.0
                    if getattr(manager, "_micro_window_sec", None) is not None
                    else None
                )
            ),
            "bandK": getattr(manager, "_micro_band_k", 2.0),
        },
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

@app.post("/api/microvwap")
async def api_microvwap(req: MicroVWAPReq):
    """
    Configure micro-VWAP window (minutes) and band multiplier k.
    Keeps logic on server so stats + hints match the UI.
    """
    minutes = max(0.5, min(float(req.minutes), 60.0))  # clamp 0.5–60 min
    band_k = max(0.5, min(float(req.band_k), 4.0))     # clamp 0.5–4σ
    # Persist on manager if supported
    if hasattr(manager, "set_micro_window_minutes"):
        manager.set_micro_window_minutes(minutes)
    # Store band_k on manager in a generic way
    setattr(manager, "_micro_band_k", band_k)
    tns_log(f"POST /api/microvwap => minutes={minutes} band_k={band_k}")
    return {"ok": True, "minutes": minutes, "band_k": band_k}
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
    best_ask, best_bid, last, volume,
    obi: float | None = None,
    obi_alpha: float | None = None,
    obi_levels: int | None = None,
):
    tolist = lambda arr: [{"price": float(l.price), "sumShares": l.sumShares, "rank": l.rank} for l in arr]
    # micro VWAP (from manager, if available)
    micro_vwap = None
    micro_sigma = None
    try:
        if hasattr(manager, "_micro_vwap_and_sigma"):
            micro_vwap, micro_sigma = manager._micro_vwap_and_sigma()
    except Exception:
        micro_vwap, micro_sigma = None, None

    # Simple action hint: compact, mutually exclusive, glanceable
    def _compute_action_hint():
        if last is not None:
            px = float(last)
        elif best_bid is not None and best_ask is not None:
            px = float(best_bid + best_ask) / 2.0
        else:
            px = None
        if px is None or micro_vwap is None:
            return None
        # band multiplier from manager (set via /api/microvwap), default 2σ
        k = float(getattr(manager, "_micro_band_k", 2.0) or 2.0)
        band = (micro_sigma or 0.0) * k
        if band <= 0:
            return None
        dist = px - micro_vwap
        # Normalize for stability
        # Rough thresholds: significant extension when |dist| > band
        # Use OBI to gate "ok to fade" vs "trend".
        o = obi if obi is not None else 0.0

        # Long fade ok: below lower band, selling not dominant
        if dist <= -band and o > -0.1:
            return "long_ok"
        # Short fade ok: above upper band, buying not dominant
        if dist >= band and o < 0.1:
            return "fade_short_ok"
        # Trend up: above band with strong bid/OBI
        if dist >= band and o >= 0.3:
            return "trend_up"
        # Trend down: below band with strong ask/OBI
        if dist <= -band and o <= -0.3:
            return "trend_down"
        return None

    action_hint = _compute_action_hint()

    stats = {
        "bestBid": float(best_bid) if best_bid is not None else None,
        "bestAsk": float(best_ask) if best_ask is not None else None,
        "spread": (float(best_ask - best_bid) if (best_ask is not None and best_bid is not None) else None),
        "last": (float(last) if last is not None else None),
        "volume": int(volume) if volume is not None else None,
        "obi": float(obi) if obi is not None else None,
        "obiAlpha": float(obi_alpha) if obi_alpha is not None else None,
        "obiLevels": int(obi_levels) if obi_levels is not None else None,
        "microVWAP": float(micro_vwap) if micro_vwap is not None else None,
        "microSigma": float(micro_sigma) if micro_sigma is not None else None,
        "microBandK": float(getattr(manager, "_micro_band_k", 2.0)),
        "actionHint": action_hint,
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
    if recorder:
        recorder.record_depth(symbol, asks, bids)
    if DEBUG:
        print(f"DEBUG: on_dom_snapshot received data. Symbol: {symbol}, Current state symbol: {state.symbol}")
    # Ignore snapshots for stale symbols
    if symbol != state.symbol:
        if DEBUG:
            print("DEBUG: Symbol mismatch, discarding snapshot.")
        return
    # Drop extreme DOM outliers relative to the current anchor before aggregating.
    asks, bids = _filter_dom_outliers(asks, bids)
    ask_book, bid_book, alerts, best_ask, best_bid = aggregate_both_top10(state, asks, bids)
    if DEBUG:
        print(f"DEBUG: Aggregated both books. Asks: {len(ask_book)}, Bids: {len(bid_book)}, Alerts: {len(alerts)}")
    # Pull last/volume from IB manager
    last, volume = manager.current_quote()

    # --- Sanity guard: if DOM best is clearly wrong, trust NBBO (tick-by-tick) ---
    try:
        use_nbbo = False
        if _last_bid is not None and _last_ask is not None and _last_ask == _last_ask and _last_bid == _last_bid:
            # Consider DOM bad if missing, crossed, or >20% off NBBO
            def _bad(px, ref):
                try:
                    return (px is None) or (float(px) <= 0) or \
                           (abs(float(px) - float(ref)) / max(1e-9, abs(float(ref))) > 0.20)
                except Exception:
                    return True
            if best_ask is None or best_bid is None:
                use_nbbo = True
            elif float(best_ask) <= float(best_bid):
                use_nbbo = True
            elif _bad(best_ask, _last_ask) or _bad(best_bid, _last_bid):
                use_nbbo = True
        if use_nbbo:
            best_bid = Decimal(str(_last_bid))
            best_ask = Decimal(str(_last_ask))
    except Exception:
        # Never let the guardrail crash the pipeline
        pass

    # --- OBI computation (top ≤3 levels per side) ---
    obi_val = None
    obi_alpha_used = None
    obi_levels_used = None
    if getattr(cfg, "obi_enabled", True) and ask_book and bid_book:
        levels_avail = min(len(ask_book), len(bid_book))
        L = max(0, min(getattr(cfg, "obi_levels_max", 3), 3, levels_avail))
        if L > 0:
            qb = [bid_book[i].sumShares for i in range(L)]
            qa = [ask_book[i].sumShares for i in range(L)]
            # Respect explicit alpha if provided; otherwise heuristic
            alpha_cfg = getattr(cfg, "obi_alpha", None)
            obi_alpha_used = (float(alpha_cfg) if isinstance(alpha_cfg, (int, float)) else
                              choose_alpha_heuristic(qb, qa))
            obi_val = compute_obi(qb, qa, obi_alpha_used)
            obi_levels_used = L
    if ask_book or bid_book:
        await broadcast_book_full(ask_book, bid_book, best_ask, best_bid, last, volume,
                                  obi=obi_val, obi_alpha=obi_alpha_used, obi_levels=obi_levels_used)
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
    if recorder:
        recorder.record_quote(bid, ask)
    if bid is not None: _last_bid = bid
    if ask is not None: _last_ask = ask
    tns_log(f"QUOTE bid={bid} ask={ask} (last_bid={_last_bid} last_ask={_last_ask})")
    last, volume = manager.current_quote()
    await broadcast({
        "type": "quote", "bid": bid, "ask": ask,
        "last": last, "volume": volume, "timeISO": None
    })

async def broadcast_trade(ev: dict):
    if recorder:
        recorder.record_trade(ev)
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
    last, volume = manager.current_quote()
    payload = {
        "type": "trade",
        "sym": sym, "price": price, "size": size,
        "amount": amount, "amountStr": amountStr,
        "timeISO": None,
        "last": last,
        "volume": volume,
        "side": side, "color": color, "big": big,
        "bid": _last_bid, "ask": _last_ask,
        "silent": state.silent,
    }
    await broadcast(payload)
