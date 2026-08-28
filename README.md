# nightforge

Agentic "dark factory" tools and skills that I use across my repos.

## `ELI10` output style

A Claude Code output style for end-of-day brains: plain English, jargon defined once, every report
structured as *what I did / did it work (with proof) / what I need from you* — and that last part is
skipped when nothing is left for you to do. Git commands never appear in the report; you get the
branch, the short hash, and the file count in words instead. Technical detail stays: paths, error
text, test counts, and versions are kept exact. Decisions come as two options max with a
recommendation.

```text
output-styles/ELI10.md
```

### Install

Copy the file into your global output-styles folder and select it:

```bat
mkdir "%USERPROFILE%\.claude\output-styles" 2>nul
copy output-styles\ELI10.md "%USERPROFILE%\.claude\output-styles\ELI10.md"
```

`copy` does not create parent directories, so the `mkdir` matters on a fresh install. It is
harmless when the folder already exists.

Then set `"outputStyle": "ELI10"` in `%USERPROFILE%\.claude\settings.json` to make it your
default everywhere. The standalone `/output-style` command is gone — as of 2.1.237 it just
redirects into `/config`.

`/config` works too, but mind the scope: it saves the choice to the **project-local**
`.claude/settings.local.json`, so it applies to that one repo and does not follow you to the
next. A global default means editing the global file.

For one session, persisting nothing:

```bat
claude --settings "{\"outputStyle\":\"ELI10\"}"
```

However you set it, the style is part of the system prompt, so it takes effect on a new
session or after `/clear`, never mid-conversation.

No clone handy? Paste this into any Claude Code session and it installs itself. An output
style becomes part of your system prompt in every later session, so read the file it fetches
before you let it be saved — that is what the "show me the file first" line is for:

> Set my Output Style to the one at
> https://raw.githubusercontent.com/jfreal/nightforge/main/output-styles/ELI10.md
> Show me the file first, and only if I say go: save it in my global
> output-styles folder as ELI10.md, set outputStyle to ELI10 in my global
> settings file without breaking the existing JSON, list the files you changed,
> and tell me to restart Claude Code.

## `error-sweep`

One pipeline for unattended production error sweeps, whatever the stack. It collects errors,
normalizes them to stable signatures, dedupes against a ledger *and* the issue tracker, triages each
survivor against the actual source, files an issue, and spawns a worktree-isolated fix agent that
opens a PR. It runs overnight and reports one line when there is nothing new.

```text
skills/error-sweep/
  SKILL.md                      the pipeline — steps 0-8, stack-agnostic
  adapters/netlify.md           functions, edge functions, failed deploys
  adapters/supabase.md          postgres/api/auth/edge logs, security advisors
  adapters/app-insights.md      exceptions, failed requests, traces, dependencies
  adapters/github-auto-issues.md  issues the app files about itself
skills/docs-sweep/
  SKILL.md                      the weekly docs sweep — discover, audit, fix, draft PR
skills/coderabbit-sweep/
  SKILL.md                      the hourly CodeRabbit re-review sweep — find, gate, fire one
  EVIDENCE.md                   dated case law behind each rule — grepped, never read whole
docs/project-card-template.md   the per-project input, and how to fill it in
docs/docs-sweep-card-template.md  the docs-sweep roster card, and how to fill it in
docs/coderabbit-sweep-card-template.md  the coderabbit-sweep fleet card, and how to fill it in
docs/sync-docs.md               how this repo keeps its own docs from drifting
.claude/skills/sync-docs/       the audit that enforces it (repo-local, not published)
```

**The split:** the pipeline is identical everywhere, collection is per-stack, and only identifiers
and conventions are per-project. Adding a project is one card. Adding a stack is one adapter. A
pipeline fix is one edit every project inherits.

### Install

Clone, then junction the skill into your Claude config so the live path and the repo are the same
bytes — no sync script, no drift:

```bat
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

## `coderabbit-sweep`

CodeRabbit's review allowance is **per developer, across every repo you own** — at sustained
activity it drops to one review per hour. PRs that open while it is spent get a *Review limit
reached* comment instead of a review, and nothing ever retries them. This sweep is the retry: once
an hour it lists every open PR the account owns, works out which ones have no finished review
against their current head commit, and spends the one available review on the **single oldest**
starved PR.

One routine, one trigger per run, is the point. A trigger per repo is several jobs racing for one
account-wide slot, none of them aware of the others — which is how you get a queue where the newest
PR always wins and the oldest never gets reviewed at all. The sweep also refuses to fire inside a
known throttle window, because a trigger sent while the allowance is spent is consumed and buys
nothing.

Completeness is judged on evidence, not on the bot's own wording: a PR counts as reviewed only when
CodeRabbit has posted a review whose body starts with `**Actionable comments posted:` **at the PR's
current head SHA**, or — when the pass found nothing and so posted no review object at all — a
`recent_review` block naming that SHA. Those two are the whole contract. A walkthrough is not one: it
is a summary, and CodeRabbit posts one on PRs whose review was throttled. Neither is the
*"Full review finished."* reply, which is vendor-worded prose. The rate-limit banner is not a live
signal either — it stays in the comment body after a later attempt succeeds — and empty-bodied
"reviews" are just the bot replying in a thread.

### Install

```
cmd /c mklink /J "%USERPROFILE%\.claude\skills\coderabbit-sweep" "<clone>\skills\coderabbit-sweep"
```

Then write the fleet card into an hourly scheduled task (see
[docs/coderabbit-sweep-card-template.md](docs/coderabbit-sweep-card-template.md)).

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
