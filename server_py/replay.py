# server_py/replay.py

from __future__ import annotations

import asyncio, contextlib, gzip, json

from dataclasses import dataclass

from decimal import Decimal

from typing import Callable, Optional, List

from .depth import DepthLevel



@dataclass

class ReplayConfig:

    path: str

    rate: float = 1.0        # 2.0 = 2x faster, 0.5 = half speed

    loop: bool = False       # loop the file



class PlaybackManager:

    """

    Drop-in replacement for IBDepthManager for offline runs.

    Replays a recorded NDJSON stream by calling the same callbacks.

    """

    def __init__(

        self,

        cfg: ReplayConfig,

        on_status: Callable[[bool], None],

        on_snapshot: Callable[[str, List[DepthLevel], List[DepthLevel]], None],

        on_error: Callable[[str], None],

        on_tape_quote: Callable[[Optional[float], Optional[float]], None],

        on_tape_trade: Callable[[dict], None],

    ):

        self.cfg = cfg

        self._on_status = on_status

        self._on_snapshot = on_snapshot

        self._on_error = on_error

        self._on_tape_quote = on_tape_quote

        self._on_tape_trade = on_tape_trade

        self._stop = asyncio.Event()

        self._task: Optional[asyncio.Task] = None

        self._symbol: str = ""

        self._last: Optional[float] = None

        self._vol: Optional[int] = None



    async def run(self):

        # no-op; kept for symmetry with IBDepthManager

        await asyncio.sleep(0)



    async def stop(self):

        self._stop.set()

        if self._task:

            self._task.cancel()

            with contextlib.suppress(Exception):

                await self._task



    async def subscribe_symbol(self, sym: str):

        await self.unsubscribe()

        self._symbol = (sym or "").upper()

        self._stop.clear()

        self._task = asyncio.create_task(self._play())



    async def unsubscribe(self):

        if self._task:

            self._task.cancel()

            with contextlib.suppress(Exception):

                await self._task

        self._task = None

        self._symbol = ""



    def current_quote(self):

        return self._last, self._vol



    async def _play(self):

        import contextlib, time

        self._on_status(True)

        try:

            while not self._stop.is_set():

                with gzip.open(self.cfg.path, "rt", encoding="utf-8") as fh:

                    prev_t = 0

                    for line in fh:

                        if self._stop.is_set():

                            break

                        evt = json.loads(line)

                        if evt.get("type") == "meta":

                            prev_t = 0

                            continue

                        t = int(evt.get("t", 0))

                        delta_ms = max(0, t - prev_t)

                        prev_t = t

                        # timing

                        await asyncio.sleep((delta_ms / max(1e-9, self.cfg.rate)) / 1000.0)

                        # dispatch

                        typ = evt["type"]

                        if typ == "depth":

                            def dec(side, rows):

                                return [DepthLevel(

                                    side=side, price=Decimal(str(r["p"])),

                                    size=int(r["s"]), venue=r.get("v","SMART"), level=int(r["l"])

                                ) for r in rows]

                            asks = dec("ASK", evt["asks"]); bids = dec("BID", evt["bids"])

                            self._on_snapshot(self._symbol or evt.get("sym",""), asks, bids)

                        elif typ == "quote":

                            bid, ask = evt.get("bid"), evt.get("ask")

                            self._on_tape_quote(bid, ask)

                        elif typ == "trade":

                            # Update last/volume so book stats and hints are meaningful in replay

                            try:

                                px = float(evt.get("price"))

                                sz = int(evt.get("size") or 0)

                                if px == px:  # not NaN

                                    self._last = px

                                if sz > 0:

                                    self._vol = (self._vol or 0) + sz

                            except Exception:

                                pass

                            self._on_tape_trade(evt)

                if not self.cfg.loop:

                    break

        except asyncio.CancelledError:

            pass

        except Exception as e:

            self._on_error(f"Replay error: {e}")

        finally:

            self._on_status(False)

