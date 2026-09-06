"""Files init writes into a repository, shipped as package data."""

from __future__ import annotations

from importlib.resources import files


def program_md() -> str:
    """The agent's instruction set."""
    return (files(__package__) / "program.md").read_text(encoding="utf-8")
