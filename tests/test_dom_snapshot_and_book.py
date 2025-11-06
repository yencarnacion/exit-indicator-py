from decimal import Decimal
import pytest

from server_py.depth import DepthLevel
from server_py.state import State


def _mk(side, px, size, level):
    return DepthLevel(side=side, price=Decimal(str(px)), size=int(size), venue="SMART", level=level)


@pytest.mark.asyncio
async def test_on_dom_snapshot_broadcasts_full_book(app_module, capture_broadcast):
    # Prepare state and dummy manager quote
    s = app_module.state
    s.set_symbol("AAPL")
    s.set_side("ASK")
    s.set_threshold(5000)
    # make the dummy manager expose a current quote
    app_module.manager._last = 123.45
    app_module.manager._vol = 987654

    asks = [
        _mk("ASK", 100.00, 2600, 0),
        _mk("ASK", 100.00, 2600, 1),  # agg 5200 -> alert on ASK side
        _mk("ASK", 100.02, 100,  2),
    ]
    bids = [
        _mk("BID",  99.98, 800,  0),
        _mk("BID",  99.99, 1200, 1),
    ]
    await app_module.on_dom_snapshot("AAPL", asks, bids)

    # Expect one "book" broadcast and one "alert" broadcast
    kinds = [m["type"] for m in capture_broadcast]
    assert "book" in kinds, f"No book broadcast: {capture_broadcast}"
    assert "alert" in kinds, f"No alert broadcast: {capture_broadcast}"

    book_msg = next(m for m in capture_broadcast if m["type"] == "book")
    data = book_msg["data"]
    assert len(data["asks"]) >= 1 and len(data["bids"]) >= 1
    assert data["stats"]["bestAsk"] == pytest.approx(100.00)
    assert data["stats"]["bestBid"] == pytest.approx(99.99)
    assert data["stats"]["last"] == pytest.approx(123.45)
    assert data["stats"]["volume"] == 987654
    # OBI is computed over top levels and should be ask-dominant (negative) here
    obi = data["stats"].get("obi", None)
    assert obi is not None and -1.0 <= obi <= 1.0, f"OBI missing or out of range: {data['stats']}"
    assert obi < 0, f"Expected ask-dominant OBI (<0); got {obi}"
