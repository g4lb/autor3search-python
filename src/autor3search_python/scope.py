"""Decides which files an agent is allowed to modify."""

from __future__ import annotations

import posixpath
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class _Rule:
    prefix: str  # normalized directory prefix; "" is the repository root
    recursive: bool


def _normalize(raw: str) -> _Rule | None:
    """Compile one pattern. Returns None for a blank entry, which must match nothing."""
    text = raw.strip()
    if not text:
        return None
    if text.startswith("./"):
        text = text[2:]
    recursive = False
    for suffix in ("/...", "/**"):
        if text.endswith(suffix):
            text, recursive = text[: -len(suffix)], True
            break
    else:
        if text in ("...", "**"):
            text, recursive = "", True
        elif text.endswith("/"):
            text, recursive = text.rstrip("/"), True
    prefix = posixpath.normpath(text) if text else ""
    if prefix == ".":
        prefix = ""
    return _Rule(prefix=prefix, recursive=recursive)


class Matcher:
    """Tests repo-relative paths against a set of scope patterns.

    Accepts both spellings a user might reach for: Go-style ``./...`` and
    ``src/...``, and Python-familiar ``src/**``, ``src/`` and ``src``. A bare
    directory name without a trailing marker is non-recursive, matching only
    that directory's direct children.
    """

    def __init__(self, patterns: Sequence[str]) -> None:
        self._rules = [r for r in (_normalize(p) for p in patterns) if r is not None]

    def match(self, rel: str) -> bool:
        path = rel[2:] if rel.startswith("./") else rel
        path = posixpath.normpath(path)
        parent = posixpath.dirname(path)
        for rule in self._rules:
            if rule.recursive:
                if rule.prefix == "" or path == rule.prefix or path.startswith(rule.prefix + "/"):
                    return True
                continue
            if parent == rule.prefix or (rule.prefix == "" and parent in ("", ".")):
                return True
        return False
