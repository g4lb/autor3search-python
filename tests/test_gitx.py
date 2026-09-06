import pytest

from autor3search_python import gitx
from tests.conftest import git


def test_root_and_head(git_repo):
    assert gitx.root(git_repo) == str(git_repo.resolve())
    assert len(gitx.head_commit(git_repo)) == 7
    assert gitx.head_subject(git_repo) == "initial"
    assert gitx.current_branch(git_repo) == "main"


def test_root_outside_a_repository_raises(tmp_path):
    with pytest.raises(gitx.GitError):
        gitx.root(tmp_path)


def test_branch_lifecycle(git_repo):
    assert gitx.branch_exists(git_repo, "run/x") is False
    gitx.create_branch(git_repo, "run/x")
    assert gitx.branch_exists(git_repo, "run/x") is True
    assert gitx.current_branch(git_repo) == "run/x"
    gitx.checkout(git_repo, "main")
    gitx.delete_branch(git_repo, "run/x")
    assert gitx.branch_exists(git_repo, "run/x") is False


def test_is_clean(git_repo):
    assert gitx.is_clean(git_repo) is True
    (git_repo / "new.py").write_text("x = 1\n")
    assert gitx.is_clean(git_repo) is False


def test_changed_since_reports_tracked_and_untracked(git_repo):
    base = gitx.head_commit(git_repo)
    (git_repo / "README.md").write_text("changed\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "edit")
    (git_repo / "untracked.py").write_text("x = 1\n")
    assert gitx.changed_since(git_repo, base) == ["README.md", "untracked.py"]


def test_changed_since_handles_a_non_ascii_path(git_repo):
    """-z output, so git does not octal-escape the name into something scope cannot match."""
    base = gitx.head_commit(git_repo)
    (git_repo / "café.py").write_text("x = 1\n")
    assert "café.py" in gitx.changed_since(git_repo, base)


def test_changed_since_respects_gitignore(git_repo):
    base = gitx.head_commit(git_repo)
    (git_repo / ".gitignore").write_text("ignored.py\n")
    (git_repo / "ignored.py").write_text("x = 1\n")
    assert "ignored.py" not in gitx.changed_since(git_repo, base)


def test_worktree_lifecycle(git_repo, tmp_path):
    commit = gitx.head_commit(git_repo)
    wt = tmp_path / "wt"
    gitx.add_worktree(git_repo, wt, commit)
    assert (wt / "README.md").exists()
    assert gitx.head_commit(wt) == commit

    (git_repo / "second.py").write_text("x = 1\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "second")
    second = gitx.head_commit(git_repo)

    gitx.checkout_detached(wt, second)
    assert gitx.head_commit(wt) == second
    assert (wt / "second.py").exists()

    gitx.remove_worktree(git_repo, wt)
    assert not wt.exists()


def test_git_error_includes_stderr(git_repo):
    with pytest.raises(gitx.GitError, match="unknown-ref"):
        gitx.checkout(git_repo, "unknown-ref")
