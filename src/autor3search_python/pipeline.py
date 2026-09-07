"""Runs one full evaluation: gate correctness, measure, score.

It lives here rather than in the cli layer so it is testable without a process
boundary — the eval command itself handles only flags, output formatting and
the exit code.

The contract that matters: a gate failure is a terminal `verdict.Result` (a row
in results.tsv, a documented exit code). A raised exception means the HARNESS
malfunctioned — I/O, git, a malformed baseline — not that the candidate was
rejected. Every caller treats those two differently, and it is what lets an
unattended loop tell "your change was refused" from "the tool is broken".
"""

from __future__ import annotations

import fnmatch
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO

from autor3search_python import (
    benchio,
    config,
    discover,
    freeze,
    gitx,
    measure,
    results,
    runner,
    scope,
    state,
    stats,
    verdict,
)

RUN_LOG_NAME = "run.log"

# Rejected regardless of scope. Changing a dependency is a supply-chain decision
# a human makes, not something an unattended overnight loop decides — and a
# swapped dependency changes WHAT is being measured, not just how fast it runs.
# The default scope matches root files, so this cannot be left to the patterns.
DEPENDENCY_FILES = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "poetry.lock",
        "uv.lock",
        "pdm.lock",
        "Pipfile",
        "Pipfile.lock",
    }
)
DEPENDENCY_GLOBS = ("requirements*.txt", "constraints*.txt")

# Rejected regardless of scope, for a different reason than a dependency file.
# pytest reads its own configuration from these, so one addopts line changes
# what is collected, how it runs and how it is timed — it changes WHAT is being
# measured rather than how fast the code is. Two demonstrated cheats:
# `--benchmark-timer=` pointing at a function that divides perf_counter by ten
# (a comment-only edit then "improves" by 90%), and `-k` narrowing collection
# so a deliberately broken implementation walks past the correctness gate that
# is deliberately not switchable.
#
# pyproject.toml and setup.cfg are in discover.PYTEST_CONFIG_FILES too, but
# they are caught as dependency files first and keep that more specific
# message. The remainder is derived, never re-listed: the whole point of the
# shared constant is that adding a name pytest reads closes this gate too, with
# no second edit to remember.
#
# This subtraction selects a MESSAGE and nothing else. It must never be the
# input to a security check: FORBIDDEN_ROOT_FILES below is a union for exactly
# that reason. It was the difference once, and pyproject.toml and setup.cfg
# were then checked only through the git diff — which --exclude-standard makes
# blind to a gitignored file — so an untracked pyproject.toml carrying
# `[tool.pytest.ini_options] addopts = "--benchmark-timer=..."` produced a
# fraudulent KEEP at -90%.
MEASUREMENT_CONFIG_FILES = frozenset(discover.PYTEST_CONFIG_FILES) - DEPENDENCY_FILES

# Rejected regardless of scope, and the reason is git itself. Every other gate
# here reads `gitx.changed_since`, which passes --exclude-standard; .gitignore
# decides what that command can see. An agent editing it is editing the
# visibility of every git-based check at once, so the file is refused rather
# than trusted — a new untracked module hidden behind one committed line is
# invisible to the scope gate and perfectly importable.
GITIGNORE_FILES = frozenset({".gitignore"})

# Rejected regardless of scope, and the reason is startup, not pytest.
# runner.bench_env puts the tree root on PYTHONPATH — it has to, or both sides
# would import the same installed copy — and CPython's site.py imports
# `sitecustomize` and `usercustomize` at interpreter startup from anything on
# sys.path. So a file here runs arbitrary code before pytest, before
# compileall, before the import gate and before both bench sides, and only on
# the candidate side, because the pinned worktree sits at the older commit.
#
# The fix is to reject the FILE, not the behaviour: -S or -P would also cut
# site-packages resolution out from under pytest itself.
#
# Matched by STEM across every suffix the import system will load, not by
# filename. A sourceless `sitecustomize.pyc` with no .py beside it imports
# perfectly well — SourcelessFileLoader exists for exactly that — so listing
# only the .py names closed this by name while leaving the class open. (.pyo is
# inert on Python 3, whose BYTECODE_SUFFIXES is ['.pyc'], but it costs nothing
# to refuse.)
STARTUP_HOOK_STEMS = frozenset({"sitecustomize", "usercustomize"})
_IMPORTABLE_SUFFIXES = (".py", ".pyc", ".pyo")
STARTUP_HOOK_FILES = frozenset(
    f"{stem}{suffix}" for stem in STARTUP_HOOK_STEMS for suffix in _IMPORTABLE_SUFFIXES
)

# Rejected on sight if they are present in the working tree at all, whatever
# git has been told about them. Every other gate here reads a git diff, and
# `gitx.changed_since` passes --exclude-standard, so a `.gitignore` the agent
# commits makes any untracked file invisible to all of them — while the
# interpreter still reads it. .gitignore is itself an in-scope root file, so
# that is one commit away. Statting the worktree is the only source of truth
# that a gitignore entry cannot rewrite.
#
# A UNION of every forbidden name, never a difference. This is the check that
# exists precisely because the diff-based ones are blind, so subtracting
# anything from it re-opens the hole it was written to close: when it was
# `MEASUREMENT_CONFIG_FILES | STARTUP_HOOK_FILES`, the two names the message
# split removed — pyproject.toml and setup.cfg — were covered by the blind path
# alone, and an untracked, gitignored pyproject.toml with a
# `--benchmark-timer=` addopts line scored a KEEP at -90%. Whatever a name is
# forbidden FOR, it belongs here as well.
FORBIDDEN_ROOT_FILES = (
    frozenset(discover.PYTEST_CONFIG_FILES)
    | STARTUP_HOOK_FILES
    | DEPENDENCY_FILES
    | GITIGNORE_FILES
)


def is_dependency_file(rel: str) -> bool:
    """Only at the repository root: a vendored pyproject.toml deep in a package
    is ordinary source, not this repository's dependency declaration."""
    if "/" in rel:
        return False
    return rel in DEPENDENCY_FILES or any(fnmatch.fnmatch(rel, g) for g in DEPENDENCY_GLOBS)


def is_measurement_config_file(rel: str) -> bool:
    """Only at the repository root: pytest reads its config from the rootdir,
    so a `pytest.ini` inside a package is ordinary data, not a control file."""
    return "/" not in rel and rel in MEASUREMENT_CONFIG_FILES


def is_startup_hook_file(rel: str) -> bool:
    """Only at the repository root: `site` imports these by top-level module
    name, so only the copy on the sys.path entry bench_env adds can run.

    By stem, across every importable suffix — `sitecustomize.pyc` with no
    source beside it runs just as happily as `sitecustomize.py`.
    """
    p = PurePosixPath(rel)
    return "/" not in rel and p.stem in STARTUP_HOOK_STEMS and p.suffix in _IMPORTABLE_SUFFIXES


def is_gitignore_file(rel: str) -> bool:
    """Only at the repository root: that is the one git consults for the whole
    tree from `changed_since`'s point of view, and a nested one can only narrow
    what is already inside an in-scope directory."""
    return "/" not in rel and rel in GITIGNORE_FILES


def is_bytecode(rel: str) -> bool:
    """Compiled bytecode, which the harness's OWN gates leave lying around.

    compile_gate runs compileall and pytest writes bytecode too, so a narrow
    scope would otherwise start failing every experiment with `scope_violation`
    from the second eval onward — untracked `__pycache__/*.pyc` that the agent
    never wrote and cannot remove often enough to matter.

    This skip is NOT safe on its own, and the ordering in `evaluate` is part of
    the fix rather than incidental. Two of the three cases really do hide
    nothing: a `.pyc` beside its source is invalidated by CPython against the
    source's mtime and size, and one under `__pycache__` with no source is not
    importable, because sourceless imports must sit at the source's own
    location. The third case is the exception — a sourceless `.pyc` sitting
    directly on a sys.path entry IS importable, and `sitecustomize.pyc` is
    then executed at interpreter startup. So every rejection that can match a
    bytecode path must be checked BEFORE this skip, never after it.
    """
    p = PurePosixPath(rel)
    return "__pycache__" in p.parts or p.suffix in (".pyc", ".pyo")


def present_forbidden_root_files(root: str | Path, baseline_commit: str) -> list[str]:
    """Forbidden root files that exist on disk and did not exist at baseline.

    Filesystem-sourced on purpose. Everything else in the scope gate reads
    `gitx.changed_since`, which passes --exclude-standard, so one committed
    `.gitignore` line makes an untracked file invisible to every git-based
    check while the interpreter goes on reading it.

    A file that WAS at baseline is left to the diff-based checks: it is tracked,
    so `git diff` reports any edit to it regardless of gitignore, and a repo
    that legitimately shipped a `pytest.ini` before the run started must not
    fail every experiment for owning one.
    """
    root = Path(root)
    return sorted(
        name
        for name in FORBIDDEN_ROOT_FILES
        if (root / name).exists() and not gitx.path_in_tree(root, baseline_commit, name)
    )


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass
class Measurements:
    """Every comparison made for one experiment.

    Time is the ONLY scored metric and the only thing verdict.decide sees. If a
    second metric is ever added here, keep that boundary: a change that scores
    on anything but wall time would silently accept work that is not faster.
    """

    time: list[benchio.Delta] = field(default_factory=list)


@dataclass
class Options:
    """Everything one evaluation needs, all resolved by the caller."""

    root: Path
    state_dir: Path
    cfg: config.Config
    base: state.Baseline
    log: IO[str] | None = None
    # Injectable so the gate logic is testable without spending real minutes
    # benchmarking. Defaults to measure.run.
    measure_fn: Callable[[measure.Options], tuple[benchio.Set, benchio.Set]] | None = None


def evaluate(opts: Options) -> tuple[verdict.Result, Measurements | None]:
    """Gate, measure and score one experiment."""
    root, sd, cfg, base = Path(opts.root), Path(opts.state_dir), opts.cfg, opts.base
    log = opts.log

    # 1. Scope, checked before anything is restored or built, so an
    #    out-of-scope edit is reported as itself rather than as a build error.
    #
    #    Diffs against base.commit — the FROZEN anchor — and NOT
    #    base.measure_commit, which moves after every KEEP. Anchoring here
    #    means the gate re-validates the FULL accumulated diff on every eval.
    #    Anchoring to the advancing pointer would give an out-of-scope edit
    #    exactly one eval in which to be caught; past that it would be part of
    #    "already accepted" state and never looked at again.
    changed = gitx.changed_since(root, base.commit)
    matcher = scope.Matcher(cfg.scope)
    for rel in changed:
        # Every rejection that can match a bytecode path comes BEFORE the
        # bytecode skip. `sitecustomize.pyc` is both a startup hook and a .pyc,
        # and skipping it first waved it straight through — closing a hole by
        # name and reopening it by extension.
        if is_dependency_file(rel):
            return (
                verdict.gate(
                    verdict.Status.FAIL,
                    verdict.Reason.SCOPE,
                    f"{rel} may not be modified: dependency changes are a human decision, "
                    f"not an autonomous one",
                ),
                None,
            )
        if is_measurement_config_file(rel):
            return (
                verdict.gate(
                    verdict.Status.FAIL,
                    verdict.Reason.SCOPE,
                    f"{rel} may not be modified: it is a pytest configuration file, so it "
                    f"changes what pytest collects, how it runs and how it times — that "
                    f"changes what is being measured rather than how fast the code is",
                ),
                None,
            )
        if is_startup_hook_file(rel):
            return (
                verdict.gate(
                    verdict.Status.FAIL,
                    verdict.Reason.SCOPE,
                    f"{rel} may not be modified: Python imports it automatically at "
                    f"interpreter startup, so it runs arbitrary code inside every gate and "
                    f"every measured process before any of them begin",
                ),
                None,
            )
        if is_gitignore_file(rel):
            return (
                verdict.gate(
                    verdict.Status.FAIL,
                    verdict.Reason.SCOPE,
                    f"{rel} may not be modified: it decides what every git-based gate here "
                    f"can see. changed_since passes --exclude-standard, so one line added "
                    f"here hides a new untracked file from the scope gate while the "
                    f"interpreter goes on importing it",
                ),
                None,
            )
        if is_bytecode(rel):
            continue  # the harness's own leavings, not the agent's edit
        if rel in (results.PATH, RUN_LOG_NAME, config.CONFIG_PATH):
            continue  # harness output, plus the human-owned config (checked below)
        if discover.is_test_file(rel):
            continue  # handled by restore, not by the scope gate
        if not matcher.match(rel):
            return (
                verdict.gate(
                    verdict.Status.FAIL,
                    verdict.Reason.SCOPE,
                    f"{rel} is outside the allowed scope {list(cfg.scope)}",
                ),
                None,
            )

    # 1a. The same forbidden root files again, this time by statting the
    #     working tree instead of reading a git diff.
    #
    #     Not redundant: `gitx.changed_since` passes --exclude-standard, and
    #     `.gitignore` is an ordinary in-scope root file. One committed line
    #     naming `pytest.ini` makes an untracked `pytest.ini` invisible to
    #     every check above, while pytest goes on reading it — the diff is
    #     simply the wrong source of truth for "is this file here". The loop
    #     above still runs first, because for a tracked file it says whether
    #     the file was EDITED, which is the more precise complaint.
    for name in present_forbidden_root_files(root, base.commit):
        return (
            verdict.gate(
                verdict.Status.FAIL,
                verdict.Reason.SCOPE,
                f"{name} is present in the working tree and was not there at baseline. "
                f"It is refused on sight, whatever git has been told about it: a "
                f".gitignore entry hides a file from every git-based check but not from "
                f"the interpreter, which reads it either way. Delete it.",
            ),
            None,
        )

    # 1b. Config integrity. config.toml lives in the repo because humans own it,
    #     which means the agent can reach it. Raising max_regress_pct or
    #     deleting benchmarks would defeat the guard, so it is hashed at
    #     baseline and any change fails the run.
    if sha256_file(root / config.CONFIG_PATH) != base.config_sha256:
        return (
            verdict.gate(
                verdict.Status.FAIL,
                verdict.Reason.CONFIG_CHANGED,
                f"{config.CONFIG_PATH} changed since baseline — scoring rules are fixed for a "
                f"run; revert it, or start a new run with 'autor3search-python baseline'",
            ),
            None,
        )

    # 2. Restore frozen files. Agent edits are erased, not argued with.
    manifest = freeze.load_manifest(sd / freeze.MANIFEST_PATH)
    try:
        restored = freeze.restore(root, sd / freeze.STORE_DIR, manifest)
    except freeze.SymlinkError as e:
        # Tampering, not a harness malfunction: report it as a verdict so the
        # run gets a results.tsv row and an actionable message.
        return (
            verdict.gate(
                verdict.Status.FAIL,
                verdict.Reason.SYMLINK_SWAP,
                f"{e} — a frozen test file must remain a regular file; restore it and rerun",
            ),
            None,
        )
    if restored and log is not None:
        log.write(f"restored {len(restored)} frozen file(s): {restored}\n")

    # 2b. Reject files that did not exist at baseline. Restore only rewrites
    #     what it froze, and the scope gate skips every test file — so without
    #     this an agent could ADD an easier benchmark and neither gate would
    #     notice.
    present = discover.frozen_files(root, cfg.unfreeze)
    added = [rel for rel in present if rel not in manifest.files]
    if added:
        return (
            verdict.gate(
                verdict.Status.FAIL,
                verdict.Reason.NEW_TEST_FILE,
                f"files not present at baseline: {added} — the benchmark set is frozen; add "
                f"them before running 'autor3search-python baseline', or list them in "
                f"config unfreeze",
            ),
            None,
        )

    timeout = cfg.timeout_seconds()
    env = runner.bench_env(root, cfg)
    r = runner.Runner(root, timeout, log=log, env=env, python=cfg.python)

    # 3. Compile — the syntax gate.
    if cfg.gates.compile_:
        res = r.compile_gate(["."])
        if res.timed_out:
            return verdict.gate(
                verdict.Status.CRASH, verdict.Reason.TIMEOUT, "compileall timed out"
            ), None
        if not res.ok():
            return verdict.gate(verdict.Status.CRASH, verdict.Reason.COMPILE, res.tail(30)), None

    # 4. Import — catches what compileall cannot see, before the expensive gate.
    if cfg.gates.import_:
        modules = runner.importable_modules(root, matcher)
        res = r.import_gate(modules)
        if res.timed_out:
            return verdict.gate(
                verdict.Status.CRASH, verdict.Reason.TIMEOUT, "import gate timed out"
            ), None
        if not res.ok():
            return verdict.gate(verdict.Status.FAIL, verdict.Reason.IMPORT, res.tail(30)), None

    # 5. Tests. Correctness is never traded for speed, and this gate is not
    #    switchable.
    res = r.pytest_gate()
    if res.timed_out:
        return verdict.gate(verdict.Status.CRASH, verdict.Reason.TIMEOUT, "pytest timed out"), None
    if not res.ok():
        return verdict.gate(verdict.Status.FAIL, verdict.Reason.TESTS, res.tail(40)), None

    # 5b. Baseline worktree integrity. An agent could edit the pinned worktree
    #     in place to make the BASELINE slow, after which every candidate
    #     "improves" and every experiment returns KEEP without optimizing
    #     anything.
    #
    #     This is a DETECTION and only a partial one. The state directory is
    #     out of tree, but the agent runs as the same OS user, so nothing stops
    #     it editing the worktree; this only catches that if HEAD itself moves.
    #     An attacker who dirties tracked files without moving HEAD, or who
    #     also rewrites baseline.json to match, is not caught. Treat it as
    #     catching accidental clobbering and careless tampering, not as a
    #     guarantee.
    #
    #     Compares against measure_commit, not commit: the worktree is
    #     legitimately re-pointed after every KEEP.
    worktree = sd / state.WORKTREE_NAME
    head = gitx.head_commit(worktree)
    if head != base.measure_commit:
        return (
            verdict.gate(
                verdict.Status.FAIL,
                verdict.Reason.BASELINE_TAMPERED,
                f"pinned baseline worktree HEAD is {head} but the recorded measurement commit "
                f"is {base.measure_commit} — the worktree no longer matches the baseline and "
                f"this run's measurements cannot be trusted. Start a fresh run with "
                f"'autor3search-python baseline'.",
            ),
            None,
        )

    # 6. Measure, interleaved against the pinned baseline worktree.
    measure_fn = opts.measure_fn or measure.run
    try:
        base_set, cand_set = measure_fn(
            measure.Options(
                base_dir=worktree,
                cand_dir=root,
                node_ids=list(base.benchmarks),
                cfg=cfg,
                log=log,
            )
        )
    except measure.MeasureError as e:
        # Ruling R7: a measurement round crashing has no honest reason in this
        # vocabulary but MEASURE — reporting it as COMPILE would send an agent
        # to debug a build that in fact compiled fine.
        return verdict.gate(verdict.Status.CRASH, verdict.Reason.MEASURE, str(e)), None

    # 7. Score. Only the declared set counts, matched by base name so a config
    #    entry covers every parametrization of that benchmark.
    base_set = base_set.select_by_base(list(base.benchmarks))
    cand_set = cand_set.select_by_base(list(base.benchmarks))
    deltas = benchio.compare_all(base_set, cand_set)
    score = stats.geomean([d.ratio for d in deltas])
    result = verdict.decide(deltas, score, cfg.max_regress_pct, cfg.min_effect_pct)

    # 8. Advance the measurement baseline on KEEP. Without this, every
    #    experiment after the first kept one is measured against the run's
    #    ORIGINAL commit forever, so a later no-op that merely fails to regress
    #    an EARLIER improvement still banks as KEEP.
    if result.status is verdict.Status.KEEP:
        _advance_measurement_baseline(root, sd, base, worktree)

    return result, Measurements(time=deltas)


def _advance_measurement_baseline(
    root: Path, sd: Path, base: state.Baseline, worktree: Path
) -> None:
    """Re-point the pinned worktree at the candidate's commit and persist it.

    A failure here is raised, not folded into the verdict: continuing against a
    worktree that no longer agrees with the recorded measure_commit would
    silently corrupt every subsequent measurement — precisely the class of bug
    this advance exists to fix. If it fails after the worktree has moved but
    before the new commit is persisted, the NEXT eval's integrity check catches
    the mismatch and fails loudly rather than measuring against it silently.
    """
    new_commit = gitx.head_commit(root)
    gitx.checkout_detached(worktree, new_commit)
    base.measure_commit = new_commit
    base.save(sd / state.BASELINE_FILE)
