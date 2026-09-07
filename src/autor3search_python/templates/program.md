# program.md

This file is your instructions, coding agent. Read it fully before doing
anything. It is the only thing a human edits to steer this run — everything
else (the metric, the gates, the verdict) is the installed harness itself
(the `autor3search-python` package). Nothing on the filesystem stops you from
editing it — it is an importable package in a venv like any other — but it is
out of bounds all the same: it is what decides whether your own experiments
are real, and an agent that can edit its own judge is not being measured by
anything. Do not change it, and do not try to.

## Setup

Before starting the loop, do this once:

1. Agree a run tag with the human if one was not already given to you (a
   short slug like `sep4` — today's date or similar is fine).
2. Confirm `autor3search-python baseline -tag <tag>` has already been run for
   this tag. If it has not, stop and ask the human to run it, or run it
   yourself if you have been told you may. `baseline` creates the run
   branch, freezes the current test files as the golden copies, and records
   the commit this run measures against. Everything downstream depends on
   this step having happened exactly once.
3. Read the repository. Skim the package(s) named in `scope` in
   `.autor3search/config.toml`. Run the declared benchmarks yourself once
   (`autor3search-python profile`) so you know what you are starting from
   before changing anything.

## Experimentation

What you MAY do:

- Edit any Python source file that falls under one of the `scope` patterns in
  `.autor3search/config.toml`.
- Add new files inside `scope`, as long as they are not `test_*.py`,
  `*_test.py` or `conftest.py` files. Any new file matching one of those
  patterns trips the `new_test_file` gate — it does not matter whether it
  duplicates an existing test, adds a new one, or only adds a benchmark; the
  benchmark set is fixed at `baseline` time and the gate rejects the whole
  experiment before it is even measured. If you want a different benchmark,
  that is a conversation with the human for the next `baseline`, not
  something to route around mid-run.
- Run any read-only diagnostic command (linters, type checkers,
  `autor3search-python profile`) as often as you like between experiments.

What you MUST NOT do:

- Edit any `test_*.py`, `*_test.py` **or `conftest.py`** file. It is restored
  from the frozen baseline copy before every `eval`, so an edit there is
  silently discarded — worse, it wastes an experiment slot for nothing.
  `conftest.py` is frozen too, and for the same reason as a test file, not a
  weaker one: it can redefine fixtures, alter collection, or monkeypatch the
  code under test, so it can change *what is measured* without touching a
  test at all. If a test looks wrong, say so in your `-desc` for that
  experiment and move on; do not try to route around it.
- Edit `pyproject.toml`, `setup.py`, `setup.cfg`, any `requirements*.txt` or
  `constraints*.txt`, `poetry.lock`, `uv.lock`, `pdm.lock`, `Pipfile` or
  `Pipfile.lock`, ever. These are rejected outright regardless of what
  `scope` says. Changing a dependency is a supply-chain decision a human
  makes, not something an unattended loop decides, and a swapped dependency
  can change *what* is measured, not just how fast it runs.
- Add or edit any file pytest reads its configuration from, at the repository
  root: `pytest.toml`, `.pytest.toml`, `pytest.ini`, `.pytest.ini`, `tox.ini`
  — and `pyproject.toml` and `setup.cfg`, which are already forbidden above as
  dependency files. These are rejected outright regardless of `scope`, for the
  same underlying reason as `conftest.py`: they change *what is measured*, not
  how fast the code is. One `addopts` line changes what pytest collects, how
  it runs and how it is timed — swapping the benchmark timer, or `-k`-ing the
  correctness gate down to nothing. If your change needs different pytest
  settings to be worth measuring, that is a conversation with the human for
  the next `baseline`, not something to route around mid-run.
- Add `sitecustomize` or `usercustomize` at the repository root, in any form
  — `.py`, or a compiled `.pyc` with no source beside it, which imports just
  as well. Python imports these automatically at interpreter startup for
  anything on `sys.path`, and the harness has to put the tree root there, so
  such a file runs arbitrary code inside every gate and every measured process
  before any of them begin.
- Reach for `PYTEST_ADDOPTS` or `PYTEST_PLUGINS` in the environment you run
  `eval` from. Both are stripped before any gate or measurement starts, so
  they will not do what you want; they are named here so you do not waste an
  experiment discovering that.
- Assume a `.gitignore` entry makes a file invisible to the harness. The
  forbidden root files above are checked by looking at the working tree, not
  by reading a git diff, so gitignoring one changes nothing except how long it
  takes you to find out.
- Edit `.autor3search/config.toml`. It is not covered by the scope gate — the
  gate explicitly skips it rather than matching it against `scope` like an
  ordinary source file. Instead, its hash is recorded at `baseline` time; if
  it has changed by `eval` time, the run fails with reason `config_changed`,
  not `scope_violation`.
- Edit an ordinary source file outside `scope`. This *is* what the scope gate
  itself rejects, failing the experiment before it is even measured.
- Try to weaken, disable, or reinterpret the verdict. `eval`'s exit code and
  `--json` output are the only truth. If a result looks wrong, say so in your
  `-desc`; do not try to make the harness agree with you by other means.
- Batch multiple unrelated changes into one experiment. One idea per
  experiment keeps every result attributable and every discard cheap.

## Output format

`autor3search-python eval` is the only command whose result decides anything.
It exits with one of four codes, and program.md's loop below branches on
exactly these:

| Exit code | Meaning | Verdict status |
|---|---|---|
| `0` | KEEP  — the change is a real, safe improvement | `KEEP` |
| `1` | DISCARD — no significant improvement, or it lost the coin flip against noise | `DISCARD` |
| `2` | FAIL — a gate rejected the change: scope violation, a new/edited test file, an import failure, or a test failure | `FAIL` |
| `3` | CRASH — the code failed to compile, a phase timed out, the measurement itself crashed, or the harness malfunctioned | `CRASH` |

A CRASH is never a judgement on your change, and one particular CRASH is not
even about your code: if `eval` prints a Python traceback on stderr and says
the command crashed, the harness itself broke. There will be no `--json`
object and no `results.tsv` row for that experiment. Reset the commit as you
would for any non-KEEP, and say plainly in the next experiment's `-desc` that
the harness crashed — do not start rewriting your change to appease it, and
do not read exit `3` as "not fast enough". That is exit `1`, and it means
something completely different.

Any status the harness cannot classify is reported as exit code `2`
(FAIL) rather than a silent success — treat an unrecognized `--json` status
the same way you would treat FAIL. `ABORTED` (see "Stopping") also arrives as
exit code `2` for exactly that reason: it is not a verdict, and treating it
like FAIL is the right thing to do with it.

**A `KEEP` requires a real, not just a technically significant, improvement.**
`eval` only returns `KEEP` when `score` clears `1 - min_effect_pct/100` (default
1%, i.e. score < 0.99) AND at least one benchmark improved past a
Bonferroni-corrected significance bar. A change that shaves off a fraction of a
percent will be `DISCARD`ed by design, even if the numbers technically moved in
the right direction — do not spend a night chasing sub-1% wins, the harness will
not bank them. And even a `KEEP` is evidence, not proof: any significance
threshold admits some false positives, so treat one `KEEP` as a good sign worth
building on, not as a guarantee that the change actually helped.

With `--json`, `eval` prints one JSON object to stdout with (at least) these
fields:

```json
{
  "status": "KEEP",
  "reason": "improved",
  "score": 0.9123,
  "message": "score 0.9123 (-8.77%)",
  "regressions": [],
  "warnings": [],
  "stop_requested": false,
  "run": {
    "tag": "sep4",
    "branch": "autor3search-python/sep4",
    "baseline_commit": "a3f1c2d",
    "measure_commit": "9b7e410",
    "worktree": "/Users/you/Library/Caches/autor3search-python/1a2b3c4d/sep4/baseline-worktree",
    "experiment": 5
  }
}
```

`status` is one of `KEEP`, `DISCARD`, `FAIL`, `CRASH` — or `ABORTED`, which is
not a verdict at all but an interrupted experiment; see "Stopping" below.
`reason` is a stable, machine-readable code: `improved`,
`no_significant_improvement`, `improvement_below_min_effect`,
`guard_regression`, `scope_violation`, `config_changed`, `new_test_file`,
`symlink_swap`, `baseline_tampered`, `compile_failed`, `import_failed`,
`tests_failed`, `measure_failed`, `timeout`, `stop_forced`.

Two of those discard reasons mean genuinely different things, and the
difference should change what you do next. `no_significant_improvement`
means nothing measurably moved — the idea did not work, drop it.
`improvement_below_min_effect` means it DID work and the harness measured a
real speedup, just a smaller one than `min_effect_pct` will bank. That is a
signal the direction is right: a variation on the same idea with a larger
effect may well clear the bar, whereas the same idea applied to a hotter
path almost certainly would. Do not read it as failure.

`score` is the geometric mean of
`new_time/base_time` across the declared benchmarks — below 1 is faster.
`message` is a human-readable one-liner suitable for a log row.
`regressions`, when present, lists which benchmarks tripped the regression
guard and by how much.

`warnings`, when present, says the measurement is too weak to carry the
verdict printed beside it — the same lines appear as `WARNING:` in the human
output, just above the `VERDICT:` line. They never change the decision; they
tell you not to over-read it. Two you may see:

- **too few rounds for a confidence interval** — the numbers are still the
  measured medians, but the interval around them is unbounded. Raise `count`.
- **no KEEP was reachable** — the significance threshold is corrected for the
  number of benchmarks compared (`alpha/k`), and with the configured `count`
  the test cannot produce a p-value that small however large the improvement
  is. Every experiment will `DISCARD` until `count` is raised, so treat this
  as a broken configuration and stop rather than continuing to burn the night
  on experiments that cannot be banked.

**What `base_time` means changes as the run progresses.** It is NOT always the
commit `baseline` recorded — it is whatever the harness's measurement
baseline currently points to, and a KEEP moves that pointer to the commit
you just kept (see the loop below). So `score` always answers "did THIS
experiment help, compared to the last thing that was kept" — never "is the
tree better than when the run started." Two consequences: (1) after a KEEP,
running `eval` again with nothing new committed measures your last commit
against itself and correctly `DISCARD`s, it is not a bug or a fluke; (2) a
long, honest run's total progress is not any single `score` — it is the
product of every kept `score`, which is what `autor3search-python report`'s
"cumulative speedup" computes for the human.

`run` is the context for the experiment just recorded: which run tag and
branch you are on, the frozen `baseline_commit` the run started from, the
advancing `measure_commit` this experiment was scored against, the pinned
baseline worktree, and `experiment`, the 1-based number of the row just
written to `results.tsv`. None of it changes what you do — it is there so you
can tell the human where the run is, since `--json` is the only channel they
can see through you.

`stop_requested` is the one field that does change what you do. See "Stopping"
below.

## Stopping

The loop does not end on its own. It ends in one of two ways, and only one of
them is yours to act on.

**A graceful stop.** The human runs `autor3search-python stop`. That does not
touch you or the experiment you are running; it writes a request that `eval`
reports back to you as `"stop_requested": true`, alongside a verdict that is
still fully valid. When you see it:

1. Apply this verdict exactly as you would have anyway — KEEP leaves the
   commit, anything else is `git reset --hard HEAD~1`. A stop must never
   leave a commit on the branch that nothing decided on.
2. Do NOT start another experiment.
3. Run `autor3search-python report` and summarize, in a few lines: what you
   tried, what was kept, and what you would try next if the run resumed.
4. Exit the loop and say you have stopped because the human asked.

`stop_requested` never changes the exit code, so read it as a separate
question from the verdict — it says whether to continue, not what this
experiment was worth.

**An interrupt.** The human presses Ctrl+C, or runs `autor3search-python stop
-force` because they could not wait for a long benchmark to finish. Either
one cancels `eval` mid-experiment. You will see `"status": "ABORTED"` with
`"reason": "stop_forced"`, exit code `2`, and no `results.tsv` row — nothing
was measured, so nothing was recorded. Treat the commit the way you would
treat any FAIL (`git reset --hard HEAD~1`), then stop as above.

If the human wants the run to continue after all, they clear the request with
`autor3search-python stop -clear`. That is their decision, not something to
wait for or ask about.

## Logging

`results.tsv` is HARNESS-OWNED. `eval` appends exactly one row to it,
automatically, on every invocation, KEEP or not. You must never create,
append to, or edit `results.tsv` yourself — any manual write is either
redundant (the row already exists) or corrupts a file the harness parses
strictly, which fails the human's morning `autor3search-python report`, or a
future `autor3search-python baseline -force`, on the whole file, not just your
line.

The columns the human sees are:

```
commit	score	best_bench_delta	status	description
```

`commit`, `score`, `best_bench_delta`, and `status` are filled in by the
harness from the verdict — you do not control them. The one column that is
yours is `description`, and you set it by passing `-desc` to `eval`:

```
autor3search-python eval --json -desc "preallocate the list"
```

Always pass `-desc`, on every invocation, KEEP or not — it is the only
record of what you were trying. Keep it terse — a short slug, e.g.
`preallocate-list` — and never dishonest: describe what you actually tried,
including for a DISCARD, FAIL, or CRASH. This file is the human's morning
read. A long trail of honest discards is more useful to the human than a
short trail that hides them.

## Python optimization idea bank

Measure first — `autor3search-python profile` gives you real cProfile and
tracemalloc data — then reach for these:

- **Ask whether the work is needed at all.** Usually the biggest win, in any
  language. Caching, early exit, doing it once instead of per item.
- **Algorithmic change.** A dict lookup instead of a linear scan, a `set` for
  membership, `bisect` on a sorted list, the right data structure for the
  access pattern.
- **Hoist attribute and global lookups out of hot loops.** Every `self.x` and
  every module-level name costs a dictionary lookup per iteration. Bind them to
  a local before the loop.
- **`str.join` instead of `+=` in a loop.** Repeated concatenation is quadratic.
  The same applies to building lists with repeated `+`.
- **Comprehensions and `map` over append loops.** The loop *body* is still
  bytecode either way, but the surrounding iteration machinery runs in C
  instead of interpreting a `for`/`append` pair on every element, which is
  where the win comes from.
- **Avoid re-creating work per call.** Precompile regexes at module scope,
  hoist constant computation, reach for `functools.lru_cache` when the input
  space is small and the function is pure.
- **`__slots__`** on a class instantiated in bulk — removes the per-instance
  dict entirely.
- **Reach for C-level primitives.** `bytes`/`bytearray` where the data really
  is bytes, `array` for homogeneous numbers, `memoryview` to slice without
  copying, `collections.deque` for queue behavior, `itertools` for pipelines.
- **Stop copying.** Slicing copies; `memoryview` and index arithmetic do not.
  A function taking `(buf, start, end)` beats one taking `buf[start:end]` in a
  loop.
- **Batch across the boundary.** If the code already depends on numpy, pandas
  or a C extension, one vectorized call beats a Python loop calling in a
  million times. Do NOT add a dependency to achieve this — dependency files
  are rejected outright.
- **Generators for streaming.** Building a full list to consume it once wastes
  the allocation and the memory traffic.
- **Local aliasing of builtins** (`_len = len`) in the hottest loops only.
  It is ugly; reserve it for a loop the profile actually named.

## The experiment loop

`eval` already splits its own output for you: the verdict — one compact
JSON object — is written to stdout, and the full, noisy compile/import/test/
benchmark transcript is written automatically to `run.log` in the
repository root. `run.log` is the harness's own file, opened by `eval`
itself before it does anything else — never redirect `eval`'s stdout into
it yourself (`eval --json > run.log 2>&1` or similar). Doing so opens a
second, independent descriptor on a path the harness already has open for
the transcript; if the two ever fall out of step, whichever one writes
second overwrites the other from byte 0, destroying exactly the
diagnostic transcript you need when a step FAILs or CRASHes and you want
to see why. Run `eval --json` bare and read the verdict straight from its
stdout; open `run.log` only to read it, never to write to it.

LOOP FOREVER:

0. Print one context line, so the human watching knows where the run is
   without having to read the JSON:
   `[exp <n> | <branch> | vs <measure_commit> | stop: autor3search-python stop]`
   Take the numbers from the previous experiment's `run` object; on the first
   pass, from `autor3search-python status`.
1. Check git state: confirm you are on the run branch.
2. If you have no strong hypothesis, run `autor3search-python profile` and
   read the hot spots.
3. Change ONE thing in the in-scope Python source. One idea per experiment.
4. `git add -A && git commit -m "<idea>"`
5. `autor3search-python eval --json -desc "<idea>"` (no redirect — do NOT tee
   or send stdout to `run.log`; either floods your context or clobbers the
   transcript, see above). `-desc` is what lands in `results.tsv`'s
   `description` column for this experiment — always pass it.
6. Read the verdict directly from stdout: it is one compact JSON object,
   so parse or `grep '"status"'` it straight from the command's own output.
7. KEEP  -> leave the commit in place, the branch advances, AND the
   harness's measurement baseline advances to this commit too — your next
   experiment is measured against what you just kept, not against where
   the run started.
   Anything else -> `git reset --hard HEAD~1`
8. If the verdict carried `"stop_requested": true`, or its status was
   `ABORTED`, the human has asked you to stop. Do not start another
   experiment — follow "Stopping" above and leave the loop.
9. Go to 0.

**NEVER STOP ON YOUR OWN**: Do not pause to ask whether to continue. The
human may be asleep. You are autonomous. If you run out of ideas, re-read the
profile output, re-read the idea bank, combine previous near-misses, or try a
more radical change.

Only the human ends this loop, and only in the two ways "Stopping" describes:
a stop request (`"stop_requested": true`) or an interrupt (`"status":
"ABORTED"`). Running out of ideas is not one of them, and neither is a long
string of DISCARDs.
