"""Coordination between the human's shell and the agent's loop.

Both files live out-of-tree alongside the rest of the run's state, for the same
reason everything else there does: a sentinel inside the repository would dirty
the working tree the agent commits from, and would have to be special-cased in
the scope gate and in .gitignore. Out here it is invisible to every gate and to
git, and both processes still find it because the state directory is derived
from the repository path and run tag.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path

STOP_REQUEST_FILE = "stop.request"
EVAL_PID_FILE = "eval.pid"

_POSIX = os.name == "posix"


class StopError(Exception):
    """A stop sentinel or eval claim that cannot be read or taken."""


def request_stop(state_dir: str | Path) -> None:
    """Ask the run to end after the current experiment.

    Creating the directory if missing is deliberate: a human reaching for the
    brake should never be told it does not exist yet. A request against a tag
    whose baseline never finished is harmless — it sits there until a run reads
    it, or clear_stop removes it.
    """
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / STOP_REQUEST_FILE).write_text("stop requested\n")


def clear_stop(state_dir: str | Path) -> None:
    """Cancel a pending request. Clearing one never made is not an error."""
    (Path(state_dir) / STOP_REQUEST_FILE).unlink(missing_ok=True)


def stop_requested(state_dir: str | Path) -> bool:
    """Whether a stop is pending.

    Returns a bool rather than raising, because every caller wants the same
    answer for an unreadable sentinel as for an absent one: carry on. A stop
    that cannot be read must never abort a run by itself — the human still has
    --force and Ctrl+C.
    """
    return (Path(state_dir) / STOP_REQUEST_FILE).exists()


def _try_lock(fd: int) -> bool:
    if not _POSIX:  # pragma: no cover - Windows degrades to existence only
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if not _POSIX:  # pragma: no cover
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _read_pid(path: Path) -> tuple[int, bool]:
    """(pid, present). An unparseable or implausible pid is an error, not absence."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return 0, False
    except OSError as e:
        raise StopError(f"read {path}: {e}") from e
    try:
        pid = int(text)
    except ValueError as e:
        raise StopError(f"read {path}: {text!r} is not a pid") from e
    # pid 1 is rejected alongside the impossible ones: `stop --force` signals
    # the pid's process GROUP, and a target of -1 means every process the caller
    # may signal rather than one group.
    if pid <= 1:
        raise StopError(f"read {path}: pid {pid} is not a process this command will signal")
    return pid, True


@contextlib.contextmanager
def claim_eval(state_dir: str | Path, pid: int) -> Iterator[None]:
    """Mark an eval as running, releasing on exit however the block ends."""
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / EVAL_PID_FILE
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if not _try_lock(fd):
            try:
                other, _ = _read_pid(path)
            except StopError:
                other = 0
            raise StopError(
                f"another autor3search-python eval (pid {other}) is already running for this run"
            )
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{pid}\n".encode(), 0)
        yield
    finally:
        # Remove before closing: closing drops the lock, and a concurrent
        # eval_running that acquired it in between would otherwise read a pid
        # file this process is about to delete.
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        os.close(fd)


def eval_running(state_dir: str | Path) -> tuple[int, bool]:
    """(pid, running). `running` is False when the claim is not held — including
    when a pid file was left behind by an eval that died without releasing it."""
    path = Path(state_dir) / EVAL_PID_FILE
    pid, present = _read_pid(path)
    if not present:
        return 0, False
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return 0, False
    except OSError as e:
        raise StopError(f"open {path}: {e}") from e
    try:
        if _try_lock(fd):
            # We took it, so nobody held it: the file is a leftover. Drop it
            # again immediately — this is a query, not a claim.
            _unlock(fd)
            return 0, False
        return pid, True
    finally:
        os.close(fd)


def clear_eval_pid(state_dir: str | Path) -> None:
    """Remove a pid file left behind by an eval that died. Absent is fine."""
    (Path(state_dir) / EVAL_PID_FILE).unlink(missing_ok=True)
