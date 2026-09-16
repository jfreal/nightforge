# Adapter: supabase

Postgres, API, auth, and edge-function errors plus security advisories, via the Supabase MCP tools.

<!-- @doc:project-card -->
Card must supply: `project_ref`.

## 1. Which tool

`get_logs` is **not exposed on every connection** — confirmed absent on 2026-08-15/16. Use `query_logs` (ClickHouse SQL over the unified `logs` stream) and fall back to `get_logs` only if `query_logs` is missing. If neither is available, that is a failed collector: say so in the report rather than reporting zero errors.

Retention is **24 hours**. A window wider than that silently returns 24h of data — never claim a 7-day Supabase window.

## 2. Filters — these streams are extremely noisy

Keep only — but read **§7 first** for the actual `log_attributes` key names. Where a row below names
a field it is the *literal* map key, so write it out in full: `log_attributes['parsed.error_severity']`.
The bare `log_attributes['error_severity']` returns `''` silently rather than erroring:

| Service | Keep | Drop |
|---|---|---|
| `postgres` | `log_attributes['parsed.error_severity']` in ERROR / FATAL / PANIC | **every `LOG` line.** Checkpoints, logical decoding, `could not receive data from client`, `unexpected EOF on standby connection` are all routine |
| `api` | status >= 500 | 4xx — usually RLS doing its job. Flag a 4xx only if it is high-volume on a path the app itself calls |
| `auth` | errors and stack traces | warnings |
| `edge-function` | errors and stack traces | info/log |

Realtime warnings are routine background noise; count them in the report, do not triage them.

## 3. Security advisors

```
get_advisors(type="security")
```

A **new class** of advisory is a finding — a table with RLS disabled is exactly the bug class these apps care most about. A moving *count* within an already-triaged class (e.g. SECURITY DEFINER RPCs 52 → 56) is not, unless a table crosses onto the `rls_enabled_no_policy` list. Report the count delta, triage only the new class.

## 4. Migration-lag false positives

`column X does not exist` / `function X does not exist` from PostgREST, right after a deploy preview goes up, usually means the preview hit prod PostgREST before its migration was applied. Check whether the object exists **now** and whether the PR merged. If both are true it self-resolved — classify **external**, not a code defect.

`404 flow_state_not_found` on `/token` from a `localhost:*` referrer is local dev, not production.

## 5. Never

No `supabase db push`. No MCP `apply_migration`. No DDL against the hosted project — from this sweep or from any fix agent it spawns. A branch that applies its own migration before merging poisons migration history for every other checkout.

## 6. `permission denied for table X` (42501) is a COLUMN privilege, not RLS

Confirmed 2026-08-22 on `auxf`. RLS denies by returning **zero rows**; SQLSTATE **42501** from
PostgREST means the role lacks a `SELECT` privilege — and on a table that uses **column-level
grants**, it fires when the request's `select=` list names one ungranted column, even though every
other column and every other caller works. So a table with thousands of 200s can still 403 a single
query shape, and the log line names only the table, never the offending column.

Find the offending column instead of guessing:

```sql
select c.column_name,
       bool_or(cp.grantee='authenticated' and cp.privilege_type='SELECT') as auth_select,
       bool_or(cp.grantee='anon'          and cp.privilege_type='SELECT') as anon_select
from information_schema.columns c
left join information_schema.column_privileges cp
  on cp.table_schema=c.table_schema and cp.table_name=c.table_name and cp.column_name=c.column_name
where c.table_schema='public' and c.table_name='<table>'
group by c.column_name, c.ordinal_position order by c.ordinal_position;
```

Then diff that against the `select=` list in the failing `edge_logs` row.

**The usual cause is a deliberate privacy migration, not a defect.** A `revoke select (cols) on t
from authenticated` shipped alongside a client change that stops selecting those columns is a
BREAKING pair: every already-loaded bundle keeps sending the old `select=` list until it reloads.
An installed PWA holds that bundle across the deploy, so the 403s arrive *hours* after the migration
and look like a live break.

**Before calling it a defect, check whether the same client later succeeds with the NEW select
shape.** Query `edge_logs` for that path over the following minutes and compare the `request.search`
prefix — a device that flips from the old list (403) to the new list (200) self-healed, and the
finding is `external`, the same family as §4. If the old shape is still 403ing with no newer shape
from anyone, the client change never shipped, and *that* is the bug.

**Read `request.method` before you believe a 200.** Supabase logs the CORS **OPTIONS** preflight as
its own 200 row, milliseconds before the GET it precedes. A naive "200 then 403 on the same query"
reading invents a flapping privilege that was never there.

## 7. The log-attribute keys are NAMESPACED — a flat key silently returns zero rows

Confirmed 2026-08-23 on `auxf`, **lost, and re-confirmed 2026-08-24**. `log_attributes` is a
ClickHouse `Map`, and a **missing key evaluates to `''` rather than raising**. So the natural first
query —

```sql
select log_attributes['error_severity'] as sev, count(*) from logs
where source='postgres_logs' and log_attributes['error_severity'] in ('ERROR','FATAL','PANIC')
group by sev
```

— comes back with **zero rows, exit 0, clean stderr**. That reads as "no postgres errors" and is the
same green-collector trap as the netlify adapter's §6/§8. On the run that found it, the true answer
behind that empty result was 15 postgres ERRORs and 15 HTTP 403s.

The 2026-08-23 run wrote the lesson up as "§7 of `adapters/supabase.md`" — and the section was never
actually appended, so the 2026-08-24 run had to re-derive the whole thing. **Verify the file after
editing it.**

**Verified keys, per source** (`ivuwwlhsppeetfkijxbo`, 2026-08-24):

| Source | Level / status key | Other useful keys |
|---|---|---|
| `postgres_logs` | `parsed.error_severity` | `parsed.sql_state_code`, `parsed.query`, `parsed.detail`, `parsed.user_name`, `parsed.command_tag`, `parsed.application_name` |
| `edge_logs` | `response.status_code` (a **String** — wrap in `toInt32OrZero`) | `request.method`, `request.path`, `request.search`, `request.headers.referer`, `request.sb.auth_user`, `request.headers.cf_connecting_ip`, `request.headers.user_agent` (§8), and — added 2026-09-12, see §13 — `response.origin_time` (origin latency in ms, also a String) plus `request.sb.apikey.apikey.prefix` / `request.sb.apikey.authorization.prefix` (which credential the caller presented) |
| `auth_logs` | `level` + `status` (these ARE bare) | `msg`, `path`, `component`, `remote_addr`, and — added 2026-09-16 — `error` (GoTrue's own error string, e.g. `error finding flow state: context canceled`), `duration` (**nanoseconds**, not ms), `referer`. `error` is the key that separates two `500`s that share a `msg`, and it is populated on the paired `request completed` row too, so an `error` row and a `request completed` row carrying the SAME `error` string at the same second are **one request**, not two events |
| ↳ | **`status` is `''` on non-HTTP lines** — 47 of 395 on `auxf` 2026-08-31. Those blank-status rows are where the OAuth outcomes live (`Login`, `Redirecting to external provider`, and `access_denied: The resource owner or authorization server denied the request` when a user cancels the consent screen). A filter of `status >= 500` or `level = 'error'` sees none of them. Group by `log_attributes['msg']` over the blank-status rows once per run. | |
| `storage_logs` | `level` — and **`warning`, not `error`, is where 4xx live** (§12) | `res.statusCode` seen 2026-08-23; absent from the 2026-08-24 pass. When it is absent, parse `event_message`: it is a fixed pipe-delimited line, `project | METHOD | STATUS | ip | cf-ray | path?token=redacted | user-agent`, so `splitByChar('|', event_message)[3]` is the status |
| `realtime_logs` | `level` | — |
| `postgrest_logs` | **none** | `event_message` only; the map carries just `host`/`identifier`/`project` |
| `pgbouncer_logs` | **none** | `event_message` only |
| `supavisor_logs` | bare `level` (`info`) on 2026-08-25; **none** on 2026-08-24; bare `level` on 2026-08-23 | `event_message`, `context.*`, `db_name`, `peer_ip` |
| `workflow_run_logs` | **none** | `event_message` only; plus `branch`/`workflow_run`/`container_name` |
| `auth_audit_logs` | bare `level` | `msg` (the whole event as JSON), `auth_audit_event.action`, `auth_audit_event.actor_id`, `auth_audit_event.actor_name`, `auth_audit_event.user_agent` |

**The source list itself is not fixed — enumerate it every run.** `auth_audit_logs` appeared on
2026-08-25 and is absent from every earlier pass on this project. A sweep that walks the table above
instead of `select source, count(*) from logs group by source` skips whatever is new that day and
still reports a clean bill of health. Its rows are `login` / `token_refreshed` / `token_revoked` /
`user_signedup` at `level='info'` — normal traffic, but the *next* new source may not be.

**Enumerating a source is not examining it — a source with no row in the table above has no filter,
and skipping it is the same clean-bill-of-health failure as a wrong key.** So for every source the
live listing returns that this file does not already cover, do all three, in order:

1. Run the key-set query scoped to that one source (`limit 5`, per the discovery note below) to find
   out whether it carries a level field at all.
2. If it has one, run the unfiltered distribution over it and check the buckets sum to the source's
   row count, exactly as for `postgres_logs` and `edge_logs`.
3. If it has **none**, fall back to `event_message` text, or to the `severity_text` base column where
   that is populated — the same treatment `postgrest_logs`, `pgbouncer_logs` and `workflow_run_logs`
   already get.

If none of the three yields a usable filter, **report that source as unclassified in the run's
unseen-classes list**. An unclassified source is an honest gap; a silently skipped one reads as
health the sweep never observed. Then add its row here so the next run starts from step 2.

Flat `log_attributes['error_severity']` and `log_attributes['status_code']` exist on **no** source.
The two rows above where the passes disagree are exactly the rows to re-derive before trusting —
which the distribution query below does in one shot. Sources with no level field must be filtered on
`event_message` text, or on the `severity_text` base column where that is populated, or you will
examine nothing and call it clean.

**A per-source filter written for one tier silently no-ops on another.** One sweep filtered seven
sources on `log_attributes['level']`; `auth_logs` and `realtime_logs` honoured it while
`storage_logs`, `postgrest_logs` and `pgbouncer_logs` were never actually examined, and the combined
result looked like a clean bill of health.

**Always run the unfiltered distribution first, then the filter.** One query proves the keys are
real before any zero result is believed:

```sql
select log_attributes['parsed.error_severity'] as sev, count(*) from logs
 where source='postgres_logs' group by sev order by 2 desc
select log_attributes['response.status_code'] as st, count(*) from logs
 where source='edge_logs' group by st order by 2 desc
```

A healthy window looks like `{LOG: 164}` and `{200: 6766, 101: 25, 304: 3, 302: 2}` — every row
accounted for. A **broken** key looks like a single `{'': 164}` bucket. Zero rows from the filter
plus a populated distribution is the only zero result worth reporting. Sanity-check it further with
`select source, count(*) from logs group by source`: that proves the stream is alive, and a non-zero
source with zero errors under your filter is the case that deserves a second look at the key name
before you write "healthy" in the report.

**Discovering keys costs a lot of context — do it narrowly.** The full sweep works:

```sql
select source, arrayStringConcat(arraySort(mapKeys(log_attributes)), ', ') as keys, count(*) as n
from logs group by source, keys order by n desc
```

but `edge_logs` alone has ~40 distinct key-sets of ~50 keys each and dumps tens of KB. Scope it to
**one** source at a time with `limit 5`, and skip it entirely when the table above still matches.
Group by the key list rather than sampling one row — the key set varies *within* a source too (an
`edge_logs` row with a JWT has ~20 more keys than an anon one).

## 8. `user_agent='node'` splits the app's OWN server calls out of `edge_logs` — and it changes the 4xx rule

Confirmed 2026-08-27 on `auxf`. `edge_logs` mixes browser traffic with the calls the project's own
server-side code makes (Netlify functions, cron jobs, build scripts). The server ones carry
`log_attributes['request.headers.user_agent'] = 'node'` and no `request.headers.referer`:

```sql
select log_attributes['request.path'] as p, log_attributes['response.status_code'] as st,
       count(*) as n, min(timestamp) as t0, max(timestamp) as t1
from logs where source='edge_logs' and log_attributes['request.headers.user_agent']='node'
group by p, st order by n desc
```

Two reasons this is worth a query of its own every run.

**It gives each scheduled job an exact invocation count.** `auxf`'s two minute-cadence drains showed
1441 and 1440 `200`s over 24h against a theoretical 1440 — better liveness evidence than the netlify
adapter's narrow-window trick (`netlify.md` §11), and immune to its truncation problems.

**The pipeline's "a lone 4xx is almost always a probe" rule does NOT apply to these rows.** A probe
is an outsider. A 4xx with UA `node` is the project's own server failing to authenticate to its own
database, and it deserves triage however few there are. Read it against the same path's success
count in the same window: `1` failure against `1441` successes on one static service key is a
transient gateway blip (**external**) — but confirm the recovery in the log rather than assuming it,
by finding the next call on that path from that caller and checking it returned 200.

A *sustained* run of them on the same path is the opposite finding and a serious one: a revoked or
rotated key that no deploy has picked up, which silently stops every scheduled job. Distinguish the
two by duration, never by count alone.

## 9. The 24h window SLIDES between queries — bucket sums drift by a few rows

`select source, count(*) from logs group by source` and a follow-up per-source distribution are
taken seconds apart against a rolling 24h window, so the second one legitimately disagrees with the
first by a handful of rows in both directions (6611 vs 6607 on 2026-08-27). §7 tells you to check
that the buckets sum to the source's row count — that check is for catching a **wrong key**, whose
signature is a single `{'': n}` bucket holding everything, not a drift of single digits. Do not
re-run queries chasing a difference of four.

## 10. SQLSTATE `P0001` is an RPC refusing on purpose, not a defect — but prove the user sees it

Confirmed 2026-09-06 on `auxf`. A `postgres_logs` row at severity `ERROR` with
`log_attributes['parsed.sql_state_code'] = 'P0001'` is Postgres's `raise_exception` — a
`raise exception '...'` written **deliberately** in a `plpgsql` function. PostgREST turns it into a
`400`, so it shows up twice: once in `postgres_logs` at ERROR, once in `edge_logs` as a `4xx`.

Two things make it read as a defect when it is not:

- **The severity is not a choice.** Postgres has no level below `ERROR` for a raised exception, so
  §2's "keep ERROR / FATAL / PANIC" filter catches every deliberate validation refusal an RPC makes.
  This is *not* the "healthy path logged at the wrong level" noise class — there is no other level to
  log it at, and no code change would move it.
- **It carries `parsed.user_name = authenticator` and `parsed.application_name = postgrest`**, which
  is exactly what a real defect in app traffic looks like too. It is genuinely the app's own users
  hitting the app's own code; the code is just saying no on purpose.

**The triage question is whether the message reaches the human, not whether it was logged.** Trace
it, do not assume:

1. Find the `raise exception` — `git grep -n -F '<the message>' <deployed-sha> -- supabase/`.
   Read the guard around it and decide whether refusing was correct for those inputs.
2. Find the client wrapper for that RPC and check it **rethrows the server's message** rather than a
   generic one. On `auxf` that is `rows()` in `src/lib/api.ts`, which rethrows `err.message` for
   every code except `42501` (§6's stale-bundle case, which it deliberately replaces).
3. Find the component and check its `catch` renders that message somewhere visible.

All three hold → **`external`**, ledgered, nothing filed. Any of them fails → a real finding, and
usually a small one: the guard is right and the UI swallows it.

**Read the count, never the presence.** One refusal is a person being told no. A *sustained* run on
one path is somebody stuck in a loop they cannot get out of, and that IS a bug — the same
count-not-presence rule §8 applies to node-UA 4xx.

## 11. A `504` in `edge_logs` is usually PostgREST's own Warp reaper — and `postgres_logs` is what tells you it is not the SQL

Confirmed 2026-09-09 on `auxf`. A single `504` on `POST /rest/v1/rpc/<name>` reads like a slow query
timing out, and the obvious next move is to go looking at the function's plan. That is the wrong
first move: the corroborating evidence sits in **two other sources**, and together they usually say
the database never even saw a problem.

**The pairing.** `postgrest_logs` on most Supabase projects is dominated by
`Warp server error: Thread killed by timeout manager` — PostgREST is a Haskell service on the Warp
server, and Warp's timeout manager reaps threads it considers stalled. That line is normal, high
volume, and almost always invisible to callers. **Occasionally one of those reapings lands on a
request that a caller was waiting for**, and *that* is what surfaces as a `504`. Look for the Warp
line within a few seconds of the 504 timestamp:

```sql
select timestamp, substring(event_message,1,120) as m from logs
where source='postgrest_logs'
  and timestamp between toDateTime('<t0>') and toDateTime('<t1>')
order by timestamp
```

On `auxf` the 504 was at `22:54:01.306Z` and the Warp line at `22:54:02.76Z`.

**The disqualifier that makes this cheap: check `postgres_logs` for the window.** If the SQL had
genuinely timed out you would see a `57014` `canceling statement due to statement timeout` there,
as §7's distribution query shows in one call. A clean `{LOG: n}` bucket — no ERROR, no FATAL, no
PANIC — means the failure sat entirely **above** Postgres, in the PostgREST/gateway tier, and there
is no query to tune and no app code in the path. That is a two-query proof and it settles the
triage.

**Then apply §8 unchanged.** These 504s usually arrive on node-UA calls from the project's own
scheduled jobs, so the count-against-denominator rule governs: read the 504 against that same path's
success count *in the same window*, and **prove the recovery** by finding the next call on the path
and checking it is a 200. One against 1441, recovered 6.2 s later, is `external`. A sustained run of
504s on one path is the opposite finding — PostgREST is genuinely unable to serve that path — and IS
a bug.

**One trap worth naming.** A ledger note that says a class like the Warp reaper "reaches no client"
is a statement about the windows that were observed, not a property of the class. This 504 is that
same class leaking through exactly once. **Read the `edge_logs` status distribution every run**
rather than trusting such a note, and correct the note when it leaks.

## 12. `storage_logs` logs a 4xx at `warning`, and a signed-URL `400` is a TOKEN failure, not a missing file

Confirmed 2026-09-10 on `auxf` (issue #291). Two traps, and the first hides the second.

**Storage never uses `level = 'error'` for a failed request.** The whole window was
`{info: 616, warning: 5}`, and those five `warning` rows were the only `400`s the tier produced.
§2's table says to keep "errors and stack traces" for the edge-function tier and that instinct
carries over wrongly here: a filter of `level = 'error'` on `storage_logs` returns **zero rows on a
tier that is actively failing**, which is the same clean-bill-of-health failure §7 documents for a
wrong key. Filter this source on `level != 'info'`, or read the status out of `event_message`
directly with the `splitByChar` recipe in §7's table.

Cross-check it against `edge_logs`, which sees the same requests through the gateway and carries a
real status key — 6 rows there against 5 `warning` rows in `storage_logs` on that run. **The two
tiers disagree by a row or two** (§9's sliding window, plus some requests never reach the storage
service's own log), so use `edge_logs` for the count and `storage_logs` for the detail.

**Then read the status carefully: on `GET /object/sign/<bucket>/<path>?token=…`, a `400` means the
TOKEN is bad — expired, malformed, or signed for a different path — not that the object is
missing.** This is the opposite of the intuition a 404-shaped mental model gives you, and it decides
the whole triage: a missing object is a data-integrity bug, an expired token is a URL-lifetime bug,
and they have nothing in common.

Prove which one you have before classifying, with three cheap reads:

1. **Does the object exist?** Look for a `HEAD /s3/<bucket>/<path>` row, or any `200` on the same
   path at any point in the window. On `auxf` a `HEAD` sweep had returned `200` for all twelve
   objects in the folder 47 minutes before the failures.
2. **Did the same path succeed either side of the failure?** A `200` before *and* after, on the
   identical path, rules out the object and rules out an outage. `auxf` had `200` at `07:07:24Z`,
   `400` at `07:47:35Z`, `200` again at `07:49:05Z`, then `200` repeatedly for the next two hours.
3. **What is the tier's overall ratio?** 290 `GET` `200` against 5 `GET` `400` is a specific
   failure, not a broken feature.

All three held → the tokens had expired, and the finding is in whatever signs the URLs (on `auxf`,
a fixed `3600` TTL in `createSignedUrls` with nothing re-signing).

**One shape worth recognising, because it doubles the log volume and misleads the count.** If the
client falls back from a thumbnail URL to a full-size URL on image error, and both were signed in
the *same* `createSignedUrls` call, they expire at the same instant — so every broken thumbnail
produces a second, doomed request a few hundred milliseconds later. The signature is N failures on
`*_thumb.*` followed by N on the bare path, all inside a second. Do not report that as 2N distinct
failures; it is N photos failing, twice each, and the second half is itself part of the defect.

## 13. `response.origin_time` and the apikey prefix turn a 504 pile into a diagnosis in two queries

Confirmed 2026-09-12 on `auxf`, where §11's single-blip framing met a window carrying **448** 504s
and was not enough. §11 tells you to check `postgrest_logs` for the Warp line and `postgres_logs` for
a `57014`. Both of those are *disqualifiers* — they rule the database out. Neither tells you what the
failure actually is, and on a sustained incident that gap is the whole triage.

`edge_logs` carries two keys that close it, and §7's table did not list either:

| Key | What it gives you |
|---|---|
| `response.origin_time` | milliseconds the origin (PostgREST) took — wrap in `toInt32OrZero`, it is a String |
| `request.sb.apikey.apikey.prefix` / `request.sb.apikey.authorization.prefix` | which **credential** the request used (`sb_secret_…`, `sb_publishable_…`, or `''` for a bearer-JWT browser call) |

**Query one — is there a timeout wall, or is it a random reaping?** Bucket the latency by hour, not
by request:

```sql
select toStartOfHour(timestamp) as hr, count(*) as n,
       quantile(0.5)(toInt32OrZero(log_attributes['response.origin_time'])) as p50,
       quantile(0.9)(toInt32OrZero(log_attributes['response.origin_time'])) as p90,
       quantile(0.99)(toInt32OrZero(log_attributes['response.origin_time'])) as p99
from logs where source='edge_logs' and log_attributes['request.headers.user_agent']='node'
group by hr order by hr
```

A Warp reaping is *random*: the tail moves but does not settle anywhere in particular. A **timeout**
pins the quantile to a constant. On `auxf` the p90 went from 174–1354 ms to `5018`, `5019`, `5021`,
`5026`, `5027`, `5044`, `5103` in eight consecutive hours — a five-second wall, visible in one query,
and not something the Warp line could have told you. Note the p50 barely moved (47 → 79 ms): **read
the p90, never the mean**, because a few seconds spread over a healthy median averages away to
nothing.

**Query two — is it everyone, or one credential?** This is the strongest single measurement the
adapter has, because it rules out your code, your queries and your data at once:

```sql
select log_attributes['request.sb.apikey.apikey.prefix'] as key_prefix,
       count(*) as n,
       countIf(log_attributes['response.status_code']='504') as e504,
       quantile(0.9)(toInt32OrZero(log_attributes['response.origin_time'])) as p90
from logs where source='edge_logs' and timestamp > toDateTime('<onset>')
group by key_prefix order by n desc
```

On `auxf`: the service key showed 3332 requests, p90 `5021 ms`, **446** 504s; publishable-key and
anonymous browser traffic **through the same gateway in the same window** showed 331 requests, p90
`276 ms`, **zero** 504s. Same PostgREST, same database, same minutes. Nothing the application does
differs between those two populations except which credential is presented, so no amount of reading
app code or query plans could have explained it — and equally, no app change could fix it.

Three rules follow.

- **Find the onset before you measure anything.** An hourly `countIf(status='504')` next to the
  success count gives it: `auxf` stepped from 2/hour to 31/hour at one hour boundary. Splitting the
  whole 24h window as a single population hides a step change inside an average.
- **A credential-scoped failure is `external` however large the count is.** §11's "a sustained run is
  the opposite finding and IS a bug" is about *severity*, not about ownership. Sustained means stop
  calling it noise and put it at the top of the report; it does not mean there is repo code to fix.
  Check whether any app code is even in the failing path before letting a count decide the triage.
- **Do not stop at the disqualifiers.** `postgres_logs` clean plus a flat Warp rate says "not the
  database and not the previously-known mechanism" — which on a sustained incident is precisely the
  point at which the old explanation must be abandoned rather than reused. On this run the Warp rate
  held at ~70/hour straight across a 15× jump in 504s, so the class the ledger had blamed for three
  runs was demonstrably not the cause.

**And check what the count means downstream before writing damage into the report.** These 504s
usually land on scheduled jobs, and `adapters/netlify.md` §22 documents that Netlify retries a
scheduled function that returns a non-2xx. On `auxf` every one of the 448 was retried and succeeded,
so a 19%-of-calls failure rate cost exactly nothing. Trace the recovery; do not infer loss from a
rate.

**The incident CHANGES STATUS CODE as it dies — filter on the credential and the path, never on
`504` alone.** Confirmed 2026-09-15 on `auxf`, watching this same incident end. A filter of
`status='504'` would have seen it stop at `13:42Z` and missed the terminal phase entirely, which
arrived as four other codes on the same two service-key paths inside the same three hours:

| code | count | window (2026-09-14) | what it was |
|---|---|---|---|
| `525` | 6 | `10:20:09–10:20:49Z` | Cloudflare could not complete TLS to the Supabase origin |
| `504` | 86 | `10:15:14–13:42:03Z` | the ~5.02 s origin wall |
| `502` | 15 | `13:25:43–13:33:10Z` | Bad Gateway |
| `500` | 8 | `13:26:05–13:35:08Z` | origin 500 |
| `401` | 5 | `13:26:03–13:30:24Z` | gateway auth rejection |

Two consequences, and the second is the one that misfiles a run.

- **Measure `countIf(toInt32OrZero(status) >= 500)` alongside the 504 count**, and add `401`
  explicitly, scoped to the credential prefix and the paths already implicated. A code you did not
  ask for is invisible; the §7 status distribution over the whole source is what surfaces it
  (`{200: 4628, 101: 116, 504: 95, 502: 16, 500: 8, 525: 6, 401: 5, …}` here), so run that first
  every time rather than going straight to the 504 filter.
- **A cluster of `401`s on a static service key during a gateway incident is NOT a rotated key.**
  This is the trap: `adapters/supabase.md` §8 and every project card treat a *sustained* run of
  node-UA 401s as the serious finding — a revoked credential that silently stops every scheduled
  job. Five 401s in four minutes looks like the start of exactly that. It was not. The
  disqualifiers, all cheap: the same paths returned `200` in the same minutes, the 401s sit inside a
  `502`/`500` cluster on the identical path and credential, and the next full hour was 130/130
  clean. **Read a 401 against the same path's success count in the SAME MINUTES, not just the same
  window**, and check whether other 5xx codes share its cluster before reaching for the credential
  explanation.

**The cleanest possible "it is over" reading is the §7 whole-source distribution with no 4xx and no
5xx in it at all.** Confirmed 2026-09-16 on `auxf`, the second window after the incident cleared:
`{200: 7054, 101: 144, 302: 9, 304: 8, 204: 2}` over 7217 rows. At that point the credential split,
the path filter and the `>= 500` count all have nothing to catch, and running them is wasted effort
— but you only know that because the distribution was run **first**, which is the §7 rule and the
reason this section tells you to run it before the 504 filter every time. The corroborating pair
still applies to the hourly view: node-UA traffic at a flat 132 requests/hour all `200`, and
`max(origin_time)` scattered `607–10785 ms` with no pin.

**And the clearing test, now that one has actually been observed clearing.** The 2026-09-14 run
corrected the p90 half of it (a quantile falls off the wall once the failure rate drops below
`1 − q`, while the wall is still armed). 2026-09-15 is the positive case and both halves fired
together: hourly 504s went to **zero** for 21 consecutive hours, *and* `max(origin_time)` per hour
came off the pin — `5018–5103 ms` during, then scattered `2922–10572 ms`, then `552–1257 ms`. A
scattered max is recovery; a pinned max is the wall, at any count. Report the pair, never either
alone.
