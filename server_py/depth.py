from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Dict, List, Literal, Tuple, Optional
from .state import State
getcontext().prec = 28 # safe precision for price math
Side = Literal["ASK", "BID"]
@dataclass(frozen=True)
class DepthLevel:
    side: Side
    price: Decimal
    size: int
    venue: str
    level: int
@dataclass(frozen=True)
class AggregatedLevel:
    price: Decimal
    sumShares: int
    rank: int
@dataclass(frozen=True)
class AlertEvent:
    side: Side
    symbol: str
    price: Decimal
    sumShares: int
    timeISO: str
def _price_key(p: Decimal) -> str:
    # Canonical key so numerically-equal decimals hash to the same key.
    # Align with UI (it groups rows by p.toFixed(4))
    return f"{p.quantize(Decimal('0.0001')):f}"

def _aggregate_for_side(
    state: State, rows: List[DepthLevel], side: Side
) -> Tuple[List[AggregatedLevel], List[AlertEvent], Optional[Decimal]]:
    if not rows:
        return [], [], None
    sums: Dict[str, int] = {}
    pmap: Dict[str, Decimal] = {}
    for r in rows:
        if r.side != side:
            continue
        if r.size <= 0:
            continue
        if not r.price.is_finite() or r.price <= 0:
            continue
        k = _price_key(r.price)
        sums[k] = sums.get(k, 0) + int(r.size)
        pmap[k] = pmap.get(k, r.price)
    if not sums:
        return [], [], None
    keys = list(sums.keys())
    # Sort: best ask lowest first; best bid highest first
    keys.sort(key=lambda k: (pmap[k],) if side == "ASK" else (-pmap[k],))
    keys = keys[:10]
    book: List[AggregatedLevel] = []
    alerts: List[AlertEvent] = []
    thr = state.threshold
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    for i, k in enumerate(keys):
        p = pmap[k]
        total = sums[k]
        book.append(AggregatedLevel(price=p, sumShares=total, rank=i))
        # Only alert on the state-selected side
        if side == state.side and total >= thr and state.allow_alert(state.symbol, p):
            alerts.append(AlertEvent(
                side=side, symbol=state.symbol, price=p, sumShares=total, timeISO=now_iso
            ))
    best_price = pmap[keys[0]] if keys else None
    return book, alerts, best_price

def aggregate_both_top10(
    state: State, asks: List[DepthLevel], bids: List[DepthLevel]
) -> Tuple[List[AggregatedLevel], List[AggregatedLevel], List[AlertEvent], Optional[Decimal], Optional[Decimal]]:
    ask_book, ask_alerts, best_ask = _aggregate_for_side(state, asks, "ASK")
    bid_book, bid_alerts, best_bid = _aggregate_for_side(state, bids, "BID")
    alerts = ask_alerts + bid_alerts
    return ask_book, bid_book, alerts, best_ask, best_bid

def aggregate_top10(state: State, asks: List[DepthLevel], bids: List[DepthLevel]) -> Tuple[List[AggregatedLevel], List[AlertEvent]]:
    side = state.side
    rows = asks if side == "ASK" else bids
    if not rows:
        return [], []
    sums: Dict[str, int] = {}
    pmap: Dict[str, Decimal] = {}
    for r in rows:
        if r.side != side:
            continue
        if r.size <= 0:
            continue
        if not r.price.is_finite() or r.price <= 0:
            continue
        k = _price_key(r.price)
        sums[k] = sums.get(k, 0) + int(r.size)
        pmap[k] = pmap.get(k, r.price)
    if not sums:
        return [], []
    # Sort: best ask lowest first; best bid highest first
    keys = list(sums.keys())
    keys.sort(key=lambda k: (pmap[k],) if side == "ASK" else (-pmap[k],))
    keys = keys[:10] # levels_to_scan enforced by config validator
    book: List[AggregatedLevel] = []
    alerts: List[AlertEvent] = []
    thr = state.threshold
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    for i, k in enumerate(keys):
        p = pmap[k]
        total = sums[k]
        book.append(AggregatedLevel(price=p, sumShares=total, rank=i))
        if total >= thr and state.allow_alert(state.symbol, p):
            alerts.append(AlertEvent(
                side=side, symbol=state.symbol, price=p, sumShares=total, timeISO=now_iso
            ))
    return book, alerts
