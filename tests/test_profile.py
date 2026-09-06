import json
import pstats

import pytest

from autor3search_python import config, profile
from autor3search_python.cli import main as cli_main
from tests.conftest import git


@pytest.fixture
def repo(git_repo):
    (git_repo / "pkg").mkdir()
    (git_repo / "pkg" / "__init__.py").write_text("")
    (git_repo / "pkg" / "mod.py").write_text(
        "def work():\n"
        "    total = 0\n"
        "    for i in range(5000):\n"
        "        total += i\n"
        "    return [str(i) for i in range(500)] and total\n"
    )
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_mod.py").write_text(
        "from pkg.mod import work\n\n\ndef test_w(benchmark):\n    benchmark(work)\n"
    )
    cli_main.main(["init", "-C", str(git_repo)])
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "init")
    return git_repo


@pytest.mark.slow
def test_profile_writes_both_artifacts_and_names_the_hot_function(repo):
    cfg = config.load(repo / config.CONFIG_PATH)
    report = profile.run_profile(repo, ["tests/test_mod.py::test_w"], cfg)
    assert report.cpu_path.exists()
    assert report.mem_path.exists()
    # A real pstats file, openable by the tool the user will reach for.
    pstats.Stats(str(report.cpu_path))
    assert "work" in report.cpu_top
    assert report.mem_top.strip()


@pytest.mark.slow
def test_profile_command_prints_both_sections(repo, capsys):
    assert cli_main.main(["profile", "-C", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "CPU" in out and "allocation" in out.lower()


@pytest.mark.slow
def test_profile_writes_no_results_row(repo):
    from autor3search_python import results

    cli_main.main(["profile", "-C", str(repo)])
    assert not (repo / results.PATH).exists()


def test_format_cpu_handles_a_missing_file(tmp_path):
    assert "no CPU profile" in profile.format_cpu(tmp_path / "absent.prof")


def test_format_mem_reads_the_json_the_plugin_writes(tmp_path):
    path = tmp_path / "mem.json"
    path.write_text(
        json.dumps(
            {
                "top": [
                    {"file": "pkg/mod.py", "line": 5, "size": 4096, "count": 100},
                    {"file": "pkg/mod.py", "line": 3, "size": 1024, "count": 10},
                ]
            }
        )
    )
    text = profile.format_mem(path)
    assert "pkg/mod.py:5" in text
    assert text.index("pkg/mod.py:5") < text.index("pkg/mod.py:3")


def test_format_mem_handles_a_missing_file(tmp_path):
    assert "no memory profile" in profile.format_mem(tmp_path / "absent.json")


def test_profile_refuses_without_a_config(git_repo, capsys):
    assert cli_main.main(["profile", "-C", str(git_repo)]) == 2
    assert "init" in capsys.readouterr().err
