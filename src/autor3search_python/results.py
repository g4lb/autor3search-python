"""Reads and appends the experiment log — the human's morning read.

Strict on load by design. Sanitising on write makes a malformed row nearly
impossible, so one that shows up is a real signal (a torn write, a hand edit),
and the error names the file and line so it can be fixed. Silently dropping
rows would let a corrupted log masquerade as a short one, which is worse for a
file that is the sole record of an overnight run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PATH = "results.tsv"
HEADER = "commit\tscore\tbest_bench_delta\tstatus\tdescription"
_FIELDS = 5

# description has no length cap of its own, and an agent pasting a stack trace
# or a diff would otherwise produce a row long enough to make the log
# unpleasant to read and awkward to parse. 256 characters is generous for a
# one-line summary.
MAX_DESCRIPTION_LEN = 256


class ResultsError(Exception):
    """A results log that cannot be read."""


@dataclass(frozen=True)
class Row:
    """One logged experiment. Field order matches HEADER, append and load.

    Five fields, not Go's six: there is no allocations column, because
    pytest-benchmark measures no allocations and a column filled with a number
    we did not measure is worse than no column.
    """

    commit: str
    score: float
    best_bench_delta: float
    status: str
    description: str


def _clean(s: str) -> str:
    """Make a field safe for a tab-separated single-line record."""
    return s.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def _truncate(s: str) -> str:
    """Cap by character count, so a multi-byte character is never split."""
    return s if len(s) <= MAX_DESCRIPTION_LEN else s[:MAX_DESCRIPTION_LEN] + "..."


def append(path: str | Path, row: Row) -> None:
    """Add one row, creating the file with a header when needed."""
    p = Path(path)
    is_new = not p.exists()
    line = "\t".join(
        [
            _clean(row.commit),
            f"{row.score:.4f}",
            f"{row.best_bench_delta:.2f}",
            _clean(row.status),
            _truncate(_clean(row.description)),
        ]
    )
    try:
        with p.open("a", encoding="utf-8") as f:
            if is_new:
                f.write(HEADER + "\n")
            f.write(line + "\n")
            f.flush()
            # Durable across a crash: this is the sole record of an overnight run.
            os.fsync(f.fileno())
    except OSError as e:
        raise ResultsError(f"append {p}: {e}") from e


def load(path: str | Path) -> list[Row]:
    """Read every row. A missing file is an empty log, not an error."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as e:
        raise ResultsError(f"open {p}: {e}") from e

    rows: list[Row] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line == HEADER:
            continue
        parts = line.split("\t")
        if len(parts) != _FIELDS:
            raise ResultsError(f"{p} line {lineno}: got {len(parts)} fields, want {_FIELDS}")
        try:
            score = float(parts[1])
        except ValueError as e:
            raise ResultsError(f"{p} line {lineno}: score: {e}") from e
        try:
            best = float(parts[2])
        except ValueError as e:
            raise ResultsError(f"{p} line {lineno}: best_bench_delta: {e}") from e
        rows.append(
            Row(
                commit=parts[0],
                score=score,
                best_bench_delta=best,
                status=parts[3],
                description=parts[4],
            )
        )
    return rows
