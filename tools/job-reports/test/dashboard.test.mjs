// The hosted page's rules: health (same cases as tools/pr-dashboard/test_routines.py), alert
// changes, and sign-in sessions.   node --test test/*.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { changes, problemOf } from "../src/alerts.js";
import { buildRows, defaultGrace, guessEvery, health } from "../src/health.js";
import { makeSession, sessionFrom, validSession } from "../src/session.js";
import { iso } from "../src/worker.js";

const NOW = Date.parse("2026-10-28T12:00:00Z");
const ago = (mins) => iso(new Date(NOW - mins * 60000));
const job = (over = {}) => ({ every_minutes: 60, grace_minutes: 30, paused: 0, created_at: "2026-10-01T00:00:00Z", ...over });
const last = (mins, status = "ok") => ({ finished: ago(mins), status });

test("timing by age", () => {
  for (const [mins, timing] of [[30, "ontime"], [75, "due"], [95, "late"]]) assert.equal(health(job(), last(mins), NOW).timing, timing);
});

test("late is broken and counts overdue minutes", () => {
  const h = health(job(), last(100), NOW);
  assert.deepEqual([h.lane, h.overdueMinutes], ["broken", 10]);
});

test("lanes: needs-you, failed, never, waiting, paused, unscheduled", () => {
  assert.equal(health(job(), last(5, "needs-you"), NOW).lane, "you");
  assert.equal(health(job(), last(5, "failed"), NOW).lane, "broken");
  assert.equal(health(job(), null, NOW).timing, "never");
  const fresh = health(job({ created_at: ago(10) }), null, NOW);
  assert.deepEqual([fresh.timing, fresh.lane], ["waiting", "healthy"]);
  assert.equal(health(job({ paused: 1 }), last(10000, "failed"), NOW).lane, "paused");
  const loose = health(job({ every_minutes: null }), last(10000), NOW);
  assert.deepEqual([loose.timing, loose.lane], ["unscheduled", "healthy"]);
});

test("a guessed cadence is used and flagged", () => {
  const h = health(job({ every_minutes: null, grace_minutes: null }), last(200), NOW, 60);
  assert.deepEqual([h.timing, h.every, h.everyGuessed, h.grace], ["late", 60, true, 30]);
});

test("default grace and the guess from history", () => {
  assert.deepEqual([defaultGrace(10), defaultGrace(120), defaultGrace(10080)], [15, 60, 240]);
  assert.equal(guessEvery([0, 60, 120, 180, 600].map(ago)), 60);
  assert.equal(guessEvery([0, 60].map(ago)), null);
});

const run = (mins, status, counts = {}) => ({ run_id: String(mins), source: "", started_at: null, finished_at: ago(mins),
                                              status, summary: `s${mins}`, counts: JSON.stringify(counts), links: "[]" });

test("rows carry the last run, the strip oldest first, counts, and sort worst first", () => {
  const jobs = [{ id: "fine", name: "Fine", every_minutes: 60, created_at: ago(5000), runs_total: 2, registered: 1 },
                { id: "asks", name: "Asks", every_minutes: 60, created_at: ago(5000), runs_total: 1, registered: 1 },
                { id: "stray", name: "stray", created_at: ago(5000), runs_total: 0, registered: 0 }];
  const recent = new Map([["fine", [run(5, "found", { prs: 2 }), run(65, "ok")]], ["asks", [run(5, "needs-you")]]]);
  const week = new Map([["fine", [run(5, "found", { prs: 2 }), run(65, "ok", { prs: 1 })]]]);
  const rows = buildRows(jobs, recent, week, NOW);
  assert.deepEqual(rows.map((r) => r.id), ["asks", "fine", "stray"]);
  const fine = rows[1];
  assert.deepEqual([fine.last.status, fine.strip.map((s) => s.status), fine.counts7d, fine.runs7d, fine.runsTotal],
                   ["found", ["ok", "found"], { prs: 3 }, 2, 2]);
  assert.equal(rows[2].kind, "not registered");
});

const row = (id, lane, timing, result, extra = {}) => ({ id, name: id.toUpperCase(),
  health: { lane, timing, result, every: 60, grace: 30, overdueMinutes: 0 }, last: { finished: ago(200), summary: "boom" }, ...extra });

test("alerts: one message per change, and one when it recovers", () => {
  const broken = [row("a", "broken", "late", "ok"), row("b", "broken", "ontime", "failed"), row("c", "healthy", "ontime", "ok")];
  const first = changes(broken, new Map(), NOW);
  assert.deepEqual(first.messages.map((m) => [m.job, m.state]), [["a", "late"], ["b", "failed"]]);
  assert.match(first.messages[0].text, /A hasn't reported for 3h 20m \(expected every 1h\)/);
  assert.match(first.messages[1].text, /B failed: boom/);
  const again = changes(broken, first.states, NOW);
  assert.equal(again.messages.length, 0, "no repeat while nothing changes");
  const fixed = changes([row("a", "healthy", "ontime", "ok"), broken[1], broken[2]], first.states, NOW);
  assert.deepEqual(fixed.messages.map((m) => m.text), ["✓ A is healthy again"]);
});

test("a paused job never alerts", () => {
  assert.equal(problemOf(row("p", "paused", "paused", "failed")), "ok");
});

test("sessions: valid, expired, tampered, other secret", async () => {
  const s = await makeSession("secret", NOW);
  assert.equal(await validSession("secret", s, NOW + 1000), true);
  assert.equal(await validSession("secret", s, NOW + 31 * 86400000), false);
  assert.equal(await validSession("other", s, NOW), false);
  const [exp, sig] = s.split(".");
  assert.equal(await validSession("secret", `${Number(exp) + 999999}.${sig}`, NOW), false, "a longer expiry breaks the signature");
  assert.equal(await validSession("secret", "nonsense", NOW), false);
  assert.equal(await validSession("", s, NOW), false);
});

test("the session cookie is read from among others", () => {
  const req = new Request("https://x/", { headers: { Cookie: "a=1; nf_session=123.abc; b=2" } });
  assert.equal(sessionFrom(req), "123.abc");
});

test("send: no channel counts as delivered; a failing channel doesn't", async () => {
  const { send } = await import("../src/alerts.js");
  const msg = [{ job: "a", state: "failed", text: "✖ A failed" }];
  assert.equal(await send({}, msg, ""), true);
  const real = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response("down", { status: 503 });
    assert.equal(await send({ ALERT_NTFY_URL: "https://ntfy.sh/x" }, msg, ""), false);
    globalThis.fetch = async (url, init) => { assert.equal(init.headers.Title, "A job needs a look"); return new Response("ok"); };
    assert.equal(await send({ ALERT_NTFY_URL: "https://ntfy.sh/x" }, msg, "https://site"), true);
  } finally { globalThis.fetch = real; }
});
