"""Shared fixtures. Every test that touches run state must use `state_home`."""

import subprocess

import pytest


@pytest.fixture(autouse=True)
def state_home(tmp_path_factory, monkeypatch):
    """Point run state at a tmpdir so the developer's real cache is never written.

    Built from `tmp_path_factory` rather than `tmp_path`: pytest names `tmp_path`
    after the test's own node id (truncated to 30 characters), so a state
    directory nested under it can echo arbitrary substrings of the test's name
    back through the CLI — e.g. a test asserting a word never reaches stdout
    would trip over that same word sitting in its own tmp dir name. A
    `tmp_path_factory` directory is still fresh and isolated per test, just not
    named after it.
    """
    home = tmp_path_factory.mktemp("state-home")
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
