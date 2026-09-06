from autor3search_python import templates


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
