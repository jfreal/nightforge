// job-reports: one endpoint every job reports to, wherever it runs, and the Routines page that
// shows them.
//
// For jobs and scripts, with `Authorization: Bearer <token>`:
//   POST   /report      a job says what one run did             REPORT_TOKEN or ADMIN_TOKEN
//   PUT    /jobs/:id    register a job, or change it            ADMIN_TOKEN
//   DELETE /jobs/:id    forget a job and its runs               ADMIN_TOKEN
//   GET    /jobs        every job with its last run             ADMIN_TOKEN
//   GET    /sync?after= jobs, plus runs received after a cursor ADMIN_TOKEN (the local dashboard)
//   GET    /health      health check, no token
//
// For you, in a browser, after signing in at /login (session.js):
//   GET    /            the Routines page (public/, built from tools/pr-dashboard/static)
//   GET    /api/routines   every job, worst first, with health      session or ADMIN_TOKEN
//   GET    /api/runs?bot=  one job's recent runs                    session or ADMIN_TOKEN
//
// The cron trigger (scheduled, below) sends alerts when a job's state changes (alerts.js).
// REPORT_TOKEN can only add runs, so it is the one to hand to GitHub Actions secrets and job
// prompts. Storage is one D1 database (migrations/).

import { changes, send } from "./alerts.js";
import { buildRows, STRIP } from "./health.js";
import { clearCookie, loginPage, makeSession, sessionFrom, setCookie, validSession } from "./session.js";

const JOB_ID = /^[a-z0-9][a-z0-9._-]{0,79}$/;
export const STATUSES = ["ok", "found", "needs-you", "failed"];
const MAX_BODY = 64 * 1024, MAX_SUMMARY = 300, MAX_DETAILS = 20000, MAX_LINKS = 20, SYNC_PAGE = 500;

export class BadRequest extends Error {}

/** The job id in a /jobs/<id> path. A malformed %-escape is the caller's mistake: 400, not 500. */
export function pathJobId(raw) {
  let decoded;
  try { decoded = decodeURIComponent(raw); } catch (err) {
    if (err instanceof URIError) throw new BadRequest(`job: ${JSON.stringify(raw)} is not a valid path`);
    throw err;
  }
  return jobId(decoded);
}

// ---- checks, shared with test/worker.test.mjs ----------------------------------------------

export function iso(date) {
  return date.toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function parseTime(value) {
  if (typeof value === "number" || /^\d{9,11}(\.\d+)?$/.test(String(value))) return new Date(Number(value) * 1000);
  const text = String(value).trim();
  // A time with no zone means UTC, as it does in the dashboard.
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(text) ? text : text + "Z");
  if (Number.isNaN(d.getTime())) throw new BadRequest(`not a time: ${JSON.stringify(value)} (use ISO 8601, like 2026-10-28T13:05:00Z)`);
  return d;
}

/** '15m', '2h', '1d', '1d12h', '1w' or a number of minutes. '' or null clears it. */
export function parseMinutes(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") return value > 0 ? Math.round(value) : null;
  const text = String(value).toLowerCase().replace(/\s+/g, "");
  if (/^\d+$/.test(text)) return Number(text) || null;
  const m = text.match(/^(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$/);
  if (!m || !m.slice(1).some(Boolean)) throw new BadRequest(`not a duration: ${JSON.stringify(value)} (use 15m, 2h, 1d or 1w)`);
  const [w, d, h, min] = m.slice(1).map((g) => Number(g || 0));
  return (((w * 7 + d) * 24 + h) * 60 + min) || null;
}

export function jobId(value) {
  const id = String(value ?? "").trim();
  if (!JOB_ID.test(id)) throw new BadRequest(`job: ${JSON.stringify(id)} is not an id (lowercase letters, digits, '-', '.', '_')`);
  return id;
}

const text = (v, max) => String(v ?? "").slice(0, max);

/** One run report, checked and brought to the stored shape. Jobs write these, so nothing in one
 *  is trusted: links must be http(s), counts whole numbers, and text is cut to size. */
export function cleanReport(r, now = new Date()) {
  if (!r || typeof r !== "object" || Array.isArray(r)) throw new BadRequest("a report is a JSON object");
  const job = jobId(r.job ?? r.bot);
  const status = String(r.status ?? "").trim().toLowerCase();
  if (!STATUSES.includes(status)) throw new BadRequest(`status: ${JSON.stringify(status)} is not one of ${STATUSES.join(", ")}`);
  const finished = r.finished ? parseTime(r.finished) : now;
  const started = r.started ? parseTime(r.started) : null;
  const counts = {};
  for (const [k, v] of Object.entries(r.counts ?? {})) {
    if (typeof v !== "number" || !Number.isInteger(v)) throw new BadRequest(`counts.${k}: ${JSON.stringify(v)} is not a whole number`);
    counts[k.slice(0, 40)] = v;
  }
  const links = [];
  for (let link of (Array.isArray(r.links) ? r.links : []).slice(0, MAX_LINKS)) {
    if (typeof link === "string") link = { label: link, url: link };
    const url = String(link?.url ?? "");
    // The dashboard puts these in href, so only plain web links get through.
    if (!/^https?:\/\//i.test(url)) throw new BadRequest(`links: ${JSON.stringify(url)} is not an http(s) link`);
    links.push({ label: text(link.label || url, 120), url: url.slice(0, 2000) });
  }
  let summary = String(r.summary ?? "").split(/\s+/).filter(Boolean).join(" ");
  if (summary.length > MAX_SUMMARY) summary = summary.slice(0, MAX_SUMMARY - 1) + "…";
  return {
    job, status, summary, counts, links,
    name: text(r.name, 120).trim(),
    run: text(r.run || crypto.randomUUID(), 200),
    details: text(r.details, MAX_DETAILS),
    source: text(r.source, 40),
    started: started ? iso(started) : null,
    finished: iso(finished),
  };
}

const JOB_FIELDS = { name: 120, does: 300, runsOn: 60, schedule: 120, url: 2000 };

/** A job registration: only the fields given are changed. */
export function cleanJob(body) {
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new BadRequest("a job is a JSON object");
  const out = {};
  for (const [k, max] of Object.entries(JOB_FIELDS)) if (k in body) out[k] = text(body[k], max).trim();
  if (out.url && !/^https?:\/\//i.test(out.url)) throw new BadRequest(`url: ${JSON.stringify(out.url)} is not an http(s) link`);
  if ("every" in body) out.every = parseMinutes(body.every);
  if ("grace" in body) out.grace = parseMinutes(body.grace);
  if ("paused" in body) {
    // A string "false" is truthy; from a shell curl that would silence a job's alarms.
    if (typeof body.paused !== "boolean") throw new BadRequest(`paused: ${JSON.stringify(body.paused)} is not true or false`);
    out.paused = body.paused ? 1 : 0;
  }
  return out;
}

// ---- auth --------------------------------------------------------------------------------

async function same(a, b) {
  if (!a || !b) return false;
  const enc = new TextEncoder();
  // Hash first so the comparison is fixed-length and constant-time whatever was sent.
  const [x, y] = await Promise.all([a, b].map((s) => crypto.subtle.digest("SHA-256", enc.encode(s))));
  return crypto.subtle.timingSafeEqual(x, y);
}

async function role(request, env) {
  const m = (request.headers.get("Authorization") || "").match(/^Bearer\s+(.+)$/i);
  if (!m) return null;
  if (await same(m[1].trim(), env.ADMIN_TOKEN)) return "admin";
  if (await same(m[1].trim(), env.REPORT_TOKEN)) return "report";
  return null;
}

// ---- handlers ----------------------------------------------------------------------------

const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
});

async function body(request) {
  const raw = await request.text();
  if (raw.length > MAX_BODY) throw new BadRequest(`body over ${MAX_BODY} bytes`);
  try { return JSON.parse(raw || "{}"); } catch { throw new BadRequest("body is not JSON"); }
}

async function report(request, env) {
  const r = cleanReport(await body(request));
  const now = iso(new Date());
  // A job nobody registered still shows up, marked so the dashboard can say "not registered".
  await env.DB.prepare("INSERT OR IGNORE INTO jobs (id, name, registered, created_at, updated_at) VALUES (?, ?, 0, ?, ?)")
    .bind(r.job, r.name || r.job, now, now).run();
  const res = await env.DB.prepare(
    "INSERT OR IGNORE INTO runs (job_id, run_id, received_at, started_at, finished_at, status, summary, counts, links, details, source)"
    + " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
    .bind(r.job, r.run, now, r.started, r.finished, r.status, r.summary, JSON.stringify(r.counts),
          JSON.stringify(r.links), r.details, r.source).run();
  // The same run sent twice (a retried step) is stored once, and counted once.
  if (res.meta.changes) await env.DB.prepare("UPDATE jobs SET runs_total = runs_total + 1 WHERE id = ?").bind(r.job).run();
  return json({ ok: true, job: r.job, run: r.run, duplicate: res.meta.changes === 0 });
}

const COLUMNS = { name: "name", does: "does", runsOn: "runs_on", schedule: "schedule", url: "url",
                  every: "every_minutes", grace: "grace_minutes", paused: "paused" };

async function putJob(id, request, env) {
  const fields = cleanJob(await body(request));
  const now = iso(new Date());
  await env.DB.prepare("INSERT OR IGNORE INTO jobs (id, name, registered, created_at, updated_at) VALUES (?, ?, 1, ?, ?)")
    .bind(id, fields.name || id, now, now).run();
  const sets = Object.keys(fields).map((k) => `${COLUMNS[k]} = ?`);
  await env.DB.prepare(`UPDATE jobs SET ${[...sets, "registered = 1", "updated_at = ?"].join(", ")} WHERE id = ?`)
    .bind(...Object.values(fields), now, id).run();
  return json({ ok: true, job: shapeJob(await env.DB.prepare("SELECT * FROM jobs WHERE id = ?").bind(id).first()) });
}

function shapeJob(j) {
  return j && { id: j.id, name: j.name, every: j.every_minutes, grace: j.grace_minutes, does: j.does,
                runsOn: j.runs_on, schedule: j.schedule, url: j.url, paused: !!j.paused,
                registered: !!j.registered, createdAt: j.created_at, updatedAt: j.updated_at };
}

function shapeRun(r) {
  return { seq: r.seq, job: r.job_id, run: r.run_id, receivedAt: r.received_at, started: r.started_at,
           finished: r.finished_at, status: r.status, summary: r.summary, counts: JSON.parse(r.counts),
           links: JSON.parse(r.links), details: r.details, source: r.source };
}

async function listJobs(env) {
  const { results } = await env.DB.prepare(
    "SELECT j.*, r.status AS last_status, r.finished_at AS last_finished, r.summary AS last_summary"
    + " FROM jobs j LEFT JOIN runs r ON r.seq = (SELECT seq FROM runs WHERE job_id = j.id ORDER BY finished_at DESC LIMIT 1)"
    + " ORDER BY j.id").all();
  return json({ ok: true, jobs: results.map((j) => ({ ...shapeJob(j),
    last: j.last_finished ? { status: j.last_status, finished: j.last_finished, summary: j.last_summary } : null })) });
}

async function sync(url, env) {
  const after = Math.max(0, parseInt(url.searchParams.get("after") || "0", 10) || 0);
  const [{ results: jobs }, { results: runs }] = await Promise.all([
    env.DB.prepare("SELECT * FROM jobs ORDER BY id").all(),
    env.DB.prepare("SELECT * FROM runs WHERE seq > ? ORDER BY seq LIMIT ?").bind(after, SYNC_PAGE + 1).all(),
  ]);
  const page = runs.slice(0, SYNC_PAGE);
  return json({ ok: true, jobs: jobs.map(shapeJob), runs: page.map(shapeRun),
                next: page.length ? page[page.length - 1].seq : after, more: runs.length > SYNC_PAGE });
}

// ---- the Routines page --------------------------------------------------------------------

const HISTORY = 50;
const ASSETS = new Set(["/style.css", "/routines.css", "/routines.js", "/logo.png"]);

/** Every job with its health, worst first: the same rows the local dashboard serves. */
export async function board(env, now = Date.now()) {
  const { results: jobs } = await env.DB.prepare("SELECT * FROM jobs").all();
  const weekAgo = iso(new Date(now - 7 * 86400000));
  const statements = [env.DB.prepare("SELECT job_id, counts FROM runs WHERE finished_at >= ?").bind(weekAgo),
    ...jobs.map((j) => env.DB.prepare("SELECT run_id, source, started_at, finished_at, status, summary, counts, links"
      + " FROM runs WHERE job_id = ? ORDER BY finished_at DESC LIMIT ?").bind(j.id, Math.max(STRIP, 10)))];
  const [week, ...recent] = statements.length > 1 ? await env.DB.batch(statements) : [await statements[0].all()];
  const weekByJob = new Map(), recentByJob = new Map();
  for (const r of week.results) {
    if (!weekByJob.has(r.job_id)) weekByJob.set(r.job_id, []);
    weekByJob.get(r.job_id).push(r);
  }
  jobs.forEach((j, i) => recentByJob.set(j.id, recent[i].results));
  return buildRows(jobs, recentByJob, weekByJob, now);
}

async function runsOf(url, env) {
  const id = jobId(url.searchParams.get("bot") || url.searchParams.get("job"));
  const { results } = await env.DB.prepare("SELECT * FROM runs WHERE job_id = ? ORDER BY finished_at DESC LIMIT ?")
    .bind(id, HISTORY).all();
  // The page's run list wants the same fields as the local dashboard's, so drop the sync ones.
  return json({ ok: true, runs: results.map((r) => {
    const { seq, job, receivedAt, ...run } = shapeRun(r);
    return run;
  }) });
}

/** HTML and its assets can't be framed, so no other site can steer clicks on this one. */
function guard(response) {
  const r = new Response(response.body, response);
  r.headers.set("X-Frame-Options", "DENY");
  r.headers.set("Content-Security-Policy", "frame-ancestors 'none'");
  r.headers.set("Referrer-Policy", "same-origin");
  return r;
}

const html = (body, status = 200, headers = {}) => guard(new Response(body, {
  status, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", ...headers } }));

async function signedIn(request, env) {
  return (await validSession(env.ADMIN_TOKEN, sessionFrom(request))) || (await role(request, env)) === "admin";
}

/** Browser routes. Returns null when the path isn't one of them. */
async function page(request, env, url, path) {
  const secure = url.protocol === "https:";
  if (ASSETS.has(path) && request.method === "GET") return guard(await env.ASSETS.fetch(request));
  if (path === "/login" && request.method === "GET") return html(loginPage());
  if (path === "/login" && request.method === "POST") {
    const form = await request.formData().catch(() => null);
    const token = String(form?.get("token") || "").trim();
    if (!(await same(token, env.ADMIN_TOKEN))) return html(loginPage("That isn't this site's admin token."), 401);
    return new Response(null, { status: 303, headers: { Location: "/", "Set-Cookie": setCookie(await makeSession(env.ADMIN_TOKEN), secure) } });
  }
  if (path === "/logout" && request.method === "POST") {
    return new Response(null, { status: 303, headers: { Location: "/login", "Set-Cookie": clearCookie(secure) } });
  }
  if ((path === "/" || path === "/routines") && request.method === "GET") {
    if (!(await signedIn(request, env))) return Response.redirect(new URL("/login", url).toString(), 303);
    return guard(await env.ASSETS.fetch(new Request(new URL("/", url), request)));
  }
  if (path.startsWith("/api/") && request.method === "GET") {
    if (!(await signedIn(request, env))) return json({ ok: false, error: "sign in at /login" }, 401);
    if (path === "/api/routines") {
      return json({ mode: "cloud", rows: await board(env), error: "", collectors: [], bootId: "cloud",
                    collectedAt: iso(new Date()), db: "", prDashboardUrl: env.PR_DASHBOARD_URL || "" });
    }
    if (path === "/api/runs") return await runsOf(url, env);
  }
  return null;
}

// ---- alerts, from the cron trigger ---------------------------------------------------------

export async function checkAlerts(env, now = Date.now()) {
  const rows = await board(env, now);
  const { results } = await env.DB.prepare("SELECT job_id, state FROM alerts").all();
  const known = new Map(results.map((r) => [r.job_id, r.state]));
  const { messages, states } = changes(rows, known, now);
  if (!messages.length) return messages;
  // Not delivered (Slack or ntfy down): remember nothing, so the next cron run tries again.
  if (!(await send(env, messages, env.DASHBOARD_URL || ""))) return [];
  const at = iso(new Date(now));
  await env.DB.batch(messages.map((m) => env.DB.prepare(
    "INSERT INTO alerts (job_id, state, changed_at) VALUES (?, ?, ?)"
    + " ON CONFLICT(job_id) DO UPDATE SET state = excluded.state, changed_at = excluded.changed_at")
    .bind(m.job, states.get(m.job), at)));
  return messages;
}

/** Once a day: drop runs older than RETENTION_DAYS (365 by default), so the database stays small.
 *  runs_total keeps counting them. */
async function prune(env, now) {
  const days = Math.max(30, Number(env.RETENTION_DAYS) || 365);
  await env.DB.prepare("DELETE FROM runs WHERE finished_at < ?").bind(iso(new Date(now - days * 86400000))).run();
}

export default {
  async scheduled(event, env, ctx) {
    const now = event.scheduledTime || Date.now();
    ctx.waitUntil((async () => {
      await checkAlerts(env, now);
      const d = new Date(now);
      if (d.getUTCHours() === 3 && d.getUTCMinutes() < 5) await prune(env, now);
    })());
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";
    try {
      if (path === "/health" && request.method === "GET") return json({ ok: true, service: "job-reports" });
      if (!env.ADMIN_TOKEN || !env.REPORT_TOKEN) return json({ ok: false, error: "server has no tokens set" }, 500);
      const browser = await page(request, env, url, path);
      if (browser) return browser;
      const who = await role(request, env);
      if (!who) return json({ ok: false, error: "missing or wrong token" }, 401);
      if (path === "/report" && request.method === "POST") return await report(request, env);
      if (who !== "admin") return json({ ok: false, error: "this token can only POST /report" }, 403);
      const jobPath = path.match(/^\/jobs\/([^/]+)$/);
      if (jobPath && request.method === "PUT") return await putJob(pathJobId(jobPath[1]), request, env);
      if (jobPath && request.method === "DELETE") {
        const id = pathJobId(jobPath[1]);
        const [, gone] = await env.DB.batch([env.DB.prepare("DELETE FROM runs WHERE job_id = ?").bind(id),
                                             env.DB.prepare("DELETE FROM jobs WHERE id = ?").bind(id),
                                             env.DB.prepare("DELETE FROM alerts WHERE job_id = ?").bind(id)]);
        return json({ ok: true, removed: gone.meta.changes > 0 });
      }
      if (path === "/jobs" && request.method === "GET") return await listJobs(env);
      if (path === "/sync" && request.method === "GET") return await sync(url, env);
      return json({ ok: false, error: "not found" }, 404);
    } catch (err) {
      if (err instanceof BadRequest) return json({ ok: false, error: err.message }, 400);
      console.error(err);
      return json({ ok: false, error: "server error" }, 500);
    }
  },
};
