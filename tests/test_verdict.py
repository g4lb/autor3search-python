import pytest

from autor3search_python import verdict
from autor3search_python.benchio import Delta


def delta(name="x", pct=0.0, p=1.0, alpha=0.05, n=10, warnings=()):
    ratio = 1 + pct / 100
    return Delta(
        name=name,
        base_center=1.0,
        cand_center=ratio,
        ratio=ratio,
        pct_change=pct,
        p=p,
        alpha=alpha,
        significant=p < alpha,
        n_base=n,
        n_cand=n,
        warnings=tuple(warnings),
    )


def decide(deltas, score, max_regress=5.0, min_effect=1.0):
    return verdict.decide(deltas, score, max_regress, min_effect)


def test_keep_needs_effect_and_corrected_significance():
    r = decide([delta(pct=-20.0, p=0.001)], score=0.80)
    assert r.status is verdict.Status.KEEP
    assert r.reason is verdict.Reason.IMPROVED
    assert r.exit_code() == 0


def test_improvement_below_min_effect_is_its_own_reason():
    """A real but tiny win must not be reported as 'nothing happened'."""
    r = decide([delta(pct=-0.5, p=0.001)], score=0.995)
    assert r.status is verdict.Status.DISCARD
    assert r.reason is verdict.Reason.BELOW_MIN_EFFECT
    assert "0.9950" in r.message


def test_no_significant_improvement():
    r = decide([delta(pct=-0.2, p=0.9)], score=0.998)
    assert r.reason is verdict.Reason.NO_IMPROVEMENT


def test_regression_guard_beats_a_good_score():
    r = decide([delta("a", pct=-40.0, p=0.001), delta("b", pct=+30.0, p=0.001)], score=0.80)
    assert r.status is verdict.Status.DISCARD
    assert r.reason is verdict.Reason.GUARD_REGRESSION
    assert [d.name for d in r.regressions] == ["b"]
    assert "+30.0%" in r.message


def test_regression_within_the_limit_does_not_trip_the_guard():
    r = decide([delta("a", pct=-40.0, p=0.001), delta("b", pct=+3.0, p=0.001)], score=0.80)
    assert r.status is verdict.Status.KEEP


def test_insignificant_regression_does_not_trip_the_guard():
    r = decide([delta("a", pct=-40.0, p=0.001), delta("b", pct=+30.0, p=0.9)], score=0.80)
    assert r.status is verdict.Status.KEEP


def test_regression_guard_uses_the_uncorrected_alpha():
    """Deliberate asymmetry: correcting here would make real harm easier to miss."""
    ds = [delta(f"b{i}", pct=+30.0, p=0.04) for i in range(10)]
    r = decide(ds, score=0.80)
    assert r.reason is verdict.Reason.GUARD_REGRESSION


def test_bonferroni_correction_blocks_a_marginal_keep():
    """p=0.04 clears raw alpha but not 0.05/4; the score alone must not carry a KEEP."""
    ds = [delta(f"b{i}", pct=-20.0, p=0.04) for i in range(4)]
    r = decide(ds, score=0.80)
    assert r.status is verdict.Status.DISCARD
    assert r.reason is verdict.Reason.NO_IMPROVEMENT


def test_one_benchmark_clearing_the_corrected_bar_is_enough():
    ds = [delta("a", pct=-20.0, p=0.001)] + [delta(f"b{i}", pct=-1.0, p=0.5) for i in range(3)]
    assert decide(ds, score=0.80).status is verdict.Status.KEEP


def test_min_effect_of_zero_only_requires_a_score_below_one():
    r = decide([delta(pct=-0.5, p=0.001)], score=0.995, min_effect=0.0)
    assert r.status is verdict.Status.KEEP


def test_delta_warnings_are_carried_through_deduplicated():
    ds = [delta("a", warnings=["w"]), delta("b", warnings=["w"])]
    r = decide(ds, score=1.0)
    assert r.warnings.count("w") == 1


def test_unreachable_alpha_is_warned_about():
    """count=5 with 7 benchmarks: 0.05/7 = 0.00714 against a floor of 0.00794."""
    ds = [delta(f"b{i}", n=5) for i in range(7)]
    r = decide(ds, score=1.0)
    assert any("no KEEP was reachable" in w for w in r.warnings)
    assert any("raise count to at least 6" in w for w in r.warnings)


def test_no_unreachable_warning_when_one_benchmark_can_clear_it():
    ds = [delta(f"b{i}", n=10) for i in range(2)]
    r = decide(ds, score=1.0)
    assert not any("no KEEP was reachable" in w for w in r.warnings)


def test_warnings_never_change_the_decision():
    """The unreachable-alpha warning fires, and the decision is still made on the numbers."""
    ds = [delta(f"b{i}", pct=-20.0, p=0.0001, n=5) for i in range(7)]
    r = decide(ds, score=0.80)
    assert any("no KEEP was reachable" in w for w in r.warnings)
    assert r.status is verdict.Status.KEEP


def test_empty_deltas_do_not_divide_by_zero():
    assert decide([], score=1.0).status is verdict.Status.DISCARD


@pytest.mark.parametrize(
    "status,code",
    [
        (verdict.Status.KEEP, 0),
        (verdict.Status.DISCARD, 1),
        (verdict.Status.FAIL, 2),
        (verdict.Status.CRASH, 3),
        (verdict.Status.ABORTED, 2),
    ],
)
def test_exit_codes(status, code):
    assert verdict.gate(status, verdict.Reason.TIMEOUT, "m").exit_code() == code


def test_gate_result_serializes_without_measurement_fields():
    d = verdict.gate(verdict.Status.FAIL, verdict.Reason.SCOPE, "out of scope").to_json_dict()
    assert d["status"] == "FAIL"
    assert d["reason"] == "scope_violation"
    assert d["regressions"] == []


def test_measure_reason_exists():
    """Ruling R7: a later task reports a crashed measurement round as this, not compile_failed."""
    assert verdict.Reason.MEASURE == "measure_failed"
