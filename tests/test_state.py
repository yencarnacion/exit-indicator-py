from decimal import Decimal
from server_py.state import State


def test_state_defaults_and_setters():
    s = State(cooldown_seconds=1.5, default_threshold=20000)
    assert s.threshold == 20000, "Default threshold should come from config"
    assert s.set_side("bid") == "BID"
    assert s.set_side("whatever") == "ASK"
    s.set_threshold(0)
    assert s.threshold == 1, "Threshold must floor at 1"

    s.set_tape_thresholds(-5, -10)
    assert s.dollar_threshold == 0 and s.big_dollar_threshold == 0, "Dollar thresholds clamp at 0"

    s.set_silent("yes")
    assert s.silent is True
    s.set_silent("no")
    assert s.silent is False


def test_allow_alert_cooldown():
    s = State(cooldown_seconds=2.0, default_threshold=1000)
    sym = "AAPL"
    price = Decimal("123.45")
    assert s.allow_alert(sym, price, now=100.0) is True, "First alert must pass"
    assert s.allow_alert(sym, price, now=101.0) is False, "Cooldown blocks within 2s"
    assert s.allow_alert(sym, price, now=102.1) is True, "Cooldown expired"
    # different price should bypass cooldown key
    assert s.allow_alert(sym, Decimal("123.46"), now=101.0) is True
    # different symbol should bypass cooldown key
    assert s.allow_alert("MSFT", price, now=101.0) is True
