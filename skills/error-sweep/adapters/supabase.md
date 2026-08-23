# Adapter: supabase

Postgres, API, auth, and edge-function errors plus security advisories, via the Supabase MCP tools.

<!-- @doc:project-card -->
Card must supply: `project_ref`.

## 1. Which tool

`get_logs` is **not exposed on every connection** — confirmed absent on 2026-08-15/16. Use `query_logs` (ClickHouse SQL over the unified `logs` stream) and fall back to `get_logs` only if `query_logs` is missing. If neither is available, that is a failed collector: say so in the report rather than reporting zero errors.

Retention is **24 hours**. A window wider than that silently returns 24h of data — never claim a 7-day Supabase window.

## 2. Filters — these streams are extremely noisy

Keep only:

| Service | Keep | Drop |
|---|---|---|
| `postgres` | `error_severity` in ERROR / FATAL / PANIC | **every `LOG` line.** Checkpoints, logical decoding, `could not receive data from client`, `unexpected EOF on standby connection` are all routine |
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
