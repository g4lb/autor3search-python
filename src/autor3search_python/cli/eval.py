"""eval — run one experiment: gate, measure, score, record.

The only command whose result decides anything. With --json it prints exactly
one JSON object to stdout and nothing else, because that is what the agent's
loop parses. The noisy transcript goes to run.log, opened here rather than by
the agent: an agent redirecting stdout into run.log would open a second
descriptor on a file this process already holds, and whichever wrote second
would clobber the other from byte 0 — destroying exactly the diagnostic needed
when a gate fails.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from autor3search_python import benchio, freeze, gitx, pipeline, results, runstop, state, verdict
from autor3search_python.cli import runctx
from autor3search_python.cli.main import EXIT_USAGE


def best_bench_delta(deltas: Sequence[benchio.Delta]) -> float:
    """The largest single-benchmark improvement, percent. 0.0 for no deltas."""
    return min((d.pct_change for d in deltas), default=0.0)


def build_json(
    result: verdict.Result,
    base: state.Baseline,
    tag: str,
    worktree: Path,
    experiment: int,
    stop_requested: bool,
) -> dict[str, Any]:
    doc = result.to_json_dict()
    doc["stop_requested"] = stop_requested
    doc["run"] = {
        "tag": tag,
        "branch": base.branch,
        "baseline_commit": base.commit,
        "measure_commit": base.measure_commit,
        "worktree": str(worktree),
        "experiment": experiment,
    }
    return doc


def _print_human(result: verdict.Result, measurements: pipeline.Measurements | None) -> None:
    if measurements and measurements.time:
        print("benchmark                                             base      cand    change")
        for d in measurements.time:
            print(
                f"{d.name[:50]:<50}  {d.base_center * 1e3:8.3f}ms "
                f"{d.cand_center * 1e3:8.3f}ms  {d.pct_change:+7.2f}%  "
                f"(p={d.p:.4f}{'' if d.significant else ' n.s.'})"
            )
        print()
    for w in result.warnings:
        print(f"WARNING: {w}")
    if result.warnings:
        print()
    print(f"VERDICT: {result.status}")
    if result.message:
        print(result.message)


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="autor3search-python eval")
    parser.add_argument("-C", dest="directory", default=".", help="repository root")
    parser.add_argument("-tag", "--tag", dest="tag", default=None, help="run identifier")
    parser.add_argument("--json", dest="as_json", action="store_true", help="one JSON object")
    parser.add_argument(
        "-desc",
        "--desc",
        dest="desc",
        default="",
        help="what this experiment tried; lands in results.tsv",
    )
    opts = parser.parse_args(args)

    try:
        ctx = runctx.resolve(opts.directory, opts.tag)
    except runctx.ContextError as e:
        print(f"autor3search-python eval: {e}", file=sys.stderr)
        return EXIT_USAGE

    worktree = ctx.state_dir / state.WORKTREE_NAME
    log_path = ctx.root / pipeline.RUN_LOG_NAME

    try:
        claim = runstop.claim_eval(ctx.state_dir, os.getpid())
    except runstop.StopError as e:
        print(f"autor3search-python eval: {e}", file=sys.stderr)
        return EXIT_USAGE

    def on_signal(signum, frame):  # noqa: ARG001
        # Handled rather than died under: pytest runs the benchmark in a child
        # process, and an eval killed without a chance to clean up would leave
        # it running — burning CPU and corrupting every later measurement on
        # the machine.
        raise KeyboardInterrupt

    previous: dict[int, Any] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            previous[sig] = signal.signal(sig, on_signal)

    try:
        with claim, log_path.open("a", encoding="utf-8") as log:
            try:
                result, measurements = pipeline.evaluate(
                    pipeline.Options(
                        root=ctx.root,
                        state_dir=ctx.state_dir,
                        cfg=ctx.cfg,
                        base=ctx.base,
                        log=log,
                    )
                )
            except KeyboardInterrupt:
                # Nothing was measured, so nothing is recorded: no results.tsv
                # row for an experiment that never produced a number.
                result = verdict.Result(
                    status=verdict.Status.ABORTED,
                    reason=verdict.Reason.STOP_FORCED,
                    message="the experiment was interrupted before it was measured",
                )
                doc = build_json(
                    result,
                    ctx.base,
                    ctx.tag,
                    worktree,
                    len(_rows(ctx.root)) + 1,
                    runstop.stop_requested(ctx.state_dir),
                )
                if opts.as_json:
                    print(json.dumps(doc))
                else:
                    _print_human(result, None)
                return result.exit_code()

            row_index = len(_rows(ctx.root)) + 1
            results.append(
                ctx.root / results.PATH,
                results.Row(
                    commit=gitx.head_commit(ctx.root),
                    score=result.score,
                    best_bench_delta=best_bench_delta(measurements.time if measurements else []),
                    status=str(result.status),
                    description=opts.desc,
                ),
            )
            stop = runstop.stop_requested(ctx.state_dir)
            if opts.as_json:
                print(json.dumps(build_json(result, ctx.base, ctx.tag, worktree, row_index, stop)))
            else:
                _print_human(result, measurements)
                if stop:
                    print("\nstop requested: finish this verdict, then leave the loop.")
            return result.exit_code()
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        results.ResultsError,
        freeze.FreezeError,
        state.StateError,
        gitx.GitError,
        benchio.BenchError,
    ) as e:
        # A harness malfunction, not a verdict: no row, and a message on stderr.
        print(f"autor3search-python eval: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_USAGE
    finally:
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)


def _rows(root: Path) -> list[results.Row]:
    try:
        return results.load(root / results.PATH)
    except results.ResultsError:
        return []
