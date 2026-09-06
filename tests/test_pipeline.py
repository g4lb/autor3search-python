import hashlib

import pytest

from autor3search_python import benchio, config, freeze, gitx, pipeline, state, verdict
from tests.conftest import git


def commit_all(repo, message):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "--short=7", "HEAD")


@pytest.fixture
def run(git_repo, tmp_path):
    """A repository with a baseline recorded, ready for eval."""
    repo = git_repo
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "mod.py").write_text("def work():\n    return sum(range(100))\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_mod.py").write_text(
        "from pkg.mod import work\n\n\n"
        "def test_work(benchmark):\n    assert benchmark(work) == 4950\n"
    )
    (repo / "tests" / "conftest.py").write_text("")
    cfg_dir = repo / ".autor3search"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text(
        'benchmarks = ["tests/test_mod.py::test_work"]\nscope = ["pkg/..."]\ncount = 4\n'
    )
    head = commit_all(repo, "setup")

    sd = tmp_path / "state"
    sd.mkdir()
    m = freeze.snapshot(repo, sd / freeze.STORE_DIR, ["tests/test_mod.py", "tests/conftest.py"])
    m.save(sd / freeze.MANIFEST_PATH)
    # A REAL linked worktree, not a bare directory: test_a_real_speedup_keeps
    # exercises the genuine gitx.checkout_detached on KEEP (only some tests
    # mock it out), and a plain mkdir'd directory is not a git repository —
    # "git checkout -f --detach" against it fails with exit 128.
    gitx.add_worktree(repo, sd / state.WORKTREE_NAME, head)
    base = state.Baseline(
        tag="t",
        branch="autor3search-python/t",
        commit=head,
        measure_commit=head,
        created_at="2026-09-06T00:00:00+00:00",
        benchmarks=("tests/test_mod.py::test_work",),
        config_sha256=hashlib.sha256((cfg_dir / "config.toml").read_bytes()).hexdigest(),
    )
    base.save(sd / state.BASELINE_FILE)
    return repo, sd, base


def fake_measure(base_value=1.0, cand_value=1.0, n=4):
    def fn(_opts):
        b, c = benchio.Set(), benchio.Set()
        for i in range(n):
            b.record("tests/test_mod.py::test_work", base_value + i * 1e-6)
            c.record("tests/test_mod.py::test_work", cand_value + i * 1e-6)
        return b, c

    return fn


def options(run, monkeypatch, measure_fn=None, skip_gates=True):
    repo, sd, base = run
    cfg = config.load(repo / ".autor3search" / "config.toml")
    if skip_gates:
        cfg = config.Config(**{**cfg.__dict__, "gates": config.Gates(False, False)})
        monkeypatch.setattr(
            "autor3search_python.runner.Runner.pytest_gate",
            lambda self: __import__("autor3search_python.runner", fromlist=["Result"]).Result(
                (), "", "", 0, False, 0.0
            ),
        )
    monkeypatch.setattr(
        "autor3search_python.gitx.head_commit",
        lambda d: (
            base.measure_commit
            if str(d).endswith(state.WORKTREE_NAME)
            else __import__("subprocess")
            .run(
                ["git", "rev-parse", "--short=7", "HEAD"],
                cwd=str(d),
                capture_output=True,
                text=True,
            )
            .stdout.strip()
        ),
    )
    return pipeline.Options(
        root=repo,
        state_dir=sd,
        cfg=cfg,
        base=base,
        measure_fn=measure_fn or fake_measure(),
    )


# --- the scope gate -------------------------------------------------------


def test_out_of_scope_edit_fails_before_anything_is_built(run, monkeypatch):
    repo, _, _ = run
    (repo / "elsewhere.py").write_text("x = 1\n")
    commit_all(repo, "out of scope")
    result, m = pipeline.evaluate(options(run, monkeypatch))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.SCOPE
    assert "elsewhere.py" in result.message
    assert m is None


def test_in_scope_edit_passes_the_scope_gate(run, monkeypatch):
    repo, _, _ = run
    (repo / "pkg" / "mod.py").write_text("def work():\n    return 4950\n")
    commit_all(repo, "in scope")
    result, _ = pipeline.evaluate(options(run, monkeypatch))
    assert result.reason is not verdict.Reason.SCOPE


@pytest.mark.parametrize(
    "name",
    [
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "requirements-dev.txt",
        "constraints.txt",
        "poetry.lock",
        "uv.lock",
        "pdm.lock",
        "Pipfile",
        "Pipfile.lock",
    ],
)
def test_dependency_files_are_rejected_regardless_of_scope(run, monkeypatch, name):
    """A swapped dependency changes WHAT is measured, not just how fast it runs."""
    repo, _, _ = run
    (repo / "pkg").mkdir(exist_ok=True)
    (repo / name).write_text("# changed\n")
    commit_all(repo, "dependency change")
    result, _ = pipeline.evaluate(options(run, monkeypatch))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.SCOPE
    assert name in result.message


def test_harness_owned_files_are_not_scope_violations(run, monkeypatch):
    repo, _, _ = run
    (repo / "results.tsv").write_text("x\n")
    (repo / "run.log").write_text("x\n")
    commit_all(repo, "harness output")
    result, _ = pipeline.evaluate(options(run, monkeypatch))
    assert result.reason is not verdict.Reason.SCOPE


# --- config integrity -----------------------------------------------------


def test_editing_the_config_fails_the_run(run, monkeypatch):
    """Raising max_regress_pct mid-run would defeat the guard."""
    repo, _, _ = run
    (repo / ".autor3search" / "config.toml").write_text(
        'benchmarks = ["tests/test_mod.py::test_work"]\n'
        'scope = ["pkg/..."]\ncount = 4\nmax_regress_pct = 500.0\n'
    )
    commit_all(repo, "loosen the rules")
    result, _ = pipeline.evaluate(options(run, monkeypatch))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.CONFIG_CHANGED


# --- the frozen set -------------------------------------------------------


def test_a_weakened_test_is_restored_not_argued_with(run, monkeypatch):
    repo, _, _ = run
    original = (repo / "tests" / "test_mod.py").read_text()
    (repo / "tests" / "test_mod.py").write_text("def test_work(benchmark):\n    pass\n")
    commit_all(repo, "weaken the test")
    pipeline.evaluate(options(run, monkeypatch))
    assert (repo / "tests" / "test_mod.py").read_text() == original


def test_a_deleted_test_is_restored(run, monkeypatch):
    repo, _, _ = run
    (repo / "tests" / "test_mod.py").unlink()
    commit_all(repo, "delete the test")
    pipeline.evaluate(options(run, monkeypatch))
    assert (repo / "tests" / "test_mod.py").exists()


def test_an_edited_conftest_is_restored(run, monkeypatch):
    """conftest can bend collection and fixtures without touching a test file."""
    repo, _, _ = run
    (repo / "tests" / "conftest.py").write_text("collect_ignore = ['test_mod.py']\n")
    commit_all(repo, "bend collection")
    pipeline.evaluate(options(run, monkeypatch))
    assert (repo / "tests" / "conftest.py").read_text() == ""


def test_a_new_test_file_is_rejected(run, monkeypatch):
    """An easier benchmark added mid-run is not a benchmark this run measures."""
    repo, _, _ = run
    (repo / "tests" / "test_easy.py").write_text(
        "def test_easy(benchmark):\n    benchmark(lambda: None)\n"
    )
    commit_all(repo, "add an easy benchmark")
    result, _ = pipeline.evaluate(options(run, monkeypatch))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.NEW_TEST_FILE
    assert "tests/test_easy.py" in result.message


def test_an_unfrozen_file_may_be_added(run, monkeypatch):
    repo, sd, base = run
    (repo / "tests" / "test_extra.py").write_text("def test_extra():\n    pass\n")
    commit_all(repo, "add an exempt file")
    opts = options(run, monkeypatch)
    opts.cfg = config.Config(**{**opts.cfg.__dict__, "unfreeze": ("tests/test_extra.py",)})
    result, _ = pipeline.evaluate(opts)
    assert result.reason is not verdict.Reason.NEW_TEST_FILE


def test_a_symlinked_frozen_file_is_a_fail_not_a_crash(run, monkeypatch, tmp_path):
    repo, _, _ = run
    outside = tmp_path / "outside.py"
    outside.write_text("def test_work(benchmark):\n    pass\n")
    (repo / "tests" / "test_mod.py").unlink()
    (repo / "tests" / "test_mod.py").symlink_to(outside)
    result, _ = pipeline.evaluate(options(run, monkeypatch))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.SYMLINK_SWAP


# --- worktree integrity ---------------------------------------------------


def test_a_moved_baseline_worktree_is_detected(run, monkeypatch):
    """Editing the baseline to make it slow would make every candidate 'improve'."""
    repo, sd, base = run
    monkeypatch.setattr(
        "autor3search_python.gitx.head_commit",
        lambda d: "deadbee" if str(d).endswith(state.WORKTREE_NAME) else base.commit,
    )
    opts = pipeline.Options(
        root=repo,
        state_dir=sd,
        cfg=config.Config(
            **{
                **config.load(repo / ".autor3search" / "config.toml").__dict__,
                "gates": config.Gates(False, False),
            }
        ),
        base=base,
        measure_fn=fake_measure(),
    )
    monkeypatch.setattr(
        "autor3search_python.runner.Runner.pytest_gate",
        lambda self: __import__("autor3search_python.runner", fromlist=["Result"]).Result(
            (), "", "", 0, False, 0.0
        ),
    )
    result, _ = pipeline.evaluate(opts)
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.BASELINE_TAMPERED


# --- correctness gates ----------------------------------------------------


def test_a_syntax_error_crashes_at_the_compile_gate(run, monkeypatch):
    repo, _, _ = run
    (repo / "pkg" / "mod.py").write_text("def work(\n")
    commit_all(repo, "break the syntax")
    opts = options(run, monkeypatch, skip_gates=False)
    opts.cfg = config.Config(**{**opts.cfg.__dict__, "gates": config.Gates(True, False)})
    result, _ = pipeline.evaluate(opts)
    assert result.status is verdict.Status.CRASH
    assert result.reason is verdict.Reason.COMPILE


def test_a_module_that_raises_fails_at_the_import_gate(run, monkeypatch):
    repo, _, _ = run
    (repo / "pkg" / "mod.py").write_text("raise RuntimeError('boom')\n")
    commit_all(repo, "break the import")
    opts = options(run, monkeypatch, skip_gates=False)
    opts.cfg = config.Config(**{**opts.cfg.__dict__, "gates": config.Gates(False, True)})
    result, _ = pipeline.evaluate(opts)
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.IMPORT


def test_a_failing_test_fails_the_experiment(run, monkeypatch):
    """Correctness is never traded for speed."""
    repo, _, _ = run
    (repo / "pkg" / "mod.py").write_text("def work():\n    return 0\n")
    commit_all(repo, "break behavior")
    opts = options(run, monkeypatch, skip_gates=True)
    monkeypatch.setattr(
        "autor3search_python.runner.Runner.pytest_gate",
        lambda self: __import__("autor3search_python.runner", fromlist=["Result"]).Result(
            (), "", "assert 0 == 4950", 1, False, 0.1
        ),
    )
    result, _ = pipeline.evaluate(opts)
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.TESTS


# --- scoring and the advancing baseline -----------------------------------


def test_a_real_speedup_keeps(run, monkeypatch):
    result, m = pipeline.evaluate(options(run, monkeypatch, measure_fn=fake_measure(2.0, 1.0, n=8)))
    assert result.status is verdict.Status.KEEP
    assert result.score == pytest.approx(0.5, rel=1e-3)
    assert m is not None and len(m.time) == 1


def test_no_change_discards(run, monkeypatch):
    result, _ = pipeline.evaluate(options(run, monkeypatch, measure_fn=fake_measure(1.0, 1.0, n=8)))
    assert result.status is verdict.Status.DISCARD


def test_keep_advances_the_measurement_commit(run, monkeypatch, tmp_path):
    """Without this, a later no-op coasts to KEEP on an earlier win."""
    repo, sd, base = run
    (repo / "pkg" / "mod.py").write_text("def work():\n    return 4950\n")
    new_head = commit_all(repo, "faster")
    moved = []
    monkeypatch.setattr(
        "autor3search_python.gitx.checkout_detached",
        lambda d, c: moved.append((str(d), c)),
    )
    opts = options(run, monkeypatch, measure_fn=fake_measure(2.0, 1.0, n=8))
    result, _ = pipeline.evaluate(opts)
    assert result.status is verdict.Status.KEEP
    assert moved == [(str(sd / state.WORKTREE_NAME), new_head)]
    assert state.load_baseline(sd / state.BASELINE_FILE).measure_commit == new_head


def test_discard_does_not_advance_the_measurement_commit(run, monkeypatch):
    repo, sd, base = run
    before = state.load_baseline(sd / state.BASELINE_FILE).measure_commit
    pipeline.evaluate(options(run, monkeypatch, measure_fn=fake_measure(1.0, 1.0, n=8)))
    assert state.load_baseline(sd / state.BASELINE_FILE).measure_commit == before


def test_a_measurement_failure_is_a_crash_not_an_exception(run, monkeypatch):
    def boom(_opts):
        raise __import__("autor3search_python.measure", fromlist=["MeasureError"]).MeasureError(
            "no benchmarks matched"
        )

    result, _ = pipeline.evaluate(options(run, monkeypatch, measure_fn=boom))
    assert result.status is verdict.Status.CRASH


def test_is_dependency_file():
    assert pipeline.is_dependency_file("pyproject.toml")
    assert pipeline.is_dependency_file("requirements-dev.txt")
    assert pipeline.is_dependency_file("constraints.txt")
    assert not pipeline.is_dependency_file("pkg/pyproject.toml")
    assert not pipeline.is_dependency_file("pkg/mod.py")
