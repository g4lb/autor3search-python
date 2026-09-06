import json

import pytest

from autor3search_python import freeze


@pytest.fixture
def trees(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    (repo / "conftest.py").write_text("# fixtures\n")
    return repo, tmp_path / "state" / "frozen"


def test_snapshot_records_hashes_and_copies_content(trees):
    repo, store = trees
    m = freeze.snapshot(repo, store, ["tests/test_a.py", "conftest.py"])
    assert set(m.files) == {"tests/test_a.py", "conftest.py"}
    assert (store / "tests" / "test_a.py").read_text() == (repo / "tests" / "test_a.py").read_text()
    assert all(len(h) == 64 for h in m.files.values())


def test_restore_erases_an_edit(trees):
    repo, store = trees
    original = (repo / "tests" / "test_a.py").read_text()
    m = freeze.snapshot(repo, store, ["tests/test_a.py"])
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    pass  # weakened\n")
    changed = freeze.restore(repo, store, m)
    assert changed == ["tests/test_a.py"]
    assert (repo / "tests" / "test_a.py").read_text() == original


def test_restore_recreates_a_deleted_file(trees):
    repo, store = trees
    m = freeze.snapshot(repo, store, ["tests/test_a.py"])
    (repo / "tests" / "test_a.py").unlink()
    assert freeze.restore(repo, store, m) == ["tests/test_a.py"]
    assert (repo / "tests" / "test_a.py").exists()


def test_restore_reports_nothing_when_untouched(trees):
    repo, store = trees
    m = freeze.snapshot(repo, store, ["tests/test_a.py"])
    assert freeze.restore(repo, store, m) == []


def test_snapshot_refuses_a_symlinked_source(trees, tmp_path):
    repo, store = trees
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n")
    link = repo / "tests" / "test_link.py"
    link.symlink_to(outside)
    with pytest.raises(freeze.SymlinkError):
        freeze.snapshot(repo, store, ["tests/test_link.py"])


def test_restore_refuses_to_write_through_a_symlink(trees, tmp_path):
    """Writing through the link would reach a file outside the repository entirely."""
    repo, store = trees
    m = freeze.snapshot(repo, store, ["tests/test_a.py"])
    outside = tmp_path / "outside.py"
    outside.write_text("untouched\n")
    (repo / "tests" / "test_a.py").unlink()
    (repo / "tests" / "test_a.py").symlink_to(outside)
    with pytest.raises(freeze.SymlinkError):
        freeze.restore(repo, store, m)
    assert outside.read_text() == "untouched\n"


def test_verify_reports_edited_deleted_and_symlinked(trees, tmp_path):
    repo, store = trees
    (repo / "tests" / "test_b.py").write_text("b\n")
    (repo / "tests" / "test_c.py").write_text("c\n")
    m = freeze.snapshot(repo, store, ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"])
    assert freeze.verify(repo, m) == []
    (repo / "tests" / "test_a.py").write_text("edited\n")
    (repo / "tests" / "test_b.py").unlink()
    (repo / "tests" / "test_c.py").unlink()
    (repo / "tests" / "test_c.py").symlink_to(tmp_path / "elsewhere.py")
    assert freeze.verify(repo, m) == ["tests/test_a.py", "tests/test_b.py", "tests/test_c.py"]


@pytest.mark.parametrize("bad", ["../escape.py", "/etc/passwd", "a/../../escape.py"])
def test_manifest_paths_that_escape_the_root_are_refused(trees, bad):
    """Manifest entries come off disk, so they are untrusted input that restore writes through."""
    repo, store = trees
    m = freeze.Manifest(files={bad: "0" * 64})
    with pytest.raises(freeze.FreezeError, match="escape|relative"):
        freeze.restore(repo, store, m)


def test_manifest_round_trip(tmp_path):
    path = tmp_path / "state" / "frozen" / "manifest.json"
    freeze.Manifest(files={"tests/test_a.py": "ab" * 32}).save(path)
    assert json.loads(path.read_text())["files"]["tests/test_a.py"] == "ab" * 32
    assert freeze.load_manifest(path).files == {"tests/test_a.py": "ab" * 32}


def test_load_manifest_tolerates_a_missing_files_key(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{}")
    assert freeze.load_manifest(path).files == {}


def test_load_manifest_rejects_malformed_json(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{not json")
    with pytest.raises(freeze.FreezeError):
        freeze.load_manifest(path)
