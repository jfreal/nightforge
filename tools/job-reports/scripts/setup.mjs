#!/usr/bin/env node
// One command from nothing to a live site, safe to run again:
//
//   node scripts/setup.mjs [--slack <webhook url>] [--ntfy <https://ntfy.sh/topic>] [--rotate-tokens]
//
// 1. finds or creates the D1 database and writes wrangler.toml (gitignored) from the example
// 2. applies the migrations
// 3. deploys, then writes the site's address into DASHBOARD_URL and deploys again
// 4. sets ADMIN_TOKEN and REPORT_TOKEN the first time (or with --rotate-tokens), and the alert
//    channels you pass; tokens are saved to ~/.nightforge/job-reports.json, outside the repo
//
// Needs Node 18+ and a signed-in wrangler (`npx wrangler login` once), or CLOUDFLARE_API_TOKEN
// and CLOUDFLARE_ACCOUNT_ID in the environment, which is how the GitHub workflow runs it.
// There, the tokens and alert channels come from the environment instead and are set on every
// run: JOB_REPORTS_ADMIN_TOKEN, JOB_REPORTS_REPORT_TOKEN, ALERT_SLACK_WEBHOOK, ALERT_NTFY_URL.

import { spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(dirname(fileURLToPath(import.meta.url)));
const TOML = join(HERE, "wrangler.toml");
const SAVED = join(homedir(), ".nightforge", "job-reports.json");
const DB = "job-reports";
const WIN = process.platform === "win32";

const args = process.argv.slice(2);
const flag = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };

function wrangler(cmd, { input, quiet } = {}) {
  // npx is npx.cmd on Windows, which only starts through a shell. The arguments are ours, not user text.
  const res = spawnSync("npx", ["--yes", "wrangler@4", ...cmd], { cwd: HERE, shell: WIN, input,
    encoding: "utf8", stdio: [input === undefined ? "inherit" : "pipe", "pipe", "pipe"] });
  if (!quiet) process.stdout.write(res.stdout || "");
  if (res.status !== 0) {
    process.stderr.write(res.stderr || "");
    throw new Error(`wrangler ${cmd.join(" ")} failed`);
  }
  return res.stdout || "";
}

function step(text) { console.log(`\n== ${text}`); }

const given = { ADMIN_TOKEN: process.env.JOB_REPORTS_ADMIN_TOKEN, REPORT_TOKEN: process.env.JOB_REPORTS_REPORT_TOKEN };
// A CI runner can't show you a token it makes up, so there the tokens must come from secrets.
if (process.env.CI && (!given.ADMIN_TOKEN || !given.REPORT_TOKEN)) {
  throw new Error("set the JOB_REPORTS_ADMIN_TOKEN and JOB_REPORTS_REPORT_TOKEN repository secrets");
}
// The admin token is the site's password, so a short one is refused before anything is deployed.
for (const [name, value] of Object.entries(given)) {
  if (value && value.length < 24) throw new Error(`JOB_REPORTS_${name} is under 24 characters; use a long random string`);
}

// 1. database and wrangler.toml
step("Checking you're signed in to Cloudflare");
wrangler(["whoami"]);

let toml = existsSync(TOML) ? readFileSync(TOML, "utf8") : readFileSync(join(HERE, "wrangler.example.toml"), "utf8");
if (toml.includes("REPLACE_WITH_YOUR_D1_DATABASE_ID")) {
  step(`Finding or creating the D1 database "${DB}"`);
  const list = JSON.parse(wrangler(["d1", "list", "--json"], { quiet: true }) || "[]");
  let id = list.find((d) => d.name === DB)?.uuid;
  if (!id) {
    const out = wrangler(["d1", "create", DB]);
    id = (out.match(/database_id\s*=\s*"([0-9a-f-]{36})"/) || out.match(/"database_id":\s*"([0-9a-f-]{36})"/) || [])[1];
    if (!id) id = JSON.parse(wrangler(["d1", "list", "--json"], { quiet: true })).find((d) => d.name === DB)?.uuid;
  }
  if (!id) throw new Error("could not read the new database's id; run `npx wrangler d1 list` and paste it into wrangler.toml");
  toml = toml.replace("REPLACE_WITH_YOUR_D1_DATABASE_ID", id);
  writeFileSync(TOML, toml);
  console.log(`wrote wrangler.toml with database ${id}`);
}

// 2. schema
step("Applying migrations");
wrangler(["d1", "migrations", "apply", DB, "--remote"], { input: "y\n" });

// 3. deploy, then again with the address known
step("Deploying");
const out = wrangler(["deploy"]);
const site = (out.match(/https:\/\/[a-z0-9.-]+\.workers\.dev/i) || [])[0];
if (site && !toml.includes(`DASHBOARD_URL = "${site}"`)) {
  toml = toml.replace(/^DASHBOARD_URL = ".*"$/m, `DASHBOARD_URL = "${site}"`);
  writeFileSync(TOML, toml);
  step("Deploying again with DASHBOARD_URL set");
  wrangler(["deploy"], { quiet: true });
}

// 4. secrets
const saved = existsSync(SAVED) ? JSON.parse(readFileSync(SAVED, "utf8")) : {};
const listed = JSON.parse(wrangler(["secret", "list", "--format", "json"], { quiet: true }) || "[]").map((s) => s.name);
const token = () => randomBytes(32).toString("base64url");

for (const name of ["ADMIN_TOKEN", "REPORT_TOKEN"]) {
  if (given[name]) {
    step(`Setting ${name} from the environment`);
    wrangler(["secret", "put", name], { input: given[name] });
    continue;
  }
  if (listed.includes(name) && !args.includes("--rotate-tokens")) continue;
  step(`Setting ${name}`);
  saved[name] = token();
  wrangler(["secret", "put", name], { input: saved[name] });
}
for (const [opt, name] of [["--slack", "ALERT_SLACK_WEBHOOK"], ["--ntfy", "ALERT_NTFY_URL"]]) {
  const value = flag(opt) || process.env[name];
  if (!value) continue;
  if (!/^https:\/\//.test(value)) throw new Error(`${opt} must be an https URL`);
  step(`Setting ${name}`);
  wrangler(["secret", "put", name], { input: value });
}
if (site) saved.url = site;
if (!process.env.CI) {
  // A CI runner is thrown away after the run; there the tokens already live in GitHub secrets.
  mkdirSync(dirname(SAVED), { recursive: true });
  writeFileSync(SAVED, JSON.stringify(saved, null, 2), { mode: 0o600 });
}

step("Done");
console.log(`Site:          ${saved.url || "(see the deploy output above)"}
Tokens:        ${process.env.CI ? "from the JOB_REPORTS_* secrets" : SAVED + (saved.ADMIN_TOKEN ? "" : "  (tokens were already set; pass --rotate-tokens for new ones)")}

Next:
- Open the site and sign in with ADMIN_TOKEN.
- Give jobs REPORT_TOKEN: a GitHub secret NF_REPORT_TOKEN, or the curl line in a job's prompt.
- Point the local dashboard at it (optional): reportUrl and reportToken in tools/pr-dashboard/config.json.
- Alerts: rerun with --slack <webhook> and/or --ntfy https://ntfy.sh/<topic>.`);
