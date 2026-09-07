import pytest

from autor3search_python import containment


def test_is_symlink_true_for_a_symlink(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("x")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    assert containment.is_symlink(link) is True


def test_is_symlink_false_for_an_ordinary_file(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    assert containment.is_symlink(f) is False


def test_is_symlink_false_for_a_missing_path(tmp_path):
    assert containment.is_symlink(tmp_path / "absent.txt") is False


def test_escapes_root_true_through_a_symlinked_ancestor(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside)
    assert containment.escapes_root(root, root / "linked" / "f.txt") is True


def test_escapes_root_false_for_an_ordinary_nested_path(tmp_path):
    root = tmp_path / "repo"
    (root / "a" / "b").mkdir(parents=True)
    assert containment.escapes_root(root, root / "a" / "b" / "f.txt") is False


def test_ensure_contained_raises_for_a_symlinked_final_component(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("victim")
    link = root / "log.txt"
    link.symlink_to(outside)

    with pytest.raises(containment.ContainmentError, match="symlink"):
        containment.ensure_contained(root, link, "log.txt", "open")


def test_ensure_contained_raises_for_a_symlinked_ancestor(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "sub").symlink_to(outside)

    with pytest.raises(containment.ContainmentError, match="symlinked ancestor"):
        containment.ensure_contained(root, root / "sub" / "f.txt", "sub/f.txt", "open")


def test_ensure_contained_passes_for_an_ordinary_path(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    containment.ensure_contained(root, root / "f.txt", "f.txt", "open")


def test_ensure_contained_works_before_the_file_exists(tmp_path):
    """A log or results file's very first write: the path itself is not there
    yet, only its (real) parent directory is."""
    root = tmp_path / "repo"
    root.mkdir()
    containment.ensure_contained(root, root / "run.log", "run.log", "open")
