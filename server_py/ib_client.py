from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional, Tuple, List
from ib_async import IB, Stock, util, Contract, Ticker, DOMLevel
from .depth import DepthLevel

# --- SET TO TRUE TO ENABLE VERBOSE LOGGING ---
DEBUG = False

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
        # tick-by-tick handlers
        self._tbt_bidask_id: Optional[int] = None
        self._tbt_trades_id: Optional[int] = None
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

        self.ib.pendingTickersEvent.clear(); self.ib.pendingTickersEvent += self._on_pending_tickers
        self.ib.errorEvent.clear(); self.ib.errorEvent += self._on_ib_error
        log_debug("Event handlers attached.")

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

    async def unsubscribe(self):
        log_debug(f"unsubscribe() called. Cleaning up '{self._symbol}'.")
        
        contract_to_cancel = self._contract
        ticker_to_cancel = self._ticker
        quote_ticker_to_cancel = self._quote_ticker

        self._symbol = ""
        self._contract = None
        self._ticker = None
        self._quote_ticker = None
        self._last_price, self._day_volume = None, None
        self._tbt_bidask_id = None
        self._tbt_trades_id = None

        if quote_ticker_to_cancel:
            try: quote_ticker_to_cancel.updateEvent -= self._on_quote_update
            except: pass

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
                if self._tbt_bidask_id is not None:
                    self.ib.cancelTickByTickData(self._tbt_bidask_id)
            except Exception as e:
                log_debug(f"Non-fatal cancelTickByTickData(BidAsk): {e}")
            try:
                if self._tbt_trades_id is not None:
                    self.ib.cancelTickByTickData(self._tbt_trades_id)
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
            self._tbt_bidask_id = self.ib.reqTickByTickData(self._contract, "BidAsk", numberOfTicks=0, ignoreSize=False)
            self.ib.tickByTickBidAskEvent.clear()
            self.ib.tickByTickBidAskEvent += self._on_tbt_bidask
            # AllLast for prints (includes odd-lots & UTP where available)
            self._tbt_trades_id = self.ib.reqTickByTickData(self._contract, "AllLast", numberOfTicks=0, ignoreSize=False)
            self.ib.tickByTickAllLastEvent.clear()
            self.ib.tickByTickAllLastEvent += self._on_tbt_alllast

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
        # Check for quote updates first
        if self._quote_ticker and self._quote_ticker in tickers:
            self._on_quote_update(self._quote_ticker, True) # Force update

        # Check for depth updates, with throttling
        if self._ticker and self._ticker in tickers:
            if now_ms - self._last_emit_ms < self._throttle_ms:
                return  # Throttle depth updates
            self._last_emit_ms = now_ms
            
            if self._symbol and self._symbol == self._ticker.contract.symbol:
                log_debug(f"Processing DOM for {self._symbol} via pendingTickersEvent")
                asks = self._convert_dom(self._ticker.domAsks, "ASK")
                bids = self._convert_dom(self._ticker.domBids, "BID")
                self._on_snapshot(self._symbol, asks, bids)
    
    def _on_quote_update(self, ticker: Ticker, hasNewData: bool):
        if ticker is not self._quote_ticker: return
        
        if self._symbol and self._symbol == ticker.contract.symbol:
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

    # --- Tick-by-tick handlers ---
    def _on_tbt_bidask(self, reqId, time, bidPrice, askPrice, bidSize, askSize, tickAttribBidAsk):
        # Keep last seen for classification; broadcast a lightweight quote
        try:
            self._on_tape_quote(float(bidPrice) if bidPrice else None, float(askPrice) if askPrice else None)
        except Exception as e:
            log_debug(f"_on_tbt_bidask error: {e}")

    def _on_tbt_alllast(self, reqId, time, price, size, tickAttribLast, exchange, specialConditions):
        # Build TickSonic-like event and let app.py apply filtering
        try:
            # Access NBBO-ish bid/ask from last tick-by-tick callback is not cached by ib_async,
            # so we rely on app-side state (State) to filter/format. Here, pass raw data.
            ev = {
                "sym": self._symbol,
                "price": float(price),
                "size": int(size),
                # app will compute bid/ask/spread classification from its own latest quote
                "bid": None,
                "ask": None,
                "timeISO": None,
            }
            self._on_tape_trade(ev)
        except Exception as e:
            log_debug(f"_on_tbt_alllast error: {e}")

