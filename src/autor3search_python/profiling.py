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
    global _profiler, _tracing
    if os.environ.get(CPU_ENV):
        _profiler = cProfile.Profile()
        _profiler.enable()
    elif os.environ.get(MEM_ENV):
        # Never both in one session: tracemalloc's overhead would distort the
        # cProfile numbers, and cProfile's would distort the allocation sites.
        tracemalloc.start(1)
        _tracing = True


def pytest_unconfigure(config) -> None:  # noqa: ARG001
    global _profiler, _tracing
    if _profiler is not None:
        _profiler.disable()
        _profiler.dump_stats(os.environ[CPU_ENV])
        _profiler = None
    if _tracing:
        snapshot = tracemalloc.take_snapshot()
        tracemalloc.stop()
        _tracing = False
        top = []
        for stat in snapshot.statistics("lineno")[:_TOP_ENTRIES]:
            frame = stat.traceback[0]
            top.append(
                {
                    "file": frame.filename,
                    "line": frame.lineno,
                    "size": stat.size,
                    "count": stat.count,
                }
            )
        with open(os.environ[MEM_ENV], "w", encoding="utf-8") as f:
            json.dump({"top": top}, f)
