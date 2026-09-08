"""Loads and validates the autor3search-python run configuration."""

from __future__ import annotations

import os
import posixpath
import re
import shutil
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

CONFIG_PATH = ".autor3search/config.toml"

MIN_COUNT = 4
ALLOWED_STATS = ("median", "min", "mean")
ALLOWED_GC = ("enabled", "disabled")

_DURATION_UNITS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|ms|s|m|h)")
_ITERATION_COUNT_FORM = re.compile(r"^\d+x$")


class ConfigError(Exception):
    """A configuration file that cannot be loaded or must not be used."""


def parse_duration(text: str) -> float:
    """Parse a Go-style duration ("1s", "15m", "1m30s", "200ms") into seconds."""
    if not text or not isinstance(text, str):
        raise ValueError(f"{text!r} is not a duration")
    pos, total, seen = 0, 0.0, False
    for m in _DURATION_PART.finditer(text):
        if m.start() != pos:
            raise ValueError(f"{text!r} is not a duration")
        total += float(m.group(1)) * _DURATION_UNITS[m.group(2)]
        pos = m.end()
        seen = True
    if not seen or pos != len(text):
        raise ValueError(f"{text!r} is not a duration")
    return total


@dataclass(frozen=True)
class Gates:
    """Which pre-measurement gates run. The pytest gate is deliberately absent:
    correctness is the one thing this harness will not let a config turn off."""

    compile_: bool = True
    import_: bool = True


@dataclass(frozen=True)
class Config:
    """What is measured, what may be edited, and how strictly."""

    benchmarks: tuple[str, ...] = ()
    scope: tuple[str, ...] = ("./...",)
    count: int = 10
    benchtime: str = "1s"
    min_rounds: int = 5
    stat: str = "median"
    max_regress_pct: float = 5.0
    min_effect_pct: float = 1.0
    timeout: str = "15m"
    hashseed: int = 0
    gc: str = "enabled"
    pythonpath: tuple[str, ...] = ()
    python: str = ""
    unfreeze: tuple[str, ...] = ()
    gates: Gates = field(default_factory=Gates)

    def timeout_seconds(self) -> float:
        return parse_duration(self.timeout)

    def benchtime_seconds(self) -> float:
        return parse_duration(self.benchtime)


def default() -> Config:
    return Config()


_LIST_FIELDS = ("benchmarks", "scope", "pythonpath", "unfreeze")
_SCALAR_FIELDS = (
    "count",
    "benchtime",
    "min_rounds",
    "stat",
    "max_regress_pct",
    "min_effect_pct",
    "timeout",
    "hashseed",
    "gc",
    "python",
)


def load(path: str | Path) -> Config:
    """Read a config file, applying defaults for omitted fields."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError as e:
        raise ConfigError(f"no configuration at {p}: run 'autor3search-python init' first") from e
    except OSError as e:
        raise ConfigError(f"read {p}: {e}") from e
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"parse {p}: {e}") from e

    known = set(_LIST_FIELDS) | set(_SCALAR_FIELDS) | {"gates"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"{p}: unknown key(s) {unknown} — a typo here would silently leave the "
            f"default in force, so it is refused rather than ignored"
        )

    cfg = default()
    updates: dict[str, object] = {}
    for name in _LIST_FIELDS:
        if name in data:
            value = data[name]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ConfigError(f"{p}: {name} must be a list of strings")
            updates[name] = tuple(value)
    for name in _SCALAR_FIELDS:
        if name in data:
            updates[name] = data[name]
    if "gates" in data:
        gates = data["gates"]
        if not isinstance(gates, dict):
            raise ConfigError(f"{p}: [gates] must be a table")
        unknown_gates = sorted(set(gates) - {"compile", "import"})
        if unknown_gates:
            raise ConfigError(f"{p}: unknown gate(s) {unknown_gates}")
        updates["gates"] = Gates(
            compile_=bool(gates.get("compile", True)),
            import_=bool(gates.get("import", True)),
        )
    try:
        cfg = replace(cfg, **updates)  # type: ignore[arg-type]
    except TypeError as e:
        raise ConfigError(f"{p}: {e}") from e
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    """Raise ConfigError if the configuration is unusable."""
    if not isinstance(cfg.count, int) or isinstance(cfg.count, bool) or cfg.count < MIN_COUNT:
        raise ConfigError(
            f"count must be at least {MIN_COUNT}: the Mann-Whitney test cannot report "
            f"p < 0.05 with fewer than {MIN_COUNT} measured rounds per side no matter how "
            f"large the improvement is, so every experiment would be discarded regardless "
            f"of what changed (the default is 10)"
        )
    if not isinstance(cfg.min_rounds, int) or cfg.min_rounds < 1:
        raise ConfigError("min_rounds must be at least 1")
    if cfg.max_regress_pct < 0:
        raise ConfigError("max_regress_pct must not be negative")
    if not (0 <= cfg.min_effect_pct < 100):
        raise ConfigError("min_effect_pct must be at least 0 and less than 100")
    if not cfg.scope:
        raise ConfigError("scope must list at least one path pattern")
    for s in cfg.scope:
        if not s.strip():
            raise ConfigError("scope must not contain an empty or whitespace-only entry")
    if cfg.stat not in ALLOWED_STATS:
        raise ConfigError(f"stat must be one of {list(ALLOWED_STATS)}, got {cfg.stat!r}")
    if cfg.gc not in ALLOWED_GC:
        raise ConfigError(f"gc must be one of {list(ALLOWED_GC)}, got {cfg.gc!r}")
    if _ITERATION_COUNT_FORM.match(str(cfg.benchtime)):
        raise ConfigError(
            f"benchtime {cfg.benchtime!r} uses the fixed-iteration-count form (Nx), "
            f"which is deliberately unsupported: a fixed count makes rounds incomparable, "
            f"because a candidate that is twice as fast finishes in half the wall time and "
            f"is therefore measured under different thermal conditions — exactly what the "
            f'interleaved A/B design exists to eliminate. Use a duration, e.g. benchtime = "1s"'
        )
    try:
        parse_duration(str(cfg.benchtime))
    except ValueError as e:
        raise ConfigError(f"benchtime {cfg.benchtime!r} is not a duration: {e}") from e
    try:
        parse_duration(str(cfg.timeout))
    except ValueError as e:
        raise ConfigError(f"timeout {cfg.timeout!r} is not a duration: {e}") from e
    for b in cfg.benchmarks:
        if b.startswith("-"):
            raise ConfigError(
                f"benchmarks entry {b!r} may not start with '-': it becomes a bare argument "
                f"on the pytest command line (runner.Runner.bench, runner.Runner.compile_gate's "
                f"sibling invocations), and an entry parsed as another option instead of a node "
                f"id is argument injection into that command"
            )
    if cfg.python:
        exe = shutil.which(cfg.python) if not Path(cfg.python).is_absolute() else cfg.python
        if not exe or not Path(exe).is_file() or not os.access(exe, os.X_OK):
            raise ConfigError(
                f"python {cfg.python!r} is not an existing, executable file — it becomes "
                f"argv[0] of every gate and measurement subprocess, so a config shipped in "
                f"the repository (read the first time a human runs 'baseline') could "
                f"otherwise point at anything reachable on PATH"
            )
    for p in cfg.pythonpath:
        pp = PurePosixPath(p.replace("\\", "/"))
        if pp.is_absolute() or (len(str(pp)) > 1 and str(pp)[1] == ":"):
            raise ConfigError(
                f"pythonpath entry {p!r} must be relative to the tree root: bench_env joins "
                f"it as `root / p`, and Path.__truediv__ discards `root` entirely once the "
                f"right-hand side turns out to be absolute — silently putting an "
                f"attacker-chosen absolute path on PYTHONPATH for every gate and measurement"
            )
        normalized = posixpath.normpath(str(pp))
        if normalized == ".." or normalized.startswith("../"):
            raise ConfigError(
                f"pythonpath entry {p!r} escapes the tree root ({normalized!r}) — it is "
                f"joined onto both the candidate root and the pinned baseline worktree root "
                f"and placed on PYTHONPATH for every gate and measurement"
            )
