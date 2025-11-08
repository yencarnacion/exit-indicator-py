# tests/test_microvwap_api.py

import pytest


@pytest.mark.asyncio
async def test_microvwap_endpoint_sets_manager_fields(app_module, client):
    """
    /api/microvwap should clamp inputs and update manager's micro window and band_k.
    """
    # Ensure manager has the helper
    assert hasattr(app_module.manager, "set_micro_window_minutes"), \
        "Dummy/real manager missing set_micro_window_minutes"

    # Send out-of-range values to test clamping (minutes, band_k)
    body = {"minutes": 0.1, "band_k": 10.0}
    r = client.post("/api/microvwap", json=body)
    assert r.status_code == 200, f"/api/microvwap failed: {r.text}"
    out = r.json()
    # Clamp: minutes >=0.5, band_k <=4.0
    assert 0.5 <= out["minutes"] <= 60.0, f"Minutes not clamped correctly: {out}"
    assert 0.5 <= out["band_k"] <= 4.0, f"band_k not clamped correctly: {out}"

    # Server should stash band_k on manager for later use
    mk = getattr(app_module.manager, "_micro_band_k", None)
    assert mk is not None, "Manager did not store _micro_band_k"
    assert 0.5 <= float(mk) <= 4.0, f"_micro_band_k out of expected range: {mk}"


@pytest.mark.asyncio
async def test_microvwap_endpoint_in_start_flow(app_module, client):
    """
    Starting a subscription should allow a follow-up /api/microvwap
    to update parameters without errors.
    """
    # Start with some symbol to mimic normal usage
    resp = client.post("/api/start", json={
        "symbol": "AAPL",
        "threshold": 5000,
        "side": "ASK",
        "dollar": 0,
        "bigDollar": 0,
        "silent": True,
    })
    assert resp.status_code == 200, f"/api/start failed: {resp.text}"
    j = resp.json()
    assert j["ok"] is True and j["symbol"] == "AAPL"

    # Now update microVWAP config
    r = client.post("/api/microvwap", json={"minutes": 5, "band_k": 2})
    assert r.status_code == 200, f"/api/microvwap after start failed: {r.text}"
    out = r.json()
    assert out["ok"] is True
    assert 0.5 <= out["minutes"] <= 60
    assert 0.5 <= out["band_k"] <= 4.0

