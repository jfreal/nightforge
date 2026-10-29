# job-reports

A hosted website that shows every job you run, wherever it runs: GitHub Actions, cloud routines, Claude desktop tasks, scripts on any machine.
- You register each job once, with how often it should report.
- Each run posts one short report.
- The site shows which jobs are late, failing or waiting on you, worst first, with each job's history.
- It sends you an alert when a job goes wrong, and again when it recovers, even with every browser closed.

It's one Cloudflare Worker with a D1 database. Plain JavaScript, no dependencies. The page is the same one the local PR dashboard shows at `/routines`: it's copied from `tools/pr-dashboard/static` on every build, so the two never drift.

## The site

| Address | What it is |
|---|---|
| `/` | The Routines page. Signed-out visitors are sent to `/login`. |
| `/login` | Sign in with the site's admin token. You stay signed in on that browser for 30 days. |
| `/api/routines`, `/api/runs?bot=<id>` | What the page reads. Open with a session, or with the admin token as a bearer token. |
| `/report`, `/jobs`, `/sync` | For jobs and scripts, with a bearer token (below). |
| `/health` | Health check, no token. |

**Sign-in.** The site has one user, so the password is the admin token.
- A session is a cookie holding an expiry and an HMAC keyed by that token, so nothing is stored on the server.
- The cookie is `HttpOnly`, `Secure` and `SameSite=Strict`.
- Pages can't be framed by other sites.
- Changing the admin token signs every browser out.
- The report token can't sign in.

For a second lock, put [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/applications/configure-apps/self-hosted-public-app/) in front of `/`, `/login` and `/api/*`. Leave `/report`, `/jobs`, `/sync` and `/health` outside it, so jobs can still reach them.

**Alerts.** Every 5 minutes a cron trigger works out each job's state, and sends a message when the state changes:
- ⏰ a job hasn't reported in time
- ✖ its last run failed
- 👤 it needs you
- ✓ it's healthy again

A job that stays broken isn't repeated every 5 minutes. A paused job never alerts. Choose where the messages go with secrets:

| Secret | Sends to |
|---|---|
| `ALERT_SLACK_WEBHOOK` | A Slack channel, through an [incoming webhook](https://api.slack.com/messaging/webhooks) URL. |
| `ALERT_NTFY_URL` | Your phone, through [ntfy](https://ntfy.sh): install the app, subscribe to a topic with a long random name, and set `https://ntfy.sh/<that topic>`. |

Set either, both or neither. Each message links to the site when `DASHBOARD_URL` is set, which `scripts/setup.mjs` does for you.

**Housekeeping.** Once a day, runs older than `RETENTION_DAYS` (365 by default) are deleted, so the database stays well inside D1's free tier. Each job's total run count keeps counting them.

## The two calls

**Register a job** (admin token). Only the fields you send change:

```bash
curl -fsS -X PUT "$NF_URL/jobs/notion-pickup" -H "Authorization: Bearer $NF_ADMIN_TOKEN" \
  -d '{"name": "Notion Pickup", "every": "6h", "grace": "2h", "runsOn": "cloud routine",
       "schedule": "8:47 and 14:47 ET", "does": "Turns Ready tickets into reviewed PRs"}'
```

| Field | Means |
|---|---|
| `name` | What the dashboard calls it. |
| `every` | How often it should report: `15m`, `6h`, `1d`, `1w`. Without it, the dashboard guesses from the gaps between runs. |
| `grace` | How long past `every` it may be before it counts as late. Defaults to half of `every`, between 15 minutes and 4 hours. |
| `does` | One line on what it's for. |
| `runsOn` | Where it runs, shown as written: `github action`, `cloud routine`, `desktop task`… |
| `schedule` | When it runs, shown as written. |
| `url` | Where to manage it. |
| `paused` | `true` stops the late alarms. |

**Report a run** (report token). Only `job` and `status` are required:

```bash
curl -fsS -X POST "$NF_URL/report" -H "Authorization: Bearer $NF_REPORT_TOKEN" \
  -d '{"job": "notion-pickup", "status": "found", "summary": "2 tickets coded, 2 PRs opened",
       "counts": {"prs": 2}, "links": [{"label": "mergetel #655", "url": "https://github.com/jfreal/mergetel/pull/655"}]}'
```

| `status` | Means |
|---|---|
| `ok` | It ran, and nothing needs you. |
| `found` | It ran and produced something: PRs, issues, findings. |
| `needs-you` | It stopped on a decision only you can make. |
| `failed` | It didn't finish its job. |

Optional fields:
- `run`: the run's own id. A report sent twice with the same `run` is stored once.
- `started` and `finished`: ISO times. `finished` defaults to now.
- `details`: longer notes.
- `source`: free text, such as `github-actions`.

Links must be `http(s)`, counts whole numbers, and the summary is cut to 300 characters.

A job that reports before anyone registered it still appears, marked **not registered**.

Other calls, all with the admin token:
- `GET /jobs` lists every job with its last run.
- `DELETE /jobs/<id>` forgets a job and its runs.
- `GET /sync?after=<n>` is what the dashboard pulls.

## Tokens

- **`REPORT_TOKEN`** can only `POST /report`. This is the token to put in GitHub secrets and job prompts. If it leaks, the worst anyone can do is add fake runs.
- **`ADMIN_TOKEN`** can do everything: register, delete and read. It stays with you and the dashboard.

Tokens are compared in constant time. There's no other auth, so treat both as passwords. To rotate one, run `wrangler secret put` again.

## Set it up

You need a free Cloudflare account and Node 18 or newer. From this folder:

```bash
npx wrangler login                     # once: opens the browser to sign in to Cloudflare
node scripts/setup.mjs                 # creates everything and deploys
node scripts/setup.mjs --ntfy https://ntfy.sh/<your topic> --slack <webhook url>   # alerts, any time
```

`setup.mjs` is safe to run again. It:
1. finds or creates the D1 database `job-reports`, and writes `wrangler.toml` from the example (`wrangler.toml` is gitignored)
2. applies the migrations
3. deploys, then writes the site's address into `DASHBOARD_URL` and deploys again
4. sets `ADMIN_TOKEN` and `REPORT_TOKEN` to long random values the first time (`--rotate-tokens` makes new ones), and the alert channels you pass

It saves the tokens to `~/.nightforge/job-reports.json`, outside the repo, and prints the site's address. Open it and sign in with the admin token.

To use your own domain instead of `*.workers.dev`, add it under the Worker's **Settings > Domains & Routes** in the Cloudflare dashboard, then set `DASHBOARD_URL` to it in `wrangler.toml` and deploy.

**Deploy on every push** (optional). `.github/workflows/job-reports.yml` runs the tests on every PR that touches these tools. It also migrates and deploys on every push to `main` once these repository settings exist (Settings > Secrets and variables > Actions):

| Name | Kind | Value |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | secret | An API token with the "Edit Cloudflare Workers" template, plus D1 Edit. |
| `CLOUDFLARE_ACCOUNT_ID` | secret | Your account id, shown on the Workers overview page. |
| `JOB_REPORTS_D1_ID` | secret | The `database_id` in your `wrangler.toml`. |
| `JOB_REPORTS_URL` | variable | The site's address, for links in alerts. Optional. |

Without `CLOUDFLARE_API_TOKEN`, the deploy step is skipped. Secrets set with `wrangler secret put` stay on the Worker across deploys.

**The local dashboard** can keep showing the same jobs. Point it at the site in `tools/pr-dashboard/config.json`:

```json
{"reportUrl": "https://job-reports.<you>.workers.dev", "reportToken": "<ADMIN_TOKEN>"}
```

Or leave `reportToken` out and set `NF_ADMIN_TOKEN` in your environment.

## Reporting from each kind of job

The last step of every job sends one report.

**GitHub Actions.** Add `NF_URL` and `NF_REPORT_TOKEN` as repository or organization secrets, then add a final step. `if: always()` makes a failed run report too:

```yaml
      - name: Report to job-reports
        if: always()
        env:
          NF_URL: ${{ secrets.NF_URL }}
          NF_REPORT_TOKEN: ${{ secrets.NF_REPORT_TOKEN }}
          STATUS: ${{ job.status == 'success' && 'ok' || 'failed' }}
        run: |
          curl -fsS -X POST "$NF_URL/report" -H "Authorization: Bearer $NF_REPORT_TOKEN" \
            -d "{\"job\": \"pheidi-app-insights-triage\", \"status\": \"$STATUS\", \"run\": \"$GITHUB_RUN_ID\",
                 \"summary\": \"Run #$GITHUB_RUN_NUMBER $STATUS\", \"source\": \"github-actions\",
                 \"links\": [\"$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID\"]}"
```

**Cloud routines and other agents.** End the prompt with:

> As your very last step, report what this run did, even if it failed:
> `curl -fsS -X POST https://job-reports.<you>.workers.dev/report -H "Authorization: Bearer <REPORT_TOKEN>" -d '{"job": "<id>", "status": "<ok|found|needs-you|failed>", "summary": "<one plain sentence>", "counts": {...}, "links": [{"label": "...", "url": "..."}]}'`
> Use `found` if you produced PRs, issues or findings, `needs-you` if you stopped on a question for a person, and `failed` if you couldn't do the job.

A cloud session can only reach the Worker if its environment's network access allows it. If the curl gets a 403 from the proxy, add your Worker's host, or `*.workers.dev`, under the environment's **Network access**, in **Allowed domains**. Open it from the cloud environment menu in the session's title bar, then **Edit**. See [network access](https://code.claude.com/docs/en/cloud-environments#network-access). The token in a prompt is readable by anyone who can read the prompt, which is why it's the report-only one.

**Claude desktop tasks and scripts on your PC** can use `tools/pr-dashboard/botlog.py`. With `reportUrl` set, it sends to the Worker:

```bat
python tools\pr-dashboard\botlog.py report ga-traffic-report found "3 audit findings filed" --count issues=3
python tools\pr-dashboard\botlog.py job add ga-traffic-report --name "GA traffic report" --every 1w --runs-on "desktop task"
```

`botlog.py desktop --apply` adds that report step to every desktop task's prompt at once (see `tools/pr-dashboard/README.md`).

## Test

```bash
node --test test/*.test.mjs
```

These cover the checks every report and registration goes through, the health rules (the same cases as the local dashboard's `test_routines.py`), alert changes and sign-in sessions. To try the whole site locally:

```bash
cp wrangler.example.toml wrangler.toml
printf 'ADMIN_TOKEN=admin\nREPORT_TOKEN=report\n' > .dev.vars
npx wrangler d1 migrations apply job-reports --local
npx wrangler dev --test-scheduled
```

Then:
- Open `http://localhost:8787` and sign in with `admin`.
- Use the curl calls above against the same address.
- Run the alert check by hand with `curl "http://localhost:8787/__scheduled?cron=*/5+*+*+*+*"`.
