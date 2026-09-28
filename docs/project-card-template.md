# Project card template

<!-- docKey: project-card -->

A **project card** is the only per-project input the `error-sweep` skill needs. Everything else —
signature normalization, ledger discipline, triage rules, the fix-agent brief, the report format —
lives in the shared skill. Everything the sweep *learns* about the project lives in its Notion board.

The card holds **identifiers only**: where the app, the repo, the telemetry, and the memory are. Put
it in the scheduled task's `SKILL.md`.

**Keep cards out of a public repo.** They carry infrastructure identifiers — subscription ids, site
ids, project refs, Notion data source ids. Not credentials, but not worth publishing either. Same for
ledgers, reports, and knowledge: production log text routinely contains capability URLs, tokens, and
user data. Those live in Notion and the local task folder, never in git.

---

```markdown
---
name: <project>-error-sweep
description: Nightly production error sweep for <App>; triages each new bug and spawns a fix agent that opens a PR.
---

**Run the `error-sweep` skill against the project card below.** The pipeline lives at
`<path to>/skills/error-sweep/SKILL.md` — read it first; it is the whole procedure.
Adapters are in `<path to>/skills/error-sweep/adapters/`.

## Project card

| Field | Value |
|---|---|
| **App** | <name, one line of what it is> — <production URL> |
| **Repo** | `<local path>` — GitHub `<owner/repo>`, default branch `<main|master>` |
| **Adapters** | `<netlify>`, `<supabase>`, `<app-insights>`, `<github-auto-issues>` |
| **Window** | <per source; note any retention limit> |
| **Fix cap** | **<n>** sessions per run |
| **Issue label** | `<label>` |
| **Redaction helper** | `<function>` in `<file>` — <the secret shapes this app can leak> |
| **Notion board** | <board page URL> |
| **↳ runs** | `collection://<id>` — view <view URL> |
| **↳ signatures (ledger)** | `collection://<id>` — view <view URL> |
| **↳ knowledge** | `collection://<id>` — view <view URL> |
| **Local fallback** | `<task folder>\seen.json` — read-only mirror, used only when Notion is unreachable. Reports backed up to `<task folder>\reports\` |

### Adapter config

<one line per adapter: the ids it needs. See each adapter file for what it requires.>

Everything else this sweep knows about <App> — known noise, traps, failure classes, fix-agent
commands — lives in **Sweep knowledge**. Read every Active row before step 3.
```

---

## Setting up the Notion board

One page per project, holding three databases. The sweep never changes their schema, so create them
with these columns. Get each view URL by opening the database and copying the link of its default
view (`https://www.notion.so/<database id>?v=<view id>`).

**Error sweep runs** — one row per run.

| Column | Type | Notes |
|---|---|---|
| Name | title | the run date |
| Date | date | |
| Window | date range | the telemetry this run actually saw |
| Gap hours | number | previous window end → this window start. About 0 is healthy |
| Result | select | `Clean`, `Findings`, `Partial`, `Failed`, `Missed` |
| Headline | text | one line |
| New signatures, Filed, Spawned, Closed, Needs you | number | |
| Deployed commit | text | |
| Signatures | relation → Error signatures | |

**Error signatures** — the dedup ledger.

| Column | Type | Notes |
|---|---|---|
| Name | title | short human title |
| Signature | text | the exact normalized key |
| Class | select | `bug`, `noise`, `external`, `fixed`, `resolved` |
| Source | select | the adapter names, plus `sweep`, `ci`, `deploy`, `followup` |
| Summary | text | the triage and the ledger note; full reasoning in the page body |
| Issue | number | |
| PR | text | |
| Filed by | select | `sweep`, `bot`, `human` |
| First seen, Last seen | date | |
| Max gap h | number | step 7b's quiet test depends on it |

**Sweep knowledge** — what the sweep has learned.

| Column | Type | Notes |
|---|---|---|
| Topic | title | |
| Scope | select | `project`, `pipeline`, or an adapter name |
| Kind | select | `known-noise`, `trap`, `failure-class`, `config`, `fix-agent`, `carry-forward`, `history` |
| Rule | text | the lesson, enough to act on without opening the page (under 1800 characters) |
| Status | select | `Active`, `Retired` |
| Added | date | |
| Origin | text | where it was learned |

Seed the knowledge store with at least one `fix-agent` row: how to install deps in a bare worktree,
how to typecheck, test, lint, build, and any deploy whitelist a new build input must be added to.
The fix-agent brief pastes it verbatim.

## Why the card is thin

Three sweeps that started as three hand-written procedures drifted into three different pipelines
within a couple of months — one filed issues and opened PRs, one only filed issues, one was a single
sentence with no dedup ledger at all and re-triaged the same errors nightly.

The split that holds: **the pipeline is the same everywhere, the collection is per-stack, and only
identifiers and conventions are per-project.** Adding a project is one card and one board. Adding a
stack is one adapter. A pipeline fix is one edit that every project gets.

The cards used to hold the known-noise lists and traps too. They grew past 100 KB each, and every
lesson a run wrote back into an adapter became a pull request against this repo. Knowledge now goes
to a database the run can write without review.

## Where knowledge goes when you learn it

Into the project's **Sweep knowledge**, as one row, in the same run you learn it:

- Gotcha about a **stack** (a CLI flag that lies, a field that is a string when it looks like a
  bool) → `Scope` = the adapter's name. If it would help every project, the report lists it and the
  owner folds it into the adapter file by hand.
- Gotcha about a **project** (a route that 404s by design, a slow query that is a tier cost and not
  a defect) → `Scope` = `project`, usually `Kind` = `known-noise` or `trap`.
- How the sweep should behave on this project → `Scope` = `pipeline`.

Never leave it only in a run report. The next run does not read those, and you will pay to learn it
again. And never edit the card, the skill, or an adapter from a run.
