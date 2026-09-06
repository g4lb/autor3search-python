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
from pathlib import Path
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


def is_dependency_file(rel: str) -> bool:
    """Only at the repository root: a vendored pyproject.toml deep in a package
    is ordinary source, not this repository's dependency declaration."""
    if "/" in rel:
        return False
    return rel in DEPENDENCY_FILES or any(fnmatch.fnmatch(rel, g) for g in DEPENDENCY_GLOBS)


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
