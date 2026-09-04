# Adapter — GitHub Actions

Measurement recipes and host-specific levers for `ci-cost-sweep` on GitHub Actions.

## Billing model

- **Every job is billed rounded UP to a whole minute.** A 3-second job costs 1 minute.
- **Concurrent jobs each bill in full.** Run cost = sum over jobs, not wall-clock.
- Public repos are free; **private repos bill every minute**. Check first — it decides whether this is worth doing at all:

```bash
gh api repos/<owner>/<repo> --jq '{private,visibility}'
```

- **Rates depend on the runner SKU, not just the OS.** For *standard* GitHub-hosted runners the familiar multipliers apply — Linux 1×, Windows 2×, macOS 10× — so a macOS job is worth ten Linux ones and the runner mix is worth checking before assuming the Linux jobs are the problem. Larger runners bill at their own per-minute rate, which those multipliers do not describe. **Keep `runner_name`/labels in your aggregate** so you can apply the right rate per job instead of assuming one.
- **Included minutes come off the top.** A plan's included allowance is spent before anything is charged, so "minutes used" and "money" are not the same curve. Rank jobs by minutes; talk about cost only once you know which side of the allowance the account is on.

## Getting billed minutes — the endpoint that looks right but is not

`/actions/runs/<id>/timing` returns `billable.UBUNTU.total_ms` and it is **frequently 0**, including on private repos where minutes genuinely are billed. Do not build on it, and do not conclude "no minutes used" from it.

Sum the jobs API instead:

```bash
# 1. run list
gh run list --limit 400 --json workflowName,event,conclusion,createdAt,updatedAt,databaseId > runs.json

# 2. jobs per run — --paginate, because a matrix run can exceed one page and a
#    truncated job list silently understates that run's cost.
#    NO leading slash on the path: Git Bash rewrites it to a Windows path.
gh api --paginate "repos/<owner>/<repo>/actions/runs/<id>/jobs?per_page=100" --jq '.jobs[]'
```

`--paginate` with `--jq` emits one object per line across pages; without the `--jq` you get one JSON envelope per page and have to flatten `.jobs` yourself.

Billed minutes for a job = `ceil((completed_at - started_at) / 60)`.

**Guard for `completed_at: null` before applying that.** Queued and in-progress jobs have no completion time, and a run sampled while still going will contain them — the formula then throws or silently yields nonsense. Skip those jobs and note how many you skipped. (Cancelled jobs *are* completed and do have the field; they still cost minutes, so keep them.)

Aggregate by `(workflowName, event, job.name)`. Keep `event` in the key — `pull_request` and `push` runs of the same workflow have very different costs and different fixes.

### Step-level timings

`jobs[].steps[]` carries `started_at`/`completed_at` per step. This is where the real analysis happens.

**Post-steps matter.** A cache's save cost appears as a separate step named `Post <step name>` (or `Post Run actions/cache@v4`). Counting only the restore step understates a cache's cost.

Attribute by **identity, not by prefix**. `^Post ` also matches the post hooks of `setup-node`, `setup-dotnet`, `checkout` and anything else with a cleanup phase, and folding those into "cache cost" inflates it. A post step is named `Post ` + the originating step's name (or its `uses:` when the step is unnamed), so pair each post step with the cache step whose name it echoes, and ignore post steps that match no cache step.

**Did it download anything?** For *cost* purposes that is the whole question, and the restore step's duration answers it: ~0–2s means a lookup with no download, anything longer means bytes moved. Two caveats:

- Duration does **not** tell you *which* key matched. An exact hit on `key` and a partial hit via `restore-keys` both download. If you need that distinction — you usually do not for cost, but you do when diagnosing why a cache never hits exactly — read the step's `cache-hit` output (true only for an exact primary-key match) and `cache-matched-key`, or the restore log line naming the key it used.
- A job that skipped its real work via a path-filter also shows ~0s and is **not** a usable data point. Exclude jobs whose main steps did nothing.

## Cache inspection

```bash
gh api repos/<owner>/<repo>/actions/cache/usage --jq '{active_caches_count,active_caches_size_in_bytes}'
# --limit caps the result set; raise it or the totals understate a busy repo.
# Include `ref` — you need it to tell a real key collision from two legitimate
# branch-scoped entries (see below).
gh cache list --repo <owner>/<repo> --limit 1000 --json id,key,ref,sizeInBytes,createdAt
```

- **10 GB per repo is the *default* cap, not a universal one.** Enterprise/org owners and repo admins can raise it (the extra is billed). Read the actual usage and behaviour rather than assuming the number. Whatever the cap is, going over it makes GitHub evict least-recently-used entries, so configured caches miss in practice.
- **Caches are scoped per branch**, with fallback to the default branch. A PR branch reads the base branch's cache but saves its own, so N active branches multiply storage.
- **The same key at two different sizes** *may* mean two jobs write one key with different content — whichever finishes first then decides what all the others download. Confirm before acting: a cache entry is identified by key **plus ref scope plus cache version**, so two same-key entries at different sizes are perfectly legitimate when they belong to different branches, or were written by different runner OS/compression versions. Compare the `ref` on each entry first. A real collision is same key, same ref, different content — and the fix is splitting the key prefix, or deleting the cache entirely if Step 5b says it does not pay.
- **Deleting: use the id, not the key.** Keys are not unique (branch scoping), so `gh cache delete <key>` is ambiguous.

## Common levers, GitHub-specific

**Job merging** — remove one `- uses: actions/checkout@v4` + toolchain setup + the round-up. A gate job that runs in 3 seconds still costs a full minute *and* delays everything behind its `needs:` edge; prefer repeating the guard in each job's `if:` over a `needs:` gate job.

**Path filtering** — `paths-ignore` at the workflow level skips only when *every* changed file matches. For per-job filtering use `dorny/paths-filter`, which reads the API for `pull_request` events and needs no git history. Note it has no diff on `workflow_dispatch`, so guard for that.

**Concurrency** — `concurrency: {group: <name>-${{ github.ref }}, cancel-in-progress: true}` kills superseded runs. Check it exists before looking for cleverer savings.

**A temporary experiment switch** (Step 5b) is a `workflow_dispatch` boolean input plus `if: github.event.inputs.<name> != 'true'` on the steps under test. On `pull_request` events `github.event.inputs` is null and `null != 'true'` is true, so the steps stay on by default — no separate condition needed.

**Watch out:** dispatching twice on the same ref while `cancel-in-progress` is on means **the second run cancels the first**. Run experiment dispatches sequentially, and do not push to the branch while one is in flight.

**Scheduled events and job guards.** On a `schedule` event `github.event.pull_request` is null, so a fork guard like `github.event.pull_request.head.repo.full_name == github.repository` is false and the job skips. This is useful — it lets a scheduled run execute only the one job that opted in via `github.event_name == 'schedule'` — but it is implicit. Comment it where it matters.

## Diagnosing a slow job's shape

Ranked list of the slowest steps across recent runs of one job, which is where the lever usually is:

```bash
gh api --paginate "repos/<owner>/<repo>/actions/runs/<id>/jobs?per_page=100" \
  --jq '.jobs[] | .name as $j | .steps[] | "\($j)|\(.name)|\(.started_at)|\(.completed_at)"'
```

Bucket by step name across ~20–30 runs and take the mean. Single runs are too noisy to rank steps by.
