"""Collectors: read runs from the places bots already leave a trace, into routines.Store.

    inbox     comments on a GitHub repo's issues that carry a ```nightforge-report block.
              Cloud routines can't reach this PC, so they report here and this reads it back.
    actions   scheduled GitHub Actions runs in the repos you name. Nothing to change in them.
    windows   Windows Task Scheduler's last run and result for the tasks you name.
    desktop   Claude desktop scheduled tasks, listed from ~/.claude/scheduled-tasks (desktop.py).
              Their runs arrive through botlog.py, from the footer desktop.py adds.

Each collector returns how many new runs it stored. A source that fails raises; collect_all
catches that per source, so one broken source never hides the others.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import timedelta
from pathlib import Path

import desktop
import github
from routines import ReportError, iso, parse_time, slug, utcnow

REPORT_BLOCK = re.compile(r"```nightforge-report[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)
INBOX_LOOKBACK_DAYS = 30
FAILED_CONCLUSIONS = {"failure", "timed_out", "startup_failure", "cancelled", "action_required"}


def gh_pages(path: str) -> list:
    """`gh api --paginate` on a list endpoint. gh prints one JSON array per page, back to back,
    so decode them one at a time rather than as one document."""
    text, items, pos = github.gh(["api", "--paginate", path]), [], 0
    decoder = json.JSONDecoder()
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            return items
        page, pos = decoder.raw_decode(text, pos)
        items += page if isinstance(page, list) else [page]


# ---- inbox -------------------------------------------------------------------------------

def reports_in(body: str) -> list[dict]:
    """Every report block in one comment. A block that isn't JSON is returned as a ReportError
    so the page can say which comment to fix."""
    found = []
    for block in REPORT_BLOCK.findall(body or ""):
        try:
            found.append(json.loads(block))
        except ValueError as exc:
            found.append(ReportError(f"report block is not JSON: {exc}"))
    return found


def collect_inbox(store, repo: str, authors) -> tuple[int, list[str]]:
    key = f"inbox:{repo}"
    since = store.cursor(key) or iso(utcnow() - timedelta(days=INBOX_LOOKBACK_DAYS))
    comments = gh_pages(f"repos/{repo}/issues/comments?since={since}&per_page=100")
    added, problems, newest = 0, [], since
    allowed = {a.lower() for a in authors}
    for c in comments:
        newest = max(newest, c.get("updated_at") or newest)
        # Anyone who can comment on the repo could post a block. Only your own account counts.
        if (c.get("user") or {}).get("login", "").lower() not in allowed:
            continue
        reports = reports_in(c.get("body", ""))
        for i, report in enumerate(reports):
            where = c.get("html_url", f"comment {c.get('id')}")
            if isinstance(report, ReportError):
                problems.append(f"{where}: {report}")
                continue
            if not isinstance(report, dict):
                problems.append(f"{where}: report block is not a JSON object")
                continue
            report.setdefault("run", f"comment-{c['id']}" + (f"-{i}" if len(reports) > 1 else ""))
            report.setdefault("finished", c.get("created_at"))
            report["links"] = list(report.get("links") or []) + [{"label": "report", "url": c["html_url"]}]
            try:
                added += store.add_run(report, "inbox", kind="cloud routine")
            except (ReportError, KeyError, TypeError, AttributeError) as exc:
                problems.append(f"{where}: {exc}")
    store.set_cursor(key, newest)
    return added, problems


# ---- GitHub Actions ----------------------------------------------------------------------

def action_report(repo: str, run: dict) -> dict:
    name = repo.split("/", 1)[1]
    stem = os.path.splitext(os.path.basename(run.get("path") or run.get("name") or "workflow"))[0]
    conclusion = run.get("conclusion") or "unknown"
    return {
        "bot": slug(f"{name}-{stem}"),
        "name": f"{name}: {run.get('name') or stem}",
        "run": str(run["id"]),
        "status": "failed" if conclusion in FAILED_CONCLUSIONS else "ok",
        "summary": f"Run #{run.get('run_number')} {conclusion.replace('_', ' ')}",
        "started": run.get("run_started_at"),
        "finished": run.get("updated_at") or run.get("run_started_at"),
        "links": [{"label": f"run #{run.get('run_number')}", "url": run["html_url"]}],
    }


def collect_actions(store, repo: str) -> int:
    data = json.loads(github.gh(["api", f"repos/{repo}/actions/runs?event=schedule&status=completed&per_page=50"]))
    added = 0
    for run in data.get("workflow_runs", []):
        stem = os.path.basename(run.get("path") or "")
        added += store.add_run(action_report(repo, run), "actions", kind="github action",
                               url=f"https://github.com/{repo}/actions/workflows/{stem}" if stem else "")
    return added


# ---- Windows Task Scheduler --------------------------------------------------------------

# One PowerShell call for every task. Times leave as Unix seconds, so nothing depends on how
# this PC's locale writes a date. The names go in as JSON in an environment variable: extra
# arguments after -Command are glued onto the script text, not passed as $args.
PS_TASKS = r"""
$ErrorActionPreference = 'Stop'
$out = foreach ($n in @($env:NF_TASKS | ConvertFrom-Json)) {
  try {
    foreach ($t in @(Get-ScheduledTask -TaskName $n)) {
      $i = $t | Get-ScheduledTaskInfo
      [pscustomobject]@{
        name = $t.TaskName; path = $t.TaskPath; state = [string]$t.State
        last = [int64]([DateTimeOffset]$i.LastRunTime).ToUnixTimeSeconds()
        result = [int64]$i.LastTaskResult
        next = $(if ($i.NextRunTime) { [int64]([DateTimeOffset]$i.NextRunTime).ToUnixTimeSeconds() } else { 0 })
      }
    }
  } catch { [pscustomobject]@{ name = $n; error = $_.Exception.Message } }
}
ConvertTo-Json -InputObject @($out) -Compress
"""
TASK_RUNNING, TASK_NEVER_RAN = 0x41301, 0x41303
YEAR_2000 = 946684800   # Task Scheduler reports 30 Nov 1999 for "never ran"


def task_report(task: dict) -> dict | None:
    """The task's last run as a report, or None while it is running or if it never ran."""
    code = task["result"] & 0xFFFFFFFF
    if task["last"] < YEAR_2000 or code in (TASK_RUNNING, TASK_NEVER_RAN):
        return None
    return {
        "bot": slug(task["name"]),
        "name": task["name"],
        "run": str(task["last"]),
        "status": "ok" if code == 0 else "failed",
        "summary": "Exit code 0" if code == 0 else f"Exit code 0x{code:X}"
                   + (" (task is disabled)" if task.get("state") == "Disabled" else ""),
        "finished": task["last"],
    }


def collect_windows(store, names) -> tuple[int, list[str]]:
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_TASKS],
                          capture_output=True, creationflags=github.NO_WINDOW, timeout=60,
                          env={**os.environ, "NF_TASKS": json.dumps(list(names))})
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip() or f"powershell exited {proc.returncode}")
    tasks = json.loads(proc.stdout.decode("utf-8", "replace") or "[]")
    added, problems = 0, []
    # A list that matched nothing comes back as [null].
    for task in (t for t in tasks if isinstance(t, dict)):
        if task.get("error"):
            problems.append(f"{task['name']}: {task['error']}")
            continue
        bot = slug(task["name"])
        report = task_report(task)
        if report:
            added += store.add_run(report, "windows", kind="windows task", schedule="Task Scheduler")
        else:
            store.ensure_bot_now(bot, task["name"], "windows task")
        store.set_next(bot, iso(parse_time(task["next"])) if task["next"] else None)
    return added, problems


# ---- all of them -------------------------------------------------------------------------

def sources(cfg) -> list[tuple[str, callable]]:
    out = []
    if cfg.get("reportInbox"):
        authors = cfg.get("reportAuthors") or cfg["owners"]
        out.append((f"inbox {cfg['reportInbox']}", lambda s: collect_inbox(s, cfg["reportInbox"], authors)))
    for repo in cfg.get("actionsRepos") or []:
        out.append((f"actions {repo}", lambda s, repo=repo: collect_actions(s, repo)))
    if cfg.get("windowsTasks") and os.name == "nt":
        out.append(("windows tasks", lambda s: collect_windows(s, cfg["windowsTasks"])))
    if cfg.get("desktopTasks", True):
        root = Path(cfg["desktopTasksDir"]).expanduser() if cfg.get("desktopTasksDir") else None
        out.append(("desktop tasks", lambda s: desktop.collect(s, root)))
    return out


def collect_all(store, cfg, log=print) -> list[dict]:
    """Run every configured collector once. Returns one status line per source for the page."""
    status = []
    for name, fn in sources(cfg):
        entry = {"source": name, "at": iso(utcnow()), "added": 0, "problems": [], "error": ""}
        try:
            result = fn(store)
            entry["added"], entry["problems"] = result if isinstance(result, tuple) else (result, [])
        except (github.GhError, RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            entry["error"] = str(exc)[:500]
            log(f"collect {name} failed: {entry['error']}")
        if entry["added"]:
            log(f"collect {name}: {entry['added']} new run(s)")
        status.append(entry)
    return status
