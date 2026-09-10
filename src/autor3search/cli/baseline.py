"""baseline — freeze the tests and pin the commit this run measures against."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import shutil
import sys
from pathlib import Path

from autor3search import config, discover, freeze, gitx, pipeline, state
from autor3search.cli.main import EXIT_OK, EXIT_USAGE


class BaselineError(Exception):
    """A baseline that cannot be created for a reason that is not a git failure."""


def _fail(message: str) -> int:
    print(f"autor3search-python baseline: {message}", file=sys.stderr)
    return EXIT_USAGE


def run(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="autor3search-python baseline")
    parser.add_argument("-C", dest="directory", default=".", help="repository root")
    parser.add_argument("-tag", "--tag", dest="tag", required=True, help="run identifier")
    parser.add_argument(
        "-force",
        "--force",
        dest="force",
        action="store_true",
        help="replace an existing run under this tag, discarding its state",
    )
    opts = parser.parse_args(args)

    try:
        root = Path(gitx.root(opts.directory))
    except gitx.GitError as e:
        return _fail(f"{opts.directory} is not inside a git repository: {e}")

    try:
        state.valid_tag(opts.tag)
        sd = state.state_dir(root, opts.tag)
    except state.StateError as e:
        return _fail(str(e))

    config_path = root / config.CONFIG_PATH
    try:
        cfg = config.load(config_path)
    except config.ConfigError as e:
        return _fail(str(e))

    if not gitx.is_clean(root):
        return _fail(
            "the working tree is not clean. A baseline pinned against what is on disk, "
            "rather than what is in git, would not be reproducible. Commit or stash first."
        )

    branch = state.branch_for(opts.tag)
    if (sd / state.BASELINE_FILE).exists() or gitx.branch_exists(root, branch):
        if not opts.force:
            return _fail(
                f"a run already exists under tag {opts.tag!r} (branch {branch}). Pick a new "
                f"tag, or pass -force to replace it and discard its state."
            )
        _tear_down(root, sd, branch)

    original_branch = gitx.current_branch(root)
    created_branch = False
    try:
        gitx.create_branch(root, branch)
        created_branch = True
        commit = gitx.head_commit(root)

        frozen = discover.frozen_files(root, cfg.unfreeze)
        manifest = freeze.snapshot(root, sd / freeze.STORE_DIR, frozen)
        manifest.save(sd / freeze.MANIFEST_PATH)

        benchmarks = tuple(cfg.benchmarks) or tuple(discover.node_ids(discover.benchmarks(root)))
        if not benchmarks:
            raise BaselineError(
                "no benchmarks declared in config and none discovered — nothing to measure"
            )

        gitx.add_worktree(root, sd / state.WORKTREE_NAME, commit)

        base = state.Baseline(
            tag=opts.tag,
            branch=branch,
            commit=commit,
            measure_commit=commit,
            created_at=dt.datetime.now(dt.UTC).isoformat(),
            benchmarks=benchmarks,
            config_sha256=pipeline.sha256_file(config_path),
        )
        base.save(sd / state.BASELINE_FILE)
    except (gitx.GitError, freeze.FreezeError, BaselineError, OSError) as e:
        # Undo a partial baseline so a retry under the same tag is not blocked.
        # `rmtree` below only removes the worktree's directory; git's own
        # registration under .git/worktrees/ survives that and would permanently
        # block re-adding a worktree at the same path, so unregister it first.
        with contextlib.suppress(gitx.GitError):
            gitx.remove_worktree(root, sd / state.WORKTREE_NAME)
        if created_branch:
            try:
                gitx.checkout(root, original_branch)
                gitx.delete_branch(root, branch)
            except gitx.GitError:
                pass
        shutil.rmtree(sd, ignore_errors=True)
        return _fail(str(e))

    print(f"autor3search-python baseline: run {opts.tag!r} is ready")
    print(f"  branch          {branch}  (checked out)")
    print(f"  baseline commit {commit}")
    print(f"  frozen files    {len(manifest.files)}")
    print(f"  benchmarks      {len(benchmarks)}")
    print(f"  worktree        {sd / state.WORKTREE_NAME}")
    print("\nnext: point your agent at program.md and start the loop.")
    return EXIT_OK


def _tear_down(root: Path, sd: Path, branch: str) -> None:
    """Remove a previous run's worktree, branch and state. -force only."""
    worktree = sd / state.WORKTREE_NAME
    if worktree.exists():
        try:
            gitx.remove_worktree(root, worktree)
        except gitx.GitError:
            shutil.rmtree(worktree, ignore_errors=True)
    if gitx.branch_exists(root, branch):
        try:
            if gitx.current_branch(root) == branch:
                gitx.checkout(root, "-")
            gitx.delete_branch(root, branch)
        except gitx.GitError:
            pass
    shutil.rmtree(sd, ignore_errors=True)
