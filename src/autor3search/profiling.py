"""A pytest plugin that profiles a benchmark session.

Shipped inside this package, so it is importable whenever the harness runs and
nothing has to be added to the repository being optimized. Loaded with
`-p autor3search.profiling`, and activated only when the corresponding
environment variable names an output path — so importing it is always harmless.

This module is also imported directly (not just loaded as a "-p" plugin) by
`profile.py`, in the harness's OWN process — which, per the README's install
instructions (`uv tool install` / `pipx install`), commonly runs from an
isolated tool venv that never has `pytest` in it at all; only the *target*
repository's interpreter (`cfg.python`), which actually runs the "-p" plugin
in a fresh subprocess, is guaranteed to. `[project.dependencies]` must stay
empty, so the `pytest` import below is best-effort: it succeeds inside the
subprocess that actually profiles a benchmark session (pytest is necessarily
already running there), and simply leaves the memory pass's `benchmark`
fixture undefined everywhere else, where it is never used anyway.
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
# How many times the memory pass calls each benchmarked callable directly.
# Enough to let a one-time cost (a lazy import on the first call, say) get
# outvoted by the minimum below; small enough to keep the pass fast even
# when a benchmark itself is slow, since this replaces pytest-benchmark's
# own time-budgeted round count entirely (see _MemBenchmarkFixture).
_MEASURED_CALLS = 5

_profiler: cProfile.Profile | None = None
_tracing = False
# Steady-state peak additional traced memory per benchmark, keyed by node
# id. Populated by _MemBenchmarkFixture, not by wrapping pytest-benchmark's
# own timing loop — see that class for why.
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
    if os.environ.get(MEM_ENV):
        # The memory pass runs with "-p no:benchmark" (see profile.run_profile)
        # so pytest-benchmark's own timing loop — whose round count varies
        # inversely with how fast the callable is, and would otherwise
        # dominate the peak below — never runs at all. Disabling that plugin
        # means IT no longer registers the "benchmark" mark, so a target
        # repository's own `@pytest.mark.benchmark(...)` needs this instead,
        # or it is an unregistered-mark warning (an error under
        # filterwarnings = error).
        config.addinivalue_line(
            "markers",
            "benchmark(*args, **kwargs): pytest-benchmark's marker, accepted "
            "as a no-op here since the allocation pass disables that plugin",
        )


def pytest_collection_finish(session) -> None:  # noqa: ARG001
    global _tracing
    if os.environ.get(MEM_ENV) and not _tracing:
        tracemalloc.start(1)
        _tracing = True


def _measure_peak(nodeid: str, call) -> object:
    """Call `call()` `_MEASURED_CALLS` times and keep the minimum peak.

    Each call is bracketed by `reset_peak()`/`get_traced_memory()`, so each
    of the `_MEASURED_CALLS` samples is that one call's own high-water mark
    above whatever was already traced. Taking the MINIMUM across calls,
    rather than the first or the mean, is what makes the result a
    steady-state figure: a one-time cost (a lazy import on the first call,
    say) inflates exactly one sample, and the minimum ignores it — the same
    regime pytest-benchmark's own warmup targets for timing, which is why
    it is the right choice here too: both halves of the profile then
    describe the same regime.
    """
    peaks = []
    result = None
    for _ in range(_MEASURED_CALLS):
        before, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        result = call()
        _current, after = tracemalloc.get_traced_memory()
        peaks.append(max(0, after - before))
    _test_peaks[nodeid] = min(peaks)
    return result


class _MemBenchmarkFixture:
    """A minimal stand-in for pytest-benchmark's `benchmark` fixture.

    Used only for the memory pass, which loads this plugin with
    "-p no:benchmark" (see profile.run_profile) specifically so
    pytest-benchmark's own timing loop never runs: that loop's round count
    varies inversely with how fast the callable is (it keeps calling until
    a time budget is spent), so pytest-benchmark's own per-round
    bookkeeping — not the callable's own allocations — would dominate
    tracemalloc's peak for anything fast and cheap, and rank it ABOVE a
    slower callable that allocates a lot. Calling the callable a small,
    fixed number of times instead (see _measure_peak) makes every benchmark
    execute under equal conditions regardless of its own speed.

    Supports the slice of pytest-benchmark's fixture surface a target
    repository's tests are expected to use: `benchmark(fn, *args, **kwargs)`,
    `benchmark.pedantic(...)`, and `benchmark.extra_info`.
    """

    def __init__(self, nodeid: str) -> None:
        self._nodeid = nodeid
        self.extra_info: dict = {}

    def __call__(self, fn, *args, **kwargs):
        return _measure_peak(self._nodeid, lambda: fn(*args, **kwargs))

    def pedantic(
        self,
        target,
        args=(),
        kwargs=None,
        setup=None,
        teardown=None,
        rounds=1,  # noqa: ARG002
        warmup_rounds=0,  # noqa: ARG002
        iterations=1,  # noqa: ARG002
    ):
        kwargs = kwargs or {}

        def call_once():
            call_args, call_kwargs = args, kwargs
            if setup is not None:
                maybe = setup()
                if maybe is not None:
                    call_args, call_kwargs = maybe
            result = target(*call_args, **call_kwargs)
            if teardown is not None:
                teardown(*call_args, **call_kwargs)
            return result

        return _measure_peak(self._nodeid, call_once)


if pytest is not None and os.environ.get(MEM_ENV):
    # Only defined for the memory pass's own subprocess (MEM_ENV is set in
    # its environment before this module is even imported — see
    # profile.run_profile). The CPU pass never sees this: it keeps using
    # pytest-benchmark's real "benchmark" fixture, unaffected by any of this.

    @pytest.fixture
    def benchmark(request):
        return _MemBenchmarkFixture(request.node.nodeid)


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
        # (recorded above, by _MemBenchmarkFixture/_measure_peak) exist to
        # cover instead; the two are written out separately below and never
        # conflated.
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
