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
    # --- T&S state ---
    dollar_threshold: int = 0           # filters T&S prints (amount >= threshold)
    big_dollar_threshold: int = 0       # marks "big" prints (affects sound/pitch + row style)
    silent: bool = False                # global mute (applies to alert sound + T&S sounds)

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

    def set_tape_thresholds(self, dollar: int | None, big_dollar: int | None):
        if dollar is not None:
            self.dollar_threshold = max(0, int(dollar))
        if big_dollar is not None:
            self.big_dollar_threshold = max(0, int(big_dollar))

    def set_silent(self, v: bool | int | str):
        self.silent = bool(v) if isinstance(v, bool) else (str(v).lower() in ("1","true","yes","on"))

    def allow_alert(self, symbol: str, price: Decimal, now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        # Canonicalize price to 4 d.p. to match aggregation buckets and UI keys
        try:
            canonical = price.quantize(Decimal("0.0001"))
        except Exception:
            canonical = price
        key = f"{symbol.upper()}:{canonical}"
        last = self._last_alert.get(key, 0.0)
        if now - last >= self.cooldown_seconds:
            self._last_alert[key] = now
            return True
        return False
