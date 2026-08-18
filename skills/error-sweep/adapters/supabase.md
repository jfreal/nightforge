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
