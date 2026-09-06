import math

import pytest

from autor3search_python import stats


@pytest.mark.parametrize(
    "n,expected",
    [(2, 0.3333333333), (3, 0.1), (4, 0.0285714286), (5, 0.0079365079)],
)
def test_min_achievable_p_matches_the_known_floors(n, expected):
    """These four values are what pin the count >= 4 rule and the unreachable-alpha warning."""
    assert stats.min_achievable_p(n, n) == pytest.approx(expected, rel=1e-6)


def test_min_achievable_p_is_capped_at_one():
    assert stats.min_achievable_p(1, 1) == 1.0
    assert stats.min_achievable_p(0, 5) == 1.0


def test_count_for_alpha():
    assert stats.count_for_alpha(0.05) == 4
    assert stats.count_for_alpha(0.05 / 7) == 6
    assert stats.count_for_alpha(0.0) == 0


def test_binom():
    assert stats.binom(10, 5) == 252
    assert stats.binom(5, 0) == 1
    assert stats.binom(5, 6) == 0
    assert stats.binom(5, -1) == 0


def test_identical_samples_give_p_of_one():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert stats.mann_whitney_u(xs, list(xs)) == pytest.approx(1.0)


def test_completely_separated_samples_hit_the_floor():
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = [10.0, 11.0, 12.0, 13.0, 14.0]
    assert stats.mann_whitney_u(a, b) == pytest.approx(stats.min_achievable_p(5, 5), rel=1e-9)


def test_known_exact_p_value():
    """Textbook case: n1=n2=4, no overlap -> two-sided p = 2/C(8,4) = 0.02857."""
    assert stats.mann_whitney_u([1, 2, 3, 4], [5, 6, 7, 8]) == pytest.approx(0.0285714, rel=1e-5)


def test_symmetric_in_its_arguments():
    a = [1.0, 4.0, 6.0, 9.0, 2.0]
    b = [3.0, 5.0, 7.0, 8.0, 10.0]
    assert stats.mann_whitney_u(a, b) == pytest.approx(stats.mann_whitney_u(b, a))


def test_handles_ties_without_blowing_up():
    a = [1.0, 1.0, 1.0, 2.0, 2.0]
    b = [1.0, 2.0, 2.0, 2.0, 3.0]
    p = stats.mann_whitney_u(a, b)
    assert 0.0 <= p <= 1.0


def test_large_samples_use_the_normal_approximation_and_stay_sane():
    a = [float(i) for i in range(60)]
    b = [float(i) + 30 for i in range(60)]
    p = stats.mann_whitney_u(a, b)
    assert 0.0 <= p < 0.001


@pytest.mark.parametrize("bad", [([], [1.0, 2.0]), ([1.0], [2.0])])
def test_rejects_samples_too_small_to_compare(bad):
    with pytest.raises(ValueError):
        stats.mann_whitney_u(*bad)


def test_median_ci_is_unbounded_below_six_observations():
    """benchmath's own rule, surfaced rather than swallowed: at 95% you need 6 per side."""
    assert stats.median_ci([1.0, 2.0, 3.0, 4.0, 5.0]) is None


def test_median_ci_brackets_the_median_at_six():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert stats.median_ci(xs) == (1.0, 6.0)


@pytest.mark.parametrize("n", [6, 7, 10, 20])
def test_median_ci_actually_covers_at_least_95_percent(n):
    """Independently recomputes the binomial coverage of the returned interval.

    This is the test that would have caught the off-by-one: bracketing the
    median is not enough, the interval has to actually cover 95% of the mass.
    """
    xs = [float(i) for i in range(1, n + 1)]
    lo, hi = stats.median_ci(xs)
    k = xs.index(lo) + 1  # 1-indexed order statistic
    total = 2.0**n
    coverage = sum(stats.binom(n, i) for i in range(k, n - k + 1)) / total
    assert coverage >= 0.95


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ([1.0, 1.0, 2.0], [3.0, 4.0, 4.0]),
        ([1.0, 1.0, 2.0, 2.0], [3.0, 3.0, 4.0, 4.0]),
    ],
)
def test_tied_maximally_separated_samples_are_clamped_to_the_exact_floor(a, b):
    """A single duplicate timing must not make a KEEP reachable the exact test forbids."""
    floor = stats.min_achievable_p(len(a), len(b))
    assert stats.mann_whitney_u(a, b) == pytest.approx(floor)


def test_u_counts_returns_an_immutable_tuple():
    """Cached: a mutable list would let one caller poison every later comparison."""
    assert isinstance(stats._u_counts(3, 3), tuple)


def test_geomean():
    assert stats.geomean([1.0, 1.0]) == pytest.approx(1.0)
    assert stats.geomean([0.5, 2.0]) == pytest.approx(1.0)
    assert stats.geomean([0.25, 0.25]) == pytest.approx(0.25)
    assert stats.geomean([2.0, 8.0]) == pytest.approx(4.0)


@pytest.mark.parametrize("bad", [[], [1.0, 0.0], [1.0, -1.0], [math.nan]])
def test_geomean_rejects_non_positive_and_empty(bad):
    with pytest.raises(ValueError):
        stats.geomean(bad)
