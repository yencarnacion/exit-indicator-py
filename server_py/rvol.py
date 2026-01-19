# server_py/rvol.py
from __future__ import annotations

import bisect
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import DefaultDict, List, Optional, TYPE_CHECKING

# Only for type hints (keeps import-time lighter / safer)
if TYPE_CHECKING:
    from ib_async import IB, Contract

# Timezone: be robust if tzdata is missing (containers)
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    TZ_LABEL = "ET"
except Exception:
    ET = timezone.utc
    TZ_LABEL = "UTC"

@dataclass
class RVOLAlert:
    symbol: str
    price: float
    volume: int
    baseline: float
    rvol: float
    percentile: float
    samples: int
    nonzero: int
    pace: bool
    elapsed_sec: Optional[int]
    time_str: str
    # Scalper-friendly extras (optional; UI can ignore if not used)
    projected_volume: Optional[int] = None
    projected_percentile: Optional[float] = None

class RVOLManager:
    """
    Scalper-focused RVOL:
      - Method B: median baseline (single 1-minute bar baseline per minute-of-day bucket)
      - percentile rank vs bucket history
      - trade-driven pace RVOL so alerts can fire before the 1-min bar closes
    """
    def __init__(self, lookback_days: int = 10, threshold: float = 2.0):
        self.lookback_days = lookback_days
        self.threshold = threshold
        self.baselines: DefaultDict[int, List[int]] = defaultdict(list)  # Bucket -> [Volumes]
        self.active_symbol: str = ""
        
        # Real-time state
        self.current_minute_start: int = 0
        self.vol_so_far: int = 0
        self.last_pace_check: float = 0
        self.last_alert_at_pace: float = 0
        self.last_alert_at_close: float = 0

        # Track last trade price so close alerts have a price
        self._last_price: Optional[float] = None
        
        # Helper for cooldowns
        self.cooldown_sec = 60.0
        # Lower throttle = more scalper-responsive; still prevents per-trade spam
        self.pace_throttle_sec = 0.25

    def reset(self) -> None:
        """Clear symbol + history so switching symbols can't leak state."""
        self.active_symbol = ""
        self.baselines.clear()
        self.current_minute_start = 0
        self.vol_so_far = 0
        self.last_pace_check = 0.0
        self.last_alert_at_pace = 0.0
        self.last_alert_at_close = 0.0
        self._last_price = None

    def _get_bucket_index(self, dt: datetime) -> int:
        """Minute index since 04:00 ET."""
        dt_et = dt.astimezone(ET)
        # Base is 04:00 ET of the same day
        base = dt_et.replace(hour=4, minute=0, second=0, microsecond=0)
        diff = dt_et - base
        return int(diff.total_seconds() // 60)

    async def start_symbol(self, ib: "IB", contract: "Contract", symbol: str, *, preserve_live_state: bool = False):
        """
        Backfill baseline data and prepare for streaming.
        If preserve_live_state=True and we're already tracking this symbol, keep the
        in-progress minute counters so 'start-before-connect' doesn't lose early prints.
        """
        same_symbol = (self.active_symbol == symbol)
        self.active_symbol = symbol
        self.baselines.clear()

        if not (preserve_live_state and same_symbol):
            self.vol_so_far = 0
            self.current_minute_start = 0
            self.last_pace_check = 0.0
            self.last_alert_at_pace = 0.0
            self.last_alert_at_close = 0.0
            self._last_price = None
        
        if not ib.isConnected() or not contract:
            return

        # Fetch history: 1 min bars, TRADES, useRTH=False (to get pre-market)
        # Duration: e.g. "10 D"
        try:
            bars = await ib.reqHistoricalDataAsync(
                contract,
                endDateTime='',
                durationStr=f'{self.lookback_days} D',
                barSizeSetting='1 min',
                whatToShow='TRADES',
                useRTH=False,
                formatDate=2,  # UTC seconds
                keepUpToDate=False
            )
            
            for bar in bars:
                vol = int(getattr(bar, "volume", 0) or 0)
                # Keep zeros out of the baseline computation by filtering later,
                # but recording them allows samples/nonzero to be meaningful.
                if vol < 0:
                    continue

                d = getattr(bar, "date", None)
                dt = None
                if isinstance(d, datetime):
                    dt = d
                    if dt.tzinfo is None:
                        # IB often returns UTC-ish datetimes for historical bars; be defensive.
                        dt = dt.replace(tzinfo=timezone.utc)
                    else:
                        dt = dt.astimezone(timezone.utc)
                else:
                    # Best-effort parsing for int/str forms
                    try:
                        if isinstance(d, (int, float)):
                            dt = datetime.fromtimestamp(float(d), tz=timezone.utc)
                        elif isinstance(d, str):
                            s = d.strip()
                            if s.isdigit():
                                dt = datetime.fromtimestamp(float(s), tz=timezone.utc)
                            else:
                                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                                if dt.tzinfo is None:
                                    dt = dt.replace(tzinfo=timezone.utc)
                                else:
                                    dt = dt.astimezone(timezone.utc)
                    except Exception:
                        dt = None
                if dt is None:
                    continue

                bucket = self._get_bucket_index(dt)
                if bucket >= 0:
                    self.baselines[bucket].append(vol)
            
            print(f"[RVOL] Backfill complete for {symbol}. Loaded {len(bars)} bars into {len(self.baselines)} buckets.")
            
        except Exception as e:
            print(f"[RVOL] Backfill failed: {e}")

    @staticmethod
    def _percentile_rank(sorted_vals: List[int], x: int) -> float:
        """Percent of samples <= x (0..100)."""
        if not sorted_vals:
            return 0.0
        i = bisect.bisect_right(sorted_vals, x)
        return 100.0 * i / len(sorted_vals)

    def on_trade(self, price: float, size: int, now_utc: float = 0) -> List[RVOLAlert]:
        """
        Process a live trade and return 0..N alerts.
        (Important: minute-rollover can yield a close alert AND still ingest the new minute's first trade.)
        """
        out: List[RVOLAlert] = []
        if not self.active_symbol or size <= 0:
            return out

        if now_utc == 0:
            now_utc = time.time()

        # keep last price for close alert anchoring
        try:
            p = float(price)
            if p == p:  # not NaN
                self._last_price = p
        except Exception:
            pass
            
        dt = datetime.fromtimestamp(now_utc, tz=timezone.utc)
        bucket = self._get_bucket_index(dt)
        
        # New minute detection
        minute_start_ts = int(now_utc // 60) * 60
        if minute_start_ts != self.current_minute_start:
            # Finalize previous minute FIRST, but DO NOT early-return before rolling state.
            if self.current_minute_start > 0 and self.vol_so_far > 0:
                close_alert = self._compute_close_alert(now_utc=now_utc)
                if close_alert:
                    out.append(close_alert)

            # Roll state to the new minute and allow an immediate pace check
            self.current_minute_start = minute_start_ts
            self.vol_so_far = 0
            self.last_pace_check = 0.0
        
        self.vol_so_far += size
        
        # Throttle calculations
        if (now_utc - self.last_pace_check) < self.pace_throttle_sec:
            return out
        self.last_pace_check = now_utc

        # Get Baseline
        history = self.baselines.get(bucket, [])
        if not history:
            return out

        nonzero_hist = [int(v) for v in history if int(v) > 0]
        if not nonzero_hist:
            return out
        nonzero_hist.sort()
            
        baseline_median = float(statistics.median(nonzero_hist))
        if baseline_median <= 0:
            return out

        # --- Pace RVOL Calculation ---
        # Elapsed seconds in current minute (1..60)
        elapsed = now_utc - minute_start_ts
        elapsed = max(1.0, min(elapsed, 60.0))
        
        # Expected volume at this second = median * (elapsed / 60)
        expected = baseline_median * (elapsed / 60.0)
        if expected <= 0:
            pace_rvol = 0.0
        else:
            pace_rvol = self.vol_so_far / expected

        # Projection to full minute (very useful for scalpers)
        projected_full = int(round(self.vol_so_far * (60.0 / elapsed)))

        # Check Threshold
        if pace_rvol < self.threshold:
            return out

        # Check Alert Cooldown
        if (now_utc - self.last_alert_at_pace) < self.cooldown_sec:
            return out

        # --- Percentile Calculation ---
        percentile_now = self._percentile_rank(nonzero_hist, int(self.vol_so_far))
        percentile_proj = self._percentile_rank(nonzero_hist, int(projected_full))
        nonzero = len(nonzero_hist)

        self.last_alert_at_pace = now_utc
        
        out.append(RVOLAlert(
            symbol=self.active_symbol,
            price=float(price),
            volume=self.vol_so_far,
            baseline=baseline_median,
            rvol=pace_rvol,
            percentile=percentile_now,
            samples=len(nonzero_hist),
            nonzero=nonzero,
            pace=True,
            elapsed_sec=int(elapsed),
            time_str=dt.astimezone(ET).strftime("%H:%M:%S") + f" {TZ_LABEL}",
            projected_volume=projected_full,
            projected_percentile=percentile_proj,
        ))
        return out
    
    def _compute_close_alert(self, now_utc: Optional[float] = None) -> Optional[RVOLAlert]:
        """Compute RVOL at minute close."""
        if now_utc is None:
            now_utc = time.time()
        # Label close alerts at the end of the minute for clarity
        dt_end = datetime.fromtimestamp(self.current_minute_start + 59, tz=timezone.utc)
        dt = datetime.fromtimestamp(self.current_minute_start, tz=timezone.utc)
        bucket = self._get_bucket_index(dt)
        history = self.baselines.get(bucket, [])
        if not history:
            return None
        
        nonzero_hist = [int(v) for v in history if int(v) > 0]
        if not nonzero_hist:
            return None
        nonzero_hist.sort()
        baseline_median = float(statistics.median(nonzero_hist))
        if baseline_median <= 0:
            return None
        
        rvol = self.vol_so_far / baseline_median
        if rvol < self.threshold:
            return None
            
        if (now_utc - self.last_alert_at_close) < self.cooldown_sec:
            return None
        
        percentile = self._percentile_rank(nonzero_hist, int(self.vol_so_far))
        nonzero = len(nonzero_hist)
        
        self.last_alert_at_close = now_utc
        
        return RVOLAlert(
            symbol=self.active_symbol,
            price=float(self._last_price or 0.0),
            volume=self.vol_so_far,
            baseline=baseline_median,
            rvol=rvol,
            percentile=percentile,
            samples=len(nonzero_hist),
            nonzero=nonzero,
            pace=False,
            elapsed_sec=None,
            time_str=dt_end.astimezone(ET).strftime("%H:%M:%S") + f" {TZ_LABEL}"
        )
