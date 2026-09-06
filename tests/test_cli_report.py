import pytest

from autor3search_python import results
from autor3search_python.cli import main as cli_main
from autor3search_python.cli import report as cli_report


def rows(*specs):
    return [
        results.Row(
            commit=f"c{i}", score=s, best_bench_delta=(s - 1) * 100, status=st, description=d
        )
        for i, (s, st, d) in enumerate(specs)
    ]


def test_cumulative_speedup_is_the_product_of_kept_scores():
    """Each kept score is only that experiment's incremental contribution."""
    got = cli_report.cumulative_speedup(
        rows((0.5, "KEEP", "a"), (0.8, "KEEP", "b"), (1.0, "DISCARD", "c"))
    )
    assert got == pytest.approx(0.4)


def test_cumulative_speedup_ignores_non_keeps():
    assert cli_report.cumulative_speedup(rows((0.1, "DISCARD", "a"))) == pytest.approx(1.0)


def test_cumulative_speedup_of_an_empty_log_is_one():
    assert cli_report.cumulative_speedup([]) == pytest.approx(1.0)


def test_cumulative_speedup_ignores_a_non_positive_score():
    """A hand-edited or torn row must not make the whole summary nonsense."""
    assert cli_report.cumulative_speedup(rows((0.0, "KEEP", "a"), (0.5, "KEEP", "b"))) == (
        pytest.approx(0.5)
    )


def test_report_summarizes(git_repo, capsys):
    p = git_repo / results.PATH
    for row in rows(
        (0.50, "KEEP", "rewrite the loop"),
        (1.00, "DISCARD", "cache it"),
        (1.00, "FAIL", "broke a test"),
        (0.90, "KEEP", "preallocate"),
    ):
        results.append(p, row)
    assert cli_main.main(["report", "-C", str(git_repo)]) == 0
    out = capsys.readouterr().out
    assert "4" in out  # total
    assert "2 keep" in out
    assert "rewrite the loop" in out  # largest win named
    assert "55" in out or "0.45" in out  # cumulative 0.5 * 0.9 = 0.45


def test_report_on_an_empty_log_says_so(git_repo, capsys):
    assert cli_main.main(["report", "-C", str(git_repo)]) == 0
    assert "no experiments" in capsys.readouterr().out.lower()


def test_report_on_a_corrupt_log_names_the_line(git_repo, capsys):
    (git_repo / results.PATH).write_text(results.HEADER + "\nbroken\n")
    assert cli_main.main(["report", "-C", str(git_repo)]) == 2
    assert "line 2" in capsys.readouterr().err
