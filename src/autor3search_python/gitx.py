"""The git subcommands the harness needs."""

from __future__ import annotations

import subprocess
from pathlib import Path

_TIMEOUT = 120


class GitError(Exception):
    """A git invocation that failed."""


def _git(d: str | Path, *args: str) -> str:
    """Run a git subcommand and return its trimmed stdout.

    stdout and stderr are captured separately so that stderr chatter on an
    otherwise successful command — lfs filter warnings, advice hints, a user's
    own hooks — never gets parsed as part of the result. stderr appears in the
    error message only when the command fails.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(d),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except OSError as e:
        raise GitError(f"git {' '.join(args)}: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(args)}: timed out after {_TIMEOUT}s") from e
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: exit {proc.returncode}\n{proc.stderr.strip()}")
    return proc.stdout.strip()


def root(d: str | Path) -> str:
    return _git(d, "rev-parse", "--show-toplevel")


def head_commit(d: str | Path) -> str:
    return _git(d, "rev-parse", "--short=7", "HEAD")


def head_subject(d: str | Path) -> str:
    """The first line of HEAD's message — never the body or its trailers."""
    return _git(d, "log", "-1", "--format=%s")


def current_branch(d: str | Path) -> str:
    return _git(d, "rev-parse", "--abbrev-ref", "HEAD")


def branch_exists(d: str | Path, name: str) -> bool:
    try:
        _git(d, "show-ref", "--verify", "--quiet", f"refs/heads/{name}")
    except GitError:
        return False
    return True


def create_branch(d: str | Path, name: str) -> None:
    _git(d, "checkout", "-b", name)


def checkout(d: str | Path, ref: str) -> None:
    _git(d, "checkout", ref)


def delete_branch(d: str | Path, name: str) -> None:
    _git(d, "branch", "-D", name)


def is_clean(d: str | Path) -> bool:
    return _git(d, "status", "--porcelain") == ""


def changed_since(d: str | Path, commit: str) -> list[str]:
    """Repo-relative paths modified since `commit`, including untracked files.

    Both calls use -z. Without it git quotes and octal-escapes any path with
    non-ASCII bytes, quotes or backslashes (``"internal/caf\\303\\251.py"``),
    which would then fail scope matching.
    """
    tracked = _git(d, "diff", "--name-only", "-z", commit)
    untracked = _git(d, "ls-files", "-z", "--others", "--exclude-standard")
    out: set[str] = set()
    for block in (tracked, untracked):
        out.update(e for e in block.split("\0") if e)
    return sorted(out)


def add_worktree(repo: str | Path, path: str | Path, commit: str) -> None:
    _git(repo, "worktree", "add", "--detach", str(path), commit)


def checkout_detached(d: str | Path, commit: str) -> None:
    """Move an existing checkout to `commit`, detached.

    -f discards stray changes first. Nothing should ever be modifying the pinned
    baseline worktree, but checking out over a dirty tree without -f would fail
    instead of re-pointing it.
    """
    _git(d, "checkout", "-f", "--detach", commit)


def remove_worktree(repo: str | Path, path: str | Path) -> None:
    """Delete a worktree created by add_worktree.

    --force silently discards uncommitted changes in the target, so callers must
    only ever point this at a worktree the harness itself created.
    """
    _git(repo, "worktree", "remove", "--force", str(path))
