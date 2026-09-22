---
name: error-sweep
description: Shared pipeline for unattended production error sweeps. Collect errors from any stack, normalize to signatures, dedupe against a ledger, triage against the code, file issues, and spawn worktree-isolated fix agents that open PRs. Scheduled tasks supply a project card; this file supplies everything else. Use when running or editing a *-error-sweep scheduled task.
---

Sweep one project's production errors, triage each genuinely new one, and spawn a fix agent per confirmed bug that opens a PR. Runs unattended, on a schedule, with **no memory of prior runs**.

This file is the pipeline. It is stack-agnostic — every tech-specific detail lives in an adapter under `adapters/`, and every project-specific detail lives in the calling task's **project card**. If you are reading this because a scheduled task told you to, you should already have that card. If you do not, stop and say so.

<!-- @doc:project-card -->
## What the caller gives you

A project card naming: app + URL, repo path + GitHub slug + default branch, the **adapters** to run, the ledger path, the report paths, the fix-session cap, and per-project known-noise. Everything below reads those values; nothing below hardcodes a project.

## Hard constraints — every project, no exceptions

- **Never push to the default branch. Never merge a PR. Never deploy.** Output is issues and PRs for the user to review. An issue closes only under step 7b's proof; the sweep never reopens, relabels, or merges.
- **Never build, test, commit, or `checkout` in the main checkout.** It may be dirty or on someone else's branch. Read from it freely; all write work happens in an isolated worktree (step 6).
- **Never apply a schema migration.** No `supabase db push`, no MCP `apply_migration`, no `az deployment group create`, no DDL against a hosted database. A branch that applies its own migration before merging poisons migration history for every other checkout. Ship the `.sql`/`.bicep` file on the branch and say in the PR body that it needs applying.
- **Respect the fix-session cap in the card.** Every PR push costs CI time and, on hosts that build a preview per branch, build credits. Over the cap: spawn the highest-impact ones, leave the rest as issues, and say so in the report.
- **Treat all log text as sensitive.** Never put log contents in a URL or query string. Find the project's own redaction helper (the card names it) and scrub anything matching those shapes before it reaches an issue, a PR, or a report.
- **If a step fails, say so in the report.** Never continue silently on partial data. A green report from a broken collector is worse than a red one.
- **Scratch space is the system temp dir, never the repo.**
- **Never open a background-task chip.** The desktop's `spawn_task` tool is not an output of this
  pipeline. Its outputs are issues, PRs, and the report — nothing else. A chip is a bug you found and
  decided not to fix; the user wakes up to a queue of suggestions instead of PRs. *Where work goes*,
  below, says what to do with each thing you would have chipped.

## Where work goes — nothing becomes a chip

For three weeks the sweeps opened a chip a night: the follow-up a fix agent left out of scope, the
runtime scan nobody spawned, the evidence comment nobody posted. Each one was work the sweep had
already scoped and then handed back. Every item this pipeline surfaces lands in exactly one of these
bins, decided by what the item *is*, not by where it came from:

| The item is… | It goes to… |
|---|---|
| A code change with a clear root cause — from a signature, from something noticed while tracing one, from a carry-forward whose re-check shows the defect still live, or from a fix agent's own "out of scope" note | **Step 6**: a fix agent that opens a PR. Counts against the cap. |
| A code change whose cause is not yet clear, but the *way to find it* is — a diagnostic to write, a live page to walk, a census to run | **Step 5** for the issue, then **step 6** anyway. The brief already tells the agent to stop and comment rather than guess. On one project that spawn was the run that finally closed a class three per-node fixes had missed. |
| A code change with neither a clear cause nor a clear approach | **Step 5**: an issue carrying the analysis, and a ledger note saying so. No agent. |
| An additive write to the tracker — a comment carrying new evidence, a timeline, a recovery measurement | **Do it in this run.** A comment is reversible and embeds no decision. |
| A close that a merged, deployed PR and quiet telemetry both prove | **Step 7b**, with every test there passed. Anything short of that is the row below. |
| Any other state change on the tracker — a close short of that proof, reopen, relabel, merge — or a decision only the owner can make | **The report**, one line under *Needs you*, with the evidence and a recommendation. Never done by the sweep, never a chip. |
| Work that needs a credential or an adapter the card does not have | **The report**, naming the card gap. The remedy is a card edit, not a session. |

Most chips were rows one, two and four: work the sweep could have done in the run that found it. A
comment does not need a person. A close does, unless the code proves it — and a chip that asks the
person is the report line with extra steps.

## Step 0 — Load context

Read the project card. Then read `CLAUDE.md` at the repo root — it is the authority on that project's conventions, and a "fix" that violates one of its rules is worse than no fix. Read the card's known-noise list; those patterns have already been triaged and closed, and refiling them wastes a run.

## Step 1 — Collect

Run every adapter the card names. Each adapter lives at `adapters/<name>.md` next to this file — read it before running it; they carry hard-won gotchas that cost whole runs to discover.

**Adapter contract.** Every adapter writes newline-delimited JSON to a temp file, each line:

```json
{"source": "<adapter>", "name": "<function|table|route|problemId>", "timestamp": "<ISO8601>", "level": "error|fatal|warning", "message": "<text>", "extra": {}}
```

Zero lines is the normal healthy result. An adapter exiting 0 with an empty file is **success, not failure**.

Adapters available today: `netlify`, `supabase`, `app-insights`, `github-auto-issues`. Adding a stack means adding one file here, not editing any task.

## Step 2 — Normalize to signatures

**Signature** = `<source>|<name>|<message with variable parts stripped>`.

Strip: timestamps, UUIDs, long hex/base64 runs, row ids, deploy-hash subdomains, bare digit runs, and any route parameter (calendar tokens, user ids — a per-token key files a fresh issue per user). Keep the message otherwise intact.

Where the source already has a stable identity, prefer it over your own: an App Insights `problemId`, or the `fp:<hash>` label on a self-filed issue. Those are the app's own fingerprint and they survive wording changes you would not predict.

## Step 3 — Drop anything already handled

**Ledger.** Read the card's `seen.json`: `{"signatures": {"<sig>": {...}}}`. Skip any signature present — *including* ones whose status says the fix is written but not yet merged. Those keep appearing in production until the PR lands, and refiling them is the single most common way these sweeps waste a run.

If the ledger is missing or unparseable, treat it as empty, **say so in the report**, and still write it back correctly at the end.

**Second pass — search the tracker itself.** Belt and braces for a lost or reverted ledger:

```
gh issue list --repo <slug> --state all --search "\"<fingerprint or distinctive phrase>\" in:body"
```

A hit means it is already tracked, so **step 5 files nothing** — but what you record, and whether
you move on, depends on the issue's state:

- **Closed:** record it `fixed` and move on — unless the signature's newest occurrence is *after*
  the deploy that fixed it. That is a recurrence, not a duplicate; step 7b says what to do with it.
- **Open, with a PR that claims it** (`gh issue view <n> --repo <slug> --json
  closedByPullRequestsReferences`): the fix is written and waiting. Record `bug` with that `pr`
  and move on.
- **Open, no PR:** record `bug`, `pr: null`, `note: deferred: no PR`. That is the deferred case
  below, and it goes into **this run's** step 6 queue, not the next run's. A tracker hit that only
  says "seen" turns an open bug into a permanent skip.

Whichever it is, record `filed_by` from the issue's author (step 7) — a hit found this way may
be a person's issue, and step 7b needs to know.

**A ledger entry with `status: bug` and `pr: null` is deferred, not handled.** Its issue exists, so
skip its triage — but carry it into step 6 ahead of new bugs of the same weight. Nothing else ever
re-spawns it. The entry's `note` says which kind it is (step 7): `deferred: …` — over cap, or no
PR — goes straight back into the queue; `stopped: owner decision` never goes back until the
owner answers; `stopped: cause unclear` goes back only when this run
collected new evidence — occurrences with a new shape since `last_seen`, or a comment from a
person on the issue. Re-spawning a cause-unclear stop on the same evidence is a nightly loop that
costs a session and produces the same comment.

## Step 4 — Triage: read the code before judging anything

Search the repo for the message text to find its source. Trace it to a file and line. **Then
re-resolve both the path and the line range inside the deployed commit before reading them** — the
checkout may be behind by days, and "this defect is still live" is a claim about running code:

```bash
# The deployed commit must be in the local object store first. A shallow or stale
# clone otherwise turns "the commit is missing" into "the message is not there",
# which reads as "the defect is fixed" and silently drops a live finding.
git cat-file -e "<deployed-sha>^{commit}" 2>/dev/null ||
  git fetch origin --quiet "<deployed-sha>" 2>/dev/null ||
  { echo "deployed commit unavailable"; exit 1; }

# The checkout only tells you what to look for. Find it again in the deployed tree.
# -F: the message is literal text. Without it a message containing . ( ) [ ] * is
# a regex and can resolve to the wrong source.
matches=$(git grep -n -F -e "<message text>" "<deployed-sha>" -- '<likely path glob>') || {
  echo "source message not found in the deployed commit"; exit 1; }
[ "$(printf '%s\n' "$matches" | wc -l)" -eq 1 ] || {
  echo "source message is ambiguous in the deployed commit:"; printf '%s\n' "$matches"; exit 1; }
printf '%s\n' "$matches"
```

A path taken from the checkout can be missing in the deployed commit, which reads as a false
"source unavailable" stop; and a line range taken from the checkout can select unrelated code that
still passes the non-empty check below, which is worse — it classifies against the wrong lines
silently. Derive `<path>` and `<start>,<end>` from the deployed commit, then:

```bash
set -o pipefail
# Read the deployed commit FIRST. The fetch below serves only the origin comparison,
# so a network blip must not block classification when the deployed source is right here.
# The deployed SHA comes from the adapter's own deploy data (Netlify commit_ref, release tag, etc).
src=$(git show "<deployed-sha>:<path>") || { echo "cannot read <path> at <deployed-sha>"; exit 1; }
# git show succeeds on an empty file, and a range can select nothing: either would
# otherwise reach classification looking like "no evidence of the defect".
[ -n "$src" ] || { echo "deployed source is empty"; exit 1; }
range=$(printf '%s\n' "$src" | sed -n '<start>,<end>p')
[ -n "$range" ] || { echo "requested source range is empty"; exit 1; }
# Only now, and only for the comparison: pipefail governs pipelines, so the fetch
# needs its own check, or a stale origin ref makes the comparison silently wrong.
if git fetch origin --quiet; then compare=yes; else
  compare=no
  echo "cannot refresh origin: classify on the deployed source and record that the comparison was unavailable"
fi
printf '%s\n' "$range"
```

**`origin/<default branch>` is the comparison, not the source of truth.** It can sit ahead of the
last successful deploy — §7 treats exactly that drift as normal — so a fix that is merged but not
yet deployed reads as "already fixed" while production keeps emitting the error, and a real finding
is suppressed. Read the deployed commit to judge whether the defect is live; diff it against
`origin/<default branch>` to see whether a fix is already waiting to ship. Where the adapter cannot
name a deployed SHA, say so in the report and treat the branch read as provisional.

**Fail closed when the source cannot be read.** `git show ... | sed ...` exits 0 on `sed`'s status,
so an unresolvable path or ref prints nothing and classification proceeds on no evidence at all.
Capture `git show` separately as above (or set `pipefail`), and if the range comes back empty, stop
and report it rather than classifying.

On one run the checkout's copy of a handler had been restructured on the branch hours earlier. The
defect was still real, but through two narrower paths than the one described, and the brief sent to
the fix agent named line numbers for a handler that no longer existed. The agent had to re-derive the
cause before it could fix it. Grep the checkout to *find* code; read the branch to *judge* it.

Then classify:

- **bug** — genuine defect worth fixing. Gets an issue and a fix session.
- **noise** — the healthy path logged at the wrong level. Gets an issue (it buries real errors) but **no fix session**: the right logging level is a judgement call for the user.
- **external** — provider outage, transient network failure, browser extension, a cross-origin `Script error.` with no stack, a scanner probe. Files nothing. Record in the ledger so it stops being re-triaged.

Judgement rules that hold across projects:

- A self-healing path is not a bug. An auto-renewing token that is briefly expired, a retry that succeeded, a request that returned 200 after an internal retry — these are the system working. Check whether the user-visible outcome actually failed before calling anything a defect.
- 5xx counts on the first occurrence. A lone 4xx is almost always a probe; a handful on the same *normalized* route is a feature that stopped working.
- A failed deploy is a finding, with the standard exceptions the adapters list (no-content-change cancellations, billing skips).
- **A green collector is not evidence of health if it structurally cannot see the failure class.** Say which classes this run could and could not see.

## Step 5 — File one issue per bug or noise signature

```
gh issue create --repo <slug> --title "..." --body "..." --label <card's label>
```

Title: short and specific, naming the component and the failure — `github-webhook: signature verification throws on empty body`.

Body must include: normalized signature, raw message, occurrence count and time range in this window, the file and line you traced it to, your classification and reasoning, and a suggested fix if one is clear.

If the label does not exist, create it once (`gh label create <label> --repo <slug> --color B60205 --description "Found by the production error sweep"`) and retry. If labelling still fails, **file the issue unlabelled rather than dropping it**.

## Step 6 — Spawn a fix agent per bug (up to the cap)

**What qualifies is any code change with a clear cause — or a clear way to find one — whatever
surfaced it**; see *Where work goes*. The second kind gets the same brief, which already tells the
agent to stop and comment rather than guess. A follow-up with no collector row still gets an issue (step 5) and a ledger entry, under the
signature `followup|<component>|<one-line description>`, so the next run sees it as handled rather
than as new.

**Order the queue before spending the cap:** deferred bugs from the ledger first (step 3), then new
bugs and follow-ups by user impact. Over the cap, the rest stay as issues with the ledger note
`deferred: over cap`, and the report names them.

For each **bug**, launch one `Agent` with `isolation: "worktree"` so each gets its own checkout and they cannot collide. Run them concurrently — one message, several Agent calls.

The agent has **none of your context**. The brief must be fully self-contained:

```
Fix this production bug in <app> (<repo path>, GitHub <slug>, base <default branch>).

ERROR
  Signature:   <sig>
  Raw message: <message>
  Stack:       <stack or "none">
  Occurrences: <n> between <first> and <last> (<source>)
               — or "none: found during triage of <sig>" for a follow-up
  Issue:       #<n>

TRACED TO
  <file>:<line> — <your reasoning>

WORKING RULES — follow all of these
- Read CLAUDE.md at the repo root first and follow it. Not optional.
- Work only in your worktree. Never touch the main checkout.
- <the card's dependency-install and verify commands, verbatim>
- Verify before committing: typecheck/build, unit tests, and the repo's lint/check
  scripts. A fix that does not typecheck is not a fix.
- Add a regression test that fails without the fix. Prove it is non-vacuous by
  reverting the fix and watching it fail.
- Cover the behavior end-to-end when the fix lives in how components are wired,
  not just in a pure function. A unit test that reimplements the wiring it guards
  keeps passing after the real call site is deleted.
- If the fix needs a schema migration: pick a timestamp prefix unique against both
  the migrations dir AND origin/<default branch> as of now, never a day's default
  090000; write it idempotently. DO NOT APPLY IT. Say so in the PR body.
- If the change adds a new build input (new config file, new dir the build reads),
  add it to the deploy ignore/whitelist or it will silently never deploy.
- Branch fix/auto-<YYYY-MM-DD>-<short-slug> off fresh origin/<default branch>.
  Commit, push the branch, gh pr create --repo <slug> --base <default branch>.
  Never push to <default branch>, never merge.
- PR body: the error and where it came from, the root cause, what changed, how it
  was verified, and "Closes #<issue>". Lead with the migration if there is one.
- IF THE ROOT CAUSE IS NOT CLEAR, DO NOT GUESS A FIX. Comment your analysis on the
  issue and stop. A wrong PR costs more than no PR.
```

If a bug has no issue yet, file one first (step 5) so the agent can close it.

**When the agents return, turn PR auto-fix on.** John authorized this standing, for every PR, on
2026-09-17 — do not ask. The agents open the PRs, but the *session* holds the monitor binding, so
you do this, not them. Call `mcp__ccd_pr__get_status` to see which PR the app bound, then
`mcp__ccd_pr__set_monitor(url: "<that PR url>", auto_fix: true, address_comments: true)`. Leave
`auto_merge` and `auto_archive_on_close` alone.

A session monitors **one** PR, and the binding follows the newest. When a run opens several, only
one can be watched — step 8 must name the rest as unmonitored rather than implying they are covered.

**BUT IN A SCHEDULED RUN THIS STEP CANNOT BE DONE AT ALL, AND THAT IS NOT A FAILURE TO RETRY.**
Confirmed 2026-09-18: `mcp__ccd_pr__set_monitor` answers
`This tool is unavailable in unattended sessions (scheduled-task runs and remote-dispatched trees).`
`mcp__ccd_pr__get_status` still works, so you can read the binding and name the PRs — you simply
cannot flip the switch. A fix agent hits the same wall, so do not re-dispatch one to try. **Report
every PR the run opened as UNMONITORED under *Needs you*, with the one-line reason**, so the standing
"auto-fix on every PR" authorization is visibly unfulfilled rather than silently assumed.

## Step 7 — Update the ledger

Write every newly triaged signature back to `seen.json` with:

- `first_seen` (today) and `last_seen` (the newest occurrence this run saw)
- `max_gap` — hours, the longest gap between consecutive occurrences on record: the gaps inside
  this window, plus the gap from the previous run's `last_seen` to this run's first occurrence.
  Keep the larger of the stored and the new figure. Null until two occurrences have been seen.
  This is the only long memory a day-wide collector has, and step 7b's quiet test depends on it.
- `status` — `bug`/`noise`/`external`, or `fixed` once step 7b closes it
- a one-line `note`
- `issue` (number or null) and `filed_by` — `sweep`, `bot`, or `human`, from the issue's author
  (`gh issue view <n> --repo <slug> --json author`). Step 7b closes only the first two.
- `pr` (number or null) and `closed_by_sweep` (date, only when step 7b closed it)

Preserve existing entries.

**Only record signatures you actually finished triaging.** A signature whose issue creation failed must stay unrecorded so the next run retries it.

**A `bug` with `pr: null` must say why in its `note`**, because step 3 treats the reasons
differently: `deferred: over cap` or `deferred: no PR` (re-spawned next run — or this run, when
step 3 found it) versus `stopped: cause unclear — analysis on #<n>` (re-spawned only on new
evidence) versus `stopped: owner decision — #<n>` (never re-spawned without a word from the
owner). A bare null is read as `deferred`.

**That third reason exists because the first two both lie about a real and recurring case: the
cause is fully known, and the only available code change is one the owner already made on
purpose.** A tuning constant, a pool depth, a tier, a timeout, a log level — where the source
carries a comment saying *why* it is that value, an agent sent to "fix" it is not fixing a bug,
it is overruling a documented decision with no new authority to do so. `deferred` would requeue
it every night; `cause unclear` is simply false and invites a pointless analysis comment. File
the issue with the measurement that makes the decision reviewable, put the decision under the
report's *Needs you*, and record `stopped: owner decision`. The sweep's contribution to a
judgement call is evidence, not a PR.

## Step 7b — Close an issue only when the code and the telemetry both prove it

The sweep may close an issue. It may not *decide* one. The line between those is evidence, and every
test below must pass. A single miss leaves the issue open and puts it under the report's *Needs you*
with the test that failed. When two readings of a test disagree, the issue stays open.

1. **A machine filed it.** The ledger's `filed_by` is `sweep` or `bot`, and the issue's author on
   GitHub agrees (`gh issue view <n> --repo <slug> --json author`). Check the author, not the
   presence of a ledger entry — the step 3 tracker search can put a person's issue in the ledger.
   `bot` means the app's own triage workflow, which the `github-auto-issues` adapter identifies by
   its label and its `fp:` fingerprint. An issue a person wrote is never closed by a machine.
2. **A merged PR fixes it, by name.** The ledger's `pr`, or a merged PR whose body says `Closes #<n>`
   or `Fixes #<n>`. Quiet with no PR is not fixed — it is waiting.
3. **That PR is deployed.** The adapter's deploy data names the running commit (Netlify `commit_ref`,
   the deploy workflow's `headSha`), and the PR's merge commit is its ancestor:

   ```bash
   git merge-base --is-ancestor <merge-sha> <deployed-sha>
   ```

   Both commits must be in the local object store first — fetch them the way step 4 does. Where the
   card's adapters cannot name a deployed SHA, nothing closes.
4. **The telemetry has been quiet since the deploy, for long enough.** Zero occurrences of the
   signature after the deploy timestamp, and the quiet span is at least the *longest* of: 72 hours;
   the card's collection window; the ledger's `max_gap` for the signature. That last figure is the
   one the window cannot supply — a 26 h Netlify pass or a 24 h Supabase pass cannot see a weekly
   bug's rhythm, and even App Insights' `P30D` is a ceiling — which is why step 7 accumulates it
   across runs. **A null `max_gap` means fewer than two occurrences are on record, so there is no
   gap to measure: leave it open** and say so. One hit proves neither a rate nor its absence. The
   72 h floor exists because one per-node fix looked good for 33 h. Another looked good for 184 h,
   which is why the reverse path below exists.
5. **The adapters can see this signature.** Absence from a stream that structurally cannot carry
   the failure — a circuit-driven action, an Information-level trace, a suppressed route — proves
   nothing. A fix whose success condition *is* absence (a telemetry filter, a sampling rule) passes
   only with the adapter's control probe on record (app-insights §6b).
6. **No person has spoken since the fix.** A human comment on the issue newer than the PR's merge
   means someone is engaged; leave it to them. This is also the override: to hold any issue open,
   comment on it.
7. **The run is not clearing the board.** If more than five issues qualify in one run, close none
   and list them all. A rule that suddenly matches everything is more likely wrong than right — a
   misread deployed SHA passes test 3 for every issue at once.

Close with the evidence on the issue, not only in the report:

```
gh issue close <n> --repo <slug> --reason completed --comment "<PR, merge SHA, deployed SHA and time, quiet span, the pass that confirmed absence>. Closed by the error sweep; reopen if it recurs."
```

Then the ledger entry: `status: fixed`, `pr`, `closed_by_sweep: <date>`.

**Never `not planned`.** An `external` or `noise` signature has no fix to prove — it went quiet on
its own, or it is correct behaviour that someone still has to agree is correct. Those closes are the
owner's. List them under *Needs you* with the evidence and, for a batch, the single command that
does it, so the decision is one click.

**The reverse is never automatic.** A `fixed` signature that recurs after its deploy is not "already
fixed" — it is a regression, or a fix that closed one instance and not the class. Comment the
recurrence on the closed issue, recommend a reopen under *Needs you*, and re-triage it as new
evidence: the routing table decides whether it earns a fix agent, and that brief must name the PR
that did not hold.

## Step 8 — Report

Write the full write-up to the card's dated report file, then a short summary to `last-run-report.md` and to the chat:

- error lines per source in the window, distinct signatures, how many were new
- what you filed and what you spawned, with issue/PR numbers and links
- what you deliberately skipped — over the cap, or matched known-noise
- what failed, and which failure classes this run could not see
- what you closed under step 7b, each with the test evidence, and what fell short and on which test
- **Needs you** — decisions and tracker state changes only: close #n (short of 7b's proof), merge #n,
  escalate to a provider, rotate a secret, each with its evidence and your recommendation. Code
  work is never in this list; it is in the PRs above. Additive comments you already posted are
  listed as done.
- any finding that is not yet actionable, and why

**Re-verify every carry-forward against the code before repeating it.** A ledger note saying "fixed,
awaiting the user's decision" was true on the day it was written and is a claim about the past, not
the present. Carrying one forward unchecked hands the user a decision they already made — on `auxf`,
a note listing three open `reportError.ts` defects was repeated across two runs after the PR that
fixed all three had already merged. Before any item reaches the report's carry-forward section, open
the file it names and confirm the state still holds. Then correct the ledger entry in the same run.

**But a file can only settle a claim about code.** Carry-forwards come in two kinds and they verify
differently:

- **Code claims** — "this defect is still present", "this PR has not landed". Answerable by reading
  the source, the branch, or the tracker. Re-verify these yourself, every run.
- **Human-action items** — "the user must rotate this secret", "awaiting the user's decision",
  "someone has to apply this migration". **No file confirms these.** A clean-looking source file does
  not mean the person acted, and it does not mean they decided. Carry these forward **unchanged**
  unless something independently attests the action — an issue closed by the owner, a comment, a
  merged PR, a changed configuration — and name that evidence in the ledger note when you clear one.

The two often ride in one entry: "defect X, fixed, awaiting the user's decision on Y" is a code claim
bolted to a human one. Verify each half separately. Clearing the whole entry because the code half
resolved is how a real pending decision disappears.

**If nothing new appeared, say exactly that in one line.** File nothing, spawn nothing, do not pad the report.

## When you learn something about the tooling

A gotcha you discover about a *stack* (a CLI flag that lies, a field that is a string when it looks like a bool) belongs in `adapters/<name>.md`, not in a report where the next run will not read it. A gotcha about a *project* belongs in that task's project card. Edit the file in the same run you learn it — that is the only reason this pipeline stops re-learning the same things.
