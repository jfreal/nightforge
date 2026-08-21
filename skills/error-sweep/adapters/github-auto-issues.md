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

## 5. Check the fix PRs, not just the issues

An auto-filed issue that already has a fix PR looks handled from the issue list alone. It is not handled until the PR merges.

Resolve the PR **from the issue**, not from a repo-wide listing — `gh pr list` is not scoped to your issues, so it both misses linked PRs (any not in the first page) and drags in unrelated ones:

```bash
# per collected issue: the PRs that actually claim it
gh issue view <issue> --repo <slug> --json number,title,closedByPullRequestsReferences
gh pr list --repo <slug> --state open --search 'linked:issue <issue>'   --json number,title,headRefName,createdAt
# then the rollup, per PR you resolved above
gh pr view <n> --repo <slug> --json state,mergeable,mergeStateStatus,statusCheckRollup
```

A repo-wide `gh pr list --state open` is still worth one look for orphans — a fix PR that never referenced its issue — but treat it as a separate sweep, not as the issue-to-PR mapping.

A fix PR sitting open for days with one red job is a finding in its own right, and often a *shared* one: on one run two unrelated auto-fix PRs were both blocked by the same flaky test that had landed on the default branch days earlier. Neither PR had touched the code the test covers.

**The trap that hides this: a job that only runs on pull requests.** A green deploy history proves nothing about it. Check which workflows actually run on pushes to the default branch — if `e2e` (or lint, or any gate) is PR-only, a break on the default branch is invisible until the next PR trips over it, and then it looks like that PR's fault. 

A pass/fail split across PRs is a *hint*, not a verdict — the same split is what a real defect in one PR looks like. Before blaming the base branch, check three things: the PRs sit on the same base commit, the failing PR changed nothing the test touches, and a re-run on the *identical* head commit flips the result. Nondeterminism on one commit is the only direct evidence of a flake; everything else is circumstantial.

When you find one, read the failing job's log (`gh run view <id> --repo <slug> --log-failed`) and name the failing test before filing. "e2e is red" is not a finding; "this named test races a 2 s self-clearing UI flag" is.
