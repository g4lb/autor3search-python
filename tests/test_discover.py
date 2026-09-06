from pathlib import Path

from autor3search_python import discover


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_finds_a_fixture_benchmark(tmp_path):
    write(
        tmp_path,
        "tests/test_speed.py",
        """
def test_plain():
    assert True

def test_fast(benchmark):
    benchmark(lambda: 1)
""",
    )
    got = discover.benchmarks(tmp_path)
    assert [b.name for b in got] == ["tests/test_speed.py::test_fast"]
    assert got[0].file == "tests/test_speed.py"
    assert got[0].func == "test_fast"


def test_finds_a_marked_benchmark(tmp_path):
    write(
        tmp_path,
        "tests/test_marked.py",
        """
import pytest

@pytest.mark.benchmark(group="g")
def test_marked():
    pass
""",
    )
    assert [b.name for b in discover.benchmarks(tmp_path)] == ["tests/test_marked.py::test_marked"]


def test_finds_a_benchmark_method_in_a_class(tmp_path):
    write(
        tmp_path,
        "tests/test_cls.py",
        """
class TestThing:
    def test_inner(self, benchmark):
        benchmark(lambda: 1)
""",
    )
    assert [b.name for b in discover.benchmarks(tmp_path)] == [
        "tests/test_cls.py::TestThing::test_inner"
    ]


def test_ignores_a_non_test_function_taking_benchmark(tmp_path):
    write(tmp_path, "tests/test_x.py", "def helper(benchmark):\n    pass\n")
    assert discover.benchmarks(tmp_path) == []


def test_unparseable_file_is_skipped_not_fatal(tmp_path):
    """Discovery must work on a tree that does not even parse — that is why it is AST-based."""
    write(tmp_path, "tests/test_broken.py", "def test_x(benchmark:\n")
    write(tmp_path, "tests/test_ok.py", "def test_y(benchmark):\n    pass\n")
    assert [b.name for b in discover.benchmarks(tmp_path)] == ["tests/test_ok.py::test_y"]


def test_skips_the_directories_the_tooling_ignores(tmp_path):
    for d in (".venv", "__pycache__", "build", "node_modules", ".hidden", "_private"):
        write(tmp_path, f"{d}/test_x.py", "def test_x(benchmark):\n    pass\n")
    assert discover.benchmarks(tmp_path) == []
    assert discover.frozen_files(tmp_path) == []


def test_frozen_files_covers_all_three_patterns(tmp_path):
    write(tmp_path, "tests/test_a.py", "")
    write(tmp_path, "tests/b_test.py", "")
    write(tmp_path, "tests/conftest.py", "")
    write(tmp_path, "conftest.py", "")
    write(tmp_path, "src/mod.py", "")
    assert discover.frozen_files(tmp_path) == [
        "conftest.py",
        "tests/b_test.py",
        "tests/conftest.py",
        "tests/test_a.py",
    ]


def test_frozen_files_honours_exclude(tmp_path):
    write(tmp_path, "tests/test_a.py", "")
    write(tmp_path, "tests/test_b.py", "")
    assert discover.frozen_files(tmp_path, ["tests/test_a.py"]) == ["tests/test_b.py"]


def test_node_ids_are_sorted_and_deduplicated():
    bs = [
        discover.Benchmark("b.py::t", "b.py", "t"),
        discover.Benchmark("a.py::t", "a.py", "t"),
        discover.Benchmark("a.py::t", "a.py", "t"),
    ]
    assert discover.node_ids(bs) == ["a.py::t", "b.py::t"]
