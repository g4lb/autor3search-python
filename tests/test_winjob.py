"""The Windows half of `runner`'s "a timed-out benchmark takes its whole tree
with it" guarantee. On POSIX a process group carries it; here a job object
does, and the only honest test of that is one that actually runs on Windows —
so the real one below is skipped everywhere else, and CI runs it on
windows-latest.
"""

import os
import subprocess
import sys
import textwrap
import time

import pytest

from autor3search_python import winjob


def test_assign_is_unavailable_off_windows():
    """Callers must be able to degrade to killing the direct child rather than
    crash on a platform with no job objects at all."""
    if os.name == "nt":
        pytest.skip("this asserts the fallback taken where job objects do not exist")
    assert winjob.available() is False
    assert winjob.assign(os.getpid()) is None


@pytest.mark.skipif(os.name != "nt", reason="job objects are a Windows API")
def test_terminating_a_job_kills_a_grandchild(tmp_path):
    """The whole point: a grandchild the harness never spawned itself must not
    outlive the timeout that killed its parent."""
    marker = tmp_path / "grandchild.txt"
    grandchild = textwrap.dedent(f"""
        import time
        time.sleep(5)
        open({str(marker)!r}, "w").write("survived")
    """)
    parent = textwrap.dedent(f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", {grandchild!r}])
        time.sleep(5)
    """)
    proc = subprocess.Popen([sys.executable, "-c", parent])
    try:
        job = winjob.assign(proc.pid)
        assert job is not None
        time.sleep(1.0)  # let the parent get its grandchild started
        job.terminate()
        job.close()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:  # pragma: no cover - only if the job failed
            proc.kill()
    time.sleep(2.0)
    assert not marker.exists(), "a grandchild outlived the job that held it"


@pytest.mark.skipif(os.name != "nt", reason="job objects are a Windows API")
def test_closing_a_job_kills_what_is_still_in_it():
    """`stop --force` relies on this: terminating eval drops the last handle to
    every job it holds, and kill-on-close takes the benchmark tree with it.
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        job = winjob.assign(proc.pid)
        assert job is not None
        job.close()
        assert proc.wait(timeout=10) != 0
    finally:
        if proc.poll() is None:  # pragma: no cover - only if kill-on-close failed
            proc.kill()
