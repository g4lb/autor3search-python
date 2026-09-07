# autor3search-python

[![PyPI](https://img.shields.io/pypi/v/autor3search-python?label=pypi)](https://pypi.org/project/autor3search-python/)
[![ci](https://github.com/g4lb/autor3search-python/actions/workflows/ci.yml/badge.svg)](https://github.com/g4lb/autor3search-python/actions/workflows/ci.yml)

**Autonomous AI-driven performance optimization for any Python repository.**

A coding agent proposes one change at a time; a harness the agent cannot edit
gates the change for correctness, measures it against a pinned baseline with
an interleaved A/B design, scores it, and answers **KEEP** or **DISCARD**. You
wake up to a branch of accepted commits and a log of every experiment,
including the failures.

Inspired by [karpathy/autoresearch](https://github.com/karpathy/autoresearch).
This is a Python port of [g4lb/autor3search-go](https://github.com/g4lb/autor3search-go),
its Go sibling — same design, same guarantees, a different metric source: where
the Go harness reads `ns/op` out of `go test -bench`, this one reads per-round
timings out of [pytest-benchmark](https://pytest-benchmark.readthedocs.io/).

> **Status: early but working.** Validated against one real library —
> [`humanize`](https://github.com/python-humanize/humanize) — where it found and
> kept a genuine 5.81% win across its 15 benchmarks (hoisting a per-call
> `import math` out of nine function bodies; `clamp` −23.2%, `apnumber` −14.9%,
> no regressions). That is one library, not the three the Go original was
> exercised against, so treat this as a tool that inherited a validated design
> and has begun earning its own record rather than one that already has it.
>
> Every number in this README is a real measurement taken on the machine that
> wrote it, never an illustration. Where a number would have been guessed, there
> is no number.
>
> The decision procedure — the scoring rules, the Bonferroni correction, the
> asymmetric regression guard, the four exit codes — is carried over unchanged
> from the Go original. If you run this against your own project, the harness's
> `results.tsv` and `report` output are the honest record of what it actually did
> there; that is rather the point of the whole design.

---

## Start here

If you are a coding agent that has just been pointed at this README, this is
everything you need:

```bash
# 1. Install the harness.
uv tool install autor3search-python   # or: pipx install autor3search-python

# 2. From the repository you want to optimize:
autor3search-python init              # discovers benchmarks, writes config + program.md
git add -A && git commit -m "autor3search-python init"
autor3search-python doctor            # is this machine fit to measure? (informational)
autor3search-python baseline -tag <a-short-tag>

# 3. Read program.md. It is the complete instruction set — the loop, the exit
#    codes, the JSON contract, what you may and may not edit. Follow it exactly.
```

Everything past this point is for the human setting the run up, or for
understanding what the agent in step 3 is actually bound by.

## The idea

Five things make up a run, and they are owned by different parties on purpose:

| What | Owned by | Editable by the agent? |
|---|---|---|
| The harness (`autor3search-python` itself) | the tool | No — it is what measures the agent, not what it measures |
| Frozen tests (`test_*.py`, `*_test.py`, `conftest.py`) | whoever wrote them | No — restored from a golden copy before every `eval` |
| Your source (everything else in `scope`) | you, then the agent | Yes — this is the whole point |
| `program.md` | you (the human) | Nominally yes, meaningfully no — it is the agent's own instructions; an agent that edits its own judge is not being measured by anything |
| Out-of-tree run state (`baseline.json`, the frozen-file store, the pinned worktree, the stop sentinel) | the harness | No — it does not live in the repository at all, specifically so the agent cannot reach it |

The design premise is adversarial: assume an agent under pressure to show
progress will eventually try to win by cheating — editing a test to match
broken code, adding an easier benchmark, editing the baseline to make it look
slow — and close each route. That is why the things the score depends on live
**outside the repository the agent edits**: state that lived in-tree would be
state the same OS user running the agent could simply rewrite.

## Quick start

```bash
autor3search-python init                                # 1
git add -A && git commit -m "autor3search-python init"   # 2
autor3search-python doctor                               # optional, informational
autor3search-python baseline -tag sep7                    # 3
autor3search-python eval --json -desc "first idea"        # 4, repeated
```

1. **`init`** AST-scans the repository for pytest-benchmark benchmarks,
   writes `.autor3search/config.toml` (commented, safe to hand-edit) and
   `program.md`, and adds gitignore entries for the harness's own in-tree
   output. It refuses outright if it finds no benchmarks — see
   [Repos with no benchmarks](#repos-with-no-benchmarks).
2. **The commit between `init` and `baseline` matters.** `baseline` refuses a
   dirty working tree, and more importantly, it freezes whatever is on disk
   *right now* as the golden copy of every test file. If `config.toml` were
   left uncommitted, a later `git reset` could silently un-pin the run's
   config from what was actually measured against.
3. **`baseline -tag <tag>`** creates branch `autor3search-python/<tag>`,
   snapshots every frozen file into an out-of-tree store, pins a detached git
   worktree at the current commit (the thing every candidate is measured
   against), and records it all in `baseline.json`. One tag, one run.
4. **`eval --json -desc "<what you tried>"`** is the only command whose
   result decides anything: gate for correctness, measure, score, append one
   row to `results.tsv`, print one JSON object, exit 0/1/2/3. Repeat this one
   command — commit an idea, `eval`, keep or revert — for as long as the run
   continues.

## Watching a run, and stopping it

`autor3search-python status [-tag <tag>]` is read-only and safe to run at any
time, from any branch:

```
run tag        sep7
branch         autor3search-python/sep7  (checked out)
baseline       1b21269  (run started here)
measuring vs   f855cca  (advanced past the baseline by earlier KEEPs)
worktree       /Users/you/Library/Caches/autor3search-python/563272a.../sep7/baseline-worktree
experiments    2 run  (1 keep, 1 discard, 0 fail, 0 crash)  — next is #3
eval           not running
stop           not requested

to stop after the current experiment:  autor3search-python stop
to stop now, abandoning it:            autor3search-python stop -force
```

Three ways to stop, differing only in what happens to the experiment in
flight:

- **`autor3search-python stop`** — graceful. Writes a request the agent reads
  at its next verdict: that experiment finishes, is scored, and its verdict is
  applied exactly as normal, and only then does the agent leave the loop.
  Nothing measured is thrown away.
- **`autor3search-python stop -force`** — also signals the running `eval`'s
  process group to abandon the experiment in progress. That experiment is
  lost (nothing was measured, so no `results.tsv` row is written for it), and
  the command reports the repository state this leaves behind without
  changing it.
- **Ctrl+C on the agent** — equivalent to `stop -force`.
- **`autor3search-python stop -clear`** cancels a pending graceful-stop
  request, letting the loop continue.

## Commands

| Command | What it does |
|---|---|
| `init` | AST-discovers benchmarks, writes `.autor3search/config.toml` + `program.md` + `.gitignore` entries. Refuses to overwrite a config without `-force`. Refuses outright when no benchmarks are found. |
| `doctor` | Machine fitness. Always exits 0; informational. |
| `baseline -tag T` | Creates branch `autor3search-python/T`, freezes files, pins a detached worktree, records the baseline. Refuses a dirty tree and a reused tag. |
| `profile` | Runs the declared benchmarks under `cProfile` and `tracemalloc`, writes `.autor3search/profiles/{cpu.prof,mem.json}`, prints top hot spots, per-benchmark peak allocation, and retained allocation sites. |
| `eval` | One experiment. `--json` prints exactly one JSON object and nothing else. `-desc` sets the `results.tsv` description. Exits 0/1/2/3. |
| `status` | Read-only: run branch, both commits, worktree, experiment counts by verdict, whether an eval is in flight, whether a stop is pending. `-tag` works from any branch. |
| `stop` | Writes the graceful-stop request. `-clear` cancels it; `-force` also signals the running eval's process group and reports the resulting repository state without changing it. |
| `report` | Summarizes `results.tsv`: counts by status, cumulative speedup as the **product** of every kept score, largest individual wins. |
| `version` | The installed distribution version, or the git commit for a checkout, marked `dirty` when the tree had uncommitted changes. |

Every command except `version` accepts `-C <dir>` to operate on another
repository without changing the process's working directory. (`version`
reports which build of the harness is running, which is not a property of any
repository, so the flag would be meaningless there.)

### Where run state lives

Everything the agent must not be able to touch — `baseline.json`, the golden
copies of every frozen file, the pinned detached worktree, the stop sentinel,
the running eval's pid — lives outside the repository entirely, under:

```
<state home>/<sha256(absolute repo path)[:16]>/<tag>/
```

`<state home>` defaults to the platform's user cache directory (macOS
`~/Library/Caches`, Linux `$XDG_CACHE_HOME` or `~/.cache`, Windows
`%LOCALAPPDATA%`), all under an `autor3search-python/` subdirectory. Override it
with `AUTOR3SEARCH_PYTHON_STATE_HOME` (must be an absolute path — a relative one
is refused, since it would resolve differently depending on which directory
each command happened to be run from). Every test in this project's own suite
points this variable at a throwaway directory, so running the tests never
touches your real cache.

The one exception is `.autor3search/config.toml`, which lives *in* the
repository on purpose: a human is meant to own it and version-control it. It
is protected by integrity checking instead of relocation — its sha256 is
recorded at `baseline` time, and any change to it fails every subsequent `eval`
with reason `config_changed`.

## Worked example

`testdata/demo/` is the repository this project's own end-to-end test drives,
and the same one this section walks through. It is a tiny word counter with a
real, if easy to miss, performance bug:

```python
def count_words(s):
    counts = {}
    for field in s.split():
        word = ""
        for ch in field:
            if "A" <= ch <= "Z":
                ch = chr(ord(ch) + 32)
            if ("a" <= ch <= "z") or ("0" <= ch <= "9"):
                word = word + ch  # rebuilds the string every character
        if word:
            counts[word] = counts.get(word, 0) + 1
    return counts
```

and one test file with a correctness test and a benchmark, which — like every
test file — is frozen and identical before and after the fix:

```python
def test_count_words_benchmark(benchmark):
    result = benchmark(count_words, BENCH_INPUT)
    assert result
```

`init`, commit, `doctor`, `baseline -tag sep7` as in Quick start above. Then
two experiments:

1. **A no-op** — append a comment to `wordcount.py`, commit, `eval`. Nothing
   about the code changed, so nothing should measure as faster, and it must
   `DISCARD`.
2. **The real fix** — replace the function with one that appends characters
   to a list and joins once, instead of concatenating a string one character
   at a time, commit, `eval`. This must `KEEP`.

Both were actually run, on this machine, to write this section (Apple Silicon
Mac, Python 3.14, `count = 10`, `benchtime = "1s"`, defaults otherwise — the
numbers below are one real run and will vary somewhat from run to run, the way
any wall-clock measurement does):

| Experiment | base | candidate | change | p | score | Verdict |
|---|---|---|---|---|---|---|
| no-op (append a comment) | 14.458 ms | 14.513 ms | +0.38% | 0.28 (n.s.) | 1.0038 | `DISCARD` (`no_significant_improvement`) |
| join once instead of `+=` | 14.553 ms | 10.767 ms | −26.02% | <0.0001 | 0.7398 | `KEEP` (`improved`) |

One detail that matters more than it looks: getting a *real* separation here
took a benchmark input with at least one multi-kilobyte token in it — a
base64 blob, an embedded payload, a serialized trace — not only short
natural-language words, and not even a short identifier. CPython's own
interpreter already optimizes `s = s + ch` in place when `s`'s refcount is 1,
so short-string concatenation in a tight loop is cheap in practice regardless
of the "obviously quadratic" source code. Measured directly against token
length: a UUID (36 chars) makes the "optimized" version *lose* by +4.4%; a
SHA-256 hex digest (64 chars) is noise at -1.4%; separation only becomes real
in the hundreds of characters and clearly dominant in the thousands (-8.8% at
256, -22.3% at 1024, -27.3% at the 6000-character token this demo ships with).
The bug is real, but it only costs something once a single token is roughly
kilobyte-scale — `testdata/demo` picks its benchmark input accordingly; a
repository you point this at may need the same care if its first benchmark
shows no separation between two versions you know behave differently.

## What the harness enforces

Every row here has a dedicated test in this project's own suite (`tests/`,
plus `tests/test_e2e.py` for the ones checked end to end against a running
process).

| Attempt | Why it fails |
|---|---|
| Edit a frozen test to match broken code | Restored from the golden copy before every `eval` runs; the edit is silently discarded and the real assertions run against the real change |
| Delete a frozen test | Same restore path — a missing frozen file comes back |
| Add a new `test_*.py` / `*_test.py` file (an easier benchmark, a duplicate) | `new_test_file`: any frozen-pattern file not present in the baseline manifest fails the whole experiment, whether it duplicates an existing test or adds a genuinely new one |
| Edit `conftest.py` | Frozen for the same reason as a test file, not a weaker one — it can redefine fixtures, alter collection, or monkeypatch the code under test, changing *what is measured* without touching a test at all |
| Symlink-swap a frozen path | `symlink_swap` — restore refuses to write through a symlink where a regular file belongs |
| Edit a source file outside `scope` | `scope_violation` — checked before anything is even restored or compiled |
| Edit `.autor3search/config.toml` | `config_changed` — its sha256 is pinned at `baseline` time; the scope gate does not even look at it, because a changed config is a different failure |
| Edit a dependency file (`pyproject.toml`, `setup.py`/`.cfg`, `requirements*.txt`, `constraints*.txt`, `poetry.lock`, `uv.lock`, `pdm.lock`, `Pipfile[.lock]`) | Rejected outright regardless of `scope` — a dependency swap is a supply-chain decision for a human, and it changes *what* is measured, not just how fast it runs |
| Edit any file pytest reads its config from (`pytest.toml`, `.pytest.toml`, `pytest.ini`, `.pytest.ini`, `tox.ini`; `pyproject.toml` and `setup.cfg` are already above) | Rejected outright regardless of `scope` — pytest reads them, so one `addopts` line changes what is collected, how it runs and how it is *timed*. `--benchmark-timer=` pointing at a fake clock turned a comment-only edit into a 90% "improvement"; `-k` narrowed collection until a broken implementation walked past the correctness gate. The list is pytest's own `locate_config` search order, shared with `doctor`'s coverage check so the two cannot drift |
| Add `sitecustomize` or `usercustomize` at the repository root, as `.py` **or** as a sourceless `.pyc` | Rejected outright regardless of `scope`, matched by stem across every importable suffix — a `.pyc` with no source beside it imports through `SourcelessFileLoader` just as well. CPython's `site` imports these at interpreter startup for anything on `sys.path`, and measurement has to put the tree root there, so such a file runs arbitrary code inside every gate and both bench sides before any of them begin, on the candidate side only |
| `.gitignore` one of those forbidden root files so the harness cannot see it | Every other gate reads a git diff, and `changed_since` passes `--exclude-standard`, so this really does hide the file from them — which is why these particular names are also checked by statting the working tree. Present on disk and absent at baseline is a `scope_violation` whatever git has been told |
| Set `PYTEST_ADDOPTS` or `PYTEST_PLUGINS` in the environment `eval` runs in | Stripped by `bench_env` before any gate or measurement starts — the agent owns that environment, and `PYTEST_ADDOPTS` is a `pytest.ini` it never has to write down |
| Edit the pinned baseline worktree in place, to make the baseline look slow | Detected (not proven-impossible — see [Limitations](#limitations)) when the recorded `measure_commit` no longer matches the worktree's actual HEAD: `baseline_tampered` |

## Scoring

```
score = geomean(candidate_median / baseline_median) over the declared benchmarks
```

`KEEP` requires all three:

1. `score < 1 - min_effect_pct/100` (default 1%, i.e. `score < 0.99`) — a
   change can be statistically real and still too small to be worth an
   unattended commit.
2. At least one benchmark improves at the **Bonferroni-corrected** threshold
   `alpha / k`, where `k` is the number of benchmarks compared. Testing `k`
   benchmarks against the same raw `alpha` inflates the family-wise
   false-positive rate; dividing by `k` is the standard correction.
3. No benchmark regresses beyond `max_regress_pct` (default 5%) at the
   **raw, uncorrected** `alpha`.

That asymmetry in rule 3 is deliberate, not an oversight: Bonferroni only ever
makes significance *harder* to reach, so applying it to the regression guard
would make real harm easier to miss. Be conservative about accepting a win; be
liberal about catching damage.

A `KEEP` re-points the pinned measurement baseline (`measure_commit`) to the
just-kept commit, while the frozen anchor (`commit`) never moves for the life
of the run. This means `score` always answers "did *this* experiment help,
compared to the last thing that was kept" — never "is the tree better than
when the run started" — and a long run's total progress is the *product* of
every kept score, which is what `report`'s cumulative speedup computes.

### When the measurement cannot carry the verdict

Two warnings appear in `--json` output (as `warnings`) and above `VERDICT:` in
human output. Neither ever changes the decision; both say the numbers beside
them should not be over-read:

- **Too few rounds for a confidence interval.** The reported medians are real,
  but with fewer than 6 observations per side the distribution-free interval
  around a median has no upper bound. Raise `count`.
- **No `KEEP` was reachable at all.** The Mann-Whitney U test has a p-value
  floor for a given sample size; if the Bonferroni-corrected threshold falls
  below that floor for every benchmark, no benchmark can ever clear it, and
  every experiment will `DISCARD` regardless of how large the real
  improvement is. This is a configuration problem (raise `count`), not a
  reason to keep burning experiments on it.

## Limitations

Ported from the Go original:

- No attempt to make the harness tamper-proof against a same-user attacker.
  The worktree-integrity check catches accidental clobbering and careless
  tampering, not a determined attacker who also rewrites `baseline.json` to
  match, or who never moves `HEAD` at all.
- One metric source. No `asv`-style backend, no analogue of `-race`.
- No allocation column in `results.tsv` (see below) — the allocation story
  moved entirely to `profile`.

Python-specific, new in this port:

- **The PYTHONPATH-injection import strategy cannot build compiled
  extensions.** Each tree is imported straight off disk with `PYTHONPATH` set
  to that tree's root (and `src/`, for a src-layout) — there is no install
  step. `doctor` checks this for real: it discovers the repository's
  top-level packages and modules and actually imports each one in a
  subprocess, under the same environment and interpreter a run would use, and
  reports FAIL with the real error when one does not import — a package with
  a `setup.py` declaring `ext_modules`, a `Cargo.toml`, `*.pyx` sources, or a
  `meson.build` cannot be served this way, and when a failure coincides with
  one of those signals `doctor` names it as the likely reason. It also checks
  for gitignored files inside the packages it imports — a generated file
  (`_version.py` from setuptools-scm or hatch-vcs, say) that exists locally
  but was never committed will be missing from the pinned baseline worktree,
  and `doctor` names it and suggests `git add -f`. None of this refuses to
  run; it reports.
- **Coverage in `addopts` silently destroys every timing.** A `--cov` baked
  into `pyproject.toml`, `pytest.ini`, `setup.cfg` or `tox.ini` instruments
  every call in every measured round; `doctor` checks for it and warns. This
  is the single highest-value check it runs.
- **`stat = "min"` vs. `"median"` is a real trade-off, not a free knob.**
  `min` gives a tighter distribution and more statistical power to detect a
  real difference, but it reports a best case rather than a typical one, and
  the Mann-Whitney layer above it is built to compare distributions of
  typical values. The default is `median`; switch to `min` only knowing what
  you are trading.
- **`profile`'s allocation section reports two different measurements, and
  neither one is per-line allocation churn.** `tracemalloc` cannot attribute
  freed allocations to a source line at all — that needs a native allocator
  hook (e.g. `memray`), which this project does not take a dependency on. So
  it reports what `tracemalloc` actually can:
  - **Steady-state peak traced memory per benchmark.** The memory pass runs
    with `-p no:benchmark`, disabling pytest-benchmark entirely, and the
    harness supplies its own minimal `benchmark` fixture instead — one that
    calls the benchmarked callable directly, a handful of times, bracketing
    each call with `tracemalloc.reset_peak()` / `get_traced_memory()`, and
    keeps the MINIMUM peak across those calls. Two things depend on that
    design, not just on tracemalloc:
    - **Why not pytest-benchmark's own timing loop.** It picks its round
      count from a time budget, so a fast, cheap callable runs far more
      rounds than a slow one within the same budget — and pytest-benchmark's
      own per-round bookkeeping, proportional to round count rather than to
      what the callable allocates, would then dominate the peak. A benchmark
      that allocates nothing could rank ABOVE one that allocates megabytes,
      purely because it ran more rounds. Calling the callable directly a
      fixed number of times sidesteps this: every benchmark runs under equal
      conditions regardless of its own speed.
    - **Why the minimum, not the first call or the mean.** A one-time cost —
      a lazy import on the first call, say — would otherwise inflate
      whatever combines it with the repeated case. The minimum reports the
      steady state instead, the same regime pytest-benchmark's own warmup
      targets for timing, so both halves of the profile describe the same
      thing.

    This is the only one of the two measurements that sees a *transient*
    allocation's volume — a benchmark that allocates and frees a large
    structure every call shows up here even though nothing about it
    survives to be snapshotted later. It is still **process-wide, not
    filtered to the target repository**, because `get_traced_memory()` has
    no such filter — so it is not literally "bytes this function
    allocated" — but it is no longer inflated by pytest-benchmark's own
    round-count bookkeeping.
  - **Retained sites, by source line, from a session-end snapshot.** This is
    `tracemalloc`'s own snapshot, filtered to the target repository, and it
    reports blocks still live when the session ended — result objects,
    caches, leaks. A benchmark whose allocations are entirely transient
    (freed well before the session ends) legitimately produces an empty
    site list here; that is not a bug, it is this measurement correctly
    reporting on a question it cannot otherwise answer. The peak figure
    above is what covers that case instead.

  Neither figure is per-line allocation churn: the peak is a single
  process-wide number per benchmark, not attributed to a source line, and
  the retained sites are a snapshot of what survives, not a running total of
  everything ever allocated.

### Repos with no benchmarks

`init` refuses outright if it finds none — this tool optimizes what it can
measure, and does not guess. Add one:

```python
def test_my_function_benchmark(benchmark):
    result = benchmark(my_function, some_realistic_input)
    assert result  # a benchmark is still a test; keep it correct
```

Any test function (`test_*` or `*_test`, including inside a `Test*` class)
that takes the `benchmark` fixture, or carries `@pytest.mark.benchmark(...)`,
is discovered automatically. `pytest` and `pytest-benchmark` must be
installed in the environment that will run the benchmarks — `doctor` reports
FAIL if either is missing, since nothing can be measured without them.
`autor3search-python` itself is only needed there because `profile` loads it
as a plugin inside that same interpreter; `eval` does not need it, so
`doctor` reports a WARN naming `profile`, not a FAIL, when only that one is
missing.

One warning worth taking seriously: benchmarking a cold, rarely-exercised
path produces numbers that are entirely real and entirely useless — a 90%
speedup on a function that runs once at startup is not a win anyone will
notice. Point the first benchmark at whatever the profiler (or your own
judgment) says is actually hot.

## License

MIT. See [LICENSE](LICENSE).
