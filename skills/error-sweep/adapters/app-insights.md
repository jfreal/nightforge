# Adapter: app-insights

Exceptions, failed requests, dependencies, and traces from Azure Application Insights via `az`.

<!-- @doc:project-card -->
Card must supply: `app_name`, `resource_group`, and optionally `workspace_id` and `subscription`.

## 1. The window trap — read this before writing any query

`az monitor app-insights query` **defaults `--offset` to 1 hour**, and that timespan is applied by the query API *before* the KQL runs. A `| where timestamp > ago(7d)` inside the KQL cannot widen it — it filters an already-1-hour result set and silently does nothing.

This is exactly how one project ran green every day for a week while missing 6 of 8 live problemIds.

**Pass `--offset` explicitly on every query. Put no timestamp filter in the KQL at all**, so the two cannot drift. Use ISO 8601 durations (`P7D`, `PT24H`) — a KQL-style `24h` is rejected or reinterpreted.

Window guidance: **7 days**, not 24 hours. Dedup is by key in the ledger, so a rolling window is idempotent — re-seeing an old key files nothing. A 24h window only catches errors that fire in the 24h before the cron, which drops everything low-frequency. Wider is not better: 7d ≈ 8 distinct problemIds, 30d ≈ 23, 90d ≈ 70, mostly already-fixed history.

## 2. Every other gotcha, all re-confirmed repeatedly

- `min(timestamp)` aliased to **`first`** or **`last`** is rejected — reserved words. Use `firstSeen`/`lastSeen`. The failure is an opaque `BadArgumentError` naming nothing.
- **`requests.success` is a string** (`'True'`/`'False'`). `where success == false` fails with the same opaque error. `dependencies.success` genuinely *is* a bool. Do not copy the predicate across tables.
- `-o table` **silently prints nothing** for some result shapes. Use `-o json` and flatten.
- A backtick inside a `--query` JMESPath literal is eaten by PowerShell before `az` sees it. Filter in KQL, not JMESPath.
- **Keep KQL on one line.** A multi-line `--analytics-query` runs only line 1 on this Windows `az` and returns a plausible *wrong* table — the worst possible failure mode.
- `az` returns column-oriented tables. Flatten to row objects before doing anything else.
- If `az monitor app-insights query` returns zero rows where you expect data, the workspace-backed path is the fallback: `az monitor log-analytics query -w <workspace_id> --analytics-query "AppExceptions | ..."`. Note the table names differ (`AppExceptions`, not `exceptions`).
- **A handled, logged error arrives in `exceptions`, not `traces`.** The App Insights `ILogger`
  provider ships any `LogError`/`LogWarning` that *carries an exception object* as
  `ExceptionTelemetry`. So a `catch (Exception ex) { _logger.LogError(ex, "Pass failed"); }` produces
  an `exceptions` row and **no** matching `traces` row — searching `traces` for the log message finds
  nothing and reads as "it never happened". Two consequences. First, never conclude a catch block did
  not run because its message is missing from `traces`. Second, this is how you tell handled from
  unhandled without reading the stack: `customDimensions.CategoryName` on an exception row is the
  *logger category*, i.e. the class that caught and logged it. A framework category
  (`Microsoft.EntityFrameworkCore.Query`, `Microsoft.AspNetCore.*`) means nothing of the app's caught
  it; an application category means something did — go read that class's handler before classifying
  it a bug.

## 3. Exceptions

```
exceptions | summarize cnt=count(), firstSeen=min(timestamp), lastSeen=max(timestamp), sampleOuter=take_any(outerMessage), sampleInner=take_any(innermostMessage), sampleMethod=take_any(method), sampleAssembly=take_any(assembly) by problemId, type | order by cnt desc
```

`problemId` **is** the signature. Use it directly; do not invent your own.

## 4. Failed requests — a 404 is not an exception

An exceptions-only sweep is structurally blind to everything the app *handles*. One project served 404 to a live calendar subscriber for 21 hours while every scheduled run reported green.

```
requests | where success == 'False' | summarize cnt=count(), firstSeen=min(timestamp), lastSeen=max(timestamp), avgDurationMs=avg(duration), sampleOperationId=take_any(operation_Id), sampleRole=take_any(cloud_RoleName) by name, resultCode | order by cnt desc
```

- There is no `problemId` on `requests`. Synthesize `req:<normalized route>:<resultCode>` — the `req:` prefix makes collision with a real problemId impossible.
- **Normalize the route first.** `name` embeds path parameters (`GET /api/calendar/<32 hex>`), so a raw key files a fresh issue per subscriber.
- Threshold: **5xx files on the first occurrence; 4xx only at ≥5** on the same normalized route.

## 5. Cold-instance join — the trick that cracks transients

For any transient that resists explanation, join it against instance first-request time:

```
requests | summarize firstSeen=min(timestamp) by cloud_RoleInstance
```

If every occurrence is in the first ~35 s of a fresh instance a few minutes after a deploy, it is a warm-up/pool problem, not randomness. This turned a three-run shrug into a root cause.

## 6. Dependencies and traces

- `dependencies`, 30d, duration > 10 s — surfaces SQL stalls. Remember `dependencies.success` is a real bool here.
- `traces`, `severityLevel >= 3` (and `== 2` for a second pass), 7d.
- A recurring slow query at a stable rate on a small DTU tier is a **cost/tier fact, not a defect**. Note it so a future run does not rediscover it as new; do not file it.

## 6b. Verifying a telemetry *suppression* — fire a synthetic probe with a control

**First check you actually need this section.** It is expensive — a control, a threshold check, a
narrow-window query — and it is only needed when the fix's success condition is *absence from
telemetry*. If the fix changed what the **HTTP response is** — a redirect, a new route, a status-code
change — verify it at the HTTP layer instead and stop:

```bash
curl -s -o /dev/null -w "%{http_code} -> %{redirect_url}\n" "https://<host>/apple-touch-icon-180x180.png"
```

A 301/200 answer is complete proof on its own, needs no control, and costs **no telemetry at all**
because the request never reaches the 404 handler — so it cannot nudge a route over a filing
threshold the way a suppression probe can. One run verified four redirect paths this way in a single
command with zero side effects, the same week the suppression path had manufactured its own issue.
Reach for the control-and-window machinery below only when there is no response to look at.

When a fix's job is to stop something reaching App Insights (a `TelemetryProcessor` that drops
scanner 404s, a sampling rule, a filter), success looks exactly like "the traffic happened to stop".
Waiting for the next natural occurrence can burn days — one project carried "suppression still
unverified" for five runs because the scanner never re-probed the suppressed family.

Do not wait. Issue the request yourself, alongside a **control** the filter is known *not* to match:

```bash
curl -s -o /dev/null -w "%{http_code}" -A "<sweep>-verify" "https://<host>/maps/site.css.map"   # suppressed?
curl -s -o /dev/null -w "%{http_code}" -A "<sweep>-verify" "https://<host>/.DS_Store"           # control
```

Then query a narrow window (`--offset PT30M`) for both. The control is what makes the result
readable: control present + target absent = the filter is live. Both absent means ingestion is
lagging or broken and you have learned nothing yet — retry, do not conclude. Ingestion latency
measured on Azure App Service is **under two minutes**.

Pick a control that already exists in the ledger as known noise, so the test adds no new signature.
Only ever probe paths that 404 by design — never a mutating route.

**Your probe is real telemetry, and a CI triage workflow will file an issue about it.** On one
project the control (`GET /.DS_Store`) sat at 6 hits in the window — one short of the filing
workflow's repeat threshold. The single verification probe made it 7. Two and a half hours later
the daily triage workflow auto-filed an issue for the route, and a fix PR was opened to suppress
it. The sweep manufactured its own finding, and nothing in the issue said so.

So before firing a control, check where it stands against the *filing* threshold, not just against
your ledger — a route already well above the threshold (already filed, already tracked) is safe,
and one sitting just under it is not. Check the target's count too, but read it the other way round:
if suppression works the target probe is never ingested, so it cannot advance the count at all. A
target that *does* appear and crosses the threshold has not proved the suppression — it has proved
the suppression failed, and the issue that gets filed is a real one.

Then re-pick the control every time. Once a suppression PR lands, the control it used stops being
a control — the filter now matches it, and the next verification reads "both absent" and concludes
"ingestion is broken" when in fact the technique lost its reference. Confirm the processor still
does not match your control before trusting the result.

## 7. Deploy drift

Compare `origin/<default branch>` head against the SHA of the last successful deploy workflow run. Prod running stale is a finding — it has silently happened for 3 days before.

**`gh run list --status success` does not mean "conclusion: success".** `--status` filters the run *status* field (`queued` / `in_progress` / `completed`); passing a conclusion value to it returns a stale, wrongly-ordered subset rather than an error. On one run this reported the newest successful deploy as 4 days old while prod was in fact current — a false "prod is stale" finding, which is the exact failure this check exists to avoid.

Ask for the conclusions and filter yourself:

```
gh run list --repo <slug> --workflow deploy.yml --limit 15 \
  --json databaseId,headSha,createdAt,status,conclusion,displayTitle
```

Then read the newest row whose `conclusion` is `success`. Do the same to spot **failed** deploys: a failure that a later push silently corrected still matters, because any fix that shipped in the failed run was not actually live until that later push — which can invalidate a "this is now suppressed/fixed" claim made by an earlier sweep. Check the deploy time of a fix against the last occurrence of the thing it fixes before calling it verified.

Also note `gh run list --json ... --template` chokes on `{{"\n"}}`; pipe the JSON to a parser instead.
