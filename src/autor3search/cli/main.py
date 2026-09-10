"""Command dispatch. Holds no decision logic — every command lives in its own module."""

from __future__ import annotations

import sys
import traceback
from collections.abc import Callable

EXIT_OK = 0
EXIT_DISCARD = 1
EXIT_USAGE = 2
EXIT_CRASH = 3

COMMANDS: dict[str, str] = {
    "init": "scan the repo, write config.toml and program.md",
    "doctor": "check whether this machine can measure reliably",
    "baseline": "freeze tests and pin the commit this run measures against",
    "profile": "run the declared benchmarks under cProfile and tracemalloc",
    "eval": "run one experiment: gate, measure, score",
    "status": "print where a run is (read-only)",
    "stop": "ask the run to end after the current experiment",
    "report": "summarize results.tsv",
    "version": "print which build of the harness is running",
}


def _load(name: str) -> Callable[[list[str]], int]:
    from importlib import import_module

    return import_module(f"autor3search.cli.{name}").run


def usage(stream) -> None:
    print("usage: autor3search-python <command> [flags]", file=stream)
    print("", file=stream)
    print("commands:", file=stream)
    width = max(len(n) for n in COMMANDS)
    for name, blurb in COMMANDS.items():
        print(f"  {name.ljust(width)}  {blurb}", file=stream)
    print("", file=stream)
    print(
        "every command except 'version' accepts -C <dir> to run against another repository",
        file=stream,
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        usage(sys.stderr)
        return EXIT_USAGE
    cmd, rest = args[0], args[1:]
    if cmd in ("-h", "--help", "help"):
        usage(sys.stdout)
        return EXIT_OK
    if cmd not in COMMANDS:
        print(f"autor3search-python: unknown command {cmd!r}", file=sys.stderr)
        usage(sys.stderr)
        return EXIT_USAGE
    try:
        return _load(cmd)(rest)
    except Exception:
        # The last line of defence, and it exists for one number: an uncaught
        # exception leaves Python exiting 1, which is DISCARD in this tool's
        # vocabulary. An unattended overnight loop would read "your change was
        # merely unimpressive", `git reset --hard HEAD~1`, and keep going
        # against a harness that can no longer decide anything — forever, and
        # confidently. Exit 3 instead: CRASH is the branch that means "the tool
        # is broken", and the agent stops rather than discarding good work.
        #
        # Deliberately broad, and deliberately not a list of exception types:
        # eval already names the nine it expects, and this catches the ones
        # nobody predicted — a TypeError from a refactor, a RecursionError, a
        # MemoryError. KeyboardInterrupt and SystemExit are BaseException and
        # still pass through, so Ctrl+C keeps eval's own ABORTED handling.
        traceback.print_exc(file=sys.stderr)
        print(
            f"autor3search-python: the {cmd!r} command crashed — this is a harness "
            f"malfunction, not a verdict on your change",
            file=sys.stderr,
        )
        return EXIT_CRASH


if __name__ == "__main__":
    raise SystemExit(main())
