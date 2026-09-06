import json
import pstats

import pytest

from autor3search_python import config, profile, profiling
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
    # The allocation report must name the user's own file, not just be
    # non-empty: a plugin that filtered out (or never recorded) anything
    # would still leave format_mem's "(no allocation sites recorded)"
    # placeholder, which is truthy but useless.
    assert "mod.py" in report.mem_top


@pytest.mark.slow
def test_profile_command_prints_both_sections(repo, capsys):
    assert cli_main.main(["profile", "-C", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "=== CPU ===" in out
    assert "=== allocation sites ===" in out
    # Not just the section headers: the hot function and the user's own
    # file must actually be named, in both sections.
    assert "work" in out
    assert "mod.py" in out


@pytest.mark.slow
def test_profile_writes_no_results_row(repo):
    from autor3search_python import results

    assert cli_main.main(["profile", "-C", str(repo)]) == 0
    assert not (repo / results.PATH).exists()


@pytest.mark.slow
def test_profile_does_not_leak_the_other_passs_env_var(repo, monkeypatch, tmp_path):
    """Regression: AUTOR3SEARCH_PYTHON_CPU_PROFILE set in the *parent*
    environment used to survive into the memory pass. pytest_configure then
    took the cProfile branch during that pass too, silently profiling the
    wrong thing, dump_stats() clobbered whatever file the leaked variable
    happened to point at, no mem.json was ever written, and the command
    still exited 0.
    """
    leaked_cpu_target = tmp_path / "leaked-cpu-target.bin"
    leaked_cpu_target.write_bytes(b"do not touch")
    leaked_mem_target = tmp_path / "leaked-mem-target.json"
    monkeypatch.setenv(profiling.CPU_ENV, str(leaked_cpu_target))
    monkeypatch.setenv(profiling.MEM_ENV, str(leaked_mem_target))

    cfg = config.load(repo / config.CONFIG_PATH)
    report = profile.run_profile(repo, ["tests/test_mod.py::test_w"], cfg)

    assert leaked_cpu_target.read_bytes() == b"do not touch"
    assert not leaked_mem_target.exists()
    assert report.cpu_path.exists()
    assert report.mem_path.exists()
    pstats.Stats(str(report.cpu_path))  # the real cpu.prof, not garbage


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


def test_format_mem_labels_sites_as_retained_not_allocated(tmp_path):
    """The site listing is memory still live when the session ended, not
    total bytes allocated over the run — tracemalloc cannot report the
    latter, and a label implying it would be actively misleading."""
    path = tmp_path / "mem.json"
    path.write_text(json.dumps({"top": [{"file": "a.py", "line": 1, "size": 1, "count": 1}]}))
    text = profile.format_mem(path)
    assert "still allocated" in text or "retained" in text
    # It must actively disclaim being an allocation-volume figure, not merely
    # omit the word "allocated" from a size column header.
    assert "not total bytes allocated" in text


def test_format_mem_reports_the_peak_when_the_plugin_recorded_one(tmp_path):
    path = tmp_path / "mem.json"
    path.write_text(
        json.dumps(
            {"top": [{"file": "a.py", "line": 1, "size": 1, "count": 1}], "peak_bytes": 2048}
        )
    )
    text = profile.format_mem(path)
    assert "peak" in text.lower()
    assert "2.0K" in text


def test_format_mem_relativizes_to_the_repo_root(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    abs_file = root / "pkg" / "mod.py"
    path = tmp_path / "mem.json"
    path.write_text(
        json.dumps({"top": [{"file": str(abs_file), "line": 5, "size": 4096, "count": 1}]})
    )
    text = profile.format_mem(path, root=root)
    assert "pkg/mod.py:5" in text
    assert str(root) not in text


def test_format_mem_elides_long_paths_at_a_separator_not_mid_component(tmp_path):
    root = tmp_path / "repo"
    nested = root / "src" / "very" / "deeply" / "nested" / "package"
    nested.mkdir(parents=True)
    abs_file = nested / "module_with_a_reasonably_long_name.py"
    path = tmp_path / "mem.json"
    path.write_text(
        json.dumps({"top": [{"file": str(abs_file), "line": 42, "size": 4096, "count": 1}]})
    )
    text = profile.format_mem(path, root=root)
    site_line = next(
        ln for ln in text.splitlines() if "module_with_a_reasonably_long_name.py" in ln
    )
    # The filename must appear whole, immediately preceded by a path
    # separator (or the "..." elision marker) — never truncated mid-name,
    # the way a blind `site[-60:]` slice would.
    idx = site_line.index("module_with_a_reasonably_long_name.py")
    assert site_line[idx - 1] == "/"


def test_profile_refuses_without_a_config(git_repo, capsys):
    assert cli_main.main(["profile", "-C", str(git_repo)]) == 2
    assert "init" in capsys.readouterr().err
