# pr-test card template

<!-- docKey: pr-test-card -->

A **project card** is the only per-project input the `pr-test` skill needs. Everything else — how the
test plan is established, when a checkbox may be ticked, the worktree-aware checkout, the screenshot
discipline, the report format, when the pass label is allowed — lives in the shared skill.

Put the card where it drifts with the thing it describes: in the product repo, as
`.claude/commands/pr-test.md` (so `/pr-test <n>` in that repo picks it up) or as a skill the repo
carries. One card per repo, because everything in it — ports, tenants, start commands — is a fact
about that one codebase.

**Keep cards out of a public repo.** They carry infrastructure identifiers, internal hostnames,
storage account names, and the names of test accounts. Not credentials — those stay in environment
variables the card only *names* — but not worth publishing either.

---

```markdown
---
description: Review and test a pull request in <Repo>.
argument-hint: "<pr-number>"
---

**Run the `pr-test` skill against the project card below.** The pipeline lives at
`<path to>/skills/pr-test/SKILL.md` — read it first; it is the whole procedure.

## Project card

| Field | Value |
|---|---|
| **Repo** | GitHub `<owner/repo>` — local checkout `<path>` |
| **Test accounts** | `<tenant/org/workspace>` — <when to use it; list each one> |
| **Credential env vars** | `$<APP>_TEST_EMAIL` / `$<APP>_TEST_PASSWORD` |
| **Screenshot dir** | `<repo-relative gitignored path>/` |
| **Screenshot host** | <upload command + public URL base, or "none — report local paths"> |
| **Pass label** | `claude-tested` |

### Services

| Service | What it is | URL | Start command |
|---|---|---|---|
| `<Project.Api>` | <API backend> | `<https://localhost:PORT>` | `<command, with every required flag>` |
| `<Project.Web>` | <frontend> | `<https://localhost:PORT>` | `<command>` |
| `<Project.Admin>` | <admin app> | `<https://localhost:PORT>` | `<command>` |

<Start order, if it matters. Which services need a secret store / VPN / tunnel flag and which
 do not — name the flag; a service started without it fails in a way that looks like a bug in
 the PR.>

### Known-unreachable surfaces — review only, never claim as tested

<Features that cannot be exercised on a dev machine: third-party callbacks, billing, anything
 behind a webhook from outside. Without this list every run rediscovers them and burns time
 proving the same thing cannot be done.>

### Gotchas

<Per-repo traps: a build step that must run before the app starts, a seed command, a port that
 another service squats on, a login that needs a second factor. Add to this every time a run
 loses time to one.>
```

---

## Why the card is thin

Every field here is something a person on that project knows and an agent cannot infer. A start
command is not guessable from a file listing — the flag that connects the API to its secret store is
invisible in the source tree, and a service started without it fails at first request, several
minutes in, looking exactly like a bug in the change under test. The same goes for which tenant has
the data the feature needs.

Everything that is *not* project-specific stays out of the card on purpose. The rule that a checkbox
may only be ticked from something observed, the rule that a read-only review is never labelled as
tested, the worktree pull that stops a stale checkout from passing a test plan it should fail — those
are the same in every repo, and a card that restates them is a card that will eventually contradict
the pipeline. When a run teaches you something, ask which kind of knowledge it is: a fact about this
repo goes in the card, a rule about testing PRs goes in the skill.

## The screenshot host is optional, and it is a publishing decision

Inline images make a test comment worth reading, and they require the PNGs to be somewhere a browser
can fetch without credentials. That is a publish: whatever is in the frame is readable by anyone with
the link, and it stays readable after the PR is merged. Name a host in the card only for an account
whose blast radius you have thought about, and keep the per-run prefix (PR number plus timestamp) so
one run never overwrites another's evidence.

With no host named, the run still works — the comment carries the local paths, and the images stay on
the machine that took them.
