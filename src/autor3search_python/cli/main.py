"""Command dispatch. Holds no decision logic — every command lives in its own module."""

from __future__ import annotations

import sys
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

    return import_module(f"autor3search_python.cli.{name}").run


def usage(stream) -> None:
    print("usage: autor3search-python <command> [flags]", file=stream)
    print("", file=stream)
    print("commands:", file=stream)
    width = max(len(n) for n in COMMANDS)
    for name, blurb in COMMANDS.items():
        print(f"  {name.ljust(width)}  {blurb}", file=stream)
    print("", file=stream)
    print("every command accepts -C <dir> to run against another repository", file=stream)


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
    return _load(cmd)(rest)


if __name__ == "__main__":
    raise SystemExit(main())
