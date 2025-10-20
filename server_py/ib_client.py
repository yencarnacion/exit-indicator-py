from __future__ import annotations
import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional, Tuple, List
from ib_async import IB, Stock, util, Contract, Ticker, DOMLevel
from .depth import DepthLevel

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
        self._contract: Optional[Contract] = None
        self._on_status = on_status
        self._on_snapshot = on_snapshot
        self._on_error = on_error
        self._stop = asyncio.Event()
        self._throttle_ms = 50
        self._last_emit_ms = 0

    async def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                if not self.ib.isConnected():
                    await self._connect_once()
                    backoff = 1.0
                # idle
                await asyncio.sleep(0.5)
            except Exception as e:
                self._on_status(False)
                self._on_error(f"connect loop: {e}")
                await asyncio.sleep(min(backoff, 30.0))
                backoff *= 2.0

    def stop(self):
        self._stop.set()
        # Cancel any active subscriptions
        if self._ticker and self._contract:
            try:
                self.ib.cancelMktDepth(self._contract)
            except Exception:
                pass
        self._ticker = None
        self._contract = None
        self._symbol = ""
        # Disconnect from IB
        try:
            if self.ib.isConnected():
                self.ib.disconnect()
        except Exception:
            pass

    async def _connect_once(self):
        # ib_insync integrates with asyncio loop when using connectAsync
        await self.ib.connectAsync(self.cfg.host, self.cfg.port, clientId=self.cfg.client_id, timeout=10.0)
        # 1 = real-time, 2 = frozen, 3 = delayed, 4 = delayed-frozen
        try:
            self.ib.reqMarketDataType(1)
        except Exception:
            pass
        self._on_status(True)

        # (Re)attach event for DOM updates (idempotent)
        try:
            self.ib.pendingTickersEvent -= self._on_pending_tickers
        except Exception:
            pass
        self.ib.pendingTickersEvent += self._on_pending_tickers

        # (Re)attach error handler
        try:
            self.ib.errorEvent -= self._on_ib_error
        except Exception:
            pass
        self.ib.errorEvent += self._on_ib_error

        # If a symbol was already chosen, (re)subscribe
        if self._symbol:
            await self._subscribe_symbol(self._symbol)

    async def subscribe_symbol(self, symbol: str):
        sym = symbol.strip().upper()
        # Prevent empty symbol subscriptions
        if not sym:
            await self.unsubscribe()
            return
        # Skip resubscription if already subscribed to the same symbol
        if sym == self._symbol and self._ticker and self._contract:
            return
        self._symbol = sym
        if not self.ib.isConnected():
            return
        await self._subscribe_symbol(self._symbol)

    async def unsubscribe(self):
        """
        Cancel current market depth subscription and clear symbol.
        Only cancels if a subscription is active to avoid Error 318.
        """
        self._symbol = ""
        if self._ticker:
            try:
                self._ticker.updateEvent -= self._on_ticker_update
            except Exception:
                pass
        if self._ticker and self._contract:
            try:
                self.ib.cancelMktDepth(self._contract)
            except Exception:
                pass
        self._ticker = None
        self._contract = None

    async def _subscribe_symbol(self, symbol: str):
        try:
            # Cancel previous
            if self._ticker:
                try:
                    self._ticker.updateEvent -= self._on_ticker_update
                except Exception:
                    pass
            if self._ticker and self._contract:
                try:
                    self.ib.cancelMktDepth(self._contract)
                except Exception:
                    pass
            self._ticker = None
            self._contract = None

            # SMART when aggregating; single venue fallback otherwise
            venue = "SMART" if self.cfg.smart_depth else "ISLAND"
            contract = Stock(symbol, venue, "USD")
            (contract,) = await self.ib.qualifyContractsAsync(contract)
            # request top-10; aggregated when smart_depth==True
            self._ticker = self.ib.reqMktDepth(
                contract, numRows=10, isSmartDepth=self.cfg.smart_depth
            )
            self._contract = contract

            # Listen to updates on *this* ticker (most reliable in ib_async 2.x)
            try:
                self._ticker.updateEvent -= self._on_ticker_update
            except Exception:
                pass
            self._ticker.updateEvent += self._on_ticker_update
        except Exception as e:
            self._on_error(f"subscribe {symbol}: {e}")

    # --- event wiring ---

    def _on_ticker_update(self, ticker: Ticker, *_):
        if ticker is not self._ticker:
            return
        now_ms = util.now() * 1000.0
        if now_ms - self._last_emit_ms < self._throttle_ms:
            return
        self._last_emit_ms = now_ms
        asks = self._convert_dom(ticker.domAsks, "ASK")
        bids = self._convert_dom(ticker.domBids, "BID")
        try:
            self._on_snapshot(self._symbol, asks, bids)
        except Exception as e:
            self._on_error(f"snapshot emit: {e}")

    def _on_ib_error(self, *args):
        # Typical signature: (reqId, code, msg, advancedJson)
        try:
            code = args[1] if len(args) >= 2 else None
            msg  = args[2] if len(args) >= 3 else " ".join(map(str, args))
        except Exception:
            code, msg = None, str(args)
        # Ignore harmless chatter; DO NOT hide 10167 or 354/355 entitlement errors.
        if code in {2104, 2106, 2158, 310}:  # 310 = depth reset
            return
        self._on_error(f"IB error{'' if code is None else f' {code}'}: {msg}")

    def _on_pending_tickers(self, *args):
        """
        Works with ib_async 2.x (no args) and ib_insync-style (list arg).
        """
        try:
            tickers = (args[0] if args and isinstance(args[0], (list, tuple, set))
                       else self.ib.pendingTickers())
        except Exception:
            tickers = []

        if not self._ticker or self._ticker not in tickers:
            return
        # throttle emits
        now_ms = util.now() * 1000.0
        if now_ms - self._last_emit_ms < self._throttle_ms:
            return
        self._last_emit_ms = now_ms

        t = self._ticker
        asks = self._convert_dom(t.domAsks, "ASK")
        bids = self._convert_dom(t.domBids, "BID")
        try:
            self._on_snapshot(self._symbol, asks, bids)
        except Exception as e:
            self._on_error(f"snapshot emit: {e}")

    @staticmethod
    def _convert_dom(rows: List[DOMLevel], side: str) -> List[DepthLevel]:
        out: List[DepthLevel] = []
        for i, r in enumerate(rows or []):
            # DOMLevel: price, size, mm (marketMaker), ev, # etc.
            # ib_insync uses floats internally; convert to Decimal
            try:
                price = Decimal(str(r.price))
            except Exception:
                continue
            size = int(r.size or 0)
            venue = getattr(r, "mm", "") or getattr(r, "exchange", "") or "SMART"
            out.append(DepthLevel(side=side, price=price, size=size, venue=venue, level=i))
        return out
