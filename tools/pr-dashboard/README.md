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

This button opens a Claude session that runs `/code-review` on the PR. When it's done, Claude posts one PR comment with the findings (or "No findings") and sets a commit status named `claude-review` on the head commit:
- `success` if no finding is a real bug or security problem
- `failure` otherwise

The comment's last line repeats the verdict as a hidden marker: `<!-- claude-review: success <head sha> -->`. Cloud sessions can post comments, but GitHub may refuse their status call, so when there is no status the dashboard reads the newest marker instead. Only markers in comments by your own `gh` account count, and only for the current head commit, so a PR author can't fake one. It's a guard rail: any session acting as you could still write one.

The dashboard counts a `success` the same as a CodeRabbit review of that commit, so the row turns Ready and gets plain Merge. A `failure` shows as "Claude review found problems" and the row gets Fix and merge. CI checks ignore this status, so a failed review never shows as failed checks.

Both the status and the marker belong to one commit, so a new push leaves the PR unreviewed again. The session never edits code, pushes, approves or merges.

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

### When a PR has no screenshots

A Screenery project holds at most 100 channels. Each CI push makes two, `pr-<n>` and one named after the branch, and nothing deletes them when the PR closes. Once a project is full, new PRs' shots still upload but get no channel, so they show no button. The CI log says `No channel was created for "pr-<n>" (channel_limit)`.

`prune_screenery.py` deletes the channels closed PRs left behind: `pr-<n>` of a closed PR, and a closed PR's branch channel when no open PR uses that branch. It never touches protected channels, the default branch or `latest`. It needs a Screenery publish token for the project, and it changes nothing without `--apply`:

```powershell
$env:SCREENERY_TOKEN = "<token>"
python prune_screenery.py jfreal/pheidi jfreal/auxf
python prune_screenery.py jfreal/pheidi jfreal/auxf --apply
```

To stop it filling up again, add a rule to the project in the Screenery console: on `pr.closed`, prune `pr-{pr.number}` and `{branch}` after 0 days.

## Send to Claude

`sessionTarget` decides where the three Claude buttons' sessions run.

**Cloud (the default).** No window opens, and the work runs on Anthropic's servers, so this PC can sleep. The server runs this in the repo's local clone:

```text
claude.exe --cloud "<Review|Fix and merge|Fix> · <repo> #<n>. <prompt>"
```

`claude --cloud` refuses to run without a terminal (even with only its output redirected), so it gets a console window of its own that is never shown. It hands the session to the cloud and exits within seconds. Its exit code is the only answer it gives back, so the page says the session started but can't link to it.

The session appears in Claude desktop's main session list and at claude.ai/code, where you can watch it and answer its questions. The list names it after the prompt's first words, so each prompt starts with the job and PR number, like `Review · pheidi #1218`. `--cloud` takes no `-n`, and the PR title stays out because its author wrote it.

The session clones the clone's GitHub remote. If the Claude GitHub app isn't installed on the repo, `claude --cloud` uploads the local clone instead, and on Windows that includes uncommitted changes to tracked files. So keep the clones clean, or install the app.

Cloud sessions also need your GitHub account connected to Claude, for their `gh api` calls. Connect it once at claude.ai/code. Without it, the first `gh api` call in a Review or Fix fails.

A cloud Fix checks out the PR's real head commit (`pull/<n>/head`) and pushes only when that head lives in the same repo. A PR from a fork stops and asks you, because the session can't push to someone else's fork.

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
python -m unittest test_github.py test_screenery.py test_server.py test_launch.py test_awake.py test_prune_screenery.py
```

The tests use made-up PRs and galleries shaped like the real GitHub and Screenery replies. Real PR bodies from private repos don't belong in this public repo.
