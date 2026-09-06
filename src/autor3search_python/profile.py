"""Runs the declared benchmarks under cProfile and tracemalloc.

Real profile data on where time and allocations actually go, rather than an
agent guessing from reading source. Never a measurement: no results.tsv row, no
baseline, no verdict.
"""

from __future__ import annotations

import io
import json
import pstats
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from autor3search_python import profiling, runner
from autor3search_python.config import Config

PROFILE_DIR = ".autor3search/profiles"
_TOP = 20


class ProfileError(Exception):
    """A profiling run that could not be completed."""


@dataclass(frozen=True)
class Report:
    cpu_top: str
    mem_top: str
    cpu_path: Path
    mem_path: Path


def format_cpu(stats_path: str | Path, limit: int = _TOP) -> str:
    path = Path(stats_path)
    if not path.exists():
        return f"(no CPU profile was written to {path})"
    buf = io.StringIO()
    st = pstats.Stats(str(path), stream=buf)
    buf.write("top by cumulative time\n")
    st.sort_stats("cumulative").print_stats(limit)
    buf.write("\ntop by time in the function itself\n")
    st.sort_stats("tottime").print_stats(limit)
    return buf.getvalue()


def format_mem(json_path: str | Path, limit: int = _TOP) -> str:
    path = Path(json_path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"(no memory profile was written to {path})"
    entries = sorted(doc.get("top", []), key=lambda e: e.get("size", 0), reverse=True)[:limit]
    if not entries:
        return "(no allocation sites recorded)"
    lines = [f"{'site':<60} {'size':>12} {'blocks':>10}"]
    for e in entries:
        site = f"{e.get('file', '?')}:{e.get('line', 0)}"
        lines.append(f"{site[-60:]:<60} {e.get('size', 0) / 1024:>10.1f}K {e.get('count', 0):>10}")
    return "\n".join(lines)


def run_profile(
    root: str | Path, node_ids: Sequence[str], cfg: Config, log: IO[str] | None = None
) -> Report:
    """Profile the declared benchmarks, in two passes, and return the summaries."""
    root = Path(root)
    out_dir = root / PROFILE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    cpu_path, mem_path = out_dir / "cpu.prof", out_dir / "mem.json"
    cpu_path.unlink(missing_ok=True)
    mem_path.unlink(missing_ok=True)

    base_env = runner.bench_env(root, cfg)
    for env_var, target in ((profiling.CPU_ENV, cpu_path), (profiling.MEM_ENV, mem_path)):
        env = dict(base_env)
        env[env_var] = str(target)
        r = runner.Runner(root, cfg.timeout_seconds(), log=log, env=env, python=cfg.python)
        res = r.python_run(
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "autor3search_python.profiling",
            "--import-mode=importlib",
            "--benchmark-only",
            "--benchmark-max-time=0.5",
            "--benchmark-min-rounds=1",
            *node_ids,
        )
        if res.timed_out:
            raise ProfileError(f"profiling pass timed out after {cfg.timeout}")
        if not res.ok():
            raise ProfileError(f"profiling pass failed (exit {res.exit_code}):\n{res.tail(30)}")

    return Report(
        cpu_top=format_cpu(cpu_path),
        mem_top=format_mem(mem_path),
        cpu_path=cpu_path,
        mem_path=mem_path,
    )
