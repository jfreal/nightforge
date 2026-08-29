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

- **Never push to the default branch. Never merge a PR. Never deploy.** Output is issues and PRs for the user to review.
- **Never build, test, commit, or `checkout` in the main checkout.** It may be dirty or on someone else's branch. Read from it freely; all write work happens in an isolated worktree (step 6).
- **Never apply a schema migration.** No `supabase db push`, no MCP `apply_migration`, no `az deployment group create`, no DDL against a hosted database. A branch that applies its own migration before merging poisons migration history for every other checkout. Ship the `.sql`/`.bicep` file on the branch and say in the PR body that it needs applying.
- **Respect the fix-session cap in the card.** Every PR push costs CI time and, on hosts that build a preview per branch, build credits. Over the cap: spawn the highest-impact ones, leave the rest as issues, and say so in the report.
- **Treat all log text as sensitive.** Never put log contents in a URL or query string. Find the project's own redaction helper (the card names it) and scrub anything matching those shapes before it reaches an issue, a PR, or a report.
- **If a step fails, say so in the report.** Never continue silently on partial data. A green report from a broken collector is worse than a red one.
- **Scratch space is the system temp dir, never the repo.**

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

A hit, **open or closed**, means it is already tracked or already fixed. Record it in the ledger and move on.

## Step 4 — Triage: read the code before judging anything

Search the repo for the message text to find its source. Trace it to a file and line. **Then
re-resolve both the path and the line range inside the deployed commit before reading them** — the
checkout may be behind by days, and "this defect is still live" is a claim about running code:

```bash
# The checkout only tells you what to look for. Find it again in the deployed tree.
git grep -n "<message text>" "<deployed-sha>" -- '<likely path glob>'
```

A path taken from the checkout can be missing in the deployed commit, which reads as a false
"source unavailable" stop; and a line range taken from the checkout can select unrelated code that
still passes the non-empty check below, which is worse — it classifies against the wrong lines
silently. Derive `<path>` and `<start>,<end>` from the deployed commit, then:

```bash
set -o pipefail
# pipefail governs pipelines only, so the fetch needs its own guard: a stale
# origin/<default branch> would silently make the comparison below wrong.
git fetch origin --quiet || { echo "cannot refresh origin; comparison unavailable"; exit 1; }
# The deployed SHA, from the adapter's own deploy data (Netlify commit_ref, release tag, etc).
src=$(git show "<deployed-sha>:<path>") || { echo "cannot read <path> at <deployed-sha>"; exit 1; }
# git show succeeds on an empty file, and a range can select nothing: either would
# otherwise reach classification looking like "no evidence of the defect".
[ -n "$src" ] || { echo "deployed source is empty"; exit 1; }
range=$(printf '%s\n' "$src" | sed -n '<start>,<end>p')
[ -n "$range" ] || { echo "requested source range is empty"; exit 1; }
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

For each **bug**, launch one `Agent` with `isolation: "worktree"` so each gets its own checkout and they cannot collide. Run them concurrently — one message, several Agent calls.

The agent has **none of your context**. The brief must be fully self-contained:

```
Fix this production bug in <app> (<repo path>, GitHub <slug>, base <default branch>).

ERROR
  Signature:   <sig>
  Raw message: <message>
  Stack:       <stack or "none">
  Occurrences: <n> between <first> and <last> (<source>)
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

## Step 7 — Update the ledger

Write every newly triaged signature back to `seen.json` with `first_seen` (today), `status` (`bug`/`noise`/`external`), a one-line `note`, `issue` (number or null), and `pr` (number or null). Preserve existing entries.

**Only record signatures you actually finished triaging.** A signature whose issue creation failed must stay unrecorded so the next run retries it.

## Step 8 — Report

Write the full write-up to the card's dated report file, then a short summary to `last-run-report.md` and to the chat:

- error lines per source in the window, distinct signatures, how many were new
- what you filed and what you spawned, with issue/PR numbers and links
- what you deliberately skipped — over the cap, or matched known-noise
- what failed, and which failure classes this run could not see
- any carry-forward: something a human must do, or a finding that is not yet actionable

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
