"""stop — ask the run to end.

Three ways to stop, differing only in what happens to the experiment in flight.
Graceful (`stop`) writes a request the agent reads at its next verdict: the
experiment finishes, is scored, its KEEP or DISCARD is applied, and only then
does the loop exit. Nothing is thrown away. `-force` writes the same request and
additionally signals the running eval to abandon the experiment — that
experiment is lost, because nothing was measured, and no results.tsv row is
written for it. Ctrl+C on the agent is equivalent to -force.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys

from autor3search_python import gitx, runstop
from autor3search_python.cli import runctx
from autor3search_python.cli.main import EXIT_OK, EXIT_USAGE


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

    pid, running = runstop.eval_running(ctx.state_dir)
    if running:
        try:
            _signal_group(pid)
            print(f"signalled the running eval (pid {pid}) to abandon its experiment.")
        except (ValueError, OSError) as e:
            print(f"autor3search-python stop: could not signal pid {pid}: {e}", file=sys.stderr)
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
