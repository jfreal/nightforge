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

**Query the route's successes in the same breath as its failures.** Dropping `where success == 'False'`
and summarizing by `name, resultCode` costs one extra query and often hands you the mechanism for free,
because the ordering of the good and bad answers is the finding. On one run `POST /api/injuries`
showed a single `201` six seconds before a run of eight `500`s from the same user: the create path
worked and every *subsequent* save of that same record failed, which pointed straight at what the
client echoes back on a repeat write rather than at the handler's happy path. The failure pass alone
shows eight 500s and no shape at all.

**`success` is app-writable, so the failure pass is not the whole 4xx picture.** An
`ITelemetryProcessor` in the app can set `RequestTelemetry.Success = true` on a request that really
answered 4xx; the row keeps its true `resultCode` and vanishes from `where success == 'False'`. This
is a legitimate thing for an app to do — it stops an expected, self-correcting 4xx from competing
with real failures — but it silently blinds a sweep that only ever runs the failure pass, which then
reports "those errors stopped" when they merely turned green.

**A multi-leg flow that dies between the legs produces no error row at all.** Every pass in this
adapter keys off a status code, and the start of a redirect flow answers `302` whether or not
anything ever comes back. An OAuth sign-in, a payment hand-off, an email-confirmation bounce — all
of them can be 100% broken while `exceptions`, `requests | where success == 'False'` and every trace
category stay perfectly clean, because the failure happens on the far side of a redirect the server
never sees.

So for any hand-off flow, **count the legs against each other** rather than looking for a bad status:

```kusto
requests | where name has_any ('<start>','<provider return>','<your callback>') | summarize cnt=count() by bin(timestamp, 1d), name, resultCode | order by timestamp asc
```

A start-to-callback ratio that collapses is the finding. On one run this turned a lone 4-hit `429`
— under the filing threshold, and correct behaviour by the limiter that emitted it — into a 30-day
funnel of 88 sign-in starts, 4 returns and 2 completions, with a raw timeline showing one user
pressing the button eleven times and then signing in with the other provider on the first try. The
`429` was the only thing any error pass could see, and it was the least interesting part of it.

Two guardrails when you find one. First, **prove the request you emit is well-formed before blaming
the provider** — one `curl` on the start route reads the `Location` header, and a second on that URL
shows whether the provider rejected the parameters or merely asked the visitor to log in. Second,
**say that the cause is not observable** when it isn't: what happens between your redirect and the
provider's answer leaves no server-side trace, so the honest classification is `noise` with a
measured funnel, not a guessed root cause.

So for any route you are actively watching, **query it by URL and result code, never by `success`**:

```
requests | where name has '/<route>' | summarize cnt=count(), firstSeen=min(timestamp), lastSeen=max(timestamp) by name, resultCode, success, tostring(customDimensions.<Marker>) | order by lastSeen desc
```

And when such a rule stamps a *reason* dimension, read what the code actually tests before trusting
the name. One of these labelled every sessionless 400 `session-restart-renegotiation` on the strength
of three facts — status 400, path prefix, header absent — and never checked whether a 200 followed.
The pathological case and the benign case therefore carry the identical reassuring label. **A
dimension asserting a recovery is not evidence of one**; get that from the raw per-request timeline.

And the label was not merely uninformative — it pointed at the wrong *cause*. Three runs derived the
mechanism behind those 400s from the repo's own handshake tests ("no session header AND not an
initialize") and were confident about it. When a diagnostics middleware finally logged the SDK's own
error body, the real reason was something no test covered: `The MCP-Protocol-Version header value
'2026-07-28' is not supported`. **When the status code is produced inside a third-party SDK rather
than by your own code, a mechanism inferred from your tests is a hypothesis, not a finding** — your
tests only pin the cases someone thought of. Get the SDK's own error text into a log and read it
before writing a root cause down. Until then, the honest report says "reason not observable", which
is what makes shipping the logging the correct next step rather than a detour.

That episode has a second, portable half. The rejected value was a *protocol version*, and the reason
the server could not speak it was a floating package reference floored at a major: `Version="1.*"`
resolves to the newest 1.x forever, so a peer that moves to a newer protocol revision can never be
met. **On any version-negotiation failure, compare the supported set compiled into the resolved
package against the newest published package** — for .NET that is a two-minute check with no build:

```bash
curl -s "https://api.nuget.org/v3-flatcontainer/<package>/index.json" | python -c "import json,sys; print(json.load(sys.stdin)['versions'][-10:])"
# then read the version strings out of each DLL (they are UTF-16 in the metadata heap):
python -c "import re;b=open('<pkg>.dll','rb').read();print(sorted(set(re.findall(r'20\d{2}-\d{2}-\d{2}',b.decode('utf-16-le','ignore')))))"
```

## 5. Cold-instance join — the trick that cracks transients

For any transient that resists explanation, join it against instance first-request time:

```
requests | summarize firstSeen=min(timestamp) by cloud_RoleInstance
```

If every occurrence is in the first ~35 s of a fresh instance a few minutes after a deploy, it is a warm-up/pool problem, not randomness. This turned a three-run shrug into a root cause.

**The same join is what separates ordinary boot cost from a real latency regression.** Slow *successful*
requests are worth a pass of their own (`requests | where duration > 5000`), because a 200 that took
30 s is still a defect and no failure pass will ever show it. But most of what that pass returns is
the app booting. Project the instance and compare each row against that instance's `firstSeen`: rows
where the two are equal are the first request on a cold instance and are the known cost of a restart.
Rows past the age threshold below are the findings; hours-old instances are simply the clearest
of them. On one run, 18 of 19 slow rows
were boot cost and the single warm one — 10.6 s with every SQL dependency under 10 ms, so not the
database — was the only thing worth writing down.

**Do not use `timestamp == firstSeen` as the cold test — it is far too strict.** Equality catches
only the literal first request on an instance; a boot serves several requests inside its first few
seconds, and every one after the first is then labelled *warm*. On one run that split returned 22
"warm" rows out of 46, and about seventeen of them were 8–30 s into an instance's life — a
`/_blazor/negotiate` 13 s after boot, four `GET /` inside the same 16 s window, a `/plan` 24 s in.
Only five rows were genuinely warm. Compare the **age**, not the timestamps:

```kusto
requests
| where duration > 5000
| join kind=leftouter (
    requests | summarize firstSeen=min(timestamp) by cloud_RoleInstance
  ) on cloud_RoleInstance
| extend instanceAgeSec = datetime_diff('second', timestamp, firstSeen)
| where instanceAgeSec > 60
| order by duration desc
```

The join is the whole point and has to be written out: `firstSeen` comes from the per-instance
summary above, so a bare `| extend instanceAgeSec = ...` fragment has no input table and no
`firstSeen` in scope. `leftouter` keeps a row whose instance never summarised rather than dropping
it silently — such a row has a null `instanceAgeSec` and fails the filter, so inspect it by hand.

**Sixty seconds is the threshold for the whole section** — do not pair this filter with a looser
"up for hours" rule in prose, or a row 61 seconds past `firstSeen` gets two contradictory verdicts.
It is generous on purpose: a slow request in a boot's first minute is still boot cost, and the pass
exists to find the row that is not. Raise it if a project boots slowly, but raise it in both places.

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

Then read the newest row whose `conclusion` is `success`. **Also read the `status` field on every row, not
just `conclusion`.** Two silent-staleness shapes never appear as a failed deploy: a run that ends
`conclusion: startup_failure` with **zero jobs** (the workflow never started, so nothing is red to
look at), and a run left in `status: queued` indefinitely. On one project a push to the default
branch produced both — one `startup_failure` and one run still `queued` 44 hours later — and prod
sat one commit stale for 9.5 hours until an *unrelated* later push carried the change out. Nothing
in the deploy history said "failed"; the newest `success` row simply predated the merge. Confirm the
newest successful deploy's SHA actually **contains** the newest merge (`gh api
repos/<slug>/compare/<merged-sha>...<deployed-sha>` → `status: ahead`, `behind_by: 0`), and flag any
non-completed run as a finding a human must clear. Do the same to spot **failed** deploys: a failure that a later push silently corrected still matters, because any fix that shipped in the failed run was not actually live until that later push — which can invalidate a "this is now suppressed/fixed" claim made by an earlier sweep. Check the deploy time of a fix against the last occurrence of the thing it fixes before calling it verified.

Also note `gh run list --json ... --template` chokes on `{{"\n"}}`; pipe the JSON to a parser instead.
