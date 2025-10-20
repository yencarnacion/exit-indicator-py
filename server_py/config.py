from __future__ import annotations
from dataclasses import dataclass
import yaml

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
    ib_port: int = 7497         # 7497 paper, 7496 live
    ib_client_id: int = 1337    # arbitrary client id

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
        if cfg.port < 1 or cfg.port > 65535:
            raise ValueError("invalid port")
        if cfg.default_threshold_shares < 1:
            raise ValueError("default_threshold_shares must be >= 1")
        return cfg
