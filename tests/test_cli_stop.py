import signal

import pytest

from autor3search_python import runstop, state
from autor3search_python.cli import main as cli_main
from autor3search_python.cli import stop as cli_stop
from autor3search_python.cli.main import EXIT_USAGE
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


def test_stop_writes_a_request(started, capsys):
    assert cli_main.main(["stop", "-C", str(started)]) == 0
    assert runstop.stop_requested(state.state_dir(started, "t1")) is True
    assert "after the current experiment" in capsys.readouterr().out


def test_clear_cancels_it(started):
    cli_main.main(["stop", "-C", str(started)])
    assert cli_main.main(["stop", "-C", str(started), "-clear"]) == 0
    assert runstop.stop_requested(state.state_dir(started, "t1")) is False


def test_stop_is_idempotent(started):
    cli_main.main(["stop", "-C", str(started)])
    assert cli_main.main(["stop", "-C", str(started)]) == 0


def test_force_reports_repository_state_without_changing_it(started, capsys, monkeypatch):
    monkeypatch.setattr(runstop, "eval_running", lambda d: (0, False))
    head_before = git(started, "rev-parse", "HEAD")
    assert cli_main.main(["stop", "-C", str(started), "-force"]) == 0
    out = capsys.readouterr().out
    assert "git reset --hard HEAD~1" in out
    assert git(started, "rev-parse", "HEAD") == head_before


@pytest.mark.parametrize("pid_contents", ["1\n", "not a pid\n"])
def test_force_survives_a_corrupt_pid_file(started, capsys, pid_contents):
    """This is the emergency brake: it must not crash on state that is
    already broken, which is exactly when a human reaches for -force."""
    sd = state.state_dir(started, "t1")
    (sd / runstop.EVAL_PID_FILE).write_text(pid_contents)
    assert cli_main.main(["stop", "-C", str(started), "-force"]) == 0
    out = capsys.readouterr().out
    assert "git reset --hard HEAD~1" in out  # the repository-state report still runs
    assert runstop.stop_requested(sd) is True  # the stop request still landed


def test_force_signals_the_running_eval(started, monkeypatch, capsys):
    """Whichever mechanism this platform uses, -force must reach the eval:
    its process group on POSIX, the process itself where there is no signal
    it could act on. Both are patched so the assertion is about the pid
    reaching one of them, not about which platform is running the test."""
    monkeypatch.setattr(runstop, "eval_running", lambda d: (4242, True))
    signalled = []
    monkeypatch.setattr(cli_stop, "_signal_group", lambda pid: signalled.append(pid))
    monkeypatch.setattr(cli_stop, "_terminate", lambda pid: signalled.append(pid))
    cli_main.main(["stop", "-C", str(started), "-force"])
    assert signalled == [4242]


@pytest.mark.parametrize("pid", [0, 1, -1, -5])
def test_group_signal_target_refuses_an_unsafe_pid(pid):
    """A target of -1 means every process the caller may signal, not one group."""
    with pytest.raises(ValueError):
        cli_stop.group_signal_target(pid)


def test_group_signal_target_negates_a_real_pid():
    assert cli_stop.group_signal_target(4242) == -4242


def test_force_on_windows_terminates_the_eval_outright(started, monkeypatch, capsys):
    """This machine cannot actually run Windows, so this fakes only os.name and
    checks our own branch fires. Windows has no signal an eval can act on
    mid-benchmark, so -force there ends the process instead of asking it to
    stop — and the process, not its negated pid: os.kill takes a real pid on
    that platform, and a group target of -4242 would be nonsense.

    The eval's job objects die with it, so the benchmark tree goes too."""
    monkeypatch.setattr(cli_stop, "_POSIX", False)
    monkeypatch.setattr(runstop, "eval_running", lambda d: (4242, True))
    killed = []
    monkeypatch.setattr(cli_stop.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    code = cli_main.main(["stop", "-C", str(started), "-force"])
    out = capsys.readouterr().out
    assert code == 0
    assert killed == [(4242, signal.SIGTERM)]
    assert "terminated the running eval (pid 4242)" in out
    assert "immediate" in out
    assert runstop.stop_requested(state.state_dir(started, "t1")) is True


def test_force_on_windows_reports_a_termination_it_could_not_perform(started, monkeypatch, capsys):
    """An eval that exited between the claim check and the kill, or one this
    user may not touch, must be reported rather than swallowed — the human is
    about to assume nothing is running."""
    monkeypatch.setattr(cli_stop, "_POSIX", False)
    monkeypatch.setattr(runstop, "eval_running", lambda d: (4242, True))

    def boom(pid, sig):
        raise PermissionError("access denied")

    monkeypatch.setattr(cli_stop.os, "kill", boom)
    code = cli_main.main(["stop", "-C", str(started), "-force"])
    assert code == EXIT_USAGE
    assert "could not stop pid 4242" in capsys.readouterr().err


def test_force_off_posix_is_unaffected_when_no_eval_is_running(started, monkeypatch):
    monkeypatch.setattr(cli_stop, "_POSIX", False)
    monkeypatch.setattr(runstop, "eval_running", lambda d: (0, False))
    assert cli_main.main(["stop", "-C", str(started), "-force"]) == 0
