from __future__ import annotations
import asyncio
import time
from typing import Any
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional, Tuple, List
import os
from ib_async import IB, Stock, util, Contract, Ticker, DOMLevel
from ib_async.objects import TickByTickAllLast, TickByTickBidAsk
from .depth import DepthLevel

# --- Verbose logging (enable via EI_TNS_DEBUG=1 or EI_DEBUG=1) ---
DEBUG = (os.getenv("EI_TNS_DEBUG", "").strip().lower() in ("1","true","yes","on","debug") or
         os.getenv("EI_DEBUG", "").strip().lower() in ("1","true","yes","on","debug"))

def log_debug(msg: str):
    """Helper for timestamped debug logging."""
    if DEBUG:
        print(f"[DEBUG {time.time():.3f}] {msg}")

@dataclass
class IBConfig:
    host: str
    port: int
    client_id: int
    smart_depth: bool

class IBDepthManager:
    """
    Wraps ib_async to connect/reconnect to TWS/Gateway and maintain a single
    SMART market-depth subscription. Emits DOM snapshots via callbacks.
    """
    def __init__(
        self,
        cfg: IBConfig,
        on_status: Callable[[bool], None],
        on_snapshot: Callable[[str, List[DepthLevel], List[DepthLevel]], None],
        on_error: Callable[[str], None],
        on_tape_quote: Callable[[Optional[float], Optional[float]], None],
        on_tape_trade: Callable[[dict], None],
    ):
        self.cfg = cfg
        self.ib = IB()
        self._symbol: str = ""
        self._ticker: Optional[Ticker] = None
        self._quote_ticker: Optional[Ticker] = None
        self._contract: Optional[Contract] = None
        self._on_status = on_status
        self._on_snapshot = on_snapshot
        self._on_error = on_error
        self._on_tape_quote = on_tape_quote
        self._on_tape_trade = on_tape_trade
        self._stop_event = asyncio.Event()
        self._throttle_ms = 50
        self._last_emit_ms = 0
        self._last_price: Optional[float] = None
        self._day_volume: Optional[int] = None
        self._official_day_volume: Optional[int] = None
        self._tbt_since_official: int = 0
        # tick-by-tick subscription state
        self._tbt_task: Optional[asyncio.Task] = None
        self._tbt_index: int = 0  # per-subscription index for quote_ticker.tickByTicks
        # Keep most recent tick-by-tick bid/ask so trades can be classified accurately
        self._last_bid: Optional[float] = None
        self._last_ask: Optional[float] = None
        # --- micro VWAP (time-based window in seconds) ---
        self._micro_window_sec: float = 300.0  # default 5 minutes; UI can override via API if needed
        # store (ts, price, size) for proper volume-weighted computation
        self._micro_trades: List[Tuple[float, float, int]] = []
        log_debug("IBDepthManager initialized.")

    async def run(self):
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                if not self.ib.isConnected():
                    log_debug("Not connected, attempting to connect...")
                    await self._connect_once()
                    backoff = 1.0
                await asyncio.sleep(0.5)
            except Exception as e:
                self._on_status(False)
                self._on_error(f"Connect loop error: {e}")
                log_debug(f"Connection error: {e}. Backing off for {backoff:.1f}s.")
                await asyncio.sleep(min(backoff, 30.0))
                backoff *= 2.0

    async def stop(self):
        log_debug("stop() called.")
        self._stop_event.set()
        await self.unsubscribe()
        if self.ib.isConnected():
            log_debug("Disconnecting from IB...")
            self.ib.disconnect()

    async def _connect_once(self):
        await self.ib.connectAsync(self.cfg.host, self.cfg.port, clientId=self.cfg.client_id, timeout=10.0)
        log_debug(f"Connected to IB on {self.cfg.host}:{self.cfg.port}")
        self.ib.reqMarketDataType(1)
        self._on_status(True)

        self.ib.pendingTickersEvent.clear();   self.ib.pendingTickersEvent   += self._on_pending_tickers
        self.ib.errorEvent.clear();            self.ib.errorEvent            += self._on_ib_error
        # T&S is handled by the pump task; do not bind global tickByTick* events (avoids duplicates).
        log_debug("Event handlers attached (DOM + error).")

        if self._symbol:
            log_debug(f"Re-subscribing to '{self._symbol}' after reconnect.")
            await self._subscribe_symbol(self._symbol)

    async def subscribe_symbol(self, symbol: str):
        log_debug(f"subscribe_symbol() called for '{symbol}'. Current active: '{self._symbol}'.")
        # First, always clean up any existing subscription completely.
        await self.unsubscribe()

        # Now, subscribe to the new symbol if one is provided.
        sym = symbol.strip().upper()
        if sym:
            self._symbol = sym
            if self.ib.isConnected():
                await self._subscribe_symbol(self._symbol)
            else:
                log_debug("IB not connected yet; symbol staged for subscribe after connect.")

    async def unsubscribe(self):
        log_debug(f"unsubscribe() called. Cleaning up '{self._symbol}'.")
        
        # stop the pump task
        if self._tbt_task:
            try:
                self._tbt_task.cancel()
                await asyncio.sleep(0)  # let it cancel
            except Exception:
                pass
            self._tbt_task = None
        self._tbt_index = 0
        
        contract_to_cancel = self._contract
        ticker_to_cancel = self._ticker
        quote_ticker_to_cancel = self._quote_ticker

        self._symbol = ""
        self._contract = None
        self._ticker = None
        self._quote_ticker = None
        self._last_price, self._day_volume = None, None
        self._official_day_volume = None
        self._tbt_since_official = 0
        self._last_bid, self._last_ask = None, None
        # Reset micro VWAP state
        self._micro_trades.clear()
        # Detach quote callback from the old quote ticker (avoid leaks)
        if quote_ticker_to_cancel:
            try:
                quote_ticker_to_cancel.updateEvent -= self._on_quote_update
            except Exception:
                pass

        if contract_to_cancel:
            log_debug(f"Sending cancellation requests for conId={contract_to_cancel.conId} (smartDepth={self.cfg.smart_depth})")
            
            # *** THE CORRECT FIX APPLIED HERE ***
            # The isSmartDepth flag MUST match the original request.
            try: self.ib.cancelMktDepth(contract_to_cancel, isSmartDepth=self.cfg.smart_depth)
            except Exception as e: log_debug(f"Non-fatal error on cancelMktDepth: {e}")
            
            try: self.ib.cancelMktData(contract_to_cancel)
            except Exception as e: log_debug(f"Non-fatal error on cancelMktData: {e}")
            # tick-by-tick cancelations
            try:
                if contract_to_cancel:
                    self.ib.cancelTickByTickData(contract_to_cancel, "BidAsk")
            except Exception as e:
                log_debug(f"Non-fatal cancelTickByTickData(BidAsk): {e}")
            try:
                if contract_to_cancel:
                    self.ib.cancelTickByTickData(contract_to_cancel, "AllLast")
            except Exception as e:
                log_debug(f"Non-fatal cancelTickByTickData(AllLast): {e}")

            log_debug("Pausing for 0.5s to allow Gateway to process cancellations...")
            await asyncio.sleep(0.5)
        
        log_debug("Unsubscribe finished and local state cleared.")


    async def _subscribe_symbol(self, symbol: str):
        """Internal method to create a new subscription."""
        if not self._symbol:
            log_debug("Internal subscribe called with no symbol set. Aborting.")
            return

        log_debug(f"_subscribe_symbol (internal) starting for '{self._symbol}'.")
        try:
            log_debug(f"Qualifying contract: Stock(symbol='{self._symbol}', exchange='SMART', currency='USD')")
            venue = "SMART" if self.cfg.smart_depth else "ISLAND"
            contract = Stock(self._symbol, venue, "USD")
            (qualified_contract,) = await self.ib.qualifyContractsAsync(contract)
            self._contract = qualified_contract
            log_debug(f"Contract QUALIFIED: {self._contract.conId}, {self._contract.symbol}")

            self._ticker = self.ib.reqMktDepth(
                self._contract, numRows=10, isSmartDepth=self.cfg.smart_depth
            )
            log_debug(f"Created new MktDepth subscription for {self._symbol}.")

            # Request RTVolume (233) so IB publishes official day volume promptly
            # genericTickList="233" = RTVolume stream (includes cumulative day volume)
            self._quote_ticker = self.ib.reqMktData(
                self._contract, "233", False, False
            )
            log_debug(f"Created new MktData subscription for {self._symbol}.")
            self._quote_ticker.updateEvent += self._on_quote_update

            # --- Tick-by-tick subscriptions ---
            # BidAsk for live NBBO-like reference
            self.ib.reqTickByTickData(
                self._contract, "BidAsk", numberOfTicks=0, ignoreSize=False
            )
            # AllLast for prints (includes odd-lots & UTP where available)
            self.ib.reqTickByTickData(
                self._contract, "AllLast", numberOfTicks=0, ignoreSize=False
            )
            log_debug(f"TBT subscriptions set for {self._symbol}")
            # Start the pump task to drain tickByTicks
            self._tbt_index = len(self._quote_ticker.tickByTicks or [])
            if self._tbt_task:
                try: self._tbt_task.cancel()
                except: pass
            self._tbt_task = asyncio.create_task(self._pump_tbt())

            # --- Bootstrap micro VWAP from recent historical trades (non-blocking) ---
            asyncio.create_task(self._bootstrap_micro_vwap())

        except Exception as e:
            log_debug(f"CRITICAL ERROR during _subscribe_symbol for '{symbol}': {e}")
            self._on_error(f"Subscribe {symbol}: {e}")
            self._symbol = "" # Clear symbol on failure

    def _on_ib_error(self, reqId, code, msg, contract):
        log_debug(f"RAW IB ERROR RECEIVED - reqId: {reqId}, code: {code}, msg: '{msg}'")
        if code in {2104, 2106, 2158, 2152, 310, 2119}: 
            return
        self._on_error(f"Error {code}, reqId {reqId}: {msg}")

    def _on_pending_tickers(self, tickers: List[Ticker]):
        """This is the primary event handler for processing all market data updates."""
        now_ms = time.time() * 1000.0
        if DEBUG:
            try:
                log_debug(f"pendingTickers: {len(tickers)} items; sym='{self._symbol or ''}'")
            except Exception:
                pass
        # Check for quote updates first (keeps last/volume fresh for stats)
        if self._quote_ticker and self._quote_ticker in tickers:
            if DEBUG:
                n = len(self._quote_ticker.tickByTicks or [])
                log_debug(f"quote_ticker in batch; tickByTicks={n}")
            self._on_quote_update(self._quote_ticker, True)  # Force update
            # T&S is pump-driven; do not drain tickByTicks here.

        # Check for depth updates, with throttling
        if self._ticker and self._ticker in tickers:
            if now_ms - self._last_emit_ms < self._throttle_ms:
                if DEBUG:
                    log_debug("depth update throttled")
                return  # Throttle depth updates
            self._last_emit_ms = now_ms
            
            if self._symbol and self._symbol == self._ticker.contract.symbol:
                log_debug(f"Processing DOM for {self._symbol} via pendingTickersEvent")
                asks = self._convert_dom(self._ticker.domAsks, "ASK")
                bids = self._convert_dom(self._ticker.domBids, "BID")
                if DEBUG:
                    log_debug(f"DOM sizes: asks={len(asks)} bids={len(bids)}")
                self._on_snapshot(self._symbol, asks, bids)
                # T&S is event-driven; no draining here.
    
    # NOTE: ib_async event signatures have shifted across 2.x; accept *args defensively.
    def _on_quote_update(self, ticker: Ticker, *_: Any):
        if ticker is not self._quote_ticker: return
        
        if self._symbol and self._symbol == ticker.contract.symbol:
            lp = getattr(ticker, "last", None)
            if lp is not None and not util.isNan(lp): self._last_price = float(lp)

            # ---- Day volume tracking (official baseline + TBT deltas) ----
            # In ib_async 2.x, quote updates can arrive frequently while the
            # "official" volume field may lag. If we reset to a stale official
            # value every update, UI volume will flicker/revert.

            vol = None
            # Prefer RTVolume stream if present (genericTickList="233").
            # Some builds expose it as ticker.rtVolume.volume.
            try:
                rtv = getattr(ticker, "rtVolume", None)
                if rtv is not None:
                    vol = getattr(rtv, "volume", None)
            except Exception:
                vol = None

            # Fallback to ticker.volume if rtVolume isn't available/mapped.
            if vol is None:
                vol = getattr(ticker, "volume", None)

            if vol is not None and not util.isNan(vol):
                try:
                    v_int = int(vol)
                except Exception:
                    v_int = None

                if v_int is not None and v_int >= 0:
                    prev_off = int(self._official_day_volume or 0)

                    # Official baseline must never go backwards.
                    if self._official_day_volume is None:
                        self._official_day_volume = v_int
                    else:
                        self._official_day_volume = max(prev_off, v_int)

                    # Preserve any already-accumulated TBT delta.
                    # Keep day volume monotonic too.
                    cur_day = int(self._day_volume or 0)
                    base = int(self._official_day_volume or 0)
                    self._day_volume = max(cur_day, base)

                    # Ensure delta stays consistent after baseline changes.
                    self._tbt_since_official = max(0, int(self._day_volume) - base)

            if DEBUG:
                log_debug(f"quote update: last={self._last_price} volume={self._day_volume}")

    def current_quote(self) -> Tuple[Optional[float], Optional[int]]:
        return self._last_price, self._day_volume

    # --- micro VWAP helpers -------------------------------------------------

    def set_micro_window_minutes(self, minutes: float) -> None:
        """Optional: allow API to override micro VWAP window."""
        try:
            m = float(minutes)
        except Exception:
            return
        self._micro_window_sec = max(30.0, min(m * 60.0, 3600.0))  # clamp: 0.5–60 min
        # prune existing buffer to new window
        now = time.time()
        cutoff = now - self._micro_window_sec
        self._micro_trades = [(ts, p, sz) for (ts, p, sz) in self._micro_trades if ts >= cutoff]

    def _micro_vwap_and_sigma(self) -> Tuple[Optional[float], Optional[float]]:
        now = time.time()
        cutoff = now - self._micro_window_sec
        # keep only (price, size) pairs within window and with positive size
        pts = [(p, sz) for (ts, p, sz) in self._micro_trades if ts >= cutoff and sz > 0]
        # prune buffer
        self._micro_trades = [(ts, p, sz) for (ts, p, sz) in self._micro_trades if ts >= cutoff and sz > 0]
        if not pts:
            return None, None
        W   = float(sum(sz for (p, sz) in pts))           # Σ size
        if W <= 0:
            return None, None
        WP  = sum(p * sz for (p, sz) in pts)              # Σ price·size
        WP2 = sum((p * p) * sz for (p, sz) in pts)        # Σ price²·size
        vwap = WP / W
        # weighted (population) variance about VWAP
        var = max(0.0, (WP2 / W) - (vwap * vwap))
        sigma = var ** 0.5
        return vwap, sigma

    async def _bootstrap_micro_vwap(self):
        """
        Fetch a short slice of recent trades to initialize micro VWAP.
        Uses ib_async.reqHistoricalTicks if available; silent on errors.
        """
        if not (self.ib.isConnected() and self._contract and self._micro_window_sec > 0):
            return
        try:
            # reqHistoricalTicks rule: exactly ONE of startDateTime or endDateTime must be set.
            # Passing both as "" triggers IB error 10187.
            from datetime import datetime, timezone
            end = datetime.now(timezone.utc)
            # reqHistoricalTicks takes a tick-count, not a time delta. We request a modest
            # batch and then time-filter to the micro window.
            ticks = await self.ib.reqHistoricalTicksAsync(
                contract=self._contract,
                startDateTime="",
                endDateTime=end,
                numberOfTicks=1000,
                whatToShow="TRADES",
                useRth=False,
                ignoreSize=False,
            )
            now = time.time()
            cutoff = now - self._micro_window_sec
            self._micro_trades.clear()
            for t in ticks or []:
                # t is HistoricalTick or similar with attributes price, size, time
                try:
                    px = float(getattr(t, "price"))
                    sz = int(getattr(t, "size", 0) or 0)
                    ts = float(getattr(t, "time", now))
                except Exception:
                    continue
                if not util.isNan(px) and ts >= cutoff and sz > 0:
                    self._micro_trades.append((ts, px, sz))
        except Exception as e:
            log_debug(f"micro VWAP bootstrap failed: {e}")

    @staticmethod
    def _convert_dom(rows: List[DOMLevel], side: str) -> List[DepthLevel]:
        out: List[DepthLevel] = []
        for i, r in enumerate(rows or []):
            price_raw = getattr(r, "price", None)
            size_raw = getattr(r, "size", 0)
            # Validate price
            try:
                if isinstance(price_raw, Decimal):
                    price = price_raw
                else:
                    if price_raw is None:
                        continue
                    if isinstance(price_raw, float) and util.isNan(price_raw):
                        continue
                    price = Decimal(str(price_raw))
            except (InvalidOperation, ValueError, TypeError):
                continue
            if not price.is_finite() or price <= 0:
                continue
            # Validate size
            try:
                size = int(size_raw or 0)
            except (ValueError, TypeError):
                continue
            if size <= 0:
                continue
            venue = getattr(r, "mm", "") or "SMART"
            out.append(DepthLevel(side=side, price=price, size=size, venue=venue, level=i))
        return out

    # --- T&S: TBT pump task ---
    async def _pump_tbt(self):
        """Continuously drain tickByTicks from the quote ticker without relying on pendingTickers batches."""
        log_debug("TBT pump started.")
        try:
            while self._symbol and self._quote_ticker and self._contract and not self._stop_event.is_set():
                items = self._quote_ticker.tickByTicks or []
                n = len(items)
                start = self._tbt_index
                # If IB resets/rotates the internal list, fast-forward to avoid replays.
                if n < start:
                    if DEBUG:
                        log_debug(f"TBT pump: list shrank (n={n} < start={start}); fast-forwarding index.")
                    self._tbt_index = n
                    start = n
                if start < n:
                    if DEBUG:
                        log_debug(f"TBT pump: consuming {n-start} items (start={start}, n={n})")
                    for i in range(start, n):
                        t = items[i]
                        try:
                            if isinstance(t, TickByTickBidAsk):
                                bid = float(t.bidPrice) if t.bidPrice is not None and not util.isNan(t.bidPrice) else None
                                ask = float(t.askPrice) if t.askPrice is not None and not util.isNan(t.askPrice) else None
                                self._last_bid = bid
                                self._last_ask = ask
                                self._on_tape_quote(bid, ask)
                            elif isinstance(t, TickByTickAllLast):
                                price = float(t.price)
                                size  = int(t.size)
                                # only guard price for NaN; size is already an int
                                if util.isNan(price):
                                    continue
                                # Fast day-volume path: increment from TBT prints between official updates
                                base = int(self._official_day_volume or 0)
                                if size > 0:
                                    self._tbt_since_official += size

                                new_day = base + int(self._tbt_since_official or 0)
                                # Monotonic guard (shouldn't trigger, but prevents rare "snap back" cases)
                                if self._day_volume is None:
                                    self._day_volume = new_day
                                else:
                                    self._day_volume = max(int(self._day_volume), new_day)

                                # If we clamped for any reason, keep delta consistent
                                self._tbt_since_official = max(0, int(self._day_volume) - base)

                                self._last_price = price  # keep last fresh from prints too
                                # feed micro VWAP buffer
                                try:
                                    ts = float(getattr(t, "time", time.time()))
                                except Exception:
                                    ts = time.time()
                                if size > 0:
                                    self._micro_trades.append((ts, price, size))
                                self._on_tape_trade({
                                    "sym": self._symbol,
                                    "price": price,
                                    "size": size,
                                    "bid": self._last_bid,
                                    "ask": self._last_ask,
                                    "timeISO": None,
                                })
                        except Exception as e:
                            log_debug(f"TBT pump item error: {e}")
                    self._tbt_index = n
                # Adaptive sleep:
                # - when we just processed ticks (start < n): keep latency tight
                # - when idle: back off to save CPU
                await asyncio.sleep(0.005 if start < n else 0.02)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log_debug(f"TBT pump crashed: {e}")
        finally:
            log_debug("TBT pump stopped.")
