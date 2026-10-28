#!/usr/bin/env python3
"""botlog — tell the Routines page what a bot did, and what it is expected to do.

A bot runs this once when it finishes:

    python botlog.py report notion-pickup found "2 tickets coded, 2 PRs opened" \\
        --count prs=2 --count tickets=2 --link "mergetel #655=https://github.com/jfreal/mergetel/pull/655"

STATUS is one of: ok (ran, nothing needs you), found (ran and produced something),
needs-you (stopped on a decision), failed (did not finish its job).

You tell it what to expect, once per bot:

    python botlog.py bot notion-pickup --name "Notion Pickup" --kind "cloud routine" \\
        --schedule "8:47 and 14:47 ET" --every 6h --grace 2h

Other commands:

    python botlog.py list               every bot and where it stands
    python botlog.py runs notion-pickup recent runs of one bot
    python botlog.py collect            run the collectors in config.json once
    python botlog.py remove BOT         forget a bot and its runs

The database is config.json's `routinesDb`, else $NIGHTFORGE_DB, else ~/.nightforge/routines.db.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import routines

HERE = Path(__file__).resolve().parent


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
    new = store.add_run(report, "cli")
    print(f"recorded {report['bot']}: {report['status']}" if new else f"{report['bot']}: that run is already recorded")
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

    a = ap.parse_args(argv)
    store = routines.Store(a.db or routines.db_path(load_cfg(a.config)))
    try:
        if a.cmd == "remove":
            print("removed" if store.remove_bot(a.bot) else f"no bot named {a.bot}")
            return 0
        return {"report": cmd_report, "bot": cmd_bot, "list": cmd_list, "runs": cmd_runs,
                "collect": cmd_collect}[a.cmd](store, a)
    except (routines.ReportError, ValueError, OSError) as exc:
        print(f"botlog: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
