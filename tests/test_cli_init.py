import pytest

from autor3search_python import config
from autor3search_python.cli import init as cli_init
from autor3search_python.cli import main as cli_main


@pytest.fixture
def repo_with_benchmark(git_repo):
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_speed.py").write_text(
        "def test_fast(benchmark):\n    benchmark(lambda: 1)\n"
    )
    return git_repo


def test_init_writes_config_program_and_gitignore(repo_with_benchmark, capsys):
    assert cli_main.main(["init", "-C", str(repo_with_benchmark)]) == 0
    cfg_path = repo_with_benchmark / ".autor3search" / "config.toml"
    assert cfg_path.exists()
    assert (repo_with_benchmark / "program.md").exists()
    text = (repo_with_benchmark / ".gitignore").read_text()
    for entry in cli_init.GITIGNORE_ENTRIES:
        assert entry in text
    out = capsys.readouterr().out
    assert "tests/test_speed.py::test_fast" in out


def test_the_written_config_loads_back(repo_with_benchmark):
    cli_main.main(["init", "-C", str(repo_with_benchmark)])
    cfg = config.load(repo_with_benchmark / ".autor3search" / "config.toml")
    assert cfg.benchmarks == ("tests/test_speed.py::test_fast",)
    assert cfg.count == 10


def test_init_refuses_to_overwrite_without_force(repo_with_benchmark, capsys):
    cli_main.main(["init", "-C", str(repo_with_benchmark)])
    assert cli_main.main(["init", "-C", str(repo_with_benchmark)]) == 2
    assert "-force" in capsys.readouterr().err


def test_force_overwrites(repo_with_benchmark):
    cli_main.main(["init", "-C", str(repo_with_benchmark)])
    assert cli_main.main(["init", "-C", str(repo_with_benchmark), "-force"]) == 0


def test_init_refuses_a_repo_with_no_benchmarks(git_repo, capsys):
    """No benchmarks means no notion of faster; a config with an empty list
    would silently optimize nothing."""
    assert cli_main.main(["init", "-C", str(git_repo)]) == 2
    err = capsys.readouterr().err
    assert "no benchmarks" in err.lower()
    assert not (git_repo / ".autor3search").exists()


def test_init_refuses_outside_a_git_repository(tmp_path, capsys):
    assert cli_main.main(["init", "-C", str(tmp_path)]) == 2
    assert "git init" in capsys.readouterr().err


def test_gitignore_entries_are_not_duplicated(repo_with_benchmark):
    cli_main.main(["init", "-C", str(repo_with_benchmark)])
    cli_main.main(["init", "-C", str(repo_with_benchmark), "-force"])
    text = (repo_with_benchmark / ".gitignore").read_text()
    assert text.count("results.tsv") == 1


def test_gitignore_re_includes_the_config(repo_with_benchmark):
    """git cannot re-include a file whose parent directory is excluded, so the
    directory's CONTENTS are excluded and the one file re-included."""
    cli_main.main(["init", "-C", str(repo_with_benchmark)])
    text = (repo_with_benchmark / ".gitignore").read_text()
    assert ".autor3search/*" in text
    assert "!.autor3search/config.toml" in text


def test_render_config_is_commented(repo_with_benchmark):
    text = cli_init.render_config(config.default())
    assert text.count("#") > 20
    for key in (
        "benchmarks",
        "scope",
        "count",
        "benchtime",
        "min_rounds",
        "stat",
        "max_regress_pct",
        "min_effect_pct",
        "timeout",
        "hashseed",
        "gc",
        "pythonpath",
        "python",
        "unfreeze",
        "[gates]",
    ):
        assert key in text


def test_default_tag_is_a_short_date_slug():
    tag = cli_init.default_tag()
    assert tag.isalnum() and tag.islower() and 3 <= len(tag) <= 6
