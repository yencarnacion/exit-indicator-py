import json
import yaml


def test_health_and_config(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    cfg = client.get("/api/config").json()
    # Presence & types (soundAvailable may be False if file missing)
    for key in ("defaultThresholdShares", "currentThresholdShares", "cooldownSeconds",
                "levelsToScan", "priceReference", "smartDepth",
                "soundAvailable", "currentSide", "silent", "dollarThreshold", "bigDollarThreshold"):
        assert key in cfg, f"/api/config missing '{key}': {cfg}"


def test_threshold_and_side_mutations(client):
    bad = client.post("/api/threshold", json={"threshold": 0})
    assert bad.status_code == 400 and "threshold must be >=1" in bad.text

    good = client.post("/api/threshold", json={"threshold": 12345}).json()
    assert good["ok"] is True
    st = client.get("/api/config").json()
    assert st["currentThresholdShares"] == 12345, f"Threshold not stored: {st}"

    s = client.post("/api/side", json={"side": "BID"}).json()
    assert s["ok"] is True and s["side"] == "BID"
    st = client.get("/api/config").json()
    assert st["currentSide"] == "BID"


def test_start_stop_and_silent(client, app_module):
    # Start with full parameters (symbol, threshold, side, dollar filters, silent)
    body = {
        "symbol": "aapl",
        "threshold": 5000,
        "side": "BID",
        "dollar": 10000,
        "bigDollar": 50000,
        "silent": True
    }
    resp = client.post("/api/start", json=body).json()
    assert resp["ok"] is True, f"Start failed: {resp}"
    assert resp["symbol"] == "AAPL" and resp["side"] == "BID" and resp["threshold"] == 5000

    cfg = client.get("/api/config").json()
    assert cfg["dollarThreshold"] == 10000 and cfg["bigDollarThreshold"] == 50000 and cfg["silent"] is True, \
        f"Start didn't set T&S thresholds or silent: {cfg}"

    # Toggle silent via dedicated endpoint
    s2 = client.post("/api/silent", json={"silent": False}).json()
    assert s2["ok"] is True
    cfg2 = client.get("/api/config").json()
    assert cfg2["silent"] is False

    # Stop cleans up (no error); symbol reset is reflected in /ws status, but not returned by /stop
    stop = client.post("/api/stop").json()
    assert stop["ok"] is True


def test_yaml_endpoints_normalize(client, set_config_dir):
    # watchlist.yaml
    (set_config_dir / "watchlist.yaml").write_text(
        "watchlist:\n  - symbol: 'AAPL'\n  - symbol: 'MSFT'\n",
        encoding="utf-8"
    )
    # thresholds.yaml (root 'thresholds' -> normalized to 'watchlist')
    (set_config_dir / "thresholds.yaml").write_text(
        "thresholds:\n  - threshold: 5000\n  - threshold: 10000\n  - threshold: 20000\n",
        encoding="utf-8"
    )
    # dollar-value.yaml (root 'dollarvalue' -> normalized to 'watchlist')
    (set_config_dir / "dollar-value.yaml").write_text(
        "dollarvalue:\n  - label: '$10'\n    threshold: 1000\n    big_threshold: 10000\n",
        encoding="utf-8"
    )

    wl = client.get("/api/yaml/watchlist").text
    th = client.get("/api/yaml/thresholds").text
    dv = client.get("/api/yaml/dollar-values").text

    wl_data = yaml.safe_load(wl)
    th_data = yaml.safe_load(th)
    dv_data = yaml.safe_load(dv)

    assert isinstance(wl_data.get("watchlist"), list) and wl_data["watchlist"][0]["symbol"] == "AAPL", \
        f"Watchlist YAML wrong: {wl_data}"
    assert isinstance(th_data.get("watchlist"), list) and th_data["watchlist"][1]["threshold"] == 10000, \
        f"Threshold YAML normalization failed: {th_data}"
    assert isinstance(dv_data.get("watchlist"), list) and dv_data["watchlist"][0]["label"] == "$10", \
        f"Dollar values YAML normalization failed: {dv_data}"
