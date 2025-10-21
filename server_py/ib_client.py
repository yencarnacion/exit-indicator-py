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
    Wraps ib_insync to connect/reconnect to TWS/Gateway and maintain a single
    SMART market-depth subscription. Emits DOM snapshots via callbacks.
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
        self._stop = asyncio.Event()
        self._throttle_ms = 50
        self._last_emit_ms = 0
        self._last_price: Optional[float] = None
        self._day_volume: Optional[int] = None
        log_debug("IBDepthManager initialized.")

    async def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if not self.ib.isConnected():
                    log_debug("Not connected, attempting to connect...")
                    await self._connect_once()
                    backoff = 1.0
                await asyncio.sleep(0.5)
            except Exception as e:
                self._on_status(False)
                self._on_error(f"connect loop: {e}")
                log_debug(f"Connection error: {e}. Backing off for {backoff:.1f}s.")
                await asyncio.sleep(min(backoff, 30.0))
                backoff *= 2.0

    async def stop(self):
        log_debug("stop() called.")
        self._stop.set()
        await self.unsubscribe()
        try:
            if self.ib.isConnected():
                log_debug("Disconnecting from IB...")
                self.ib.disconnect()
        except Exception as e:
            log_debug(f"Error during IB disconnect: {e}")

    async def _connect_once(self):
        await self.ib.connectAsync(self.cfg.host, self.cfg.port, clientId=self.cfg.client_id, timeout=10.0)
        log_debug(f"Connected to IB on {self.cfg.host}:{self.cfg.port}")
        self.ib.reqMarketDataType(1)
        self._on_status(True)

        self.ib.pendingTickersEvent.clear()
        self.ib.pendingTickersEvent += self._on_pending_tickers
        self.ib.errorEvent.clear()
        self.ib.errorEvent += self._on_ib_error
        log_debug("Event handlers attached.")

        if self._symbol:
            log_debug(f"Re-subscribing to '{self._symbol}' after reconnect.")
            await self._subscribe_symbol(self._symbol)

    async def subscribe_symbol(self, symbol: str):
        log_debug(f"subscribe_symbol() called for '{symbol}'. Current symbol: '{self._symbol}'.")
        # First, always clean up any existing subscription.
        await self.unsubscribe()

        # Now, subscribe to the new symbol if one is provided.
        sym = symbol.strip().upper()
        if sym:
            self._symbol = sym
            if self.ib.isConnected():
                await self._subscribe_symbol(self._symbol)

    async def unsubscribe(self):
        log_debug(f"unsubscribe() called. Current symbol: '{self._symbol}'")
        contract_to_cancel = self._contract
        
        # Detach event handlers first to prevent callbacks during cancellation
        if self._ticker:
            try: self._ticker.updateEvent -= self._on_ticker_update
            except: pass
        if self._quote_ticker:
            try: self._quote_ticker.updateEvent -= self._on_quote_update
            except: pass

        # Cancel IB subscriptions if a contract exists.
        if contract_to_cancel:
            log_debug(f"Cancelling subscriptions for conId={contract_to_cancel.conId}")
            try: self.ib.cancelMktDepth(contract_to_cancel)
            except Exception as e: log_debug(f"Non-fatal error on cancelMktDepth: {e}")
            
            try: self.ib.cancelMktData(contract_to_cancel)
            except Exception as e: log_debug(f"Non-fatal error on cancelMktData: {e}")

            # *** THE CRUCIAL FIX ***
            # Give the IB Gateway a moment to process the cancellations.
            await asyncio.sleep(0.5)

        # Clear all local state
        self._symbol = ""
        self._ticker = None
        self._contract = None
        self._quote_ticker = None
        self._last_price, self._day_volume = None, None
        log_debug("Unsubscribe finished and local state cleared.")


    async def _subscribe_symbol(self, symbol: str):
        log_debug(f"_subscribe_symbol (internal) starting for '{self._symbol}'.")
        try:
            log_debug(f"Qualifying contract: Stock(symbol='{self._symbol}', exchange='SMART', currency='USD')")
            venue = "SMART" if self.cfg.smart_depth else "ISLAND"
            contract = Stock(self._symbol, venue, "USD")
            (qualified_contract,) = await self.ib.qualifyContractsAsync(contract)
            self._contract = qualified_contract
            log_debug(f"Contract QUALIFIED: {self._contract.conId}, {self._contract.symbol}")

            # Request depth
            self._ticker = self.ib.reqMktDepth(
                self._contract, numRows=10, isSmartDepth=self.cfg.smart_depth
            )
            log_debug(f"Requested MktDepth for {self._symbol}.")
            self._ticker.updateEvent += self._on_ticker_update

            # Request quote data
            self._quote_ticker = self.ib.reqMktData(self._contract, "", False, False)
            log_debug(f"Requested MktData for {self._symbol}.")
            self._quote_ticker.updateEvent += self._on_quote_update

        except Exception as e:
            log_debug(f"CRITICAL ERROR during _subscribe_symbol for '{symbol}': {e}")
            self._on_error(f"subscribe {symbol}: {e}")

    def _on_ticker_update(self, ticker: Ticker, *_):
        if ticker is not self._ticker: return
        now_ms = time.time() * 1000.0
        if now_ms - self._last_emit_ms < self._throttle_ms: return
        self._last_emit_ms = now_ms
        asks = self._convert_dom(ticker.domAsks, "ASK")
        bids = self._convert_dom(ticker.domBids, "BID")
        self._on_snapshot(self._symbol, asks, bids)

    def _on_ib_error(self, reqId, code, msg, contract):
        log_debug(f"RAW IB ERROR RECEIVED - reqId: {reqId}, code: {code}, msg: '{msg}'")
        if code in {2104, 2106, 2158, 310}:
            return
        self._on_error(f"Error {code}, reqId {reqId}: {msg}")

    def _on_pending_tickers(self, tickers: List[Ticker]):
        if not self._ticker or self._ticker not in tickers:
            return
        now_ms = time.time() * 1000.0
        if now_ms - self._last_emit_ms < self._throttle_ms: return
        self._last_emit_ms = now_ms
        asks = self._convert_dom(self._ticker.domAsks, "ASK")
        bids = self._convert_dom(self._ticker.domBids, "BID")
        self._on_snapshot(self._symbol, asks, bids)

    def _on_quote_update(self, ticker: Ticker, *_):
        if ticker is not self._quote_ticker: return
        
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
            except Exception: continue
            size = int(r.size or 0)
            venue = getattr(r, "mm", "") or "SMART"
            out.append(DepthLevel(side=side, price=price, size=size, venue=venue, level=i))
        return out

