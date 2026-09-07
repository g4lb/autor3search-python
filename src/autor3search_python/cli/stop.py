"""stop — ask the run to end.

Three ways to stop, differing only in what happens to the experiment in flight.
Graceful (`stop`) writes a request the agent reads at its next verdict: the
experiment finishes, is scored, its KEEP or DISCARD is applied, and only then
does the loop exit. Nothing is thrown away. `-force` writes the same request and
additionally signals the running eval to abandon the experiment — that
experiment is lost, because nothing was measured, and no results.tsv row is
written for it. Ctrl+C on the agent is equivalent to -force.

Off POSIX, -force is immediate rather than a request: there is no signal an
eval can act on mid-benchmark, so the process is ended instead of asked, and it
does not get to report what it abandoned. Plain `stop` behaves identically
everywhere.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys

from autor3search_python import gitx, runstop
from autor3search_python.cli import runctx
from autor3search_python.cli.main import EXIT_OK, EXIT_USAGE

# Computed once, same as runstop._POSIX: it selects between the two ways
# -force can reach a running eval — `_signal_group` calls os.killpg, which
# does not exist off this platform, and `_terminate` ends the process instead.
_POSIX = os.name == "posix"


def group_signal_target(pid: int) -> int:
    """The kill target for a process GROUP, refusing every unsafe value.

    kill(2) reads -1 as "every process the caller may signal" rather than as one
    group, so a pid of 1 would turn a wedged experiment into a session-wide
    kill. runstop already refuses these when reading the file; refusing again at
    the syscall costs nothing and closes the gap if that ever changes.
    """
    if pid <= 1:
        raise ValueError(f"pid {pid} is not a process this command will signal")
    return -pid


def _signal_group(pid: int) -> None:
    os.killpg(group_signal_target(pid), signal.SIGINT)


def _terminate(pid: int) -> None:
    """End the eval outright, for a platform with no signal it can act on.

    os.kill is TerminateProcess on Windows: there is no SIGINT a benchmark
    loop could notice mid-round, so the process is stopped rather than asked.
    It takes the pid itself, never the negated one a process group needs.
    Everything the eval was measuring dies with it — the job objects holding
    each benchmark's process tree are kill-on-close, and the last handle to
    them goes when the eval does.
    """
    if pid <= 1:
        raise ValueError(f"pid {pid} is not a process this command will signal")
    os.kill(pid, signal.SIGTERM)


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="autor3search-python stop")
    parser.add_argument("-C", dest="directory", default=".", help="repository root")
    parser.add_argument("-tag", "--tag", dest="tag", default=None, help="run identifier")
    parser.add_argument(
        "-clear",
        "--clear",
        dest="clear",
        action="store_true",
        help="cancel a pending stop request",
    )
    parser.add_argument(
        "-force",
        "--force",
        dest="force",
        action="store_true",
        help="also signal the running eval to abandon the current experiment",
    )
    opts = parser.parse_args(args)

    try:
        ctx = runctx.resolve(opts.directory, opts.tag)
    except runctx.ContextError as e:
        print(f"autor3search-python stop: {e}", file=sys.stderr)
        return EXIT_USAGE

    if opts.clear:
        runstop.clear_stop(ctx.state_dir)
        print(f"stop request cleared for run {ctx.tag!r}; the loop may continue.")
        return EXIT_OK

    runstop.request_stop(ctx.state_dir)
    if not opts.force:
        print(f"stop requested for run {ctx.tag!r}.")
        print(
            "The agent will finish and score the experiment it is running, apply that "
            "verdict, then leave the loop after the current experiment."
        )
        print("To cancel:  autor3search-python stop -clear")
        return EXIT_OK

    # This is the emergency brake: it must never crash on state that is
    # already broken, which is exactly when a human reaches for -force. A pid
    # file eval_running finds implausible is reported as an unknown eval
    # state, not signalled (there is nothing safe to signal), and cleared so
    # it does not poison the next command — the same shape as R12 for status.
    try:
        pid, running = runstop.eval_running(ctx.state_dir)
    except runstop.StopError:
        print(
            "an eval may be running but its pid file is unreadable; it was not signalled. "
            "The stop request is written and will be read next time."
        )
        runstop.clear_eval_pid(ctx.state_dir)
    else:
        if running:
            try:
                if _POSIX:
                    _signal_group(pid)
                    print(f"signalled the running eval (pid {pid}) to abandon its experiment.")
                else:
                    # `_signal_group` would call os.killpg, which does not exist
                    # here at all (an AttributeError traceback, not a refusal),
                    # so the branch is taken before it is ever reached.
                    _terminate(pid)
                    print(f"terminated the running eval (pid {pid}) and everything it started.")
                    print(
                        "On this platform -force is immediate rather than a request: there is "
                        "no signal a benchmark can act on mid-round, so the eval was ended "
                        "rather than asked, and did not get to report what it abandoned."
                    )
            except (ValueError, OSError) as e:
                print(f"autor3search-python stop: could not stop pid {pid}: {e}", file=sys.stderr)
                return EXIT_USAGE
        else:
            print("no eval is running; the stop request is written and will be read next time.")
            runstop.clear_eval_pid(ctx.state_dir)

    # Report the state this leaves the repository in. Deliberately does not
    # change it: dropping a commit is the human's call, not this command's.
    print()
    print("The abandoned experiment measured nothing, so no results.tsv row was written.")
    print("Every commit kept before it is untouched. The repository is now at:")
    try:
        print(f"  branch  {gitx.current_branch(ctx.root)}")
        print(f"  commit  {gitx.head_commit(ctx.root)}  {gitx.head_subject(ctx.root)}")
    except gitx.GitError as e:
        print(f"  (could not read git state: {e})")
    print()
    print("If that commit is the abandoned experiment, drop it yourself with:")
    print("  git reset --hard HEAD~1")
    return EXIT_OK
