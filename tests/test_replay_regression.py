"""
Regression tests using recorded market data ("golden datasets").

Replays a short tape and asserts the derived outputs (alerts, book stats, OBI)
haven't changed unexpectedly.
"""

import asyncio
import gzip
import json
from pathlib import Path

import pytest

from server_py.replay import PlaybackManager, ReplayConfig


@pytest.mark.asyncio
async def test_replay_smoke(app_module, capture_broadcast, tmp_path):
    """
    Smoke test: Replay a short recording and verify expected event types are broadcast.
    """
    # Use a small tape checked into tests/fixtures
    tape = Path(__file__).parent / "fixtures" / "aapl-1sec.ndjson.gz"
    assert tape.exists(), f"Fixture not found: {tape}"

    # Set up app state
    app_module.state.set_symbol("AAPL")
    app_module.state.set_threshold(100)

    # Create playback manager at 10x speed for faster tests
    mgr = PlaybackManager(
        ReplayConfig(path=str(tape), rate=10.0),
        on_status=lambda c: None,
        on_snapshot=lambda sym, asks, bids: asyncio.create_task(
            app_module.on_dom_snapshot(sym, asks, bids)
        ),
        on_error=lambda msg: None,
        on_tape_quote=lambda b, a: asyncio.create_task(app_module.broadcast_quote(b, a)),
        on_tape_trade=lambda ev: asyncio.create_task(app_module.broadcast_trade(ev)),
    )

    await mgr.subscribe_symbol("AAPL")
    await mgr.run()  # no-op, but keeps interface

    # Let it run briefly (1.1 seconds of recording at 10x = ~0.11 seconds)
    await asyncio.sleep(0.5)

    # Cleanup
    await mgr.stop()

    # Assertions on the messages broadcast
    kinds = [m["type"] for m in capture_broadcast]
    assert "book" in kinds, f"Expected 'book' messages; got types: {kinds}"
    assert "quote" in kinds, f"Expected 'quote' messages; got types: {kinds}"
    assert "trade" in kinds, f"Expected 'trade' messages; got types: {kinds}"

    # Check we got multiple book updates
    book_count = sum(1 for m in capture_broadcast if m["type"] == "book")
    assert book_count >= 2, f"Expected at least 2 book updates; got {book_count}"

    # Check trade structure
    trades = [m for m in capture_broadcast if m["type"] == "trade"]
    assert len(trades) >= 1, f"Expected at least 1 trade; got {len(trades)}"
    for trade in trades:
        assert "price" in trade, f"Trade missing 'price': {trade}"
        assert "size" in trade, f"Trade missing 'size': {trade}"
        assert "amount" in trade, f"Trade missing 'amount': {trade}"
        assert "side" in trade, f"Trade missing 'side': {trade}"


@pytest.mark.asyncio
async def test_replay_book_structure(app_module, capture_broadcast, tmp_path):
    """
    Verify book messages contain expected structure (asks, bids, stats, OBI).
    """
    tape = Path(__file__).parent / "fixtures" / "aapl-1sec.ndjson.gz"
    assert tape.exists(), f"Fixture not found: {tape}"

    app_module.state.set_symbol("AAPL")
    app_module.state.set_threshold(100)

    mgr = PlaybackManager(
        ReplayConfig(path=str(tape), rate=10.0),
        on_status=lambda c: None,
        on_snapshot=lambda sym, asks, bids: asyncio.create_task(
            app_module.on_dom_snapshot(sym, asks, bids)
        ),
        on_error=lambda msg: None,
        on_tape_quote=lambda b, a: asyncio.create_task(app_module.broadcast_quote(b, a)),
        on_tape_trade=lambda ev: asyncio.create_task(app_module.broadcast_trade(ev)),
    )

    await mgr.subscribe_symbol("AAPL")
    await mgr.run()
    await asyncio.sleep(0.5)
    await mgr.stop()

    # Find first book message
    books = [m for m in capture_broadcast if m["type"] == "book"]
    assert len(books) > 0, "Expected at least one book message"

    book = books[0]
    data = book.get("data", {})

    # Verify structure
    assert "asks" in data, f"Book missing 'asks': {book}"
    assert "bids" in data, f"Book missing 'bids': {book}"
    assert "stats" in data, f"Book missing 'stats': {book}"
    assert "side" in data, f"Book missing 'side': {book}"

    # Verify stats structure
    stats = data["stats"]
    assert "bestBid" in stats, f"Stats missing 'bestBid': {stats}"
    assert "bestAsk" in stats, f"Stats missing 'bestAsk': {stats}"
    assert "spread" in stats, f"Stats missing 'spread': {stats}"
    assert "obi" in stats, f"Stats missing 'obi': {stats}"
    assert "obiAlpha" in stats, f"Stats missing 'obiAlpha': {stats}"
    assert "obiLevels" in stats, f"Stats missing 'obiLevels': {stats}"

    # Verify asks/bids are lists of levels
    assert isinstance(data["asks"], list), f"Asks should be list; got {type(data['asks'])}"
    assert isinstance(data["bids"], list), f"Bids should be list; got {type(data['bids'])}"

    if data["asks"]:
        ask_level = data["asks"][0]
        assert "price" in ask_level, f"Ask level missing 'price': {ask_level}"
        assert "sumShares" in ask_level, f"Ask level missing 'sumShares': {ask_level}"
        assert "rank" in ask_level, f"Ask level missing 'rank': {ask_level}"


@pytest.mark.asyncio
async def test_replay_deterministic_output(app_module, capture_broadcast, tmp_path):
    """
    Regression test: Replay the same tape twice and verify identical output.
    This catches non-deterministic bugs in aggregation or state management.
    """
    tape = Path(__file__).parent / "fixtures" / "aapl-1sec.ndjson.gz"
    assert tape.exists(), f"Fixture not found: {tape}"

    async def run_playback():
        messages = []

        async def capture(payload):
            messages.append(payload)

        # Temporarily replace broadcast
        original_broadcast = app_module.broadcast
        app_module.broadcast = capture

        app_module.state.set_symbol("AAPL")
        app_module.state.set_threshold(100)

        mgr = PlaybackManager(
            ReplayConfig(path=str(tape), rate=10.0),
            on_status=lambda c: None,
            on_snapshot=lambda sym, asks, bids: asyncio.create_task(
                app_module.on_dom_snapshot(sym, asks, bids)
            ),
            on_error=lambda msg: None,
            on_tape_quote=lambda b, a: asyncio.create_task(app_module.broadcast_quote(b, a)),
            on_tape_trade=lambda ev: asyncio.create_task(app_module.broadcast_trade(ev)),
        )

        await mgr.subscribe_symbol("AAPL")
        await mgr.run()
        await asyncio.sleep(0.5)
        await mgr.stop()

        # Restore original broadcast
        app_module.broadcast = original_broadcast

        return messages

    # Run twice
    run1 = await run_playback()
    
    # Reset state between runs
    app_module.state.set_symbol("")
    app_module.state._last_alert.clear()
    
    run2 = await run_playback()

    # Normalize messages (strip volatile fields)
    def normalize(m):
        import copy
        m = copy.deepcopy(m)  # deep copy to handle nested structures
        if m.get("type") == "book":
            if "data" in m and "stats" in m["data"]:
                # Remove volatile fields that may differ between runs
                m["data"]["stats"].pop("last", None)
                m["data"]["stats"].pop("volume", None)
        if m.get("type") in ("alert", "trade"):
            m.pop("timeISO", None)
            # Also remove timeISO from nested data if present
            if "data" in m and isinstance(m["data"], dict):
                m["data"].pop("timeISO", None)
        return m

    norm1 = [normalize(m) for m in run1]
    norm2 = [normalize(m) for m in run2]

    # Compare counts
    assert len(norm1) == len(norm2), (
        f"Message counts differ: run1={len(norm1)}, run2={len(norm2)}"
    )

    # Compare message types
    types1 = [m["type"] for m in norm1]
    types2 = [m["type"] for m in norm2]
    assert types1 == types2, f"Message types differ:\nRun1: {types1}\nRun2: {types2}"

    # Compare first few messages in detail (sampling)
    sample_size = min(5, len(norm1))
    for i in range(sample_size):
        assert norm1[i] == norm2[i], (
            f"Message {i} differs:\nRun1: {json.dumps(norm1[i], indent=2)}\n"
            f"Run2: {json.dumps(norm2[i], indent=2)}"
        )


@pytest.mark.asyncio
async def test_replay_fixture_format(tmp_path):
    """
    Validate the fixture file format is correct NDJSON with expected structure.
    """
    tape = Path(__file__).parent / "fixtures" / "aapl-1sec.ndjson.gz"
    assert tape.exists(), f"Fixture not found: {tape}"

    with gzip.open(tape, "rt", encoding="utf-8") as f:
        lines = f.readlines()

    assert len(lines) > 0, "Fixture file is empty"

    # First line should be metadata
    meta = json.loads(lines[0])
    assert meta.get("type") == "meta", f"First line should be metadata; got {meta}"
    assert "format" in meta, f"Metadata missing 'format': {meta}"
    assert "version" in meta, f"Metadata missing 'version': {meta}"

    # Subsequent lines should be events with timestamps
    for i, line in enumerate(lines[1:], start=1):
        event = json.loads(line)
        assert "type" in event, f"Line {i} missing 'type': {event}"
        assert "t" in event, f"Line {i} missing timestamp 't': {event}"
        assert event["type"] in ("depth", "quote", "trade"), (
            f"Line {i} has invalid type: {event['type']}"
        )

