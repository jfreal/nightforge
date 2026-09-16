# Adapter: github-auto-issues

Issues the app or a CI workflow filed about itself. **Usually the highest-value source in the whole sweep** — each one came from a real user session or a real telemetry query, already deduped at the point of filing.

<!-- @doc:project-card -->
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
# per collected issue: the PRs that actually claim it, by number
for pr in $(gh issue view <issue> --repo <slug>               --json closedByPullRequestsReferences               --jq '.closedByPullRequestsReferences[].number'); do
  gh pr view "$pr" --repo <slug>     --json number,title,state,mergeable,mergeStateStatus,statusCheckRollup
done
```

**Do not reach for `--search 'linked:issue <n>'`.** `linked:issue` is a *boolean* qualifier meaning "this PR is linked to some issue". The number after it is parsed as a free-text term, not an issue filter, so the search returns PRs linked to unrelated issues that merely mention that number somewhere. On `cli/cli`, `linked:issue 10` comes back with PRs about keyring failures and SAML enforcement — none of them touching issue #10. It fails the way everything else in this adapter fails: plausible output, exit 0, wrong answer.

A repo-wide `gh pr list --state open` is still worth one look for orphans — a fix PR that never referenced its issue — but treat it as a separate sweep, and confirm each candidate's actual relationship to the issue before acting on it.

A fix PR sitting open for days with one red job is a finding in its own right, and often a *shared* one: on one run two unrelated auto-fix PRs were both blocked by the same flaky test that had landed on the default branch days earlier. Neither PR had touched the code the test covers.

**The trap that hides this: a job that only runs on pull requests.** A green deploy history proves nothing about it. Check which workflows actually run on pushes to the default branch — if `e2e` (or lint, or any gate) is PR-only, a break on the default branch is invisible until the next PR trips over it, and then it looks like that PR's fault. 

**But check the workflow's *schedule* too before you call the default branch blind — and check it every
run, because it can be added without anyone telling you.** A gate absent from the push triggers may
still run on `main`/`master` on a cron, which changes the reading of a red PR job completely: with a
scheduled run you have a known-good reference point on the default branch and can date the break, and
without one you have nothing. `gh run list --workflow <name> --limit 15 --json headBranch,conclusion,createdAt`
and look for rows whose `headBranch` **is** the default branch. On one run two consecutive merges each
carried a red `dotnet-tests` and were merged anyway — which reads as alarming until a scheduled `Tests`
run on `master`, sitting between them, came back green and settled both as independent flakes.

**A merge that landed past a red required gate is worth one line either way** — always name the failing
test, and say plainly whether the merged commit has since had a green run of that job, because until it
does the default branch's state is unverified rather than good.

**But "different tests each time, and a green run in between" is NOT enough to call them flakes.** That
inference is seductive and it is how one run mislabelled a hard break. Two consecutive merges had landed
past a red job failing two *different* tests, with a scheduled green run on the default branch sitting
between them — a textbook flake pattern. It was half wrong. A third red run an hour later, on an
unrelated branch, failed the *same* test as the second one: two branches, one test, no shared code. The
"green run in between" had been at **06:10 UTC** and the failures from **09:50 UTC** onward, and the test
turned out to fail deterministically every Monday between 07:00 and 11:59 UTC, because it read the real
wall clock while a higher-priority weekly email became eligible inside exactly that window. One of the two
red merges really was a lone flake; the other was a break hiding behind it.

The rule that survives: **a pass/fail split is only a flake if you can name why the passing run passed.**
"It was a different test" is not that reason. Compare the *hour* the runs started, not just their commits —
a gate that fails on a schedule looks exactly like nondeterminism, clears itself, and comes back. And when
a fix agent reports reproducing a failure on a clean checkout of the default branch, take that seriously:
that is the observation that settles it, and it costs nothing to ask for.

**And the reason that "green run in between" was worthless is the most reusable part: a run's
`conclusion: success` does not mean the job you care about passed. It can have been skipped.** That
scheduled run on the default branch reported `conclusion: success` at the run level while its job list read
`e2e success, dotnet-tests skipped, android skipped, assets skipped` — the workflow's `schedule` trigger
existed to refresh a screenshot gallery, and the other three jobs' fork guard required `pull_request` or
`workflow_dispatch`, so they opted out on their own. The failing job had not run at all.

So **never read a workflow's conclusion as a verdict on a job**. Ask for the jobs:

```
gh run view <id> --repo <slug> --json headSha,event,conclusion,jobs \
  --jq '{sha:.headSha[0:8], event:.event, run:.conclusion,
         jobs:[.jobs[] | {name, conclusion}]}'
```

A `skipped` job is not a passing job, and `event` tells you why it skipped — the same workflow can run a
completely different subset per trigger. Before writing "the default branch is verified green", name the
run, the event, and the job, and confirm that job's own conclusion is `success`.

**A third possibility sits between "flake on the base branch" and "defect in this PR": the PR's base
is simply old, and the red job is a failure that has already been fixed on the default branch.** The
tell is that the PR cannot plausibly have caused it — a JavaScript dependency bump turning a .NET
integration test red, say. Before reasoning any further, compare two timestamps: when the PR's check
run **started**, and when the default branch last took a commit touching that test's **harness** (the
fake server, the fixture, the container setup — not the test file, which will often be byte-identical
across the two commits and will mislead you into calling it unchanged). On one run a dependabot PR
showed `dotnet-tests` red with
`Expected exception type:<RetryLimitExceededException>. Actual exception type:<SqlException>`; the
test file had not changed in three weeks, but the fake SQL server it drives had been rewritten twelve
minutes *after* that check run started, by a PR whose own commit message named this exact symptom (a
TLS handshake eating the connect timeout under parallel test load). Nothing was wrong with the PR,
the test, or the base branch as it stands. The fix is a re-run, and the finding is one line in the
report.

**A fourth possibility, and it is the one that actually breaks the default branch: two PRs in flight
against each other.** A PR's green rollup is a verdict on **the commit it ran against**, not on the
commit that merges — and when two branches are open simultaneously, one can land a change that
invalidates the other's passing tests without either PR being wrong. Neither rollup will say so,
because neither ever ran against the other's result.

On one run two PRs merged **twelve minutes apart**. The first added regression tests that seeded
duplicate rows to prove a service groups by user; the second added a unique index forbidding exactly
those rows. The second PR's own check run — on the very commit that merged — reported
`dotnet-tests: FAILURE` six minutes before the merge button was pressed, with every other job green.
Because that job was `pull_request`-only, the default branch then carried a deterministic two-test
failure that nothing on the default branch would ever report, waiting for the next unrelated PR to
inherit the blame.

So, on any run: for each recently merged PR, resolve its **head** commit and read that commit's own
check rollup, not the PR's summary state. `gh pr view <n> --json headRefOid,mergedAt` then
`gh run list --branch <branch> --json databaseId,headSha,name,conclusion` — if no run on the merged
head is green for a required job, the default branch's state is *unverified*, and that is a finding
whether or not anything is visibly red.

And when you find this shape, **say explicitly that neither PR should be reverted**. Both changes are
usually wanted; what has to change is the fixture or the seam between them. A fix agent told only "this
test is failing" will reach for `[Ignore]` or deletion, which silently discards the guarantee the test
was added for.

Resolve the base commit explicitly (`gh pr view <n> --json baseRefOid,headRefOid`) rather than
assuming it is current — a PR opened days ago and never rebased carries whatever the branch was then.

A pass/fail split across PRs is a *hint*, not a verdict — the same split is what a real defect in one PR looks like. Before blaming the base branch, check three things: the PRs sit on the same base commit, the failing PR changed nothing the test touches, and a re-run on the *identical* head commit flips the result. Nondeterminism on one commit is the only direct evidence of a flake; everything else is circumstantial.

When you find one, read the failing job's log (`gh run view <id> --repo <slug> --log-failed`) and name the failing test before filing. "e2e is red" is not a finding; "this named test races a 2 s self-clearing UI flag" is.
