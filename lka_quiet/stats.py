"""Order statistics: medians, distribution-free intervals, spreads and quantiles.

Run times are not normal (they are bounded below and skewed by interference), so the board
summarises each cell by its median and bounds that median with the binomial order-statistic
interval, which holds for any continuous distribution.
"""

import math


def _sorted_nonempty(xs) -> list:
    values = sorted(xs)
    if not values:
        raise ValueError("no values")
    return values


def median(xs) -> float:
    values = _sorted_nonempty(xs)
    n = len(values)
    mid = n // 2
    if n % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def median_ci(xs, confidence: float = 0.95):
    """(lo, hi, coverage) with lo = x_(k), hi = x_(n-k+1) for the largest k whose coverage
    1 - 2 P(Bin(n, 1/2) <= k-1) still reaches `confidence`; None when even (min, max) falls short."""
    values = _sorted_nonempty(xs)
    n = len(values)
    tail = (1 - confidence) / 2
    denominator = 2**n
    cumulative = 0
    k = 0
    for i in range(n // 2 + 1):
        cumulative += math.comb(n, i)
        if cumulative / denominator <= tail:
            k = i + 1
        else:
            break
    if k == 0:
        return None
    below = sum(math.comb(n, i) for i in range(k))
    coverage = 1 - 2 * below / denominator
    return values[k - 1], values[n - k], coverage


def relative_halfwidth(xs, confidence: float = 0.95) -> float:
    """Half the median interval's width over the median; infinite when no interval exists."""
    interval = median_ci(xs, confidence)
    if interval is None:
        return math.inf
    lo, hi, _ = interval
    centre = median(xs)
    if centre == 0:
        return 0.0 if hi == lo else math.inf
    return (hi - lo) / 2 / centre


def spread(xs) -> float:
    """(max - min) / median."""
    values = _sorted_nonempty(xs)
    centre = median(values)
    if centre == 0:
        return 0.0 if values[-1] == values[0] else math.inf
    return (values[-1] - values[0]) / centre


def quantile_nearest_rank(xs, q: float):
    """The value at rank ceil(q n) (1-based), clamped to the sample."""
    values = _sorted_nonempty(xs)
    n = len(values)
    rank = math.ceil(q * n)
    return values[min(max(rank, 1), n) - 1]


def geomean(xs) -> float:
    values = list(xs)
    if not values:
        raise ValueError("no values")
    if any(v <= 0 for v in values):
        raise ValueError("geometric mean of a non-positive value")
    return math.exp(math.fsum(math.log(v) for v in values) / len(values))
