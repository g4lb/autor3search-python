"""A pytest plugin that profiles a benchmark session.

Shipped inside this package, so it is importable whenever the harness runs and
nothing has to be added to the repository being optimized. Loaded with
`-p autor3search_python.profiling`, and activated only when the corresponding
environment variable names an output path — so importing it is always harmless.

This module is also imported directly (not just loaded as a "-p" plugin) by
`profile.py`, in the harness's OWN process — which, per the README's install
instructions (`uv tool install` / `pipx install`), commonly runs from an
isolated tool venv that never has `pytest` in it at all; only the *target*
repository's interpreter (`cfg.python`), which actually runs the "-p" plugin
in a fresh subprocess, is guaranteed to. `[project.dependencies]` must stay
empty, so the `pytest` import below is best-effort: it succeeds inside the
subprocess that actually profiles a benchmark session (pytest is necessarily
already running there), and degrades to a no-op hookwrapper everywhere else.
"""

from __future__ import annotations

import cProfile
import json
import os
import tracemalloc

try:
    import pytest
except ImportError:  # pragma: no cover - exercised only by an environment
    # that lacks pytest, i.e. never in this project's own CI/dev venv.
    pytest = None  # type: ignore[assignment]

CPU_ENV = "AUTOR3SEARCH_PYTHON_CPU_PROFILE"
MEM_ENV = "AUTOR3SEARCH_PYTHON_MEM_PROFILE"

_TOP_ENTRIES = 40

_profiler: cProfile.Profile | None = None
_tracing = False
# Peak additional traced memory recorded during each test, keyed by node id.
# See pytest_runtest_call for what "peak" means here and why it is process-
# wide rather than filtered to the target repository.
_test_peaks: dict[str, int] = {}


def pytest_configure(config) -> None:  # noqa: ARG001
    global _profiler
    if os.environ.get(CPU_ENV):
        _profiler = cProfile.Profile()
        _profiler.enable()
    # The memory pass deliberately does NOT start here: pytest_configure runs
    # before collection, so tracemalloc would spend its whole budget on
    # pytest's own import machinery (and the user's module-level imports)
    # rather than on what the benchmark actually does. It starts in
    # pytest_collection_finish instead, once all of that is behind us.


def pytest_collection_finish(session) -> None:  # noqa: ARG001
    global _tracing
    if os.environ.get(MEM_ENV) and not _tracing:
        tracemalloc.start(1)
        _tracing = True


if pytest is not None:

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(item):
        """Peak additional traced memory during this one test's call phase.

        `tracemalloc.get_traced_memory()` is process-wide — it was never
        filtered to the target repository, only the session-end snapshot
        below is — so this number is dominated by, but not exclusive to,
        whatever the benchmark itself does: it also carries pytest's and
        pytest-benchmark's own overhead for running that one test.
        `reset_peak()` immediately before the call at least keeps that
        overhead scoped to THIS test rather than accumulating across the
        whole session. This is the honest trade for line-level attribution
        `tracemalloc` cannot give at all for allocations that get freed
        (see `_filters_for` and `pytest_unconfigure` for the retained-memory
        half of the picture, which tracemalloc CAN attribute by source line).
        """
        if not (os.environ.get(MEM_ENV) and _tracing):
            return (yield)
        before, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        try:
            return (yield)
        finally:
            _current, after_peak = tracemalloc.get_traced_memory()
            _test_peaks[item.nodeid] = max(0, after_peak - before)


def _filters_for(root: str) -> list[tracemalloc.Filter]:
    """Keep only the target repository's own files.

    Without this, the top allocation sites are dominated by importlib,
    pluggy and pytest-benchmark — the same for every repository, and
    actively harmful: an agent reading this report would try to "optimize"
    code it is not even allowed to touch.
    """
    return [
        tracemalloc.Filter(True, f"{root}/*"),
        tracemalloc.Filter(False, "*/site-packages/*"),
        tracemalloc.Filter(False, "*/.venv/*"),
        tracemalloc.Filter(False, "*/.autor3search/*"),
    ]


def pytest_unconfigure(config) -> None:  # noqa: ARG001
    global _profiler, _tracing
    if _profiler is not None:
        _profiler.disable()
        _profiler.dump_stats(os.environ[CPU_ENV])
        _profiler = None
    if _tracing:
        # This snapshot only ever reports blocks still live right now — a
        # transient allocation that was already freed is invisible to it,
        # no matter how large it was. That is exactly what per-test peaks
        # (recorded above, in pytest_runtest_call) exist to cover instead;
        # the two are written out separately below and never conflated.
        snapshot = tracemalloc.take_snapshot()
        tracemalloc.stop()
        _tracing = False
        # PYTHONPATH always points bench_env's caller at this exact directory
        # (see runner.bench_env), and Runner launches the subprocess with it
        # as the cwd, so os.getcwd() here is the same tree the benchmark's
        # own modules were imported from.
        root = os.getcwd()
        filtered = snapshot.filter_traces(_filters_for(root))
        top = []
        for stat in filtered.statistics("lineno")[:_TOP_ENTRIES]:
            frame = stat.traceback[0]
            top.append(
                {
                    "file": frame.filename,
                    "line": frame.lineno,
                    "size": stat.size,
                    "count": stat.count,
                }
            )
        doc = {"top": top, "test_peaks": dict(_test_peaks)}
        _test_peaks.clear()
        with open(os.environ[MEM_ENV], "w", encoding="utf-8") as f:
            json.dump(doc, f)
