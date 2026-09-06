"""Snapshots test files at baseline and restores them before every evaluation,
so an agent cannot weaken its own success criteria.

Edits to a frozen file are erased, not argued about. That is deliberate: an
argument is something an agent can win.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

STORE_DIR = "frozen"
MANIFEST_PATH = "frozen/manifest.json"


class FreezeError(Exception):
    """A frozen store or manifest that cannot be used."""


class SymlinkError(FreezeError):
    """A frozen path is a symlink.

    Reading and writing follow symlinks, so a symlinked destination writes
    through to wherever it points — potentially outside the repository
    entirely. Snapshot and restore refuse rather than follow, so a caller can
    tell tampering from an ordinary I/O failure.
    """


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_join(root: Path, rel: str) -> Path:
    """Join rel onto root, refusing anything that would escape it."""
    # Backslashes must be normalized to "/" BEFORE the absolute-path check:
    # isabs() only recognizes the leading-slash form, so "\etc\passwd" reads as
    # relative, and Path.__truediv__ then discards root entirely once the
    # right-hand operand turns out to be absolute after normalization.
    normalized = rel.replace("\\", "/")
    if posixpath.isabs(normalized) or (len(normalized) > 1 and normalized[1] == ":"):
        raise FreezeError(f"frozen path {rel!r} must be relative")
    clean = posixpath.normpath(normalized)
    if clean == ".." or clean.startswith("../"):
        raise FreezeError(f"frozen path {rel!r} escapes the repository root")
    return root / clean


def _is_symlink(path: Path) -> bool:
    """Lstat, not stat: the question is about the path, not its target."""
    try:
        return path.is_symlink()
    except OSError:
        return False


def _escapes_root(root: Path, path: Path) -> bool:
    """Whether path's fully-resolved location falls outside root.

    _is_symlink only asks about the final path component. A symlinked
    ancestor directory walks straight past that check: the path itself looks
    like an ordinary file, but the directory it lives in points elsewhere, so
    every read or write through it lands wherever that directory really is.
    realpath resolves every symlink in the chain, not just the last one.
    """
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    return path_real != root_real and not path_real.startswith(root_real + os.sep)


def _ensure_contained(root: Path, path: Path, rel: str, verb: str) -> None:
    """Raise SymlinkError if path resolves outside root through some ancestor.

    Must be called before any read or write through `path`: a SymlinkError
    raised after the bytes have already landed outside the repository is
    worthless.
    """
    if _escapes_root(root, path):
        raise SymlinkError(
            f"{verb} {rel}: resolves outside {root} through a symlinked ancestor "
            f"directory; refusing to touch it, which could reach a file outside "
            f"the repository"
        )


@dataclass
class Manifest:
    """Repo-relative path -> sha256 at baseline time."""

    files: dict[str, str] = field(default_factory=dict)

    def sorted_paths(self) -> list[str]:
        return sorted(self.files)

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"files": self.files}, indent=2, sort_keys=True) + "\n")


def load_manifest(path: str | Path) -> Manifest:
    p = Path(path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise FreezeError(f"no frozen manifest at {p}: run 'autor3search-python baseline'") from e
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise FreezeError(f"read manifest {p}: {e}") from e
    files = doc.get("files") if isinstance(doc, dict) else None
    if files is None:
        files = {}
    if not isinstance(files, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in files.items()
    ):
        raise FreezeError(f"manifest {p}: 'files' must map paths to hashes")
    return Manifest(files=files)


def snapshot(repo_root: str | Path, store_dir: str | Path, files: Sequence[str]) -> Manifest:
    """Copy each file into the store and record its hash."""
    repo_root, store_dir = Path(repo_root), Path(store_dir)
    m = Manifest()
    for rel in files:
        src = _safe_join(repo_root, rel)
        if _is_symlink(src):
            raise SymlinkError(
                f"snapshot {rel}: symlinked test files are unsupported because the harness "
                f"cannot guarantee that restoring them stays inside the repository"
            )
        _ensure_contained(repo_root, src, rel, "snapshot")
        try:
            data = src.read_bytes()
        except OSError as e:
            raise FreezeError(f"snapshot {rel}: {e}") from e
        dst = _safe_join(store_dir, rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        m.files[rel] = _hash(data)
    return m


def restore(repo_root: str | Path, store_dir: str | Path, m: Manifest) -> list[str]:
    """Rewrite every frozen file from the store. Returns the paths it changed."""
    repo_root, store_dir = Path(repo_root), Path(store_dir)
    changed: list[str] = []
    for rel in m.sorted_paths():
        src = _safe_join(store_dir, rel)
        dst = _safe_join(repo_root, rel)
        try:
            want = src.read_bytes()
        except OSError as e:
            raise FreezeError(f"restore {rel}: {e}") from e
        if _is_symlink(dst):
            raise SymlinkError(
                f"restore {rel}: a frozen test file was replaced by a symlink; refusing to "
                f"write through it, which could reach a file outside the repository"
            )
        # Before any read or write through dst: a symlinked ANCESTOR directory
        # (not dst itself) passes the check above but still writes through to
        # wherever that directory really points.
        _ensure_contained(repo_root, dst, rel, "restore")
        try:
            if dst.read_bytes() == want:
                continue
        except OSError:
            pass  # missing or unreadable: rewrite it
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(want)
        except OSError as e:
            raise FreezeError(f"restore {rel}: {e}") from e
        changed.append(rel)
    return changed


def verify(repo_root: str | Path, m: Manifest) -> list[str]:
    """Which frozen files currently differ from baseline. Deleted counts as changed."""
    repo_root = Path(repo_root)
    changed: list[str] = []
    for rel in m.sorted_paths():
        path = _safe_join(repo_root, rel)
        if _is_symlink(path) or _escapes_root(repo_root, path):
            # At least as suspicious as a deletion. Report it rather than
            # following the link (or the symlinked ancestor directory) to
            # read whatever it actually points at.
            changed.append(rel)
            continue
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            changed.append(rel)
            continue
        except OSError as e:
            raise FreezeError(f"verify {rel}: {e}") from e
        if _hash(data) != m.files[rel]:
            changed.append(rel)
    return changed
