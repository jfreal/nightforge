# pr-dashboard

A local page with every open PR across your repos in one table, oldest first. It shows the
signals GitHub's own list hides: merge conflicts, checks, CodeRabbit review, the
`verified` / `verify-failed` labels, a merge-risk guess, and a timer for how long each PR
has waited. Buttons on each row act on it:
- **Merge** merges a ready PR. Any other PR gets **Fix and merge** instead, which hands it to Claude to fix and then merge.
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

| Flag | Effect |
|---|---|
| `--dry-run` | Merge and Send print the command they would run, and do nothing else. |
| `--no-open` | Don't open the browser. |
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
- **Ready to merge.** Checks passed (or the repo has none), no conflict, nothing hard blocking, and either CodeRabbit reviewed the head commit or the PR has the `verified` label.
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

It opens a new Windows Terminal tab in the repo's local clone and runs:

```text
claude.exe -n "<repo> #<n>" -w pr-<n>-<time> "<prompt>"
```

The `-w` worktree keeps the clone's own checkout untouched.

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
python -m unittest test_github.py test_screenery.py test_server.py
```

The tests use made-up PRs and galleries shaped like the real GitHub and Screenery replies. Real PR bodies from private repos don't belong in this public repo.
