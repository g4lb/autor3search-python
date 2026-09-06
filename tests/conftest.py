"""Shared fixtures. Every test that touches run state must use `state_home`."""

import subprocess

import pytest


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    """Point run state at a tmpdir so the developer's real cache is never written."""
    home = tmp_path / "state-home"
    home.mkdir()
    monkeypatch.setenv("AUTOR3SEARCH_PYTHON_STATE_HOME", str(home))
    return home


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    """An initialized repository with one commit, and a deterministic identity."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("demo\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    return repo
