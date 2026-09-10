"""Finds benchmarks and frozen files by parsing, never by importing.

AST-based on purpose: discovery has to work on a tree that does not import —
`init` runs before anything is known to be sound, and `eval`'s new-test-file
gate runs against whatever the agent just wrote.
"""

from __future__ import annotations

import ast
import fnmatch
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = frozenset({".git", "__pycache__", ".venv", "venv", "node_modules", "build", "dist"})

TEST_FILE_GLOBS = ("test_*.py", "*_test.py")
CONFTEST = "conftest.py"

# Every file pytest reads its own configuration from, at the rootdir, in
# pytest's own `locate_config` search order. ONE list, because two modules need
# it for two different reasons and they must never drift apart: doctor scans
# them for coverage in addopts, and the scope gate rejects edits to them.
#
# It has now been wrong twice, the same way both times — a name pytest reads
# that we did not list is a working route to a fabricated 90% "improvement",
# because an addopts line can swap the benchmark timer or -k its way past the
# correctness gate. First doctor knew four names and the gate knew two; then
# both knew four and pytest read seven. Check this against pytest's own
# `locate_config` when upgrading pytest, and add the name here — both consumers
# pick it up with no second edit.
PYTEST_CONFIG_FILES = (
    "pytest.toml",
    ".pytest.toml",
    "pytest.ini",
    ".pytest.ini",
    "pyproject.toml",
    "tox.ini",
    "setup.cfg",
)

BENCHMARK_FIXTURE = "benchmark"
BENCHMARK_MARK = "benchmark"


@dataclass(frozen=True)
class Benchmark:
    """One discovered benchmark, addressed the way pytest addresses it."""

    name: str  # pytest node id, e.g. "tests/test_x.py::TestThing::test_y"
    file: str  # repo-relative path
    func: str  # function name


def _skip_dir(name: str) -> bool:
    return (
        name in SKIP_DIRS
        or name.endswith(".egg-info")
        or name.startswith(".")
        or name.startswith("_")
    )


def is_test_file(rel: str) -> bool:
    """True for a path the harness freezes: a pytest test file, or a conftest."""
    base = Path(rel).name
    return base == CONFTEST or any(fnmatch.fnmatch(base, g) for g in TEST_FILE_GLOBS)


def _walk(root: Path) -> Iterator[str]:
    """Yield repo-relative POSIX paths of every frozen-pattern file, skipping ignored dirs."""
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
        rel_dir = Path(dirpath).relative_to(root)
        for name in sorted(filenames):
            rel = (rel_dir / name).as_posix()
            if rel.startswith("./"):
                rel = rel[2:]
            if is_test_file(rel):
                yield rel


def _is_test_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return node.name.startswith("test_") or node.name.endswith("_test")


def _takes_benchmark_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    args = node.args
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    return BENCHMARK_FIXTURE in names


def _has_benchmark_mark(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        # matches @pytest.mark.benchmark and @pytest.mark.benchmark(...)
        if isinstance(target, ast.Attribute) and target.attr == BENCHMARK_MARK:
            inner = target.value
            if isinstance(inner, ast.Attribute) and inner.attr == "mark":
                return True
    return False


def _is_benchmark(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return _is_test_function(node) and (_takes_benchmark_fixture(node) or _has_benchmark_mark(node))


def benchmarks(root: str | Path) -> list[Benchmark]:
    """Every benchmark declared in the repository, sorted by node id."""
    root = Path(root)
    out: list[Benchmark] = []
    for rel in _walk(root):
        try:
            tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue  # an unparseable file is not fatal to discovery
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _is_benchmark(node):
                out.append(Benchmark(f"{rel}::{node.name}", rel, node.name))
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                for sub in node.body:
                    if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef) and _is_benchmark(
                        sub
                    ):
                        out.append(Benchmark(f"{rel}::{node.name}::{sub.name}", rel, sub.name))
    out.sort(key=lambda b: b.name)
    return out


def frozen_files(root: str | Path, exclude: Sequence[str] = ()) -> list[str]:
    """Every repo-relative path the harness freezes, minus `exclude`, sorted."""
    skip = {Path(e).as_posix() for e in exclude}
    return sorted(rel for rel in _walk(Path(root)) if rel not in skip)


def node_ids(bs: Sequence[Benchmark]) -> list[str]:
    """The benchmark node ids, sorted and deduplicated."""
    return sorted({b.name for b in bs})
