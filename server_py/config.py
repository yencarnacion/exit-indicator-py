# server_py/config.py
from __future__ import annotations
from dataclasses import dataclass
import yaml
from typing import Optional

@dataclass
class Config:
    # Web/UI
    port: int = 8086
    default_threshold_shares: int = 20_000
    sound_file: str = "./web/sounds/hey.mp3"
    cooldown_seconds: int = 1
    smart_depth: bool = True
    levels_to_scan: int = 10
    price_reference: str = "best_ask"
    log_level: str = "info"
    # TWS / IB Gateway
    ib_host: str = "127.0.0.1"
    ib_port: int = 7497 # 7497 paper, 7496 live
    ib_client_id: int = 1337 # arbitrary client id

    # OBI (Order Book Imbalance)
    obi_enabled: bool = True
    obi_alpha: Optional[float] = None     # None => auto heuristic
    obi_levels_max: int = 3               # 1..10 (app clamps to <=3)

    # RVOL
    rvol_enabled: bool = True
    rvol_threshold: float = 2.0
    rvol_lookback_days: int = 10

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg = cls(**{**cls().__dict__, **data})
        # Validation
        if cfg.levels_to_scan != 10:
            raise ValueError("levels_to_scan must be 10")
        if cfg.price_reference != "best_ask":
            raise ValueError('price_reference must be "best_ask"')
        # OBI invariants
        if not (1 <= int(cfg.obi_levels_max) <= 10):
            raise ValueError("obi_levels_max must be between 1 and 10")
        if cfg.obi_alpha is not None:
            try:
                a = float(cfg.obi_alpha)
            except Exception:
                raise ValueError("obi_alpha must be a float or null")
            if not (0.0 < a < 5.0):
                raise ValueError("obi_alpha must be in (0, 5)")

        # RVOL invariants
        if int(cfg.rvol_lookback_days) < 1:
            raise ValueError("rvol_lookback_days must be >= 1")
        if float(cfg.rvol_threshold) <= 0:
            raise ValueError("rvol_threshold must be > 0")
        if cfg.port < 1 or cfg.port > 65535:
            raise ValueError("invalid port")
        if cfg.default_threshold_shares < 1:
            raise ValueError("default_threshold_shares must be >= 1")
        return cfg
