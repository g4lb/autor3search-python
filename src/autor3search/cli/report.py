"""report — summarize results.tsv. The human's morning read."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from autor3search import gitx, results
from autor3search.cli.main import EXIT_OK, EXIT_USAGE

_TOP_WINS = 5


def cumulative_speedup(rows: Sequence[results.Row]) -> float:
    """The product of every kept score.

    Not the latest score, and not the best one. The measurement baseline
    advances after every KEEP, so each kept score is that experiment's own
    incremental contribution — successive real improvements compound the way
    percentage changes do.
    """
    total = 1.0
    for r in rows:
        if r.status.upper() == "KEEP" and r.score > 0:
            total *= r.score
    return total


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="autor3search-python report")
    parser.add_argument("-C", dest="directory", default=".", help="repository root")
    opts = parser.parse_args(args)

    try:
        root = Path(gitx.root(opts.directory))
    except gitx.GitError:
        root = Path(opts.directory)
    try:
        rows = results.load(root / results.PATH)
    except results.ResultsError as e:
        print(f"autor3search-python report: {e}", file=sys.stderr)
        return EXIT_USAGE

    if not rows:
        print(f"no experiments recorded in {root / results.PATH}")
        return EXIT_OK

    counts = Counter(r.status.upper() for r in rows)
    total = cumulative_speedup(rows)

    print(f"experiments    {len(rows)}")
    print(
        f"               {counts['KEEP']} keep, {counts['DISCARD']} discard, "
        f"{counts['FAIL']} fail, {counts['CRASH']} crash"
    )
    print(f"cumulative     {total:.4f}  ({(total - 1) * 100:+.2f}% overall)")
    print()

    kept = sorted((r for r in rows if r.status.upper() == "KEEP"), key=lambda r: r.score)[
        :_TOP_WINS
    ]
    if kept:
        print("largest wins:")
        for r in kept:
            print(f"  {r.score:.4f}  ({(r.score - 1) * 100:+6.2f}%)  {r.commit}  {r.description}")
        print()

    print("every experiment, in order:")
    for i, r in enumerate(rows, start=1):
        print(f"  #{i:<3} {r.status:<8} {r.score:.4f}  {r.commit}  {r.description}")
    return EXIT_OK
