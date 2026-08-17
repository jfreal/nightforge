# Adapter: github-auto-issues

Issues the app or a CI workflow filed about itself. **Usually the highest-value source in the whole sweep** — each one came from a real user session or a real telemetry query, already deduped at the point of filing.

Card must supply: `slug` and the `label(s)` that mark auto-filed issues.

## 1. Collect

```bash
gh issue list --repo <slug> --label <label> --state open --limit 50 \
  --json number,title,body,labels,createdAt
```

Every open one is a real error that nothing has looked at yet.

## 2. Signature

Prefer the issue's own fingerprint over anything you compute:

- an `fp:<hash>` label — the app's own stable identity for the error
- an App Insights `problemId` or a `req:<route>:<code>` key in the body

Fall back to the normal `<source>|<name>|<stripped message>` only when neither exists.

## 3. Do not double-file

These already have an issue. The pipeline's step 5 is a no-op for them — go straight to triage and, if it is a **bug**, straight to a fix session pointed at the existing issue number.

## 4. Watch for a CI ledger you must not fight

Some projects already keep their own dedup state for the filing workflow (e.g. `.claude/seen-errors.json`, updated through a state PR). That file is the *filing* workflow's state, not this sweep's. Never write to it — a fix agent that commits it will collide with the workflow's own state PR. This sweep's ledger is the one named in the project card, and the two are allowed to disagree.

If the state PR (`chore/triage-state` or similar) is sitting open and unmerged, that **is** a finding: until it lands the workflow refiles the same keys every day. Report it; do not merge it.
