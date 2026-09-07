"""Parses pytest-benchmark JSON and compares two measurement sets.

One pytest process run yields a whole distribution per benchmark; the
interleaved design above this module runs the process `count` times per side.
So each run is reduced to a single observation by the configured statistic, and
the comparison happens across runs — never across pytest's own inner rounds,
which all share one process, one interpreter state and one moment in time.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from autor3search_python import stats

DEFAULT_ALPHA = 0.05

# Observations are seconds, as pytest-benchmark reports them. Ratios are
# unit-independent; only human-facing output converts for display.
_PARAM_SUFFIX = re.compile(r"\[.*\]$")


class BenchError(Exception):
    """Benchmark output that cannot be parsed, or a comparison that cannot be made."""


def base_name(node_id: str) -> str:
    """Strip a parametrization suffix, so config can name a benchmark once.

    The direct analogue of Go's sub-benchmark base: a config entry
    ``tests/test_x.py::test_y`` must select every ``...::test_y[case]``.
    """
    return _PARAM_SUFFIX.sub("", node_id)


@dataclass
class Series:
    """Every observation of one benchmark, in observation order."""

    name: str
    base: str
    values: list[float] = field(default_factory=list)


class Set:
    """A collection of benchmark observations, keyed by node id."""

    def __init__(self) -> None:
        self.series: dict[str, Series] = {}

    def record(self, name: str, value: float) -> None:
        ser = self.series.get(name)
        if ser is None:
            ser = Series(name=name, base=base_name(name))
            self.series[name] = ser
        ser.values.append(value)

    def names(self) -> list[str]:
        return sorted(self.series)

    def values(self, name: str) -> list[float] | None:
        ser = self.series.get(name)
        return list(ser.values) if ser is not None else None

    def add(self, other: Set) -> None:
        for name in other.names():
            for v in other.series[name].values:
                self.record(name, v)

    def select_by_base(self, bases: Sequence[str]) -> Set:
        """A new Set holding only series whose base appears in `bases`.

        An empty `bases` selects everything, matching config's "empty means all".
        """
        wanted = set(bases)
        out = Set()
        for name in self.names():
            ser = self.series[name]
            if wanted and ser.base not in wanted:
                continue
            for v in ser.values:
                out.record(name, v)
        return out


def parse(payload: str | bytes | dict, stat: str) -> Set:
    """Build a Set from one pytest-benchmark JSON document."""
    if isinstance(payload, dict):
        doc = payload
    else:
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        try:
            doc = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise BenchError(f"parse benchmark output: {e}") from e
    if not isinstance(doc, dict):
        raise BenchError("benchmark output is not a JSON object")
    entries = doc.get("benchmarks")
    if not isinstance(entries, list):
        raise BenchError("benchmark output has no 'benchmarks' array")
    out = Set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise BenchError("benchmark entry is not an object")
        name = entry.get("fullname") or entry.get("name")
        if not isinstance(name, str) or not name:
            raise BenchError("benchmark entry has no name")
        st = entry.get("stats")
        if not isinstance(st, dict) or stat not in st:
            raise BenchError(f"benchmark {name}: no {stat!r} in its reported stats")
        value = st[stat]
        if not isinstance(value, int | float):
            raise BenchError(f"benchmark {name}: {stat!r} is not a number")
        out.record(name, float(value))
    return out


@dataclass(frozen=True)
class Delta:
    """One benchmark's comparison between baseline and candidate."""

    name: str
    base_center: float
    cand_center: float
    ratio: float  # cand/base; below 1 is faster
    pct_change: float  # (ratio - 1) * 100
    p: float
    alpha: float
    significant: bool  # p < alpha, at the RAW alpha — never Bonferroni-corrected
    n_base: int
    n_cand: int
    warnings: tuple[str, ...] = ()


def _ci_warning(values: Sequence[float]) -> str | None:
    if stats.median_ci(values) is not None:
        return None
    return (
        f"{len(values)} observations per side is too few for a bounded confidence interval "
        f"around the median at 95% confidence (needs at least {stats.MIN_CI_SAMPLES}); "
        f"the reported centers are real but the uncertainty around them is not bounded"
    )


def _dedupe(items: Iterable[str | None]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item is None or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return tuple(out)


def compare(base: Set, cand: Set, name: str, alpha: float = DEFAULT_ALPHA) -> Delta:
    """Compare one benchmark across two sets."""
    bv = base.values(name)
    cv = cand.values(name)
    if bv is None:
        raise BenchError(f"baseline has no observations for {name}")
    if cv is None:
        raise BenchError(f"candidate has no observations for {name}")
    if len(bv) < 2 or len(cv) < 2:
        raise BenchError(f"{name}: need at least 2 observations per side, got {len(bv)}/{len(cv)}")
    base_center = stats.median(bv)
    cand_center = stats.median(cv)
    if base_center == 0:
        raise BenchError(f"{name}: baseline median is zero, cannot form a ratio")
    ratio = cand_center / base_center
    p = stats.mann_whitney_u(bv, cv)
    return Delta(
        name=name,
        base_center=base_center,
        cand_center=cand_center,
        ratio=ratio,
        pct_change=(ratio - 1) * 100,
        p=p,
        alpha=alpha,
        significant=p < alpha,
        n_base=len(bv),
        n_cand=len(cv),
        warnings=_dedupe([_ci_warning(bv), _ci_warning(cv)]),
    )


def total_reported_seconds(payload: str | bytes | dict) -> float | None:
    """Sum of pytest-benchmark's own reported total time across every
    benchmark in one JSON report — independent of `stat`, which only ever
    picks one center per benchmark.

    Used solely by the harness's own wall-clock timing-plausibility tripwire
    (see measure.TimingImplausibleError), never by scoring. Returns None
    when the report carries no usable timing field at all — e.g. an
    unexpected pytest-benchmark schema — rather than 0.0, so a caller cannot
    mistake "nothing to measure" for "instant", which would make every
    subsequent ratio blow up to a false positive.
    """
    if isinstance(payload, dict):
        doc = payload
    else:
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        try:
            doc = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
    if not isinstance(doc, dict):
        return None
    entries = doc.get("benchmarks")
    if not isinstance(entries, list):
        return None
    total = 0.0
    found = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        st = entry.get("stats")
        if not isinstance(st, dict):
            continue
        t = st.get("total")
        if isinstance(t, int | float):
            total += float(t)
            found = True
            continue
        median, rounds = st.get("median"), st.get("rounds")
        if isinstance(median, int | float) and isinstance(rounds, int | float):
            total += float(median) * float(rounds)
            found = True
    return total if found else None


def compare_all(base: Set, cand: Set, alpha: float = DEFAULT_ALPHA) -> list[Delta]:
    """Compare every benchmark measured at baseline, sorted by name.

    A benchmark that disappears from the candidate is an error, not a skip: it
    cannot be checked for regressions, which would hide a real problem.
    """
    missing = [n for n in base.names() if cand.values(n) is None]
    if missing:
        raise BenchError(
            f"benchmark(s) measured at baseline but missing from the candidate: "
            f"{sorted(missing)} — a benchmark that disappears cannot be checked for regressions"
        )
    out = [compare(base, cand, name, alpha) for name in base.names()]
    if not out:
        raise BenchError("no benchmark appears in both baseline and candidate")
    return out
