---
name: ga-traffic-report
description: Pulls a week of GA4 traffic for every OpenSEO project, triages what needs fixing, keeps site audits on a cadence, and republishes one report artifact. Use from a scheduled task whose card names the data folder and the artifact; see docs/ga-traffic-report-card-template.md.
---

# ga-traffic-report

<!-- @doc:ga-traffic-report-card -->

Build the owner's GA traffic report and republish it to its artifact. The scheduled task holds a
**report card** (template: `docs/ga-traffic-report-card-template.md`). Read it first: every
**bold field** below comes from it. If the card is missing a field this file needs, stop and say
which one. Do not guess.

Read-only toward every account: never change anything in OpenSEO, GA4 or Search Console, and never
call an OpenSEO tool that spends credits (no get_serp_results, rank tracker runs, backlinks, keyword
research). Exceptions: the site audits in Step 1.4 (run_site_audit, plus whoami to watch credits),
and the GitHub writes in Step 2b: create or update the card's **Issue label**, and open, edit,
comment on and reopen issues, only in the repos the card's **Repos** field lists.

This skill's folder (the one holding this file, `build.py` and `template.html`) is the **skill
folder**. The card's **Data folder** holds this run's files: `report.json`, `history.json` and
`report.html`.

## Step 1 - gather data (free OpenSEO reads, plus the audits in 1.4)

Load the tools with ONE ToolSearch call:
select:mcp__openseo__list_projects,mcp__openseo__get_google_analytics_traffic_acquisition,mcp__openseo__get_google_analytics_page_performance,mcp__openseo__get_google_analytics_organic_overview,mcp__openseo__get_google_analytics_key_events,mcp__openseo__get_google_analytics_measurement_health,mcp__openseo__get_search_opportunities,mcp__openseo__list_site_audits,mcp__openseo__get_audit_issues,mcp__openseo__run_site_audit,mcp__openseo__whoami

Date windows (in the card's **Time zone**):
- Current = last 7 complete days, ending yesterday. Previous = the 7 days before that.

1. list_projects. For EVERY project call traffic_acquisition (channel_group, current window,
   comparePreviousPeriod true). Always pass the window's startDate and endDate: with no dates
   OpenSEO reports the last 28 days, which the page would mislabel as one week.
   Error code `ga4_not_connected` = put it in `notConnected` with its actionUrl, not an error.
2. For each connected site, call (in parallel where you can):
   - traffic_acquisition, source_medium, current window, limit 5.
   - page_performance, channel "all", current window, limit 8.
   - organic_overview over the last 28 days ending yesterday, trend daily (sparkline only).
   - key_events, breakdown event, channel "all", current window, comparePreviousPeriod true.
   - measurement_health (GA setup problems).
   - search_opportunities, default 28-day window, limit 100. OpenSEO sorts by its own score and
     then cuts at the limit, so a small limit can hide the high-impression pages Step 2 picks.
     If it errors because Search Console
     is not connected, set health.searchConsole=false; that is not a run error.
   - list_site_audits; if one exists, get_audit_issues severity critical and severity warning,
     limit 1 each, just to read the counts from `summary`.
3. Any other failed call: add {site, call, message (exact error text)} to `errors` and keep going.
4. Site audits (the one credit-spending step; cadence from the card's **Audits** fields). For
   EVERY project that has a `domain` (GA connected or not; skip projects with no domain), call
   list_site_audits. Skip a site with an audit in progress. Otherwise decide, at most ONE audit
   per site per run:
   a. Full audit, due when the newest completed audit with `ranLighthouse: true` finished
      **Full audit every** days ago or more (or there is none): run_site_audit with
      renderJavaScript true, runLighthouse true, and maxPages sized to the site (below). This is
      the costly one: OpenSEO bills JS rendering per page rendered (its estimate: $0.07-$0.39 per
      100 pages), and it HOLDS credits for the whole maxPages up front, about 3.84 credits per
      page, until the audit ends. (2026-10-15: a 1,000-page limit held 3,840 credits on a
      ~200-page site and left too little free for any other full audit that run.)
      maxPages = the newest completed PLAIN audit's `pagesCrawled` x 1.5, rounded up to a multiple
      of 10, at least 50 and at most 1000 (the JS-render cap). A plain crawl's 5,000 limit shows a
      site's real size; a full audit's own limit does not. If that plain crawl stopped at a round
      limit (`pagesCrawled` is exactly 50, 100, 200, 500, 1000 or 2000) the site may be bigger than
      it showed, and with no plain audit at all the size is unknown: in both cases run the due
      plain audit (b) first instead, so the next full audit can be sized. Only when no plain audit
      is due use 1000 (sites over 1,000 pages always get 1000).
   b. Plain audit, due when the newest completed audit of any kind finished **Plain audit every**
      days ago or more (or there is none): run_site_audit with renderJavaScript false,
      runLighthouse false, maxPages 5000.
      Exception: on a site whose last plain crawl found more than 1000 pages, a full audit cannot
      cover every page, so only plain audits reset its plain-audit clock.
   If both are due, run (a) this time (unless the sizing rule above sends it to (b)); (b) follows on a later run if still due. But if (a) is
   blocked by the credit check below, start (b) instead when it is due: a low balance must not
   leave the plain crawl overdue for good.
   url = the `startUrl` of the site's newest audit (some sites redirect to www), else "https://<domain>/".
   Never change these settings otherwise. Start at most **Max audits per run**, most overdue
   first; the rest wait for the next run. Call whoami right before EACH full-audit start (the
   balance drops as earlier audits render). If whoami fails or creditsRemaining is under
   **Credit floor**, start no more full audits this run (plain audits may still start) and add a
   "now" action saying credits are low or could not be checked. JS-render charges post while an
   audit runs, so the floor is a soft guard; **Max audits per run** is what bounds one run's spend.
   Do not wait for audits to finish; this run reads the previous audit, and the next run picks up
   the new one. Add an "fyi" action per started audit: id `<site>:audit-started:<auditId>`,
   title says plain or full, why = last audit date -> today, link = the run's `meta.url`.
   Call whoami again at the end and put credits used this run in an "fyi" action
   (`all:credits:audits`). If run_site_audit errors (for example, not enough credits), add it
   to `errors` and keep going.

5. Audit visits. For each connected site, the candidate audits are the completed or running audits
   from 1.4's list_site_audits call with `ranLighthouse: true` whose dates touch either window.
   An audit's dates run from its `startedAt` date to its `completedAt` date (or today while it
   runs), in the card's **Time zone**. Work each window on its own, because one audit can span the
   boundary:
   - audit days = every date in this window that any candidate audit touches, counted once even
     when two audits share a day.
   - Call traffic_acquisition channel_group once per audit day (startDate = endDate = that day).
     dayDirect = that day's Direct sessions.
   - baseline = (the window's Direct sessions - the sum of dayDirect over all audit days) /
     (7 - the number of audit days), rounded. With every day an audit day, baseline = 0.
   - auditVisits = the sum over audit days of max(0, dayDirect - baseline), capped at the window's
     Direct sessions and at 1.1 x the sum of `pagesCrawled` of the candidate audits in it.
   - adjusted Direct = Direct - auditVisits. Audit visits never engage, so engaged Direct stays as
     GA reports it.
   Use adjusted Direct, both weeks, for the bot test in Step 2 and the headline. Keep
   `channels[].sessions` and site `sessions` as GA reports them (the page must match GA), and say
   what was removed in a standout, for example "About 1,000 Direct visits on Oct 16 were
   OpenSEO's full audit, not people." Add one "fyi" action per site per window with auditVisits
   over 0 (`<site>:audit-traffic:<first audit day>`): title "OpenSEO's full audit added about N
   fake Direct visits", why = the audit days and dayDirect vs baseline, fix = "Nothing to do.
   Read Direct for that week with about N visits taken off." If a day call fails, add it to
   `errors` and use the raw numbers for that window.

How to read the tool output (learned on the first run, 2026-10-05):
- Site totals: no call returns one. Sum `comparison.rows[].metrics.sessions.{current,previous}`
  from the channel_group call (null = 0). Site engagement rate = sum(engagedSessions) / sum(sessions)
  from the same block. Site keyEvents [cur, prev] = sum of keyEvents across those channel rows.
- Channel `engagementRate` = current week only (null if the channel had no sessions this week).
- organic_overview `trend[]` skips zero days and uses YYYYMMDD dates: zero-fill to 28 values.
- page_performance rows carry `hostName`: put it in topPages[].host.
- search_opportunities has no query field and its window ends ~3 days back (Search Console lag);
  ignore the `source_time_zones_differ` warning. Rows with joinStatus "gsc_only" have score null.
  Set health.searchConsole=false only when the call errors for a missing connection; 0 rows means
  connected but nothing in positions 4-20.
- get_audit_issues `summary` is an array of {issueType, title, severity, count}: sum `count` for
  the total; empty array = 0. Audit date = `completedAt` from list_site_audits.
- measurement_health `keyEvents[].createTime` tells you when a key event was set up.
- Links: GA and opportunity tools return no URL. Use the project `url` from list_projects;
  for audit actions use `meta.url` from list_site_audits.
- Full audits fake Direct visits (found 2026-10-21): a renderJavaScript audit opens every page in
  a real browser, so the site's GA tag fires once per page rendered. Each one lands as a Direct,
  desktop, 0-engagement session, mostly from the United States. Wooden Nutmeg's 1,000-page full
  audit on 2026-10-16 showed up as 1,021 Direct sessions that day (about 28 on a normal day).
  Plain audits do not run JS and do not fire the tag. list_site_audits has no renderJavaScript
  field, and OpenSEO treats JS rendering and Lighthouse as separate options. This pipeline always
  sets both, so Step 1.5 uses `ranLighthouse: true` to find candidates. An audit started by hand
  with Lighthouse only fires no tag, so its days show no spike and Step 1.5 removes about 0.

## Step 2 - triage (the "Needs you" list)

Make one `actions` entry per thing the owner should do. Give each a stable `id` built as
`<site>:<kind>:<target>` (e.g. `shop:bot:direct`, `shop:seo-opportunity:/guides/sizing/`)
so the same problem keeps the same id on every run. build.py rewrites the site part from `site`,
so only kind and target must stay the same. That id is how the page shows NEW vs "open since".

priority "now" (something is broken or losing data):
- tracking: a connected site with 0 sessions this week, or measurement_health reports issues.
- traffic-drop: total sessions down 30%+ week over week, when the previous week had 50+.
- conversions: a key event that had 3+ last week and 0 this week. Skip events whose
  createTime falls inside either window (new setup, not a real change).
- site-health: the latest site audit has critical issues (name the top issue type). The audit can
  be older than the fix: if a merged PR since `completedAt` clearly addresses the issue, say so in
  `why` and set priority "fyi" (the next audit will confirm it).
priority "soon" (worth doing this week):
- seo-opportunity: at most 2 per site from search_opportunities: the top 2 by impressions among
  pages ranking 4-10 with CTR under 2% and 200+ impressions. Fix = open the page's queries in
  Search Console and rewrite the title and meta description to match the top query; name the path.
- bot: a channel whose sessions jumped 40%+ with engagement rate under 5% (Direct is the usual
  one), or that sits under 5% engagement with 100+ sessions in both weeks (ongoing bot share).
  For Direct, run both tests on adjusted Direct from Step 1.5, both weeks. Engagement rate for the
  test = engaged Direct sessions / adjusted Direct sessions. When adjusted Direct is 0 this week,
  skip the test (no bot traffic left). If neither test fires, there is no bot action, only the
  audit fyi. Call what is left "likely bot or spam traffic", never real growth,
  and leave it out of the headline growth story. Fix = these GA4 steps, in plain words: "In GA4
  open Reports > Acquisition > Traffic acquisition. Click Add filter, pick Session default
  channel group, exactly matches, Direct, Apply. Click the + next to the first column header and
  add Country, then try City, Device category and Hostname. Bots show up as one place, all
  desktop, near 0% engagement." Do not tell the owner to check source/medium (Direct is always
  "(direct) / (none)") or to add a GA4 data filter: GA4 data filters only drop internal traffic
  by listed IP address and developer traffic, so they cannot match bots by country or hostname.
  If the same source keeps coming back, the lasting fix is to block it at the host or CDN.
- tracking-hygiene: top pages include a host that is not the live site (localhost,
  deploy-preview-*, *.netlify.app, *.pages.dev). GA is counting dev or preview visits.
  Fix = only load the GA tag on the production hostname (gate the `gtag('config', …)` call on
  `location.hostname`).
- site-health: no completed site audit in twice **Plain audit every** days even though Step 1.4
  should have started one (it failed or keeps getting skipped). Fix = check the audit page for the error.
priority "fyi": anything else the owner should know but need not act on.

Every action needs: title (one short sentence), why (the number that triggered it, with
before -> after), fix (one concrete step, plain words), link (the OpenSEO project or tool URL
from the tool's `meta.url`). Do not invent problems to fill the list. An empty list is a good result.
Do not raise tagging for any site in the card's **Parked sites**. They may still appear in
`notConnected`, with "parked": true.

## Step 2b - file audit findings as GitHub issues

Turn what the audits found into GitHub issues the owner can work from, one issue per problem
type per site. Only sites in the card's **Repos** field get issues; skip the rest silently. A
site with no repo listed, or a repo you cannot reach, is not an error unless a `gh` call fails.

State. `audit-issues.json` in the **Data folder** records, per site, the audit last turned into
issues: `{"<site>": {"auditId": "...", "pending": true|false}}`. A missing file or site means
nothing was filed yet. Rewrite the file at the end of this step, even when the page is not
published later: it tracks what is in GitHub, not what is on the page.

When to file. Work from the site's newest completed audit (list_site_audits). Handle a site when
that audit's id differs from the recorded `auditId`, or when the record has `pending: true`
(the cap below held some findings back). Otherwise leave its issues alone.

1. Label. Once per repo per run: `gh label create "<Issue label>" -R <repo> --color 0E8A16
   --description "Found by the GA traffic report's site audits" --force` (creates or updates it).
2. SEO findings. get_audit_issues for that audit, once per severity in the card's
   **Issue severities**, limit 1000. Group by `issueType`. One issue per type:
   - title: `[site audit] <site>: <summary title>` (for example
     `[site audit] Pheidi: Thin content`).
   - body: what it is and why it matters in one or two plain sentences; the count and audit date;
     the first `howToFix`; up to 20 affected URLs as a checklist (`- [ ] <url>`), plus "and N more"
     with the audit link (`meta.url`); then two last lines, the marker
     `<!-- ga-traffic-report:<site-slug>:<issueType> -->` and the state
     `<!-- ga-traffic-report-state: audit=<auditId>; count=<N>; absent=none -->`.
     site-slug = the site name lowercased, runs of other characters turned into `-`.
   If any of these calls fails, add it to `errors` and file nothing for the site this run (its
   record stays as it was, so the next run tries again).
3. Performance. OpenSEO does not return Lighthouse scores, so read them from the PageSpeed
   Insights API, and only for sites whose newest completed audit has `ranLighthouse: true`. Test
   the site's home page plus its top 2 pages by views from Step 1.2 (production host only), mobile:
   `curl -s --max-time 120 -H "X-Goog-Api-Key: <key>" "https://www.googleapis.com/pagespeedonline/v5/runPagespeed?url=<url-encoded>&strategy=mobile&category=performance"`.
   The key is `api_key` in `~/.config/claude-seo/google-api.json`. Send it only in that header:
   never in the URL, a file, the report or an issue. Without the key the shared quota is usually
   used up (429). A page fails when performance score < 0.9, or LCP > 2.5 s, CLS > 0.1 or TBT >
   200 ms. If any page fails, one issue per site, title `[site audit] <site>: Mobile performance`:
   a table of page, score, LCP, CLS and TBT; then the audits on failing pages with score under 0.9
   and their `displayValue`, worst first, at most 10; then the marker
   `<!-- ga-traffic-report:<site-slug>:performance -->` and the state line, with count = the
   number of failing pages. If any page's call errors, add it to `errors` and treat performance
   as not checked this run: no new issue, no comment, no "not found".
4. Match before you write. List every labeled issue in the repo once, all pages:
   `gh api --paginate "repos/<repo>/issues?labels=<url-encoded label>&state=all&per_page=100"`,
   and drop entries that have a `pull_request` key. Match on the marker line, then on the exact
   title. Read the old numbers from the issue's state line, never from its text.
   - No match: open it with `gh issue create -R <repo> --label "<Issue label>"`, title and body
     from a temp file (`--body-file`). At most **Max new issues per run** across all sites, worst
     first (critical, then performance, then warning, then info; bigger counts first). Any site
     with findings left over gets `pending: true`, and the next run files them.
   - Open match: when the state line's count differs, or the audit is new and lists new URLs,
     comment "Audit <date>: N pages (was M)" plus the new URLs. Then rewrite the state line
     (audit, count, absent=none) with `gh issue edit -R <repo> <n> --body-file`, keeping the rest
     of the body. No comment if nothing changed.
   - Closed match that came back: reopen it (`gh issue reopen`) with a comment saying which audit
     found it again, and rewrite its state line the same way.
   - An open issue for this site whose type this audit checked and no longer finds: if its state
     line says `absent=none`, comment "Not found in the <date> audit. Close this if the fix is
     live." and set `absent=<auditId>`. If `absent` is already set, do nothing. Never close it.
     Only check types the run actually read: the card's **Issue severities**, and performance
     only when step 3 succeeded.
5. Record it. Set the site's `audit-issues.json` entry to this audit's id, with `pending` as step
   4 left it.
6. Report it. One "fyi" action per site where anything was written,
   id `<site>:github-issues:audit`, title "Opened N and updated M GitHub issues from the <date>
   audit", why = the issue titles (and "K more wait for the next run" when pending), fix = "Work
   through them in GitHub.", link = `https://github.com/<repo>/issues?q=label%3A<Issue label>`.
   Any failed `gh` call goes in `errors` with its exact message; keep going.

## Step 3 - write report.json

Write `report.json` in the **Data folder**, UTF-8, exactly this shape ([current, previous] pairs):

{
  "generatedAt": "<ISO timestamp with offset>",
  "window": {"current": ["YYYY-MM-DD","YYYY-MM-DD"], "previous": ["YYYY-MM-DD","YYYY-MM-DD"]},
  "headline": "1-2 plain sentences: the real story across all sites this week (bots excluded)",
  "doThisNext": "the single most useful action across all sites, or null",
  "actions": [{"id","priority","site","kind","title","why","fix","link"}],
  "sites": [{
    "name", "domain", "projectUrl", "status": "ok" | "error", "error": null,
    "sessions": [cur, prev], "engagementRate": [cur, prev], "keyEvents": [cur, prev],
    "organicDaily": [28 daily organic session counts, oldest first],
    "channels": [{"name", "sessions": [cur, prev], "engagementRate", "bot": true|false}],
    "sources": [{"name": "source / medium", "sessions"}],
    "topPages": [{"host", "path", "views", "users"}],
    "events": [{"name", "count": [cur, prev]}],
    "opportunities": [{"page": "/path", "position", "impressions", "clicks", "ctr", "score", "note"}],
    "health": {"gaIssues": ["..."] | null, "audit": {"date","critical","warning"} | null, "searchConsole": true|false|null},
    "standouts": ["1-3 plain bullets: biggest real changes and a best guess why"]
  }],
  "notConnected": [{"name", "domain", "actionUrl", "parked": true|false}],
  "errors": [{"site", "call", "message"}]
}

`health` null means "not checked": set gaIssues null when measurement_health failed, and
searchConsole null when search_opportunities failed for any reason other than a missing
connection. `[]` means checked with no problems; false means not connected.

Rates are 0-1 fractions. Order sites by current sessions, highest first. Plain English in every
string: short sentences, define jargon in a few words. `note` on an opportunity says why it is
worth it in under 12 words.

## Step 4 - build and publish

1. Run: python "<skill folder>/build.py" "<Data folder>"
   Non-zero exit = stop, report the exact error, do not publish. The build writes
   `history.pending.json`, not `history.json`, so a run that never publishes leaves the
   NEW / "open since" memory untouched.
2. Artifact tool, action "read", url = the card's **Artifact** (a publish to an unread artifact
   is refused).
3. Artifact tool publish with that url and file_path `<Data folder>/report.html`.
   Never publish without the url (that makes a duplicate). Do not pass icon or title.
4. Only after the publish succeeds, run: python "<skill folder>/build.py" "<Data folder>" --commit
   It moves `history.pending.json` over `history.json`. If the publish failed, skip this step.

Do not edit template.html or build.py unless the build is actually broken; if you do, say what
and why, and edit them in a nightforge worktree with a PR, never the installed copy.

## Step 5 - reply in 5 lines or fewer

- Total sessions across sites, change vs last week.
- Action counts (fix now / this week), naming every "fix now" item.
- GitHub issues opened and updated, per repo (skip the line if none).
- Anything that failed, named.
- The artifact link.
