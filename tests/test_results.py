import pytest

from autor3search_python import containment, results


def row(**kw):
    base = dict(
        commit="abc1234",
        score=0.9,
        best_bench_delta=-10.0,
        status="keep",
        description="preallocate",
    )
    return results.Row(**{**base, **kw})


def test_append_creates_the_file_with_a_header(tmp_path):
    p = tmp_path / "results.tsv"
    results.append(p, row())
    lines = p.read_text().splitlines()
    assert lines[0] == results.HEADER
    assert lines[1].split("\t") == ["abc1234", "0.9000", "-10.00", "keep", "preallocate"]


def test_append_writes_the_header_only_once(tmp_path):
    p = tmp_path / "results.tsv"
    results.append(p, row())
    results.append(p, row(description="second"))
    assert p.read_text().count(results.HEADER) == 1
    assert len(results.load(p)) == 2


def test_round_trip(tmp_path):
    p = tmp_path / "results.tsv"
    results.append(p, row())
    got = results.load(p)
    assert got == [row()]


def test_append_writes_the_header_into_a_preexisting_empty_file(tmp_path):
    """A file that exists but is empty (touched by hand, or left by a torn write)
    must still get a header — existence alone is not proof a header was written."""
    p = tmp_path / "results.tsv"
    p.write_text("")
    results.append(p, row())
    lines = p.read_text().splitlines()
    assert lines[0] == results.HEADER
    assert len(results.load(p)) == 1


def test_missing_file_is_an_empty_log_not_an_error(tmp_path):
    assert results.load(tmp_path / "absent.tsv") == []


def test_tabs_and_newlines_in_a_field_are_flattened(tmp_path):
    p = tmp_path / "results.tsv"
    results.append(p, row(description="line one\nline\ttwo\r"))
    assert len(p.read_text().splitlines()) == 2
    assert results.load(p)[0].description == "line one line two"


def test_over_long_description_is_truncated(tmp_path):
    """An agent pasting a stack trace must not jam every future load."""
    p = tmp_path / "results.tsv"
    results.append(p, row(description="x" * 5000))
    got = results.load(p)[0].description
    assert len(got) == results.MAX_DESCRIPTION_LEN + 3
    assert got.endswith("...")


def test_truncation_does_not_split_a_multibyte_character(tmp_path):
    p = tmp_path / "results.tsv"
    results.append(p, row(description="é" * 5000))
    got = results.load(p)[0].description
    assert got.startswith("é")
    assert len(got) == results.MAX_DESCRIPTION_LEN + 3


def test_a_malformed_line_fails_the_whole_load(tmp_path):
    """Strict on purpose: a corrupt log must not masquerade as a short one."""
    p = tmp_path / "results.tsv"
    p.write_text(results.HEADER + "\nonly\ttwo\n")
    with pytest.raises(results.ResultsError, match="line 2"):
        results.load(p)


def test_a_non_numeric_score_names_the_line(tmp_path):
    p = tmp_path / "results.tsv"
    p.write_text(results.HEADER + "\nabc\tnope\t-1.0\tkeep\td\n")
    with pytest.raises(results.ResultsError, match="score"):
        results.load(p)


def test_blank_lines_are_skipped(tmp_path):
    p = tmp_path / "results.tsv"
    p.write_text(results.HEADER + "\n\nabc\t0.9\t-1.0\tkeep\td\n\n")
    assert len(results.load(p)) == 1


def test_append_refuses_a_symlinked_target(tmp_path):
    """results.tsv is gitignored and its name is waved through the scope gate,
    so nothing else stops an agent from replacing it with a symlink before an
    eval runs. Demonstrated: the harness would otherwise append a results row
    through the link to whatever file it points at, outside the repository."""
    outside = tmp_path / "victim.txt"
    outside.write_text("untouched")
    link = tmp_path / "repo" / "results.tsv"
    link.parent.mkdir()
    link.symlink_to(outside)
    with pytest.raises((results.ResultsError, containment.ContainmentError)):
        results.append(link, row())
    assert outside.read_text() == "untouched"


def test_append_refuses_a_symlinked_ancestor_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sub").symlink_to(outside)
    target = repo / "sub" / "results.tsv"
    with pytest.raises((results.ResultsError, containment.ContainmentError)):
        results.append(target, row(), root=repo)
    assert not (outside / "results.tsv").exists()
