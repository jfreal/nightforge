<p align="center">
  <img src="docs/logo.png" alt="NightForge logo" width="160">
</p>

# nightforge

Agentic "dark factory" tools and skills that I use across my repos.

## Install

Every skill under `skills/` ships as one Claude Code plugin. The repo is its own marketplace, so
two commands install all of them on any machine that can read this repo:

```bat
claude plugin marketplace add jfreal/nightforge
claude plugin install nightforge@nightforge --scope user
```

The installed plugin is a copy pinned to a commit, not a live link to your clone. After you push a
skill change, pull it in and restart Claude Code:

```bat
claude plugin marketplace update nightforge
claude plugin update nightforge@nightforge
```

Plugin skills are namespaced, so `error-sweep` shows up as `nightforge:error-sweep`. A scheduled
task that reads a skill by file path should point at a clone (`<clone>\skills\error-sweep\SKILL.md`).
The plugin copy lives under a folder named after a commit hash, and that name changes on every
update.

Install the ELI10 output style by hand, as its section below shows.

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
skills/ci-cost-sweep/
  SKILL.md                      the CI-minutes pipeline — measure, profile, apply levers, prove
  adapters/github-actions.md    billing model, jobs-API measurement, cache inspection
  adapters/test-runners.md      per-test timings and parallelism, per runner
skills/codebase-cleanup/
  SKILL.md                      the whole-repo audit — research, size, one PR per finding
skills/sync-docs/
  SKILL.md                      the docs-drift audit — one engine, a config per repo
skills/onboarding-sweep/
  SKILL.md                      the hourly onboarding sweep: dogfood board to fix PRs
skills/unslop/
  SKILL.md                      strips AI tells from prose and adds voice
skills/simple-issue-description/
  SKILL.md                      turns a rough report or PR into a plain-language issue
  agents/openai.yaml            display metadata for Codex-style agent hosts
.claude-plugin/
  plugin.json                   the repo as one plugin: every skill under skills/
  marketplace.json              the repo as its own marketplace, listing that plugin
docs/project-card-template.md   the per-project input, and how to fill it in
docs/docs-sweep-card-template.md  the docs-sweep roster card, and how to fill it in
docs/coderabbit-sweep-card-template.md  the coderabbit-sweep fleet card, and how to fill it in
docs/sync-docs.md               how sync-docs works, and how this repo configures it
docs/logo.png                   the NightForge logo shown at the top of this README
.claude/sync-docs/              this repo's own sync-docs config and registry
```

**The split:** the pipeline is identical everywhere, collection is per-stack, and only identifiers
and conventions are per-project. Adding a project is one card. Adding a stack is one adapter. A
pipeline fix is one edit every project inherits.

### Install

Comes with the plugin (see [Install](#install)).

Then, per app: make a Notion board with the three databases the sweep remembers things in (runs,
signatures, knowledge — layouts in
[docs/project-card-template.md](docs/project-card-template.md)), write a project card that points
at them, and point a scheduled task at the card.

### What is deliberately not here

Project cards, dedup ledgers, run reports, and what the sweeps learn about each app. Cards carry
infrastructure identifiers; ledgers, reports, and lessons carry raw production log text, which
routinely includes capability URLs, tokens, and user data. The sweep keeps them in a private Notion
board per app and the card in the local scheduled task. A run never writes to this repo, so a night
of sweeping is never a pull request here.

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
  report. Gotchas go in the app's Notion knowledge store, in the same run you learn them.
- **Knowledge written back into the skill is a pull request a night.** For a month the sweeps edited
  the adapters and their own cards whenever they learned something. The cards passed 100 KB, and the
  adapters filled with one app's function names and dates, each change a PR with review and CI. Now a
  run writes lessons to a database, and the skill changes only by hand.
- **A chip is a bug you found and decided not to fix.** For three weeks the sweeps opened one a
  night — the follow-up a fix agent left out of scope, the runtime scan nobody spawned — and the
  owner woke up to a queue of suggestions instead of PRs. Now every code change with a clear cause
  goes to a fix agent, every additive tracker comment is posted in the run, an issue closes only
  when a merged, deployed PR and quiet telemetry both prove it, and the report's *Needs you* list
  holds only decisions — the closures, failed tests, skips, and not-yet-actionable findings stay in
  the sections above it. The pipeline has no chip output at all.

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
each local repo carrying a `.claude/sync-docs/config.json` (or, until it migrates, an old repo-local
`.claude/skills/sync-docs/` port), runs that repo's audit in a fresh worktree off the default branch,
and where the docs drifted, runs the fix scope and opens a **draft PR** for review. Clean repos get one line in the report; nothing is pushed to a
default branch and nothing is merged.

The split mirrors error-sweep, one level up: the pipeline is identical everywhere, and the per-repo
knowledge is not in a card — it is the target repo's own sync-docs config, versioned beside the docs
it guards. A repo joins the sweep by carrying the config; there is no registration step. The one
roster card (see `docs/`) only says where to scan, what to exclude, the PR cap, and per-repo
overrides like a docs build command.

### Install

Comes with the plugin (see [Install](#install)).

Then write the roster card into a weekly scheduled task (see
[docs/docs-sweep-card-template.md](docs/docs-sweep-card-template.md)).

## `coderabbit-sweep`

CodeRabbit's review allowance is **per developer, across every repo you own** — at sustained
activity it drops to one review per hour. PRs that open while it is spent get a *Review limit
reached* comment instead of a review, and nothing ever retries them. This sweep is the retry: once
an hour it lists every open PR the account owns, works out which ones have no finished review
against their current head commit, and spends the one available review on **one** of them: a PR
you labelled with one of the card's priority labels if there is one, the single oldest starved PR
otherwise. `coderabbit-priority` is the default label; a card can name others, or none. The label
is the one lever a human has over the queue — it reorders it, and never switches off a guard.

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

Comes with the plugin (see [Install](#install)).

Then write the fleet card into an hourly scheduled task (see
[docs/coderabbit-sweep-card-template.md](docs/coderabbit-sweep-card-template.md)).

## `ci-cost-sweep`

Finds where a repo's CI minutes actually go, then cuts them without cutting coverage. Unlike the
other three this is **user-invokable, not scheduled** — you run it when a bill or a build annoys
you. `/ci-cost-sweep` measures and reports; `/ci-cost-sweep fix` implements the levers on a branch
and opens a PR.

The whole skill exists to enforce one rule: **measure, never assume.** Every plausible CI
optimisation in it has been wrong in some real repo. The worked example that produced it: a
dependency cache that "obviously" saved time was measured at **71 seconds per run slower** than no
cache at all, because the runner reached the package registry about as fast as it reached the
Actions cache — while the browser-binary cache in the same workflow was a clear win. "Caching is
good" is not a finding; per-cache numbers are.

Two measurement traps it encodes, both of which produce confidently wrong answers:

- **The billed-minutes endpoint lies.** `/actions/runs/<id>/timing` returns `total_ms: 0` on repos
  that are definitely being billed. Sum the jobs API and round each job up instead — because
  GitHub bills every job rounded **up** to a whole minute, which makes job *count* a cost driver
  independent of job *duration*.
- **You often cannot A/B from history.** To compare "cache hit" against "cold restore doing real
  work" you need runs of the second kind, and a high hit rate means there may be none — the only
  misses were jobs that short-circuited and did no work anyway. The answer is to *create* the arm
  with a temporary dispatch switch, run it twice against a warm control on the same commit, then
  delete the switch.

On the test side it profiles per-test timings and classifies the time before touching anything,
because the classification picks the lever: **blocked** time (app boots, retry backoff, sleeps)
parallelises past the core count, since a blocked worker holds no core, while CPU-bound time does
not. That distinction is the difference between a 27% and a 33% cut on the same suite. It also
knows where the floor is — when a runner keeps tests within a class sequential, no worker count
takes the suite below its slowest single class.

It will not shorten a retry-policy test's backoff or drop a matrix leg to make a number look
better. Those are coverage cuts wearing a performance costume, and they are on the hard-constraint
list.

### Install

Comes with the plugin (see [Install](#install)).

No card — the repo you point it at is the input.

## `codebase-cleanup`

A full code-quality audit of one repo, then many small fixes. It reads the whole codebase first
(security, correctness, error handling, dead code, types, tests, perf, accessibility, docs drift),
writes every finding down with `path:line` evidence, and sizes each one S, M or L. Every shippable S or
M finding gets its own branch off the default branch and its own PR, with a regression test for
logic fixes and the exact test output in the body. L findings, and S or M findings that can't ship
directly, get an issue for a human, and no code. User-invokable: `/codebase-cleanup`, with config overrides as arguments.

It never merges, never touches production, and re-checks each finding against the current code
before fixing it — a finding that turns out wrong is recorded as rejected, not shipped.

### Install

Comes with the plugin (see [Install](#install)). Defaults (labels, title prefix, branch prefix)
sit in the config table at the top of the skill.

## `onboarding-sweep`

The hourly onboarding-improvement loop. A bot signs up as a brand-new user and writes what it hit
to a Notion "Onboarding dogfood" board, as run rows and finding rows. This sweep reads the board,
turns every actionable finding into a worktree-isolated fix agent that opens a PR, and moves each
finding's status as its PR merges and deploys. It also files findings for failed runs nobody wrote
up. Same split as error-sweep: the pipeline lives here, and the board IDs, caps and repo live in
a per-project card inside the scheduled task.

### Install

Comes with the plugin (see [Install](#install)). Then write a project card into an hourly
scheduled task.

## `unslop`

Cuts AI tells from any prose a person will read, and adds voice back. Repos that want it on for
every contributor, cloud agents included, still carry their own copy in `.claude/skills/unslop/`.
Keep that copy the same as this one.

### Install

Comes with the plugin (see [Install](#install)).

## `simple-issue-description`

Turns a rough bug report, feature request, support note or PR into a short issue about the
problem and the behavior you want, with the implementation detail taken out. User-invokable:
`/simple-issue-description`.

### Install

Comes with the plugin (see [Install](#install)).

## `sync-docs`

Keeps a repo's doc pages honest about the files they describe. Sources carry a `@doc:<key>` comment,
the page that explains them carries a matching `docKey:` marker, and a registry ties the two
together. The audit finds pages whose sources changed, tags nobody registered, pages no index links
to, and lists that no longer match disk. Fix scope repairs them, and never deletes a page or invents
a detail.

It is one skill for every repo. Each repo adds `.claude/sync-docs/config.json`, which says where its
sources and docs are and which checks apply (index, status, tests, inventory), plus its
`registry.json` and, if needed, a `rules.md`. This repo uses it on itself: `docs/project-card-template.md`
documents a card that the pipeline and every adapter *read*, so a new required field in an adapter
would otherwise make that page silently wrong.

### Install

Comes with the plugin (see [Install](#install)). Then give a repo its config and registry (see
[docs/sync-docs.md](docs/sync-docs.md)). Run `/nightforge:sync-docs` to audit,
`/nightforge:sync-docs fix` to repair. Full mechanism: [docs/sync-docs.md](docs/sync-docs.md).
