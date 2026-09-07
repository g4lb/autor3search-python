"""Checks whether this machine can measure benchmarks reliably.

Numbers are only as good as the machine that produced them. A thermally
throttled laptop on battery produces noise dressed as data, and a repository
with coverage in its addopts produces timings that measure the instrumentation.
None of this gates anything: doctor reports, the human decides.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from autor3search_python import config, discover, gitx, runner, state

_EXTENSION_SIGNALS = ("Cargo.toml", "meson.build")

# Modules needed only to run the measurement itself. Missing either one means
# nothing can be measured at all: a FAIL.
_MEASURE_MODULES = ("pytest", "pytest_benchmark")

# autor3search_python is needed only because `profile` loads its plugin with
# `-p autor3search_python.profiling` inside the measuring interpreter, not the
# one running the harness. `eval` and `bench` never import it there, so its
# absence is a WARN naming the one thing it breaks, not a repo-wide FAIL.
_PROFILE_MODULES = ("autor3search_python",)

_REQUIRED_MODULES = _MEASURE_MODULES + _PROFILE_MODULES

_MISSING_MODULE_RE = re.compile(r"No module named ['\"]([\w.]+)['\"]")

# Computed once, same as runstop._POSIX and runner._POSIX: this is the one
# thing that determines whether the concurrency guard, `stop --force`, and
# the timeout's process-group kill actually work.
_POSIX = os.name == "posix"


class Severity(IntEnum):
    NOT_APPLICABLE = -1
    OK = 0
    WARN = 1
    FAIL = 2


@dataclass(frozen=True)
class Finding:
    name: str
    detail: str
    severity: Severity


def check_python() -> Finding:
    v = sys.version_info
    if v < (3, 11):
        return Finding("python", f"{sys.version.split()[0]} is below the 3.11 floor", Severity.FAIL)
    detail = f"{sys.version.split()[0]} at {sys.executable}"
    if hasattr(sys, "gettotalrefcount"):
        return Finding(
            "python", detail + " — a DEBUG build; timings are not representative", Severity.WARN
        )
    return Finding("python", detail, Severity.OK)


def check_git() -> Finding:
    path = shutil.which("git")
    if not path:
        return Finding(
            "git", "not found on PATH; the harness cannot keep or discard commits", Severity.FAIL
        )
    return Finding("git", path, Severity.OK)


def check_git_repo(directory: str | Path) -> Finding:
    try:
        return Finding("git repo", gitx.root(directory), Severity.OK)
    except gitx.GitError:
        return Finding(
            "git repo",
            f"{directory} is not inside a git repository; run `git init`",
            Severity.FAIL,
        )


def check_cpu() -> Finding:
    count = os.cpu_count() or 0
    if count < 2:
        return Finding(
            "cpu",
            f"{count} logical CPU(s); measurement will contend with everything else",
            Severity.WARN,
        )
    return Finding("cpu", f"{count} logical CPUs", Severity.OK)


def check_load() -> Finding:
    try:
        one, _, _ = os.getloadavg()
    except (OSError, AttributeError):  # pragma: no cover - Windows
        return Finding("load", "not available on this platform", Severity.NOT_APPLICABLE)
    cpus = os.cpu_count() or 1
    if one > cpus * 0.3:
        return Finding(
            "load",
            f"1-minute load {one:.2f} on {cpus} CPUs — the machine is busy; "
            f"measurements will be noisy",
            Severity.WARN,
        )
    return Finding("load", f"1-minute load {one:.2f} on {cpus} CPUs", Severity.OK)


def check_platform() -> Finding:
    if not _POSIX:
        return Finding(
            "platform",
            "this platform is not POSIX; this harness has never been run or tested here, "
            "and three of its guarantees are silently absent rather than merely degraded. "
            "(1) The concurrency guard in claim_eval always reports success, so two evals "
            "can run against the same pinned baseline worktree at once. (2) `stop --force` "
            "cannot signal the running eval's process group here, so it cannot stop one. "
            "(3) A benchmark that times out has only its direct child killed, not its "
            "process group, so its grandchildren keep running and burn CPU, corrupting "
            "every later measurement. Do not trust a number produced here.",
            Severity.FAIL,
        )
    if sys.platform == "darwin":
        return Finding(
            "platform",
            "macOS schedules across performance and efficiency cores, which makes timings "
            "jump. Interleaving and a higher count mitigate it; a quiet Linux box is cleaner.",
            Severity.WARN,
        )
    if sys.platform.startswith("linux"):
        gov = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
        try:
            value = gov.read_text().strip()
        except OSError:
            return Finding("platform", "cpufreq governor not readable", Severity.NOT_APPLICABLE)
        if value != "performance":
            return Finding(
                "platform",
                f"cpufreq governor is {value!r}, not 'performance' — the CPU may change "
                f"frequency mid-measurement",
                Severity.WARN,
            )
        return Finding("platform", "cpufreq governor is 'performance'", Severity.OK)
    return Finding("platform", sys.platform, Severity.NOT_APPLICABLE)


def check_disk(directory: str | Path) -> Finding:
    try:
        usage = shutil.disk_usage(str(directory))
    except OSError as e:
        return Finding("disk", str(e), Severity.NOT_APPLICABLE)
    free_gb = usage.free / 1e9
    if free_gb < 1.0:
        return Finding(
            "disk",
            f"{free_gb:.1f} GB free — the pinned worktree and profiles need room",
            Severity.FAIL,
        )
    if free_gb < 5.0:
        return Finding("disk", f"{free_gb:.1f} GB free", Severity.WARN)
    return Finding("disk", f"{free_gb:.1f} GB free", Severity.OK)


def _nearest_existing_ancestor(path: Path) -> Path:
    """`path` itself, or the closest ancestor that actually exists.

    `state_home()`'s directory, and everything under it, is created lazily —
    `baseline` is what first makes it exist. `os.stat` needs a real path, so
    a filesystem comparison has to walk up to one.
    """
    probe = path
    while not probe.exists():
        parent = probe.parent
        if parent == probe:  # reached the filesystem root without finding one
            break
        probe = parent
    return probe


def _device_id(path: Path) -> int:
    """The filesystem device id for `path`.

    A seam, and it earns its place: the obvious way to test check_state_fs is
    to patch `Path.stat`, but that patches it for the whole process — pytest's
    own traceback formatter calls `Path.exists()`, which calls it too. A stub
    that runs dry there raises StopIteration inside pytest's internals and
    crashes the run with INTERNALERROR rather than failing a test. Replacing
    this function instead touches only the two calls that matter.
    """
    return path.stat().st_dev


def check_state_fs(directory: str | Path) -> Finding:
    """The pinned baseline worktree lives under `state_home()`, out of the
    repository on purpose (see state.py's module docstring) — deliberately
    reachable by the agent only through the repository it edits, not
    through the state directory itself. Nothing then checks the two share a
    filesystem, though. Point AUTOR3SEARCH_PYTHON_STATE_HOME at a tmpfs or a
    second volume and the two A/B sides of every comparison end up measured
    on different storage — invisible in the reported numbers, and fatal for
    a benchmark whose time is dominated by I/O rather than CPU.
    """
    try:
        root = Path(gitx.root(directory))
    except gitx.GitError:
        root = Path(directory)
    try:
        home = state.state_home()
    except state.StateError as e:
        return Finding(
            "state filesystem",
            f"{e} — see the state finding a run command would report",
            Severity.NOT_APPLICABLE,
        )
    home_probe = _nearest_existing_ancestor(home)
    try:
        root_dev = _device_id(root)
        home_dev = _device_id(home_probe)
    except OSError as e:
        return Finding(
            "state filesystem",
            f"could not stat {root} or {home_probe}: {e}",
            Severity.NOT_APPLICABLE,
        )
    if root_dev != home_dev:
        return Finding(
            "state filesystem",
            f"{root} and the run-state directory ({home_probe}) are on different "
            f"filesystems — the pinned baseline worktree lives under the latter, so an "
            f"I/O-bound benchmark would measure its two sides against different storage "
            f"without anything in the reported numbers saying so. Point "
            f"AUTOR3SEARCH_PYTHON_STATE_HOME at a directory on the same filesystem as "
            f"this repository",
            Severity.WARN,
        )
    return Finding("state filesystem", f"{root} and {home_probe} share a filesystem", Severity.OK)


def _describe(names: Sequence[str]) -> str:
    verb = "is" if len(names) == 1 else "are"
    return f"{' and '.join(names)} {verb}"


def check_benchmark_tooling(python: str = "") -> Finding:
    """Everything the measuring interpreter must be able to import.

    pytest and pytest-benchmark are a FAIL: without them nothing can be
    measured at all. `autor3search_python` is only a WARN — it is on this list
    solely because `profile` runs `-p autor3search_python.profiling` under
    exactly this interpreter, not under the one running the harness, so its
    absence breaks `profile` alone. Configure `python` to a venv without the
    harness installed and `eval` still measures fine while `profile` fails at
    collection — a confusing failure at 3am if this check does not say so.
    """
    exe = python or sys.executable
    script = (
        "import sys\n"
        "missing = []\n"
        f"for _name in {list(_REQUIRED_MODULES)!r}:\n"
        "    try:\n"
        "        __import__(_name)\n"
        "    except ImportError:\n"
        "        missing.append(_name)\n"
        "if missing:\n"
        "    print(' '.join(missing), file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "import pytest, pytest_benchmark\n"
        "print(pytest.__version__, pytest_benchmark.__version__)\n"
    )
    try:
        proc = subprocess.run([exe, "-c", script], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return Finding("pytest-benchmark", f"could not run {exe}: {e}", Severity.FAIL)
    if proc.returncode != 0:
        missing = set(proc.stderr.strip().split())
        fix = (
            f"Fix with: {exe} -m pip install "
            f"{' '.join(m.replace('_', '-') for m in _REQUIRED_MODULES)}"
        )
        hard = [m for m in _MEASURE_MODULES if m in missing]
        soft = [m for m in _PROFILE_MODULES if m in missing]
        if hard:
            return Finding(
                "pytest-benchmark",
                f"{_describe(hard)} not importable by {exe} — nothing can be measured "
                f"without it. {fix}",
                Severity.FAIL,
            )
        if soft:
            return Finding(
                "pytest-benchmark",
                f"{_describe(soft)} not importable by {exe} — `profile` needs it to load "
                f"its plugin there; `eval` and `bench` do not. {fix}",
                Severity.WARN,
            )
        return Finding(
            "pytest-benchmark",
            f"unexpected failure importing tooling under {exe} (exit {proc.returncode}): "
            f"{proc.stderr.strip()!r}. {fix}",
            Severity.FAIL,
        )
    versions = proc.stdout.strip().split()
    if len(versions) < 2:
        return Finding(
            "pytest-benchmark",
            f"unexpected output from {exe}: {proc.stdout!r}",
            Severity.WARN,
        )
    return Finding(
        "pytest-benchmark",
        f"pytest {versions[0]}, pytest-benchmark {versions[1]}",
        Severity.OK,
    )


def check_coverage_addopts(root: str | Path) -> Finding:
    """Coverage in addopts silently instruments every call and destroys every timing.

    The single highest-value check here: it produces confident percentages
    attached to numbers that measure the instrumentation, not the code.
    """
    root = Path(root)
    for name in discover.PYTEST_CONFIG_FILES:
        path = root / name
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "addopts" not in stripped:
                continue
            if "--cov" in stripped:
                return Finding(
                    "coverage",
                    f"{name} has coverage in addopts ({stripped}) — it instruments every call "
                    f"and will destroy every timing. Remove it, or override with "
                    f"`-p no:cov` in a project-local config.",
                    Severity.WARN,
                )
    return Finding(
        "coverage", "not enabled by default in this repository's pytest config", Severity.OK
    )


@dataclass(frozen=True)
class _Target:
    """One top-level importable name and the path it resolves to on disk."""

    name: str
    path: Path


def _top_level_targets(anchor: Path) -> list[_Target]:
    """Packages (a directory with `__init__.py`) and modules (`*.py`) directly under `anchor`."""
    if not anchor.is_dir():
        return []
    out: list[_Target] = []
    for child in sorted(anchor.iterdir()):
        name = child.name
        if name.startswith((".", "_")) or name in discover.SKIP_DIRS or name.endswith(".egg-info"):
            continue
        if child.is_dir():
            if (child / "__init__.py").exists():
                out.append(_Target(name, child))
        elif child.suffix == ".py" and name != "setup.py" and not discover.is_test_file(name):
            out.append(_Target(child.stem, child))
    return out


def _discover_targets(root: Path) -> list[_Target]:
    """Top-level importable names under the repo root, and under `src/` for that layout.

    Root is checked first because that is the resolution order `runner.bench_env`
    puts on PYTHONPATH (root, then `src/`) — a name defined in both would
    resolve to the root one, same as the actual import will.
    """
    targets = _top_level_targets(root)
    targets += _top_level_targets(root / "src")
    seen: set[str] = set()
    out: list[_Target] = []
    for t in targets:
        if t.name in seen:
            continue
        seen.add(t.name)
        out.append(t)
    return out


def _extension_signals(root: Path) -> list[str]:
    """Proxies for "needs a build step" — an explanation for a failed import, not a verdict."""
    signals: list[str] = []
    for name in _EXTENSION_SIGNALS:
        if (root / name).exists():
            signals.append(name)
    setup_py = root / "setup.py"
    try:
        if "ext_modules" in setup_py.read_text(encoding="utf-8", errors="replace"):
            signals.append("setup.py declares ext_modules")
    except OSError:
        pass
    if any(root.rglob("*.pyx")):
        signals.append("*.pyx sources")
    return signals


def _ignored_for_missing_module(
    root: Path, targets: Sequence[_Target], error_text: str
) -> str | None:
    """The path a `ModuleNotFoundError` in `error_text` names, if git would ignore it.

    Works even when the path does not exist on disk here — exactly the case
    for a fresh checkout, where the missing file was never committed. This is
    `git check-ignore` rather than `git status`: check-ignore answers a pure
    pattern question ("would this path be ignored?") with no dependency on
    whether the path is actually present.

    The anchor (repo root, or `src/`) is taken from the top-level target the
    missing submodule belongs to, not guessed — both anchors can satisfy the
    same trailing pattern (a bare `_version.py` in `.gitignore` matches at any
    depth), so guessing would silently name the wrong tree's copy.
    """
    m = _MISSING_MODULE_RE.search(error_text)
    if not m:
        return None
    parts = m.group(1).split(".")
    anchor = next((t.path.parent for t in targets if t.name == parts[0]), None)
    anchors = [anchor] if anchor is not None else [root, root / "src"]
    for base_dir in anchors:
        base = base_dir / "/".join(parts)
        for candidate in (base.with_suffix(".py"), base / "__init__.py"):
            try:
                rel = candidate.relative_to(root).as_posix()
            except ValueError:
                continue
            try:
                proc = subprocess.run(
                    ["git", "check-ignore", "-q", "--", rel],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                return None
            if proc.returncode == 0:
                return rel
    return None


def _gitignored_within(root: Path, targets: Sequence[_Target]) -> list[str]:
    """Gitignored paths that physically exist under `targets` right now.

    These are present in this working tree only by accident of local state —
    a generated file, typically — and will be absent from a fresh checkout,
    including the pinned baseline worktree the harness measures against.
    """
    rel = [str(t.path.relative_to(root)) for t in targets if t.path.exists()]
    if not rel:
        return []
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--ignored", "--", *rel],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.startswith("!! "):
            continue
        path = line[3:].strip()
        parts = Path(path).parts
        if "__pycache__" in parts or path.endswith((".pyc", ".pyo")):
            continue
        out.append(path)
    return sorted(set(out))


def check_imports(root: str | Path, python: str = "", cfg: config.Config | None = None) -> Finding:
    """Actually try to import the repository, instead of guessing from proxy files.

    This runs in a subprocess, under the same `runner.bench_env` and the same
    interpreter the real measurement uses, because a heuristic like "no
    Cargo.toml, so it must be pure Python and importable" can be — and was,
    against a real repository — confidently wrong: a generated file absent
    from a fresh checkout broke every import while every proxy still said OK.
    """
    root = Path(root)
    cfg = cfg or config.default()
    targets = _discover_targets(root)
    if not targets:
        return Finding(
            "imports", "no top-level package or module found to import", Severity.NOT_APPLICABLE
        )
    names = [t.name for t in targets]
    exe = python or sys.executable
    env = runner.bench_env(root, cfg)
    script = (
        "import sys\n"
        f"for _name in {names!r}:\n"
        "    try:\n"
        "        __import__(_name)\n"
        "    except BaseException as e:\n"
        "        print(f'{_name}: {type(e).__name__}: {e}', file=sys.stderr)\n"
        "        raise SystemExit(1)\n"
    )
    try:
        proc = subprocess.run(
            [exe, "-c", script],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return Finding("imports", f"could not run {exe}: {e}", Severity.FAIL)

    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"{exe} exited {proc.returncode} importing {names}"
        signals = _extension_signals(root)
        if signals:
            detail += (
                f" — this repository looks like it needs a build step "
                f"({', '.join(signals)}); PYTHONPATH injection cannot build extensions, so "
                f"this strategy cannot serve it."
            )
        ignored = _ignored_for_missing_module(root, targets, proc.stderr)
        if ignored:
            detail += (
                f" {ignored} is gitignored — it will be missing from a fresh checkout, "
                f"including the pinned baseline worktree. If it must exist, generate it and "
                f"`git add -f {ignored}`."
            )
        return Finding("imports", detail, Severity.FAIL)

    drifting = _gitignored_within(root, targets)
    if drifting:
        return Finding(
            "imports",
            f"{', '.join(names)} import cleanly here, but "
            f"{', '.join(drifting)} {'is' if len(drifting) == 1 else 'are'} gitignored — "
            f"present in this working tree but will be missing from the pinned baseline "
            f"worktree, where the import will then fail. Fix with: "
            f"git add -f {' '.join(drifting)}",
            Severity.WARN,
        )
    return Finding("imports", f"{', '.join(names)} import cleanly", Severity.OK)


def check(
    directory: str | Path, python: str = "", cfg: config.Config | None = None
) -> list[Finding]:
    """Every diagnostic, in report order."""
    try:
        root = Path(gitx.root(directory))
    except gitx.GitError:
        root = Path(directory)
    return [
        check_python(),
        check_git(),
        check_git_repo(directory),
        check_cpu(),
        check_load(),
        check_platform(),
        check_benchmark_tooling(python),
        check_coverage_addopts(root),
        check_imports(root, python, cfg),
        check_disk(directory),
        check_state_fs(directory),
    ]
