# Project card template

A **project card** is the only per-project input the `error-sweep` skill needs. Everything else —
signature normalization, ledger discipline, triage rules, the fix-agent brief, the report format —
lives in the shared skill.

Put the card in the scheduled task's `SKILL.md` (or in the product repo and point at it from there —
the card drifts with the code it describes, so versioning it beside that code is usually right).

**Keep cards out of a public repo.** They carry infrastructure identifiers — subscription ids, site
ids, project refs. Not credentials, but not worth publishing either. Same for ledgers and reports:
production log text routinely contains capability URLs, tokens, and user data.

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
| **Ledger** | `<abs path>/seen.json` |
| **Reports** | `<abs path>/reports/<YYYY-MM-DD>-errors.md`, summary to `..\last-run-report.md` |
| **Fix cap** | **<n>** sessions per run |
| **Issue label** | `<label>` |
| **Redaction helper** | `<function>` in `<file>` — <the secret shapes this app can leak> |

### Adapter config

<one line per adapter: the ids it needs, and any project-specific quirk.
 See each adapter file for what it requires.>

### Known noise — do not refile, do not spawn

<Patterns already seen and closed. This list is why a run does not waste itself
 re-triaging the same non-bugs. Add to it every time you close something as noise.>

### Standing carry-forward — re-check each run, do not re-derive

<Things a human must do, and findings that are real but not yet actionable.
 Without this the sweep rediscovers them from scratch every night and pads
 every report with the same paragraph.>

### Fix-agent commands (paste into the brief verbatim)

<How to install deps in a bare worktree, how to typecheck, test, lint, build.
 Any convention the repo enforces that a fix could violate.
 Any deploy whitelist a new build input must be added to.>
```

---

## Why the card is thin

Three sweeps that started as three hand-written procedures drifted into three different pipelines
within a couple of months — one filed issues and opened PRs, one only filed issues, one was a single
sentence with no dedup ledger at all and re-triaged the same errors nightly.

The split that holds: **the pipeline is the same everywhere, the collection is per-stack, and only
identifiers and conventions are per-project.** Adding a project is one card. Adding a stack is one
adapter. A pipeline fix is one edit that every project gets.

## Where knowledge goes when you learn it

- Gotcha about a **stack** (a CLI flag that lies, a field that is a string when it looks like a
  bool) → the adapter.
- Gotcha about a **project** (a route that 404s by design, a slow query that is a tier cost and not
  a defect) → that project's card, under known-noise.
- Neither → the shared skill.

Never leave it only in a run report. The next run does not read those, and you will pay to learn it
again.
