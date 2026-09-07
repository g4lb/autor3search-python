import sys
import time

import pytest

from autor3search_python import config, runner
from autor3search_python.scope import Matcher


def r(tmp_path, timeout=30):
    return runner.Runner(tmp_path, timeout)


def test_captures_stdout_and_stderr_separately(tmp_path):
    res = r(tmp_path).run(
        sys.executable, "-c", "import sys; print('o'); print('e', file=sys.stderr)"
    )
    assert res.ok() is True
    assert res.stdout.strip() == "o"
    assert res.stderr.strip() == "e"


def test_reports_a_nonzero_exit(tmp_path):
    res = r(tmp_path).run(sys.executable, "-c", "raise SystemExit(7)")
    assert res.exit_code == 7
    assert res.ok() is False


def test_runs_in_the_given_directory(tmp_path):
    (tmp_path / "marker.txt").write_text("here")
    res = r(tmp_path).run(sys.executable, "-c", "import os; print(os.path.exists('marker.txt'))")
    assert res.stdout.strip() == "True"


def test_timeout_is_reported_not_raised(tmp_path):
    start = time.monotonic()
    res = runner.Runner(tmp_path, 1).run(sys.executable, "-c", "import time; time.sleep(30)")
    assert res.timed_out is True
    assert res.ok() is False
    assert time.monotonic() - start < 20


def test_timeout_kills_grandchildren(tmp_path):
    """go test runs its benchmark as a grandchild; pytest does too. A survivor
    burns CPU and corrupts every later measurement on the machine."""
    marker = tmp_path / "alive.txt"
    script = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', "
        f"\"import time, pathlib; time.sleep(6); pathlib.Path(r'{marker}').write_text('x')\"])\n"
        "time.sleep(30)\n"
    )
    res = runner.Runner(tmp_path, 1).run(sys.executable, "-c", script)
    assert res.timed_out is True
    time.sleep(8)
    assert not marker.exists(), "a grandchild outlived the timeout"


@pytest.mark.slow
def test_recovery_communicate_is_bounded_when_a_grandchild_escapes_the_group(tmp_path):
    """A grandchild that calls os.setsid() escapes _kill_group's killpg
    entirely and, inheriting the pipe fd, can keep it open indefinitely. An
    unbounded recovery communicate() would then block forever; Runner.run
    must still return, with timed_out=True, within a bounded wall time."""
    script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        "'import os, time; os.setsid(); time.sleep(30)'])\n"
        "time.sleep(30)\n"
    )
    start = time.monotonic()
    res = runner.Runner(tmp_path, 1).run(sys.executable, "-c", script)
    elapsed = time.monotonic() - start
    assert res.timed_out is True
    assert elapsed < 20, f"run() blocked for {elapsed:.1f}s instead of returning"


def test_output_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "CAP_BYTES", 1000)
    res = r(tmp_path).run(sys.executable, "-c", "print('x' * 50000)")
    assert len(res.stdout) < 5000
    assert "truncated" in res.stdout


def test_tail_prefers_stderr_and_falls_back_to_stdout(tmp_path):
    res = r(tmp_path).run(
        sys.executable, "-c", "import sys; [print(i, file=sys.stderr) for i in range(10)]"
    )
    assert res.tail(3).splitlines() == ["7", "8", "9"]
    res2 = r(tmp_path).run(sys.executable, "-c", "[print(i) for i in range(10)]")
    assert res2.tail(2).splitlines() == ["8", "9"]


def test_log_receives_the_command_line_and_output(tmp_path):
    import io

    log = io.StringIO()
    runner.Runner(tmp_path, 30, log=log).run(sys.executable, "-c", "print('hello')")
    text = log.getvalue()
    assert "hello" in text and "exit=0" in text


def test_bench_env_pins_hashseed_and_pythonpath(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pkg").mkdir()
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    env = runner.bench_env(tmp_path, config.default(), base_env={"PATH": "/bin"})
    assert env["PYTHONHASHSEED"] == "0"
    parts = env["PYTHONPATH"].split(":")
    assert str(tmp_path) in parts
    assert str(tmp_path / "src") in parts


def test_bench_env_omits_hashseed_when_disabled(tmp_path):
    cfg = config.default().__class__(hashseed=-1)
    assert "PYTHONHASHSEED" not in runner.bench_env(tmp_path, cfg, base_env={})


def test_bench_env_prepends_configured_pythonpath(tmp_path):
    cfg = config.default().__class__(pythonpath=("lib",))
    env = runner.bench_env(tmp_path, cfg, base_env={})
    assert env["PYTHONPATH"].split(":")[0] == str(tmp_path / "lib")


def test_compile_gate_fails_on_a_syntax_error(tmp_path):
    (tmp_path / "bad.py").write_text("def f(\n")
    res = r(tmp_path).compile_gate(["."])
    assert res.ok() is False


def test_compile_gate_passes_on_valid_source(tmp_path):
    (tmp_path / "good.py").write_text("def f():\n    return 1\n")
    assert r(tmp_path).compile_gate(["."]).ok() is True


def test_compile_gate_excludes_dot_prefixed_paths(tmp_path):
    """Pinning the -x exclude: deleting it would leave this suite green while
    silently reintroducing the exact regression that already happened once."""
    (tmp_path / "good.py").write_text("def f():\n    return 1\n")
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "broken.py").write_text("def f(\n")
    (tmp_path / ".hidden.py").write_text("def g(\n")
    res = r(tmp_path).compile_gate(["."])
    assert res.ok() is True
    assert "broken.py" not in res.stdout + res.stderr
    assert "hidden.py" not in res.stdout + res.stderr


def test_import_gate_fails_on_a_module_that_raises(tmp_path):
    (tmp_path / "boom.py").write_text("raise RuntimeError('nope')\n")
    res = runner.Runner(tmp_path, 30, env=runner.bench_env(tmp_path, config.default())).import_gate(
        ["boom"]
    )
    assert res.ok() is False
    assert "nope" in res.stdout + res.stderr


def test_import_gate_passes_on_a_sound_module(tmp_path):
    (tmp_path / "fine.py").write_text("VALUE = 1\n")
    res = runner.Runner(tmp_path, 30, env=runner.bench_env(tmp_path, config.default())).import_gate(
        ["fine"]
    )
    assert res.ok() is True


def test_import_gate_with_no_modules_is_a_pass(tmp_path):
    assert r(tmp_path).import_gate([]).ok() is True


def test_importable_modules_finds_in_scope_dotted_names(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pkg").mkdir()
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    (tmp_path / "src" / "pkg" / "mod.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("")
    got = runner.importable_modules(tmp_path, Matcher(["src/..."]))
    assert got == ["pkg", "pkg.mod"]


def test_importable_modules_skips_test_files(tmp_path):
    (tmp_path / "test_x.py").write_text("")
    (tmp_path / "conftest.py").write_text("")
    (tmp_path / "real.py").write_text("")
    assert runner.importable_modules(tmp_path, Matcher(["./..."])) == ["real"]


def test_compile_gate_skips_the_directories_discovery_skips(tmp_path):
    """The exclude is derived from discover.SKIP_DIRS, so build/ and dist/ are
    out. They hold vendored or stale copies the agent never touched; compiling
    them turned a syntax error nobody was measuring into a CRASH verdict."""
    (tmp_path / "good.py").write_text("def f():\n    return 1\n")
    for name in ("build", "dist", "node_modules", "__pycache__"):
        d = tmp_path / name
        d.mkdir()
        (d / "broken.py").write_text("def f(\n")
    res = r(tmp_path).compile_gate(["."])
    assert res.ok() is True
    assert "broken.py" not in res.stdout + res.stderr


def test_pytest_gate_resolves_modules_the_same_way_bench_does(tmp_path, monkeypatch):
    """A gate and a measurement that import differently are two programs, and
    only one of them gets a verdict."""
    seen = []
    rr = r(tmp_path)
    monkeypatch.setattr(
        rr, "python_run", lambda *a: seen.append(a) or runner.Result((), "", "", 0, False, 0.0)
    )
    rr.pytest_gate()
    rr.bench(["tests/test_x.py::test_y"], tmp_path / "out.json", config.default())
    gate_args, bench_args = seen
    assert "--import-mode=importlib" in gate_args
    assert "--import-mode=importlib" in bench_args
