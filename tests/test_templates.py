import re

from autor3search_python import pipeline, templates, verdict


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


def test_program_md_forbids_every_file_the_scope_gate_rejects_outright():
    """Derived from the constants, not retyped: adding a file to either set
    must fail here until program.md names it too. The agent cannot read the
    source of its own judge, so a rule missing from program.md is a rule it
    can only learn by burning an experiment on it.
    """
    text = templates.program_md()
    # Startup hooks by STEM: the gate refuses every importable suffix, but
    # program.md explains the class in prose rather than listing six filenames.
    for name in sorted(
        pipeline.DEPENDENCY_FILES | pipeline.MEASUREMENT_CONFIG_FILES | pipeline.STARTUP_HOOK_STEMS
    ):
        assert name in text, f"{name} is rejected outright but program.md never says so"


def test_program_md_names_every_reason_code():
    """Derived from the enum, not retyped: adding a Reason must fail here until
    program.md names it too. `reason` is presented to the agent as an
    exhaustive list, so a code it can receive but cannot find there is one it
    can only interpret by guessing.
    """
    text = templates.program_md()
    for reason in verdict.Reason:
        assert f"`{reason.value}`" in text, (
            f"{reason.value} can appear in --json output but program.md never names it"
        )
