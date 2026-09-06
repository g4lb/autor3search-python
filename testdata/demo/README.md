# demo

The worked example from the README, and the fixture the end-to-end test drives.

`wordcount.py` builds each word by repeated concatenation, which is quadratic in
the word length. `fast_wordcount.py.txt` is the optimized replacement the test
copies over it, to prove the harness returns KEEP for a real improvement and
DISCARD for a no-op.

The bug only costs anything measurable once a single "word" is roughly
kilobyte-scale — see the comment above `BENCH_INPUT` in `test_wordcount.py`
for the numbers. Running this demo against ordinary short words and seeing a
DISCARD does not mean the tool is broken; it means the input has no word long
enough to separate the two implementations.

It is deliberately not a package and has no `pyproject.toml`: the harness
imports each tree straight off disk via PYTHONPATH, and this is the simplest
layout that exercises that.
