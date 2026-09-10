"""status — print where a run is. Read-only: checking on a run cannot change it."""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from autor3search import gitx, results, runstop, state
from autor3search.cli import runctx
from autor3search.cli.main import EXIT_OK, EXIT_USAGE


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="autor3search-python status")
    parser.add_argument("-C", dest="directory", default=".", help="repository root")
    parser.add_argument("-tag", "--tag", dest="tag", default=None, help="run identifier")
    opts = parser.parse_args(args)

    try:
        ctx = runctx.resolve(opts.directory, opts.tag)
    except runctx.ContextError as e:
        print(f"autor3search-python status: {e}", file=sys.stderr)
        return EXIT_USAGE

    checked_out = gitx.current_branch(ctx.root) == ctx.base.branch
    try:
        rows = results.load(ctx.root / results.PATH)
    except results.ResultsError as e:
        print(f"autor3search-python status: {e}", file=sys.stderr)
        return EXIT_USAGE
    counts = Counter(r.status.upper() for r in rows)

    # R12: status is read-only and is what a human runs to find out what is
    # going on — often precisely because something is wrong. A corrupt pid
    # file must be reported as an unknown eval state, not crash the one
    # command meant to explain that state.
    try:
        pid, running = runstop.eval_running(ctx.state_dir)
        if running:
            eval_state = f"running (pid {pid}) — an experiment is being measured"
        else:
            eval_state = "not running"
    except runstop.StopError:
        eval_state = "unknown (the pid file could not be read)"

    print(f"run tag        {ctx.tag}")
    print(
        f"branch         {ctx.base.branch}  ({'checked out' if checked_out else 'not checked out'})"
    )
    print(f"baseline       {ctx.base.commit}  (run started here)")
    if ctx.base.measure_commit == ctx.base.commit:
        print(f"measuring vs   {ctx.base.measure_commit}  (still at the baseline)")
    else:
        print(
            f"measuring vs   {ctx.base.measure_commit}  "
            f"(advanced past the baseline by earlier KEEPs)"
        )
    print(f"worktree       {ctx.state_dir / state.WORKTREE_NAME}")
    print(
        f"experiments    {len(rows)} run  "
        f"({counts['KEEP']} keep, {counts['DISCARD']} discard, "
        f"{counts['FAIL']} fail, {counts['CRASH']} crash)  — next is #{len(rows) + 1}"
    )
    print(f"eval           {eval_state}")
    stop_state = "requested" if runstop.stop_requested(ctx.state_dir) else "not requested"
    print(f"stop           {stop_state}")
    print()
    print("to stop after the current experiment:  autor3search-python stop")
    print("to stop now, abandoning it:            autor3search-python stop -force")
    return EXIT_OK
