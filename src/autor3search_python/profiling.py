"""A pytest plugin that profiles a benchmark session.

Shipped inside this package, so it is importable whenever the harness runs and
nothing has to be added to the repository being optimized. Loaded with
`-p autor3search_python.profiling`, and activated only when the corresponding
environment variable names an output path — so importing it is always harmless.
"""

from __future__ import annotations

import cProfile
import json
import os
import tracemalloc

CPU_ENV = "AUTOR3SEARCH_PYTHON_CPU_PROFILE"
MEM_ENV = "AUTOR3SEARCH_PYTHON_MEM_PROFILE"

_TOP_ENTRIES = 40

_profiler: cProfile.Profile | None = None
_tracing = False


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
        # get_traced_memory()'s peak is the honest "how much memory did this
        # session use" figure: it is the high-water mark of everything traced
        # since pytest_collection_finish, not just what is still reachable
        # now. The per-line breakdown below answers a different question
        # (see the label written alongside it in profile.format_mem).
        _current, peak = tracemalloc.get_traced_memory()
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
        doc = {"top": top, "peak_bytes": peak}
        with open(os.environ[MEM_ENV], "w", encoding="utf-8") as f:
            json.dump(doc, f)
