---
name: ci-cost-sweep
description: Measure where a repo's CI minutes actually go, then cut them without cutting coverage. Profiles billed minutes per workflow/job/event, profiles the test suites inside them, applies a catalogue of levers (test parallelism, dead caches, job merging, moving nice-to-have work off the per-push path), and proves the saving on real runs before claiming it. Use when CI costs too much, a build feels slow, or a test suite is the bottleneck.
user-invokable: true
args:
  - name: scope
    description: "'audit' (measure and report only, default), 'fix' (audit, then implement the levers on a branch and open a PR), or a single lever name to apply just that one"
    required: false
---

Find where a repo's CI minutes go, then cut them. Output is a measured before/after, not an opinion.

This file is the pipeline and it is stack-agnostic. Host-specific measurement lives in `adapters/` — read the adapter for the CI host before running anything. Test-runner specifics live in `adapters/test-runners.md`.

## Modes

The `scope` argument selects how far to go. Every mode runs Steps 0–4 first: you cannot pick a lever before you have measured.

| `scope` | Do |
|---|---|
| *(empty)* or `audit` | Steps 0–4, then **report** the ranked lever list with the measured evidence behind each. Change nothing. |
| `fix` | Steps 0–7 in full: audit, implement the levers Step 1 put in the dominant slice, on a branch, verified, PR opened. |
| a lever name from Step 5 — `parallel-tests`, `caches`, `merge-jobs`, `off-peak-work`, `path-filters`, `run-count` | Steps 0–4, then that one lever, then Steps 6–7. Use when the user has already chosen. |

Anything else: say you did not recognise it, list the accepted values, and stop. Do not guess — a misread scope that silently runs `fix` pushes commits nobody asked for.

## The one rule

**Measure. Never assume.** Every plausible-sounding CI optimisation in this document has been wrong in some real repo, including the obvious ones. A dependency cache that "obviously" saves time was measured at 71s per run *slower* than no cache. An estimate built on a plausible model was wrong by 2x, twice, in the same investigation.

You will be tempted to skip measurement because the change looks safe and the reasoning sounds tight. That is exactly when it is wrong. If you cannot measure a lever, say so and leave it alone.

## Hard constraints

- **Never reduce what is tested.** Every lever here preserves coverage. Deleting tests, narrowing a filter, dropping a browser/platform matrix leg, or lowering a timeout until flakes "pass" are not on this list. If the only remaining saving requires cutting coverage, stop and put the decision to the user with the trade-off stated.
- **Never push to the default branch. Never merge.** Work on a branch, open a PR.
- **Never claim a saving you did not measure on real runs.** "Should be faster" is not a result.
- **Report a range, not a single run.** CI run-to-run variance is routinely ±2 billed minutes. One post-change run is an anecdote.
- **Say what you did not do.** A lever you considered and rejected, with the reason, is part of the deliverable.
- **Scratch space is the system temp dir, never the repo.**

## Step 0 — Load context

Read `CLAUDE.md` (or `AGENTS.md`, `CONTRIBUTING.md`) at the repo root. It is the authority on that repo's conventions. A "saving" that violates one is worse than no saving — in particular, look for rules about which projects must be built or tested together, because those exist to catch cross-project breakage and are easy to defeat accidentally by splitting or filtering jobs.

Identify the CI host and read the matching adapter. `adapters/github-actions.md` exists today.

## Step 1 — Measure the bill

Get **billed** minutes per job, not wall-clock per run. They differ, and the difference is a lever in itself.

Two facts that drive everything downstream:

- **Most hosts bill each job rounded UP to a whole minute.** A 3-second job costs a full minute. Job *count* is therefore a cost driver independent of job *duration*, and merging two small jobs can save more than making either faster.
- **Parallel jobs bill in parallel.** A run whose wall-clock is 12 minutes across 5 concurrent jobs bills ~30. Never report wall-clock as cost.

Produce a table over a window of at least ~2 weeks or ~100 runs:

| workflow | event | job | runs | billed min | share |
|---|---|---|---|---|---|

Then stop and look at it before touching anything. Usually one workflow × one event is most of the bill, and everything else is rounding. Optimising outside that slice is wasted work.

Also record, because they change which lever applies:

- **Runs per unit of work.** Runs ÷ pull requests tells you whether the problem is per-run cost or run count. They have completely different fixes.
- **Cache storage vs the host's cap.** Over the cap, the host evicts, so caches that look like hits in the config are misses in practice.
- **Conclusion mix.** A high `cancelled` count means concurrency cancellation is already working.

  A high `failure` count is worth noting, but **it is not rerun cost**. Failed runs prove minutes spent for no green signal; they do not prove anyone re-ran them. If rerun cost is what you want, measure it: count runs whose `run_attempt` is above 1, or group runs by head SHA and look for repeats. Report whichever you actually measured, and say which one it was.

## Step 2 — Establish an honest baseline

This is where the analysis usually goes wrong.

- **Use recent, like-for-like runs.** Compare only runs where the same job set succeeded. A long window averages over changes in the code itself — in one real case a 220-run average and a 24-most-recent average disagreed by 27% on the same job, because the test suite had got slower over the window. The recent comparable set is the honest baseline.
- **Exclude jobs whose behaviour differs between the two sides.** If your change touches a file that a path-filter watches, that filtered job runs fully on your branch and mostly short-circuits on the baseline. It is not comparable; exclude it from both sides and say you did.
- **Record variance.** Take more than one post-change run when the decision is worth it.

## Step 3 — Profile the test suites

Usually the largest single step. Do not parallelise on instinct — find out *what the time is made of* first.

1. **Get per-test timings.** Every runner can emit them (`adapters/test-runners.md`). Parse into per-class/per-file totals and a slowest-individual-test list.
2. **Compare the sum of test durations to the step's wall-clock.** The gap is fixture/setup/build overhead living outside the tests.
3. **Classify the time.** This determines the lever:

   - **Blocked** — process/app boots, `sleep`/`delay`, real backoff/retry policies, network waits, container starts. Parallelises extremely well, and you should oversubscribe workers *past* the core count because a blocked worker is not using a core.
   - **CPU** — actual computation. Parallelises up to the core count and no further.
   - **Serialised by a single hot unit** — one class/file that dominates. Parallelism floors out at that unit's duration if the runner keeps tests within a unit sequential. Splitting that unit is then the only way lower.

4. **Look for the usual suspects**: a test-runner that defaults to serial (many do), per-class fixture setup that boots an entire application, and tests that deliberately sleep to exercise timeouts or retry policies.

## Step 4 — Check parallel-safety before enabling it

Enabling parallelism on an unsafe suite buys you flakes, which cost more than they save. Check, and report what you checked:

- **Shared mutable state** between the units that will now run concurrently — `static`/module-level/global mutable fields, singletons, class-level caches. Pure helpers are fine.
- **Fixed ports.** Anything binding a literal port breaks. It must ask the OS for one (port `0` / an ephemeral binding).
- **Shared external resources** — one database, one temp file path, one output directory, one fake-clock, environment variables set at runtime.
- **Ordering assumptions** — tests that depend on another test having run.

Then **prove it empirically**: run the suite several times consecutively and require every run green. A single green run does not clear a race. Four consecutive is a reasonable bar for a suite of ~1,000 tests.

If a shared resource blocks it, prefer giving each worker its own instance (its own in-memory DB, its own temp dir) over serialising the tests that touch it.

## Step 5 — Work the lever catalogue

Ranked by what typically pays. Apply only what Step 1 says is in the dominant slice.

### 5a. Parallelise the test suites — usually the biggest single win

Per Steps 3–4. Set the worker count from what Step 3 found: at the core count for CPU-bound work, above it for blocked work. Measure both settings on the CI host, not locally — **core counts differ, and a setting that reads slightly worse on a 16-core dev machine can be clearly better on a 2- or 4-core runner.** A local suite already at its floor cannot show you a CI gain.

### 5b. Audit every cache — they are not free, and some are net-negative

A cache costs a restore and a save on every run. It pays only if that cost is less than what it avoids. For big dependency trees on hosts with fast package-registry access, it frequently is not.

**How to actually settle it**, because run history usually cannot:

The comparison you need is "cache hit" vs "cold restore doing real work". If the cache hit rate is high, there may be **no historical runs of the second kind** — the only misses were jobs that short-circuited and did no work anyway. You cannot A/B what never happened.

So *create* the arm: add a temporary boolean dispatch input that skips the cache steps, run it **at least twice** against a warm-cache control run on the same commit, and compare step-by-step. Then delete the input along with whatever you decided.

Judge each cache separately — in one real repo the dependency cache was a 71s/run loss and the browser-binary cache in the same workflow was a clear win (268 MB restoring in 5–7s against a 20–38s reinstall). "Caching is good" is not a finding; per-cache numbers are.

Also check **total cache size against the host's cap**. Over it, the host evicts least-recently-used entries, so caches thrash and you pay the save cost repeatedly for nothing. And check whether several jobs **share one key while storing different content** — whichever job finishes first decides what every other job downloads, which is both slower and non-deterministic. The tell is one key present at two different sizes, but the tell is not proof: a cache entry is identified by key **plus ref scope plus cache version**, so the same key at two sizes is entirely legitimate across two branches. Compare the entries' refs before concluding anything — the host adapter says how.

### 5c. Merge jobs that do too little to justify their overhead

Every job pays checkout + toolchain setup + dependency restore + the per-job billing round-up. A job doing well under a minute of real work can easily cost 3–4× that.

Merge it into a job it shares a toolchain with. **Order the merged steps fastest-first**, so a quick failure still reports quickly instead of queueing behind a long suite — you are trading a little parallelism for cost, and step order buys most of the feedback speed back.

Do not merge jobs with genuinely different runner requirements, or where the parallelism is load-bearing for feedback time on the critical path.

### 5d. Move nice-to-have work off the per-push path

Screenshot galleries, demo recordings, doc generation, preview builds, benchmark charts. These re-run work the real tests already validated, on every push, to produce something read once if at all.

Move to a `schedule` plus an on-demand trigger (a dispatch input and/or a label read on every event). Keep the artefact; change the cadence.

Watch for these when you do:

- **Event-guard fallout.** Jobs guarded on `pull_request` context skip on a scheduled event — sometimes that is exactly what you want (only the one job you need runs), but check rather than discover it. State it in a comment either way.
- **Steps that assume a PR exists.** Anything posting a comment or keyed on a PR number needs a non-PR path, or must be skipped.
- **Destination scoping.** If the output is published somewhere, a manual run from any branch must not overwrite the canonical copy. Scope the destination by trigger and derive any label from the *same* variable so the two cannot drift.
- **Empty output.** If the producing step is `continue-on-error`, the publish step also runs when it failed. Stage output and count it before replacing anything, or a failed run deletes a good artefact and replaces it with nothing.

### 5e. Path filters and ignore lists

Skip jobs that cannot be affected by the changed files. Cheap and safe, but bounded: it only helps if a meaningful share of changes miss the filter, so check the actual distribution before writing one.

Two traps:
- **An allowlist is a whitelist.** Add a new input the build reads, and you must add its path, or changes to it silently never trigger.
- **Filters that watch the workflow file itself** make every CI-tuning PR run everything — which is correct, but remember it when reading your own before/after numbers (see Step 2).

### 5f. Reduce run count, not just run cost

If Step 1 shows many runs per PR, per-run cost is the wrong target. Check concurrency cancellation is on (`cancel-in-progress` or equivalent). Consider whether pushes can be batched. **Verify before gating on draft PRs** — in one repo 119 of the last 120 PRs were never drafts, making that popular suggestion worth exactly nothing.

## Step 6 — Verify

Re-run CI and measure the same way as Step 1. Compare against the Step 2 baseline, like-for-like.

- Confirm every job still **passes**, not just that it is faster.
- Confirm each behavioural change did what you intended, in **both** directions — a step you gated off should be observed `skipped` when it should skip *and* observed running when it should run.
- If a number moved the wrong way, say so and explain it rather than quietly reporting the good ones.

## Step 7 — Report

Structure:

1. **Where the minutes went** — the Step 1 table.
2. **What changed** — one entry per lever, each with its own measured delta.
3. **The result** — before/after as a range, with n, and the monthly extrapolation stated as an extrapolation.
4. **What you did not do** — levers rejected and why; anything needing a human decision.
5. **Anything left on the table** — the next biggest cost and what attacking it would require.

Correct your own earlier estimates explicitly if the measurements contradicted them. An estimate that was wrong and got fixed is a better artefact than a number that quietly changed.

## Shell gotchas that cost real time

- **Match the user's shell.** On Windows/PowerShell, `xargs`, `grep`, and `sed` pipelines do not exist. Give PowerShell, not bash, when the user is the one running it.
- **PowerShell 5.1 `ConvertFrom-Json` fails on multi-line CLI output** piped directly, with a confusing `op_Division` error. Pipe through `Out-String` first.
- **Git Bash mangles API paths.** A leading `/` in an argument gets rewritten to a Windows path (`gh api /repos/...` becomes `C:/Program Files/repos/...`). Drop the leading slash, or set `MSYS_NO_PATHCONV=1` for `git show <rev>:<path>`.
- **CRLF repos.** Read, normalise to `\n`, patch, write back with the original line endings, or you get a whole-file diff. Check `git diff --numstat` afterwards.
- **Assert before you write.** When patching config by string replacement, assert the match count first. A silently-failed replacement in a file you then report as fixed is the worst outcome here.
