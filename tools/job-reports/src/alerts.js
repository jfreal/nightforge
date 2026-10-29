// Telling you when a job goes wrong, even with every page closed. The Worker's cron runs this
// every few minutes. A message is sent when a job's state changes, not on every run of the cron:
// once when it goes late, fails or needs you, and once more when it is healthy again.
//
// Channels, each optional and set as a Worker secret:
//   ALERT_SLACK_WEBHOOK  a Slack incoming-webhook URL
//   ALERT_NTFY_URL       an ntfy topic URL (https://ntfy.sh/<topic>), for a push to your phone

const WORDS = {
  late: (r, now) => `hasn't reported for ${span(Math.floor((now - Date.parse(r.last.finished)) / 60000))}`
    + (r.health.every ? ` (expected every ${span(r.health.every)})` : ""),
  never: () => "has never reported",
  failed: (r) => `failed${r.last?.summary ? `: ${r.last.summary}` : ""}`,
  "needs-you": (r) => `needs you${r.last?.summary ? `: ${r.last.summary}` : ""}`,
};
const ICON = { late: "⏰", never: "⏰", failed: "✖", "needs-you": "👤", ok: "✓" };

function span(mins) {
  const d = Math.floor(mins / 1440), h = Math.floor((mins % 1440) / 60), m = mins % 60;
  return d ? (h ? `${d}d ${h}h` : `${d}d`) : h ? (m ? `${h}h ${m}m` : `${h}h`) : `${m}m`;
}

/** What is wrong with a row right now, in one word, or "ok". */
export function problemOf(row) {
  const h = row.health;
  if (h.lane === "paused") return "ok";
  if (h.result === "needs-you") return "needs-you";
  if (h.result === "failed") return "failed";
  if (h.timing === "late" || h.timing === "never") return h.timing;
  return "ok";
}

/** Compare the board with what was last alerted. Returns the messages to send and the states
 *  to remember. `known` maps job id -> the problem last alerted ("ok" or absent if none). */
export function changes(rows, known, now = Date.now()) {
  const messages = [], states = new Map();
  for (const row of rows) {
    const state = problemOf(row), before = known.get(row.id) || "ok";
    states.set(row.id, state);
    if (state === before) continue;
    // A job that was never broken and is fine now needs no message; one that recovered does.
    if (state === "ok") messages.push({ job: row.id, state, text: `${ICON.ok} ${row.name} is healthy again` });
    else messages.push({ job: row.id, state, text: `${ICON[state]} ${row.name} ${WORDS[state](row, now)}` });
  }
  return { messages, states };
}

/** Post the messages to every configured channel. True when at least one took them, or when no
 *  channel is set up (then there is nowhere to retry to, and a later setup shouldn't get a flood). */
export async function send(env, messages, dashboardUrl) {
  if (!messages.length) return true;
  const link = dashboardUrl ? `\n${dashboardUrl}` : "";
  const text = messages.map((m) => m.text).join("\n") + link;
  const results = [];
  if (env.ALERT_SLACK_WEBHOOK) {
    results.push(fetch(env.ALERT_SLACK_WEBHOOK, { method: "POST", headers: { "Content-Type": "application/json" },
                                                  body: JSON.stringify({ text }) }));
  }
  if (env.ALERT_NTFY_URL) {
    const bad = messages.some((m) => m.state !== "ok");
    results.push(fetch(env.ALERT_NTFY_URL, { method: "POST", body: text, headers: {
      Title: bad ? "A job needs a look" : "Jobs recovered", Tags: bad ? "warning" : "white_check_mark",
      ...(dashboardUrl ? { Click: dashboardUrl } : {}) } }));
  }
  if (!results.length) return true;
  const settled = await Promise.allSettled(results);
  return settled.some((r) => r.status === "fulfilled" && r.value.ok);
}
