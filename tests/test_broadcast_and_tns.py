import asyncio
import json
import pytest

from server_py.app import _classify_trade, _fmt_amount


def test_fmt_amount_and_classify():
    assert _fmt_amount(999.12) == ("999.12", False)
    assert _fmt_amount(1000.0) == ("1K", False)
    assert _fmt_amount(1500.0) == ("1.5K", False)
    assert _fmt_amount(1_000_000.0) == ("1 million", True)

    # price vs bid/ask classification
    assert _classify_trade(10.00, 10.00, 10.02)[0] == "at_bid"
    assert _classify_trade(10.02, 10.00, 10.02)[0] == "at_ask"
    assert _classify_trade(10.03, 10.00, 10.02)[0] == "above_ask"
    assert _classify_trade(9.99, 10.00, 10.02)[0] == "below_bid"
    # between: closer to ask
    assert _classify_trade(10.018, 10.00, 10.02)[0] in ("between_ask", "at_ask")


@pytest.mark.asyncio
async def test_broadcast_trade_filters_and_payload(app_module, capture_broadcast):
    # Set last seen bid/ask for classification
    await app_module.broadcast_quote(10.00, 10.02)

    # Configure thresholds
    app_module.state.set_symbol("TSLA")
    app_module.state.set_tape_thresholds(dollar=10_000, big_dollar=20_000)  # $10K filter, $20K big
    app_module.state.set_silent(False)

    # Below threshold → should DROP (no broadcast)
    initial_len = len(capture_broadcast)
    await app_module.broadcast_trade({"price": 10.00, "size": 999, "sym": "TSLA"})
    assert len(capture_broadcast) == initial_len, f"Expected no new messages after dropped trade; got {capture_broadcast}"

    # Above threshold (10.02 * 1000 = 10020)
    initial_len = len(capture_broadcast)  # Reset for next check
    await app_module.broadcast_trade({"price": 10.02, "size": 1000, "sym": "TSLA"})
    assert len(capture_broadcast) == initial_len + 1, f"Expected 1 new message; got {capture_broadcast}"
    msg = capture_broadcast[-1]
    assert msg["type"] == "trade" and msg["sym"] == "TSLA"
    assert msg["side"] in ("at_ask", "between_ask"), f"Unexpected side: {msg}"
    assert msg["amount"] == pytest.approx(10020.0)
    assert msg["amountStr"].endswith("K"), f"Human label for $ should be K style; got {msg['amountStr']}"
    assert msg["big"] is False, f"Should not be big (threshold 20K): {msg}"

    # Now a big print (>= $20K)
    initial_len = len(capture_broadcast)  # Reset for next check
    await app_module.broadcast_trade({"price": 10.50, "size": 2000, "sym": "TSLA"})  # $21,000
    assert len(capture_broadcast) == initial_len + 1, f"Expected 1 new message; got {capture_broadcast}"
    assert capture_broadcast[-1]["big"] is True, f"Expected big print True; got {capture_broadcast[-1]}"
