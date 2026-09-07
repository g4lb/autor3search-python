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

from autor3search_python import containment, profiling, runner
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


def _format_size(num_bytes: int) -> str:
    return f"{num_bytes / 1024:.1f}K"


def _format_test_peaks(test_peaks: dict, limit: int) -> str:
    """Render the per-benchmark steady-state peak-memory table.

    This is the measurement that a session-end snapshot alone cannot give:
    it sees allocation volume during a benchmark even when every byte of it
    is freed before the run ends. It is the minimum peak additional traced
    memory over a handful of direct calls to the benchmarked callable (see
    `profiling._MemBenchmarkFixture`/`_measure_peak`) — steady state, so a
    one-time cost like a lazy import on the first call is excluded, and
    measured by calling the callable directly rather than through
    pytest-benchmark's own timing loop, whose round count would otherwise
    swamp the number for anything fast and cheap. Still process-wide in the
    sense that `tracemalloc.get_traced_memory()` is never filtered to the
    repository the way the retained-sites snapshot below is — so it is not
    literally "bytes this one function allocated" — but no longer inflated
    by pytest-benchmark's own round-count bookkeeping.
    """
    header = (
        "peak traced memory for one call of each benchmark, steady state (the "
        "minimum peak over several direct calls, excluding one-time setup cost; "
        "process-wide — get_traced_memory is not filtered to this repository):"
    )
    if not test_peaks:
        return f"{header}\n(no per-benchmark peaks recorded — no benchmarks ran)"

    entries = sorted(test_peaks.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    table = [f"{'benchmark':<{_SITE_WIDTH}} {'peak':>{_SIZE_WIDTH}}"]
    for node_id, peak in entries:
        name = _elide(str(node_id), _SITE_WIDTH)
        table.append(f"{name:<{_SITE_WIDTH}} {_format_size(peak):>{_SIZE_WIDTH}}")
    return "\n".join([header, "", *table])


def _format_retained_sites(entries_raw: list, limit: int, root: str | Path | None) -> str:
    """Render the session-end snapshot's per-line retained-memory table.

    Blocks still live when the session ended — result objects, caches,
    leaks. It is genuinely useful for exactly those questions, and useless
    for allocation volume: a transient allocation, however large, is freed
    before this snapshot is taken and simply will not appear here (see
    `_format_test_peaks` for the measurement that covers that case instead).
    """
    header = [
        "blocks still allocated when the session ended, by source line "
        "(retained memory, not total bytes allocated over the run):"
    ]
    entries = sorted(entries_raw, key=lambda e: e.get("size", 0), reverse=True)[:limit]
    if not entries:
        header.append(
            "(no allocation sites recorded — transient allocations are freed before "
            "the session-end snapshot; see the per-benchmark peak table above for "
            "allocation volume)"
        )
        return "\n".join(header)

    root_path = Path(root).resolve() if root is not None else None
    table = [f"{'site':<{_SITE_WIDTH}} {'size':>{_SIZE_WIDTH}} {'blocks':>{_BLOCKS_WIDTH}}"]
    for e in entries:
        site = _display_site(e.get("file", "?"), e.get("line", 0), root_path)
        table.append(
            f"{site:<{_SITE_WIDTH}} {_format_size(e.get('size', 0)):>{_SIZE_WIDTH}} "
            f"{e.get('count', 0):>{_BLOCKS_WIDTH}}"
        )
    return "\n".join([*header, "", *table])


def format_mem(json_path: str | Path, limit: int = _TOP, root: str | Path | None = None) -> str:
    path = Path(json_path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return f"(no memory profile was written to {path})"

    peaks_section = _format_test_peaks(doc.get("test_peaks", {}), limit)
    sites_section = _format_retained_sites(doc.get("top", []), limit, root)
    return f"{peaks_section}\n\n{sites_section}"


def run_profile(
    root: str | Path, node_ids: Sequence[str], cfg: Config, log: IO[str] | None = None
) -> Report:
    """Profile the declared benchmarks, in two passes, and return the summaries."""
    try:
        runner.validate_node_ids(node_ids)
    except ValueError as e:
        raise ProfileError(str(e)) from e
    root = Path(root)
    out_dir = root / PROFILE_DIR
    # PROFILE_DIR is a fixed, gitignored, multi-component path (see cli/init.py)
    # that the scope gate never has to see change — exactly the shape of thing
    # nothing else stops an agent from pre-creating as a symlink. mkdir(...,
    # exist_ok=True) does not notice a symlinked directory already sitting
    # there (it only checks that the target IS a directory, which a symlink to
    # one satisfies), and the unlink calls below would then remove and the
    # subprocess passes below would then write through it, reaching a file
    # outside the repository. Checked before either happens.
    try:
        containment.ensure_contained(root, out_dir, PROFILE_DIR, "profile")
    except containment.ContainmentError as e:
        raise ProfileError(str(e)) from e
    out_dir.mkdir(parents=True, exist_ok=True)
    cpu_path, mem_path = out_dir / "cpu.prof", out_dir / "mem.json"
    for target, name in ((cpu_path, "cpu.prof"), (mem_path, "mem.json")):
        try:
            containment.ensure_contained(root, target, f"{PROFILE_DIR}/{name}", "profile")
        except containment.ContainmentError as e:
            raise ProfileError(str(e)) from e
    cpu_path.unlink(missing_ok=True)
    mem_path.unlink(missing_ok=True)

    base_env = runner.bench_env(root, cfg)
    # The CPU pass keeps using pytest-benchmark's own timing loop — cProfile
    # doesn't care how many rounds run. The memory pass explicitly disables
    # that plugin ("-p no:benchmark") and supplies its own `benchmark`
    # fixture instead (see profiling._MemBenchmarkFixture): pytest-benchmark
    # picks its round count from a time budget, so it runs far more rounds
    # for a fast, cheap callable than for a slow one — and that round-count
    # difference, not the callable's own allocations, would dominate
    # tracemalloc's peak. `--benchmark-*` flags are meaningless (and
    # unrecognised) once that plugin is disabled, so the memory pass gets
    # none of them; node_ids alone already select exactly the tests to run.
    passes = (
        (
            profiling.CPU_ENV,
            cpu_path,
            # bench_env disables pytest's entry-point plugin autoloading (see
            # runner._DISABLE_AUTOLOAD_ENV), and pytest-benchmark is itself an
            # entry-point plugin, so this pass — the one that still relies on
            # its timing loop — must load it explicitly or `--benchmark-*`
            # below is "unrecognized arguments".
            (
                "-p",
                runner.BENCHMARK_PLUGIN,
                "--benchmark-only",
                "--benchmark-max-time=0.5",
                "--benchmark-min-rounds=1",
            ),
        ),
        (profiling.MEM_ENV, mem_path, ("-p", "no:benchmark")),
    )
    for env_var, target, extra_args in passes:
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
            *extra_args,
            "--",  # see runner.validate_node_ids: node_ids are untrusted, never shell-typed
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
