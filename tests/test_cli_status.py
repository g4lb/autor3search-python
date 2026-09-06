import pytest

from autor3search_python import results, runstop, state
from autor3search_python.cli import main as cli_main
from tests.conftest import git


@pytest.fixture
def started(git_repo):
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_m.py").write_text(
        "def test_w(benchmark):\n    benchmark(lambda: 1)\n"
    )
    cli_main.main(["init", "-C", str(git_repo)])
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "init")
    cli_main.main(["baseline", "-C", str(git_repo), "-tag", "t1"])
    return git_repo


def test_status_reports_the_run(started, capsys):
    assert cli_main.main(["status", "-C", str(started)]) == 0
    out = capsys.readouterr().out
    assert "t1" in out
    assert "autor3search-python/t1" in out
    # Parenthesized: "not checked out" also contains "checked out" as a plain
    # substring, so that weaker check would pass against either rendering.
    assert "(checked out)" in out
    assert "stop           not requested" in out
    assert "0 run" in out


def test_status_counts_experiments_by_verdict(started, capsys):
    p = started / results.PATH
    for status in ("KEEP", "DISCARD", "DISCARD", "FAIL", "CRASH"):
        results.append(p, results.Row("abc1234", 1.0, 0.0, status, "x"))
    cli_main.main(["status", "-C", str(started)])
    out = capsys.readouterr().out
    assert "5 run" in out
    assert "1 keep" in out and "2 discard" in out and "1 fail" in out and "1 crash" in out
    assert "next is #6" in out


def test_status_reports_a_pending_stop(started, capsys):
    runstop.request_stop(state.state_dir(started, "t1"))
    cli_main.main(["status", "-C", str(started)])
    # The full field, not just "requested": that substring also appears
    # inside "not requested", so a check against it alone would pass even if
    # the code never noticed the pending stop at all.
    assert "stop           requested" in capsys.readouterr().out


def test_status_reports_no_pending_stop_by_default(started, capsys):
    """Paired with the positive case above: together they cannot both pass
    unless the code actually distinguishes the two states."""
    cli_main.main(["status", "-C", str(started)])
    assert "stop           not requested" in capsys.readouterr().out


def test_status_works_from_another_branch_with_an_explicit_tag(started, capsys):
    git(started, "checkout", "-q", "main")
    assert cli_main.main(["status", "-C", str(started), "-tag", "t1"]) == 0
    out = capsys.readouterr().out
    assert "(not checked out)" in out


def test_status_writes_nothing(started):
    sd = state.state_dir(started, "t1")
    before = sorted(p.name for p in sd.iterdir())
    cli_main.main(["status", "-C", str(started)])
    assert sorted(p.name for p in sd.iterdir()) == before
    assert not (started / results.PATH).exists()


def test_status_on_an_unknown_tag_explains(started, capsys):
    assert cli_main.main(["status", "-C", str(started), "-tag", "nope"]) == 2
    assert "baseline" in capsys.readouterr().err


def test_status_survives_a_corrupt_pid_file(started, capsys):
    """R12: status is read-only and a human's go-to when something is wrong —
    it must never crash on a pid file that eval_running finds implausible."""
    sd = state.state_dir(started, "t1")
    (sd / runstop.EVAL_PID_FILE).write_text("not a pid\n")
    assert cli_main.main(["status", "-C", str(started)]) == 0
    out = capsys.readouterr().out
    assert "unknown" in out
