"""Executes subprocesses with a timeout, captured output and a process group.

The process group matters more than it sounds. pytest runs its benchmark inside
the same process, but plugins, xdist workers and the interpreter's own children
are grandchildren of this harness; a round killed without cleaning up its group
would leave one running, burning CPU and corrupting every later measurement on
the machine.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING

from autor3search_python import discover

if TYPE_CHECKING:
    from autor3search_python.config import Config
    from autor3search_python.scope import Matcher

CAP_BYTES = 4 * 1024 * 1024  # per stream
_TRUNCATED = "\n[output truncated]\n"
_GRACE_SECONDS = 5.0

# Every entry-point ("pytest11") plugin autoloading finds via importlib.metadata
# on sys.path — including one an agent supplies itself, e.g. an
# `evil-1.0.dist-info/entry_points.txt` at the repository root declaring one —
# gets loaded into the SAME process that then times the benchmark. That process
# is not a sandbox: a plugin loaded this way can monkeypatch anything, pytest-
# benchmark's own accounting included. Disabling autoload and loading only the
# plugins this harness names explicitly closes that route. pytest-benchmark is
# itself an entry-point plugin (see _BENCHMARK_PLUGIN below), so every pytest
# invocation that needs it — `pytest_gate` and `bench` here, and profile.py's
# CPU pass — must now load it explicitly, or it silently stops being measured
# at all rather than being measured insecurely.
_DISABLE_AUTOLOAD_ENV = "PYTEST_DISABLE_PLUGIN_AUTOLOAD"
BENCHMARK_PLUGIN = "pytest_benchmark.plugin"

_POSIX = os.name == "posix"


@dataclass(frozen=True)
class Result:
    """The outcome of one subprocess."""

    args: tuple[str, ...]
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    duration: float

    def ok(self) -> bool:
        """A clean, in-time run."""
        return self.exit_code == 0 and not self.timed_out

    def tail(self, n: int) -> str:
        """The last n lines of stderr, falling back to stdout when stderr is blank."""
        src = self.stderr if self.stderr.strip() else self.stdout
        lines = src.rstrip("\n").split("\n")
        return "\n".join(lines[-max(n, 0) :]) if n > 0 else ""


def _compile_exclude() -> str:
    """The -x regex for compileall: what discovery skips, compileall skips too.

    Derived from discover.SKIP_DIRS rather than written out again, so the two
    cannot drift. They had: this used to be a hand-written dotfile pattern, so
    the gate compiled everything under `build/` and `dist/` — vendored, stale
    or deliberately broken code the agent never touched — and could CRASH an
    experiment on a syntax error in a directory no one was measuring.

    compileall matches this with `search` against each path it walks. The extra
    `[^/]` in the dotfile branch matters when a caller passes "." (this
    module's own test does): compileall then reports files as "./bad.py", and a
    bare `(^|/)\\.` matches that leading "./" itself, excluding every file in
    the tree instead of just dotfiles and dotdirs.
    """
    names = "|".join(re.escape(d) for d in sorted(discover.SKIP_DIRS))
    return rf"(^|/)(\.[^/]|({names})(/|$))"


def _cap(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    if len(raw) <= CAP_BYTES:
        return text
    return text[:CAP_BYTES] + _TRUNCATED


def _kill_group(proc: subprocess.Popen) -> None:
    """Terminate the whole process group, then insist."""
    if _POSIX:
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=_GRACE_SECONDS)
                return
            except subprocess.TimeoutExpired:
                continue
    else:  # pragma: no cover - exercised only on Windows
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_GRACE_SECONDS)


class Runner:
    """Runs commands in a fixed directory."""

    def __init__(
        self,
        directory: str | Path,
        timeout: float,
        log: IO[str] | None = None,
        env: dict[str, str] | None = None,
        python: str = "",
    ) -> None:
        self.directory = Path(directory)
        self.timeout = float(timeout)
        self.log = log
        self.env = env
        self.python = python or sys.executable

    def run(self, *args: str) -> Result:
        start = time.monotonic()
        popen_kwargs: dict = {
            "cwd": str(self.directory),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": self.env if self.env is not None else os.environ.copy(),
        }
        if _POSIX:
            popen_kwargs["start_new_session"] = True
        else:  # pragma: no cover
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        proc = subprocess.Popen(list(args), **popen_kwargs)
        timed_out = False
        try:
            out, err = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            # Bounded: a grandchild that escapes the process group (by calling
            # setsid itself) would otherwise block this call forever. An
            # unattended overnight loop must return timed_out=True rather than
            # hang — a wrong answer beats nobody noticing it never came back.
            try:
                out, err = proc.communicate(timeout=_GRACE_SECONDS)
            except subprocess.TimeoutExpired as e:
                out, err = e.stdout, e.stderr
        duration = time.monotonic() - start
        res = Result(
            args=tuple(args),
            stdout=_cap(out or b""),
            stderr=_cap(err or b""),
            exit_code=-1 if timed_out else proc.returncode,
            timed_out=timed_out,
            duration=duration,
        )
        if self.log is not None:
            self.log.write(
                f"\n$ {' '.join(args)}\n"
                f"(dir={self.directory} exit={res.exit_code} timedOut={res.timed_out} "
                f"took={res.duration:.3f}s)\n"
            )
            self.log.write(res.stdout)
            self.log.write(res.stderr)
            self.log.flush()
        return res

    def python_run(self, *args: str) -> Result:
        return self.run(self.python, *args)

    def compile_gate(self, paths: Sequence[str]) -> Result:
        """The syntax gate. `go build`'s role: is this even valid code?"""
        return self.python_run("-m", "compileall", "-q", "-x", _compile_exclude(), *paths)

    def import_gate(self, modules: Sequence[str]) -> Result:
        """Import every in-scope module in a subprocess.

        Catches the large class of breakage compileall cannot see — a bad
        import, a NameError at module scope, a decorator that raises — before
        spending ten minutes benchmarking.
        """
        if not modules:
            return Result((), "", "", 0, False, 0.0)
        script = (
            "import importlib, sys\n"
            f"for name in {list(modules)!r}:\n"
            "    try:\n"
            "        importlib.import_module(name)\n"
            "    except BaseException as e:\n"
            "        print(f'{name}: {type(e).__name__}: {e}', file=sys.stderr)\n"
            "        raise SystemExit(1)\n"
        )
        return self.python_run("-c", script)

    def pytest_gate(self) -> Result:
        """The full test suite. Correctness is never traded for speed.

        --import-mode=importlib is here to match `bench` exactly. Without it
        the gate and the measurement resolve modules by different rules, and a
        change could pass correctness under one resolution while being timed
        under another — two different programs, one verdict.

        `--benchmark-disable` is a pytest-benchmark option, and plugin
        autoloading is off (see `bench_env`), so pytest-benchmark is loaded
        explicitly here too — without it this flag would be "unrecognized
        arguments" and the gate would fail on every run, not silently pass.
        """
        return self.python_run(
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            BENCHMARK_PLUGIN,
            "--import-mode=importlib",
            "--benchmark-disable",
        )

    def bench(self, node_ids: Sequence[str], json_path: str | Path, cfg: Config) -> Result:
        """One measurement round for the declared benchmarks."""
        args = [
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            BENCHMARK_PLUGIN,  # loaded explicitly: autoloading is off, see bench_env
            "--import-mode=importlib",
            "--benchmark-only",
            f"--benchmark-json={json_path}",
            f"--benchmark-max-time={cfg.benchtime_seconds()}",
            f"--benchmark-min-rounds={cfg.min_rounds}",
        ]
        if cfg.gc == "disabled":
            args.append("--benchmark-disable-gc")
        args.extend(node_ids)
        return self.python_run(*args)


def bench_env(
    directory: str | Path, cfg: Config, base_env: dict[str, str] | None = None
) -> dict[str, str]:
    """The environment a gate or measurement runs under, for one tree.

    PYTHONPATH points at THAT tree, which is the whole point: without it both
    sides would import the same installed copy and the pinned baseline would
    never actually be measured.
    """
    root = Path(directory).resolve()
    env = dict(os.environ if base_env is None else base_env)
    entries = [str(root / p) for p in cfg.pythonpath]
    entries.append(str(root))
    src = root / "src"
    if src.is_dir() and any(c.is_dir() or c.suffix == ".py" for c in src.iterdir()):
        entries.append(str(src))
    existing = env.get("PYTHONPATH")
    if existing:
        entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    if cfg.hashseed >= 0:
        env["PYTHONHASHSEED"] = str(cfg.hashseed)
    else:
        env.pop("PYTHONHASHSEED", None)
    # Measurement must not inherit ambient pytest flags. The agent is the
    # process that invokes `eval`, so it owns this environment, and
    # PYTEST_ADDOPTS is a pytest.ini it never has to write down:
    # `PYTEST_ADDOPTS='-k benchmark'` neuters the correctness gate that is
    # deliberately not switchable, and `--benchmark-timer=` swaps the clock.
    # PYTEST_PLUGINS goes for the same reason — it loads arbitrary modules into
    # every gate and both bench sides.
    for var in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        env.pop(var, None)
    # Forced, not merely stripped like the two above: an unset
    # PYTEST_DISABLE_PLUGIN_AUTOLOAD does not merely inherit an ambient
    # default, it turns autoloading back ON, which is the hole this closes.
    # See the module docstring-adjacent comment above _DISABLE_AUTOLOAD_ENV.
    env[_DISABLE_AUTOLOAD_ENV] = "1"
    return env


def importable_modules(root: str | Path, matcher: Matcher) -> list[str]:
    """Dotted module names for every in-scope, non-test .py file.

    Names are relative to the tree root or to `src/`, matching what bench_env
    puts on PYTHONPATH, so the import gate exercises the same resolution the
    benchmarks will.
    """
    root = Path(root)
    src = root / "src"
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in discover.SKIP_DIRS
            and not d.endswith(".egg-info")
            and not d.startswith((".", "_"))
        )
        here = Path(dirpath)
        for name in filenames:
            if not name.endswith(".py"):
                continue
            rel = (here / name).relative_to(root).as_posix()
            if discover.is_test_file(rel) or not matcher.match(rel):
                continue
            path = here / name
            anchor = src if src.is_dir() and src in path.parents else root
            parts = list(path.relative_to(anchor).with_suffix("").parts)
            if parts and parts[-1] == "__init__":
                parts.pop()
            if parts and all(p.isidentifier() for p in parts):
                out.add(".".join(parts))
    return sorted(out)
