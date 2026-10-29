# job-reports

One endpoint every job reports to, wherever it runs: GitHub Actions, cloud routines, Claude
desktop tasks, scripts on any machine. You register each job once with how often it should
report. Each run then posts one short report. The PR dashboard's **Routines** tab
(`tools/pr-dashboard`) pulls the reports, and shows which jobs are late, failing or waiting on you.

It's a Cloudflare Worker with a D1 database. Plain JavaScript, no dependencies.

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

Once, from this folder, signed in with `npx wrangler login`:

```bash
npx wrangler d1 create job-reports            # prints a database_id
cp wrangler.example.toml wrangler.toml        # paste the database_id in; wrangler.toml is gitignored
npx wrangler d1 migrations apply job-reports --remote
npx wrangler secret put ADMIN_TOKEN           # paste a long random string
npx wrangler secret put REPORT_TOKEN          # and another one
npx wrangler deploy                           # prints https://job-reports.<you>.workers.dev
```

On Windows, `python -c "import secrets; print(secrets.token_urlsafe(32))"` makes a good token.

Then point the dashboard at it in `tools/pr-dashboard/config.json`:

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

These cover the checks every report and registration goes through. To try the whole Worker locally:

```bash
cp wrangler.example.toml wrangler.toml
printf 'ADMIN_TOKEN=admin\nREPORT_TOKEN=report\n' > .dev.vars
npx wrangler d1 migrations apply job-reports --local
npx wrangler dev
```

Then use the curl calls above against `http://localhost:8787`.
