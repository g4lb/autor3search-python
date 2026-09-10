"""The README's enforcement table is the human's map of what the harness
refuses. Nothing tied it to the constants, and the agent-facing copy of the
same list drifted exactly that way: 3a437bf added freeze_drift and
timing_implausible to README.md but not to templates/program.md.
"""

import re
from pathlib import Path

import pytest

from autor3search import pipeline

README = Path(__file__).parents[1] / "README.md"
HEADING = "## What the harness enforces"


def enforcement_table() -> str:
    """The enforcement section alone, so a name mentioned elsewhere in the
    README does not satisfy a claim the table is the one making."""
    if not README.is_file():
        pytest.skip("README.md is not present (running against an installed package)")
    text = README.read_text(encoding="utf-8")
    if HEADING not in text:
        pytest.fail(
            f"README.md has no {HEADING!r} section — if it was renamed, update HEADING here "
            f"rather than leaving the coverage below asserting against nothing"
        )
    start = text.index(HEADING)
    rest = text[start + len(HEADING) :]
    end = re.search(r"^## ", rest, flags=re.MULTILINE)
    return rest[: end.start()] if end else rest


def test_enforcement_table_names_every_file_rejected_outright():
    """Derived from the constants, not retyped: adding a file to any of the
    three sets must fail here until the table says so too.
    """
    table = enforcement_table()
    for name in sorted(
        pipeline.DEPENDENCY_FILES | pipeline.MEASUREMENT_CONFIG_FILES | pipeline.STARTUP_HOOK_STEMS
    ):
        assert name in table, (
            f"{name} is rejected outright but the README's enforcement table never names it"
        )


def test_enforcement_table_is_a_table_with_rows():
    """A guard for the guard: if the heading is renamed or the table replaced
    with prose, the coverage test above would pass against an empty slice.
    """
    table = enforcement_table()
    assert "| Attempt | Why it fails |" in table
    rows = [ln for ln in table.splitlines() if ln.startswith("| ") and " | " in ln]
    assert len(rows) > 10, f"enforcement table has only {len(rows)} rows — did it move?"
