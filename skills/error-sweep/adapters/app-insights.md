# Adapter: app-insights

Exceptions, failed requests, dependencies, and traces from Azure Application Insights via `az`.

<!-- @doc:project-card -->
Card must supply: `app_name`, `resource_group`, and optionally `workspace_id` and `subscription`.

## 1. The window trap — read this before writing any query

`az monitor app-insights query` **defaults `--offset` to 1 hour**, and that timespan is applied by the query API *before* the KQL runs. A `| where timestamp > ago(7d)` inside the KQL cannot widen it — it filters an already-1-hour result set and silently does nothing.

This is exactly how one project ran green every day for a week while missing 6 of 8 live problemIds.

**Pass `--offset` explicitly on every query. Put no timestamp filter in the KQL at all**, so the two cannot drift. Use ISO 8601 durations (`P7D`, `PT24H`) — a KQL-style `24h` is rejected or reinterpreted.

Window guidance: **7 days**, not 24 hours. Dedup is by key in the ledger, so a rolling window is idempotent — re-seeing an old key files nothing. A 24h window only catches errors that fire in the 24h before the cron, which drops everything low-frequency. Wider is not better: 7d ≈ 8 distinct problemIds, 30d ≈ 23, 90d ≈ 70, mostly already-fixed history.

## 2. Every other gotcha, all re-confirmed repeatedly

- `min(timestamp)` aliased to **`first`** or **`last`** is rejected — reserved words. Use `firstSeen`/`lastSeen`. The failure is an opaque `BadArgumentError` naming nothing.
- **`requests.success` is a string** (`'True'`/`'False'`). `where success == false` fails with the same opaque error. `dependencies.success` genuinely *is* a bool. Do not copy the predicate across tables.
- `-o table` **silently prints nothing** for some result shapes. Use `-o json` and flatten.
- A backtick inside a `--query` JMESPath literal is eaten by PowerShell before `az` sees it. Filter in KQL, not JMESPath.
- **Keep KQL on one line.** A multi-line `--analytics-query` runs only line 1 on this Windows `az` and returns a plausible *wrong* table — the worst possible failure mode.
- `az` returns column-oriented tables. Flatten to row objects before doing anything else.
- **A `union` leg may not `project`/`extend` a constant string column** — that is a third source of
  the same opaque `BadArgumentError`, and unlike the two above it is not a KQL error at all, so
  re-reading your syntax teaches you nothing. Confirmed deterministically:
  `union (requests | project timestamp, kind='REQ'), (exceptions | project timestamp, kind='EXC')`
  is rejected, and so is the version with the *identical* literal in both legs; the same
  `extend kind = 'REQ'` **outside** a union is accepted, as are numeric constants and string
  expressions over real columns (`substring`, `toupper`, `strcat(name, 'z')`, a bare rename).
  A cross-table timeline is the natural thing to want here, so label the legs with a column that
  already exists instead: `union requests, exceptions, dependencies` supplies `itemType` for free.
  Where that will not do, run the passes separately and merge them yourself.
- **`client_City` is per-telemetry-item, not per-operation — an outbound call and its exception can
  carry the *server's* city.** Inside one `operation_Id` the inbound `request` and its SQL
  dependencies read `Kobenhavn` (the visitor), while the outbound `GET /v1/forecast` dependency and
  the `HttpRequestException` it raised both read `Des Moines` — the app's own datacenter. Read a
  city off an exception row and you will place a provider failure in a city no user was in. Two
  consequences: resolve the visitor from the operation's own inbound `request` row, never from the
  exception; and on a circuit-scoped exception (no `operation_Id` at all) treat `client_City` as
  meaningless rather than as a hint. This also makes datacenter cities worth recognising as
  *monitor* traffic in a raw timeline — one `GET /` per minute from Des Moines, San Antonio,
  Northlake, Quincy and Boydton is uptime checking, not five visitors.
- If `az monitor app-insights query` returns zero rows where you expect data, the workspace-backed path is the fallback: `az monitor log-analytics query -w <workspace_id> --analytics-query "AppExceptions | ..."`. Note the table names differ (`AppExceptions`, not `exceptions`).
- **A handled, logged error arrives in `exceptions`, not `traces`.** The App Insights `ILogger`
  provider ships any `LogError`/`LogWarning` that *carries an exception object* as
  `ExceptionTelemetry`. So a `catch (Exception ex) { _logger.LogError(ex, "Pass failed"); }` produces
  an `exceptions` row and **no** matching `traces` row — searching `traces` for the log message finds
  nothing and reads as "it never happened". Two consequences. First, never conclude a catch block did
  not run because its message is missing from `traces`. Second, this is how you tell handled from
  unhandled without reading the stack: `customDimensions.CategoryName` on an exception row is the
  *logger category*, i.e. the class that caught and logged it. A framework category
  (`Microsoft.EntityFrameworkCore.Query`, `Microsoft.AspNetCore.*`) means nothing of the app's caught
  it; an application category means something did — go read that class's handler before classifying
  it a bug.
- **The same category rule applies to `traces`, and it cuts the other way: a framework Warning can
  describe a path the app deliberately handles.** A framework component often logs its own complaint
  *before* handing control to the app's hook, and the app's handler may then log at Information —
  which the standard `severityLevel >= 3` / `== 2` passes never request (§4). So the only trace you
  see is the framework's, at Warning, for an outcome that is entirely by design. On one run
  `Microsoft.AspNetCore.Authentication.Google.GoogleHandler` logged `'.AspNetCore.Correlation.<t>'
  cookie not found` for a visitor who had signed in successfully two minutes earlier and merely
  replayed a torn-down tab's callback; the app's `OnRemoteFailure` hook classified it, logged at
  Information, and redirected. **Before classifying a framework auth/middleware Warning, look for the
  app's own hook on that extension point** (`OnRemoteFailure`, exception filters, `IExceptionHandler`)
  and check the raw per-request timeline for the visitor. Neither the trace's severity nor its
  category tells you the user-visible outcome.

## 3. Exceptions

```
exceptions | summarize cnt=count(), firstSeen=min(timestamp), lastSeen=max(timestamp), sampleOuter=take_any(outerMessage), sampleInner=take_any(innermostMessage), sampleMethod=take_any(method), sampleAssembly=take_any(assembly) by problemId, type | order by cnt desc
```

`problemId` **is** the signature. Use it directly; do not invent your own.

**But it is a *method* key, not a defect key — and a ledger keyed on it alone will skip a live bug.**
`problemId` is `<exception type> at <throwing method>`. Two unrelated defects that fault in the same
framework method collapse into one key. On one run
`Microsoft.Data.SqlClient.SqlException at Microsoft.Data.SqlClient.SqlConnection.OnError` had sat in
the ledger for two weeks as an accepted `UserVisits` unique-index race; the same key came back
carrying a **different** inner message — a duplicate `SentEmails` key that proved a user had been
emailed twice. A dedup pass that stops at the key never reads the second one. **Compare
`sampleInner` against the ledger's recorded message before skipping a known problemId**, and record
that message in the ledger entry so the next run can.

**One handled failure also produces two problemIds, not one.** A `catch { _logger.LogWarning(ex, …) }`
in application code emits the app's own `ExceptionTelemetry` row *and* the framework's — EF's
`Microsoft.EntityFrameworkCore.Update` Error-level log fires for the same `SaveChangesAsync`. They
arrive tens of milliseconds apart with identical messages and different `problemId`s (one framework
category, one application category — see §2). Reconcile them into a single finding by timestamp
proximity and identical `innermostMessage`, and triage the **application** one; the framework row is
the same event seen from underneath, and filing both files the same bug twice.

**A by-design warning can still carry a finding — read the identity fields, not the message.** Once
you have decided a handled-exception key is the healthy path logged loudly, the temptation is to note
the rate and move on. Do not stop before projecting `customDimensions`: an app that logs
`LogWarning(ex, "… for user {UserId} on plan {PlanId} …")` stamps those parameters as their own
dimensions, and the *distribution* of the identities is evidence the aggregate cannot show.

The rule that pays: for anything emitted by a periodic background job, **count distinct entity ids
against occurrences within a single pass**. A job that visits each entity once per pass cannot log
about the same id twice in the same pass — unless its work list contains that entity twice. On one
run six identical-looking claim-collision warnings landed inside five seconds of one hourly pass, and
four of them were four different users doing exactly what the design intended. The finding was that
the other three all read *user 1*. The only way to reach that is three rows for one user in the table
the job iterates — which turned out to be a documented, tolerated duplicate that a *sibling* service
handled by grouping and this one did not. The loud key was harmless; the thing it revealed was a
daily duplicate email on a code path with no error telemetry of its own at all.

So: a signature classified `noise` is not finished being read. Ask what each occurrence proves about
the *state* of the data, not only about the level of the log.

## 4. Failed requests — a 404 is not an exception

An exceptions-only sweep is structurally blind to everything the app *handles*. One project served 404 to a live calendar subscriber for 21 hours while every scheduled run reported green.

```
requests | where success == 'False' | summarize cnt=count(), firstSeen=min(timestamp), lastSeen=max(timestamp), avgDurationMs=avg(duration), sampleOperationId=take_any(operation_Id), sampleRole=take_any(cloud_RoleName) by name, resultCode | order by cnt desc
```

- There is no `problemId` on `requests`. Synthesize `req:<normalized route>:<resultCode>` — the `req:` prefix makes collision with a real problemId impossible.
- **Normalize the route first.** `name` embeds path parameters (`GET /api/calendar/<32 hex>`), so a raw key files a fresh issue per subscriber.
- Threshold: **5xx files on the first occurrence; 4xx only at ≥5** on the same normalized route.

**Query the route's successes in the same breath as its failures.** Dropping `where success == 'False'`
and summarizing by `name, resultCode` costs one extra query and often hands you the mechanism for free,
because the ordering of the good and bad answers is the finding. On one run `POST /api/injuries`
showed a single `201` six seconds before a run of eight `500`s from the same user: the create path
worked and every *subsequent* save of that same record failed, which pointed straight at what the
client echoes back on a repeat write rather than at the handler's happy path. The failure pass alone
shows eight 500s and no shape at all.

**`success` is app-writable, so the failure pass is not the whole 4xx picture.** An
`ITelemetryProcessor` in the app can set `RequestTelemetry.Success = true` on a request that really
answered 4xx; the row keeps its true `resultCode` and vanishes from `where success == 'False'`. This
is a legitimate thing for an app to do — it stops an expected, self-correcting 4xx from competing
with real failures — but it silently blinds a sweep that only ever runs the failure pass, which then
reports "those errors stopped" when they merely turned green.

**A multi-leg flow that dies between the legs produces no error row at all.** The `requests` passes
in this adapter key off a status code — the `exceptions` and `traces` passes do not, and neither
sees this either — and the start of a redirect flow answers `302` whether
anything ever comes back. An OAuth sign-in, a payment hand-off, an email-confirmation bounce — all
of them can be 100% broken while `exceptions`, `requests | where success == 'False'` and every trace
category stay perfectly clean, because the failure happens on the far side of a redirect the server
never sees.

So for any hand-off flow, **count the legs against each other** rather than looking for a bad status:

```kusto
requests
| where name in ('<start>', '<provider return>', '<your callback>')   // exact names, not has_any
| extend leg = case(name == '<start>', 'start',
                    name == '<provider return>', 'return',
                    'callback')
| summarize legs = make_set(leg), resultCodes = make_set(resultCode) by operation_Id
| summarize flows = count() by tostring(legs)
| order by flows desc
```

**Correlate the legs before calling anything a completion ratio.** Counting each leg on its own
cannot show that a return belongs to a start: a window can hold 88 starts and 4 returns from
entirely different visitors and still read as a 4.5% completion rate. Group by `operation_Id` first,
as above, so each row is one flow and the shape of `legs` says where it stopped.

**Where no key survives the redirect, say so and stop at counts.** Some providers drop the
correlation id across the hand-off. Then the aggregate leg counts are all you have — report them as
counts, label the funnel unconfirmed, and require manual confirmation before treating the drop as a
defect. Do not present an uncorrelated ratio as a completion rate.

**Never bin the legs by day.** `bin(timestamp, 1d)` cuts a flow at midnight, so a start at 23:58
and its return at 00:02 land in different buckets and the day reads as a completion collapse that
never happened. Correlating by `operation_Id` as above already avoids this. If you want a trend,
bin the flow's *start* time after the correlation — never each leg independently.

**Match the legs exactly.** `has_any` tests indexed *terms*, not whole `name` values, so an
unrelated route sharing a term joins the funnel and quietly distorts the very ratio being measured.
Use `in (...)` with exact names where they are stable; where they carry ids, normalize the route
first and group on the normalized value.

A start-to-callback ratio that collapses is the finding. On one run this turned a lone 4-hit `429`
— under the filing threshold, and correct behaviour by the limiter that emitted it — into a 30-day
funnel of 88 sign-in starts, 4 returns and 2 completions, with a raw timeline showing one user
pressing the button eleven times and then signing in with the other provider on the first try. The
`429` was the only thing any error pass could see, and it was the least interesting part of it.

Two guardrails when you find one. First, **prove the request you emit is well-formed before blaming
the provider** — one `curl` on the start route reads the `Location` header, and a second on that URL
shows whether the provider rejected the parameters or merely asked the visitor to log in. Second,
**say that the cause is not observable** when it isn't: what happens between your redirect and the
provider's answer leaves no server-side trace. Record that as *unobservable* alongside the measured
funnel — do not reach for a classification the evidence does not support. `noise` means a healthy
path logged at the wrong level and `external` means a provider or network failure; an outcome you
cannot see is neither until something outside this telemetry says which. Report the funnel, name
what could not be seen, and classify only when there is evidence for it.

**A challenge status is the first leg of a flow, not a failure — correlate before classifying it.**
Some protocols answer 401 or 400 *by design* to tell the client what to do next, and the aggregate
pass shows only the refusal. The same `operation_Id` correlation that measures a broken funnel also
settles a healthy one, and it is the cheapest evidence there is. On one run two `POST /mcp` 401s
looked like an auth failure until the per-operation timeline showed the whole OAuth 2.1 hand-off
completing behind each of them — `POST /mcp` 401 → `/.well-known/oauth-protected-resource` 200 →
`/.well-known/oauth-authorization-server` 200 → `POST /oauth/register` 201 → `GET /oauth/authorize`
200 → `POST /oauth/authorize` 302 → `POST /oauth/token` 200 → `POST /mcp` 200. The 401 *was* the
discovery challenge. Read the operation forward before writing "auth failing" down, and record that
you observed the continuation rather than assumed it.

That timeline is also the discriminator against the scanner reading of the same routes. The same
window carried `GET /oauth/authorize` 400, `GET /oauth/token` 400 and `GET /oauth/register` 405 from
a spread of hosting cities inside one four-minute sweep — the correct refusals to a parameterless
probe, and *not* the same clients. A real client's refusal is followed by its own continuation in
the same operation; a scanner's is followed by nothing.

**And read a status against whoever actually owns the route, not against the `MapX` you grepped.**
A framework that claims an endpoint by configuration installs middleware ahead of routing, so it can
answer a status the route registration cannot explain. `MapPost("/oauth/token")` plus OpenIddict's
`SetTokenEndpointUris("/oauth/token")` answers a GET with **400**, not the 405 the bare `MapPost`
implies — and a 400 on a token endpoint reads like a malformed real exchange, which is exactly the
wrong conclusion. Before triaging an odd status on a route, check whether a library was configured to
own it.

So for any route you are actively watching, **query it by URL and result code, never by `success`**:

```
requests | where name has '/<route>' | summarize cnt=count(), firstSeen=min(timestamp), lastSeen=max(timestamp) by name, resultCode, success, tostring(customDimensions.<Marker>) | order by lastSeen desc
```

And when such a rule stamps a *reason* dimension, read what the code actually tests before trusting
the name. One of these labelled every sessionless 400 `session-restart-renegotiation` on the strength
of three facts — status 400, path prefix, header absent — and never checked whether a 200 followed.
The pathological case and the benign case therefore carry the identical reassuring label. **A
dimension asserting a recovery is not evidence of one**; get that from the raw per-request timeline.

And the label was not merely uninformative — it pointed at the wrong *cause*. Three runs derived the
mechanism behind those 400s from the repo's own handshake tests ("no session header AND not an
initialize") and were confident about it. When a diagnostics middleware finally logged the SDK's own
error body, the real reason was something no test covered: `The MCP-Protocol-Version header value
'2026-07-28' is not supported`. **When the status code is produced inside a third-party SDK rather
than by your own code, a mechanism inferred from your tests is a hypothesis, not a finding** — your
tests only pin the cases someone thought of. Get the SDK's own error text into a log and read it
before writing a root cause down. Until then, the honest report says "reason not observable", which
is what makes shipping the logging the correct next step rather than a detour.

That episode has a second, portable half. The rejected value was a *protocol version*, and the reason
the server could not speak it was a floating package reference floored at a major: `Version="1.*"`
resolves to the newest 1.x forever, so a peer that moves to a newer protocol revision can never be
met. **On any version-negotiation failure, compare the supported set compiled into the resolved
package against the newest published package** — for .NET that is a two-minute check with no build:

```bash
curl -s "https://api.nuget.org/v3-flatcontainer/<package>/index.json" | python -c "import json,sys; print(json.load(sys.stdin)['versions'][-10:])"
# then read the version strings out of each DLL (they are UTF-16 in the metadata heap):
python -c "import re;b=open('<pkg>.dll','rb').read();print(sorted(set(re.findall(r'20\d{2}-\d{2}-\d{2}',b.decode('utf-16-le','ignore')))))"
```

**A request row is not the only way an action reaches the app — and some frameworks have a whole
channel this adapter cannot see.** On Blazor Server a button click travels over the circuit's
persistent connection and runs its handler *inside that circuit*, resolving services from DI. No
per-action HTTP request is made, so it appears in no `requests` pass, at any status code.

Scope the blind spot to that shape — **an action carried by a persistent connection with no
per-action HTTP request** — and do not assume it of every SignalR or WebSocket UI. A generic hub
invocation is a message on an already-open connection, and plenty of WebSocket front ends still
fire an ordinary HTTP request alongside the socket. Check how the app dispatches the action before
claiming its trigger is unloggable.

If the handler's own log is Information-level, §6's passes do not see it either — but the reason is
the pass, not the data. The row does land in `traces`, with `severityLevel == 1`; the standard
passes ask for `severityLevel >= 3` and a second at `== 2`, so they never request it. Widen to
`severityLevel >= 1` over the minutes around a finding when that matters, and say which you ran.
Between the two gaps the action is effectively invisible — right up until something it did fails
later, in a background worker, with no `operation_Id` to tie it back.

That is exactly how one run's only new bug presented: three foreign-key violations from a timer-driven
flush, `operation_Name` empty, `operation_Id` empty. The cause was an account deletion 18 seconds
earlier that left **no row of its own anywhere in telemetry**. It was identified from the one HTTP
side-effect the flow happened to have — a `POST /auth/clear-cookie` fired by the sign-out that follows
a confirmed deletion.

So: **an exception with no `operation_Id` may be a background-worker failure, and its trigger is
often not in `requests` at all.** The absence does not by itself identify the execution context —
App Insights omits `operation_Id` whenever no correlation context is active, which also covers
SignalR execution and direct `TelemetryClient` calls. Read it as a prompt, not a verdict: scan the
surrounding minutes of the *whole* request stream for a side-effect that brackets the failure, and
check whether the suspected trigger is a circuit-driven action before concluding it never happened.
Say so in the report's blind-spot section — for such an app, "no failed requests" is
silent about an entire class of user action.

**A stack with no app frames is not evidence that the defect is not yours.** A framework component
that fails *on the far side* of the wire produces a stack made entirely of framework frames by
construction: a Blazor Server render batch that the browser cannot apply comes back as
`InvalidOperationException at ExceptionDispatchInfo.Throw` under
`Renderer.InvokeRenderCompletedCallsAfterUpdateDisplayTask`, carrying the *browser's* error text
(`TypeError: Cannot read properties of null (reading 'insertBefore')`) and nothing of yours. The
absent app frame is a property of where the failure surfaced, not of who caused it. One sweep
called such a key `external` across six consecutive runs on exactly that reasoning while it was
killing a quarter of the app's new-user onboarding sessions.

What settles it is the raw surrounding request stream, and it is cheap:

```kusto
requests
| where timestamp between (datetime(<crash ts>) - 4m .. datetime(<crash ts>) + 3m)
| where name !has '_content' and name !has '.woff2' and name !has '.css' and name !has '.js'
| project timestamp, client_City, name, resultCode
| order by timestamp asc
```

Filter the static assets out or the navigations drown. Then read the timeline as a story: what the
visitor did just before, and — decisively — **what they did next**. A visitor who reloads the same
route within seconds of the crash, three or four times, and then leaves for a different page has
told you both that the failure is user-visible and which route owns it. That is the whole finding,
and it is invisible in every aggregate.

**Then measure the gap, because the offset from the connection handshake names the lifecycle phase.**
A circuit-scoped exception carries no `operation_Id`, so the timeline is the only thing that can say
*when in the page's life* it fired — and the framework's own handshake request is a clock you already
have. On one run the crash landed 0.77–1.02 s after each `POST /_blazor/negotiate` and 0.77–2.10 s
after the page `GET`, three consecutive times, with no request in between and the visitor leaving
straight afterwards. That is the server-rendered → interactive hand-off patching the existing DOM,
not an idle page re-rendering itself. The distinction decided the whole investigation: an earlier fix
attempt had measured the page in a test harness, found two render batches inside 180 ms and then
nothing, and concluded there was no post-connect render for the fault to race — so it could not
reproduce the bug. The production gap said the render it needed was the transition itself.

Two rules fall out. **A "no re-render happens here" conclusion drawn from a harness is a claim about
the harness**; check it against the handshake-to-crash offset in real traffic before building on it.
And **repetition across reloads is the cheap significance test**: three crashes at the same offset,
from one visitor inside half a minute, is a far stronger signal than three crashes spread over a week,
and it costs one timeline query to see.

**A route correlation and a demographic skew are not competing explanations — check whether they
compose.** The same run produced both: every crash was on the onboarding wizard *and* the affected
visitors were in France, Japan, Germany, Italy, Spain and South Africa with **zero** from the US,
by a wide margin the largest traffic source. Each looks like it explains the other away. The route
skew has an innocent reading — a page only one cohort ever visits inherits that cohort's geography
for free — and that reading was written into the issue as "the geography is not the signal". It was
**wrong**. Both facts were load-bearing: the route said *where* the fragile markup was, and the
geography said *what the client was doing to the page* (Chrome auto-translates when the browser
language differs from the page's, and remembers the choice — which is why every reload crashed
again). A machine translator replaces each bare text node with an element of its own; Blazor holds
a direct reference to the node it rendered and anchors inserts on it, so the reference detaches and
`nextSibling.parentNode.insertBefore(...)` throws on `null`. Be precise about *which* node, because
the obvious reading sends a fix agent to the wrong markup: the anchor is the node **after** the
insert position — `const nextSibling = parentLogicalChildren[childIndex];
nextSibling.parentNode.insertBefore(child, nextSibling)` — not the label sitting beside it. That
matters twice over. It makes the checkable thing a **stale reference**, present the instant the
translator runs and independent of whether any render follows; and because the framework keeps those
logical-children arrays on a symbol-keyed expando, a test can walk them from the live page and name
every detached entry, which is a far tighter guard than asserting on labels a human remembered to
list. It also explains why only *some* bare labels are fragile: text inside a static markup blob is
never recorded per-node, so only text the compiler split into its own logical child can ever go
stale.

So when a finding offers you two independent skews, **do not spend one to explain the other**. Ask
what mechanism needs both to be true at once. And where a hypothesis is about the client, say
plainly that server telemetry cannot settle it and hand the fix agent the means to test it —
here a Playwright test that applied the translator's own `<font>`-wrapping rewrite to the live page
reproduced the production error verbatim in one run, and scope-narrowing pinned the failure to a
single 12-text-node region.

**A skew that was load-bearing once is still only evidence, and the recurrence is what tests it.**
That same key came back 33 hours after its fix deployed — on the route the fix had wrapped, from a
visitor in the **United States**, the one cohort the original issue had recorded as having *zero*
occurrences and had used to argue the mechanism. One counter-example retires a screening rule
outright: a demographic pattern earned from n≈8 tells you what the cohort was doing, not what only
that cohort can do. When you write a skew into an issue, write the count it rests on beside it, so
the next run can see how little it takes to break.

**And when a fix ships with tests, ask what the test harness structurally cannot reach — that is
where the same defect survives.** Here every guard was per-label: an integration test asserting the
specific rendered labels the fix wrapped, and a browser test that drove translated pages but whose
test classes all derived from a *signed-in* page fixture. So no test in the suite had ever loaded a
signed-out page, and the sign-in route — which the fix had edited — went straight back to
production unexercised in the very state the bug needs. Read the fixtures and base classes, not
just the test names: a suite can look like it covers a page while being unable to render it in the
state that fails. The portable form is that a per-instance fix plus per-instance tests closes the
instances, never the class, and the sweep is what notices the difference — the recurrence is one
event against a fix's whole verification pass, so it only shows up if you compare `lastSeen`
against the fix's deploy time on **every** key the ledger calls resolved, not just the open ones.

**A route you deleted on purpose can still be a finding, and the delay before it shows is the trap.**
When a release removes an integration, the provider on the far side does not know: a webhook push
subscription, a callback registration, a polling job keyed to your URL all keep firing, and every one
of them now lands on a route that no longer exists. The failed-request pass sees them as 4xx on a
route nobody in the codebase can explain, which reads as scanner noise — check whether the route used
to answer 200 before you dismiss it. On one run `POST /webhooks/strava` had answered 200 fifty times
and then 404 fifteen times, in bursts of three to six attempts spaced exactly two minutes apart: that
regular retry ladder from one datacenter city is a provider, not a scanner.

**The first 404 arrived twenty-eight hours after the removal deployed**, because it takes a real
event on the provider's side to produce one. So the sweep that ran the morning after the removal saw
a perfectly clean window — the absence proved only that nobody had recorded an activity yet. Read a
removal deploy's first quiet window as *not yet observed*, not as *clean*.

The classification is the interesting part: this is a defect, and it usually has **no correct fix
inside the repo**. Restoring a tombstone endpoint contradicts the removal and keeps the provider
delivering forever; filtering the route out of telemetry hides a real client asking a real route. The
remediation is an authenticated call to the provider's own API to delete the registration, which
needs the credentials and is a write against a third party — file the issue, do not spawn a fix
agent, and say plainly in the report why. Two things worth checking while you are there: whether the
removal PR also deleted the tooling that could unregister it (it usually did — that admin page was
part of the same feature), and whether the provider's credentials are still sitting in the live app's
configuration after the deployment template stopped setting them. Removing a setting from a template
does not remove it from a running app.

**And the mirror image costs nothing to rule out: a route you have not built *yet*.** Before triaging
a brand-new 4xx on a route nobody in the codebase can explain, **list the open pull requests**. A
feature branch's preview environment — a Netlify deploy preview, a staging front end, a locally-run
UI — routinely points at *production* for its API, so the new page calls an endpoint that exists only
on that branch. On one run four `OPTIONS /api/preview-plan` 404s from a single city over four minutes
looked like a probe until the open-PR list showed a PR opened **two minutes after the last one**,
adding exactly that endpoint plus the marketing page that calls it. The route's only other mention in
the whole repo was a research document *proposing* it.

Three tells separate this from a scanner, and you want all three: the method is `OPTIONS` (a browser
CORS preflight, which scanners do not bother with), the hits come from **one** `client_City` in a
tight burst with nothing else probed alongside, and the path reads like the project's own naming
rather than a generic `/wp/` or `/.env`. Grep the repo including docs and research notes — a hit in a
design document is strong evidence that this is your own work in flight.

Classify it **external** (a client asking for a route the app genuinely does not have) and file
nothing. Two things are still worth writing in the report. First, check the hit count against the
*filing* workflow's threshold, because preview traffic is bursty and will trip it. Second, when the
front end and the API deploy on **separate triggers from the same merge** — a static host and an app
host, say — say so: if the front end publishes first, every real visitor gets this identical 404 until
the API catches up, and that window is a real outage nobody will be watching for.

## 5. Cold-instance join — the trick that cracks transients

For any transient that resists explanation, join it against instance first-request time:

```
requests | summarize firstSeen=min(timestamp) by cloud_RoleInstance
```

If every occurrence is in the first ~35 s of a fresh instance a few minutes after a deploy, it is a warm-up/pool problem, not randomness. This turned a three-run shrug into a root cause.

**The same join is what separates ordinary boot cost from a real latency regression.** Slow *successful*
requests are worth a pass of their own (`requests | where duration > 5000`), because a 200 that took
30 s is still a defect and no failure pass will ever show it. But most of what that pass returns is
the app booting. Project the instance and compare each row against that instance's `firstSeen`: rows
where the two are equal are the first request on a cold instance and are the known cost of a restart.
Rows past the age threshold below are the findings; hours-old instances are simply the clearest
of them. On one run, 18 of 19 slow rows
were boot cost and the single warm one — 10.6 s with every SQL dependency under 10 ms, so not the
database — was the only thing worth writing down.

**Do not use `timestamp == firstSeen` as the cold test — it is far too strict.** Equality catches
only the literal first request on an instance; a boot serves several requests inside its first few
seconds, and every one after the first is then labelled *warm*. On one run that split returned 22
"warm" rows out of 46, and about seventeen of them were 8–30 s into an instance's life — a
`/_blazor/negotiate` 13 s after boot, four `GET /` inside the same 16 s window, a `/plan` 24 s in.
Only five rows were genuinely warm. Compare the **age**, not the timestamps:

```kusto
requests
| where duration > 5000 and success == 'True'   // success is a string here, not a bool
| join kind=leftouter (
    requests | summarize firstSeen=min(timestamp) by cloud_RoleName, cloud_RoleInstance
  ) on cloud_RoleName, cloud_RoleInstance
| extend instanceAgeSec = datetime_diff('second', timestamp, firstSeen)
| where instanceAgeSec > 60
| order by duration desc
```

**`firstSeen` is a heuristic for process start, not a measurement of it.** `cloud_RoleInstance`
names a host or container, so two roles on one host collide unless `cloud_RoleName` is in the key —
hence both above. Even then the value can outlive a process restart, which makes `firstSeen`
*earlier* than the process actually running and lets a genuine cold-start row clear the 60-second
filter. Where the app emits a process-start marker, key on that instead; where it does not, treat a
surviving row as a candidate and confirm it before filing.

The join is the whole point and has to be written out: `firstSeen` comes from the per-instance
summary above, so a bare `| extend instanceAgeSec = ...` fragment has no input table and no
`firstSeen` in scope. `leftouter` keeps a row whose instance never summarized rather than dropping
it silently — such a row has a null `instanceAgeSec` and fails the filter, so inspect it by hand.

**Sixty seconds is the threshold for the whole section** — do not pair this filter with a looser
"up for hours" rule in prose, or a row 61 seconds past `firstSeen` gets two contradictory verdicts.
It is generous on purpose: a slow request in a boot's first minute is still boot cost, and the pass
exists to find the row that is not. Raise it if a project boots slowly, but raise it in both places.

**A warm-up that logs success is not evidence that the cold path is warm — read what it warmed, not
whether it finished.** Apps that pre-compile query shapes at boot usually log one line on the way
out, and a sweep that finds that line naturally reads the cold-start question as closed. It is not:
the log says the routine ran, and says nothing about coverage. On one run the instance logged
`Database warm-up completed in 3691ms`, well inside a 15 s budget, and fifty seconds later the first
`GET /plan` still paid two 15 s command timeouts — on a query the warm-up had just executed.

The mechanism is worth knowing because it defeats the obvious warm-up design. The warm-up called the
real repository method for a **user id chosen to match no row**, which is the right instinct: it
compiles the shape without reading anyone's data. But the query was an EF `AsSplitQuery()` — one SQL
command for the root and a separate command per collection navigation — and with a zero-row root
there are no principals to load collections for, so EF never issues the child commands and their SQL
is never generated or cached. **A sentinel-id warm-up over a split query compiles only the root.**
Generalize it past EF: any warm-up whose work is *conditional on the data it found* warms less than
it appears to when it is deliberately pointed at nothing.

So when a cold-start timeout recurs after a warm-up fix, read the failing SQL against the warm-up's
list before concluding the list is short a shape. If the failing shape is a *child* of one already
on the list, the list is right and the warming call is wrong. Two tells, both cheap: the warm-up's
own duration (a few hundred ms of commands where you expected a dozen), and whether the failing
statement contains the warmed query as a subquery — the child command re-states the root inline, so
it reads as the same query with one more `INNER JOIN`.

**And the same count is how you *verify* a warm-up fix, without waiting for a natural cold request.**
This is the useful half. A warm-up fix's success condition is normally a user-visible one — the first
request on a fresh instance stops being slow — and that row can take days to appear, because it needs
a restart and a real visitor to coincide. One project waited three sweeps for it while an app that
had been up 37 hours refused to recycle. Meanwhile the fix's *mechanism* is directly countable: warm-up
work is dependency telemetry like any other, so count the commands each boot issues and split the
series on the deploy timestamp.

```kusto
let boots = traces | where message has '<the warm-up completion message>' | project inst=cloud_RoleInstance, warmEnd=timestamp, msg=message;
dependencies | where type == 'SQL' | join kind=inner (boots) on $left.cloud_RoleInstance == $right.inst | where timestamp < warmEnd and timestamp > warmEnd - 30s | summarize sqlCommands=count() by inst, warmEnd, msg | order by warmEnd asc
```

A clean step at the deploy boundary — every boot before it at one count, every boot after it at
another, holding across several boots — is direct evidence that the warm-up now compiles what it
did not before. On the run that found this, the series was fifteen pre-deploy boots at **16**
commands and four post-deploy boots at exactly **23**, and the `+7` matched the fix's diff line for
line: one root lookup plus the six child-collection commands a split query had been skipping.

Three cautions, all of which cost something to learn:

- **The 30-second lookback is a heuristic, and real traffic contaminates it.** Two pre-deploy boots
  in that series read 36 and 25 against a mode of 16 — those are ordinary requests overlapping the
  window, not warm-up work. Read the mode, not the mean, and treat a lone outlier as noise unless the
  whole post-deploy series moves.
- **Warm-up duration is not the signal — the count is.** In the same data the post-deploy durations
  (1545/2306/2442/4860 ms) sat entirely inside the pre-deploy range (642–3691 ms). Duration varies
  with DTU contention and proves nothing on its own; a run that reaches for it instead of the count
  will report a fix as unproven, or worse, as proven.
- **This settles the mechanism, never the timing.** It says the commands are now issued at boot. It
  does not say the first cold request got faster. Report it as exactly that, keep waiting for the
  user-visible row, and do not let a clean step promote the entry to "verified".

**Budget for that wait, and do not read its length as a verdict.** The user-visible row needs a fresh
instance *and* a real visitor inside the same minute, and those two are much less correlated than they
look: an uptime monitor reaches a new instance within seconds, so a boot can come and go with nothing
but synthetic `GET /` in its first two minutes. On the project that found this, seven days and eleven
post-deploy boots produced no qualifying row — three of them on one day, all serving only monitor
traffic while young — and then **two arrived within ten hours of each other** once ordinary traffic
happened to land on a boot. So a fresh instance is necessary and not sufficient; count boots *that also
served a real request while young*, not boots. Two things make the wait cheap rather than anxious: the
command-count step above already carries the mechanism, and the failure class itself is directly
checkable in the meantime (`dependencies | where success == false` over the window, plus the per-minute
dependency max on each new instance's first two minutes), so you can report the defect as absent long
before you can report the latency as fixed.

The SQL `data` field is often empty on these rows (no command text captured), so the count is
frequently all you get — which is fine, because the count is what the question needs.

**A cold start has TWO independent costs, and fixing one tells you nothing about the other.** Query
compilation is the one everybody warms; **connection establishment** — TCP, TLS and the database's own
login — is the one nobody does, and it is invisible in every query-shaped check. The tell is a
dependency row whose `resultCode` is **empty** and whose `data` field reads `InternalOpenAsync` (or the
equivalent open call for your client), sitting just before the command timeouts rather than among them.

This is how a verified warm-up fix can read as regressed when nothing about it regressed. On one run
two fresh instances each completed the warm-up with the *exact* post-fix command count (23, the step the
fix introduced, holding across every boot in the window) and then served their first real page request
in **23 s and 38 s** — because a single `InternalOpenAsync` had taken **29 287 ms** and two commands
timed out on top of it. The sweep that had earlier written "verified, closed" against that fix was not
wrong about the fix; it had measured the only cost it knew to look for.

**That row has more than one spelling, so do not key on the one you saw first.** On the same project the
identical failure later arrived as `resultCode` **`0`** with `data` = **`Open`**, 24 079 ms, where every
earlier occurrence had read `data` = `InternalOpenAsync` with an **empty** `resultCode`. A ledger note or
a KQL filter written around either literal silently misses the other. Key the pass on
`type == 'SQL' and success == false` and *read* `data` and `resultCode` as evidence, never as the
predicate — the client library chooses that label and is free to change it between versions.

Two checks settle which cost you are looking at, and both are cheap:

- **Read the SQL text of what timed out.** A primary-key lookup on a single table cannot be slow for
  want of a compiled plan. If the failing statement is trivial, the time is not in compilation.
- **Grep the repo for a pool minimum** (`Min Pool Size`, `MinPoolSize`, or your driver's spelling) and
  for an explicit open in the warm-up (`OpenConnection`, `OpenAsync`, `CanConnectAsync`). A warm-up that
  only runs queries through an ORM leaves pool population to chance: the first real visitor still pays
  the login, and pays it *concurrently* with the uptime monitor and everyone else who arrived in the
  same few seconds.

Corroborate with whatever else the boot was doing. In that same window the instance's managed-identity
token acquisition took **10 923 ms** twenty-six seconds before the stall — the boot was contending for
everything at once, which is the shape of a resource problem and not of a missing query plan.

**And before filing either a warm slow request or a warm command timeout, check its timestamp against the
deploy history.** A rolling deploy runs two instances at once: the draining old one and the booting new
one contend for the same database and the same identity endpoint, so the window manufactures *both*
shapes within seconds of each other. On one run a 9 024 ms request on a 10-minute-old instance, a
background-job command timeout on that same instance two minutes later, an 8 790 ms request on a
*different* old instance an hour on, and a 24 s connection-open failure on its replacement 29 seconds
after that were all filed against three separate watches — and all four sat inside two deploy windows.
One `gh run list --workflow <deploy> --json headSha,createdAt,conclusion` separates "the tier is
contended while we roll" from four independent findings. Read it as a capacity fact, not a defect, and
say so.

## 6. Dependencies and traces

- `dependencies`, 30d, duration > 10 s — surfaces SQL stalls. Remember `dependencies.success` is a real bool here.
- `traces`, `severityLevel >= 3` (and `== 2` for a second pass), 7d.
- A recurring slow query at a stable rate on a small DTU tier is a **cost/tier fact, not a defect**. Note it so a future run does not rediscover it as new; do not file it.

## 6b. Verifying a telemetry *suppression* — fire a synthetic probe with a control

**First check you actually need this section.** It is expensive — a control, a threshold check, a
narrow-window query — and it is only needed when the fix's success condition is *absence from
telemetry*. If the fix changed what the **HTTP response is** — a redirect, a new route, a status-code
change — verify it at the HTTP layer instead and stop:

```bash
curl -s -o /dev/null -w "%{http_code} -> %{redirect_url}\n" "https://<host>/apple-touch-icon-180x180.png"
```

A 301/200 answer is complete proof on its own, needs no control, and costs **no telemetry at all**
because the request never reaches the 404 handler — so it cannot nudge a route over a filing
threshold the way a suppression probe can. One run verified four redirect paths this way in a single
command with zero side effects, the same week the suppression path had manufactured its own issue.
Reach for the control-and-window machinery below only when there is no response to look at.

When a fix's job is to stop something reaching App Insights (a `TelemetryProcessor` that drops
scanner 404s, a sampling rule, a filter), success looks exactly like "the traffic happened to stop".
Waiting for the next natural occurrence can burn days — one project carried "suppression still
unverified" for five runs because the scanner never re-probed the suppressed family.

Do not wait. Issue the request yourself, alongside a **control** the filter is known *not* to match:

```bash
curl -s -o /dev/null -w "%{http_code}" -A "<sweep>-verify" "https://<host>/maps/site.css.map"   # suppressed?
curl -s -o /dev/null -w "%{http_code}" -A "<sweep>-verify" "https://<host>/.DS_Store"           # control
```

Then query a narrow window (`--offset PT30M`) for both. The control is what makes the result
readable: control present + target absent = the filter is live. Both absent means ingestion is
lagging or broken and you have learned nothing yet — retry, do not conclude. Ingestion latency
measured on Azure App Service is **under two minutes**.

Pick a control that already exists in the ledger as known noise, so the test adds no new signature.
Only ever probe paths that 404 by design — never a mutating route.

**Your probe is real telemetry, and a CI triage workflow will file an issue about it.** On one
project the control (`GET /.DS_Store`) sat at 6 hits in the window — one short of the filing
workflow's repeat threshold. The single verification probe made it 7. Two and a half hours later
the daily triage workflow auto-filed an issue for the route, and a fix PR was opened to suppress
it. The sweep manufactured its own finding, and nothing in the issue said so.

So before firing a control, check where it stands against the *filing* threshold, not just against
your ledger — a route already well above the threshold (already filed, already tracked) is safe,
and one sitting just under it is not. Check the target's count too, but read it the other way round:
if suppression works the target probe is never ingested, so it cannot advance the count at all. A
target that *does* appear and crosses the threshold has not proved the suppression — it has proved
the suppression failed, and the issue that gets filed is a real one.

Then re-pick the control every time. Once a suppression PR lands, the control it used stops being
a control — the filter now matches it, and the next verification reads "both absent" and concludes
"ingestion is broken" when in fact the technique lost its reference. Confirm the processor still
does not match your control before trusting the result.

## 7. Deploy drift

Compare `origin/<default branch>` head against the SHA of the last successful deploy workflow run. Prod running stale is a finding — it has silently happened for 3 days before.

**`gh run list --status success` does not mean "conclusion: success".** `--status` filters the run *status* field (`queued` / `in_progress` / `completed`); passing a conclusion value to it returns a stale, wrongly-ordered subset rather than an error. On one run this reported the newest successful deploy as 4 days old while prod was in fact current — a false "prod is stale" finding, which is the exact failure this check exists to avoid.

Ask for the conclusions and filter yourself:

```
gh run list --repo <slug> --workflow deploy.yml --limit 15 \
  --json databaseId,headSha,createdAt,status,conclusion,displayTitle
```

Then read the newest row whose `conclusion` is `success`. **Also read the `status` field on every row, not
just `conclusion`.** Two silent-staleness shapes never appear as a failed deploy: a run that ends
`conclusion: startup_failure` with **zero jobs** (the workflow never started, so nothing is red to
look at), and a run left in `status: queued` indefinitely. On one project a push to the default
branch produced both — one `startup_failure` and one run still `queued` 44 hours later — and prod
sat one commit stale for 9.5 hours until an *unrelated* later push carried the change out. Nothing
in the deploy history said "failed"; the newest `success` row simply predated the merge. Confirm the
newest successful deploy's SHA actually **contains** the newest merge (`gh api
repos/<slug>/compare/<merged-sha>...<deployed-sha>` → `status: ahead`, `behind_by: 0`), and flag any
non-completed run as a finding a human must clear. Do the same to spot **failed** deploys: a failure that a later push silently corrected still matters, because any fix that shipped in the failed run was not actually live until that later push — which can invalidate a "this is now suppressed/fixed" claim made by an earlier sweep. Check the deploy time of a fix against the last occurrence of the thing it fixes before calling it verified.

Also note `gh run list --json ... --template` chokes on `{{"\n"}}`; pipe the JSON to a parser instead.
