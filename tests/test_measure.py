import json

import pytest

from autor3search import benchio, config, measure
from autor3search import runner as runner_mod


def const(value):
    def fn(round_index):
        s = benchio.Set()
        s.record("x", value + round_index * 0.0)
        return s

    return fn


def test_interleave_accumulates_both_sides():
    base, cand = measure.interleave(4, False, const(2.0), const(1.0))
    assert base.values("x") == [2.0] * 4
    assert cand.values("x") == [1.0] * 4


def test_interleave_alternates_sides():
    """Both sides must see the same conditions; a batched A-then-B ordering
    would attribute thermal drift to the code change."""
    order = []

    def side(label):
        def fn(i):
            order.append(f"{label}{i}")
            s = benchio.Set()
            s.record("x", 1.0)
            return s

        return fn

    measure.interleave(3, False, side("b"), side("c"))
    assert order == ["b0", "c0", "b1", "c1", "b2", "c2"]


def test_warmup_round_is_run_and_discarded():
    calls = []

    def side(value):
        def fn(i):
            calls.append(i)
            s = benchio.Set()
            s.record("x", value)
            return s

        return fn

    base, cand = measure.interleave(3, True, side(2.0), side(1.0))
    assert len(calls) == 8  # 4 rounds x 2 sides
    assert base.values("x") == [2.0] * 3
    assert cand.values("x") == [1.0] * 3


def test_fewer_than_two_rounds_is_refused():
    with pytest.raises(measure.MeasureError):
        measure.interleave(1, False, const(1.0), const(1.0))


def test_a_failing_round_names_the_side_and_index():
    def boom(i):
        raise RuntimeError("round exploded")

    with pytest.raises(measure.MeasureError, match="candidate round 0"):
        measure.interleave(3, False, const(1.0), boom)


def test_timing_plausibility_passes_for_comparable_overhead():
    base_audit = measure._TimingAudit(wall_seconds=1.0, reported_seconds=0.5)
    cand_audit = measure._TimingAudit(wall_seconds=1.0, reported_seconds=0.4)
    measure._check_timing_plausibility(base_audit, cand_audit)  # must not raise


def test_timing_plausibility_fails_when_the_candidate_reports_far_too_little():
    """The demonstrated attack's shape: the candidate subprocess ran for the
    same wall time as the baseline but reported a fraction of it, because its
    own in-scope code monkeypatched pytest-benchmark's accounting."""
    base_audit = measure._TimingAudit(wall_seconds=1.0, reported_seconds=0.5)
    cand_audit = measure._TimingAudit(wall_seconds=1.0, reported_seconds=0.05)
    with pytest.raises(measure.TimingImplausibleError):
        measure._check_timing_plausibility(base_audit, cand_audit)


def test_timing_plausibility_does_not_fire_when_baseline_has_more_overhead():
    """Only a gap favouring the CANDIDATE is treated as suspicious — a
    baseline that happens to look slower relative to its own report is not."""
    base_audit = measure._TimingAudit(wall_seconds=5.0, reported_seconds=0.1)
    cand_audit = measure._TimingAudit(wall_seconds=1.0, reported_seconds=0.5)
    measure._check_timing_plausibility(base_audit, cand_audit)  # must not raise


def test_timing_plausibility_skips_when_nothing_usable_was_reported():
    """A missing/unrecognized pytest-benchmark schema must not manufacture a
    false alarm — see benchio.total_reported_seconds."""
    measure._check_timing_plausibility(
        measure._TimingAudit(wall_seconds=1.0, reported_seconds=0.0),
        measure._TimingAudit(wall_seconds=1.0, reported_seconds=0.0),
    )


def _fake_bench_reporting(reported_by_dir_suffix: dict[str, float], wall_seconds: float):
    """A Runner.bench stand-in that writes a benchmark JSON report whose
    total is chosen by which side's directory it was run in, while every
    side takes the SAME wall-clock time — reproducing the demonstrated
    attack's shape without spawning a real subprocess."""

    def fake_bench(self, node_ids, json_path, cfg):
        suffix = "cand" if str(self.directory).endswith("cand") else "base"
        reported = reported_by_dir_suffix[suffix]
        doc = {
            "benchmarks": [
                {
                    "fullname": node_ids[0],
                    "stats": {"median": reported, "rounds": 1, "total": reported},
                }
            ]
        }
        json_path.write_text(json.dumps(doc), encoding="utf-8")
        return runner_mod.Result((), "", "", 0, False, wall_seconds)

    return fake_bench


def test_run_refuses_a_candidate_side_that_underreports_wall_time(tmp_path, monkeypatch):
    """End-to-end reproduction, at the measure.run() level, of the
    demonstrated Stats.update monkeypatch: same wall time on both sides,
    candidate reports far less of it."""
    monkeypatch.setattr(
        runner_mod.Runner,
        "bench",
        _fake_bench_reporting({"base": 0.5, "cand": 0.01}, wall_seconds=0.5),
    )
    base_dir, cand_dir = tmp_path / "base", tmp_path / "cand"
    base_dir.mkdir()
    cand_dir.mkdir()
    opts = measure.Options(
        base_dir=base_dir,
        cand_dir=cand_dir,
        node_ids=["tests/test_x.py::test_y"],
        cfg=config.default(),
    )
    with pytest.raises(measure.TimingImplausibleError):
        measure.run(opts)


def test_run_accepts_comparable_overhead_on_both_sides(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner_mod.Runner,
        "bench",
        _fake_bench_reporting({"base": 0.5, "cand": 0.45}, wall_seconds=0.5),
    )
    base_dir, cand_dir = tmp_path / "base", tmp_path / "cand"
    base_dir.mkdir()
    cand_dir.mkdir()
    opts = measure.Options(
        base_dir=base_dir,
        cand_dir=cand_dir,
        node_ids=["tests/test_x.py::test_y"],
        cfg=config.default(),
    )
    base_set, cand_set = measure.run(opts)
    assert base_set.names() == ["tests/test_x.py::test_y"]
    assert cand_set.names() == ["tests/test_x.py::test_y"]
