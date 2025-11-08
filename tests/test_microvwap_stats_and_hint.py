# tests/test_microvwap_stats_and_hint.py

import pytest
from decimal import Decimal

from server_py.depth import AggregatedLevel
from server_py import app as app_module


@pytest.mark.asyncio
async def test_broadcast_book_full_includes_micro_fields_and_hint(monkeypatch, capture_broadcast):
    """
    broadcast_book_full should emit microVWAP, microSigma, microBandK, and actionHint.
    The hint must be one of the supported tokens or None.
    """
    # Arrange: mock manager micro VWAP helper and band_k
    def fake_micro():
        # microVWAP = 100, sigma = 1
        return 100.0, 1.0

    monkeypatch.setattr(app_module, "manager", app_module.manager, raising=False)
    setattr(app_module.manager, "_micro_vwap_and_sigma", fake_micro)
    setattr(app_module.manager, "_micro_band_k", 2.0)

    # Construct minimal books: price clearly above upper band to force a hint
    # microVWAP=100, sigma=1, k=2 => bands [98, 102]
    asks = [AggregatedLevel(price=Decimal("105.00"), sumShares=1000, rank=0)]
    bids = [AggregatedLevel(price=Decimal("95.00"), sumShares=1000, rank=0)]

    # Patch broadcast to capture the payload
    async def fake_broadcast(payload: dict):
        capture_broadcast.append(payload)

    monkeypatch.setattr(app_module, "broadcast", fake_broadcast)

    # Act
    await app_module.broadcast_book_full(
        asks=asks,
        bids=bids,
        best_ask=asks[0].price,
        best_bid=bids[0].price,
        last=105.00,        # above upper band
        volume=123456,
        obi=0.0,            # neutral OBI → should prefer fade_short_ok
        obi_alpha=None,
        obi_levels=None,
    )

    # Assert
    assert capture_broadcast, "No broadcast captured from broadcast_book_full"
    msg = capture_broadcast[-1]
    assert msg["type"] == "book", f"Expected book type; got {msg}"
    data = msg.get("data", {})
    stats = data.get("stats", {})

    # micro fields present
    assert "microVWAP" in stats, f"microVWAP missing: {stats}"
    assert "microSigma" in stats, f"microSigma missing: {stats}"
    assert "microBandK" in stats, f"microBandK missing: {stats}"
    # values as per fake
    assert stats["microVWAP"] == 100.0
    assert stats["microSigma"] == 1.0
    assert stats["microBandK"] == 2.0

    # actionHint should be one of the allowed or None
    hint = stats.get("actionHint")
    allowed = {None, "long_ok", "fade_short_ok", "trend_up", "trend_down"}
    assert hint in allowed, f"Unexpected actionHint: {hint}"

    # In this setup (price >> upper band, obi ~ 0), we expect fade_short_ok
    assert hint == "fade_short_ok", f"Expected fade_short_ok; got {hint}"


@pytest.mark.asyncio
async def test_action_hint_long_ok_scenario(monkeypatch, capture_broadcast):
    """
    Scenario: price below lower band, OBI not strongly negative => 'long_ok'.
    """
    def fake_micro():
        # microVWAP=50, sigma=0.5 -> bands [49,51]
        return 50.0, 0.5

    setattr(app_module.manager, "_micro_vwap_and_sigma", fake_micro)
    setattr(app_module.manager, "_micro_band_k", 2.0)

    asks = [AggregatedLevel(price=Decimal("48.00"), sumShares=1000, rank=0)]
    bids = [AggregatedLevel(price=Decimal("47.90"), sumShares=1000, rank=0)]

    async def fake_broadcast(payload: dict):
        capture_broadcast.append(payload)

    monkeypatch.setattr(app_module, "broadcast", fake_broadcast)

    await app_module.broadcast_book_full(
        asks=asks,
        bids=bids,
        best_ask=asks[0].price,
        best_bid=bids[0].price,
        last=48.00,        # below 49 lower band
        volume=999,
        obi=-0.05,         # mildly negative but > -0.1 threshold
        obi_alpha=None,
        obi_levels=None,
    )

    msg = capture_broadcast[-1]
    stats = msg["data"]["stats"]
    assert stats.get("actionHint") == "long_ok", f"Expected long_ok; got {stats.get('actionHint')}"


@pytest.mark.asyncio
async def test_action_hint_trend_up_and_down(monkeypatch, capture_broadcast):
    """
    Scenario checks mapping to 'trend_up' and 'trend_down'.
    """
    def fake_micro():
        return 100.0, 1.0  # bands [98,102] with k=2

    setattr(app_module.manager, "_micro_vwap_and_sigma", fake_micro)
    setattr(app_module.manager, "_micro_band_k", 2.0)

    async def fake_broadcast(payload: dict):
        capture_broadcast.append(payload)

    monkeypatch.setattr(app_module, "broadcast", fake_broadcast)

    # Trend up: above band with strong positive OBI
    capture_broadcast.clear()
    await app_module.broadcast_book_full(
        asks=[AggregatedLevel(price=Decimal("105.0"), sumShares=500, rank=0)],
        bids=[AggregatedLevel(price=Decimal("104.9"), sumShares=2000, rank=0)],
        best_ask=Decimal("105.0"),
        best_bid=Decimal("104.9"),
        last=105.0,
        volume=1_000_000,
        obi=0.4,
        obi_alpha=None,
        obi_levels=None,
    )
    stats = capture_broadcast[-1]["data"]["stats"]
    assert stats.get("actionHint") == "trend_up", f"Expected trend_up; got {stats.get('actionHint')}"

    # Trend down: below band with strong negative OBI
    capture_broadcast.clear()
    await app_module.broadcast_book_full(
        asks=[AggregatedLevel(price=Decimal("95.1"), sumShares=2000, rank=0)],
        bids=[AggregatedLevel(price=Decimal("95.0"), sumShares=500, rank=0)],
        best_ask=Decimal("95.1"),
        best_bid=Decimal("95.0"),
        last=95.0,
        volume=1_000_000,
        obi=-0.4,
        obi_alpha=None,
        obi_levels=None,
    )
    stats = capture_broadcast[-1]["data"]["stats"]
    assert stats.get("actionHint") == "trend_down", f"Expected trend_down; got {stats.get('actionHint')}"

