import pytest

from autor3search.scope import Matcher


@pytest.mark.parametrize("pattern", ["./...", "...", "**"])
def test_root_recursive_matches_everything(pattern):
    m = Matcher([pattern])
    assert m.match("main.py")
    assert m.match("src/pkg/deep/mod.py")


@pytest.mark.parametrize("pattern", ["./src/...", "src/...", "src/**", "src/"])
def test_directory_spellings_are_equivalent(pattern):
    """Go-style and Python-familiar spellings must normalize to the same rule."""
    m = Matcher([pattern])
    assert m.match("src/pkg/mod.py")
    assert m.match("src/mod.py")
    assert not m.match("tests/test_mod.py")
    assert not m.match("srcextra/mod.py")


@pytest.mark.parametrize("pattern", ["./src", "src"])
def test_non_recursive_matches_only_direct_children(pattern):
    m = Matcher([pattern])
    assert m.match("src/mod.py")
    assert not m.match("src/pkg/mod.py")


def test_root_non_recursive_matches_only_root_files():
    m = Matcher(["."])
    assert m.match("main.py")
    assert not m.match("src/mod.py")


def test_blank_pattern_matches_nothing_rather_than_granting_root():
    """A stray empty list entry must not silently mean 'the whole repository'."""
    m = Matcher(["   ", ""])
    assert not m.match("main.py")
    assert not m.match("src/mod.py")


def test_multiple_patterns_are_a_union():
    m = Matcher(["src/...", "tools"])
    assert m.match("src/a/b.py")
    assert m.match("tools/x.py")
    assert not m.match("tools/sub/x.py")
    assert not m.match("docs/x.md")


def test_leading_dot_slash_on_the_candidate_is_tolerated():
    assert Matcher(["src/..."]).match("./src/a.py")
