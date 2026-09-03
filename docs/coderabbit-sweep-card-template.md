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
| **Paused quiet** | **<n> minutes** (120) — a PR on a branch CodeRabbit paused waits until its head commit is this old |
| **Barren backoff max** | **<n>** (3) — how many times a PR's cooldown may double after consecutive reviews that found nothing |
| **Retention** | **<n>** (40) — `fired` entries kept. Must be at least `cooldown x 2^backoff max` in **hours**, since at most one fire lands per hour |
| **Ledger** | `<abs path>/ledger.json` |
| **Report** | `<abs path>/reports/<YYYY-MM-DD>.md` |

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

**Paused quiet** and **barren backoff max** — the two guards against one busy PR eating the fleet's
budget. Raise *paused quiet* on a fleet where agents push in long bursts; it only delays a PR whose
branch CodeRabbit has already flagged as churning, and never a quiet one. Raise *barren backoff max*
where PRs sit open a long time between real changes, lower it where a stale review costs little.
`0` disables the backoff. Neither can retire a PR — only the give-up flag does that.

Whatever these become, **retention must outlast the longest window they can produce**
(`cooldown x 2^max`), because cooldowns are read from the ledger's `fired` list and a trimmed entry
is a cooldown that silently stops applying.

State that floor in **entries**, which is what retention actually counts. At most one fire lands per
hour, so the minimum is the widest window in hours, plus a few for the reconcile tail: 90 minutes
doubled 3 times is 12 hours, so 16 entries — the default 40 covers it comfortably. Raise the cap to
6 and the window becomes 96 hours, needing 100 entries; leave retention at 40 there and the PR fires
after about 40 hours with nothing saying why. The script checks this at startup and raises retention
rather than refusing to run, but the card should carry the right number so the check stays quiet.

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
