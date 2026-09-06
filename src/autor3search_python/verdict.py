"""Turns gate outcomes and measurements into a single decision."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from autor3search_python import stats
from autor3search_python.benchio import Delta


class Status(StrEnum):
    """The terminal outcome of one experiment.

    ABORTED is not a verdict: it is an experiment that was interrupted before
    anything was measured. It exits 2 so an agent that treats it like FAIL does
    the right thing with it.
    """

    KEEP = "KEEP"
    DISCARD = "DISCARD"
    FAIL = "FAIL"
    CRASH = "CRASH"
    ABORTED = "ABORTED"


class Reason(StrEnum):
    """A machine-readable explanation of a Status."""

    IMPROVED = "improved"
    NO_IMPROVEMENT = "no_significant_improvement"
    BELOW_MIN_EFFECT = "improvement_below_min_effect"
    GUARD_REGRESSION = "guard_regression"
    SCOPE = "scope_violation"
    CONFIG_CHANGED = "config_changed"
    NEW_TEST_FILE = "new_test_file"
    SYMLINK_SWAP = "symlink_swap"
    BASELINE_TAMPERED = "baseline_tampered"
    COMPILE = "compile_failed"
    IMPORT = "import_failed"
    TESTS = "tests_failed"
    TIMEOUT = "timeout"
    STOP_FORCED = "stop_forced"
    # Ruling R7: a measurement round itself crashing has no other honest
    # reason in this vocabulary — reporting it as COMPILE would be untrue.
    MEASURE = "measure_failed"


_EXIT_CODES = {
    Status.KEEP: 0,
    Status.DISCARD: 1,
    Status.FAIL: 2,
    Status.CRASH: 3,
    Status.ABORTED: 2,
}


@dataclass(frozen=True)
class Result:
    """The harness's answer for one experiment."""

    status: Status
    reason: Reason
    score: float = 0.0
    message: str = ""
    regressions: tuple[Delta, ...] = ()
    warnings: tuple[str, ...] = field(default=())

    def exit_code(self) -> int:
        return _EXIT_CODES.get(self.status, 2)

    def to_json_dict(self) -> dict:
        return {
            "status": str(self.status),
            "reason": str(self.reason),
            "score": self.score,
            "message": self.message,
            "regressions": [
                {"name": d.name, "pct_change": d.pct_change, "p": d.p} for d in self.regressions
            ],
            "warnings": list(self.warnings),
        }


def gate(status: Status, reason: Reason, message: str) -> Result:
    """A Result for a stage that failed before measurement."""
    return Result(status=status, reason=reason, message=message)


def decide(
    deltas: Sequence[Delta], score: float, max_regress_pct: float, min_effect_pct: float
) -> Result:
    """Apply the scoring rules.

    1. Any regression significant at the RAW, uncorrected alpha and larger than
       `max_regress_pct` rejects the change, however good the overall score.
       This deliberately does not apply the Bonferroni correction from rule 2:
       the correction only ever makes significance harder to reach, so applying
       it to a guard would make real harm easier to miss. Be conservative about
       accepting a win; be liberal about catching damage.
    2. Otherwise KEEP only when BOTH the score clears
       ``1 - min_effect_pct/100`` — not merely 1 — and at least one benchmark
       improved at the Bonferroni-corrected threshold ``alpha / k``, where k is
       the number of benchmarks compared. Testing k benchmarks against the same
       raw alpha inflates the family-wise false-positive rate; dividing by k is
       the standard correction.

    `Delta.significant` always means "significant at the raw alpha" — that stays
    the honest statistic a human reads. The correction is a KEEP threshold
    layered on top, not a redefinition of significance.
    """
    k = max(len(deltas), 1)
    warnings = _measurement_warnings(deltas, k)

    regressions = tuple(d for d in deltas if d.significant and d.pct_change > max_regress_pct)
    if regressions:
        detail = ", ".join(f"{d.name} {d.pct_change:+.1f}%" for d in regressions)
        return Result(
            status=Status.DISCARD,
            reason=Reason.GUARD_REGRESSION,
            score=score,
            message=f"regression guard tripped (limit {max_regress_pct:+.1f}%): {detail}",
            regressions=regressions,
            warnings=warnings,
        )

    improved = any(d.pct_change < 0 and d.p < d.alpha / k for d in deltas)
    threshold = 1 - min_effect_pct / 100

    if improved and score < threshold:
        return Result(
            status=Status.KEEP,
            reason=Reason.IMPROVED,
            score=score,
            message=f"score {score:.4f} ({(score - 1) * 100:+.2f}%)",
            warnings=warnings,
        )

    # "Your idea did nothing" and "your idea worked, but too little to bank"
    # discard alike but call for different next moves.
    if improved and score < 1:
        return Result(
            status=Status.DISCARD,
            reason=Reason.BELOW_MIN_EFFECT,
            score=score,
            message=(
                f"score {score:.4f} ({(score - 1) * 100:+.2f}%), a real improvement but "
                f"below the {min_effect_pct:.1f}% minimum effect size"
            ),
            warnings=warnings,
        )
    return Result(
        status=Status.DISCARD,
        reason=Reason.NO_IMPROVEMENT,
        score=score,
        message=f"score {score:.4f} ({(score - 1) * 100:+.2f}%), no net improvement",
        warnings=warnings,
    )


def _measurement_warnings(deltas: Sequence[Delta], k: int) -> tuple[str, ...]:
    """Everything qualifying how far these numbers can be trusted. Never decisive."""
    out: list[str] = []
    seen: set[str] = set()
    for d in deltas:
        for w in d.warnings:
            if w not in seen:
                seen.add(w)
                out.append(w)
    unreachable = _unreachable_alpha_warning(deltas, k)
    if unreachable:
        out.append(unreachable)
    return tuple(out)


def _unreachable_alpha_warning(deltas: Sequence[Delta], k: int) -> str | None:
    """Warn when rule 2 cannot be satisfied by any result whatsoever.

    The U test has a p-value floor for a given sample size. If the corrected
    threshold alpha/k falls below that floor for EVERY benchmark, no benchmark
    can clear it and every experiment discards no matter what the agent does.
    config.validate enforces a count floor for k=1; this is the same footgun at
    k benchmarks, which the validator cannot see because it does not know how
    many benchmarks a run will compare.
    """
    if not deltas:
        return None
    best_n = 0
    alpha = 0.0
    for d in deltas:
        corrected = d.alpha / k
        if stats.min_achievable_p(d.n_base, d.n_cand) < corrected:
            return None  # one benchmark can clear it, which is all a KEEP needs
        n = min(d.n_base, d.n_cand)
        # The largest sample size seen is the most favourable case: a smaller n
        # only makes the p-value floor higher, so reporting the maximum is the
        # strongest true claim this warning can make ("even the best-sampled
        # benchmark cannot clear the bar").
        if n > best_n:
            best_n, alpha = n, d.alpha
    corrected = alpha / k
    floor = stats.min_achievable_p(best_n, best_n)
    msg = (
        f"no KEEP was reachable: comparing {k} benchmark(s) corrects the significance "
        f"threshold to {corrected:.5f}, but with {best_n} rounds per side the test cannot "
        f"produce a p-value below {floor:.5f} however large the improvement is"
    )
    need = stats.count_for_alpha(corrected)
    return msg + (
        f" — raise count to at least {need}"
        if need
        else " — raise count, or measure fewer benchmarks"
    )
