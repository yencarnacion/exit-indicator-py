from server_py.obi import compute_obi


def test_obi_balanced_zero():
    assert compute_obi([100, 100, 100], [100, 100, 100], alpha=0.5) == 0.0


def test_obi_bid_dominant_l1_only():
    # alpha=0.6 -> expected > 0.75 (sanity check from spec)
    val = compute_obi([100, 0, 0], [10, 0, 0], alpha=0.6)
    assert val > 0.75, f"Expected strong positive OBI; got {val}"


def test_obi_ask_dominant_deeper_levels():
    val = compute_obi([50, 20, 0], [50, 80, 0], alpha=0.4)
    assert val < 0.0, f"Expected negative OBI; got {val}"


def test_obi_zero_depth_and_nans():
    assert compute_obi([0, 0, 0], [0, 0, 0], alpha=0.5) == 0.0
    # negative and NaN-like inputs are coerced to 0
    val = compute_obi([100, -50, "NaN"], [100, 0, 0], alpha=0.5)
    assert abs(val) < 1e-9, f"Sanitized negatives/NaN should not skew; got {val}"


def test_obi_range_and_monotonicity():
    # Range clamp
    v1 = compute_obi([1e12, 0, 0], [0, 0, 0], alpha=0.5)
    v2 = compute_obi([0, 0, 0], [1e12, 0, 0], alpha=0.5)
    assert -1.0 <= v1 <= 1.0 and -1.0 <= v2 <= 1.0, f"OBI must be clamped: {v1}, {v2}"
    # Monotonic: increasing bid qty at L1 increases OBI
    a = compute_obi([10, 0, 0], [10, 0, 0], alpha=0.5)
    b = compute_obi([20, 0, 0], [10, 0, 0], alpha=0.5)
    assert b > a, f"OBI should increase as bid size increases at L1: {a} -> {b}"

