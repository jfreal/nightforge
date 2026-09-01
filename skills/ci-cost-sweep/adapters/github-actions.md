# Adapter — GitHub Actions

Measurement recipes and host-specific levers for `ci-cost-sweep` on GitHub Actions.

## Billing model

- **Every job is billed rounded UP to a whole minute.** A 3-second job costs 1 minute.
- **Concurrent jobs each bill in full.** Run cost = sum over jobs, not wall-clock.
- Public repos are free; **private repos bill every minute**. Check first — it decides whether this is worth doing at all:

```bash
gh api repos/<owner>/<repo> --jq '{private,visibility}'
```

- Linux is 1×, Windows 2×, macOS 10×. A macOS job is worth ten Linux jobs — check the runner mix before assuming Linux jobs are the problem.

## Getting billed minutes — the endpoint that looks right but is not

`/actions/runs/<id>/timing` returns `billable.UBUNTU.total_ms` and it is **frequently 0**, including on private repos where minutes genuinely are billed. Do not build on it, and do not conclude "no minutes used" from it.

Sum the jobs API instead:

```bash
# 1. run list
gh run list --limit 400 --json workflowName,event,conclusion,createdAt,updatedAt,databaseId > runs.json

# 2. jobs per run (note: NO leading slash — Git Bash rewrites it to a Windows path)
gh api "repos/<owner>/<repo>/actions/runs/<id>/jobs?per_page=100"
```

Billed minutes for a job = `ceil((completed_at - started_at) / 60)`.

Aggregate by `(workflowName, event, job.name)`. Keep `event` in the key — `pull_request` and `push` runs of the same workflow have very different costs and different fixes.

### Step-level timings

`jobs[].steps[]` carries `started_at`/`completed_at` per step. This is where the real analysis happens.

**Post-steps matter.** A cache's save cost appears as a separate step named `Post <step name>` (or `Post Run actions/cache@v4`). Counting only the restore step understates a cache's cost. Match `^Post ` case-insensitively and attribute it to the same cache.

**Cache hit vs miss** is readable from the restore step's duration: a miss is ~0–2s (lookup, no download), a hit is however long the download takes. Beware: a job that skipped its real work via a path-filter also shows ~0s, and is not a usable data point.

## Cache inspection

```bash
gh api repos/<owner>/<repo>/actions/cache/usage --jq '{active_caches_count,active_caches_size_in_bytes}'
gh cache list --repo <owner>/<repo> --limit 100 --json id,key,sizeInBytes
```

- **The cap is 10 GB per repo.** Over it, GitHub evicts least-recently-used entries, so configured caches miss in practice.
- **Caches are scoped per branch**, with fallback to the default branch. A PR branch reads the base branch's cache but saves its own, so N active branches multiply storage.
- **The same key at two different sizes** means two jobs write one key with different content — whichever finishes first decides what all the others download. Fix by splitting the key prefix, or by deleting the cache if Step 5b says it does not pay.
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
gh api "repos/<owner>/<repo>/actions/runs/<id>/jobs?per_page=100" \
  --jq '.jobs[] | .name as $j | .steps[] | "\($j)|\(.name)|\(.started_at)|\(.completed_at)"'
```

Bucket by step name across ~20–30 runs and take the mean. Single runs are too noisy to rank steps by.
