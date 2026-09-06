"""Runs the declared benchmarks under cProfile and tracemalloc.

Real profile data on where time and allocations actually go, rather than an
agent guessing from reading source. Never a measurement: no results.tsv row, no
baseline, no verdict.
"""

from __future__ import annotations

import io
import json
import pstats
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from autor3search_python import profiling, runner
from autor3search_python.config import Config

PROFILE_DIR = ".autor3search/profiles"
_TOP = 20
_SITE_WIDTH = 60
_SIZE_WIDTH = 12
_BLOCKS_WIDTH = 12

# Both env vars a pass could ever see, so each subprocess can be started with
# only the one it needs, regardless of what the parent environment carries.
_ALL_PROFILE_ENV_VARS = (profiling.CPU_ENV, profiling.MEM_ENV)


class ProfileError(Exception):
    """A profiling run that could not be completed."""


@dataclass(frozen=True)
class Report:
    cpu_top: str
    mem_top: str
    cpu_path: Path
    mem_path: Path


def _elide(site: str, width: int) -> str:
    """Shorten `site` to `width` without ever cutting inside a path component.

    Builds the display path from the right (the part a reader cares about
    most — the file and line), adding parent directories while they still
    fit, and marks the missing prefix with "...". Only falls back to a hard
    cut, on the trailing component itself, when even that alone overflows.
    """
    if len(site) <= width:
        return site
    parts = site.split("/")
    kept = parts[-1]
    for part in reversed(parts[:-1]):
        candidate = f"{part}/{kept}"
        if len(f".../{candidate}") > width:
            break
        kept = candidate
    shortened = f".../{kept}"
    return shortened if len(shortened) <= width else shortened[-width:]


def _display_site(file: str, line: int, root: Path | None) -> str:
    display = file
    if root is not None:
        try:
            display = str(Path(file).resolve().relative_to(root))
        except ValueError:
            display = file
    return _elide(f"{display}:{line}", _SITE_WIDTH)


def format_cpu(stats_path: str | Path, limit: int = _TOP, root: str | Path | None = None) -> str:
    path = Path(stats_path)
    if not path.exists():
        return f"(no CPU profile was written to {path})"
    buf = io.StringIO()
    st = pstats.Stats(str(path), stream=buf)
    if root is not None:
        # Unrestricted, this section is 20 lines of pluggy/pytest call
        # machinery — identical for every repository and no help to an
        # agent deciding what to change. Cumulative time still matters (it is
        # how a slow leaf function shows up in its caller), so restrict
        # rather than drop: keep only frames inside the target repository.
        pattern = re.escape(str(Path(root).resolve()) + "/")
        buf.write("top by cumulative time (repository code only)\n")
        st.sort_stats("cumulative").print_stats(pattern, limit)
    else:
        buf.write("top by cumulative time\n")
        st.sort_stats("cumulative").print_stats(limit)
    buf.write("\ntop by time in the function itself\n")
    st.sort_stats("tottime").print_stats(limit)
    return buf.getvalue()


def format_mem(json_path: str | Path, limit: int = _TOP, root: str | Path | None = None) -> str:
    path = Path(json_path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"(no memory profile was written to {path})"

    header: list[str] = []
    peak = doc.get("peak_bytes")
    if peak is not None:
        header.append(f"peak traced memory during the session: {peak / 1024:.1f}K")
    header.append(
        "sites below: size of blocks still allocated when the session ended "
        "(retained memory, not total bytes allocated over the run)"
    )

    entries = sorted(doc.get("top", []), key=lambda e: e.get("size", 0), reverse=True)[:limit]
    if not entries:
        if peak is not None:
            header.append(
                "(no allocation sites recorded — transient allocations are freed before "
                "the session-end snapshot; see the peak figure above for total volume)"
            )
        else:
            header.append(
                "(no allocation sites recorded — transient allocations are freed before "
                "the session-end snapshot, and no peak was recorded either)"
            )
        return "\n".join(header)

    root_path = Path(root).resolve() if root is not None else None
    table = [f"{'site':<{_SITE_WIDTH}} {'size':>{_SIZE_WIDTH}} {'blocks':>{_BLOCKS_WIDTH}}"]
    for e in entries:
        site = _display_site(e.get("file", "?"), e.get("line", 0), root_path)
        size_str = f"{e.get('size', 0) / 1024:.1f}K"
        table.append(
            f"{site:<{_SITE_WIDTH}} {size_str:>{_SIZE_WIDTH}} {e.get('count', 0):>{_BLOCKS_WIDTH}}"
        )
    return "\n".join([*header, "", *table])


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
        # Strip BOTH variables first. Without this, a leftover value for the
        # OTHER pass's variable — inherited from the parent shell, or from a
        # process that set it and never unset it — survives into this pass's
        # environment. pytest_configure then takes the CPU branch even during
        # the memory pass, silently profiles the wrong thing, and (worse)
        # dump_stats() overwrites whatever file that stale variable pointed
        # at, all while this command still exits 0.
        for var in _ALL_PROFILE_ENV_VARS:
            env.pop(var, None)
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
        cpu_top=format_cpu(cpu_path, root=root),
        mem_top=format_mem(mem_path, root=root),
        cpu_path=cpu_path,
        mem_path=mem_path,
    )
