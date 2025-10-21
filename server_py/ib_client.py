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

    def stop(self):
        log_debug("stop() called.")
        self._stop.set()
        # Create a task to perform async cleanup
        asyncio.create_task(self.unsubscribe())
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
        sym = symbol.strip().upper()
        log_debug(f"subscribe_symbol() called for '{sym}'. Current symbol: '{self._symbol}'.")
        if not sym:
            await self.unsubscribe()
            return

        if sym == self._symbol and self._ticker and self._contract:
            log_debug(f"Already subscribed to '{sym}', skipping.")
            return

        self._symbol = sym
        if not self.ib.isConnected():
            log_debug("Not connected to IB, subscription will be deferred until connection is established.")
            return
        
        await self._subscribe_symbol(self._symbol)

    async def unsubscribe(self):
        log_debug(f"unsubscribe() called. Current symbol: '{self._symbol}'")
        log_debug(f"  State before unsubscribe: contract={self._contract}, ticker is {'set' if self._ticker else 'None'}, quote_ticker is {'set' if self._quote_ticker else 'None'}")

        self._symbol = "" # Clear symbol immediately

        # 1. Detach event handlers
        if self._ticker:
            try: self._ticker.updateEvent.clear()
            except Exception as e: log_debug(f"Error clearing ticker updateEvent: {e}")
        if self._quote_ticker:
            try: self._quote_ticker.updateEvent.clear()
            except Exception as e: log_debug(f"Error clearing quote_ticker updateEvent: {e}")
        
        # 2. Cancel IB subscriptions if we have a contract
        if self._contract:
            contract_to_cancel = self._contract
            log_debug(f"Have a contract to cancel: conId={contract_to_cancel.conId}")
            
            if self._ticker:
                try:
                    log_debug(f"Attempting to cancel MktDepth for conId={contract_to_cancel.conId}")
                    self.ib.cancelMktDepth(contract_to_cancel)
                except Exception as e:
                    log_debug(f"Error on cancelMktDepth: {e}")

            if self._quote_ticker:
                try:
                    log_debug(f"Attempting to cancel MktData for conId={contract_to_cancel.conId}")
                    self.ib.cancelMktData(contract_to_cancel)
                except Exception as e:
                    log_debug(f"Error on cancelMktData: {e}")
        else:
            log_debug("No contract found, nothing to cancel on IB side.")

        # 3. Clear all local state
        self._ticker = None
        self._contract = None
        self._quote_ticker = None
        self._last_price, self._day_volume = None, None
        log_debug("Local state has been cleared.")


    async def _subscribe_symbol(self, symbol: str):
        log_debug(f"_subscribe_symbol (internal) starting for '{symbol}'.")
        try:
            # Cancel previous subscriptions first
            if self._ticker or self._quote_ticker or self._contract:
                await self.unsubscribe()
            
            # Restore the symbol since unsubscribe clears it
            self._symbol = symbol
            log_debug(f"Symbol restored to '{self._symbol}' after clearing previous subscription.")

            venue = "SMART" if self.cfg.smart_depth else "ISLAND"
            contract = Stock(symbol, venue, "USD")
            log_debug(f"Qualifying contract: {contract}")
            (qualified_contract,) = await self.ib.qualifyContractsAsync(contract)
            self._contract = qualified_contract
            log_debug(f"Contract QUALIFIED: conId={self._contract.conId}, symbol={self._contract.symbol}, exchange={self._contract.exchange}")

            # Request depth
            self._ticker = self.ib.reqMktDepth(
                self._contract, numRows=10, isSmartDepth=self.cfg.smart_depth
            )
            log_debug("Requested MktDepth. Ticker object created.")
            self._ticker.updateEvent += self._on_ticker_update

            # Request quote data
            self._quote_ticker = self.ib.reqMktData(self._contract, "", False, False)
            log_debug("Requested MktData. Quote Ticker created.")
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
        # Always log the raw error
        log_debug(f"RAW IB ERROR RECEIVED - reqId: {reqId}, code: {code}, msg: '{msg}'")
        # Ignore harmless chatter for application logic, but keep the log
        if code in {2104, 2106, 2158, 310}: # 310 = "Can't find subscribed market depth" on unsubscribe
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
        if lp is not None and util.isNan(lp) is False: self._last_price = float(lp)

        vol = getattr(ticker, "volume", None)
        if vol is not None and util.isNan(vol) is False: self._day_volume = int(vol)

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

