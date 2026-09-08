"""The whole thing, end to end, against a real repository and real measurement."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from autor3search_python import results, state
from autor3search_python.cli import main as cli_main
from tests.conftest import git

DEMO = Path(__file__).resolve().parent.parent / "testdata" / "demo"

pytestmark = pytest.mark.slow


@pytest.fixture
def demo_repo(tmp_path):
    repo = tmp_path / "demo"
    shutil.copytree(DEMO, repo)
    (repo / "fast_wordcount.py.txt").rename(repo / "fast.txt")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "demo")
    return repo


def run_eval(repo, desc):
    """Invoke eval as a subprocess so the JSON contract is tested as agents see it."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "autor3search_python.cli.main",
            "eval",
            "-C",
            str(repo),
            "--json",
            "-desc",
            desc,
        ],
        capture_output=True,
        text=True,
    )
    assert proc.stdout.strip(), proc.stderr
    return proc.returncode, json.loads(proc.stdout)


def test_full_run(demo_repo):
    repo = demo_repo

    # init discovers the benchmark and refuses nothing.
    assert cli_main.main(["init", "-C", str(repo)]) == 0
    cfg_path = repo / ".autor3search" / "config.toml"
    assert cfg_path.exists()

    # Raise the minimum effect size well above the default 1%. The no-op step
    # below MUST discard, but on a loaded shared runner a no-op's measured
    # delta can land a point or two from zero with a convincing p-value: CI
    # has scored one at -1.36%, clearing both the default 1% floor and the
    # corrected significance bar. That is the residual Type-I error the
    # README's Limitations section documents rather than a bug in the
    # decision rule, but it makes for a flaky test.
    #
    # 10% sits between the two effects with room on both sides: roughly seven
    # times the largest no-op delta observed, and well clear of the KEEP step
    # below, which turns a quadratic per-character concatenation into a single
    # join and measures -25.8% (score 0.7418) on the machine this was written
    # on. What matters is that ratio — the floor sitting well inside the gap
    # between the two effects — not the absolute number, so a fixture with a
    # different true effect would want a different floor.
    raised = cfg_path.read_text().replace("min_effect_pct = 1.0", "min_effect_pct = 10.0")
    assert "min_effect_pct = 10.0" in raised, "init's config no longer has the key this rewrites"
    cfg_path.write_text(raised)

    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "autor3search-python init")

    # doctor is informational and never blocks.
    assert cli_main.main(["doctor", "-C", str(repo)]) == 0

    # baseline pins the run.
    assert cli_main.main(["baseline", "-C", str(repo), "-tag", "e2e"]) == 0
    sd = state.state_dir(repo, "e2e")
    base = state.load_baseline(sd / state.BASELINE_FILE)

    # A no-op commit must DISCARD: nothing changed, so nothing improved.
    (repo / "wordcount.py").write_text(
        (repo / "wordcount.py").read_text() + "\n# a comment changes nothing\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "no-op")
    code, doc = run_eval(repo, "no-op")
    assert code == 1, doc
    assert doc["status"] == "DISCARD"
    assert doc["run"]["experiment"] == 1
    git(repo, "reset", "-q", "--hard", "HEAD~1")

    # The real optimization must KEEP, and must advance the measurement commit.
    (repo / "wordcount.py").write_text((repo / "fast.txt").read_text())
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "join once instead of concatenating per character")
    code, doc = run_eval(repo, "join-once")
    assert code == 0, doc
    assert doc["status"] == "KEEP"
    assert doc["score"] < 0.99
    assert doc["run"]["experiment"] == 2
    advanced = state.load_baseline(sd / state.BASELINE_FILE)
    assert advanced.measure_commit != base.measure_commit
    assert advanced.commit == base.commit  # the frozen anchor never moves

    # results.tsv holds both experiments, honestly.
    rows = results.load(repo / results.PATH)
    assert [r.status for r in rows] == ["DISCARD", "KEEP"]
    assert [r.description for r in rows] == ["no-op", "join-once"]

    # report summarizes them.
    assert cli_main.main(["report", "-C", str(repo)]) == 0

    # stop is read by the next eval.
    assert cli_main.main(["stop", "-C", str(repo)]) == 0
    (repo / "wordcount.py").write_text((repo / "wordcount.py").read_text() + "\n# another no-op\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "third")
    _, doc = run_eval(repo, "third")
    assert doc["stop_requested"] is True


def test_a_weakened_test_cannot_buy_a_keep(demo_repo):
    """The headline guarantee, end to end."""
    repo = demo_repo
    cli_main.main(["init", "-C", str(repo)])
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    cli_main.main(["baseline", "-C", str(repo), "-tag", "e2e"])

    original = (repo / "test_wordcount.py").read_text()
    (repo / "wordcount.py").write_text("def count_words(s):\n    return {'x': 1}\n")
    (repo / "test_wordcount.py").write_text(
        "from wordcount import count_words\n\n\n"
        "def test_count_words_benchmark(benchmark):\n    benchmark(count_words, 'x')\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "gut the implementation and the test")

    code, doc = run_eval(repo, "cheat")
    assert code == 2
    assert doc["status"] == "FAIL"
    assert doc["reason"] == "tests_failed"
    # The frozen test was restored before it ran, so the real assertions ran.
    assert (repo / "test_wordcount.py").read_text() == original


def test_an_added_benchmark_is_rejected(demo_repo):
    repo = demo_repo
    cli_main.main(["init", "-C", str(repo)])
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    cli_main.main(["baseline", "-C", str(repo), "-tag", "e2e"])

    (repo / "test_easy.py").write_text("def test_easy(benchmark):\n    benchmark(lambda: None)\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add an easier benchmark")
    code, doc = run_eval(repo, "easier-benchmark")
    assert code == 2
    assert doc["reason"] == "new_test_file"
