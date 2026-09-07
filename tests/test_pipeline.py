import hashlib

import pytest

from autor3search_python import (
    benchio,
    config,
    discover,
    freeze,
    gitx,
    pipeline,
    runner,
    state,
    verdict,
)
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
    # No gitx.head_commit patch here on purpose: the fixture builds a REAL
    # linked worktree, so the worktree-integrity check can exercise real git
    # and actually observe a mismatch when there is one. Faking it to always
    # equal base.measure_commit would silently disable that check in every
    # test that goes through this helper.
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
    # The default fake_measure() reports identical base/candidate values, so an
    # in-scope edit that clears every gate runs all the way to a real DISCARD
    # verdict — not merely "not SCOPE", which would also pass on a crash.
    assert result.status is verdict.Status.DISCARD
    assert result.reason is verdict.Reason.NO_IMPROVEMENT


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


# --- files that change what is measured -----------------------------------
#
# Every test in this section widens scope to the DEFAULT "./..." — what `init`
# writes and what the reviewer's repository ran. The fixture's narrower
# "pkg/..." would have the ordinary scope gate reject these root files first,
# so the tests would stay green with the new gates deleted, proving nothing.


def test_every_pytest_config_file_is_rejected_by_one_gate_or_the_other():
    """ONE list, two modules, and this is the join that keeps them honest.

    doctor scans discover.PYTEST_CONFIG_FILES for coverage in addopts; the
    scope gate rejects edits to them. The two lists diverged once — doctor knew
    all four, the gate knew two — and the two the gate did not know were a
    working route to a fabricated 90% "improvement". Adding a fifth pytest
    config file to the shared constant must close this gate too, with no second
    edit to remember.
    """
    for name in discover.PYTEST_CONFIG_FILES:
        assert pipeline.is_dependency_file(name) or pipeline.is_measurement_config_file(name), (
            f"{name} is a pytest config file doctor knows about but no gate rejects"
        )


def wide_scope(opts):
    """The default './...' scope: these files are in scope, and rejected anyway."""
    opts.cfg = config.Config(**{**opts.cfg.__dict__, "scope": ("./...",)})
    return opts


@pytest.mark.parametrize(
    "name", ["pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "tox.ini"]
)
def test_pytest_config_files_are_rejected_regardless_of_scope(run, monkeypatch, name):
    """pytest reads these, so an addopts line changes what is collected, how it
    runs and how it is timed — not how fast the code is.

    `addopts = -k benchmark` is the reviewer's second cheat: it narrows
    collection until a deliberately broken implementation walks straight past
    the correctness gate that is deliberately not switchable.
    """
    repo, _, _ = run
    (repo / name).write_text("[pytest]\naddopts = -k benchmark\n")
    commit_all(repo, "add a pytest config")
    result, _ = pipeline.evaluate(wide_scope(options(run, monkeypatch)))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.SCOPE
    assert name in result.message
    assert "pytest configuration file" in result.message


def test_the_fake_timer_cheat_is_rejected_before_anything_is_measured(run, monkeypatch):
    """The whole cheat, end to end, as it was actually reproduced on a real repo.

    A comment-only edit to in-scope source, plus a timer that divides
    perf_counter by ten, plus a pytest.ini pointing --benchmark-timer at it.
    Every file involved is in scope, non-test and not a dependency, so before
    the pytest-config gate existed this returned KEEP with score 0.099: a 90%
    "improvement" from a comment. The measurement stub here would report
    exactly that 10x win, and it must never be reached.

    Scope is the DEFAULT "./..." on purpose — the reviewer's repository and
    every repo `init` writes. Under the fixture's narrower "pkg/..." the
    ordinary scope gate would stop faketimer.py first, and the test would pass
    while proving nothing about pytest.ini.
    """
    repo, _, _ = run
    source = repo / "pkg" / "mod.py"
    source.write_text("# a comment, and nothing else changes\n" + source.read_text())
    (repo / "faketimer.py").write_text(
        "import time\n\n\ndef t():\n    return time.perf_counter()/10\n"
    )
    (repo / "pytest.ini").write_text("[pytest]\naddopts = --benchmark-timer=faketimer.t\n")
    commit_all(repo, "cheat")

    called = []

    def measure_fn(opts):
        called.append(opts)
        return fake_measure(10.0, 1.0, n=8)(opts)

    result, m = pipeline.evaluate(wide_scope(options(run, monkeypatch, measure_fn=measure_fn)))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.SCOPE
    assert "pytest.ini" in result.message
    assert m is None
    assert called == [], "the gate must fire before the measurement, not after it"


@pytest.mark.parametrize("name", ["sitecustomize.py", "usercustomize.py"])
def test_startup_hook_files_are_rejected_regardless_of_scope(run, monkeypatch, name):
    """site.py imports these at interpreter startup for anything on sys.path,
    and bench_env puts the tree root there — so this file would run arbitrary
    code inside every gate and every measured process, on the candidate side
    only, before any of them begin."""
    repo, _, _ = run
    (repo / name).write_text("open('marker', 'w').write('ran')\n")
    commit_all(repo, "add a startup hook")
    result, _ = pipeline.evaluate(wide_scope(options(run, monkeypatch)))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.SCOPE
    assert name in result.message
    assert "startup" in result.message


def test_a_sourceless_startup_hook_is_rejected_like_its_source(run, monkeypatch):
    """`sitecustomize.pyc` with no .py beside it imports and runs — that is what
    SourcelessFileLoader is for.

    This is a regression test in the strict sense. The bytecode skip added for
    the stray-__pycache__ fix ran first and waved the file through, and the
    `*.py[cod]` gitignore entry added at the same time meant changed_since
    never reported it either: a hole closed by name, reopened by extension.
    """
    repo, _, _ = run
    (repo / "sitecustomize.pyc").write_bytes(b"\x00compiled\x00")
    commit_all(repo, "a compiled startup hook")
    result, _ = pipeline.evaluate(wide_scope(options(run, monkeypatch)))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.SCOPE
    assert "sitecustomize.pyc" in result.message


def test_editing_a_startup_hook_that_was_there_at_baseline_is_still_rejected(run, monkeypatch):
    """Pins the ORDER of the checks, not just their presence.

    The working-tree check skips a file that was present at baseline, on
    purpose — so for this case the diff loop is the only thing standing there,
    and it only works because every rejection is tested before the bytecode
    skip. Move `is_bytecode` back to the front of the loop and this experiment
    sails through to measurement with a live startup hook in the tree.
    """
    repo, sd, base = run
    (repo / "sitecustomize.pyc").write_bytes(b"\x00original\x00")
    head = commit_all(repo, "a hook that predates the run")
    base.commit = base.measure_commit = head
    gitx.checkout_detached(sd / state.WORKTREE_NAME, head)

    (repo / "sitecustomize.pyc").write_bytes(b"\x00rewritten by the agent\x00")
    commit_all(repo, "rewrite it")
    result, _ = pipeline.evaluate(wide_scope(options(run, monkeypatch)))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.SCOPE
    assert "sitecustomize.pyc" in result.message


def test_a_gitignored_forbidden_file_is_still_rejected(run, monkeypatch, tmp_path):
    """.gitignore decides what every other gate here can see, because they all
    read a git diff that honours it.

    One line makes an untracked pytest.ini invisible to changed_since — which
    passes --exclude-standard — while pytest goes on reading it. The working
    tree, not the diff, is the source of truth for "is this file here".

    The .gitignore is part of the BASELINE commit here, so this test exercises
    the stat-based check alone: with the ignore line predating the run, the
    diff loop has nothing to say about it and only the working-tree stat stands
    between the run and a swapped benchmark timer.
    """
    repo, sd, base = run
    (repo / ".gitignore").write_text("pytest.ini\n")
    head = commit_all(repo, "a repo that ignores pytest.ini")
    base.commit = base.measure_commit = head
    gitx.checkout_detached(sd / state.WORKTREE_NAME, head)
    (repo / "pytest.ini").write_text("[pytest]\naddopts = --benchmark-timer=faketimer.t\n")

    assert "pytest.ini" not in gitx.changed_since(repo, head)

    result, m = pipeline.evaluate(wide_scope(options(run, monkeypatch)))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.SCOPE
    assert "pytest.ini" in result.message
    assert m is None


def test_editing_gitignore_is_rejected(run, monkeypatch):
    """The agent editing .gitignore is editing what every git-based gate sees.

    A modification to a tracked file always shows up in the diff, so the only
    thing an ignore line can hide is a NEW untracked file — an out-of-scope
    module the scope gate never hears about and the interpreter imports
    happily. Refuse the edit rather than reason about what it hid.
    """
    repo, _, _ = run
    (repo / ".gitignore").write_text("secret_helpers/\n")
    commit_all(repo, "widen what git cannot see")
    result, m = pipeline.evaluate(wide_scope(options(run, monkeypatch)))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.SCOPE
    assert ".gitignore" in result.message
    assert m is None


def test_an_added_gitignore_is_rejected_even_when_it_hides_itself(run, monkeypatch):
    """`.gitignore` listing itself is untracked, so no diff mentions it. The
    stat-based check does not consult git at all."""
    repo, _, _ = run
    (repo / ".gitignore").write_text(".gitignore\n")
    result, m = pipeline.evaluate(wide_scope(options(run, monkeypatch)))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.SCOPE
    assert ".gitignore" in result.message
    assert m is None


@pytest.mark.parametrize(
    "name",
    sorted(pipeline.DEPENDENCY_FILES | frozenset(discover.PYTEST_CONFIG_FILES)),
)
def test_every_forbidden_name_is_also_checked_by_statting_the_tree(name):
    """FORBIDDEN_ROOT_FILES must be a UNION, never a difference.

    It drives the one check that does not read a git diff, so a name missing
    from it is a name covered ONLY by the path --exclude-standard makes blind.
    Subtracting DEPENDENCY_FILES from it to pick a nicer message left
    pyproject.toml and setup.cfg in exactly that position.
    """
    assert name in pipeline.FORBIDDEN_ROOT_FILES


def test_a_gitignored_untracked_pyproject_toml_cannot_swap_the_benchmark_timer(run, monkeypatch):
    """The demonstrated attack, end to end, exactly as it was reproduced.

    Commit a .gitignore naming pyproject.toml (as part of the baseline, so the
    .gitignore gate is not what catches this), commit an in-scope fakeclock.py,
    drop an UNTRACKED pyproject.toml carrying
    `[tool.pytest.ini_options] addopts = "--benchmark-timer=fakeclock.t"`, then
    make a comment-only edit to a source file. Every git-based gate is blind to
    the pyproject.toml and pytest reads it anyway: this returned
    `KEEP, score 0.0968 (-90.32%)` — a 90% "improvement" from a comment.
    """
    repo, sd, base = run
    (repo / ".gitignore").write_text("pyproject.toml\n")
    (repo / "fakeclock.py").write_text(
        "import time\n\n\ndef t():\n    return time.perf_counter()/10\n"
    )
    head = commit_all(repo, "a clock, and an ignore line")
    base.commit = base.measure_commit = head
    gitx.checkout_detached(sd / state.WORKTREE_NAME, head)

    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "--benchmark-timer=fakeclock.t"\n'
    )
    source = repo / "pkg" / "mod.py"
    source.write_text("# a comment, and nothing else changes\n" + source.read_text())
    commit_all(repo, "a comment")

    assert "pyproject.toml" not in gitx.changed_since(repo, head)

    called = []

    def measure_fn(opts):
        called.append(opts)
        return fake_measure(10.0, 1.0, n=8)(opts)

    result, m = pipeline.evaluate(wide_scope(options(run, monkeypatch, measure_fn=measure_fn)))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.SCOPE
    assert "pyproject.toml" in result.message
    assert m is None
    assert called == [], "the gate must fire before the measurement, not after it"


def test_a_forbidden_file_present_at_baseline_does_not_fail_every_experiment(run, monkeypatch):
    """A repo that legitimately shipped a tox.ini before the run started must
    still be usable. Untouched, it is not the agent's doing; the diff-based
    check above covers it the moment it IS edited."""
    repo, sd, base = run
    (repo / "tox.ini").write_text("[tox]\n")
    head = commit_all(repo, "a pre-existing tox.ini")
    base.commit = base.measure_commit = head
    gitx.checkout_detached(sd / state.WORKTREE_NAME, head)
    result, _ = pipeline.evaluate(wide_scope(options(run, monkeypatch)))
    assert result.status is verdict.Status.DISCARD
    assert result.reason is verdict.Reason.NO_IMPROVEMENT


def test_stray_bytecode_does_not_trip_a_narrow_scope_gate(run, monkeypatch):
    """The harness's OWN leavings must not lock a run out of its own tool.

    compile_gate runs compileall and pytest writes bytecode too, both of them
    outside the fixture's `pkg/...` scope. Counting those untracked .pyc files
    as agent edits made every experiment after the first return
    scope_violation, permanently, with nothing the agent could do about it.
    """
    repo, _, _ = run
    cache = repo / "tests" / "__pycache__"
    cache.mkdir()
    (cache / "test_mod.cpython-313.pyc").write_bytes(b"\x00\x01\x02")
    (repo / "tests" / "stray.pyo").write_bytes(b"\x00")
    (repo / "pkg" / "mod.py").write_text("def work():\n    return 4950\n")
    commit_all(repo, "in scope, with bytecode lying around")
    result, _ = pipeline.evaluate(options(run, monkeypatch))
    assert result.status is verdict.Status.DISCARD
    assert result.reason is verdict.Reason.NO_IMPROVEMENT


def test_harness_owned_files_are_not_scope_violations(run, monkeypatch):
    repo, _, _ = run
    (repo / "results.tsv").write_text("x\n")
    (repo / "run.log").write_text("x\n")
    commit_all(repo, "harness output")
    result, _ = pipeline.evaluate(options(run, monkeypatch))
    # As above: nothing else changed, so a correct exemption runs all the way
    # to a real DISCARD verdict, not just "any non-SCOPE outcome".
    assert result.status is verdict.Status.DISCARD
    assert result.reason is verdict.Reason.NO_IMPROVEMENT


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
    # Nothing else changed, so a correct exemption runs all the way to a real
    # DISCARD verdict, not just "any non-NEW_TEST_FILE outcome" (which a crash
    # would also satisfy).
    assert result.status is verdict.Status.DISCARD
    assert result.reason is verdict.Reason.NO_IMPROVEMENT


def test_a_symlinked_frozen_file_is_a_fail_not_a_crash(run, monkeypatch, tmp_path):
    repo, _, _ = run
    outside = tmp_path / "outside.py"
    outside.write_text("def test_work(benchmark):\n    pass\n")
    (repo / "tests" / "test_mod.py").unlink()
    (repo / "tests" / "test_mod.py").symlink_to(outside)
    result, _ = pipeline.evaluate(options(run, monkeypatch))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.SYMLINK_SWAP


def test_a_frozen_file_rewritten_during_the_gates_fails_before_measurement(run, monkeypatch):
    """freeze.restore (step 2) runs before compileall, the import gate and the
    full test suite — three subprocesses that execute the candidate's OWN
    code with its tree on PYTHONPATH. Something that runs during one of them
    (a plugin hook, a fixture, an import-time side effect — stood in for
    here by the stubbed pytest_gate itself) rewriting a frozen file after
    restore must be caught before the bench rounds start, not measured as
    written."""
    repo, _, _ = run
    opts = options(run, monkeypatch)  # skip_gates=True: pytest_gate stubbed to a clean pass

    def rogue_pytest_gate(self):
        (repo / "tests" / "conftest.py").write_text("# rewritten mid-gate\n")
        return runner.Result((), "", "", 0, False, 0.0)

    monkeypatch.setattr("autor3search_python.runner.Runner.pytest_gate", rogue_pytest_gate)
    result, measurements = pipeline.evaluate(opts)
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.FREEZE_DRIFT
    assert measurements is None
    assert "tests/conftest.py" in result.message


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


# --- anchor invariants: commit vs measure_commit --------------------------
#
# Both of the following exercise a property invisible to every test above: as
# long as base.commit == base.measure_commit (true everywhere until a KEEP
# happens), the scope gate and the worktree-integrity check cannot be told
# apart by which of the two fields they read. These force a real divergence —
# a real KEEP, through real git — so the anchor actually matters.


def test_scope_gate_still_catches_an_out_of_scope_edit_after_a_keep_advances_the_baseline(
    run, monkeypatch
):
    """The scope gate must diff against base.commit (frozen), not
    base.measure_commit (which moves on every KEEP). Anchoring to the moving
    pointer would give an out-of-scope edit exactly one eval to be caught: the
    next KEEP folds it into "already accepted" state and the gate never looks
    at it again, silently disabling scope enforcement for the rest of the run.
    """
    repo, sd, base = run
    (repo / "elsewhere.py").write_text("x = 1\n")
    commit_all(repo, "out of scope")

    # 1. The violation is caught immediately, as it always was.
    result, _ = pipeline.evaluate(options(run, monkeypatch))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.SCOPE
    assert "elsewhere.py" in result.message

    # 2. Force a KEEP through with scope broadened to "everything", so a real
    #    optimization can be measured and banked even though the out-of-scope
    #    file is still sitting in the tree. This is what advances
    #    measure_commit PAST the commit containing the violation, and
    #    re-points the (real) pinned worktree there.
    (repo / "pkg" / "mod.py").write_text("def work():\n    return 4950\n")
    commit_all(repo, "faster, with the violation still present")
    permissive_cfg = config.Config(
        **{
            **config.load(repo / ".autor3search" / "config.toml").__dict__,
            "scope": ("...",),
            "gates": config.Gates(False, False),
        }
    )
    monkeypatch.setattr(
        "autor3search_python.runner.Runner.pytest_gate",
        lambda self: __import__("autor3search_python.runner", fromlist=["Result"]).Result(
            (), "", "", 0, False, 0.0
        ),
    )
    keep_result, _ = pipeline.evaluate(
        pipeline.Options(
            root=repo,
            state_dir=sd,
            cfg=permissive_cfg,
            base=base,
            measure_fn=fake_measure(2.0, 1.0, n=8),
        )
    )
    assert keep_result.status is verdict.Status.KEEP  # sanity: the KEEP really happened

    # 3. Evaluate again with the ORIGINAL restrictive scope and nothing new
    #    committed. The violation never left the tree; a correct
    #    implementation re-validates the FULL diff from base.commit on every
    #    eval and must catch it again. An implementation anchored to
    #    measure_commit would see an empty diff here (measure_commit now IS
    #    HEAD) and let it through.
    result, _ = pipeline.evaluate(options(run, monkeypatch))
    assert result.status is verdict.Status.FAIL
    assert result.reason is verdict.Reason.SCOPE
    assert "elsewhere.py" in result.message


def test_no_spurious_baseline_tampered_on_the_eval_after_a_keep(run, monkeypatch):
    """The worktree-integrity check must compare against base.measure_commit
    (the advancing pointer), not base.commit (the frozen anchor). The pinned
    worktree is legitimately re-pointed after every KEEP, so anchoring to the
    frozen commit would report baseline_tampered on the very next eval after
    any successful optimization — breaking the tool permanently the moment it
    first succeeds.
    """
    repo, sd, base = run
    (repo / "pkg" / "mod.py").write_text("def work():\n    return 4950\n")
    commit_all(repo, "faster")

    keep_result, _ = pipeline.evaluate(
        options(run, monkeypatch, measure_fn=fake_measure(2.0, 1.0, n=8))
    )
    assert keep_result.status is verdict.Status.KEEP  # sanity: worktree re-pointed for real

    result, _ = pipeline.evaluate(options(run, monkeypatch, measure_fn=fake_measure(1.0, 1.0, n=8)))
    assert result.reason is not verdict.Reason.BASELINE_TAMPERED
    assert result.status is verdict.Status.DISCARD


def test_is_dependency_file():
    assert pipeline.is_dependency_file("pyproject.toml")
    assert pipeline.is_dependency_file("requirements-dev.txt")
    assert pipeline.is_dependency_file("constraints.txt")
    assert not pipeline.is_dependency_file("pkg/pyproject.toml")
    assert not pipeline.is_dependency_file("pkg/mod.py")


# --- run.log rotation ------------------------------------------------------


def test_rotate_run_log_leaves_a_small_log_alone(tmp_path):
    log_path = tmp_path / pipeline.RUN_LOG_NAME
    log_path.write_text("small transcript\n")
    pipeline.rotate_run_log(tmp_path)
    assert log_path.read_text() == "small transcript\n"
    assert not (tmp_path / pipeline.RUN_LOG_BACKUP_NAME).exists()


def test_rotate_run_log_does_nothing_when_no_log_exists_yet(tmp_path):
    pipeline.rotate_run_log(tmp_path)  # must not raise
    assert not (tmp_path / pipeline.RUN_LOG_NAME).exists()


def test_rotate_run_log_rolls_over_past_the_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "RUN_LOG_MAX_BYTES", 100)
    log_path = tmp_path / pipeline.RUN_LOG_NAME
    log_path.write_text("x" * 200)
    pipeline.rotate_run_log(tmp_path)
    assert not log_path.exists()  # a fresh append below starts a clean transcript
    assert (tmp_path / pipeline.RUN_LOG_BACKUP_NAME).read_text() == "x" * 200


def test_rotate_run_log_keeps_only_one_backup(tmp_path, monkeypatch):
    """Unbounded backups would just move the disk-filling problem sideways."""
    monkeypatch.setattr(pipeline, "RUN_LOG_MAX_BYTES", 100)
    (tmp_path / pipeline.RUN_LOG_BACKUP_NAME).write_text("stale backup")
    log_path = tmp_path / pipeline.RUN_LOG_NAME
    log_path.write_text("y" * 200)
    pipeline.rotate_run_log(tmp_path)
    assert (tmp_path / pipeline.RUN_LOG_BACKUP_NAME).read_text() == "y" * 200


def test_rotate_run_log_bounds_growth_across_many_experiments(tmp_path):
    """The bug this closes: with no cap, every experiment's (already
    per-stream-capped) transcript just keeps appending, so a long overnight
    loop writes tens of GB into the user's working tree. Simulates many
    experiments each appending a chunk close to the rotation threshold."""
    log_path = tmp_path / pipeline.RUN_LOG_NAME
    chunk = "t" * (pipeline.RUN_LOG_MAX_BYTES // 4)
    for _ in range(20):
        pipeline.rotate_run_log(tmp_path)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(chunk)
    total_on_disk = sum(
        p.stat().st_size for p in (log_path, tmp_path / pipeline.RUN_LOG_BACKUP_NAME) if p.exists()
    )
    # However many experiments ran, at most "current + one backup" survive.
    assert total_on_disk <= 2 * pipeline.RUN_LOG_MAX_BYTES + len(chunk)


def test_rotate_run_log_refuses_a_symlinked_log(tmp_path, monkeypatch):
    """run.log is gitignored and its name is waved through the scope gate —
    the same symlink-swap concern containment.py exists for elsewhere. A
    no-op here is safe: the containment check right before the real open in
    cli/eval.py still catches and reports it."""
    monkeypatch.setattr(pipeline, "RUN_LOG_MAX_BYTES", 1)
    outside = tmp_path / "outside.log"
    outside.write_bytes(b"do not touch")
    log_path = tmp_path / pipeline.RUN_LOG_NAME
    log_path.symlink_to(outside)
    pipeline.rotate_run_log(tmp_path)  # must not raise, and must not touch `outside`
    assert outside.read_bytes() == b"do not touch"
