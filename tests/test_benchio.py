import json

import pytest

from autor3search_python import benchio


def payload(*entries):
    return json.dumps(
        {
            "machine_info": {},
            "benchmarks": [
                {"fullname": name, "stats": {"median": med, "min": mn, "mean": mean}}
                for name, med, mn, mean in entries
            ],
        }
    )


def test_parse_takes_the_configured_statistic():
    s = benchio.parse(payload(("t.py::test_a", 2.0, 1.0, 3.0)), "median")
    assert s.values("t.py::test_a") == [2.0]
    assert benchio.parse(payload(("t.py::test_a", 2.0, 1.0, 3.0)), "min").values(
        "t.py::test_a"
    ) == [1.0]
    assert benchio.parse(payload(("t.py::test_a", 2.0, 1.0, 3.0)), "mean").values(
        "t.py::test_a"
    ) == [3.0]


def test_parse_accepts_bytes_and_dict():
    raw = payload(("t.py::test_a", 2.0, 1.0, 3.0))
    assert benchio.parse(raw.encode(), "median").names() == ["t.py::test_a"]
    assert benchio.parse(json.loads(raw), "median").names() == ["t.py::test_a"]


def test_parse_rejects_malformed_json():
    with pytest.raises(benchio.BenchError):
        benchio.parse("{not json", "median")


def test_parse_rejects_a_missing_statistic():
    bad = json.dumps({"benchmarks": [{"fullname": "t.py::test_a", "stats": {"min": 1.0}}]})
    with pytest.raises(benchio.BenchError, match="median"):
        benchio.parse(bad, "median")


def test_base_name_strips_parametrization():
    assert benchio.base_name("t.py::test_a[big]") == "t.py::test_a"
    assert benchio.base_name("t.py::TestC::test_a[1-2]") == "t.py::TestC::test_a"
    assert benchio.base_name("t.py::test_a") == "t.py::test_a"


def test_select_by_base_matches_every_parametrization():
    s = benchio.Set()
    s.record("t.py::test_a[big]", 1.0)
    s.record("t.py::test_a[small]", 2.0)
    s.record("t.py::test_b", 3.0)
    got = s.select_by_base(["t.py::test_a"])
    assert got.names() == ["t.py::test_a[big]", "t.py::test_a[small]"]


def test_select_by_base_with_no_bases_selects_everything():
    s = benchio.Set()
    s.record("t.py::test_a", 1.0)
    assert s.select_by_base([]).names() == ["t.py::test_a"]


def test_add_accumulates_in_order():
    a, b = benchio.Set(), benchio.Set()
    a.record("x", 1.0)
    b.record("x", 2.0)
    a.add(b)
    assert a.values("x") == [1.0, 2.0]


def test_values_returns_a_defensive_copy():
    s = benchio.Set()
    s.record("x", 1.0)
    got = s.values("x")
    got.append(99.0)
    assert s.values("x") == [1.0]


def _set(name, values):
    s = benchio.Set()
    for v in values:
        s.record(name, v)
    return s


def test_compare_reports_a_speedup_as_a_ratio_below_one():
    base = _set("x", [10.0] * 8)
    cand = _set("x", [5.0] * 8)
    d = benchio.compare(base, cand, "x")
    assert d.ratio == pytest.approx(0.5)
    assert d.pct_change == pytest.approx(-50.0)
    assert d.significant is True
    assert d.n_base == 8 and d.n_cand == 8


def test_compare_on_identical_samples_is_not_significant():
    base = _set("x", [1.0, 1.1, 0.9, 1.0, 1.05, 0.95, 1.0, 1.02])
    cand = _set("x", [1.0, 1.1, 0.9, 1.0, 1.05, 0.95, 1.0, 1.02])
    assert benchio.compare(base, cand, "x").significant is False


def test_compare_warns_when_the_sample_is_too_small_for_an_interval():
    d = benchio.compare(_set("x", [1.0, 2.0, 3.0]), _set("x", [1.0, 2.0, 3.0]), "x")
    assert any("confidence interval" in w for w in d.warnings)


def test_compare_deduplicates_warnings():
    d = benchio.compare(_set("x", [1.0, 2.0, 3.0]), _set("x", [1.0, 2.0, 3.0]), "x")
    assert len(d.warnings) == len(set(d.warnings))


def test_compare_rejects_a_zero_baseline_median():
    with pytest.raises(benchio.BenchError, match="zero"):
        benchio.compare(_set("x", [0.0] * 8), _set("x", [1.0] * 8), "x")


def test_compare_all_fails_when_a_benchmark_vanishes():
    base = benchio.Set()
    for v in [1.0] * 8:
        base.record("x", v)
        base.record("y", v)
    cand = _set("x", [1.0] * 8)
    with pytest.raises(benchio.BenchError, match="missing from the candidate"):
        benchio.compare_all(base, cand)


def test_compare_all_fails_on_an_empty_intersection():
    with pytest.raises(benchio.BenchError):
        benchio.compare_all(benchio.Set(), benchio.Set())


def test_compare_all_is_sorted_by_name():
    base, cand = benchio.Set(), benchio.Set()
    for name in ("b", "a"):
        for v in [1.0] * 8:
            base.record(name, v)
            cand.record(name, v)
    assert [d.name for d in benchio.compare_all(base, cand)] == ["a", "b"]
