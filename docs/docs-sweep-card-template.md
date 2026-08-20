# Roster card template

<!-- docKey: docs-sweep-card -->

A **roster card** is the only input the `docs-sweep` skill needs. Everything else — discovery,
worktree isolation, the audit/fix handoff to each repo's own sync-docs port, the PR shape, the
report format — lives in the shared skill.

Put the card in the scheduled task's `SKILL.md`. Unlike an error-sweep project card there is one
card for the whole fleet, because the per-repo knowledge already lives *in* each repo: its
`.claude/skills/sync-docs/` port defines what to audit and what to write. The card only says where
to look and what to override.

**Keep the card out of a public repo** — it carries local filesystem layout and the private-repo
slugs of everything you work on. Not credentials, but not worth publishing.

---

```markdown
---
name: docs-sweep
description: Weekly docs sweep across every repo carrying a sync-docs skill; opens a draft PR per repo whose docs drifted.
---

**Run the `docs-sweep` skill against the roster card below.** The pipeline lives at
`<path to>/skills/docs-sweep/SKILL.md` — read it first; it is the whole procedure.

## Roster card

| Field | Value |
|---|---|
| **Repos root** | `<abs path scanned for */.claude/skills/sync-docs/>` |
| **Worktrees root** | `<abs path>` |
| **Exclude** | `<repo>`, `<repo>` — <why, one clause each> |
| **Extra repos** | `<abs path outside the root>` |
| **PR cap** | **<n>** PRs per run |
| **Branch prefix** | `docs/sweep-` |
| **Report** | `<abs path>/reports/<YYYY-MM-DD>-docs.md` (optional) |

### Per-repo overrides

<one line per repo that needs one — GitHub slug when the remote lies, default
 branch when origin/HEAD is unset, and the verify command for docs that build:>

| Repo | Override |
|---|---|
| `<repo>` | verify: `<command that builds the docs, run from the worktree root>` |
```

---

## Why one card

The per-project split that error-sweep needs does not exist here. A docs sweep has no adapters, no
ledger, no per-app noise list: the repo's own sync-docs port *is* the per-repo configuration, and it
is versioned in that repo where it drifts with the docs it guards. What remains — paths, excludes,
caps, a build command — is one thin table.

## Where knowledge goes when you learn it

- Gotcha about a **repo** (its docs build command, a quirk of its sync-docs port) → this card's
  per-repo overrides.
- Gotcha about the **pipeline** → `skills/docs-sweep/SKILL.md`.

Never leave it only in a run report. The next run does not read those.
