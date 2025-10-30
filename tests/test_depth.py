from decimal import Decimal
from server_py.depth import DepthLevel, aggregate_both_top10, aggregate_top10
from server_py.state import State


def _mk(side, px, size, venue="SMART", level=0):
    return DepthLevel(side=side, price=Decimal(str(px)), size=int(size), venue=venue, level=level)


def test_aggregate_both_top10_basic():
    s = State(cooldown_seconds=0.0, default_threshold=5000)
    s.set_side("ASK")  # only ASK alerts will be emitted
    asks = [
        _mk("ASK", 100.00, 3000, level=0),
        _mk("ASK", 100.00, 2600, level=1),  # sums to 5600 -> alert
        _mk("ASK", 100.05, 1000, level=2),
    ]
    bids = [
        _mk("BID", 99.90, 8000, level=0),   # big bid agg, but state.side is ASK so no alert
        _mk("BID", 99.85, 200, level=1),
    ]

    ask_book, bid_book, alerts, best_ask, best_bid = aggregate_both_top10(s, asks, bids)

    assert ask_book[0].price == Decimal("100.00"), f"Best ask should be 100.00; got {ask_book[0].price}"
    assert ask_book[0].sumShares == 5600, f"Aggregated ask size wrong; got {ask_book[0].sumShares}"
    assert bid_book[0].price == Decimal("99.90"), "Best bid should be highest bid"
    assert best_ask == Decimal("100.00") and best_bid == Decimal("99.90")
    assert len(alerts) == 1 and alerts[0].sumShares == 5600 and alerts[0].side == "ASK", \
        f"Only ASK alert expected; got {alerts}"


def test_aggregate_top10_side_specific():
    s = State(cooldown_seconds=0.0, default_threshold=2000)
    s.set_side("BID")
    asks = [_mk("ASK", 10.00, 999)]
    bids = [
        _mk("BID", 9.99, 1200),
        _mk("BID", 9.99, 900),  # agg 2100 -> >= threshold so alert on BID side
    ]
    book, alerts = aggregate_top10(s, asks, bids)
    assert book[0].sumShares == 2100
    assert len(alerts) == 1 and alerts[0].side == "BID"


def test_aggregate_skips_non_positive_levels():
    s = State(cooldown_seconds=0.0, default_threshold=100)
    s.set_side("ASK")
    asks = [
        _mk("ASK", 0, 500),
        _mk("ASK", 101.50, 0),
        _mk("ASK", 101.75, -50),
        _mk("ASK", 101.80, 1000),
    ]
    bids = [
        _mk("BID", 101.60, 0),
        _mk("BID", 101.40, -200),
        _mk("BID", 101.20, 900),
    ]

    ask_book, bid_book, alerts, best_ask, best_bid = aggregate_both_top10(s, asks, bids)

    assert best_ask == Decimal("101.80")
    assert best_bid == Decimal("101.20")
    assert ask_book[0].sumShares == 1000
    assert bid_book[0].sumShares == 900
    assert alerts, "Valid level should still trigger alerts when threshold met"
