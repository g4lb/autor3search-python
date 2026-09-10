import subprocess
from pathlib import Path

import pytest

from autor3search import gitx
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


def test_path_in_tree_reads_the_commit_not_the_worktree(git_repo):
    """The gate that stats the working tree needs an answer git cannot be
    talked out of by .gitignore or by staging."""
    assert gitx.path_in_tree(git_repo, "HEAD", "README.md") is True
    assert gitx.path_in_tree(git_repo, "HEAD", "pytest.ini") is False
    # Neither the working tree, nor .gitignore, nor the INDEX may change the
    # answer: an agent that stages a file has not put it in any commit, and a
    # check reading the index instead of the tree would say it had.
    (git_repo / "pytest.ini").write_text("[pytest]\n")
    (git_repo / ".gitignore").write_text("pytest.ini\n")
    assert gitx.path_in_tree(git_repo, "HEAD", "pytest.ini") is False
    git(git_repo, "add", "-f", "pytest.ini")
    assert gitx.path_in_tree(git_repo, "HEAD", "pytest.ini") is False


def test_git_output_is_decoded_as_utf8_not_the_platform_locale(monkeypatch, git_repo):
    """text=True alone decodes with the locale's encoding, which is cp1252 on
    a default Windows install: a path like `café.py` came back as `caf?.py`,
    and a gate that compares those names against the ones on disk would then
    be reasoning about a file that does not exist. git speaks UTF-8; say so.
    """
    seen = {}
    real_run = subprocess.run

    def capture(*args, **kwargs):
        seen.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(gitx.subprocess, "run", capture)
    gitx.head_commit(git_repo)
    assert seen.get("encoding") == "utf-8"


def test_root_is_returned_in_the_platforms_own_path_form(monkeypatch, git_repo):
    """git prints POSIX separators everywhere, Windows included, so its
    `rev-parse --show-toplevel` answer is `C:/Users/...` there. Every caller
    wraps this in Path() and does not care, but `doctor` prints it verbatim,
    and a run's state directory is keyed on the repository path — one form,
    not two."""
    monkeypatch.setattr(gitx, "_git", lambda d, *a: "C:/Users/x/repo")
    assert gitx.root(git_repo) == str(Path("C:/Users/x/repo"))
