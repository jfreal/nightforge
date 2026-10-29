#!/usr/bin/env python3
"""botlog — tell the Routines page what a bot did, and what it is expected to do.

A bot runs this once when it finishes:

    python botlog.py report notion-pickup found "2 tickets coded, 2 PRs opened" \\
        --count prs=2 --count tickets=2 --link "mergetel #655=https://github.com/jfreal/mergetel/pull/655"

STATUS is one of: ok (ran, nothing needs you), found (ran and produced something),
needs-you (stopped on a decision), failed (did not finish its job).

With `reportUrl` in config.json, a report goes to the job-reports endpoint (tools/job-reports),
the one place every job reports to, and the dashboard pulls it from there. Without it, the report
is written straight into the local database.

Register a job on the endpoint, once per job:

    python botlog.py job add notion-pickup --name "Notion Pickup" --every 6h --grace 2h \\
        --does "Turns Ready tickets into reviewed PRs" --runs-on "cloud routine" --schedule "8:47 and 14:47 ET"
    python botlog.py job list           every registered job and its last run
    python botlog.py job rm notion-pickup

Without an endpoint, `bot` sets the same things in the local database:

    python botlog.py bot notion-pickup --name "Notion Pickup" --kind "cloud routine" --every 6h

Other commands:

    python botlog.py list               every bot and where it stands
    python botlog.py runs notion-pickup recent runs of one bot
    python botlog.py collect            run the collectors in config.json once
    python botlog.py desktop            list Claude desktop scheduled tasks; --apply adds the
                                        report-back step to each one's prompt
    python botlog.py remove BOT         forget a bot and its runs

The database is config.json's `routinesDb`, else $NIGHTFORGE_DB, else ~/.nightforge/routines.db.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

import endpoint
import routines

HERE = Path(__file__).resolve().parent


class LazyStore:
    """routines.Store, created the first time a command touches it."""

    def __init__(self, path):
        self.path, self.store = path, None

    def __getattr__(self, name):
        if self.store is None:
            self.store = routines.Store(self.path)
        return getattr(self.store, name)


def load_cfg(path) -> dict:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def pairs(items, what) -> list[tuple[str, str]]:
    out = []
    for item in items or []:
        if what == "link":
            # Split at the '=' just before http(s)://, since the URL may hold '=' of its own.
            m = re.match(r"^(.*?)=(https?://.*)$", item, re.I)
            key, value = (m.group(1), m.group(2)) if m and m.group(1).strip() else (item, item)
        else:
            key, sep, value = item.partition("=")
            if not sep:
                raise routines.ReportError(f"--{what} {item!r}: write it as name=value")
        out.append((key.strip(), value.strip()))
    return out


def cmd_report(store, a) -> int:
    if a.json:
        text = sys.stdin.read() if a.json == "-" else Path(a.json).read_text(encoding="utf-8")
        report = json.loads(text)
    else:
        if not a.bot or not a.status:
            raise routines.ReportError("give BOT and STATUS, or --json FILE")
        counts = {}
        for k, v in pairs(a.count, "count"):
            try:
                counts[k] = int(v)
            except ValueError:
                raise routines.ReportError(f"--count {k}={v}: the value must be a whole number") from None
        report = {"bot": a.bot, "status": a.status, "summary": a.summary or "", "counts": counts,
                  "links": [{"label": k, "url": v} for k, v in pairs(a.link, "link")]}
        for key in ("name", "run", "started", "finished"):
            if getattr(a, key):
                report[key] = getattr(a, key)
        if a.details_file:
            report["details"] = Path(a.details_file).read_text(encoding="utf-8")
        elif a.details:
            report["details"] = a.details
    remote = None if a.local else endpoint.settings(a.cfg)
    if remote:
        routines.clean_report(report)   # same checks the endpoint makes, so a typo fails here
        r = endpoint.report(*remote, {**report, "job": report.get("job") or report.get("bot"),
                                      "source": report.get("source") or "botlog"})
        print(f"sent {r['job']}: {report['status']}" + (" (already sent)" if r.get("duplicate") else ""))
        return 0
    new = store.add_run(report, "cli")
    name = report.get("bot") or report.get("job")
    print(f"recorded {name}: {report['status']}" if new else f"{name}: that run is already recorded")
    return 0


def cmd_job(store, a) -> int:
    remote = endpoint.settings(a.cfg)
    if not remote:
        raise routines.ReportError("`job` needs reportUrl in config.json; use `bot` for the local database")
    if a.job_cmd == "list":
        for j in endpoint.jobs(*remote):
            last = j.get("last") or {}
            every = f"every {j['every']} min" if j.get("every") else "no cadence"
            print(f"{j['id']:32} {every:16} {'registered' if j['registered'] else 'NOT registered':15}"
                  f" last {last.get('finished', 'never'):20} {last.get('status', ''):9} {last.get('summary', '')}")
        return 0
    if a.job_cmd == "rm":
        r = endpoint.remove(*remote, a.job)
        print("removed" if r.get("removed") else f"no job named {a.job}")
        if r.get("removed"):
            store.remove_bot(a.job)
        return 0
    fields = {k: v for k, v in {"name": a.name, "every": a.every, "grace": a.grace, "does": a.does,
                                "runsOn": a.runs_on, "schedule": a.schedule, "url": a.url}.items()
              if v is not None}
    if a.pause or a.resume:
        fields["paused"] = bool(a.pause)
    j = endpoint.register(*remote, a.job, fields)["job"]
    every = f"every {j['every']} min" if j["every"] else "no cadence set"
    print(f"{j['id']}: {j['name']} · {j['runsOn'] or 'runs-on not set'} · {every}" + (" · paused" if j["paused"] else ""))
    return 0


def cmd_bot(store, a) -> int:
    fields = {k: v for k, v in {"name": a.name, "kind": a.kind, "schedule": a.schedule,
                                "every_minutes": a.every, "grace_minutes": a.grace,
                                "description": a.desc, "url": a.url}.items() if v is not None}
    if a.pause or a.resume:
        fields["paused"] = bool(a.pause)
    bot = store.set_bot(a.bot, **fields)
    every = f"every {bot['every_minutes']} min" if bot["every_minutes"] else "no cadence set"
    print(f"{bot['id']}: {bot['name']} · {bot['kind'] or 'kind not set'} · {every}"
          + (" · paused" if bot["paused"] else ""))
    return 0


def cmd_list(store, a) -> int:
    rows = store.board()
    if not rows:
        print("No bots yet. A bot appears after its first report, or after `botlog.py bot <id>`.")
    for r in rows:
        h, last = r["health"], r["last"]
        when = last["finished"] if last else "never"
        print(f"{h['lane']:8} {h['timing']:11} {r['id']:32} last {when:20} {r['runs7d']:3} runs/7d  "
              f"{(last or {}).get('summary', '')}")
    return 0


def cmd_runs(store, a) -> int:
    for run in store.runs(a.bot, a.limit):
        print(f"{run['finished']}  {run['status']:9} {run['summary']}")
    return 0


def cmd_collect(store, a) -> int:
    import collect
    import server   # for DEFAULTS, so owners match what the dashboard uses
    cfg = server.load_config(Path(a.config))
    status = collect.collect_all(store, cfg)
    if not status:
        print("No collectors configured. Set reportInbox, actionsRepos or windowsTasks in config.json.")
    for s in status:
        print(f"{s['source']}: " + (f"error: {s['error']}" if s["error"] else f"{s['added']} new")
              + "".join(f"\n  problem: {p}" for p in s["problems"]))
    return 1 if any(s["error"] for s in status) else 0


def cmd_desktop(store, a) -> int:
    import desktop
    root = Path(a.dir).expanduser() if a.dir else desktop.tasks_dir()
    tasks = [t for t in desktop.discover(root) if not a.only or t["folder"] in a.only or t["id"] in a.only]
    if not tasks:
        print(f"No scheduled tasks found in {root}" + (" matching --only" if a.only else "") + ".")
        return 1
    changed = 0
    for t in tasks:
        will = desktop.remove(t, a.apply) if a.remove else desktop.install(t, a.apply)
        if a.apply and not a.remove:
            # Only a task that now reports gets a row; the preview writes nothing at all.
            store.ensure_bot_now(t["id"], t["name"], "desktop task", description=t["description"][:300])
        changed += will
        now = "reports" if t["reports"] else "silent"
        verb = ("removed" if a.apply else "would remove") if a.remove else \
               ("updated" if t["reports"] else "added") if a.apply else \
               ("would update" if t["reports"] else "would add")
        print(f"{now:8} {t['id']:40} {verb if will else 'unchanged'}")
    if not a.apply:
        print(f"\n{changed} file(s) would change. Nothing written; add --apply to do it.")
        if not a.remove:
            print("Each changed SKILL.md keeps its original as SKILL.md.nightforge.bak.")
            print("\nThe footer added to each prompt:\n")
            print(desktop.footer(tasks[0]["id"]))
    else:
        print(f"\n{changed} file(s) changed. The next run of each task uses it.")
    if not a.remove:
        print("\nIf a task's permission mode asks before running commands, its last step will wait for you."
              " Answer \"always allow\" the first time, or allow this prefix in ~/.claude/settings.json:\n"
              "  " + json.dumps(f'Bash("{desktop.posix(sys.executable)}" "{desktop.posix(desktop.BOTLOG)}" report:*)'))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--db", help="database file (overrides config and $NIGHTFORGE_DB)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("report", help="record one finished run")
    r.add_argument("bot", nargs="?")
    r.add_argument("status", nargs="?", choices=list(routines.STATUSES))
    r.add_argument("summary", nargs="?", help="one line, plain words")
    r.add_argument("--count", action="append", metavar="NAME=N", help="a number worth tracking; repeatable")
    r.add_argument("--link", action="append", metavar="LABEL=URL", help="a PR, issue or report; repeatable")
    r.add_argument("--details", help="longer notes, shown when the row is opened")
    r.add_argument("--details-file", help="read the notes from a file")
    r.add_argument("--name", help="display name, used when the bot is new")
    r.add_argument("--run", help="the run's own id, so a retried report is not counted twice")
    r.add_argument("--started")
    r.add_argument("--finished", help="defaults to now")
    r.add_argument("--json", metavar="FILE", help="read the whole report as JSON ('-' for stdin)")
    r.add_argument("--local", action="store_true", help="write to the local database even when reportUrl is set")

    jb = sub.add_parser("job", help="register jobs on the job-reports endpoint")
    jsub = jb.add_subparsers(dest="job_cmd", required=True)
    ja = jsub.add_parser("add", help="register a job, or change one (only the options given change)")
    ja.add_argument("job")
    ja.add_argument("--name")
    ja.add_argument("--every", help="how often it should report: 15m, 6h, 1d, 1w ('' clears)")
    ja.add_argument("--grace", help="how late it may be before it counts as late ('' = half of --every)")
    ja.add_argument("--does", help="what it is for, one line")
    ja.add_argument("--runs-on", help="github action, cloud routine, desktop task, script…")
    ja.add_argument("--schedule", help="shown as written, like '8:47 and 14:47 ET'")
    ja.add_argument("--url", help="where to manage it")
    jg = ja.add_mutually_exclusive_group()
    jg.add_argument("--pause", action="store_true", help="stop alarming about it")
    jg.add_argument("--resume", action="store_true")
    jr = jsub.add_parser("rm", help="forget a job and its runs, on the endpoint and here")
    jr.add_argument("job")
    jsub.add_parser("list", help="every registered job and its last run")

    b = sub.add_parser("bot", help="add a bot, or change what it is expected to do")
    b.add_argument("bot")
    b.add_argument("--name")
    b.add_argument("--kind", help="cloud routine, desktop task, windows task, github action, script…")
    b.add_argument("--schedule", help="shown as written, like '8:47 and 14:47 ET'")
    b.add_argument("--every", help="how often it should report: 15m, 6h, 1d, 1w ('' clears)")
    b.add_argument("--grace", help="how late it may be before it counts as late ('' = half of --every)")
    b.add_argument("--desc", help="what it is for, one line")
    b.add_argument("--url", help="where to manage it")
    g = b.add_mutually_exclusive_group()
    g.add_argument("--pause", action="store_true", help="stop alarming about it")
    g.add_argument("--resume", action="store_true")

    sub.add_parser("list", help="every bot and where it stands")
    rr = sub.add_parser("runs", help="recent runs of one bot")
    rr.add_argument("bot")
    rr.add_argument("--limit", type=int, default=20)
    rm = sub.add_parser("remove", help="forget a bot and its runs")
    rm.add_argument("bot")
    sub.add_parser("collect", help="run the configured collectors once")
    d = sub.add_parser("desktop", help="list desktop scheduled tasks; --apply adds the report-back step")
    d.add_argument("--apply", action="store_true", help="write the change (default: preview only)")
    d.add_argument("--remove", action="store_true", help="take the report-back step out again")
    d.add_argument("--only", action="append", metavar="TASK", help="just this task folder or id; repeatable")
    d.add_argument("--dir", help="the scheduled-tasks folder (default ~/.claude/scheduled-tasks)")

    a = ap.parse_args(argv)
    try:
        a.cfg = load_cfg(a.config)
        # Opened on first use, so a report sent to the endpoint never needs the local file.
        store = LazyStore(a.db or routines.db_path(a.cfg))
        if a.cmd == "remove":
            print("removed" if store.remove_bot(a.bot) else f"no bot named {a.bot}")
            return 0
        return {"report": cmd_report, "bot": cmd_bot, "list": cmd_list, "runs": cmd_runs,
                "collect": cmd_collect, "desktop": cmd_desktop, "job": cmd_job}[a.cmd](store, a)
    except (routines.ReportError, endpoint.EndpointError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"botlog: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
