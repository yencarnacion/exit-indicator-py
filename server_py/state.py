from __future__ import annotations
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict

@dataclass
class State:
    cooldown_seconds: float
    default_threshold: int

    symbol: str = ""
    side: str = "ASK"  # or "BID"
    threshold: int = field(default_factory=int)
    connected: bool = False
    _last_alert: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        if not self.threshold:
            self.threshold = max(1, int(self.default_threshold))

    def set_symbol(self, sym: str) -> str:
        self.symbol = sym.strip().upper()
        return self.symbol

    def set_side(self, side: str) -> str:
        s = (side or "ASK").strip().upper()
        self.side = "BID" if s == "BID" else "ASK"
        return self.side

    def set_threshold(self, v: int):
        self.threshold = max(1, int(v))

    def set_connected(self, v: bool):
        self.connected = bool(v)

    def allow_alert(self, symbol: str, price: Decimal, now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        key = f"{symbol.upper()}:{price.normalize()}"
        last = self._last_alert.get(key, 0.0)
        if now - last >= self.cooldown_seconds:
            self._last_alert[key] = now
            return True
        return False
