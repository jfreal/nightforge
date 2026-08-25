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
| `edge_logs` | `response.status_code` (a **String** — wrap in `toInt32OrZero`) | `request.method`, `request.path`, `request.search`, `request.headers.referer`, `request.sb.auth_user`, `request.headers.cf_connecting_ip` |
| `auth_logs` | `level` + `status` (these ARE bare) | `msg`, `path`, `component`, `remote_addr` |
| `storage_logs` | `level` | `res.statusCode` seen 2026-08-23; absent from the 2026-08-24 pass |
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
