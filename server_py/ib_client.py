from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
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
        # tick-by-tick subscription state
        self._tbt_task: Optional[asyncio.Task] = None
        self._tbt_index: int = 0  # per-subscription index for quote_ticker.tickByTicks
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

            self._quote_ticker = self.ib.reqMktData(self._contract, "", False, False)
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
    
    def _on_quote_update(self, ticker: Ticker, hasNewData: bool):
        if ticker is not self._quote_ticker: return
        
        if self._symbol and self._symbol == ticker.contract.symbol:
            lp = getattr(ticker, "last", None)
            if lp is not None and not util.isNan(lp): self._last_price = float(lp)

            vol = getattr(ticker, "volume", None)
            if vol is not None and not util.isNan(vol): self._day_volume = int(vol)
            if DEBUG:
                log_debug(f"quote update: last={self._last_price} volume={self._day_volume}")

    def current_quote(self) -> Tuple[Optional[float], Optional[int]]:
        return self._last_price, self._day_volume

    @staticmethod
    def _convert_dom(rows: List[DOMLevel], side: str) -> List[DepthLevel]:
        out: List[DepthLevel] = []
        for i, r in enumerate(rows or []):
            try: price = Decimal(str(r.price))
            except: continue
            size = int(r.size or 0)
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
                                self._on_tape_quote(bid, ask)
                            elif isinstance(t, TickByTickAllLast):
                                price = float(t.price)
                                size  = int(t.size)
                                # only guard price for NaN; size is already an int
                                if util.isNan(price):
                                    continue
                                self._on_tape_trade({
                                    "sym": self._symbol,
                                    "price": price,
                                    "size": size,
                                    "bid": None, "ask": None, "timeISO": None,
                                })
                        except Exception as e:
                            log_debug(f"TBT pump item error: {e}")
                    self._tbt_index = n
                # short sleep keeps latency low but avoids a hot loop
                await asyncio.sleep(0.05)  # 50 ms
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log_debug(f"TBT pump crashed: {e}")
        finally:
            log_debug("TBT pump stopped.")

