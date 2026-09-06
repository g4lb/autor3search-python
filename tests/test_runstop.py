import subprocess
import sys
import textwrap

import pytest

from autor3search_python import runstop


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
            from autor3search_python import runstop
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
