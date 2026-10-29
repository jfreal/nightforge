// Whether each job is keeping its schedule. A port of tools/pr-dashboard/routines.py (guess_every,
// default_grace, health, sort_key, Store.board), so the hosted page and the local one agree.
// test/health.test.mjs runs the same cases as test_routines.py.

import { iso } from "./worker.js";

export const STRIP = 14;        // runs drawn as squares in the Track record column
const GUESS_FROM = 10;          // recent runs used to guess a cadence nobody set
const LANE_ORDER = { you: 0, broken: 1, healthy: 2, paused: 3 };
const MIN = 60000;

/** The median gap between recent runs, in minutes. Needs three runs: two points are one gap. */
export function guessEvery(finishedTimes) {
  const times = finishedTimes.slice(0, GUESS_FROM).map((t) => Date.parse(t)).sort((a, b) => a - b);
  const gaps = [];
  for (let i = 1; i < times.length; i++) {
    const g = (times[i] - times[i - 1]) / MIN;
    if (g >= 1) gaps.push(g);
  }
  if (gaps.length < 2) return null;
  gaps.sort((a, b) => a - b);
  const mid = gaps.length >> 1;
  return Math.round(gaps.length % 2 ? gaps[mid] : (gaps[mid - 1] + gaps[mid]) / 2);
}

/** Half the cadence, kept between a quarter of an hour and four hours. */
export const defaultGrace = (every) => Math.max(15, Math.min(240, Math.floor(every / 2)));

/** `timing` from the clock, `result` from the last report, `lane` for the page's filters. */
export function health(job, last, now, guessed = null) {
  const every = job.every_minutes || guessed;
  const grace = job.grace_minutes || (every ? defaultGrace(every) : null);
  const result = last ? last.status : null;
  let timing, dueAt = null, lateAt = null;
  if (job.paused) timing = "paused";
  else if (!every) timing = last ? "unscheduled" : "waiting";
  else {
    const since = Date.parse(last ? last.finished : job.created_at);
    dueAt = since + every * MIN;
    lateAt = since + (every + grace) * MIN;
    if (now > lateAt) timing = last ? "late" : "never";
    else if (now > dueAt) timing = "due";
    else timing = last ? "ontime" : "waiting";
  }
  const lane = timing === "paused" ? "paused"
    : result === "needs-you" ? "you"
    : result === "failed" || timing === "late" || timing === "never" ? "broken"
    : "healthy";
  const overdue = lateAt ? Math.max(0, Math.floor((now - lateAt) / MIN)) : 0;
  return { timing, result, lane, every, everyGuessed: Boolean(every && !job.every_minutes), grace,
           dueAt: dueAt ? iso(new Date(dueAt)) : null, lateAt: lateAt ? iso(new Date(lateAt)) : null,
           overdueMinutes: overdue };
}

export function sortRows(rows) {
  return rows.sort((a, b) => LANE_ORDER[a.health.lane] - LANE_ORDER[b.health.lane]
    || b.health.overdueMinutes - a.health.overdueMinutes
    || a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
}

const runOf = (r) => ({ run: r.run_id, source: r.source, started: r.started_at, finished: r.finished_at,
                        status: r.status, summary: r.summary, counts: JSON.parse(r.counts), links: JSON.parse(r.links) });

/** One page row per job, worst first, in the shape the local dashboard's /api/routines returns.
 *  `recent` holds each job's newest runs (newest first); `week` its runs of the last 7 days. */
export function buildRows(jobs, recentByJob, weekByJob, now) {
  return sortRows(jobs.map((job) => {
    const recent = recentByJob.get(job.id) || [];
    const week = weekByJob.get(job.id) || [];
    const last = recent.length ? runOf(recent[0]) : null;
    const guessed = job.every_minutes ? null : guessEvery(recent.map((r) => r.finished_at));
    const counts7d = {};
    for (const r of week) for (const [k, v] of Object.entries(JSON.parse(r.counts))) counts7d[k] = (counts7d[k] || 0) + v;
    return {
      id: job.id, name: job.name, kind: job.runs_on || (job.registered ? "" : "not registered"),
      schedule: job.schedule, description: job.does, url: job.url, paused: Boolean(job.paused),
      nextAt: null, createdAt: job.created_at, health: health(job, last, now, guessed), last,
      strip: recent.slice(0, STRIP).reverse().map((r) => ({ status: r.status, finished: r.finished_at, summary: r.summary })),
      runsTotal: job.runs_total || 0, runs7d: week.length, counts7d,
    };
  }));
}
