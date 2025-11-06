from __future__ import annotations
"""
Order Book Imbalance (OBI)
--------------------------
Pure, deterministic computation of a distance-weighted order book imbalance.

Range: [-1, +1]  (-1 ask-dominant .. +1 bid-dominant)

Definition for a snapshot t using up to the best 3 levels per side:
  w_i = exp(-alpha * i), i=1 for top of book
  OBI_t = (sum_i w_i * Q_bid[i] - sum_i w_i * Q_ask[i]) /
          (sum_i w_i * Q_bid[i] + sum_i w_i * Q_ask[i])

Edge cases:
  - Missing/negative/NaN quantities are treated as 0.
  - If denominator <= 0, returns 0.0.
  - The function clamps the final result to [-1, +1].
"""
from typing import List, Optional
import math

__all__ = ["compute_obi", "choose_alpha_heuristic"]

def _sanitize_levels(arr: List[float]) -> List[float]:
    out: List[float] = []
    for x in arr:
        try:
            f = float(x)
            if not math.isfinite(f) or f < 0:
                f = 0.0
        except Exception:
            f = 0.0
        out.append(f)
    return out

def choose_alpha_heuristic(qb: List[float], qa: List[float]) -> float:
    """
    Experience-based alpha selection when alpha is not provided.
    - Start at 0.5.
    - If L1 dominates deeper queues by >2x across sides, bump +0.1 (cap 0.8).
    - If deeper queues dominate L1 by >2x, reduce -0.1 (floor 0.3).
    (Churn/spread-awareness are not available at this layer; safe defaults.)
    """
    alpha = 0.5
    qb = _sanitize_levels(qb[:3])
    qa = _sanitize_levels(qa[:3])
    l1_total = (qb[0] if qb else 0.0) + (qa[0] if qa else 0.0)
    deeper_total = sum(qb[1:]) + sum(qa[1:])

    if l1_total > 0 and deeper_total > 0:
        if l1_total > 2.0 * deeper_total:
            alpha += 0.1
        elif deeper_total > 2.0 * l1_total:
            alpha -= 0.1
    elif l1_total > 0 and deeper_total == 0:
        alpha += 0.1
    elif deeper_total > 0 and l1_total == 0:
        alpha -= 0.1

    # clip to [0.3, 0.8]
    alpha = max(0.3, min(0.8, alpha))
    return alpha

def compute_obi(Q_bid: List[float], Q_ask: List[float], alpha: Optional[float] = None) -> float:
    """
    Pure OBI computation. Keeps no state, does no I/O.
    Parameters
    ----------
    Q_bid : list-like of non-negative numerics (top of book first)
    Q_ask : list-like of non-negative numerics (top of book first)
    alpha : optional float. If None, choose_alpha_heuristic is applied.

    Returns
    -------
    float in [-1.0, +1.0]
    """
    qb = _sanitize_levels(list(Q_bid))
    qa = _sanitize_levels(list(Q_ask))
    L = min(3, len(qb), len(qa))
    if L <= 0:
        return 0.0

    if alpha is None or not isinstance(alpha, (int, float)) or not math.isfinite(float(alpha)):
        a = choose_alpha_heuristic(qb[:L], qa[:L])
    else:
        a = float(alpha)
        if a <= 0.0:
            a = 1e-6  # avoid degenerate equal weights

    num = 0.0
    den = 0.0
    for i in range(1, L + 1):
        w = math.exp(-a * i)
        b = qb[i - 1]
        k = qa[i - 1]
        num += w * (b - k)
        den += w * (b + k)

    if den <= 0.0:
        return 0.0
    obi = num / den
    # guard numerical drift
    return max(-1.0, min(1.0, obi))

