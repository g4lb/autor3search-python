import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from autor3search import runstop


def test_stop_request_lifecycle(tmp_path):
    d = tmp_path / "state"
    assert runstop.stop_requested(d) is False
    runstop.request_stop(d)
    assert runstop.stop_requested(d) is True
    runstop.clear_stop(d)
    assert runstop.stop_requested(d) is False


def test_request_stop_creates_a_missing_state_dir(tmp_path):
    """A human reaching for the brake must never be told the directory does not exist."""
    d = tmp_path / "not" / "there"
    runstop.request_stop(d)
    assert runstop.stop_requested(d) is True


def test_clearing_a_request_never_made_is_not_an_error(tmp_path):
    runstop.clear_stop(tmp_path / "state")


def test_claim_reports_the_running_pid(tmp_path):
    d = tmp_path / "state"
    with runstop.claim_eval(d, 4242):
        pid, running = runstop.eval_running(d)
        assert (pid, running) == (4242, True)
    assert runstop.eval_running(d) == (0, False)


def test_a_second_claim_in_another_process_is_refused(tmp_path):
    """Two concurrent evals would fight over the same pinned worktree."""
    d = tmp_path / "state"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
            import os, time
            from autor3search import runstop
            with runstop.claim_eval({str(d)!r}, os.getpid()):
                print("held", flush=True)
                time.sleep(20)
        """),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(runstop.StopError, match="already running"), runstop.claim_eval(d, 1234):
            pass
    finally:
        holder.kill()
        holder.wait()


def test_a_refused_claim_leaves_the_holders_pid_file_intact(tmp_path):
    """A refused claim must never unlink the live holder's pid file: that
    blinds eval_running and lets a later claim take a fresh inode while the
    holder is still running -- two evals against the same pinned worktree."""
    d = tmp_path / "state"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
            import os, time
            from autor3search import runstop
            with runstop.claim_eval({str(d)!r}, os.getpid()):
                print("held", flush=True)
                time.sleep(20)
        """),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(runstop.StopError, match="already running"), runstop.claim_eval(d, 1234):
            pass
        assert runstop.eval_running(d) == (holder.pid, True)
    finally:
        holder.kill()
        holder.wait()


def test_a_stale_pid_file_does_not_report_a_live_eval(tmp_path):
    """A pid file left by a SIGKILLed eval is a corpse, not a running process."""
    d = tmp_path / "state"
    d.mkdir(parents=True)
    (d / runstop.EVAL_PID_FILE).write_text("999999\n")
    assert runstop.eval_running(d) == (0, False)


@pytest.mark.parametrize("content", ["", "not-a-pid", "0", "1", "-1"])
def test_an_implausible_pid_is_an_error_not_a_guess(tmp_path, content):
    """The value is about to be handed to killpg, where -1 means every process
    the caller may signal. Refusing to guess is the only safe reading."""
    d = tmp_path / "state"
    d.mkdir(parents=True)
    (d / runstop.EVAL_PID_FILE).write_text(content)
    with pytest.raises(runstop.StopError):
        runstop.eval_running(d)


def test_claim_releases_on_an_exception(tmp_path):
    d = tmp_path / "state"
    with pytest.raises(ValueError), runstop.claim_eval(d, 4242):
        raise ValueError("boom")
    assert runstop.eval_running(d) == (0, False)


def test_clear_eval_pid_removes_a_leftover(tmp_path):
    d = tmp_path / "state"
    d.mkdir(parents=True)
    (d / runstop.EVAL_PID_FILE).write_text("999999\n")
    runstop.clear_eval_pid(d)
    assert not (d / runstop.EVAL_PID_FILE).exists()
    runstop.clear_eval_pid(d)  # again, not an error


class _FakeMsvcrt:
    """Stands in for the module this test runner does not have.

    The real cross-process refusal above is what proves the Windows lock on
    Windows (CI runs it there); these fakes prove the far cheaper thing a
    POSIX machine can still check — that the non-POSIX branch reaches for a
    real lock at all, rather than the unconditional success it used to
    report, which let two evals share one pinned worktree.
    """

    LK_NBLCK = 3
    LK_UNLCK = 0

    def __init__(self, *, refuse: bool = False) -> None:
        self.calls: list[tuple[int, int]] = []
        self.offsets: list[int] = []
        self.refuse = refuse

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((mode, nbytes))
        self.offsets.append(os.lseek(fd, 0, os.SEEK_CUR))
        if self.refuse and mode == self.LK_NBLCK:
            raise OSError(13, "another process has locked a portion of the file")


@pytest.fixture
def fake_windows(monkeypatch):
    def install(*, refuse: bool = False) -> _FakeMsvcrt:
        fake = _FakeMsvcrt(refuse=refuse)
        monkeypatch.setattr(runstop, "_POSIX", False)
        monkeypatch.setitem(sys.modules, "msvcrt", fake)
        return fake

    return install


def test_windows_claim_takes_a_real_lock(tmp_path, fake_windows):
    fake = fake_windows()
    with runstop.claim_eval(tmp_path / "state", 4242):
        assert (fake.LK_NBLCK, 1) in fake.calls


def test_windows_claim_is_refused_when_the_lock_is_held(tmp_path, fake_windows):
    """The whole guarantee: a locked pid file means an eval is already running
    against this baseline, and the second one must not start."""
    fake_windows(refuse=True)
    with (
        pytest.raises(runstop.StopError, match="already running"),
        runstop.claim_eval(tmp_path / "state", 4242),
    ):
        pass


def test_windows_eval_running_reports_a_held_claim(tmp_path, fake_windows):
    """`stop --force` asks this question before it signals anything."""
    fake_windows(refuse=True)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / runstop.EVAL_PID_FILE).write_text("4242\n")
    assert runstop.eval_running(tmp_path / "state") == (4242, True)


def test_windows_release_unlocks_and_closes_before_deleting(tmp_path, fake_windows, monkeypatch):
    """Windows refuses to unlink a file that is still open, so the POSIX order
    (unlink first, deliberately, so no one can take a fresh inode) would leave
    the pid file behind on every run. Release, close, then delete."""
    fake_windows()
    order: list[str] = []
    real_close = os.close
    monkeypatch.setattr(runstop, "_unlock", lambda fd: order.append("unlock"))
    monkeypatch.setattr(runstop.os, "close", lambda fd: (order.append("close"), real_close(fd))[1])
    real_unlink = Path.unlink
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda self, **kw: (order.append("unlink"), real_unlink(self, **kw))[1],
    )
    d = tmp_path / "state"
    with runstop.claim_eval(d, 4242):
        pass
    assert order == ["unlock", "close", "unlink"]
    assert not (d / runstop.EVAL_PID_FILE).exists()


def test_windows_locks_a_byte_nothing_needs_to_read(tmp_path, fake_windows):
    """Windows file locks are mandatory, not advisory: the locked range cannot
    be READ by another handle either. Locking byte 0 therefore locked the pid
    text itself, and every eval_running and every refused claim — the paths
    that exist to report which pid holds the run — failed with a permission
    error instead of an answer. The lock has to sit past anything the file
    contains, so the bytes stay readable while the claim is held.
    """
    fake = fake_windows()
    d = tmp_path / "state"
    with runstop.claim_eval(d, 4242):
        assert (d / runstop.EVAL_PID_FILE).stat().st_size < runstop._LOCK_OFFSET
        assert fake.offsets and set(fake.offsets) == {runstop._LOCK_OFFSET}
