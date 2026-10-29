// The checks every report and registration goes through.   node --test test/*.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { cleanJob, cleanReport, parseMinutes, parseTime, pathJobId, iso, BadRequest } from "../src/worker.js";

const NOW = new Date("2026-10-29T12:00:00Z");
const good = (over = {}) => ({ job: "notion-pickup", status: "found", summary: "2 PRs", ...over });

test("a good report keeps its fields", () => {
  const r = cleanReport(good({ counts: { prs: 2 }, links: [{ label: "#655", url: "https://github.com/x/y/pull/655" }],
                               run: "cse_1", finished: "2026-10-29T11:00:00Z", source: "cloud routine" }), NOW);
  assert.deepEqual([r.job, r.status, r.run, r.finished, r.source], ["notion-pickup", "found", "cse_1", "2026-10-29T11:00:00Z", "cloud routine"]);
  assert.deepEqual(r.counts, { prs: 2 });
  assert.equal(r.links[0].label, "#655");
});

test("'bot' is accepted for 'job', and finished defaults to now", () => {
  const r = cleanReport({ bot: "x", status: "ok" }, NOW);
  assert.equal(r.job, "x");
  assert.equal(r.finished, "2026-10-29T12:00:00Z");
  assert.ok(r.run, "a run id is made up when none is sent");
});

test("bad fields are refused, naming the field", () => {
  for (const [over, field] of [[{ status: "done" }, /status/], [{ job: "Notion Pickup" }, /job/],
                               [{ links: ["javascript:alert(1)"] }, /links/], [{ counts: { prs: 1.5 } }, /counts.prs/],
                               [{ counts: { prs: "2" } }, /counts.prs/], [{ finished: "soon" }, /not a time/]]) {
    assert.throws(() => cleanReport(good(over), NOW), (e) => e instanceof BadRequest && field.test(e.message));
  }
  assert.throws(() => cleanReport([], NOW), BadRequest);
});

test("summary folds whitespace and is cut to 300", () => {
  const r = cleanReport(good({ summary: "a\n  b " + "x".repeat(400) }), NOW);
  assert.ok(r.summary.startsWith("a b x"));
  assert.equal(r.summary.length, 300);
});

test("durations", () => {
  const cases = [["15m", 15], ["2h", 120], ["1d", 1440], ["1d12h", 2160], ["1w", 10080], ["90", 90], [45, 45], ["", null], [null, null]];
  for (const [input, mins] of cases) assert.equal(parseMinutes(input), mins, String(input));
  assert.throws(() => parseMinutes("soon"), BadRequest);
});

test("times: zone, offset, epoch, and no zone means UTC", () => {
  assert.equal(iso(parseTime("2026-10-29T08:00:00-04:00")), "2026-10-29T12:00:00Z");
  assert.equal(iso(parseTime(1793275200)), "2026-10-29T12:00:00Z");
  assert.equal(iso(parseTime("2026-10-29T12:00:00")), "2026-10-29T12:00:00Z");
});

test("a registration changes only what it names", () => {
  assert.deepEqual(cleanJob({ name: " Notion Pickup ", every: "6h", grace: "2h", paused: false }),
                   { name: "Notion Pickup", every: 360, grace: 120, paused: 0 });
  assert.deepEqual(cleanJob({ does: "x" }), { does: "x" });
  assert.throws(() => cleanJob({ url: "ftp://x" }), /url/);
});

test("paused must be a real boolean", () => {
  assert.deepEqual(cleanJob({ paused: true }), { paused: 1 });
  for (const bad of ["false", "true", 0, null]) assert.throws(() => cleanJob({ paused: bad }), /paused/);
});

test("a malformed job path is a 400, not a crash", () => {
  assert.equal(pathJobId("notion-pickup"), "notion-pickup");
  assert.throws(() => pathJobId("%"), (e) => e instanceof BadRequest && /not a valid path/.test(e.message));
  assert.throws(() => pathJobId("Bad%20Id"), BadRequest);
});
