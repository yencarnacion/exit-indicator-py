from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional, Tuple, List
from ib_async import IB, Stock, util, Contract, Ticker, DOMLevel
from .depth import DepthLevel

# --- SET TO TRUE TO ENABLE VERBOSE LOGGING ---
DEBUG = True

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
    Wraps ib_async to connect/reconnect and reuse a single, persistent
    market-depth subscription to prevent race conditions and quota errors.
    """
    def __init__(
        self,
        cfg: IBConfig,
        on_status: Callable[[bool], None],
        on_snapshot: Callable[[str, List[DepthLevel], List[DepthLevel]], None],
        on_error: Callable[[str], None],
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
        self._stop_event = asyncio.Event()
        self._throttle_ms = 50
        self._last_emit_ms = 0
        self._last_price: Optional[float] = None
        self._day_volume: Optional[int] = None
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
        if self.ib.isConnected():
            log_debug("Performing final cancellation of subscriptions.")
            # On final shutdown, we truly cancel the persistent tickers
            if self._ticker: self.ib.cancelMktDepth(self._ticker.contract)
            if self._quote_ticker: self.ib.cancelMktData(self._quote_ticker.contract)
            await asyncio.sleep(0.5) # Give gateway time to process
            log_debug("Disconnecting from IB...")
            self.ib.disconnect()

    async def _connect_once(self):
        await self.ib.connectAsync(self.cfg.host, self.cfg.port, clientId=self.cfg.client_id, timeout=10.0)
        log_debug(f"Connected to IB on {self.cfg.host}:{self.cfg.port}")
        self.ib.reqMarketDataType(1)
        self._on_status(True)

        # Create persistent ticker objects ONCE.
        self._ticker = Ticker()
        self._quote_ticker = Ticker()
        log_debug("Persistent Ticker objects created.")

        self.ib.pendingTickersEvent.clear(); self.ib.pendingTickersEvent += self._on_pending_tickers
        self.ib.errorEvent.clear(); self.ib.errorEvent += self._on_ib_error
        self._ticker.updateEvent.clear(); self._ticker.updateEvent += self._on_ticker_update
        self._quote_ticker.updateEvent.clear(); self._quote_ticker.updateEvent += self._on_quote_update
        log_debug("Event handlers attached.")

        if self._symbol:
            log_debug(f"Re-subscribing to '{self._symbol}' after reconnect.")
            await self.subscribe_symbol(self._symbol)

    async def subscribe_symbol(self, symbol: str):
        sym = symbol.strip().upper()
        if not sym:
            # If stopping, we just clear the local symbol but keep tickers alive.
            log_debug("Empty symbol provided. Clearing active symbol.")
            self._symbol = ""
            self._contract = None
            return

        if sym == self._symbol and self._contract:
            log_debug(f"Already subscribed to {sym}. Ignoring.")
            return

        self._symbol = sym
        log_debug(f"subscribe_symbol() called for '{self._symbol}'.")

        if not self.ib.isConnected() or not self._ticker:
            log_debug("Not connected or tickers not ready. Will subscribe on connect.")
            return

        try:
            log_debug(f"Qualifying contract for {self._symbol}")
            venue = "SMART" if self.cfg.smart_depth else "ISLAND"
            new_contract = Stock(self._symbol, venue, "USD")
            (qualified_contract,) = await self.ib.qualifyContractsAsync(new_contract)
            self._contract = qualified_contract
            log_debug(f"Contract QUALIFIED: {self._contract.conId}, {self._contract.symbol}")

            # REUSE the existing ticker objects by passing the new contract.
            # This MODIFIES the subscription instead of creating a new one.
            self.ib.reqMktDepth(self._contract, numRows=10, isSmartDepth=self.cfg.smart_depth, ticker=self._ticker)
            log_debug(f"Modified MktDepth subscription for {self._symbol}.")
            
            self.ib.reqMktData(self._contract, "", False, False, ticker=self._quote_ticker)
            log_debug(f"Modified MktData subscription for {self._symbol}.")

        except Exception as e:
            log_debug(f"CRITICAL ERROR during subscribe_symbol for '{self._symbol}': {e}")
            self._on_error(f"Subscribe {self._symbol}: {e}")
            self._symbol = "" # Clear symbol on failure

    async def unsubscribe(self):
        # This now simply clears the active symbol. The ticker objects persist.
        log_debug("unsubscribe() called, clearing active symbol.")
        self._symbol = ""
        self._contract = None
        # Optionally send a blank update to clear the UI immediately
        self._on_snapshot("", [], [])
        await asyncio.sleep(0) # Yield control

    def _on_ticker_update(self, ticker: Ticker, hasNewData: bool):
        # hasNewData is True for the first update, then False for subsequent ones
        if not hasNewData or ticker is not self._ticker: return
        now_ms = time.time() * 1000.0
        if now_ms - self._last_emit_ms < self._throttle_ms: return
        self._last_emit_ms = now_ms

        if self._symbol == ticker.contract.symbol:
            asks = self._convert_dom(ticker.domAsks, "ASK")
            bids = self._convert_dom(ticker.domBids, "BID")
            self._on_snapshot(self._symbol, asks, bids)

    def _on_ib_error(self, reqId, code, msg, contract):
        log_debug(f"RAW IB ERROR RECEIVED - reqId: {reqId}, code: {code}, msg: '{msg}'")
        if code in {2104, 2106, 2158, 2152}: return # Ignore informational messages
        # We now expect 310 on shutdown, which is OK. We won't get it during normal operation.
        if code == 310 and self._stop_event.is_set(): return
        self._on_error(f"Error {code}, reqId {reqId}: {msg}")

    def _on_pending_tickers(self, tickers: List[Ticker]):
        # This event is less reliable for updates, we rely on ticker.updateEvent instead.
        # However, we can use it as a fallback trigger.
        for t in tickers:
            if t is self._ticker:
                self._on_ticker_update(t, True)
            if t is self._quote_ticker:
                self._on_quote_update(t, True)
    
    def _on_quote_update(self, ticker: Ticker, hasNewData: bool):
        if not hasNewData or ticker is not self._quote_ticker: return
        if self._symbol == ticker.contract.symbol:
            lp = getattr(ticker, "last", None)
            if lp is not None and not util.isNan(lp): self._last_price = float(lp)
            vol = getattr(ticker, "volume", None)
            if vol is not None and not util.isNan(vol): self._day_volume = int(vol)

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

