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

from autor3search_python import benchio, runner
from autor3search_python.config import Config

RoundFn = Callable[[int], benchio.Set]


class MeasureError(Exception):
    """A measurement that could not be taken."""


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


def _side(directory: str | Path, opts: Options) -> RoundFn:
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
        s = benchio.parse(payload, opts.cfg.stat)
        if not s.names():
            raise MeasureError(f"no benchmarks matched {list(opts.node_ids)} in {d}")
        return s

    return one_round


def run(opts: Options) -> tuple[benchio.Set, benchio.Set]:
    """Measure both trees with real pytest invocations."""
    if not opts.node_ids:
        raise MeasureError("no benchmarks selected")
    return interleave(opts.cfg.count, True, _side(opts.base_dir, opts), _side(opts.cand_dir, opts))
