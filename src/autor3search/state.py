"""Persists a run's reference points, out-of-tree from the repository.

NONE of this may live inside the repository. The agent edits the repository and
runs as the same OS user, so in-tree state would be silently writable by the
very agent it constrains: it could edit the frozen golden copies, delete a key
from the manifest, or — worst — edit the pinned baseline worktree to make the
BASELINE slow, after which every candidate "improves" and every experiment
returns KEEP without optimizing anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

STATE_DIR_NAME = "autor3search-python"
STATE_HOME_ENV = "AUTOR3SEARCH_PYTHON_STATE_HOME"
BRANCH_PREFIX = "autor3search-python/"

BASELINE_FILE = "baseline.json"
WORKTREE_NAME = "baseline-worktree"

# Strict allow-list. Notably absent is any path separator, which alone blocks
# both traversal ("../../etc") and absolute paths: a tag can never be more than
# one path segment.
_VALID_TAG = re.compile(r"^[A-Za-z0-9._-]+$")


class StateError(Exception):
    """Run state that cannot be located, read or trusted."""


def valid_tag(tag: str) -> None:
    """Raise unless `tag` is safe as a filesystem path segment.

    state_dir joins the tag into an out-of-tree path and callers mkdir it
    immediately, long before git's own ref-name rules would reject anything.
    Without this, a tag like "../../../../tmp/evil" reaches mkdir and creates a
    directory wherever the traversal lands.
    """
    if not tag:
        raise StateError("tag must not be empty")
    if tag in (".", ".."):
        raise StateError(f"tag {tag!r} is a directory reference, not a run identifier")
    if not _VALID_TAG.match(tag):
        raise StateError(
            f"tag {tag!r} is not allowed: tags may contain only letters, digits, '.', '_' and '-'"
        )


def branch_for(tag: str) -> str:
    valid_tag(tag)
    return f"{BRANCH_PREFIX}{tag}"


def tag_from_branch(branch: str) -> str | None:
    return branch[len(BRANCH_PREFIX) :] if branch.startswith(BRANCH_PREFIX) else None


def _user_cache_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches"
    if os.name == "nt":  # pragma: no cover
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) if local else Path.home() / "AppData" / "Local"
    xdg = os.environ.get("XDG_CACHE_HOME")
    return Path(xdg) if xdg else Path.home() / ".cache"


def state_home() -> Path:
    """Where every repository's run state lives.

    A relative override is refused rather than resolved: the result would depend
    on the working directory each command was invoked from, so `eval` from a
    subdirectory and `stop` from the repository root would address different
    state for the same run, and the brake would silently miss.
    """
    override = os.environ.get(STATE_HOME_ENV)
    if override:
        p = Path(override)
        if not p.is_absolute():
            raise StateError(
                f"{STATE_HOME_ENV} must be an absolute path, got {override!r}: a relative "
                f"state home would resolve differently depending on where each command runs"
            )
        return p
    return _user_cache_dir() / STATE_DIR_NAME


def state_dir(repo_root: str | Path, tag: str) -> Path:
    """The out-of-tree directory for one repository and run tag.

    Keyed by a hash of the repository's resolved absolute path, so two checkouts
    of the same project never share state.
    """
    valid_tag(tag)
    p = Path(repo_root)
    try:
        resolved = p.resolve(strict=True)
    except OSError:
        resolved = p.absolute()
    key = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    return state_home() / key / tag


@dataclass
class Baseline:
    """A run's reference points. Two, deliberately kept apart.

    `commit` is the FROZEN anchor: the commit the run started from, recorded
    once and never changed. The frozen snapshots are relative to it and the
    scope gate diffs against it, so the gate re-validates the FULL accumulated
    diff on every eval rather than trusting that anything already banked as a
    KEEP must have been in scope.

    `measure_commit` is the ADVANCING pointer: what the pinned worktree is
    checked out to, and what each candidate is measured against. It starts equal
    to `commit` and re-points to the candidate's own commit after every KEEP, so
    each eval answers "did THIS change help", not "is the tree better than when
    the run started".

    Collapsing them either freezes the measurement baseline forever — letting a
    later no-op coast to KEEP on an earlier win — or lets the scope gate's
    comparison point drift, letting out-of-scope edits launder themselves into
    accepted state after a single eval.
    """

    tag: str
    branch: str
    commit: str
    measure_commit: str
    created_at: str  # ISO 8601, UTC
    benchmarks: tuple[str, ...]
    config_sha256: str

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        doc = asdict(self)
        doc["benchmarks"] = list(self.benchmarks)
        p.write_text(json.dumps(doc, indent=2) + "\n")


def load_baseline(path: str | Path) -> Baseline:
    p = Path(path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise StateError(
            f"no baseline at {p}: run 'autor3search-python baseline -tag <tag>' first"
        ) from e
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise StateError(f"read baseline {p}: {e}") from e
    if not isinstance(doc, dict):
        raise StateError(f"baseline {p}: not a JSON object")
    try:
        commit = str(doc["commit"])
        return Baseline(
            tag=str(doc["tag"]),
            branch=str(doc["branch"]),
            commit=commit,
            # A record written before measure_commit existed has no such field.
            # Fall back to commit — what a fresh baseline would have started it
            # at — rather than leaving it empty and failing the first eval's
            # worktree-integrity check.
            measure_commit=str(doc.get("measure_commit") or commit),
            created_at=str(doc["created_at"]),
            benchmarks=tuple(doc.get("benchmarks") or ()),
            config_sha256=str(doc["config_sha256"]),
        )
    except KeyError as e:
        raise StateError(f"baseline {p}: missing field {e}") from e
