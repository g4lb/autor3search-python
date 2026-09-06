import pytest

from autor3search_python import benchio, measure


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
