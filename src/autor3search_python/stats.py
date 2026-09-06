"""The statistics behind a verdict.

The Go original gets these from golang.org/x/perf/benchmath. Implementing them
here keeps the harness free of a scipy dependency, and — more usefully — keeps
the exact small-sample behavior under this project's own tests, since small
samples are the entire operating regime (`count` defaults to 10 per side).
"""

from __future__ import annotations

import functools
import math
import statistics
from collections.abc import Sequence

# Below this many observations per side, the distribution-free confidence
# interval for a median at 95% confidence is unbounded: there is no pair of
# order statistics whose coverage reaches the level. Reporting an interval
# anyway would dress up a number the sample cannot support.
MIN_CI_SAMPLES = 6

# Above this sample size the exact U distribution is both slow to build and
# unnecessary — the normal approximation is accurate well before it.
_EXACT_LIMIT = 30


def binom(n: int, k: int) -> float:
    """C(n, k) as a float, multiplying and dividing in step to stay near the result."""
    if k < 0 or k > n:
        return 0.0
    k = min(k, n - k)
    c = 1.0
    for i in range(k):
        c = c * (n - i) / (i + 1)
    return c


def min_achievable_p(n1: int, n2: int) -> float:
    """The smallest two-sided p the U test can return for these sample sizes.

    Two maximally separated samples still only reach 2/C(n1+n2, n1), because
    that is the fraction of orderings at least as extreme as the observed one.
    This floor is why `count` has a hard minimum, and why a run comparing many
    benchmarks against a Bonferroni-corrected alpha can be incapable of a KEEP
    before it starts.
    """
    if n1 < 1 or n2 < 1:
        return 1.0
    c = binom(n1 + n2, n1)
    if c == 0:
        return 1.0
    return min(2.0 / c, 1.0)


def count_for_alpha(alpha: float) -> int:
    """Smallest rounds-per-side at which the U test can produce p < alpha, else 0."""
    for n in range(2, 51):  # far past any sensible benchmark budget
        if min_achievable_p(n, n) < alpha:
            return n
    return 0


@functools.cache
def _u_counts(n1: int, n2: int) -> tuple[float, ...]:
    """counts[u] = number of arrangements with Mann-Whitney statistic exactly u.

    Cached and returned as a tuple rather than a list: a mutable result shared
    across every call with these sizes could be poisoned by one careless caller.
    """
    size = n1 * n2 + 1
    memo: dict[tuple[int, int, int], float] = {}

    def f(a: int, b: int, u: int) -> float:
        if u < 0:
            return 0.0
        if a == 0 or b == 0:
            return 1.0 if u == 0 else 0.0
        key = (a, b, u)
        hit = memo.get(key)
        if hit is not None:
            return hit
        out = f(a - 1, b, u - b) + f(a, b - 1, u)
        memo[key] = out
        return out

    return tuple(f(n1, n2, u) for u in range(size))


def _exact_two_sided_p(u: float, n1: int, n2: int) -> float:
    counts = _u_counts(n1, n2)
    total = sum(counts)
    if total == 0:
        return 1.0
    mean = n1 * n2 / 2.0
    # Two-sided: mass at least as far from the mean as the observed statistic.
    distance = abs(u - mean)
    tol = 1e-9
    mass = sum(c for i, c in enumerate(counts) if abs(i - mean) >= distance - tol)
    return min(mass / total, 1.0)


def _normal_two_sided_p(u: float, n1: int, n2: int, tie_groups: Sequence[int]) -> float:
    floor = min_achievable_p(n1, n2)
    mean = n1 * n2 / 2.0
    n = n1 + n2
    tie_term = sum(t**3 - t for t in tie_groups)
    var = (n1 * n2 / 12.0) * ((n + 1) - tie_term / (n * (n - 1)))
    if var <= 0:
        return 1.0
    z = (abs(u - mean) - 0.5) / math.sqrt(var)  # continuity correction
    if z <= 0:
        return 1.0
    p = min(math.erfc(z / math.sqrt(2)), 1.0)
    # The untied exact floor is a lower bound on the true tied-permutation floor
    # (ties only ever remove distinct arrangements, never add them), so clamping
    # up to it here can only ever refuse a KEEP the exact test would have
    # granted anyway — never grant one the exact test would have refused.
    return max(p, floor)


def _ranks(values: Sequence[float]) -> tuple[list[float], list[int]]:
    """Midranks, plus the size of each tie group."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    groups: list[int] = []
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        midrank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = midrank
        groups.append(j - i + 1)
        i = j + 1
    return ranks, groups


def mann_whitney_u(x: Sequence[float], y: Sequence[float]) -> float:
    """Two-sided Mann-Whitney U p-value for the null that x and y are drawn alike.

    Exact for small samples (the regime here); normal approximation with tie
    correction above `_EXACT_LIMIT` per side.
    """
    n1, n2 = len(x), len(y)
    if n1 < 2 or n2 < 2:
        raise ValueError(f"need at least 2 observations per side, got {n1}/{n2}")
    combined = [*x, *y]
    ranks, groups = _ranks(combined)
    r1 = sum(ranks[:n1])
    u1 = r1 - n1 * (n1 + 1) / 2.0
    has_ties = any(g > 1 for g in groups)
    if n1 <= _EXACT_LIMIT and n2 <= _EXACT_LIMIT and not has_ties:
        return _exact_two_sided_p(u1, n1, n2)
    return _normal_two_sided_p(u1, n1, n2, groups)


def median(xs: Sequence[float]) -> float:
    if not xs:
        raise ValueError("median of an empty sample")
    return statistics.median(xs)


def median_ci(xs: Sequence[float], confidence: float = 0.95) -> tuple[float, float] | None:
    """Distribution-free confidence interval for the median.

    Returns None when no pair of order statistics covers `confidence` — which
    at 95% means fewer than MIN_CI_SAMPLES observations. That is not a failure
    to report; it is the honest answer, and callers surface it as a warning.
    """
    n = len(xs)
    if n < MIN_CI_SAMPLES:
        return None
    ordered = sorted(xs)
    total = 2.0**n
    # mass is the coverage of the 1-indexed interval [X_(k), X_(n+1-k)], i.e.
    # order statistics k and n-k+1. In 0-indexed terms that is
    # ordered[k - 1], ordered[n - k] — narrower by one on each side than
    # ordered[k], ordered[n - 1 - k], which is what the interval covers if k
    # is (wrongly) treated as a 0-indexed position.
    for k in range(n // 2, 0, -1):
        mass = sum(binom(n, i) for i in range(k, n - k + 1)) / total
        if mass >= confidence:
            return ordered[k - 1], ordered[n - k]
    return None


def geomean(ratios: Sequence[float]) -> float:
    """Geometric mean — the single score an experiment is judged on."""
    if not ratios:
        raise ValueError("geomean of an empty set")
    total = 0.0
    for r in ratios:
        if not math.isfinite(r) or r <= 0:
            raise ValueError(f"non-positive or non-finite ratio {r!r}")
        total += math.log(r)
    return math.exp(total / len(ratios))
