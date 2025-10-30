from pathlib import Path
import pytest

from server_py.config import Config


def test_config_valid_load():
    cfg = Config.load("./server_py/config.tws.yaml")
    assert cfg.port == 8086, "Expected default port 8086"
    assert cfg.levels_to_scan == 10, "levels_to_scan must be exactly 10"
    assert cfg.price_reference == "best_ask", "price_reference must be best_ask"


def test_config_invalid_levels(tmp_path: Path):
    p = tmp_path / "bad.yaml"
    p.write_text("levels_to_scan: 5\n", encoding="utf-8")
    with pytest.raises(ValueError) as e:
        Config.load(str(p))
    assert "levels_to_scan must be 10" in str(e.value)


def test_config_invalid_price_reference(tmp_path: Path):
    p = tmp_path / "bad2.yaml"
    p.write_text('price_reference: "best_bid"\n', encoding="utf-8")
    with pytest.raises(ValueError) as e:
        Config.load(str(p))
    assert 'price_reference must be "best_ask"' in str(e.value)


def test_config_invalid_default_threshold(tmp_path: Path):
    p = tmp_path / "bad3.yaml"
    p.write_text("default_threshold_shares: 0\n", encoding="utf-8")
    with pytest.raises(ValueError) as e:
        Config.load(str(p))
    assert "default_threshold_shares must be >= 1" in str(e.value)


def test_config_port_range(tmp_path: Path):
    p = tmp_path / "bad4.yaml"
    p.write_text("port: 70000\n", encoding="utf-8")
    with pytest.raises(ValueError) as e:
        Config.load(str(p))
    assert "invalid port" in str(e.value)
