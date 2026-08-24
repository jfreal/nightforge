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
timestamp is an artifact and proves nothing about liveness. A level-filtered run that
returned *fewer* than 100 lines per function saw the whole window. Say in the report which
pass was truncated.

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
Widening the window to reach further back actively *hides* recent data. Never diagnose a
"function stopped running" from a wide-window pass — narrow the window instead, and
compare like-for-like windows across runs.

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
