# Report card template

<!-- docKey: ga-traffic-report-card -->

A **report card** is the only input the `ga-traffic-report` skill needs. The pipeline — which
OpenSEO calls to make, how to read them, the triage rules, the audit cadence logic, the report
shape and the page — lives in the shared skill. The card says where the report lives and sets
the knobs.

Put the card in the scheduled task's `SKILL.md`, and schedule the task as often as you want the
report (three times a week works well; the audit cadence is counted in days, so it does not
depend on the schedule).

**Keep the card out of a public repo.** The artifact link and the local paths are not credentials,
but they are not worth publishing.

Before the first run: connect the OpenSEO MCP server, and publish any page once with the Artifact
tool to get an artifact URL for the card. Each OpenSEO project needs GA4 connected in OpenSEO's
settings to show traffic; projects without it are listed under "Not connected".

---

```markdown
---
name: ga-traffic-report
description: Pulls GA traffic for every OpenSEO site, triages what needs fixing, and republishes the GA Traffic Report artifact.
---

**Run the `ga-traffic-report` skill against the report card below.** The pipeline lives at
`<path to>/skills/ga-traffic-report/SKILL.md` — read it first; it is the whole procedure. If that
file does not exist, stop and report exactly that — do not improvise a report.

## Report card

| Field | Value |
|---|---|
| **Data folder** | `<abs path>` — holds report.json, history.json, report.html |
| **Artifact** | `https://claude.ai/artifact/<id>` |
| **Time zone** | `<IANA zone of the GA4 properties, e.g. America/New_York>` |
| **Parked sites** | `<site>`, `<site>` — untagged on purpose; never raise tagging for them |
| **Plain audit every** | **<n>** days (crawl only, up to 5,000 pages) |
| **Full audit every** | **<n>** days (JS rendering + Lighthouse, page limit sized to the site, up to 1,000; costs credits) |
| **Max audits per run** | **<n>** |
| **Credit floor** | **<n>** — below this, no full audits start |
| **Repos** | `<site name>` → `<owner/repo>`, one per line — sites that get GitHub issues from audit findings |
| **Issue label** | `site-audit` |
| **Issue severities** | `critical`, `warning` (add `info` for everything) |
| **Max new issues per run** | **<n>** |
```

## Fields

- **Data folder** — `history.json` lives here, and it is how the page marks an action NEW or
  "open since". The build only stages `history.pending.json`; the run moves it over
  `history.json` (`build.py --commit`) after the publish succeeds, so a run that never publishes
  leaves that memory untouched. Moving the folder resets it.
- **Artifact** — the run reads it, then republishes `report.html` to it. A run with no URL would
  create a second artifact, so the field is required.
- **Plain / Full audit every** — a full audit also counts as a plain one, except on sites over
  1,000 pages, which a full audit cannot cover. At most one audit starts per site per run. A full
  audit's page limit is about 1.5x the site's last plain crawl, because OpenSEO holds credits for
  the whole limit until the audit ends.
- **Credit floor** — checked with `whoami` before any full audit starts. Each run reports the
  credits it used.
- **Repos** — Step 2b files or updates one GitHub issue per problem type per site, from the
  newest audit, in the repo listed for that site. Later audits comment on changed counts, reopen
  issues that come back and note ones that are gone; the owner closes them. `audit-issues.json`
  in the Data folder remembers which audit each site's issues come from. Leave a site out to get no issues for it. Mobile performance
  issues need a Google API key at `~/.config/claude-seo/google-api.json` (`api_key`), because
  OpenSEO does not return Lighthouse scores.
- **Issue label** — the label on every issue the run files, and how it lists them again. After a
  change, old issues are no longer found, so the next new audit on each site opens fresh issues
  under the new label.
- **Max new issues per run** — caps new issues only. Findings over the cap are filed on the next
  run. Comments and reopens on existing issues are not capped.
