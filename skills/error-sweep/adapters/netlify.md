# Adapter: netlify

Function, edge-function, and deploy errors from a Netlify site. The CLI is logged in under the user's Windows profile — **no token needed**.

<!-- @doc:project-card -->
Card must supply: `site_id`, and whether the repo is `netlify link`ed.

## 1. Working directory

`netlify logs` refuses to run outside a linked directory, and `--url` does **not** lift that.

If the repo is linked (`.netlify/state.json` present), run from the repo. If not, make a scratch link dir once:

```bash
mkdir -p "$TEMP/<proj>-netlify/.netlify"
printf '{"siteId":"<site_id>"}' > "$TEMP/<proj>-netlify/.netlify/state.json"
```

Never create `.netlify/state.json` inside a repo that does not already have one.

## 2. Function + edge-function errors

```bash
netlify logs --json --since <window> --level error --level fatal \
  --source functions --source edge-functions > out.json
```

- **Redirect to a file. Do not pipe through `Select-Object`** — piping truncates the stream *and* returns a spurious non-zero exit code, which reads as a failed collector when it succeeded.
- **Do NOT pass `--source deploy`** — it 404s on these sites. Use step 3.
- Each line is already `{source, name, timestamp, level, message}` — the adapter contract shape.
- **Zero lines is the healthy result.** Exit 0 + empty file = success.

**Sanity-check a zero-line result — but read §6 and §8 before you do.** A broken collector
and a healthy site both produce an empty file, so an unfiltered re-run is what proves the
pipe works. It only proves that if it avoids the two defects those sections document: the
re-run must drop `--json` (§6) and pass a **single** `--source` (§8). Re-running the command
above verbatim minus `--level` keeps both defects, comes back empty for its own reasons, and
cheerfully confirms a dead collector. Compare filtered against unfiltered **per source**, and
scan `info`/`warn` for error-shaped text the level filter cannot see.

**The stream is capped at ~100 lines per function.** If any function comes back with exactly
100 lines, `--since` was not the binding constraint and that function's slice is truncated —
and the lines you keep are the **oldest** in the window, not the newest (§9), so its newest
timestamp is an artifact and proves nothing about liveness. **A level-filtered run that returned fewer than 100 lines has NOT necessarily seen the whole
window** — see the 2026-08-27 refinement in §9, where a 10-line error pass silently dropped both
of the window's real error bursts. Collect errors as a LADDER of overlapping windows and union the
result; a single pass is not a collection. Say in the report which passes were truncated.

## 3. Failed deploys

```bash
netlify api listSiteDeploys --data '{"site_id":"<site_id>","per_page":10}'
```

**On PowerShell the inner double quotes must be backslash-escaped** or the CLI dies with
`SyntaxError: Expected property name or '}' in JSON at position 1`. Single-quoting the
argument is not enough — PowerShell hands it to the exe with the quotes stripped:

```powershell
netlify api listSiteDeploys --data '{\"site_id\":\"<site_id>\",\"per_page\":25}'
```

Parse each deploy's `state` and `error_message`. `state == "error"` is a finding, with two exceptions that are **normal and must be ignored**:

| `error_message` | Why it is not a bug |
|---|---|
| `Canceled build due to no content change` | `netlify.toml`'s `[build] ignore` whitelist working as designed |
| `Skipped due to account credit usage exceeded` | Billing condition. Mention in the report; file nothing |

Two further `error_message` shapes, both seen 2026-09-22 on mergetel, are **real build failures and
not exceptions** — but note the exit code is not always `2`, so do not grep for that string:

| `error_message` | Notes |
|---|---|
| `Failed during stage 'building site': Build script returned non-zero exit code: 4` | Same class as `exit code 2`. The number is the tool's, not Netlify's, and it varies |
| `Timeout` | A bare one-word message with no stage prefix. The build exceeded the host's limit. §14 applies, so the build log is unretrievable from the CLI; use deploy metadata and neighboring deploys for recovery analysis |

**Judge a cluster of preview failures by the neighbours before reaching for a shared cause.** On
2026-09-22 four failures landed inside one 18-minute band across two branches, which reads like
§18's 2026-09-14 case where a provider incident froze every deploy. It was not: each branch had a
`ready` deploy *in the middle* of the band, and production was clean throughout. Two branches being
pushed rapidly, some commits building and some not, is ordinary development. **A `ready` deploy
interleaved with the failures rules out only a continuous site-wide failure that would have affected
that deploy.** An intermittent provider issue, or a condition limited to particular branches or
commits, remains possible.

There is a **third** shape that is neither of those and is not a code defect either. Seen
2026-08-27 on mergetel:

```text
Failed during stage 'preparing repo': ... remote: Repository not found.
fatal: repository 'https://github.com/<owner>/<repo>/' not found
: exit status 128
```

The build never reached the tree — Netlify could not **clone** at all, a host-side git/GitHub-token
transient. Distinguish it from a real failure by the neighbours: deploys of the *same branch* twenty
minutes either side cloned and built fine. So do not file it on first sight, and do not read it as
"the repo was deleted". **Do** record it — but not as an ordinary signature. Step 3 skips every signature already in
`seen.json`, so filing this one normally guarantees the next run skips it and the consecutive
repeat can never be observed.

Record it under a distinct pending key that carries a run count and that the skip rule does not
consume. Store the run that last saw it alongside the count, and **increment once per sweep, only
when this sweep immediately follows the recorded one** — several matching deploys in a single sweep
are one sighting, not several, and a count that survives a clean sweep in between is not a
consecutive repeat. Reset the count when a sweep goes by without it. A single sighting stays unfiled
and still visible to the next run; that is the whole point of treating this class differently.

At the second consecutive sighting, file the issue — then **consume the pending key, but only once
filing returns a confirmed issue number**. Convert it to the ordinary `seen.json` entry carrying
that number and its status, so step 3's skip rule takes over. Both halves matter: an unconsumed key
re-files the same failure on every later sweep, because the exemption that keeps it visible is
exactly what would otherwise suppress a duplicate; and a key consumed after a *failed* filing loses
the finding outright, which is why step 7 requires a signature whose issue creation failed to stay
unrecorded so the next run retries it.

Note this failure mode interacts with §14's `commit_ref` recovery test: the failing commit usually
never gets a ready deploy of its own, because the branch simply moved on. **`commit_ref` stays the
recovery key.** A later clean deploy of the same *branch* is not that commit and does not prove it
recovered — use it only to decide whether the clone failure was transient. With no ready deploy at
the same `commit_ref`, recovery is `unknown`, not recovered; say so rather than closing the row.

## 4. The whitelist gotcha, and why it matters to a fix agent

`netlify.toml`'s `ignore` command is a **whitelist** of build inputs. If a fix adds a new input — a new top-level folder the build reads, a new config file, a script that starts consuming a JSON file — that path **must** be added to the `ignore` command in the same PR, or edits to it will silently never deploy. Put this in every fix brief for a Netlify project.

## 5. Build-credit cost

Every pushed PR branch triggers a deploy-preview build. On metered plans it is usually builds, not bandwidth, that dominate the bill — which is why the pipeline has a per-project fix-session cap. Check the project's own budget before raising a cap, and say in the report what it will cost.

## 6. `--json` is silently broken in netlify-cli 26.2.0 — use plain text

Confirmed 2026-08-18 on `netlify-cli/26.2.0 win32-x64 node-v24.14.0`: `netlify logs --json`
exits **0** and writes an **empty file**, whatever `--since`/`--source` you pass. The same
command without `--json` returns the logs. `--since 7d --json` → 0 lines; `--since 26h`
plain → 101 lines. Nothing on stderr. This is exactly the "green collector, broken pipe"
failure the pipeline warns about, and it survives the step-2 sanity check if you only
re-run the unfiltered pass **also with `--json`**.

Collect in plain text — and per source, one invocation each, per §8:

```bash
netlify logs --since 26h --level error --level fatal --source functions      > err-fn.txt
netlify logs --since 26h --level error --level fatal --source edge-functions > err-edge.txt
netlify logs --since 26h --source functions                                  > all-fn.txt
netlify logs --since 26h --source edge-functions                             > all-edge.txt
```

- `No logs found for the given time range.` (one line) is the healthy zero result.
- Parse the plain lines yourself: `[𝒇 <function>] <ISO ts> <LEVEL> <message>`. The
  function marker is a multibyte `𝒇`, so **`grep -P` fails** in Git Bash
  (`-P supports only unibyte and UTF-8 locales`) — use `sed`/`awk`/plain `grep -o`.
- Re-test `--json` occasionally; when it starts returning lines again the contract-shaped
  output is nicer than parsing text.

**Normalize to NDJSON before step 2 — the plain text is not the adapter contract.**
`SKILL.md` requires one JSON object per line with `source`, `name`, `timestamp`, `level`,
and `message`. Handing `err-fn.txt` straight downstream breaks that. Convert explicitly, in
node, so the message is JSON-escaped and the banner lines (§10) are skipped:

```bash
normalize() {                                    # normalize <in.txt> <out.ndjson>
  node -e 'const fs=require("fs");
   for (const l of fs.readFileSync(process.argv[1],"utf8").split(/\r?\n/)) {
     const m = l.match(/^\[\S+ (.+?)\] (\S+) (\w+) (.*)$/); if (!m) continue;
     process.stdout.write(JSON.stringify({source:"netlify", name:m[1], timestamp:m[2],
       level:m[3].toLowerCase(), message:m[4]})+"\n");
   }' "$1" > "$2"
}

normalize err-fn.txt   err-fn.ndjson
normalize err-edge.txt err-edge.ndjson
cat err-fn.ndjson err-edge.ndjson > err.ndjson   # what step 2 reads
```

Run it over **every** error file the collection produced, not just the functions one. An
unconverted `err-edge.txt` either breaks the contract downstream or gets quietly dropped, and
either way the edge tier vanishes from the sweep.

Lowercase the level: the contract wants `error|fatal|warning`, the CLI prints `ERROR`.

## 7. The unfiltered cross-check is near-useless when a cron function is chatty

The ~100-line cap is spent by whichever function logs most. On a site with a
minute-cadence scheduled function, the unfiltered pass comes back as 100 `Duration: … ms`
lines from that one function covering ~6h, with every other function invisible — and the
newest line can be many hours stale, so it is not even "newest first" in practice. It
still proves the pipe works, which is its real job. Say so in the report.

**But do not skip reading it, either.** The error/fatal pass structurally cannot see a
`warn`, and this app logs real config gaps at warn — 2026-08-21's run found `/updates`
serving its empty state in production to every visitor because `CHANGELOG_FEED_URL` was
never set, and the only trace anywhere was two WARN lines in the unfiltered pass. Scan the
unfiltered output for error-shaped text before writing it off as chrome. Beware the false
positives: structured `INFO` ticks carry fields like `"failed":0`, so grep for the word and
then read the line.

## 8. Repeating `--source` silently guts the result — pass exactly one, per invocation

Confirmed 2026-08-18 on `netlify-cli/26.2.0`. Passing `--source` **twice** returns a tiny
arbitrary slice instead of the union, and exits 0:

| Command (`--since 26h`) | Log lines |
|---|---|
| `--source functions --source edge-functions` | **5** (3 runs, identical) |
| *(no `--source` at all)* | **5** |
| `--source functions` | **351** (2 runs, identical) |
| `--source edge-functions` | `No logs found for the given time range.` |

The 5 lines were one contiguous block from a single function — not a sample of the window.
Omitting `--source` is just as lossy as repeating it. This is the same "green collector"
trap as `--json` in §6, and it defeats the §2 sanity check if the unfiltered cross-check
*also* repeats `--source`: both passes come back near-empty and agree with each other.

Repeating `--level` is **fine** — it unions correctly (`--level warn --level error` = 16,
`--level warn --level info` = 351, `--level warn` = 16). The defect is `--source` only.

So collect per source, one invocation each:

```bash
netlify logs --since 26h --level error --level fatal --source functions      > err-fn.txt
netlify logs --since 26h --level error --level fatal --source edge-functions > err-edge.txt
netlify logs --since 26h --source functions                                  > all-fn.txt
netlify logs --since 26h --source edge-functions                             > all-edge.txt
```

Every error pass needs its own same-source unfiltered partner — that is why `all-edge.txt` is
on the list. Without it an empty `err-edge.txt` has no comparator and gets written up as a
healthy edge tier when it may be this very bug.

**Cross-check any zero-line error pass against a same-source unfiltered pass.** If the
unfiltered pass on that source is also near-empty while the site is plainly alive
(deploys landing, scheduled functions configured), you are looking at this bug, not a
quiet site.

## 9. The per-function cap fills from the OLDEST end on a wide window

`--since 7d --source functions` returned 669 lines: exactly 100 each for the five chatty
functions, and their newest line was **2026-08-11/12** — the *start* of the window, six
days stale. The same functions' current logs appeared fine under `--since 26h`.

So the ~100-line cap is not "newest first" (§7 hedges this; this is the confirmation).

**Refinement, 2026-08-25: the retained slice is not reliably the oldest end either — it is an
arbitrary CONTIGUOUS block.** A 26h `--source functions` pass covering 08-24T08:09 → 08-25T10:09
returned exactly 100 lines for each of two minute-cadence drains, and every one of them fell in
`19:10–20:53 on 08-24` — 11h after the window opened and 13h before it closed. Neither end of the
window is in the result. The safe rule is the operational one: **a function that comes back with
exactly 100 lines tells you nothing about any timestamp**, earliest or latest. Only an uncapped pass
does.

**Refinement, 2026-08-26: a count BELOW 100 is not proof the slice covered the window either.**
A 26h `--source functions` pass returned 43 lines for `generate-background` — well under the cap,
so by the rule above it should have been complete. It was not: every line fell between
`08-25T13:19` and `08-25T20:02`, and the pass contained **zero** lines dated `08-26` at all. A 15m
pass taken minutes later on the same source showed that function invoked at `10:15`, `10:20` and
`10:25` on `08-26`. So the truncation is applied to the stream as a whole, not just per function at
100, and an uncapped-looking function can still be handed an arbitrary contiguous block. Use the
26h pass to find *what* is failing; use a narrow pass to establish *when* anything last happened.
Never read "no lines today" off a wide pass as "it did not run today."

Widening the window to reach further back actively *hides* recent data. Never diagnose a
"function stopped running" from a wide-window pass — narrow the window instead, and
compare like-for-like windows across runs.

**Refinement, 2026-08-27: the LEVEL-FILTERED pass is truncated the same way, at counts nowhere
near 100 — and that is how a live error hides from a sweep entirely.** On mergetel, thirteen
overlapping `--level error --level fatal --source functions` passes taken minutes apart returned
mutually inconsistent subsets of the same window:

| `--since` | error log lines | Contains the `17:54` burst? | Contains the `09:30` burst? |
|---|---|---|---|
| 26h | 10 | no | no |
| 24h | 20 | yes | no |
| 22h / 20h / 18h | 22 | yes | no |
| 16h / 14h | 11 / 3 | no | no |
| 12h | 2 | no | **no** — though 09:30 is inside it |
| 10h / 8h / 6h / 4h / 2h | 4 / 2 / 2 / 2 / 2 | no | yes |

Twelve ERROR lines at `08-26T17:54` are visible only at 18h–24h. Two at `08-27T09:30` are visible
only at 2h–10h. **No single window shows both**, and the nominal 26h window the card asks for shows
neither — it returned 10 lines and looked like a clean, uncapped, complete result. A third finding
(one line at `21:45`) appeared in exactly one window of the thirteen.

So the §9 rule generalises past liveness checks: **a wide pass tells you an error class exists, never
that one does not.** Zero error lines in a 26h pass is not evidence of a quiet site, and neither is
ten.

The working recipe — run a ladder and union it:

```bash
for w in 2h 4h 6h 8h 10h 12h 14h 16h 18h 20h 22h 24h 26h; do
  netlify logs --since $w --level error --level fatal --source functions > "err-$w.txt"
done
cat err-*h.txt | grep '^\[' | sort -u        # escaped ^\[ per §10
```

Thirteen invocations cost a couple of minutes and no build credits. Dedupe on
`<timestamp> <function> <message>`; the same line appears in several windows. Report the count from
the union, not from any one pass, and say in the report that the union is what you used — a future
run comparing "10 lines" against "23 lines" would otherwise read a collection artifact as a trend.

**Refinement, 2026-09-11 on `auxf`: an incomplete union is a property of one RUN, not a ceiling — and
the yield can split across SEVERAL windows.** Two consecutive runs had their entire error yield in a
single window (`12h`, then `18h`), which invites the shortcut "find the window that has them". On
2026-09-11 the three error lines came back as **2 in the `18h` window and 1 in the `22h` window**,
with the nominal `26h` pass a clean zero for the third run running. Worse for the shortcut: the
09-10 union was *incomplete* against an independent count (4 lines against 5 known failures) while
the 09-11 union was *exact* (3 against 3) — same site, same ladder, two days apart.

So completeness is not a property you can establish once and rely on:

- **Never stop laddering early because one window "has them".** Run all thirteen and union.
- **A complete union this run predicts nothing about the next run**, and an incomplete one is not
  evidence of a missing log site. Where a second, untruncated source counts the same failures (a
  database gateway log, an APM), reconcile against it every run and let *it* carry the count.
- A count on the Netlify side that **exceeds** the independent source is the only direction that
  is anomalous.

**Refinement, 2026-08-28: ladder the WARN pass too — the truncation is not specific to `--level
error`.** On mergetel a single `--since 26h --level warn --source functions` pass returned 11 log
lines. A seven-window warn ladder (`2h 6h 10h 14h 18h 22h 26h`) on the same site minutes later
returned **16** deduped, including five `___netlify-server-handler` lines the 26h pass could not
see at all — four `.marketing.yml` 404s and a `CHANGELOG_FEED_URL is not set` at `06:08`. §7 makes
the warn tier load-bearing on this project (that is where a real config gap surfaced), so a single
warn pass has the same blind spot the single error pass does.

The rule generalises: **ladder every level-filtered pass you intend to draw a conclusion from.**
Use the same thirteen windows for warn as for error. The seven-window run above proves it recovered
five lines the single pass missed; it does not prove the union was complete, and §9 is explicit that
a level-filtered call can return an arbitrary contiguous subset even well under 100 lines. Seven
windows is a heuristic — if you ladder only seven, a narrow follow-up over the gaps is required
before declaring the warn tier quiet. Thirteen costs nothing, so prefer it.

## 10. A zero-result pass looks like TWO different things — learn both

Confirmed 2026-08-19 on `netlify-cli/26.2.0`. Plain-text output now opens with a table
header before any log lines:

```text
Showing logs from functions for the last 26h:

  𝒇   Function

[𝒇 publish-scheduled] 2026-08-19T09:48:04.000Z INFO …
```

So a **healthy zero-error result on a source that has functions is 4 lines of header and
nothing else** — not the `No logs found for the given time range.` documented in §6. That
one-liner is what you get when the *source itself* is empty (a site with no edge
functions). Both are exit 0.

Do not misread the header-only form as a hung interactive picker: `𝒇   Function` is a
column heading, not a prompt. Count lines *after* the header when deciding whether a pass
was empty, and remember `wc -l` on an "empty" run reads 4, not 0. Count only lines matching
the log shape — `grep -c '^\[' out.txt`, escaped, because a bare `^[` opens a character
class that is never closed and grep exits 2 with `Invalid regular expression` — and treat the
banner and the `No logs found`
one-liner as chrome.

**There is a THIRD zero shape, and it is a lie: a completely EMPTY file — zero bytes, no
banner at all.** Confirmed 2026-09-01 on `auxf`. `netlify logs --since 26h --source functions`
exited **0** and wrote **0 bytes**; two identical re-runs seconds later each wrote 22028 bytes
and 228 log lines. Nothing on stderr, and the *level-filtered* passes taken in the same minute
all carried their normal 4-line banner, so the CLI and the login were plainly fine.

This matters because it is the collector shape that reads as the worst possible thing. The
healthy-zero form (§10, 4 lines) and the empty-source form (`No logs found`, 1 line) both
*announce themselves* — the CLI got a reply and printed a header for it. A zero-byte file means
the command produced no output at all, which is a transient CLI/API failure, not a quiet site.
Read as "the unfiltered cross-check came back empty" it convicts a perfectly healthy error pass
of being the §6/§8 green-collector bug.

**Recurred 2026-09-05 on `auxf`, identically — so it is a standing property of this CLI, not a
one-off.** Same command, same site: **0 bytes** on the first invocation, then 22637 bytes and 226
log lines on each of two re-runs seconds later, while all 26 level-filtered passes taken minutes
earlier carried their normal 4-line banner. Two sightings four days apart on one site mean the byte
check below is not a defensive nicety — budget for the re-run.

**The rule: a pass that writes zero BYTES is a failed invocation — re-run it before drawing any
conclusion from it, and never let it stand as the unfiltered partner for a zero-line error
pass.** Distinguish the three cheaply, and treat only the first two as data:

```bash
bytes=$(wc -c < out.txt); lines=$(grep -c '^\[' out.txt)
# bytes >  0, lines >= 0  -> real answer (banner present)
# bytes == 0              -> failed invocation, retry
```

## 11. Prove a scheduled function is alive with a NARROW window, always

The §9 oldest-end cap means a 26h unfiltered pass reports a chatty cron function's newest
line as many hours stale — 2026-08-19's run showed `publish-scheduled` newest at
`08-18T09:17` (25h old) purely from truncation. A 90m pass on the same source showed it
running at `09:48`, one minute-cadence tick behind now.

Make the narrow re-run a standing step, not a debugging afterthought: after the 26h passes,
run a narrow `--source functions` pass and list the newest timestamp per function. It is the
only cheap evidence that every function is still firing, and a silently dead cron is a real
bug that the error-level pass structurally cannot see.

**90m is NOT narrow enough — go to 15m.** Confirmed 2026-08-21: a 90m pass still returned
exactly 100 lines for `publish-scheduled`, so it was capped, so its "newest" (10:34) was a
truncation artifact and proved nothing. A 15m pass on the same source returned 30 lines for
it, newest 11:31:04 against a wall clock of 11:31:52 — one minute-cadence tick behind, which
is the actual proof. **The rule: if the function you are vouching for came back with exactly
100 lines, you have not proven anything about it — halve the window and run again.** Check
the count per function, not just the timestamp.

**Size the first window from the function's own cadence — 15m is not a universal floor.** A
window narrower than the schedule interval proves nothing about an hourly or nightly
function: it returns zero lines while the function is perfectly healthy, and reading that as
a dead cron is a false positive. Open at roughly twice the configured interval, then halve
only while the result is still capped at 100. A daily function cannot be vouched for by a
narrow pass at all — compare its last run against its schedule instead.

**Enumerate the schedules before you call any function missing.** Read every
`export const config` in `netlify/functions/*.ts` and note its `schedule` cron. A function
whose interval is longer than the window is *supposed* to be absent from every pass:
mergetel's `flush-batches-scheduled` runs `0 15 * * 1` (Mondays 15:00 UTC), so a 26h sweep
sees five functions plus `___netlify-server-handler` and that is the correct, healthy
result. Counting log-visible functions against the directory listing without reading the
crons manufactures a dead-cron finding every run.

**`--function <name>` removes the whole truncation problem for liveness — use it.** Discovered
2026-09-03 on mergetel; `netlify logs --help` in 26.2.0 lists it and no earlier section had
noticed. It filters the stream to one function *before* the cap applies, so a chatty
minute-cadence cron can no longer eat the window:

```bash
netlify logs --since 26h --source functions --function digest-scheduled   # 6 lines, uncapped
netlify logs --since 26h --source functions                               # 420 lines, 4 functions capped at 100
```

That is what everything above is working around. The narrow-window ladder of §9/§11 exists
because a wide pass is truncated by whichever function logs most; `--function` makes a **wide**
pass usable for the quiet function, which is exactly the one you cannot vouch for otherwise.

Make it the standing liveness check: **one `--function` pass per name in
`netlify/functions/*.ts`**, at a window comfortably wider than that function's cron interval, and
read the newest timestamp. Six invocations on this site, no build credits.

**Refinement, 2026-09-04 on `auxf`: `--function` lifts the cap CONTENTION, not the cap.** It stops
a chatty neighbour eating the window, but the ~100-line limit still binds on the filtered stream, so
a function chatty enough on its own is capped just the same:

| `--source functions --function <name> --since <w>` | lines |
|---|---|
| `weekly-digest` (hourly), `26h` | **24** — uncapped, complete, and 24/24 is the liveness proof |
| `quest-narrative-drain` (per minute), `2h` | **100** — capped; its "newest" `09:55` was 20 min stale at a `10:15` wall clock |
| `quest-narrative-drain` (per minute), `15m` | **15** — uncapped, newest `10:14:02` one tick behind now |

So §11's own rule survives `--function` unchanged: **count the lines first — exactly 100 means you
have proven nothing about any timestamp, and the window must be halved.** What `--function` buys is
that the *quiet* function's wide pass is now uncapped and its newest timestamp real, which is what
§19's stopped-cron test needs. For a minute-cadence function still size the window from the cadence:
`15m` at ~1/min is 15 lines, comfortably clear of the cap.

**Prove a STOPPED cron with a WIDE `--function` pass — this is the cheap version of §19.** §19
found the same failure class on `auxf` the same day and reached for downstream call counts in the
database, because from the log side "absent from this window" is also what a healthy low-cadence
function looks like. `--function` closes that gap without leaving the log source. On mergetel's
`digest-scheduled` (issue #174):

| `--function digest-scheduled --since <w>` | lines | newest line |
|---|---|---|
| `2h` / `6h` / `12h` / `18h` | 0 (`No logs found`) | — |
| `26h` | 6 | `2026-09-02T11:00:35.922Z` |
| `48h` / `72h` / `7d` | 53 / 82 / 94 | `2026-09-02T11:00:35.922Z` |

**A newest timestamp that does not move as you widen 26h → 48h → 72h → 7d is the signature of a
cron that stopped**, and a count still under 100 at `7d` proves the pass was not truncated. Four
narrow windows that each contain the same missed tick and each return zero cannot all have
truncated it away (§17's two-window proof, applied to absence). Widen only until the count
approaches 100; past that the cap binds again and the newest is once more an artifact.

**Correction, 2026-09-05 on mergetel: the stale-newest half of that signature ALONE is a FALSE
POSITIVE, and it fires on a perfectly healthy cron.** A `--function` pass is subject to §9's
arbitrary-contiguous-block truncation exactly like an unfiltered one, well under the 100-line cap,
and the block it keeps sits at the OLD end of the window — so widening the window moves the block
*backwards* and the newest timestamp sits still. That is the same reading a stopped cron gives.
`digest-scheduled` (`0 * * * *`), measured twice minutes apart at a `10:31Z` wall clock:

| `--function digest-scheduled --since <w>` | lines | oldest | newest |
|---|---|---|---|
| `12h` | 25 | `2026-09-04T23:00:30Z` | **`2026-09-05T10:00:29Z`** — 31 min old, healthy |
| `26h` | **2** | `2026-09-04T09:00:32Z` | `2026-09-04T09:00:33Z` |
| `48h` | 22 | `2026-09-04T00:00:52Z` | `2026-09-04T09:00:33Z` |
| `72h` | 24 | `2026-09-02T11:00:35Z` | `2026-09-04T09:00:33Z` |

Identical newest across 26h/48h/72h, every count far under 100 — the textbook signature above, on a
function that had fired 31 minutes earlier. The 12h pass is *inside* the 26h window and returned 25
lines the 26h pass did not have, which is proof of truncation and not of anything about the cron.
All five of this site's functions showed the same 25h-stale ceiling in their 26h `--function` pass
that run, while narrow passes put every one of them within a tick of now.

**So the load-bearing evidence for a stopped cron was never the stale newest — it is the NARROW
windows returning zero.** Re-read the £174 table above: its `2h`/`6h`/`12h`/`18h` rows are all
`No logs found`, and *that* is what could not be truncation, because four windows cannot each drop
the same missed tick. The wide rows only corroborate.

The corrected test, both halves required:

1. **Narrow passes, sized to the cadence, return ZERO.** At least two of different widths, each wide
   enough to contain several scheduled ticks. This is the finding.
2. **Wide passes show a newest that does not move.** Corroboration only.

If a narrow pass returns lines, the function is alive and the stale wide newest is this artifact —
say so and move on. Never open a cron-outage issue off widening windows alone.

**Refinement, 2026-09-08 on mergetel: a NARROW `--function` pass drops the newest ticks too, and
that reads as a stall in progress.** The corrected test above assumes a narrow pass that returns
lines is proof of life. It is not, if you read its newest timestamp. `reconcile-scheduled`
(`*/5 * * * *`), measured across four minutes at a `10:31Z` wall clock:

| `--function reconcile-scheduled --since <w>` | lines | newest |
|---|---|---|
| `30m` (at `10:27Z`) | 4 | `10:15:11Z` |
| `25m` (at `10:29Z`) | 3 | `10:15:11Z` |
| `20m` (at `10:30Z`) | 2 | `10:15:11Z` |
| **`12m`** (at `10:31Z`) | **3** | **`10:30:15Z`** — one tick behind now, healthy |

Three consecutive passes each ended at the same stale `10:15`, on a five-minute cron, at a wall
clock 15 minutes later: three missed ticks, and the watchdog's own threshold for that schedule is
20 minutes. It looked exactly like an outage opening up. The 12m pass, taken one minute after the
20m one, held all three of the "missing" ticks — so the block those wider passes kept was contiguous
and OLD, at counts of 2 to 4 lines, nowhere near the 100-line cap.

So §9's arbitrary-contiguous-block truncation reaches all the way down to single-digit line counts
on a `--function` pass. Two rules follow:

- **Never read a stale newest as a stall, at ANY window width.** Halve the window and re-run first.
  The finding is still only what the corrected test says it is: narrow passes returning ZERO.
- **Size the liveness window tight to the cadence and no wider.** Twice the interval, per §11's own
  rule, and stop: `12m` on a 5-minute cron was the honest pass and `30m` was not, which is the
  opposite of the intuition that a wider window sees more.

The database count of §19 is still the stronger evidence where the function's logging is sparse or
the host's log retention is short — but run this first. It is two CLI calls and needs no second
adapter.

**Refinement, 2026-09-11 on mergetel: a `--function` pass combined with `--level` can return a
CLEAN, BANNER-BEARING ZERO over a window that contains hundreds of matching lines.** This is the
same §9 truncation, but the arbitrary contiguous block it kept was *empty* — and the result is
indistinguishable, byte for byte, from the healthy-zero form of §10. `publish-scheduled`
(`* * * * *`) was emitting one ERROR per tick throughout:

| `--source functions --function publish-scheduled --level error --level fatal --since <w>` | log lines |
|---|---|
| `30m` | 21 |
| `2h` | 33 |
| `6h` | 33 |
| **`26h`** | **0**, with the normal 4-line banner and exit 0 |

The 13-window ladder union held **229** ERROR lines for that function in that window. So the wide
`--function` pass did not merely truncate — it reported nothing at all while the function was the
single loudest error source on the site.

Two rules, and the first one is the expensive one:

- **A `--function` pass is a LADDER too, never a single call.** §9 already says this for
  `--source` passes; `--function` buys freedom from the chatty *neighbour* (§11's 2026-09-04 note),
  not freedom from truncation of its own stream. Ladder it whenever you intend to conclude
  *absence* — which is exactly what the per-function error pass is for.
- **Never read a wide `--function` zero as "this function is clean".** §11's corrected stopped-cron
  test already demands narrow passes for the same reason; the identical caution applies to the
  error level. On this run the three functions that came back zero at `26h`
  (`___netlify-server-handler`, `generate-background`, `publish-scheduled`) had to be re-run at
  `1h`/`3h`/`6h`/`12h` before two of them could honestly be called clean — and the third turned out
  to be the flood.

**And note what makes this hard to catch: a failing function floods its own error channel.** The
398 identical `[cron-watch] … failed` lines of mergetel issue #213 were the §7 chatty-neighbour
problem arriving from a *broken* function rather than a talkative one, and they pushed
`___netlify-server-handler` out of the ladder union entirely. When one signature dominates an error
union, treat every *other* name's absence as unestablished and re-collect per function before
writing "no other errors" — see §21, which is the same trap measured in lines rather than names.

**A dead cron emits ZERO log lines, so both ladders are structurally blind to it.** No error, no
warn, not even the platform's `Duration: … ms` line — the function is not failing, it is not being
invoked. Both ladders came back clean on the run that found this, and clean *correctly*. Per-function
liveness is a **collection** step, not a debugging afterthought, on every project with a cron.

**Then use §19's `searchSiteFunctions` to decide whose fault it is.** That is the call that reports
what the **host** believes, and it carries the bundle timestamp `getDeploy` does not surface as
usefully. On mergetel it answered `digest-scheduled schedule="0 * * * *"`, bundled
`2026-08-31T20:32:29Z` — registration intact, so the cause is host-side and **no fix agent is
warranted**: there is nothing in `src/` to fix and a fix agent would guess.

`getDeploy` corroborates from the deploy side — `function_schedules` lists the crons that deploy
declared, and `available_functions` gives each function's content digest, id and size, which on
mergetel were **byte-identical** across the last two production deploys. One trap there:
`available_functions` entries carry *different key sets* between two deploys (the older snapshot had
`m`, `rg`, `obl`, `oblv` that the newer lacked). That is API enrichment noise, not a deploy
difference. Compare the digest `d`, the id, and the size `s`; ignore which optional keys are present.

Read the bundle timestamp against §19's warning before recommending a fix: mergetel's
`digest-scheduled` bundle dated `2026-08-31T20:32:29Z` was **reused unchanged** by the
`2026-09-02T11:03Z` production deploy, so "redeploy to re-register the schedule" would have
re-bundled nothing. A no-op touch to the function file is what forces a fresh bundle.

**Confirmed 2026-09-04 on mergetel, by accident, which is why it is worth trusting.** The stopped
`digest-scheduled` of issue #174 came back on its own after ~37h (`2026-09-02T11:00:35Z` →
`2026-09-04T00:00:52Z`, ~36 missed hourly ticks). What separated the deploy that fixed it from the
ones that did not is exactly the bundle timestamp:

| production deploys during the outage | re-bundled `digest-scheduled`? | cron restored? |
|---|---|---|
| six, `09-03` `12:13`→`19:13` | no — bundle stayed `2026-08-31T20:32:29Z` | no |
| `48952a91` (#176), ready `2026-09-03T23:20:25Z` | **yes** — bundle became `2026-09-03T23:21:34.529Z` | **yes, next tick at `00:00:52Z`** |

So the paragraph above is not a hunch any more: **an ordinary redeploy re-registers nothing when the
cache serves the same bundle, and a genuine re-bundle brings the schedule back on the very next
tick.** Two consequences for a sweep:

- When recommending the fix, say *cache-clear or no-op touch*, never "redeploy" on its own — the
  user can burn several deploys, as this site did six, and correctly conclude the remedy failed.
- `searchSiteFunctions`' bundle timestamp is also the cheapest **recovery** check. Re-read it before
  writing up a stopped cron as still broken: a timestamp that moved since the issue was filed
  predicts the cron is already back, and the `--function` ladder of §11 then confirms it in one call.

## 12. `netlify api ... > file.json` on PowerShell writes a BOM, and PS 5.1 chokes on it

`netlify api listSiteDeploys --data '...' > deploys.json` in PowerShell writes UTF-8
**with BOM**. Piping that back through `Get-Content | ConvertFrom-Json | Select-Object`
silently yields rows with every property empty — a header-only table, exit 0, nothing on
stderr. Same green-collector trap as §6.

Parse it in Git Bash instead, stripping the BOM explicitly:

```bash
node -e 'const d=JSON.parse(require("fs").readFileSync("deploys.json","utf8").replace(/^\uFEFF/,""));
  console.log(d.length); d.filter(x=>x.state!=="ready")
   .forEach(x=>console.log(x.state,x.created_at,x.branch,x.error_message))'
```

## 13. `netlify api --data` escaping is SHELL-SPECIFIC — the two forms are not interchangeable

§3 gives the PowerShell form. It **fails in Git Bash**, with the identical error, because Bash
passes the backslashes through literally:

```bash
# the §3 PowerShell form, run in Git Bash -> SyntaxError:
netlify api getDeploy --data '{\"deploy_id\":\"<id>\"}'

# what Git Bash actually wants — plain double quotes:
netlify api getDeploy --data '{"deploy_id":"<id>"}'
```

So: **PowerShell needs `\"`, Git Bash needs plain `"`.** Because the failure message is the
same one §3 documents (`SyntaxError: Expected property name or } in JSON at position 1`),
it reads as "I forgot to escape" when the real answer is "I escaped in the wrong shell."
Check which tool you are in before reaching for the escapes.

## 14. You cannot retrieve the build log of a PAST failed deploy from the CLI

`netlify api getDeploy` on a deploy whose `state == "error"` returns the `error_message` and
nothing usable beyond it:

```text
summary               {"status":"unavailable","messages":[]}
log_access_attributes false
```

And `netlify logs:deploy` is **gone** in 26.2.0 — it now tells you to run
`netlify logs --source deploy --follow`, which (a) only streams a build happening *now* and
(b) 404s on these sites anyway per §2.

So a stale `Build script returned non-zero exit code: 2` is triageable only from its
`error_message`, its `commit_ref`, and the repo — or from the Netlify web UI, which a
scheduled run cannot reach. Do not burn a run trying; record what the deploy list gives you,
and say in the report that the log itself was unavailable.

For "did it recover", match on `commit_ref`, not on branch. A later `ready` deploy of the same
branch is usually a *different* commit, so it says nothing about whether the failing tree was
fixed — the build could still break on that commit. Only a `ready` deploy carrying the same
`commit_ref` proves recovery. If no later deploy shares the ref, report recovery as unknown
rather than assuming either way.

## 15. `curl -w '%{http_code}'` prints `000` and exits 43 on this box — do not read it as an outage

Confirmed 2026-08-22 in Git Bash on `curl 8.8.0 (x86_64-w64-mingw32) ... Schannel`. Any
`-w` format variable makes curl print `000` and exit **43** (`CURLE_BAD_FUNCTION_ARGUMENT`)
even though the request itself succeeded — the body still downloads. It is not `-o
/dev/null`; writing to a real file fails the same way:

```bash
curl -s -o /dev/null -w '%{http_code}' https://merge.tel/updates   # -> 000, exit 43
curl -s -o page.html -w '%{http_code}' https://merge.tel/updates   # -> 000, exit 43
```

This matters because confirming a finding against the live site is a standard triage step
here (2026-08-21 confirmed issue #131 that way), and `000` reads exactly like "the site is
down" — a false outage filed off a broken probe. Dump the headers instead; that path works:

```bash
curl -sS -D - -o /dev/null https://merge.tel/updates | head -1   # -> HTTP/1.1 200 OK
```

## 16. A silent edge tier gives the same `No logs found` as a missing one — read the code, not the CLI

§10 says the `No logs found for the given time range.` one-liner is what an *empty source* returns,
and §8 says a near-empty `--source edge-functions` pass can be the repeated-`--source` CLI defect.
There is a **third** cause, and on `auxf` it is the actual one: edge functions that never call
`console.*` emit **nothing**, however often they run.

`auxf`'s `netlify/edge-functions/route-meta.ts` declares `export const config = { path: '/*' }` —
it runs on every single request to the site — and both its error pass and its unfiltered pass came
back as the one-liner on 2026-08-23 and 2026-08-24. That is correct and healthy. Only a
`console.error` inside the function would ever produce a line.

So before writing up an empty edge tier as a broken collector, **read
`netlify/edge-functions/*.ts`**: check whether any of them logs at all. If none do, the tier is
structurally invisible to this adapter — say exactly that in the report's unseen-classes list, and
do not re-diagnose it as the §8 defect every run. The distinguishing evidence is the *functions*
source: `--source functions` returning hundreds of lines in the same invocation style proves the
CLI is fine.

## 17. A scheduled function that RETURNS a non-2xx logs absolutely nothing

Confirmed 2026-08-27 on `auxf`. `netlify/functions/quest-narrative-drain.mts` returned
`new Response(..., { status: 502 })` at `2026-08-26T12:30:35Z` after its Supabase RPC came back
401. The function log recorded **no error line, no warn line, nothing** — proven with two
overlapping passes that both contain that instant and were both uncapped:

```bash
netlify logs --since 22h --level error --level fatal --level warn --source functions   # 0 lines
netlify logs --since 24h --level error --level fatal --level warn --source functions   # 0 lines
```

A **thrown** error does produce a line. A **returned** non-2xx `Response` does not: to Netlify it is
a normal return value. So the whole error-level pass is blind to any failure the app handles by
returning a status instead of throwing — and on a *scheduled* function nobody reads that status
either, because there is no caller.

This is a real hole in what this adapter can see, and it must go in every run's unseen-classes list
for any project whose functions return non-2xx on failure. Grep the function tree for
`status: 4` / `status: 5` against `console.error` before writing "0 function errors" as health:

```bash
grep -rn 'status: *[45][0-9][0-9]' netlify/functions/ | wc -l
grep -rn 'console\.\(error\|warn\)' netlify/functions/ netlify/shared/ | wc -l
```

A large first number with a near-zero second one means the error pass is decorative for that
project. On `auxf` it was 40 against 1.

**Two overlapping uncapped windows are the proof technique.** A single window cannot distinguish
"nothing was logged" from "the truncation of §9 dropped the block containing it". Two windows of
different widths that both contain the instant, both returning 0 lines, cannot both have truncated
away the same moment.

## 18. EVERY file under `netlify/functions/` is a function — a test file there fails the whole build

Confirmed 2026-08-27 on `auxf`. A fix agent added `netlify/functions/narrative-drains.test.mts` next
to the two drains it was testing. Local `npm run typecheck`, `npm test`, `npm run db:check` and
`npm run build` all passed. The deploy preview did not:

```
Incorrect function names. Name should consist of only alphanumeric characters, hyphen & underscores
```

Netlify derives each function's NAME from its filename and treats every file in the functions
directory as deployable. `narrative-drains.test` contains a `.`, which is outside the allowed set,
and **one bad name rejects the entire build** — not just that file.

Two things follow.

**Put it in every fix brief for a Netlify project:** a test for a function does not go next to the
function. Move it to a directory the deploy does not scan, and then confirm the test runner still
collects it — a test that silently stops running passes the suite and guards nothing, which is
strictly worse than the build break it replaced.

**And the general rule this is a case of: a green local build is not a green deploy.** The whole
class of host-side packaging checks — function naming, bundle size, config validation, redirect and
header parsing — runs only on the host. After a fix agent pushes, read the deploy the push created
(match on `commit_ref`, per §14) before recording the PR as healthy. On this run the PR was reported
"done, all checks pass" while `netlify api listSiteDeploys` showed its head commit `error`.

**Refinement, 2026-09-14 on mergetel: the deploy can fail for a reason that is in NEITHER the branch
nor the host — the RUNNING SITE.** A build-time fetch makes deployability depend on production's
health, so an outage you are already tracking as external quietly becomes a total deploy freeze.

The #224 fix branch errored with the message that tells you nothing:

```
Failed during stage 'building site': Build script returned non-zero exit code: 2
```

Its diff was innocent. The build died prerendering a statically-rendered page that fetches the site's
**own** live feed endpoint over the network; that endpoint was returning 500 because of the very
Supabase outage the branch was about; and the page's reader throws rather than degrading, so the
prerender failed and took `next build` with it. The loop closes on itself: while the provider is
unwell, **no branch can deploy, including the fix**.

Three things follow for a sweep:

- **When a deploy fails during a provider incident you are already tracking, suspect the coupling
  before the diff.** §14 says a past failed deploy's build log is unretrievable from the CLI
  (`summary {"status":"unavailable","messages":[]}`), so attribution is timestamp matching against
  the error union — the same technique §20 uses for module-load crashes, run in the other direction.
- **Find the coupling in the repo, do not infer it.** Grep the app for build-time network reads:
  a statically rendered route (Next: a `revalidate` export and no dynamic API) whose data function
  `fetch`es a URL, and then check whether that function **throws** or degrades on failure. A throw in
  a prerendered route fails the whole build, not just that route.
- **Do not let a fix agent's scope claim stand unchecked.** The agent reported this as "failing every
  deploy of this site, on every branch". Only one deploy had been *attempted* during the acute phase;
  an earlier preview that day went `ready` before the incident began. The mechanism was real and the
  pattern was not observed. Say which you have.

This is a **bug** and gets an issue, but usually **not** a fix agent: the throw is typically
deliberate and commented, and the question is whether to re-make the trade now that its blast radius
is measured. On mergetel that became issue #226.

## 19. A schedule that STOPS is invisible here — count the function's own downstream calls

Confirmed 2026-09-03 on `auxf` (issue #287). `weekly-digest`, `schedule: '0 * * * *'`, simply stopped
being invoked: last run `2026-09-02T20:00:20Z`, then fourteen consecutive hours of nothing. Every
pass this adapter runs was **clean**, and all of them were clean *correctly*:

- a 13-window error/fatal ladder: 0 lines, every pass a valid 4-line banner;
- a 13-window warn ladder: 0 lines;
- the unfiltered 26h pass: 212 lines, all `INFO`, no error-shaped text.

There was no error to find. §17 is about a failure the log cannot *represent*; this is a failure that
produces no log event at all, because a function that is never invoked writes nothing. §11's narrow
pass catches a cron that is *late*; it does not, on its own, catch one that is **gone**, because
"absent from this window" is also what §11's own caveat says a longer-cadence function looks like.

**Try §11's `--function <name>` pass first — it is two CLI calls and usually settles it.** Found on
mergetel later the same day: `--function` filters before the ~100-line cap, so a *wide* pass on the
quiet function is uncapped and its newest timestamp is real. A newest that does not move as you
widen 26h → 48h → 72h → 7d is the stopped-cron signature, with no second adapter and no SQL. Fall
back to the downstream count below when the function logs too sparsely for that, or when log
retention is shorter than the outage.

**The technique that works: count the function's own downstream calls in a source that is not
truncated.** Every scheduled function of consequence talks to something — a database, an API, a
queue. That system's log is a complete table over the window, immune to the truncation of §9. Find
the handler's **first unconditional** call (the one before any branching or slot filtering), and
count it:

```sql
-- adapters/supabase.md §8: the app's own server calls carry user_agent 'node'
select toStartOfHour(timestamp) as hr, count(*) as n from logs
where source='edge_logs' and log_attributes['request.headers.user_agent']='node'
  and log_attributes['request.path']='/rest/v1/<the first thing the handler reads>'
group by hr order by hr
```

An hourly function shows 24 buckets of 1. `weekly-digest` showed **four rows in twenty-four hours**.
Do this for every scheduled function in the tree, every run, and read it against the cron you
enumerated per §11.

**Then separate "the app forgot the cron" from "the host is not firing it" before writing it up** —
they read identically from the log side and have completely different owners:

```bash
netlify api searchSiteFunctions --data '{"site_id":"<site_id>"}' > sfns.json   # Git Bash quoting, §13
node -e 'const d=JSON.parse(require("fs").readFileSync("sfns.json","utf8").replace(/^\uFEFF/,""));
  d.functions.forEach(f=>console.log(String(f.n).padEnd(24),"schedule=",JSON.stringify(f.schedule)," bundled=",f.c))'
```

This is the only CLI call that reports what the **host** believes each function's schedule to be, and
it also gives the bundle timestamp. On `auxf` it answered `weekly-digest schedule="0 * * * *"`,
bundled `2026-09-01T23:32:32Z` — so the registration was intact and current, the deployed source
still carried the same `export const config`, and the cause was host-side. Note `listSiteFunctions`
is **not** a valid method name (`netlify api --list` to check); the one you want is
`searchSiteFunctions`.

The bundle timestamp earns its own line: it tells you whether a later deploy actually **re-bundled**
the function or reused the cache. On `auxf` two production deploys landed after the outage began and
neither re-bundled `weekly-digest` — so "just redeploy to re-register the schedule" needs a no-op
touch to the function file to mean anything.

**Say so in the unseen-classes list on every project with a scheduled function**, in these terms: the
error pass cannot see a function that never runs, and the only thing that can is a count of its
downstream calls.

**One more trap in the same family, milder and easy to misread as a trend.** The same run found the
two minute-cadence drains firing ~53 of 60 ticks an hour (1290/1288/1283/1281 against 1440), where
the previous run had measured 1445/1445/1443/1442. That is real and it is worth recording — but it is
a *host cadence* observation, not an app defect, and the number moves. Re-measure it from the log
each run; a figure copied forward from a report reads as a trend that was never observed.

## 20. `Invoke Error … ERR_MODULE_NOT_FOUND` is a module-load crash — and the `Duration:` pairing gives you the failure RATE for free

Found 2026-09-06 on mergetel (issue #193). A function whose bundle cannot resolve an import dies
before its handler runs:

```
[𝒇 <name>] <ts> ERROR Invoke Error {"errorType":"Error","errorMessage":"Cannot find module
'/var/task/node_modules/<pkg>' imported from /var/task/netlify/functions/<name>.mjs",
"code":"ERR_MODULE_NOT_FOUND","stack":[… finalizeResolution … ModuleJob.syncLink …]}
```

Two things make this shape worth recognising on sight.

**It is one of the few failures this adapter sees cleanly.** The runtime raises it, so it reaches the
error level — unlike §17's returned non-2xx and unlike §19's cron that never runs. But nothing the
*handler* would have logged appears, because the handler never ran. Do not read "only one distinct
message from this function" as "one small problem".

**The platform still emits a `Duration:` line for a crashed invocation**, at INFO, in the unfiltered
pass. So the two counts together give the failure rate with no extra calls:

```bash
grep -c '^\[𝒇 <name>\]' all-fn.txt                    # every line for the function
grep '^\[𝒇 <name>\]' all-fn.txt | grep -c 'Invoke Error'
grep '^\[𝒇 <name>\]' all-fn.txt | grep -c 'Duration:'
```

On mergetel that read 89 = 45 errors + 44 `Duration:` lines — i.e. **every** invocation in the window
failed, stated from the logs alone. Because the error ladder is truncated (§9) and the unfiltered pass
is capped (§7), neither count is an absolute total; the **ratio** is what carries, and a ratio near
1:1 of errors to Durations means a 100% failure rate rather than an intermittent one. That
distinction decides whether the finding is "a bug" or "this integration is entirely down", and it
costs two greps.

Then date it against the deploys: match the first error timestamp against `listSiteDeploys`. In #193
the first failure was 2026-09-05T18:07:24Z and the production deploy of the offending commit went
ready at 18:05:32Z, ~2 minutes earlier — which named the culprit commit before any code was read.

**And note what a green local build proves here: nothing.** §18 makes this point for function
*naming*; module resolution is the same class. `tsc --noEmit`, the unit suite, the linter and
`next build` all pass, and the deploy state is `ready`, because the break is in the deployed function
bundle rather than in the type graph. A fix agent must be told to assert the *bundling* constraint —
walk the static import graph reachable from `netlify/functions/*.ts` and fail on the forbidden import
— because a unit test of the leaf function passes unchanged straight through the outage.

## 21. One `console.error(msg, obj)` becomes SIX log lines with the SAME timestamp — count occurrences, not lines

Found 2026-09-09 on mergetel. `console.error('[reconcile] mcp grant sweep failed', err)` where `err`
is a PostgREST error object renders as Node's multi-line object dump, and **Netlify prefixes every
physical line of it** with the identical `[𝒇 <fn>] <ts> ERROR` header:

```text
[𝒇 reconcile-scheduled] 2026-09-09T09:30:21.548Z ERROR [reconcile] mcp grant sweep failed {
[𝒇 reconcile-scheduled] 2026-09-09T09:30:21.548Z ERROR   code: 'PGRST205',
[𝒇 reconcile-scheduled] 2026-09-09T09:30:21.548Z ERROR   details: null,
[𝒇 reconcile-scheduled] 2026-09-09T09:30:21.548Z ERROR   hint: "Perhaps you meant the table 'public.accounts'",
[𝒇 reconcile-scheduled] 2026-09-09T09:30:21.548Z ERROR   message: "Could not find the table 'public.mcp_grants' in the schema cache"
[𝒇 reconcile-scheduled] 2026-09-09T09:30:21.548Z ERROR }
```

So `grep -c '^\['` — the count §10 tells you to use, and correctly, for deciding whether a pass was
*empty* — inflates the **occurrence** count by the object's height. On that run 637 counted lines for
`reconcile-scheduled` were 159 actual failures, a 4x overstatement, and the raw figure would have
been reported as a burst four times its real size.

Two different questions, two different counts, and they must not be swapped:

```bash
grep -c '^\[' err-union.txt                                  # was the pass empty? (§10)
grep -c 'mcp grant sweep failed' err-union.txt               # how many times did it happen?
```

The rule: **count occurrences by grepping the message's own first line**, the one carrying the app's
text, never by counting prefixed lines. Where the message is not distinctive, dedupe on
`<timestamp> <function>` instead — a repeated timestamp on one function is one event, not several,
because a real repeat at millisecond resolution is vanishingly unlikely on a 5-minute cron.

This also skews the §2/§9 truncation reading: a function whose errors dump objects reaches the
~100-line cap after ~16 real failures, so its slice is truncated far sooner than the line count
suggests. Read "exactly 100 lines" as "capped after about 100/height failures", and ladder harder for
that function than the raw counts imply you need to.

Note the ~100-line cap is per stream, so a single fat error dump can also crowd out other functions'
lines in the same pass — which is the §7 chatty-neighbour problem arriving from a function that
failed sixteen times rather than one that logged a thousand times successfully.

**Refinement, 2026-09-14 on mergetel: the dump can be a WHOLE HTML PAGE, and at that height it hides
its own cause from every level-filtered pass you will run.** Supabase answered a PostgREST query with
a Cloudflare interstitial, so the error object's `message` was a ~6KB HTML document and
`console.error('[reconcile] failed to query jobs', error)` rendered as **~110 physical lines, every
one carrying the identical `[𝒇 reconcile-scheduled] 2026-09-14T10:20:48.025Z ERROR` header.** One
failure. One stream cap, consumed.

The measurement is the point:

| pass | windows | union lines | contains `525: SSL handshake failed`? |
|---|---|---|---|
| `--level error --level fatal --source functions` ladder | 13 | 334 | **no** |
| the same plus `--function` error ladders on all three crons | +18 | 383 | **no** |
| **unfiltered `--function publish-scheduled --since 10m`** | 1 | 37 | **yes, 7 of them** |

The two strings that named the provider as the cause (`525: SSL handshake failed` and
`Failed to get project config`) were in **none** of the 383 lines that thirty-one level-filtered
windows returned, and both were sitting in a single unfiltered ten-minute pass.

So the rule that §11 sells as a *liveness* check is really a **collection** step:

- **Run the unfiltered narrow `--function` pass for its CONTENT, not just its newest timestamp.**
  Grep each one for error-shaped text the way §7 tells you to grep the wide unfiltered pass. On this
  run it was the only pass that held the diagnosis.
- **When one function's errors dump objects, no error ladder can be called complete.** §9 already says
  a wide pass proves existence and never absence; a fat-dump function makes that true of *every*
  width at once, because each retained contiguous block is a fragment of one HTML page.
- **File the dump itself as a finding.** It is not log tidiness: it is the mechanism that hid a
  60-hour provider outage's cause from the sweep. On mergetel that became issue #224, whose fix is
  #204's pattern (route the object through the project's own `errText`) plus a cap on the message
  string, since a project's `errText` may only cap its JSON *fallback* path and pass a long `message`
  through at full length.

**Counter-example, 2026-09-15 on `auxf`: a project that ALREADY CAPS the message produces the same
shape harmlessly, and neither of the two properties above holds. Check the cap before filing.**
The same Cloudflare interstitial (`525: SSL handshake failed`) reached the same kind of
`console.error`, and:

- **It was 10 physical lines ending in `…`, not ~110.** `logBody` in `src/lib/fnGuard.ts` truncates
  at `LOGGED_BODY_CHARS = 500` — which is exactly the remedy #224 proposes, already shipped.
- **The diagnosis was INSIDE the cap.** `<title>supabase.co | 525: SSL handshake failed</title>` is
  the eighth line, so the ordinary level-filtered ladder carried it. No unfiltered narrow pass was
  needed, and the "no error ladder can be called complete" conclusion does not generalise — it is a
  property of an *uncapped* dump, not of dumps.
- **Only the FIRST physical line carried the `[𝒇 <fn>] <ts> ERROR` prefix**; the continuation lines
  were bare. So on this site `grep -c '^\['` counts occurrences **correctly**, and mergetel's 4–6×
  overstatement is not a universal property of the platform. The union was 95 lines for 95 events.

So the §21 rule needs one step in front of it: **read the logging call's own truncation before
deciding the dump is a finding.** Grep the project for a body/message cap around the `console.error`
that produced it. A capped dump whose first 500 characters name the cause is the system working, and
filing it wastes a run. Note the prefixing behaviour differs between sites for reasons this file
cannot yet explain — so establish which one you have (`wc -l` against `grep -c '^\['`) before
reading any count, in either direction.

## 22. Netlify CAN RETRY a scheduled function that RETURNS a non-2xx — more than one extra attempt, with unknown depth and delay

Found 2026-09-12 on `auxf`. §17 establishes that a returned non-2xx is invisible to the log. This is
the other half of that behaviour, and it is the more consequential half: **the platform treats the
non-2xx as a failed run and invokes the function again, within the same minute.** Nothing in the
handler does this — there is no retry anywhere in the code — and no section here had noticed it.

It matters because it inverts the triage of a whole class. A scheduled function answering 502 on 15%
of its ticks looks like a feature that is 15% broken. It is not: every one of those ticks ran again
seconds later and succeeded, so the user-visible loss was **zero**. Read a returned non-2xx from a
*scheduled* function as "this attempt failed", never as "this tick was lost", until you have checked
for the retry.

**The tell is in the unfiltered pass: a failing minute carries TWO `Duration:` lines.** A healthy
minute carries one.

```text
02:25:05.346Z ERROR match-narrative-drain: scheduled run answered 502 — Could not sweep the queue …
02:25:10.599Z INFO  Duration: 5235.59 ms     <- the invocation that failed
02:25:12.040Z INFO  Duration:  123.25 ms     <- the retry, ~1.4 s later, succeeded
```

So the two counts of §20 acquire a third reading. `Invoke Error` ≈ `Duration:` means every invocation
crashed. Two `Duration:` lines in a minute that carries an ERROR mean two invocations; they can be
the platform retrying and the schedule absorbing the failures, but they are not proof of a retry.
Attribute a retry only under the 2026-09-22 correction below.

**Confirm a retry in a downstream log rather than trusting the pairing** — the log is
truncated (§9) and the pairing is easy to misread. On 2026-09-12 a complete table (a database
gateway log, an APM) lined up with one extra call per non-2xx return:

```
observed calls  =  ticks in the window  +  number of non-2xx the function returned
```

On `auxf`, `retire_stale_match_narratives` was called **1659** times in 24h against a minute cadence:
`1440 + 219`, and `219 = 157 + 62` was exactly the count of 502s the drain returned. The quest drain
gave `1666 = 1440 + 226` against `225`. That is a measurement from that day, not an identity to
invert: an independent duplicate invocation adds a call with no non-2xx, and one non-2xx can
produce more than one extra invocation. A count above the tick count does not by itself name the
failure count, a second caller, or a broken cadence. §19 counts these calls to prove a schedule is
*alive*; do not subtract inferred failures to correct it.

Two warnings.

- **The retry is not a guarantee.** If the underlying condition outlasts every attempt the platform
  makes, the tick is genuinely lost — so the finding is still the failure rate.
- **The retry delay tracks the cadence, not a constant.** On the minute drains it landed ~1–7 s
  later; on the hourly `weekly-digest` a 504 at `02:00:33.998Z` retried at `02:01:11.624Z`, 38 s
  later. Do not window a recovery check to a couple of seconds.

**Correction, 2026-09-13 on `auxf`: it is NOT "once". The platform makes MORE than one extra
attempt, so the headroom is wider than one retry — do not size damage against a single retry.**
The paragraph above said "one extra attempt" and the sentence "the headroom is exactly one attempt
wide" was carried into that project's card as the thing to watch for. Both were wrong, and the
counter-example is unambiguous because the RPC involved has exactly one caller:

```text
18:02:03.006  retire_stale_quest_narratives  200   169 ms   <- invocation A
18:02:03.213  claim_quest_narratives         504  5019 ms   <- A fails, drain returns 502
18:02:12.150  retire_stale_quest_narratives  200   140 ms   <- invocation B
18:02:13.780  retire_stale_quest_narratives  200    41 ms   <- invocation C
18:02:21.306  retire_stale_quest_narratives  200   294 ms   <- invocation D
```

Four invocations inside ONE minute of a `* * * * *` cron, after a single returned 502. `git grep`
at the deployed commit showed `retire_stale_quest_narratives` called from exactly one line
(`netlify/functions/quest-narrative-drain.mts:72`), once per invocation, with no retry anywhere in
the handler — so the call count *is* the invocation count and three of those four are the platform.

**Establish the caller count before reading a minute's call count as invocations.** That `git grep`
is the whole proof; without it, extra calls in a minute are equally explained by a second caller
(§19's own warning, in the other direction).

Two rules replace the "one attempt wide" framing:

- **Count double failures, but do not call them losses.** On this run 16 minutes carried two or more
  504s on the same path and 1 carried three; every one recovered, both narrative queues were empty,
  and `weekly-digest` completed all 24 hourly reads. Under the old framing each of those 16 would
  have been written up as a lost tick.
- **Do not infer non-2xx returns from call counts alone.** An independent duplicate invocation
  adds a call without a non-2xx return, and one non-2xx can produce more than one additional
  invocation. The same day's match drain showed 1900 observed against `1440 + 461` predicted, and
  `claim calls = retire successes` held **exactly** (1589 = 1589) on both drains — a measurement,
  not a way to recover the failure count from a truncated Netlify log. Attribute a retry only with
  the downstream-latency, reachable-exit, and contiguous-log checks in the 2026-09-22 correction.

The exact retry policy (how many attempts, on what schedule, whether attempts overlap) is **not**
pinned down — B, C and D above arrived 9 s, 11 s and 18 s after A started, and B had already
succeeded before C and D ran, which no simple "retry until success" rule explains. Treat the depth
as unknown-but-greater-than-one rather than substituting a new constant.

**Correction, 2026-09-22 on `auxf`: two `Duration:` lines mean two invocations, not proof of a retry.
The platform also fires a scheduled function twice on its own, and the two cases are byte-identical
in the log.** Attribute a retry only after establishing that a non-2xx return was possible and
checking the downstream latency, reachable non-2xx exits, and contiguous unfiltered logs.
The double-fire matters more than it sounds, because on a project whose card says "a drain non-2xx with no
guard ERROR line is a regression" — `auxf` carries exactly that rule for PR #269 — the duplicate
reads as a *broken guard*, which is a finding, filed against a guard that is working perfectly.

One minute out of 1440 carried two invocations of `match-narrative-drain`:

```text
[𝒇 match-narrative-drain] 2026-09-21T23:06:02.020Z INFO Duration: 1014 ms
[𝒇 match-narrative-drain] 2026-09-21T23:06:18.129Z INFO Duration:   66 ms
```

Textbook §22: a slow first attempt, a fast second one 16 s later, and no error line anywhere in
thirteen ladder widths. It was not a retry.

**The discriminator is one field and it is free: compare the first invocation's `Duration` against
that invocation's own downstream call latency.** The claim RPC at `23:06:02.037` carried
`response.origin_time = 986 ms` (`adapters/supabase.md` §13) against a `Duration` of `1014 ms` —
**~28 ms of handler time either side of the call**, which leaves no room for a failure path to have
run at all. A genuine failing invocation spends time *after* its call failing.

Three corroborators, each cheap, and none of them the ladder's silence:

- **Both attempts' downstream calls returned `200`.** A retry follows a non-2xx *return*, which on
  most handlers requires a failed call; if every call succeeded, ask what else could have returned
  one.
- **Read the handler and enumerate its non-2xx exits.** Here they were a throw in the claim helper
  and `failed > 0`; the queue was provably empty all window and no other downstream path was touched,
  so the function returned `200 nothing queued` and neither exit was reachable.
- **Check a CONTIGUOUS unfiltered `--function` block that contains both lines.** The two sat at
  lines 56 and 57 of a 100-line one-per-minute block, adjacent, both `INFO` — so nothing was dropped
  between them. This is worth more than a level-filtered ladder (§9: a pass proves existence, never
  absence) precisely because it is contiguous.

So the corrected rule: **two `Duration:` lines in a minute mean "something invoked this twice", and
the retry is only one of the two explanations.** Establish that a non-2xx was *possible* before
reading the pair as a failure — and never open a missing-guard finding off the pairing alone.

## 23. A RE-BUNDLED function loses its Netlify log history at the deploy — the ladder cannot reach past it

Found 2026-09-14 on `auxf`, and it is the sharpest limit on the ladder yet recorded, because no number
of rungs recovers what it removes.

PR #294 deployed at `2026-09-13T19:13:15Z` and changed only the two narrative drains. A thirteen-window
error ladder run the next morning covered `09-13T09:00` → `09-14T09:27` and returned **158** lines. Its
earliest drain line was `2026-09-13T19:15:24Z` — **two minutes after the deploy** — and it held **zero**
drain lines before it, while Supabase's `edge_logs` recorded 25–37 gateway 504s per hour on those same
drains' RPCs straight through `10:00–19:00`. Nine hours of a loud, continuous failure, invisible to
every rung.

`weekly-digest` in the same union retained lines back past `09:00`, so this was not the ordinary
truncation of §9 hitting the whole stream.

**`searchSiteFunctions` (§19) names the mechanism in one call — read the bundle timestamp `c`:**

| function | bundled | earliest line in the union |
|---|---|---|
| `match-narrative-drain` | `2026-09-13T19:13:56.454Z` | `19:15:24Z` — nothing before |
| `quest-narrative-drain` | `2026-09-13T19:13:56.531Z` | `19:15:24Z` — nothing before |
| `weekly-digest` | `2026-09-01T23:32:32.267Z` (cache-reused) | `09:00:35Z`, and `48h` reached 33 hours |

A function whose bundle is **rebuilt** starts its log history at the new bundle. A function the deploy
served from **cache** keeps its history across that deploy. §11's note that a no-op touch forces a fresh
bundle is the same lever seen from the other side — it revives a dead cron, and it also wipes that
function's log history.

Three consequences.

- **After any deploy, a changed function's pre-deploy errors are unreachable from `netlify logs`.**
  Not truncated-and-recoverable-by-laddering: gone. Only a downstream count (§19, `adapters/supabase.md`
  §8) can see them. Read `commit_ref` and the bundle timestamps **before** interpreting a union, and say
  in the report which functions had their history cut and at what instant.
- **Do not read the resulting silence as a quiet period.** The natural misreading is "the drains were
  fine until 19:15 and then started failing" — the exact opposite of the truth here, where the failure
  rate was *higher* before the deploy and the app change *reduced* it.
- **It breaks the Netlify↔downstream reconciliation asymmetrically**, so recompute the expected count
  over the post-bundle window only. On this run the drain half came back 135 against 326 Supabase 504s
  (~41%) where earlier runs sat near 50% — most of that gap is this, not ordinary truncation. §9's rule
  that a Netlify count **exceeding** the independent source is the only anomalous direction still holds;
  this simply widens the expected shortfall after a deploy.

**Counter-observation, 2026-09-16 on mergetel: the history cut did NOT bite, and the shape that
looks like it is ordinary §9 truncation.** Four functions were re-bundled at `2026-09-16T09:41:37Z`
(`publish-scheduled`, `reconcile-scheduled`, `generate-background`, `github-webhook`; the deploy that
did it went ready `09:40:56Z`). Ladders run 40-55 minutes later still returned those functions'
**pre-bundle** lines — the error ladder held `publish-scheduled` ERRORs from `2026-09-15T18:01`
through `20:30`, and the unfiltered 26h `--source functions` pass held `publish-scheduled` lines back
to `2026-09-15T08:30`. Sixteen hours of history across a re-bundle, where §23 on `auxf` had none at
two minutes.

What *did* look like the cut, and is not it: the **unfiltered `--function publish-scheduled` pass
returned the identical 3-line block at `1h`, `2h` and `3h`, oldest `09:43:02Z`** — two minutes after
the bundle timestamp, on a minute-cadence cron that should have put ~360 lines in the 3h window. That
is exactly §23's tell, and exactly §9's arbitrary-contiguous-block truncation, and here it was the
latter: a level-filtered ladder taken minutes earlier reached hours past that boundary.

So the test for a genuine history cut needs a second leg:

- **Before attributing missing history to a re-bundle, check whether ANOTHER pass on the same
  function reaches past the bundle timestamp.** One that does disproves the cut outright. §23's
  `auxf` case had that leg — a thirteen-window ladder, zero drain lines before the deploy, with
  `weekly-digest` (cache-reused) retaining nine hours in the same union.
- **Identical oldest AND newest across several widths is the §9 signature first**, whatever the
  bundle timestamps say. `searchSiteFunctions`' `c` field tells you a re-bundle happened; it does not
  tell you the log history went with it.

Whether the difference is the host, the plan, or the interval between deploy and query is not pinned
down. Record which you observed rather than assuming either way.

## 24. An APP-SIDE log rollup makes the line count a FLOOR — §21 in the other direction

Found 2026-09-22 on mergetel. §21 warns that one `console.error(msg, obj)` inflates the line count
by the object's height, so counting prefixed lines overstates occurrences. The opposite failure
exists too, and it arrives the moment a project fixes its own log flooding.

mergetel's watchdog now de-duplicates its own repeats in process:

```text
[𝒇 publish-scheduled] 2026-09-21T19:16:04.559Z ERROR [cron-watch] staleness sweep failed
publish-scheduled TimeoutError: The operation was aborted due to timeout
(3 times since 2026-09-21T17:26:10.458Z)
```

That suffix is **not** a platform feature, and the first instinct that it is one costs a wrong
write-up. It is `logBoundedFailure` (`src/lib/cron-watch.ts:792-822`): a module-scope
`Map` keyed on `what\0name\0message`, a `count` incremented per repeat, and a re-log only once per
`REPEAT_SUMMARY_MS`. It shipped as PR #215 closing issue #214, whose title is the giveaway —
"identical ERROR logged every tick floods the error channel and hides other classes".

**Check the repo before attributing any log shape to Netlify.** One `git grep` settles it:

```bash
git grep -n -i -E "times since" <deployed-sha> -- src/ netlify/
```

Three things follow, and the third is the useful one.

- **The line count becomes a FLOOR on occurrences.** On this run 17 lines were at least 19 real
  failures. Sum the `(N times …)` counts per `(key, firstAt)` run and take the maximum per run,
  because the counts are cumulative within a warm process rather than incremental.
- **Strip the suffix in step 2's normalization.** `(3 times since <ts>)` carries a timestamp and a
  digit run, so an unstripped suffix makes every rollup line its own signature and files one issue
  per rollup. Add it to the strip list alongside timestamps and bare digit runs.
- **The suffix's `firstAt` NAMES AN OCCURRENCE THE LADDER DID NOT RETURN, which is free proof of
  §9 truncation from inside a single run.** On 2026-09-22 a line at `21:05:09.137Z` read
  `(2 times since 2026-09-21T20:15:22.900Z)`, and no `20:15:22` line existed anywhere in the
  thirteen-window union. Normally establishing truncation costs a second ladder or a narrow
  follow-up; here one line did it. Look for a `firstAt` with no matching line in the union whenever
  a project logs this way.

**And the same shape is positive evidence that a log-flooding fix is live**, which is worth
recording in the ledger rather than only noticing. §11's bundle timestamp tells you a function was
re-bundled; a rollup suffix tells you a specific PR's behaviour is actually running in production.
