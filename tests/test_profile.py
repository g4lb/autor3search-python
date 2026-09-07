import json
import pstats
from pathlib import Path

import pytest

from autor3search_python import config, profile, profiling
from autor3search_python.cli import main as cli_main
from tests.conftest import git


@pytest.fixture
def repo(git_repo):
    (git_repo / "pkg").mkdir()
    (git_repo / "pkg" / "__init__.py").write_text("")
    (git_repo / "pkg" / "mod.py").write_text(
        # `work` deliberately RETAINS its allocation. The allocation pass
        # reports blocks still live at the session-end snapshot, so a
        # benchmark whose allocations are transient leaves the site list
        # legitimately empty — which made the earlier version of this
        # fixture pass on macOS and fail on Linux depending on whether the
        # interpreter happened to still hold the list. Replacing the
        # contents each call keeps exactly one live allocation attributable
        # to this module, bounded however many rounds run.
        "_RETAINED = []\n\n\n"
        "def work():\n"
        "    total = 0\n"
        "    for i in range(5000):\n"
        "        total += i\n"
        "    _RETAINED[:] = [str(i) for i in range(500)]\n"
        "    return total\n"
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
    assert "=== allocation ===" in out
    # Not just the section headers: the hot function and the user's own
    # file must actually be named, in both sections.
    assert "work" in out
    assert "mod.py" in out


@pytest.fixture
def transient_repo(git_repo):
    """A repo whose benchmark allocates only transiently, retaining nothing."""
    (git_repo / "pkg").mkdir()
    (git_repo / "pkg" / "__init__.py").write_text("")
    (git_repo / "pkg" / "mod.py").write_text(
        "def work():\n    return len([str(i) for i in range(500)])\n"
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
def test_a_transient_benchmark_reports_a_real_peak_and_labels_what_it_measured(transient_repo):
    """The bug this whole change fixes.

    Before per-benchmark peaks existed, a transient-allocating benchmark
    reported NOTHING: the session-end snapshot only sees blocks still live
    when it is taken, and this benchmark frees everything before then. A
    fix that only changed wording, without actually measuring anything new,
    would still pass a test that merely checks for labels — so this test
    also pins a real magnitude for the transient benchmark's own node id,
    which is exactly the number that used to not exist.
    """
    cfg = config.load(transient_repo / config.CONFIG_PATH)
    report = profile.run_profile(transient_repo, ["tests/test_mod.py::test_w"], cfg)

    doc = json.loads(report.mem_path.read_text(encoding="utf-8"))
    peaks = doc["test_peaks"]
    assert set(peaks) == {"tests/test_mod.py::test_w"}
    # 500 short strings is at least a few tens of KB once pytest-benchmark's
    # own per-round bookkeeping is added in; a broken (or reverted) plugin
    # reports 0 here, because nothing about this benchmark survives to the
    # session-end snapshot the old code relied on exclusively.
    assert peaks["tests/test_mod.py::test_w"] > 10_000

    assert "peak additional traced memory during each benchmark" in report.mem_top
    assert "tests/test_mod.py::test_w" in report.mem_top
    assert "not filtered to this repository" in report.mem_top
    assert "still allocated when the session ended" in report.mem_top
    assert "retained memory, not total bytes allocated" in report.mem_top


@pytest.fixture
def mixed_alloc_repo(git_repo):
    """One module with a transient-only function and a retaining one.

    The two measurements this change adds/keeps answer different questions,
    and this fixture is built so both have something real to say: the
    session-end snapshot sees `retaining`'s survivor (an assertable site),
    while only the per-benchmark peak sees `transient`'s churn (which the
    snapshot alone would report as nothing at all).
    """
    (git_repo / "pkg").mkdir()
    (git_repo / "pkg" / "__init__.py").write_text("")
    (git_repo / "pkg" / "mod.py").write_text(
        "_RETAINED = []\n\n\n"
        "def transient():\n"
        "    return len([str(i) for i in range(50_000)])\n\n\n"
        "def retaining():\n"
        "    _RETAINED[:] = [str(i) for i in range(500)]\n"
        "    return len(_RETAINED)\n"
    )
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_mod.py").write_text(
        "from pkg.mod import retaining, transient\n\n\n"
        "def test_transient(benchmark):\n"
        "    benchmark(transient)\n\n\n"
        "def test_retaining(benchmark):\n"
        "    benchmark(retaining)\n"
    )
    cli_main.main(["init", "-C", str(git_repo)])
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "init")
    return git_repo


@pytest.mark.slow
def test_per_benchmark_peaks_are_keyed_by_node_id_for_every_benchmark(mixed_alloc_repo):
    cfg = config.load(mixed_alloc_repo / config.CONFIG_PATH)
    node_ids = ["tests/test_mod.py::test_transient", "tests/test_mod.py::test_retaining"]
    report = profile.run_profile(mixed_alloc_repo, node_ids, cfg)

    doc = json.loads(report.mem_path.read_text(encoding="utf-8"))
    peaks = doc["test_peaks"]
    assert set(peaks) == set(node_ids)
    # The transient benchmark allocates ~50,000 short strings — a peak on
    # the order of a megabyte, not zero. This is exactly the case the old
    # session-end-only snapshot reported as empty.
    assert peaks["tests/test_mod.py::test_transient"] > 500_000
    # The retaining benchmark allocates far less (500 strings); its peak
    # should be real but much smaller than the transient one's.
    assert (
        0 < peaks["tests/test_mod.py::test_retaining"] < peaks["tests/test_mod.py::test_transient"]
    )

    # The retaining benchmark's allocation also survives to the session-end
    # snapshot and is named there, by source line.
    assert "mod.py" in report.mem_top
    for node_id in node_ids:
        assert node_id in report.mem_top


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


def test_format_mem_reports_the_peak_for_each_test_id(tmp_path):
    path = tmp_path / "mem.json"
    path.write_text(
        json.dumps(
            {
                "top": [{"file": "a.py", "line": 1, "size": 1, "count": 1}],
                "test_peaks": {"tests/test_x.py::test_hot": 2048},
            }
        )
    )
    text = profile.format_mem(path)
    assert "peak" in text.lower()
    assert "tests/test_x.py::test_hot" in text
    assert "2.0K" in text


def test_format_mem_keys_peaks_by_node_id_and_shows_every_benchmark(tmp_path):
    path = tmp_path / "mem.json"
    path.write_text(
        json.dumps(
            {
                "top": [],
                "test_peaks": {
                    "tests/test_x.py::test_hot": 105_000,
                    "tests/test_x.py::test_cold": 2_100,
                },
            }
        )
    )
    text = profile.format_mem(path)
    assert "tests/test_x.py::test_hot" in text
    assert "tests/test_x.py::test_cold" in text
    # The bigger peak is listed first — descending, like the retained-sites
    # table below it, so the most actionable entry is always on top.
    assert text.index("test_hot") < text.index("test_cold")


def test_format_mem_says_plainly_when_no_benchmarks_ran(tmp_path):
    path = tmp_path / "mem.json"
    path.write_text(json.dumps({"top": [], "test_peaks": {}}))
    text = profile.format_mem(path)
    assert "no per-benchmark peaks recorded" in text
    assert "no benchmarks ran" in text
    # Not an empty table: no "benchmark ... peak" header row printed when
    # there is nothing to put under it.
    assert not any(line.strip().startswith("benchmark ") for line in text.splitlines())


def test_format_mem_distinguishes_peak_from_retained_labels(tmp_path):
    """A reader must not be able to mistake the per-benchmark peak (allocation
    volume, process-wide) for the retained-sites table (still-live bytes,
    filtered to the repository) — the whole point of reporting both."""
    path = tmp_path / "mem.json"
    path.write_text(
        json.dumps(
            {
                "top": [{"file": "a.py", "line": 1, "size": 4096, "count": 1}],
                "test_peaks": {"tests/test_x.py::test_hot": 2048},
            }
        )
    )
    text = profile.format_mem(path)
    assert "peak additional traced memory during each benchmark" in text
    assert "not filtered to this repository" in text
    assert "blocks still allocated when the session ended" in text
    assert "retained memory, not total bytes allocated" in text


def test_format_mem_relativizes_to_the_repo_root(tmp_path):
    # A short, fixed root — NOT built from tmp_path. tmp_path carries a long,
    # unpredictable prefix (pytest-of-<user>/pytest-NNN/<test-name>/...) that
    # can itself exceed the display width, so an un-relativized absolute path
    # would get truncated away by chance and "root not in text" would pass
    # for the wrong reason even with relativization removed entirely. A short
    # root that comfortably fits the width means it can only be absent from
    # the output because it was actually stripped.
    root = Path("/repo")
    abs_file = root / "pkg" / "mod.py"
    path = tmp_path / "mem.json"
    path.write_text(
        json.dumps({"top": [{"file": str(abs_file), "line": 5, "size": 4096, "count": 1}]})
    )
    text = profile.format_mem(path, root=root)
    assert "pkg/mod.py:5" in text
    assert str(root) not in text


def test_format_mem_elides_long_paths_at_a_separator_not_mid_component(tmp_path):
    # A short, fixed root (see above) plus long parent-directory components,
    # so the *relativized* display path alone is long enough to force
    # elision — a blind `site[-60:]` on this path lands inside a directory
    # name, not on a "/" boundary, which the previous fixture's short
    # filename could never expose (the character immediately before a short
    # filename always survives a tail slice unchanged, bug or no bug).
    root = Path("/repo")
    nested = (
        root
        / "src"
        / "very_deeply_nested_package_directory"
        / "another_long_subpackage_name"
        / "yet_another_nested_module_dir"
    )
    abs_file = nested / "module_with_a_reasonably_long_name.py"
    path = tmp_path / "mem.json"
    path.write_text(
        json.dumps({"top": [{"file": str(abs_file), "line": 42, "size": 4096, "count": 1}]})
    )
    text = profile.format_mem(path, root=root)
    site_line = next(
        ln for ln in text.splitlines() if "module_with_a_reasonably_long_name.py" in ln
    )
    # Elision must fall back to "...", dropping the parent directories that
    # do not fit, rather than blindly slicing the last 60 characters of the
    # raw relative path — which would cut into "yet_another_nested_module_dir"
    # mid-word and never produce this exact, whole-component result.
    assert site_line.startswith(".../module_with_a_reasonably_long_name.py:42")


def test_profile_refuses_without_a_config(git_repo, capsys):
    assert cli_main.main(["profile", "-C", str(git_repo)]) == 2
    assert "init" in capsys.readouterr().err
