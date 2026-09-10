"""Collects observations from a baseline and a candidate tree, interleaved.

Interleaving is the core measurement discipline. Comparing a candidate measured
now against a baseline measured minutes ago attributes CPU thermal drift,
frequency scaling and background load to the code change. Alternating the two
sides within a single session cancels that drift, because both sides experience
the same conditions.

One asymmetry remains and is documented rather than hidden: each round measures
the baseline a moment before the candidate, so on a steadily warming machine the
candidate is sampled a fraction hotter. Interleaving cancels the drift *between*
rounds, which dominates; this sub-second offset does not.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from autor3search import benchio, runner
from autor3search.config import Config

RoundFn = Callable[[int], benchio.Set]


class MeasureError(Exception):
    """A measurement that could not be taken."""


class TimingImplausibleError(MeasureError):
    """Wall-clock time for a bench subprocess and its own reported numbers
    disagree by a factor wide enough that no legitimate machine explains it.

    The benchmark timer runs inside the process executing the candidate's
    code (see runner.py and the README's Limitations section), so a
    candidate could rewrite pytest-benchmark's own accounting from in-scope
    source and have every reported number understated without the reported
    DATA itself looking malformed. What it cannot forge is how long THIS
    process, running outside that subprocess, waited for it to exit.

    This is a tripwire for the GROSS case only — a monkeypatch that divides
    every reported duration by ten, say — and it does not, and cannot,
    catch a subtle few-percent skew: ordinary interpreter startup,
    collection and fixture setup already make wall time exceed reported
    time by some machine- and workload-dependent factor, and a candidate
    could shave a few percent off its own numbers within that same
    normal-looking margin without ever tripping this. What it catches is a
    discrepancy so much larger than the OTHER side's own overhead on the
    very same run that no explanation other than rigged accounting is
    plausible.
    """


# How many times wider the candidate's own (wall / reported) ratio may be
# than the baseline's before this refuses to trust the run. Wide on purpose:
# this exists to catch a fraud that hides a real number behind a fake one an
# order of magnitude smaller, not to referee normal machine-to-machine or
# run-to-run variance in startup overhead.
_TIMING_FRAUD_FACTOR = 5.0


@dataclass
class _TimingAudit:
    """Accumulated wall-clock and self-reported time for one side, across
    every measured round (including the discarded warmup round — the wall
    time was genuinely spent either way)."""

    wall_seconds: float = 0.0
    reported_seconds: float = 0.0


def _check_timing_plausibility(base_audit: _TimingAudit, cand_audit: _TimingAudit) -> None:
    """Harness-side sanity check: see TimingImplausibleError for exactly what
    this catches and what it deliberately does not."""
    if base_audit.reported_seconds <= 0 or cand_audit.reported_seconds <= 0:
        return  # nothing usable to compare against; do not manufacture a false alarm
    base_overhead = base_audit.wall_seconds / base_audit.reported_seconds
    cand_overhead = cand_audit.wall_seconds / cand_audit.reported_seconds
    if base_overhead <= 0:
        return
    if cand_overhead > base_overhead * _TIMING_FRAUD_FACTOR:
        raise TimingImplausibleError(
            f"candidate bench subprocesses ran for {cand_audit.wall_seconds:.3f}s of wall "
            f"time but reported only {cand_audit.reported_seconds:.3f}s of benchmark time "
            f"({cand_overhead:.1f}x overhead), against {base_overhead:.1f}x on the baseline "
            f"side of the very same run. A gap this wide, in the direction that makes the "
            f"candidate look faster, is not explained by ordinary interpreter startup and "
            f"collection cost. This is a tripwire for a gross timing fraud, not proof of "
            f"one and not a general timing audit — see measure.TimingImplausibleError."
        )


def interleave(
    rounds: int, warmup: bool, base: RoundFn, cand: RoundFn
) -> tuple[benchio.Set, benchio.Set]:
    """Run both sides alternately and accumulate their observations.

    When `warmup` is true an extra leading round is run and discarded, absorbing
    first-touch effects: cold caches, on-demand bytecode compilation, lazy
    imports.
    """
    if rounds < 2:
        raise MeasureError(f"need at least 2 measured rounds, got {rounds}")
    total = rounds + (1 if warmup else 0)
    base_set, cand_set = benchio.Set(), benchio.Set()
    for i in range(total):
        try:
            b = base(i)
        except MeasureError:
            raise
        except Exception as e:
            raise MeasureError(f"baseline round {i}: {e}") from e
        try:
            c = cand(i)
        except MeasureError:
            raise
        except Exception as e:
            raise MeasureError(f"candidate round {i}: {e}") from e
        if warmup and i == 0:
            continue
        base_set.add(b)
        cand_set.add(c)
    return base_set, cand_set


@dataclass
class Options:
    """Everything one measurement needs."""

    base_dir: str | Path
    cand_dir: str | Path
    node_ids: Sequence[str]
    cfg: Config
    log: IO[str] | None = None


def _side(directory: str | Path, opts: Options, audit: _TimingAudit) -> RoundFn:
    d = Path(directory)
    env = runner.bench_env(d, opts.cfg)
    r = runner.Runner(d, opts.cfg.timeout_seconds(), log=opts.log, env=env, python=opts.cfg.python)

    def one_round(_: int) -> benchio.Set:
        with tempfile.TemporaryDirectory(prefix="autor3search-") as tmp:
            json_path = Path(tmp) / "bench.json"
            res = r.bench(opts.node_ids, json_path, opts.cfg)
            if res.timed_out:
                raise MeasureError(f"benchmark round timed out after {opts.cfg.timeout} in {d}")
            if not res.ok():
                raise MeasureError(
                    f"benchmark round failed in {d} (exit {res.exit_code}):\n{res.tail(30)}"
                )
            try:
                payload = json_path.read_text(encoding="utf-8")
            except OSError as e:
                raise MeasureError(
                    f"benchmark round in {d} wrote no JSON report: {e} — is "
                    f"pytest-benchmark installed for this interpreter?"
                ) from e
        # This process's own wall-clock measurement of the subprocess, which
        # the subprocess cannot influence, feeds the timing-plausibility
        # tripwire below — independent of anything `payload` claims.
        audit.wall_seconds += res.duration
        reported = benchio.total_reported_seconds(payload)
        if reported is not None:
            audit.reported_seconds += reported
        s = benchio.parse(payload, opts.cfg.stat)
        if not s.names():
            raise MeasureError(f"no benchmarks matched {list(opts.node_ids)} in {d}")
        return s

    return one_round


def run(opts: Options) -> tuple[benchio.Set, benchio.Set]:
    """Measure both trees with real pytest invocations."""
    if not opts.node_ids:
        raise MeasureError("no benchmarks selected")
    base_audit, cand_audit = _TimingAudit(), _TimingAudit()
    base_set, cand_set = interleave(
        opts.cfg.count,
        True,
        _side(opts.base_dir, opts, base_audit),
        _side(opts.cand_dir, opts, cand_audit),
    )
    _check_timing_plausibility(base_audit, cand_audit)
    return base_set, cand_set
