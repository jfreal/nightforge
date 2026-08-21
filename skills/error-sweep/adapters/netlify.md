# Adapter: netlify

Function, edge-function, and deploy errors from a Netlify site. The CLI is logged in under the user's Windows profile — **no token needed**.

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

**Sanity-check a zero-line result by re-running without `--level`.** A broken collector
and a healthy site both produce an empty file. The unfiltered run proves the pipe works
and lets you scan `info`/`warn` for error-shaped text that the level filter cannot see.

**The stream is capped at ~100 lines per function, newest first.** If any function comes
back with exactly 100 lines, `--since` was not the binding constraint and the unfiltered
view reaches back only a few hours, not the requested window. This does not invalidate a
level-filtered run that returned *fewer* than 100 lines per function — that one saw the
whole window. Say in the report which pass was truncated.

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

So:

```bash
# collect (plain text, NOT --json)
netlify logs --since 26h --level error --level fatal \
  --source functions --source edge-functions > err.txt
netlify logs --since 26h --source functions > all.txt   # sanity/cross-check
```

- `No logs found for the given time range.` (one line) is the healthy zero result.
- Parse the plain lines yourself: `[𝒇 <function>] <ISO ts> <LEVEL> <message>`. The
  function marker is a multibyte `𝒇`, so **`grep -P` fails** in Git Bash
  (`-P supports only unibyte and UTF-8 locales`) — use `sed`/`awk`/plain `grep -o`.
- Re-test `--json` occasionally; when it starts returning lines again the contract-shaped
  output is nicer than parsing text.

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
```

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

```
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
the log shape — `grep -c '^[' out.txt` — and treat the banner and the `No logs found`
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

```
netlify api getDeploy --data {deploy_id:<id>}      # Git Bash -> SyntaxError
netlify api getDeploy --data {deploy_id:<id>}        # Git Bash -> works
```

So: **PowerShell needs `\"`, Git Bash needs plain `"`.** Because the failure message is the
same one §3 documents (`SyntaxError: Expected property name or } in JSON at position 1`),
it reads as "I forgot to escape" when the real answer is "I escaped in the wrong shell."
Check which tool you are in before reaching for the escapes.

## 14. You cannot retrieve the build log of a PAST failed deploy from the CLI

`netlify api getDeploy` on a deploy whose `state == "error"` returns the `error_message` and
nothing usable beyond it:

```
summary               {"status":"unavailable","messages":[]}
log_access_attributes false
```

And `netlify logs:deploy` is **gone** in 26.2.0 — it now tells you to run
`netlify logs --source deploy --follow`, which (a) only streams a build happening *now* and
(b) 404s on these sites anyway per §2.

So a stale `Build script returned non-zero exit code: 2` is triageable only from its
`error_message`, its `commit_ref`, and the repo — or from the Netlify web UI, which a
scheduled run cannot reach. Do not burn a run trying; record what the deploy list gives you,
check whether a later deploy of the same branch went `ready` (that is your "did it recover"
signal), and say in the report that the log itself was unavailable.
