from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
from typing import Dict, Set, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response, Request
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
from pydantic import BaseModel
from .config import Config
from .state import State
from .sound import sound_info
from .depth import aggregate_top10, AggregatedLevel, AlertEvent, DepthLevel
from .ib_client import IBConfig, IBDepthManager
CONFIG_PATH = os.environ.get("CONFIG_PATH", "./config.tws.yaml")
cfg = Config.load(CONFIG_PATH)
app = FastAPI()
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
)
# --- lifecycle ---
@app.on_event("startup")
async def _startup():
    asyncio.create_task(manager.run())
@app.on_event("shutdown")
def _shutdown():
    manager.stop()
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
    # strong caching is handled by the UI with ?v=hash
    path = WEB_DIR / "sounds" / filename
    if not path.exists():
        return PlainTextResponse("not found", status_code=404)
    headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    return FileResponse(path, headers=headers, media_type="audio/mpeg")
# --- API models ---
class StartReq(BaseModel):
    symbol: str
    threshold: Optional[int] = None
    side: Optional[str] = None
class ThresholdReq(BaseModel):
    threshold: int
class SideReq(BaseModel):
    side: str
# --- API routes ---
@app.get("/api/health")
def api_health():
    return {"ok": True, "connected": state.connected}
@app.get("/api/config")
def api_config():
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
    }
@app.post("/api/start")
async def api_start(req: StartReq):
    sym = state.set_symbol(req.symbol)
    if req.threshold is not None and req.threshold > 0:
        state.set_threshold(req.threshold)
    if req.side:
        state.set_side(req.side)
    # Allow starts even if not yet connected, to match dev affordance
    await manager.subscribe_symbol(sym)
    await broadcast_status(state.connected)
    return {"ok": True, "symbol": state.symbol, "threshold": state.threshold, "side": state.side}
@app.post("/api/stop")
async def api_stop():
    state.set_symbol("")
    await broadcast_status(state.connected)
    # Manager keeps connection; subscription is effectively cleared by not resubscribing
    await manager.subscribe_symbol("") # no-op; safe
    return {"ok": True}
@app.post("/api/threshold")
async def api_threshold(req: ThresholdReq):
    if req.threshold < 1:
        return PlainTextResponse("threshold must be >=1", status_code=400)
    state.set_threshold(req.threshold)
    return {"ok": True, "threshold": state.threshold}
@app.post("/api/side")
async def api_side(req: SideReq):
    s = state.set_side(req.side)
    return {"ok": True, "side": s}
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
    # Keep field names aligned with your UI ({ levels, asks, side })
    data = [{"price": float(l.price), "sumShares": l.sumShares, "rank": l.rank} for l in levels]
    await broadcast({"type": "book", "data": {"levels": data, "asks": data, "side": side}})
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
    # Ignore snapshots for stale symbols
    if symbol != state.symbol:
        return
    levels, alerts = aggregate_top10(state, asks, bids)
    if levels:
        await broadcast_book(levels, state.side)
    for a in alerts:
        await broadcast_alert(a)
