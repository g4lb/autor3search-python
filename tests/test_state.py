import json

import pytest

from autor3search_python import state


@pytest.mark.parametrize("tag", ["sep6", "v1.2_3-rc", "A"])
def test_valid_tags_are_accepted(tag):
    state.valid_tag(tag)


@pytest.mark.parametrize(
    "tag", ["", ".", "..", "../../tmp/evil", "a/b", "a\\b", "a b", "tag!", "/abs"]
)
def test_invalid_tags_are_refused(tag):
    """A tag reaches mkdir long before git's ref rules would ever see it."""
    with pytest.raises(state.StateError):
        state.valid_tag(tag)


def test_branch_round_trip():
    assert state.branch_for("sep6") == "autor3search-python/sep6"
    assert state.tag_from_branch("autor3search-python/sep6") == "sep6"
    assert state.tag_from_branch("main") is None


def test_state_dir_is_out_of_tree_and_keyed_by_repo(tmp_path, state_home):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    da, db = state.state_dir(a, "t"), state.state_dir(b, "t")
    assert da != db
    assert state_home in da.parents
    assert da.name == "t"
    assert len(da.parent.name) == 16  # sha256-of-repo-path key, truncated


def test_state_dir_is_stable_across_path_spellings(tmp_path):
    """macOS /tmp -> /private/tmp must not produce two different state keys."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert state.state_dir(repo, "t") == state.state_dir(repo / ".", "t")


def test_state_dir_separates_tags(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert state.state_dir(repo, "a") != state.state_dir(repo, "b")


def test_state_dir_validates_the_tag(tmp_path):
    with pytest.raises(state.StateError):
        state.state_dir(tmp_path, "../escape")


def test_relative_state_home_is_refused(monkeypatch):
    """It would resolve against whatever directory each command ran from, so
    eval from a subdirectory and stop from the root would address different state."""
    monkeypatch.setenv(state.STATE_HOME_ENV, "relative/path")
    with pytest.raises(state.StateError, match="absolute"):
        state.state_home()


def test_unset_state_home_falls_back_to_the_user_cache(monkeypatch):
    monkeypatch.delenv(state.STATE_HOME_ENV, raising=False)
    assert state.state_home().name == state.STATE_DIR_NAME


def baseline(**kw):
    base = dict(
        tag="sep6",
        branch="autor3search-python/sep6",
        commit="abc1234",
        measure_commit="abc1234",
        created_at="2026-09-06T00:00:00+00:00",
        benchmarks=("t.py::test_a",),
        config_sha256="0" * 64,
    )
    return state.Baseline(**{**base, **kw})


def test_baseline_round_trip(tmp_path):
    p = tmp_path / "state" / "baseline.json"
    baseline().save(p)
    assert json.loads(p.read_text())["measure_commit"] == "abc1234"
    assert state.load_baseline(p) == baseline()


def test_load_baseline_defaults_measure_commit_to_commit(tmp_path):
    """A record written before measure_commit existed must not fail the first eval."""
    p = tmp_path / "baseline.json"
    p.write_text(
        json.dumps(
            {
                "tag": "sep6",
                "branch": "autor3search-python/sep6",
                "commit": "abc1234",
                "created_at": "2026-09-06T00:00:00+00:00",
                "benchmarks": ["t.py::test_a"],
                "config_sha256": "0" * 64,
            }
        )
    )
    assert state.load_baseline(p).measure_commit == "abc1234"


def test_load_baseline_missing_names_the_next_command(tmp_path):
    with pytest.raises(state.StateError, match="baseline"):
        state.load_baseline(tmp_path / "absent.json")


def test_load_baseline_rejects_malformed_json(tmp_path):
    p = tmp_path / "baseline.json"
    p.write_text("{not json")
    with pytest.raises(state.StateError):
        state.load_baseline(p)
