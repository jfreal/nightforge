# pr-dashboard

A local page with every open PR across your repos in one table, oldest first. It shows the
signals GitHub's own list hides: merge conflicts, checks, CodeRabbit review, the
`verified` / `verify-failed` labels, a merge-risk guess, and a timer for how long each PR
has waited. Buttons on each row act on it:
- **Merge** merges a ready PR. Any other PR gets **Fix and merge** instead, which hands it to Claude to fix and then merge.
- **Review with Claude** shows when a PR has no review of its latest commit. It runs `/code-review` in a Claude session.
- **Send to Claude** opens a Claude Code session on it.
- **Screenshots** opens the PR's Screenery gallery.

The header also shows this week's shipping numbers from the Night Forge weekly report.

The **Routines** tab at `/routines` shows every bot and scheduled job: when each one last ran, whether it is late, what it found and its recent runs. See [Routines](#routines).

Plain Python, standard library only. The only outside dependency is the `gh` CLI, signed in.

## Run it

Double-click `run.cmd`, or:

```bash
python server.py
```

It opens `http://127.0.0.1:8765/` in your browser. Close the window to stop it.

## Keep it running

To have it start at every log on, with no window, run this once in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File tools\pr-dashboard\install-startup.ps1
```

It adds one Task Scheduler task for your user, "Nightforge PR dashboard", and starts it now. It also stops any copy you started by hand.

The task runs `supervise.py` with `pythonw.exe`, which starts `server.py --watch` and starts it again whenever it stops:
- **After a code change**, such as a `git pull`, the server exits on purpose and comes straight back with the new code. Page files load fresh on every refresh. An open tab notices the restart and reloads itself.
- **After a crash**, the supervisor waits 2 seconds and starts it again. If it keeps crashing, the wait doubles each time, up to 5 minutes.

**The PC stays awake while sessions work.** This matters only with `sessionTarget` `"background"`; cloud sessions don't need this PC. Background sessions run on this PC, so they stop when it sleeps. Claude desktop's "keep awake" setting covers only its own sessions, not these. So every minute the server asks `claude agents --json` about the background sessions under `reposRoot`. While any of them has state `working`, it tells Windows not to idle-sleep, and the header shows "keeping this PC awake for N sessions".
- A session waiting on you (state `blocked`) keeps the PC awake for 1 more hour, so you can still answer it from your phone. After that the PC may sleep again.
- If `claude agents` fails or times out, the last answer stands for 10 minutes before the hold is dropped.
- When the last session finishes, the hold is released within a minute.
- It only blocks *idle* sleep. Closing the lid, pressing the power button or picking Sleep still puts the PC to sleep.

Output goes to `dashboard.log` next to the script. To remove the task:

```powershell
powershell -ExecutionPolicy Bypass -File tools\pr-dashboard\install-startup.ps1 -Uninstall
```

| Flag | Effect |
|---|---|
| `--dry-run` | Merge and Send print the command they would run, and do nothing else. |
| `--no-open` | Don't open the browser. |
| `--watch` | Exit with code 75 when a `.py` file changes, so `supervise.py` restarts it. |
| `--port N` | Use another port. |
| `--config PATH` | Read another config file. |

No setup is needed. To change the defaults, copy `config.example.json` to `config.json` (it's
gitignored) and edit it:

| Key | Default | What it does |
|---|---|---|
| `owners` | `["jfreal"]` | GitHub accounts whose open PRs are listed. |
| `excludeRepos` | `["archive"]` | Repo names to leave out. |
| `reposRoot` | the folder this repo sits in | Where local clones live, as `<reposRoot>/<repo>`. Send to Claude starts there. |
| `port` | `8765` | Port on 127.0.0.1. |
| `refreshSeconds` | `60` | How often the page reloads the list. The server asks GitHub at most every half of this. |
| `statsFile` | `~/.claude/scheduled-tasks/pr-shipping-report/report.json` | The Night Forge weekly report's data. If it is missing, the header shows no stats. |
| `statsUrl` | the Night Forge artifact | Where the "report" link in the header goes. |
| `sessionTarget` | `"cloud"` | Where Review with Claude, Fix and merge and Send to Claude run. `"cloud"`: Anthropic's servers, no window, in Claude desktop's session list, and this PC can sleep. `"background"`: this PC, no window, in Claude desktop through Remote Control. `"terminal"`: this PC, a local session in a terminal tab. |
| `keepAwake` | `true` | Keep the PC from idle-sleeping while a background session under `reposRoot` is working (see above). |
| `screeneryProjects` | `{}` | Screenery project per repo name, when it isn't `<owner>/<repo>`. `""` turns a repo off. |

## What the columns mean

Rules live in `github.py`, and `test_github.py` covers each one.

- **Waiting.** Time since the PR opened, with a heat level for the bar: New (under 24 h), Warming (under 72 h), Hot (under 120 h), Burning.
- **What blocks it.** Hard blockers put the PR in **Needs a fix**:
  - merge conflict
  - failed checks
  - `verify-failed` label
  - open, unresolved review comments

  Soft ones only mean it is **Waiting**:
  - checks running
  - no CodeRabbit review yet
  - review is for an older commit
  - GitHub still working out mergeability
- **Ready to merge.** Checks passed (or the repo has none), no conflict, nothing hard blocking, and a review of the head commit: CodeRabbit's, a passed `claude-review` status, or the `verified` label.
- **Risk.** High if it touches migrations or SQL, a schema, auth or security code, or CI workflows, changes more than 20 files, or its description has a GitHub `[!WARNING]`, `[!CAUTION]` or `[!IMPORTANT]` note. Low if it only touches docs. Medium if it changes more than 300 lines or config files like `package.json`, `*.csproj` or YAML. Low otherwise. Details lists the reasons.

## Merge

1. Merge asks you to confirm first.
2. The server then re-reads the PR from GitHub. It refuses if anyone pushed since the page loaded, or if the PR:
   - is a draft
   - has a conflict
   - has checks that have not passed
   - has the `verify-failed` label
   - has open review comments

   Those PRs get Fix and merge instead.
3. Then it runs `gh pr merge <n> -R <owner>/<repo> --<method> --match-head-commit <sha>`, with the method the repo allows (squash first, then merge, then rebase).

## Review with Claude

This button opens a Claude session that runs `/code-review` on the PR. When it's done, Claude sets a commit status named `claude-review` on the head commit:
- `success` if no finding is a real bug or security problem
- `failure` otherwise, with the findings posted as one PR comment

The dashboard counts a `success` the same as a CodeRabbit review of that commit, so the row turns Ready and gets plain Merge. A `failure` shows as "Claude review found problems" and the row gets Fix and merge. CI checks ignore this status, so a failed review never shows as failed checks.

A status belongs to one commit, so a new push leaves the PR unreviewed again. The session never edits code, pushes, approves or merges.

## Fix and merge

Every PR that is not ready gets this button instead of Merge. After you confirm, it opens a Claude session. Claude merges the base branch in, fixes the blockers and pushes. If the PR is a draft, Claude marks it ready. Then Claude waits for checks with `gh pr checks --watch` and merges with the repo's method.

Claude stops and asks you before merging if:
- the risk is high
- the description warns against merging yet
- a fix needs a judgement call
- checks still fail after two tries

It uses its own endpoint, `/api/fixmerge`. An older server that doesn't know it answers "not found", so a click never turns into a direct merge.

## Stats

Every Monday, the `night-forge-weekly` routine writes `report.json`. The header reads it and shows:
- this week's merges, and the week before
- merges per day
- median time to merge
- the all-time count

It also shows the date of the report, linked to its artifact. The page re-reads the file every half hour.

## Screenshots

Screenery keeps each PR's CI screenshots on a channel named `pr-<n>`. After the list loads, the server asks for each PR's gallery once, at `https://app.screenery.dev/v1/projects/<owner>/<repo>/channels/pr-<n>/gallery`. It keeps each answer for 10 minutes.

A row with screenshots gets a **Screenshots (N changed)** button. The gallery shows changed and new shots first. Each changed shot can switch between this PR and `main`, and clicking an image opens it full size.

Public Screenery projects need no token. A private project, or a PR whose CI pushed nothing, shows no button.

## Send to Claude

`sessionTarget` decides where the three Claude buttons' sessions run.

**Cloud (the default).** No window opens, and the work runs on Anthropic's servers, so this PC can sleep. The server runs this in the repo's local clone:

```text
claude.exe --cloud "<Review|Fix and merge|Fix> · <repo> #<n>. <prompt>"
```

`claude --cloud` refuses to run without a terminal (even with only its output redirected), so it gets a console window of its own that is never shown. It hands the session to the cloud and exits within seconds. Its exit code is the only answer it gives back, so the page says the session started but can't link to it.

The session appears in Claude desktop's main session list and at claude.ai/code, where you can watch it and answer its questions. The list names it after the prompt's first words, so each prompt starts with the job and PR number, like `Review · pheidi #1218`. `--cloud` takes no `-n`, and the PR title stays out because its author wrote it.

The session clones the clone's GitHub remote. If the Claude GitHub app isn't installed on the repo, `claude --cloud` uploads the local clone instead, and on Windows that includes uncommitted changes to tracked files. So keep the clones clean, or install the app.

Cloud sessions block `gh pr` and other GraphQL commands, so cloud prompts use `git` and `gh api` REST calls instead. One thing can't be done over REST: taking a PR out of draft. So a cloud Fix and merge on a draft stops and asks you to mark it ready.

**Background.** No window opens, but the session runs on this PC. The server runs this in the repo's local clone, hidden:

```text
claude.exe --bg --remote-control "<repo> #<n> <mode>" --allowedTools … -n … -w pr-<n>-<mode>-<time> "<prompt>"
```

The session runs on this PC in its own worktree. Remote Control makes it appear in Claude desktop and at claude.ai/code, where you can watch it and answer its questions. The page shows the session's id.

**Sign in once.** Background and cloud sessions use the command-line `claude`, which signs in separately from Claude desktop. Run `claude auth login` once in a terminal. Until you do, the dashboard refuses these buttons with that instruction, instead of starting a session that would wait at a login screen you can't see.

**Commands that don't need your OK.** Background and terminal sessions skip the question for the `git` and `gh` commands their prompt names:
- every session: any `git` command, and `gh pr view`, `diff`, `checkout` and `checks`
- Send to Claude: also `gh pr comment`
- Review with Claude: also `gh pr comment`, and `gh api` only for `repos/<owner>/<repo>/statuses/…` (the verdict)
- Fix and merge: also `gh pr comment`, `gh pr merge` and `gh pr ready`

Some forms are refused even though a rule above would allow them:
- force pushes: `--force`, `--force-with-lease`, `-f`, `--mirror` or a `+refspec` anywhere after `git … push`
- `gh pr merge --admin`, which skips branch protection
- `gh api` with `-X` or `--method`, or a path containing `/merge`

These rules match the command's text, so they are a guard rail, not a sandbox. A command written in an unusual form (an alias, say) goes to the normal question instead. Anything else, like editing files or running tests, still asks.

**Terminal.** A new tab runs a local session in a fresh worktree, so the clone's own checkout is untouched:

```text
claude.exe -n "<repo> #<n>" -w pr-<n>-<mode>-<time> "<prompt>"
```

The prompt depends on the row:
- A PR that **needs a fix** gets told to merge the base branch, fix the blockers and push.
- A **ready** PR (the button says "Ask Claude first") gets asked for a review and a plain safe-to-merge answer.
- Anything else gets asked to find out what it needs.

Send to Claude and Ask Claude first prompts end with "Do not merge the PR." Only Fix and merge lets Claude run `gh pr merge`, and only after checks pass.

No prompt contains the PR's title, description or branch name, because the PR's author wrote that text. Each prompt names the PR by owner, repo and number. It tells Claude to read the rest with `gh` and to treat it as untrusted data.

`wt.exe` re-parses its command line, so two characters get swapped: `;` is escaped, and straight double quotes become curly ones. Without that, a PR title could cut the prompt into pieces. The npm `claude.cmd` shim can't be started by `wt.exe`, so the launcher finds the real `claude.exe` next to it.

## Routines

`http://127.0.0.1:8765/routines` has one row per bot, worst first:
- **Needs you**: its last run stopped on a decision.
- **Broken or late**: its last run failed, or it hasn't reported within its cadence plus grace, or it has never reported.
- **Healthy**: the rest.
- **Paused**: you turned its alarms off.

Each row shows:
- how long ago it last ran, and a bar that fills as its next report comes due
- its last result in one line, with counts such as `2 prs · 11 findings`
- runs this week and in total, and a square for each of its last 14 runs
- under **Details**, every run with its summary, links and notes

### Where the data lives

One SQLite file, `~/.nightforge/routines.db` (`%USERPROFILE%\.nightforge\routines.db` on Windows). Set `routinesDb` in `config.json` or the `NIGHTFORGE_DB` variable to put it somewhere else. It's outside the repo on purpose: run summaries carry whatever the bots found. `*.db` is gitignored as well, in case you point it inside a clone. Open it with any SQLite tool. It has three tables: `bots`, `runs` and `cursors`.

### The report every run sends

Every source turns into the same report. Only `bot` and `status` are required:

```json
{
  "bot": "notion-pickup",
  "status": "found",
  "summary": "2 tickets coded: 1 verified, 1 verify-failed",
  "counts": {"prs": 2, "tickets": 2},
  "links": [{"label": "mergetel #655", "url": "https://github.com/jfreal/mergetel/pull/655"}],
  "details": "Longer notes, shown under Notes.",
  "run": "cse_01SSK7fcjmqhWjEYVazmeK8x",
  "started": "2026-10-28T12:55:37Z",
  "finished": "2026-10-28T12:56:53Z"
}
```

| `status` | Means |
|---|---|
| `ok` | It ran, and nothing needs you. |
| `found` | It ran and produced something: PRs, issues, findings. |
| `needs-you` | It stopped on a decision only you can make. |
| `failed` | It didn't finish its job. |

Late and never-reported aren't statuses a bot sends. The page works them out from the clock.

`run` is the run's own id, so a report sent twice is stored once. Links must be `http(s)`. Counts must be whole numbers.

### Reporting from this PC

This covers scripts, Windows tasks and Claude desktop scheduled tasks. Run `botlog.py` once at the end of each run:

```bat
python tools\pr-dashboard\botlog.py report error-sweep-pheidi found "2 new signatures filed, 1 fix PR" ^
  --count issues=2 --count prs=1 --link "pheidi #1262=https://github.com/jfreal/pheidi/pull/1262"
```

For a Claude scheduled task, end its prompt with something like this:

> When you finish, run `python <clone>\tools\pr-dashboard\botlog.py report <bot-id> <ok|found|needs-you|failed> "<one line, plain words>"`, with a `--count name=N` for each number worth tracking and a `--link "label=url"` for each PR or issue you opened. Use `needs-you` if you stopped on a question for John, and `failed` if you couldn't do the job.

### Reporting from the cloud

Cloud routines can't reach this PC, so they post their report as a GitHub issue comment and the dashboard reads it back:

1. Make a **private** repo, for example `jfreal/bot-reports`, and open one issue in it called "Run reports".
2. Set `"reportInbox": "jfreal/bot-reports"` in `config.json`.
3. End each routine's prompt with:

> When you finish, attach `jfreal/bot-reports` with `add_repo` (access "push") and post one comment on its issue #1. Say in a sentence or two what you did, then end the comment with a fenced code block tagged `nightforge-report` holding one JSON object: `{"bot": "<bot-id>", "status": "ok|found|needs-you|failed", "summary": "<one line>", "counts": {...}, "links": [{"label": "...", "url": "..."}]}`.

Only comments by `reportAuthors` count. It defaults to `owners`. The comment's own link is added to the run, so you can jump from the row to the full write-up. The repo has to be private, because anyone who can read it can read the reports.

### Reading sources without changing them

| Key | Reads |
|---|---|
| `actionsRepos` | Scheduled GitHub Actions runs (`event=schedule`) in these `owner/repo`s. One bot per workflow. A failed, timed-out or cancelled run counts as `failed`. |
| `windowsTasks` | Task Scheduler's last run time, exit code and next run time for these task names. Wildcards like `Nightforge*` work. Exit code 0 is `ok`, and anything else is `failed`. A running task waits until it finishes. |
| `reportInbox` | The cloud report comments above. |

The server runs every collector when it starts and again every `collectMinutes` (5 by default). **Collect now** runs them straight away. A source that can't be read shows in a banner above the table, and the others still load.

### Telling it what to expect

A bot appears with its first report. To let the page notice when a bot goes quiet, give it a cadence:

```bat
python botlog.py bot notion-pickup --name "Notion Pickup" --kind "cloud routine" ^
  --schedule "8:47 and 14:47 ET" --every 6h --grace 2h --desc "Ready tickets to reviewed PRs" ^
  --url https://claude.ai/code/routines
```

- `--every` is how often it should report. `--grace` is how long past that it may be before it counts as late, and defaults to half of `--every`, between 15 minutes and 4 hours.
- With no `--every`, the page guesses one from the median gap between its last runs, once it has three, and marks it "(guessed)".
- `--schedule` is only shown, never parsed.
- `--pause` stops the alarms and `--resume` brings them back.

You can also add a bot before it has ever run. If it stays silent past its cadence, the page says "Never reported".

`python botlog.py list` prints the same health view in a terminal. `runs <bot>` lists one bot's history, `remove <bot>` forgets a bot and its runs, and `collect` runs the collectors once.

## Safety

- The server listens on `127.0.0.1` only.
- Each start makes a random token, which the page sends with every Merge and Send.
- The server also checks the request's `Origin`. Another website open in your browser can't press the buttons for you.
- Every request must name `127.0.0.1` or `localhost` as its `Host`. This blocks DNS rebinding, where a website points its own name at your PC to read the page and its token.
- Every response forbids framing (`X-Frame-Options: DENY`, `frame-ancestors 'none'`). Another site can't hide the page under its own and trick you into clicking Merge.
- `gh` calls time out after 90 seconds, so a hung call can't freeze the page.
- No GitHub token is stored. Everything goes through `gh`.

## Test

```bash
python -m unittest test_github.py test_screenery.py test_server.py test_launch.py test_awake.py test_routines.py test_collect.py
```

The tests use made-up PRs and galleries shaped like the real GitHub and Screenery replies. Real PR bodies from private repos don't belong in this public repo.
