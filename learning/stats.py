"""Small, dependency-free statistics for the causal readout.

No scipy/numpy -- just the closed forms we need, so the learning package stays importable
anywhere the engine runs. Everything here is a pure function over counts.
"""

import math

Z_95 = 1.959963984540054  # standard normal quantile for a two-sided 95% interval


def _phi(x: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def wilson_interval(k: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion k/n. Chosen over the normal
    (Wald) interval because it stays inside [0, 1] and behaves at small n / extreme
    rates -- exactly the regime an early experiment lives in. Returns (lo, hi)."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def newcombe_diff_ci(
    k1: int, n1: int, k2: int, n2: int, z: float = Z_95
) -> tuple[float, float]:
    """Confidence interval for the DIFFERENCE p1 - p2 (Newcombe's method 10, built from the
    two Wilson intervals). This is the interval on the lift (R_treatment - R_control); if it
    excludes 0 the lift is significant. Robust at small/zero counts where a normal-approx
    difference interval misbehaves."""
    p1 = k1 / n1 if n1 else 0.0
    p2 = k2 / n2 if n2 else 0.0
    l1, u1 = wilson_interval(k1, n1, z)
    l2, u2 = wilson_interval(k2, n2, z)
    lower = (p1 - p2) - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2)
    upper = (p1 - p2) + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2)
    return (max(-1.0, lower), min(1.0, upper))


def two_proportion_p(k1: int, n1: int, k2: int, n2: int) -> float:
    """Two-sided p-value for H0: p1 == p2 (pooled two-proportion z-test). Returns 1.0 when
    either arm is empty or the pooled rate is degenerate (no evidence either way)."""
    if n1 == 0 or n2 == 0:
        return 1.0
    p1, p2 = k1 / n1, k2 / n2
    p_pool = (k1 + k2) / (n1 + n2)
    if p_pool in (0.0, 1.0):
        return 1.0
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = (p1 - p2) / se
    return 2.0 * (1.0 - _phi(abs(z)))
