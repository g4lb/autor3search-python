"""The one place that answers "is it safe to read or write through this path".

Reading and writing follow symlinks. A path this process opens for writing
that turns out to be a symlink — or that sits inside a symlinked ancestor
directory — writes through to wherever that link points, which can be
anywhere the OS user can reach: outside the repository entirely.

`freeze.py` needed this first, for frozen test files. `results.py` (the
results log), `cli/eval.py` (`run.log`) and `profile.py` (the profile output
directory) all open a path whose NAME the agent knows in advance and cannot
get the scope gate to see once it is gitignored — so the same tampering
applies to every one of them. This module exists so the check lives in
exactly one place: it was previously implemented once, inside `freeze.py`,
and every other caller had to remember to copy it correctly or go unguarded.

Every caller must run `ensure_contained` immediately before the read or write
it guards. A raise afterward, once bytes have already landed outside the
repository, is worthless.
"""

from __future__ import annotations

import os
from pathlib import Path


class ContainmentError(Exception):
    """A path is a symlink, or resolves outside its expected root through one."""


def is_symlink(path: Path) -> bool:
    """Lstat, not stat: the question is about the path itself, not its target."""
    try:
        return path.is_symlink()
    except OSError:
        return False


def escapes_root(root: Path, path: Path) -> bool:
    """Whether path's fully-resolved location falls outside root.

    `is_symlink` only asks about the final path component. A symlinked
    ancestor directory walks straight past that check: the path itself looks
    like an ordinary file, but the directory it lives in points elsewhere, so
    every read or write through it lands wherever that directory really is.
    `realpath` resolves every symlink in the chain, not just the last one,
    and works even when `path` itself does not exist yet — the common case
    for a log file or a profile output on its very first write.
    """
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    return path_real != root_real and not path_real.startswith(root_real + os.sep)


def ensure_contained(root: Path, path: Path, label: str, verb: str) -> None:
    """Raise ContainmentError if `path` is itself a symlink, or escapes `root`
    through some symlinked ancestor directory.

    `label` names the path in the message (typically the repo-relative form);
    `verb` names the operation being guarded ("open", "restore", "profile", …).
    """
    if is_symlink(path):
        raise ContainmentError(
            f"{verb} {label}: refusing to follow a symlink at this path — the target could "
            f"be anywhere outside {root}"
        )
    if escapes_root(root, path):
        raise ContainmentError(
            f"{verb} {label}: resolves outside {root} through a symlinked ancestor directory; "
            f"refusing to touch it, which could reach a file outside the repository"
        )
