"""Checks whether this machine can measure benchmarks reliably.

Numbers are only as good as the machine that produced them. A thermally
throttled laptop on battery produces noise dressed as data, and a repository
with coverage in its addopts produces timings that measure the instrumentation.
None of this gates anything: doctor reports, the human decides.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from autor3search_python import discover, gitx

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


def check_compiled_extensions(root: str | Path) -> Finding:
    """PYTHONPATH injection cannot build a compiled extension into the baseline worktree."""
    root = Path(root)
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
    if signals:
        return Finding(
            "extensions",
            f"this repository looks like it needs a build step ({', '.join(signals)}). "
            f"Measurement imports each tree straight off disk via PYTHONPATH and cannot "
            f"build extensions, so the pinned baseline worktree may fail to import.",
            Severity.WARN,
        )
    return Finding("extensions", "pure Python; importable straight off disk", Severity.OK)


def check(directory: str | Path, python: str = "") -> list[Finding]:
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
        check_compiled_extensions(root),
        check_disk(directory),
    ]
