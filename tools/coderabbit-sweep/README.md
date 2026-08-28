# coderabbit-sweep (script)

A deterministic Python port of `skills/coderabbit-sweep/SKILL.md`. Same pipeline, same hard
constraints, no model in the loop — so a run costs API calls instead of ~20k tokens.

CodeRabbit enforces one review per hour, account-wide. PRs that land while the allowance is spent
get a *Review limit reached* comment and nothing ever retries them. This is that retry: it finds
the starved PRs, picks the single oldest one, and spends the one available review on it.

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
| `--verbose` | Per-PR classification and every gate source. |

Exit is always 0; problems are printed, written into the report, and shown on the board.

## Scheduled

A Windows scheduled task named **`CodeRabbit Sweep`** runs it every 15 minutes.

```
C:\Python314\pythonw.exe "<stateDir>\sweep.py" --config "<stateDir>\config.json"
```

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
`sweep.py` here, push the copy:

```bash
cp sweep.py config.json README.md "C:/Users/John/.claude/scheduled-tasks/coderabbit-sweep/"
```

## Output

Everything lands in `stateDir` (by default the existing scheduled-task folder, so the script
inherits the ledger the skill was already keeping):

| File | What |
|---|---|
| `board.html` | **The deliverable.** One dense table: every unmerged PR, its coverage state, and whether a throttle notice sits on its current head. Open it with `file://`, or serve the folder. |
| `runs.html` | Run log — one row per run, newest first, linked from the board footnote. |
| `runs.json` | Data behind the run log. Last 500 runs. |
| `ledger.json` | Memory between runs: `throttledUntil`, the last 12 fires, `lastRun`. |
| `reports/<YYYY-MM-DD>.md` | Text audit trail, one block appended per run. |

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
| `cooldownMinutes` | A PR fired inside this window is not re-fired. 90. |
| `searchLimit` | Passed to `gh search prs`. Never set it below the fleet's real size. |
| `retention` | How many `fired` entries the ledger keeps. 12. |
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
