import re

from autor3search_python import templates, verdict


def test_program_md_exit_code_table_agrees_with_verdict():
    """A stale table would send the agent branching on the wrong exit code."""
    text = templates.program_md()
    start = text.index("| Exit code | Meaning | Verdict status |")
    table = text[start : start + 500]
    rows = re.findall(r"^\| `(\d)` \|.*\| `([A-Z]+)` \|$", table, flags=re.MULTILINE)
    assert len(rows) == 4  # KEEP, DISCARD, FAIL, CRASH — ABORTED is documented separately
    any_reason = next(iter(verdict.Reason))
    for code_str, status_str in rows:
        status = verdict.Status(status_str)
        expected = verdict.Result(status=status, reason=any_reason).exit_code()
        assert int(code_str) == expected, (
            f"program.md's table lists exit code {code_str} for {status_str}, "
            f"but verdict.Result.exit_code() gives {expected}"
        )


def test_program_md_is_shipped_and_non_trivial():
    text = templates.program_md()
    assert len(text) > 4000
    assert text.startswith("# program.md")


def test_program_md_documents_every_exit_code():
    text = templates.program_md()
    for token in ("`0`", "`1`", "`2`", "`3`", "KEEP", "DISCARD", "FAIL", "CRASH"):
        assert token in text


def test_program_md_names_no_go_artifacts():
    """A stale Go reference would send the agent looking for a file that is not there."""
    text = templates.program_md()
    for stale in (
        "autor3search-go",
        "go.mod",
        "go.sum",
        "_test.go",
        "config.yaml",
        "ns/op",
        "allocs_delta",
        "go test",
    ):
        assert stale not in text


def test_program_md_forbids_editing_conftest():
    assert "conftest.py" in templates.program_md()


def test_program_md_covers_the_stop_protocol():
    text = templates.program_md()
    assert '"stop_requested": true' in text
    assert "ABORTED" in text
