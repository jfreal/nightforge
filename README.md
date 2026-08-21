# nightforge

Agentic "dark factory" tools and skills that I use across my repos.

## `ELI10` output style

A Claude Code output style for end-of-day brains: plain English, jargon defined once, every report
structured as *what I did / did it work (with proof) / what you do next*. Decisions come as two
options max with a recommendation. Paths, commands, and code stay exact.

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

Then pick it in `/config`, or set `"outputStyle": "ELI10"` in
`%USERPROFILE%\.claude\settings.json` to make it the default. The standalone
`/output-style` command is gone — as of 2.1.237 it just redirects into `/config`. Either way
the style is part of the system prompt, so it takes effect on a new session or after
`/clear`, not mid-conversation.

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
docs/project-card-template.md   the per-project input, and how to fill it in
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
