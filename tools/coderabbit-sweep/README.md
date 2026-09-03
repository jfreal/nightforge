# coderabbit-sweep (script)

A deterministic Python port of `skills/coderabbit-sweep/SKILL.md`. Same pipeline, same hard
constraints, no model in the loop — so a run costs API calls instead of ~20k tokens.

CodeRabbit enforces one review per hour, account-wide. PRs that land while the allowance is spent
get a *Review limit reached* comment and nothing ever retries them. This is that retry: it finds
the starved PRs, picks the single oldest one, and spends the one available review on it.

## Set it up

Only `config.example.json` is tracked; `config.json` is gitignored because it holds
machine-specific paths. On a fresh checkout, make your own copy first — both entry points
fail without it:

```bash
cp config.example.json config.json
```

Then edit `config.json`: set `owners` to the GitHub account(s) to sweep, and `stateDir` to
where the ledger, board, and reports should live. `stateDir` defaults to `"."`, which puts
them next to the config file — fine to start with, and it means `board-template.html` in this
directory is found without any further setup. Point `boardTemplate` elsewhere if you keep the
template somewhere else.

## Run it

```bash
python sweep.py --config config.json
```

Or double-click `run.cmd`.

| Flag | Effect |
|---|---|
| `--dry-run` | Classify, derive the gate, render the board. Never comments, never writes the ledger. Board goes to `board-dryrun.html`. |
| `--no-poll` | Fire, then skip the 5-minute confirmation poll. The next run reconciles. |
| `--open` | Open the board in a browser when the run ends. |
| `--only OWNER/REPO#N` | Fire at this PR instead of the ranked pick. Overrides the *ranking* only — the throttle gate, fail-closed, cooldown and give-up all still apply, and an ineligible target refuses rather than falling back. |
| `--verbose` | Per-PR classification and every gate source. |

A sweep that completes exits 0 — including a gated, fail-closed, or nothing-to-do run; problems
are printed, written into the report, and shown on the board rather than raised. Startup can
still exit non-zero: an unreadable or invalid config, bad CLI arguments, or a crash.

## Scheduled

A Windows scheduled task named **`CodeRabbit Sweep`** runs it every 15 minutes.

```
<your-python>\pythonw.exe "<stateDir>\sweep.py" --config "<stateDir>\config.json"
```

Find your interpreter with `(Get-Command python).Source` and swap `python.exe` for
`pythonw.exe` in the same folder.

`pythonw.exe` means no console window ever flashes. Because it has no console of its own, every
`gh.exe` child would otherwise allocate one — `sweep.py` passes `CREATE_NO_WINDOW` to stop that.

Only one live sweep runs at a time. The task setting below prevents it overlapping itself, and a
`sweep.lock` file in `stateDir` prevents an on-demand run colliding with a tick that is mid-poll.
A second live run logs who holds the lock and exits without firing. Dry runs do not take it.

Settings that matter:

- **`MultipleInstances: IgnoreNew`** — a slow run can never overlap the next tick. This is what
  keeps "exactly one trigger per run" true when a run takes longer than the interval.
- **`StartWhenAvailable`** — a tick missed while the machine was asleep runs on wake.
- **Battery allowed**, and it will not stop when unplugged.
- **20-minute execution limit** — a run is ~30s gated, ~6min when it fires and polls.

Every run appends to `<stateDir>\sweep.log` (rotated at 2 MB), so an unattended run leaves a trace
even though nothing was watching. An unhandled crash writes `CRASHED` plus the traceback there too.

### Why 15 minutes and not hourly

The SKILL says hourly. That rule existed because each run cost ~20k tokens and cron cannot express
75 minutes. Neither applies to a script that costs nothing to run. The gate is what prevents a bad
fire, not the interval — and a shorter interval claims the slot sooner after it opens, instead of
leaving it idle for up to an hour. To go back to hourly, change the task's repetition interval.

### Where the script actually lives

The scheduled task points at a **copy** in `stateDir`, not at this repo, because this repo is often
checked out in a transient git worktree. This directory is the source of truth; after changing
`sweep.py` here, push the copy to whatever `stateDir` your `config.json` names:

```bash
cp sweep.py board-template.html README.md "<your-stateDir>/"
```

`board-template.html` is in that list because `boardTemplate` resolves under `stateDir` by
default. Without it the board — the run's actual deliverable — fails to render on every run,
and only a line in the report says so.

**`stateDir` needs its own `config.json`.** The scheduled command reads
`"<stateDir>\config.json"`, and that copy — not the one in this repo — is authoritative for the
task. Create it once, when you first deploy:

```bash
cp config.example.json "<your-stateDir>/config.json"
```

then edit it there. It is left out of the routine sync above on purpose: the installed config
names real paths and may differ from the one you develop against, so re-copying it every time
would overwrite your deployment settings.

## Output

Each output path is configurable and resolved independently; an absolute value is honoured as
given. By default they all land in `stateDir` — which points at the existing scheduled-task
folder, so the script inherits the ledger the skill was already keeping:

| File | What |
|---|---|
| `board.html` | **The deliverable.** One dense table: every unmerged PR, its coverage state, whether a throttle notice sits on its current head, and the sweep's verdict on it this run — `fired now`, `#N in queue`, `held` (gate closed), `in cooldown`, `give-up`. The header self-reports staleness: more than 40 quiet minutes on a 15-minute tick paints a red warning that the scheduled task has stopped. Open it with `file://`, or serve the folder. |
| `runs.html` | Run log — one row per run, newest first, linked from the board footnote. Each row expands to the full audit: every candidate considered, why it was not fired, and any problems the run hit. |
| `runs.json` | Data behind the run log. Last 500 runs. |
| `ledger.json` | Memory between runs: `throttledUntil`, the last 12 fires, `lastRun`. |
| `<reportsDir>/<YYYY-MM-DD>.md` | Text audit trail, one block appended per run. `reportsDir` defaults to `reports`. |

The board and run log are plain self-contained HTML files. They are not published anywhere — link
to them on disk, or point a static file server at `stateDir`.

## Config

`config.json`, alongside the script. This is the fleet card in machine-readable form.

| Key | Meaning |
|---|---|
| `owners` | GitHub owners to sweep. More than one is fine; the one-trigger cap still spans all of them. |
| `excludeRepos` | Repo names (not slugs) to skip. |
| `excludePRs` | `repo#number` or `owner/repo#number` to skip. |
| `includeDrafts` | `false` — CodeRabbit answers drafts with *Review skipped*. |
| `triggerPhrase` | Posted alone as the comment body. |
| `cooldownMinutes` | A PR fired inside this window is not re-fired. 90. Doubled by `barrenBackoffMax`. |
| `pausedQuietMinutes` | While CodeRabbit has paused a branch, hold the PR until its head commit is this old. 120. |
| `barrenBackoffMax` | How many times a PR's cooldown may double after consecutive reviews that found nothing. 3, so 90m → 3h → 6h → 12h and no further. |
| `searchLimit` | Passed to `gh search prs`. Never set it below the fleet's real size. |
| `retention` | How many `fired` entries the ledger keeps. 40 — it must outlast the longest backoff window, because a trimmed entry is a cooldown that silently stops applying. |
| `oversizeFiles` | Over this many changed files CodeRabbit refuses outright; such a PR ranks last within its tier. 300. |
| `pollRounds` / `pollInterval` | Confirmation poll. 11 × 30s ≈ 5 minutes, ending on a fetch. |
| `stateDir` | Where every output above lives. Relative paths resolve against the config file. |

## What it enforces

Each of these is a rule from the SKILL, implemented rather than remembered:

- **One trigger per run, fleet-wide.** Never two, on any outcome.
- **The only write is one comment.** No pushes, merges, closes, or edits to CodeRabbit's comments.
- **The gate is the `max` of five sources**, four of which leave no rate-limit block: the ledger
  window, the newest block's reset, the newest completed pass's attempt + 60min, a live
  `review in progress` marker + 60min (capped at 60min), and a failed attempt + 60min.
- **A 60-second firing margin.** Derived gates are systematically early; `now < gate + 60s` is
  treated as gated.
- **Attempt time is `max(commit committer date, PR createdAt)`**, not the review time.
- **A closed-PR sweep** whenever the open-fleet gate reads expired — a spend on a since-merged PR
  is otherwise invisible — classified in full, not scanned for blocks.
- **A re-scan immediately before firing**, with the gate re-derived from it.
- **The completeness contract**: a pass at head **OR** a `recent_review` block naming head. Not
  both. A review object counts as a pass only when its body is non-empty and either starts with
  `**Actionable comments posted:` or contains `Outside diff range comments` — empty-bodied reviews
  are CodeRabbit replying to a thread, and PRs carry them at head in quantity.
- **Both rate-limit wordings, with the unit captured**, and the size refusal (`Too many files!`,
  no countdown) told apart from a throttle.
- **`--paginate` everywhere**, with concatenated JSON arrays decoded properly and a zero-byte
  response raised as an error. `[]` is a valid answer; nothing is one.
- **Give-up after two refusals**, so one permanently unreviewable PR cannot eat every slot.
- **A churn hold on a paused branch.** CodeRabbit pauses *automatic* review on a branch under
  active development; a manual trigger still works, so the sweep used to spend reviews straight
  through the pause. The marker is sticky and never clears itself, so it cannot be a permanent
  block — the PR waits only until its head commit is `pausedQuietMinutes` old.
- **A barren backoff.** A PR goes stale on every push and stale PRs rank oldest-first, so an old
  branch someone keeps pushing to wins the queue every time and can eat the fleet's whole budget
  while returning nothing. Each consecutive review that finds nothing doubles that PR's cooldown,
  up to `barrenBackoffMax`; any review that finds something resets it. The streak lives outside
  `fired` for the reason refusals do — a trimmed ledger would reset it forever.
- **Zero findings is a number, not a blank.** CodeRabbit posts a review object only when it has
  actionable comments, so a completion carried by the summary comment alone is recorded as
  `findings: 0`. Because the summary can move a moment before the review object lands, such an
  entry keeps its baseline and gets exactly one re-check on the next run before the backoff
  trusts the count. A review *object* clears the streak on its kind alone — the
  `Outside diff range comments` form has no `**Actionable comments posted: N**` header to parse,
  and an unparsed count is unknown, never zero.
- **Reserve the ledger entry before posting**, so a run killed mid-fire cannot cause a second
  trigger inside a spent hour.
- **Reconcile last run's `pending` / `unknown` entries first**, and only when a baseline was
  recorded — without one there is nothing to judge "new" against — and never an entry young
  enough to still belong to a sweep that is running right now.

## What it deliberately does not do

- No LLM judgement. If a PR's state is ambiguous, it is reported, not guessed at.
- No board publishing to claude.ai. The board is a local file.
- No evidence-log writing. `EVIDENCE.md` stays a human/skill artifact.

## Requirements

`gh` authenticated (`gh auth status`), Python 3.9+. No third-party packages.
