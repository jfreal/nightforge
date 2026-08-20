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
