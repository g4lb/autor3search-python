"""Snapshots test files at baseline and restores them before every evaluation,
so an agent cannot weaken its own success criteria.

Edits to a frozen file are erased, not argued about. That is deliberate: an
argument is something an agent can win.
"""

from __future__ import annotations

import hashlib
import json
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
    if posixpath.isabs(rel) or (len(rel) > 1 and rel[1] == ":"):
        raise FreezeError(f"frozen path {rel!r} must be relative")
    clean = posixpath.normpath(rel.replace("\\", "/"))
    if clean == ".." or clean.startswith("../"):
        raise FreezeError(f"frozen path {rel!r} escapes the repository root")
    return root / clean


def _is_symlink(path: Path) -> bool:
    """Lstat, not stat: the question is about the path, not its target."""
    try:
        return path.is_symlink()
    except OSError:
        return False


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
        try:
            if dst.read_bytes() == want:
                continue
        except OSError:
            pass  # missing or unreadable: rewrite it
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(want)
        changed.append(rel)
    return changed


def verify(repo_root: str | Path, m: Manifest) -> list[str]:
    """Which frozen files currently differ from baseline. Deleted counts as changed."""
    repo_root = Path(repo_root)
    changed: list[str] = []
    for rel in m.sorted_paths():
        path = _safe_join(repo_root, rel)
        if _is_symlink(path):
            # At least as suspicious as a deletion. Report it rather than
            # following the link to read whatever it points at.
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
