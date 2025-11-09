# server_py/recording.py

from __future__ import annotations

import asyncio, gzip, json, time

from dataclasses import dataclass

from decimal import Decimal

from typing import Optional, List

from .depth import DepthLevel



def _now_ms(t0: float) -> int:

    return int((time.monotonic() - t0) * 1000)



class NDJSONRecorder:

    """

    Stream a compact, compressed NDJSON ('*.ndjson.gz') file:

      - meta header (first line)

      - {t:ms, type:'depth'|'quote'|'trade', ...}

    Uses an async writer so callbacks stay non-blocking.

    """

    def __init__(self, path: str, meta: dict):

        self.path = path

        self.meta = {"format": "ei.ndjson", "version": 1, **meta}

        self._q: asyncio.Queue[str | None] = asyncio.Queue()

        self._t0 = time.monotonic()

        self._task = asyncio.create_task(self._writer())



    async def _writer(self):

        with gzip.open(self.path, "wt", encoding="utf-8") as fh:

            fh.write(json.dumps({"type": "meta", **self.meta}) + "\n")

            while True:

                line = await self._q.get()

                if line is None:

                    # Mark sentinel as done so join() can complete

                    self._q.task_done()

                    break

                fh.write(line + "\n")

                self._q.task_done()



    async def close(self):

        await self._q.put(None)

        await self._q.join()



    def _enqueue(self, obj: dict):

        obj["t"] = _now_ms(self._t0)

        self._q.put_nowait(json.dumps(obj, separators=(",", ":")))



    # --- capture helpers called from app.py ---

    def record_depth(self, symbol: str, asks: List[DepthLevel], bids: List[DepthLevel]):

        def enc(side: str, rows: List[DepthLevel]):

            return [{"p": str(r.price), "s": int(r.size), "l": int(r.level), "v": r.venue} for r in rows[:10]]

        self._enqueue({"type":"depth","sym":symbol,"asks":enc("ASK", asks),"bids":enc("BID", bids)})



    def record_quote(self, bid: Optional[float], ask: Optional[float]):

        self._enqueue({"type":"quote","bid": bid, "ask": ask})



    def record_trade(self, ev: dict):

        self._enqueue({

            "type":"trade", "sym": ev.get("sym",""),

            "price": float(ev.get("price", 0.0)), "size": int(ev.get("size", 0)),

            # keep any timeISO from upstream if you ever add it

            "timeISO": ev.get("timeISO")

        })

