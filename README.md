# nightforge

Agentic "dark factory" tools and skills that I use across my repos.

## `error-sweep`

One pipeline for unattended production error sweeps, whatever the stack. It collects errors,
normalizes them to stable signatures, dedupes against a ledger *and* the issue tracker, triages each
survivor against the actual source, files an issue, and spawns a worktree-isolated fix agent that
opens a PR. It runs overnight and reports one line when there is nothing new.

```
skills/error-sweep/
  SKILL.md                      the pipeline — steps 0-8, stack-agnostic
  adapters/netlify.md           functions, edge functions, failed deploys
  adapters/supabase.md          postgres/api/auth/edge logs, security advisors
  adapters/app-insights.md      exceptions, failed requests, traces, dependencies
  adapters/github-auto-issues.md  issues the app files about itself
skills/docs-sweep/
  SKILL.md                      the weekly docs sweep — discover, audit, fix, draft PR
docs/project-card-template.md   the per-project input, and how to fill it in
docs/docs-sweep-card-template.md  the docs-sweep roster card, and how to fill it in
docs/sync-docs.md               how this repo keeps its own docs from drifting
.claude/skills/sync-docs/       the audit that enforces it (repo-local, not published)
```

**The split:** the pipeline is identical everywhere, collection is per-stack, and only identifiers
and conventions are per-project. Adding a project is one card. Adding a stack is one adapter. A
pipeline fix is one edit every project inherits.

### Install

Clone, then junction the skill into your Claude config so the live path and the repo are the same
bytes — no sync script, no drift:

```
cmd /c mklink /J "%USERPROFILE%\.claude\skills\error-sweep" "<clone>\skills\error-sweep"
```

Then write a project card per app (see `docs/`) and point a scheduled task at it.

### What is deliberately not here

Project cards, dedup ledgers, and run reports. Cards carry infrastructure identifiers; ledgers and
reports carry raw production log text, which routinely includes capability URLs, tokens, and user
data. Keep all three in a private repo, or out of git entirely.

### War stories

Every rule in here was paid for. The adapters carry the sharp edges inline, where you will hit them;
these are the ones worth reading before you write your own:

- **A sweep with no dedup ledger re-triages the same errors forever.** Three sweeps written by hand
  drifted into three different pipelines in about two months — one filed issues and opened PRs, one
  only filed issues, and one was a single sentence with no ledger at all, so it rediscovered its
  whole backlog nightly and its reports grew to 24 KB of the same findings. That drift is the reason
  this repo exists.
- **Dedupe against closed issues, not just open ones.** On this pipeline's first live run a
  duplicate-key error was new to the empty ledger and would have been filed — except the tracker
  search found an issue already closed by a PR merged fourteen minutes after the error's last
  occurrence. Signature dedup could not have caught it. That one check stopped a junk issue and a
  fix agent aimed at an already-merged fix.
- **A green collector is not evidence of health if it cannot see the failure class.** An
  exceptions-only sweep is structurally blind to a 404. One app served 404s to a live subscriber for
  21 hours while every scheduled run reported success.
- **A silently narrowed window looks exactly like a healthy app.** `az monitor app-insights query`
  defaults `--offset` to one hour and applies it *before* the KQL, so a `| where timestamp > ago(7d)`
  filter narrows nothing and widens nothing. One project ran green every day for a week while missing
  six of its eight live problemIds.
- **Fix agents cost money downstream.** Every PR branch pushed to a host that builds a preview per
  branch triggers a build. The per-project fix cap is a budget decision, not a safety rail — set it
  against that project's actual bill.
- **Knowledge left in a run report is knowledge you will pay for twice.** Nothing reads last night's
  report. Gotchas go in the adapter or the card, in the same run you learn them.

Mechanical traps the adapters document, each of which fails *quietly*:

- `requests.success` is a **string** in App Insights; `dependencies.success` is a real bool. The
  wrong predicate errors out naming nothing.
- A multi-line `--analytics-query` runs only line 1 on Windows `az` and returns a plausible wrong
  table — the worst kind of failure.
- Piping `netlify logs` through a shell filter truncates the stream *and* returns a spurious
  non-zero exit, which reads as a dead collector when it worked.
- `min(timestamp)` aliased to `first` or `last` is rejected as a reserved word, with an opaque error
  that names nothing.

## `docs-sweep`

The weekly counterpart to `sync-docs` (below): where sync-docs keeps *one* repo's docs honest when
you remember to run it, `docs-sweep` runs it for you, across every repo that has it. It discovers
each local repo carrying a repo-local `.claude/skills/sync-docs/` port, runs that repo's own audit
in a fresh worktree off the default branch, and where the docs drifted, runs that port's fix scope
and opens a **draft PR** for review. Clean repos get one line in the report; nothing is pushed to a
default branch and nothing is merged.

The split mirrors error-sweep, one level up: the pipeline is identical everywhere, and the per-repo
knowledge is not in a card — it is the target repo's own sync-docs port, versioned beside the docs
it guards. A repo joins the sweep by carrying the port; there is no registration step. The one
roster card (see `docs/`) only says where to scan, what to exclude, the PR cap, and per-repo
overrides like a docs build command.

### Install

```
cmd /c mklink /J "%USERPROFILE%\.claude\skills\docs-sweep" "<clone>\skills\docs-sweep"
```

Then write the roster card into a weekly scheduled task (see
[docs/docs-sweep-card-template.md](docs/docs-sweep-card-template.md)).

## `sync-docs`

Repo-local tooling, not a published skill: it lives in `.claude/skills/sync-docs/` and runs *on*
nightforge. It keeps the pages under `docs/` honest about the files they describe.

Most of this repo documents itself — `skills/error-sweep/SKILL.md` explains the pipeline it also
defines, so it cannot drift. The exceptions are the pages that describe something living elsewhere:
`docs/project-card-template.md` documents a card that the pipeline and every adapter *read*, so a
new required field in an adapter silently makes that page wrong. `sync-docs` ties the two together
with a **doc key** — a name that appears as a `@doc:<key>` comment in each source and as a
`docKey:` marker on the page — and audits the pair. It also checks that every doc page is linked
from this README, that this README's file tree matches disk, and that the adapter roster inside
`skills/error-sweep/SKILL.md` matches the adapter files that actually exist.

Run `/sync-docs` to audit, `/sync-docs fix` to repair. Full mechanism:
[docs/sync-docs.md](docs/sync-docs.md).
