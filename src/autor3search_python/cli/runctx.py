"""Resolves the run a command is operating on: root, tag, state, config, baseline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autor3search_python import config, gitx, state


class ContextError(Exception):
    """A run that cannot be located from where the command was invoked."""


def resolve_tag(root: Path, tag: str | None) -> str:
    """An explicit -tag wins; otherwise derive it from the checked-out branch."""
    if tag:
        return tag
    branch = gitx.current_branch(root)
    derived = state.tag_from_branch(branch)
    if derived is None:
        raise ContextError(
            f"not on a run branch (currently {branch!r}) and no -tag given — pass "
            f"-tag <tag>, or check out the run branch"
        )
    return derived


@dataclass
class RunContext:
    root: Path
    tag: str
    state_dir: Path
    cfg: config.Config
    base: state.Baseline


def resolve(directory: str, tag: str | None) -> RunContext:
    """Everything a run-aware command needs, or a ContextError explaining what is missing."""
    try:
        root = Path(gitx.root(directory))
    except gitx.GitError as e:
        raise ContextError(f"{directory} is not inside a git repository: {e}") from e
    resolved = resolve_tag(root, tag)
    try:
        sd = state.state_dir(root, resolved)
        cfg = config.load(root / config.CONFIG_PATH)
        base = state.load_baseline(sd / state.BASELINE_FILE)
    except (state.StateError, config.ConfigError) as e:
        raise ContextError(str(e)) from e
    return RunContext(root=root, tag=resolved, state_dir=sd, cfg=cfg, base=base)
