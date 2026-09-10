"""version — report which build of the harness produced a results.tsv row."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from autor3search import __version__
from autor3search.cli.main import EXIT_OK


def _git(repo: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def _checkout_description() -> str | None:
    """Describe the source checkout this module was imported from, if it is one."""
    repo = Path(__file__).resolve().parent.parent.parent.parent
    if not (repo / ".git").exists():
        return None
    commit = _git(repo, "rev-parse", "--short=7", "HEAD")
    if not commit:
        return None
    dirty = _git(repo, "status", "--porcelain")
    return f"{commit} dirty" if dirty else commit


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="autor3search-python version", add_help=True)
    parser.parse_args(args)
    checkout = _checkout_description()
    if checkout:
        print(f"autor3search-python {__version__} (checkout {checkout})")
    else:
        print(f"autor3search-python {__version__}")
    return EXIT_OK
