# Fleet card template

<!-- docKey: coderabbit-sweep-card -->

A **fleet card** is the only input the `coderabbit-sweep` skill needs. Everything else — PR
discovery, the throttle gate, the complete-vs-starved test, oldest-first ranking, the single
trigger, and the ledger — lives in the shared skill.

Put the card in the scheduled task's `SKILL.md`. There is one card for the whole account, not one
per repo, because CodeRabbit's review allowance is **per developer across every repo**. A card per
repo would recreate the exact problem this sweep exists to fix: several triggers racing for one
hourly slot, each unaware of the others.

**Keep the card out of a public repo** — it names private repo slugs and local filesystem paths.
Not credentials, but not worth publishing. The ledger and reports stay private for the same reason.

---

```markdown
---
name: coderabbit-sweep
description: Hourly sweep for open PRs CodeRabbit never finished reviewing; triggers a full review on the single oldest one.
---

**Run the `coderabbit-sweep` skill against the fleet card below.** The pipeline lives at
`<path to>/skills/coderabbit-sweep/SKILL.md` — read it first; it is the whole procedure.

## Fleet card

| Field | Value |
|---|---|
| **Owner** | `<github login or org>` — one card may list several, comma-separated |
| **Exclude repos** | `<repo>`, `<repo>` — <why, one clause each> |
| **Exclude PRs** | `<slug>#<n>` — <why> |
| **Include drafts** | **no** (default) / yes |
| **Trigger phrase** | `@coderabbitai full review` |
| **Cooldown** | **<n> minutes** — a PR fired inside this window is not re-fired |
| **Ledger** | `<abs path>/ledger.json` — keep the newest **<n>** `fired` entries |
| **Board template** | `<abs path>/board-template.html` |
| **Report** | `<abs path>/reports/<YYYY-MM-DD>.md` |
| **Evidence** | `<path to>/skills/coderabbit-sweep/EVIDENCE.md` |

### Per-repo notes

Add a row the run you learn one.

| Repo | Note |
|---|---|
| `<repo>` | <what is different about it> |
```

## Filling in the fields

**Owner** — one `gh search prs --owner <owner>` covers every repo it owns, so this is the whole
roster. For several owners, list them all in the one Owner row: `--owner` is repeatable, so they
become `--owner <a> --owner <b>` in a **single** search, not one card each and not one search per
owner. The one-trigger-per-run cap spans every owner on the card, because the allowance is per
developer, not per owner — and a second card would be a second runner fighting the first for the
same slot, which is the problem this sweep was built to end.

**Exclude repos** — repos where CodeRabbit is not installed. They cost an API round trip per run
and can never produce a candidate. A repo with no CodeRabbit comment on any PR is the tell.

**Include drafts** — leave this `no` unless the CodeRabbit config actually reviews drafts. A draft
gets a *Review skipped* comment instead of a review, so triggering one can spend the hourly slot and
buy nothing. It matters most when other automation opens draft PRs in bulk: they are numerous and
they are old, so oldest-first ranking would hand them every slot.

**Cooldown** — long enough to cover a queued review landing, short enough that a swallowed trigger
gets retried the same day. Somewhere near the review interval is the safe default; shorter than the
run interval defeats the purpose.

**Ledger** — the only memory between runs: `throttledUntil`, the published `boardUrl`, and the
`fired` list. Name a retention on the card and keep it small; the sweep drops older `fired` entries
when it writes the ledger back. It once grew to 90KB and cost about 6k tokens of read on *every*
hourly run. It follows that `fired` is not a lifetime record — never compute totals or streaks off it.

**Board template** — the HTML the run fills in to publish the board Artifact. It lives on disk
precisely so the sweep never has to fetch the published board to recover its own design: fetching
returns the artifact runtime preamble, roughly 12k tokens of framework JavaScript, for CSS that was
already sitting in this file.

**Evidence** — the path to `EVIDENCE.md`, the dated case-law log beside the pipeline. Dated
observations and worked examples are appended there; the pipeline file keeps only the rules. Grep it
for the case you need — never read it whole.

**Trigger phrase** — `@coderabbitai full review` re-reviews the entire PR from scratch;
`@coderabbitai review` is an incremental pass over what changed since the last review. Full is right
for a PR that was never reviewed at all, which is the common case here.

## Where a learned gotcha goes

- About a **repo or the fleet** (a repo without the app, a PR to leave alone, a phrase that behaves
  differently on one repo): the card.
- About **CodeRabbit or the pipeline** (a new marker in the summary comment, a change to the
  rate-limit block, a better completeness test): `skills/coderabbit-sweep/SKILL.md`, so every fleet
  running the sweep inherits it.

Never only in a run report. Nothing reads last hour's report.
