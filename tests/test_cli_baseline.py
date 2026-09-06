import pytest

from autor3search_python import freeze, gitx, state
from autor3search_python.cli import main as cli_main
from tests.conftest import git


@pytest.fixture
def ready(git_repo):
    (git_repo / "pkg").mkdir()
    (git_repo / "pkg" / "__init__.py").write_text("")
    (git_repo / "pkg" / "mod.py").write_text("def work():\n    return 1\n")
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_mod.py").write_text(
        "from pkg.mod import work\n\n\ndef test_w(benchmark):\n    benchmark(work)\n"
    )
    assert cli_main.main(["init", "-C", str(git_repo)]) == 0
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "init")
    return git_repo


def test_baseline_creates_branch_freeze_worktree_and_record(ready):
    assert cli_main.main(["baseline", "-C", str(ready), "-tag", "t1"]) == 0
    assert gitx.current_branch(ready) == "autor3search-python/t1"

    sd = state.state_dir(ready, "t1")
    base = state.load_baseline(sd / state.BASELINE_FILE)
    assert base.tag == "t1"
    assert base.commit == base.measure_commit
    assert base.benchmarks == ("tests/test_mod.py::test_w",)
    assert len(base.config_sha256) == 64

    manifest = freeze.load_manifest(sd / freeze.MANIFEST_PATH)
    assert "tests/test_mod.py" in manifest.files

    worktree = sd / state.WORKTREE_NAME
    assert (worktree / "pkg" / "mod.py").exists()
    assert gitx.head_commit(worktree) == base.commit


def test_baseline_refuses_a_dirty_tree(ready, capsys):
    """A baseline pinned against what is on disk, not what is in git, is not reproducible."""
    (ready / "pkg" / "mod.py").write_text("def work():\n    return 2\n")
    assert cli_main.main(["baseline", "-C", str(ready), "-tag", "t1"]) == 2
    assert "clean" in capsys.readouterr().err.lower()


def test_baseline_refuses_a_reused_tag(ready, capsys):
    assert cli_main.main(["baseline", "-C", str(ready), "-tag", "t1"]) == 0
    assert cli_main.main(["baseline", "-C", str(ready), "-tag", "t1"]) == 2
    assert "already" in capsys.readouterr().err.lower()


def test_baseline_refuses_a_traversal_tag(ready, capsys):
    assert cli_main.main(["baseline", "-C", str(ready), "-tag", "../../evil"]) == 2
    assert "tag" in capsys.readouterr().err.lower()


def test_baseline_refuses_without_a_config(git_repo, capsys):
    assert cli_main.main(["baseline", "-C", str(git_repo), "-tag", "t1"]) == 2
    assert "init" in capsys.readouterr().err


def test_a_failed_baseline_leaves_no_branch_behind(ready, monkeypatch, capsys):
    """A retry under the same name must not be permanently blocked."""
    monkeypatch.setattr(
        "autor3search_python.gitx.add_worktree",
        lambda *a, **k: (_ for _ in ()).throw(gitx.GitError("worktree failed")),
    )
    assert cli_main.main(["baseline", "-C", str(ready), "-tag", "t1"]) != 0
    assert gitx.branch_exists(ready, "autor3search-python/t1") is False
    assert gitx.current_branch(ready) == "main"


def test_a_failed_baseline_unregisters_the_worktree_and_allows_retry(ready, monkeypatch):
    """rmtree alone only removes the worktree's directory; git's own
    registration under .git/worktrees/ survives that and permanently blocks
    re-adding a worktree at the same path unless it is explicitly removed."""

    def _raise(self, path):
        raise OSError("disk full")

    monkeypatch.setattr("autor3search_python.state.Baseline.save", _raise)
    assert cli_main.main(["baseline", "-C", str(ready), "-tag", "t1"]) != 0

    listing = git(ready, "worktree", "list")
    sd = state.state_dir(ready, "t1")
    assert str(sd / state.WORKTREE_NAME) not in listing

    # The user-visible consequence: a retry under the same tag now succeeds.
    monkeypatch.undo()
    assert cli_main.main(["baseline", "-C", str(ready), "-tag", "t1"]) == 0


def test_baseline_freezes_conftest_too(ready):
    (ready / "tests" / "conftest.py").write_text("# fixtures\n")
    git(ready, "add", "-A")
    git(ready, "commit", "-q", "-m", "add conftest")
    cli_main.main(["baseline", "-C", str(ready), "-tag", "t1"])
    sd = state.state_dir(ready, "t1")
    assert "tests/conftest.py" in freeze.load_manifest(sd / freeze.MANIFEST_PATH).files
