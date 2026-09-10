import pytest

from autor3search import gitx, state
from autor3search.cli import main as cli_main
from autor3search.cli import runctx
from tests.conftest import git


def test_resolve_tag_prefers_an_explicit_tag(git_repo):
    assert runctx.resolve_tag(git_repo, "explicit") == "explicit"


def test_resolve_tag_derives_from_the_run_branch(git_repo):
    branch = state.branch_for("t1")
    git(git_repo, "checkout", "-b", branch)
    assert runctx.resolve_tag(git_repo, None) == "t1"


def test_resolve_tag_raises_off_a_run_branch_with_no_explicit_tag(git_repo):
    with pytest.raises(runctx.ContextError, match="not on a run branch"):
        runctx.resolve_tag(git_repo, None)


def test_resolve_tag_raises_context_error_not_git_error_outside_a_repo(tmp_path):
    """A non-repo root must surface the same ContextError every other failure
    mode does, not leak gitx.GitError past the module's own contract."""
    with pytest.raises(runctx.ContextError):
        runctx.resolve_tag(tmp_path, None)


def test_resolve_raises_outside_a_git_repository(tmp_path):
    with pytest.raises(runctx.ContextError, match="git repository"):
        runctx.resolve(str(tmp_path), "t1")


def test_resolve_raises_without_a_config(git_repo):
    with pytest.raises(runctx.ContextError):
        runctx.resolve(str(git_repo), "t1")


def test_resolve_raises_without_a_baseline(git_repo):
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_speed.py").write_text(
        "def test_fast(benchmark):\n    benchmark(lambda: 1)\n"
    )
    cli_main.main(["init", "-C", str(git_repo)])
    with pytest.raises(runctx.ContextError):
        runctx.resolve(str(git_repo), "t1")


def test_resolve_succeeds_after_baseline(git_repo):
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_speed.py").write_text(
        "def test_fast(benchmark):\n    benchmark(lambda: 1)\n"
    )
    cli_main.main(["init", "-C", str(git_repo)])
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "init")
    cli_main.main(["baseline", "-C", str(git_repo), "-tag", "t1"])

    ctx = runctx.resolve(str(git_repo), "t1")
    assert ctx.tag == "t1"
    assert ctx.root == git_repo.resolve()
    assert ctx.base.tag == "t1"
    assert ctx.cfg.benchmarks == ("tests/test_speed.py::test_fast",)


def test_resolve_derives_tag_from_branch_after_baseline(git_repo):
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_speed.py").write_text(
        "def test_fast(benchmark):\n    benchmark(lambda: 1)\n"
    )
    cli_main.main(["init", "-C", str(git_repo)])
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "init")
    cli_main.main(["baseline", "-C", str(git_repo), "-tag", "t1"])
    assert gitx.current_branch(git_repo) == "autor3search-python/t1"

    ctx = runctx.resolve(str(git_repo), None)
    assert ctx.tag == "t1"
