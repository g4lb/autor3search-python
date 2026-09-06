import json

import pytest

from autor3search_python import benchio, pipeline, results, runstop, state, verdict
from autor3search_python.cli import eval as cli_eval
from autor3search_python.cli import main as cli_main
from tests.conftest import git


@pytest.fixture
def run_ready(git_repo, monkeypatch):
    (git_repo / "pkg").mkdir()
    (git_repo / "pkg" / "__init__.py").write_text("")
    (git_repo / "pkg" / "mod.py").write_text("def work():\n    return 1\n")
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_mod.py").write_text(
        "from pkg.mod import work\n\n\ndef test_w(benchmark):\n    benchmark(work)\n"
    )
    cli_main.main(["init", "-C", str(git_repo)])
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "init")
    cli_main.main(["baseline", "-C", str(git_repo), "-tag", "t1"])
    return git_repo


def stub(result, deltas=()):
    def fake(_opts):
        return result, pipeline.Measurements(time=list(deltas))

    return fake


def delta(pct=-10.0):
    ratio = 1 + pct / 100
    return benchio.Delta(
        name="tests/test_mod.py::test_w",
        base_center=1.0,
        cand_center=ratio,
        ratio=ratio,
        pct_change=pct,
        p=0.001,
        alpha=0.05,
        significant=True,
        n_base=10,
        n_cand=10,
    )


def keep():
    return verdict.Result(
        status=verdict.Status.KEEP,
        reason=verdict.Reason.IMPROVED,
        score=0.9,
        message="score 0.9000 (-10.00%)",
    )


@pytest.mark.parametrize(
    "status,code",
    [
        (verdict.Status.KEEP, 0),
        (verdict.Status.DISCARD, 1),
        (verdict.Status.FAIL, 2),
        (verdict.Status.CRASH, 3),
    ],
)
def test_exit_code_follows_the_verdict(run_ready, monkeypatch, status, code):
    r = verdict.Result(status=status, reason=verdict.Reason.IMPROVED, score=0.9)
    monkeypatch.setattr(pipeline, "evaluate", stub(r, [delta()]))
    assert cli_main.main(["eval", "-C", str(run_ready), "-desc", "x"]) == code


def test_json_prints_exactly_one_object_and_nothing_else(run_ready, monkeypatch, capsys):
    """The agent's loop parses stdout directly; a second line would break it."""
    monkeypatch.setattr(pipeline, "evaluate", stub(keep(), [delta()]))
    cli_main.main(["eval", "-C", str(run_ready), "--json", "-desc", "preallocate"])
    out = capsys.readouterr().out
    doc = json.loads(out)  # raises if there is anything else on stdout
    assert doc["status"] == "KEEP"
    assert doc["reason"] == "improved"
    assert doc["score"] == pytest.approx(0.9)
    assert doc["stop_requested"] is False
    assert doc["run"]["tag"] == "t1"
    assert doc["run"]["branch"] == "autor3search-python/t1"
    assert doc["run"]["experiment"] == 1
    assert "worktree" in doc["run"]


def test_a_row_is_appended_on_every_verdict(run_ready, monkeypatch):
    monkeypatch.setattr(pipeline, "evaluate", stub(keep(), [delta(-25.0)]))
    cli_main.main(["eval", "-C", str(run_ready), "-desc", "first"])
    monkeypatch.setattr(
        pipeline,
        "evaluate",
        stub(
            verdict.Result(verdict.Status.DISCARD, verdict.Reason.NO_IMPROVEMENT, 1.0), [delta(0.0)]
        ),
    )
    cli_main.main(["eval", "-C", str(run_ready), "-desc", "second"])
    rows = results.load(run_ready / results.PATH)
    assert [r.status for r in rows] == ["KEEP", "DISCARD"]
    assert [r.description for r in rows] == ["first", "second"]
    assert rows[0].best_bench_delta == pytest.approx(-25.0)


def test_a_gate_failure_still_gets_a_row(run_ready, monkeypatch):
    r = verdict.gate(verdict.Status.FAIL, verdict.Reason.SCOPE, "out of scope")
    monkeypatch.setattr(pipeline, "evaluate", lambda opts: (r, None))
    cli_main.main(["eval", "-C", str(run_ready), "-desc", "oops"])
    rows = results.load(run_ready / results.PATH)
    assert rows[0].status == "FAIL"
    assert rows[0].score == 0.0


def test_stop_requested_is_reported_without_changing_the_exit_code(run_ready, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "evaluate", stub(keep(), [delta()]))
    runstop.request_stop(state.state_dir(run_ready, "t1"))
    code = cli_main.main(["eval", "-C", str(run_ready), "--json", "-desc", "x"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["stop_requested"] is True


def test_a_concurrent_eval_is_refused(run_ready, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "evaluate", stub(keep(), [delta()]))

    def claim_fails(*a, **k):
        raise runstop.StopError("another autor3search-python eval (pid 5) is already running")

    monkeypatch.setattr(runstop, "claim_eval", claim_fails)
    assert cli_main.main(["eval", "-C", str(run_ready), "-desc", "x"]) == 2
    assert "already running" in capsys.readouterr().err


def test_the_transcript_goes_to_run_log_not_stdout(run_ready, monkeypatch, capsys):
    def noisy(opts):
        opts.log.write("a very long build transcript\n")
        return keep(), pipeline.Measurements(time=[delta()])

    monkeypatch.setattr(pipeline, "evaluate", noisy)
    cli_main.main(["eval", "-C", str(run_ready), "--json", "-desc", "x"])
    assert "transcript" not in capsys.readouterr().out
    assert "transcript" in (run_ready / pipeline.RUN_LOG_NAME).read_text()


def test_run_log_is_appended_not_truncated(run_ready, monkeypatch):
    def noisy(opts):
        opts.log.write("round\n")
        return keep(), pipeline.Measurements(time=[delta()])

    monkeypatch.setattr(pipeline, "evaluate", noisy)
    cli_main.main(["eval", "-C", str(run_ready), "-desc", "a"])
    cli_main.main(["eval", "-C", str(run_ready), "-desc", "b"])
    assert (run_ready / pipeline.RUN_LOG_NAME).read_text().count("round") == 2


def test_human_output_shows_warnings_above_the_verdict(run_ready, monkeypatch, capsys):
    r = verdict.Result(
        status=verdict.Status.DISCARD,
        reason=verdict.Reason.NO_IMPROVEMENT,
        score=1.0,
        message="score 1.0000 (+0.00%), no significant improvement",
        warnings=("no KEEP was reachable: ...",),
    )
    monkeypatch.setattr(pipeline, "evaluate", stub(r, [delta(0.0)]))
    cli_main.main(["eval", "-C", str(run_ready), "-desc", "x"])
    out = capsys.readouterr().out
    assert out.index("WARNING:") < out.index("VERDICT:")


def test_keyboard_interrupt_becomes_aborted_with_no_row(run_ready, monkeypatch, capsys):
    """An interrupted experiment measured nothing, so it records nothing."""

    def interrupted(_opts):
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "evaluate", interrupted)
    assert cli_main.main(["eval", "-C", str(run_ready), "--json", "-desc", "x"]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "ABORTED"
    assert doc["reason"] == "stop_forced"
    assert results.load(run_ready / results.PATH) == []


def test_a_harness_malfunction_is_not_a_verdict(run_ready, monkeypatch, capsys):
    def broken(_opts):
        raise OSError("disk gone")

    monkeypatch.setattr(pipeline, "evaluate", broken)
    assert cli_main.main(["eval", "-C", str(run_ready), "-desc", "x"]) == 2
    assert "disk gone" in capsys.readouterr().err
    assert results.load(run_ready / results.PATH) == []


def test_best_bench_delta_picks_the_largest_improvement():
    assert cli_eval.best_bench_delta([delta(-5.0), delta(-30.0), delta(2.0)]) == pytest.approx(
        -30.0
    )
    assert cli_eval.best_bench_delta([]) == 0.0
