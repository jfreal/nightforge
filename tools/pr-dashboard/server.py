#!/usr/bin/env python3
"""pr-dashboard — a local page listing every open PR, oldest first, with Merge and Send to Claude.

Serves static/ and three endpoints on 127.0.0.1 only:

    GET  /api/prs     every open PR as a row (cached for refreshSeconds/2)
    GET  /api/stats   headline numbers from the Night Forge shipping report, if it exists
    GET  /api/screens one PR's Screenery screenshots (?key=owner/repo#n), changed ones first
    POST /api/merge     re-check one PR, then `gh pr merge` it with the repo's merge method
    POST /api/fixmerge  open a session told to fix one PR, wait for green, then merge it
    POST /api/review    open a session that runs /code-review on one PR and records the verdict
    POST /api/send    open a Claude Code session on one PR (see launch.py)

Both POSTs need the per-start token the page carries, so no other website can drive them
through the browser.

Usage:
    python server.py                 # uses config.json next to this file if present
    python server.py --dry-run       # Merge and Send log their command instead of running it
    python server.py --no-open       # do not open the browser
    python server.py --watch         # exit with RESTART when a .py file changes (supervise.py
                                     # starts it again, so a git pull needs no manual restart)
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import github
import launch
import screenery

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
LOGO = HERE.parents[1] / "docs" / "logo.png"   # the repo's one copy, shared with the site
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml"}

DEFAULTS = {
    "owners": ["jfreal"],
    "excludeRepos": ["archive"],
    # Clones sit side by side with this repo: <reposRoot>/<repo name>.
    "reposRoot": str(HERE.parents[2]),
    "port": 8765,
    "refreshSeconds": 60,
    # Written every Monday by the night-forge-weekly routine; the header shows its headline numbers.
    "statsFile": str(Path.home() / ".claude" / "scheduled-tasks" / "pr-shipping-report" / "report.json"),
    "statsUrl": "https://claude.ai/artifact/5DELXS2AMoHrbGMWVYPkDm",
    # Where Review, Fix and merge and Send to Claude run: "background" (no window; pick it up in
    # Claude desktop through Remote Control), "cloud" (Anthropic's servers, started from a
    # terminal tab) or "terminal" (a local session in a new Windows Terminal tab).
    "sessionTarget": "background",
    # Screenery project per repo, when it is not "<owner>/<repo>". "" turns a repo off.
    "screeneryProjects": {},
}


LOG_FILE = HERE / "dashboard.log"
LOG_MAX_BYTES = 1_000_000
RESTART = 75          # exit code that tells supervise.py "code changed, start me again"


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    if sys.stdout is not None:
        print(line, flush=True)
    else:
        # Under pythonw.exe (supervise.py, the logon task) there is no console; keep a file.
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
                LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
            with LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def code_stamp() -> dict:
    return {p.name: p.stat().st_mtime for p in HERE.glob("*.py")}


def watch_code(server, every=2.0) -> None:
    """Stop the server when its own Python changes. Static files are read per request, so
    only .py edits need a restart, and supervise.py does that restart."""
    first = code_stamp()
    while True:
        time.sleep(every)
        try:
            now = code_stamp()
        except OSError:
            continue
        if now != first:
            changed = sorted(k for k in set(now) | set(first) if now.get(k) != first.get(k))
            log(f"code changed ({', '.join(changed)}), restarting")
            server.restart_requested = True
            server.shutdown()
            return


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    if not cfg["owners"]:
        raise SystemExit("config error: 'owners' must name at least one GitHub owner")
    return cfg


# Changes on every start. The page reloads itself when it sees a new one, so it picks up the
# new per-start token and page code after a restart without anyone pressing anything.
BOOT_ID = secrets.token_hex(6)


class Server(ThreadingHTTPServer):
    # Python sets SO_REUSEADDR, which on Windows lets a second copy bind the same port and
    # silently share it. A dashboard that can merge must fail loudly instead.
    allow_reuse_address = False
    allow_reuse_port = False


class Board:
    """The PR list, fetched at most once per refreshSeconds/2 however many tabs ask."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.rows, self.fetched_at, self.error = [], 0.0, ""

    def get(self, force=False) -> dict:
        with self.lock:
            stale = time.time() - self.fetched_at > self.cfg["refreshSeconds"] / 2
            if force or stale:
                try:
                    self.rows = github.build_rows(github.fetch_nodes(self.cfg["owners"]),
                                                  self.cfg["excludeRepos"])
                    self.error = ""
                except (github.GhError, ValueError, KeyError) as exc:
                    # Keep showing the last good list; the page shows the error above it.
                    self.error = str(exc)[:500]
                    log(f"refresh failed: {self.error}")
                self.fetched_at = time.time()
            return {"rows": self.rows, "error": self.error, "bootId": BOOT_ID,
                    "fetchedAt": datetime.fromtimestamp(self.fetched_at, timezone.utc).isoformat()}

    def invalidate(self):
        with self.lock:
            self.fetched_at = 0.0


def parse_key(key: str):
    """'jfreal/pheidi#1243' -> ('jfreal', 'pheidi', 1243)."""
    slug, _, num = str(key).partition("#")
    owner, _, repo = slug.partition("/")
    if not owner or not repo or not num.isdigit():
        raise ValueError(f"bad PR key: {key!r}")
    return owner, repo, int(num)


def stats(cfg) -> dict:
    """The few numbers worth a glance, picked out of the weekly shipping report."""
    path = Path(cfg["statsFile"])
    if not cfg["statsFile"] or not path.exists():
        return {"ok": False}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        week = next(w for w in d["windows"] if w["days"] == 7)
        month = next((m for m in d.get("forecast", {}).get("months", []) if m.get("partial")), None)
        return {"ok": True, "url": cfg["statsUrl"], "generatedAt": d["generated_at"],
                "week": week["prs"], "prevWeek": d["prev7"], "perDay": week["per_day"],
                "medianMergeHours": week["median_merge_hours"], "allTime": d["totals"]["prs"],
                "monthForecast": month and {"month": month["month"], "prs": month["prs"]}}
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        log(f"stats file unreadable: {exc}")
        return {"ok": False}


def screens(cfg, raw_path) -> dict:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(raw_path).query)
    try:
        owner, repo, num = parse_key((query.get("key") or [""])[0])
    except ValueError as exc:
        return {"ok": False, "reason": str(exc)}
    if owner not in cfg["owners"]:
        return {"ok": False, "reason": f"{owner} is not in this dashboard's owners"}
    project = cfg["screeneryProjects"].get(repo, f"{owner}/{repo}")
    if not project:
        return {"ok": False, "reason": "none"}
    return screenery.gallery(project, num)


def merge(cfg, body, dry_run) -> dict:
    owner, repo, num = parse_key(body.get("key"))
    if owner not in cfg["owners"]:
        return {"ok": False, "error": f"{owner} is not in this dashboard's owners"}
    row = github.fetch_row(owner, repo, num)
    # Re-check on fresh data: the list in the page can be a minute old.
    problems = []
    if row["headSha"] != body.get("headSha"):
        problems.append("someone pushed since the page loaded. Refresh and look again")
    if row["draft"]:
        problems.append("it is a draft")
    if row["conflict"] != "no":
        problems.append("it has a merge conflict" if row["conflict"] == "yes"
                        else "GitHub is still checking for conflicts")
    if row["checks"] not in ("pass", "none"):
        problems.append(f"checks are {row['checks']}")
    # Same review gate as github.lane_of: a missing review is only a soft blocker on the page,
    # but a label removed since the page loaded must still stop a direct merge.
    if row["review"] != "done" and row["verify"] != "pass":
        problems.append("this commit has no review yet (CodeRabbit, Claude or the verified label)")
    # Verify failed or open review comments: green checks alone are not enough. Use Fix and merge.
    problems += [b["text"].lower() for b in row["blockers"]
                 if b["hard"] and b["text"] not in ("Merge conflict", "Checks failed")]
    if not row["mergeMethod"]:
        problems.append("the repo allows no merge method")
    if problems:
        return {"ok": False, "error": "Not merged: " + "; ".join(problems) + "."}
    cmd = ["pr", "merge", str(num), "-R", f"{owner}/{repo}", f"--{row['mergeMethod']}",
           "--match-head-commit", row["headSha"]]
    if dry_run:
        log("dry run, would run: gh " + " ".join(cmd))
        return {"ok": True, "dryRun": True, "command": "gh " + " ".join(cmd)}
    try:
        github.gh(cmd)
    except github.GhError as exc:
        return {"ok": False, "error": str(exc)}
    log(f"merged {owner}/{repo}#{num} ({row['mergeMethod']})")
    return {"ok": True, "method": row["mergeMethod"]}


def fix_and_merge(cfg, body, dry_run) -> dict:
    """Not mergeable yet: hand it to a session that fixes it, waits for green, then merges."""
    owner, repo, num = parse_key(body.get("key"))
    if owner not in cfg["owners"]:
        return {"ok": False, "error": f"{owner} is not in this dashboard's owners"}
    return start_session(cfg, owner, repo, num, dry_run, "merge", "fix and merge")


def review(cfg, body, dry_run) -> dict:
    """Missing a review: a session runs /code-review and sets the claude-review status."""
    owner, repo, num = parse_key(body.get("key"))
    if owner not in cfg["owners"]:
        return {"ok": False, "error": f"{owner} is not in this dashboard's owners"}
    return start_session(cfg, owner, repo, num, dry_run, "review", "review")


def start_session(cfg, owner, repo, num, dry_run, mode, what) -> dict:
    result = launch.open_session(github.fetch_row(owner, repo, num), cfg["reposRoot"], dry_run, mode,
                                 cfg["sessionTarget"])
    log((("dry run, would run: " if dry_run else f"{what} sent: ") + f"{owner}/{repo}#{num}")
        if result["ok"] else f"{what} failed: {result['error']}")
    return result


def send(cfg, body, dry_run) -> dict:
    owner, repo, num = parse_key(body.get("key"))
    if owner not in cfg["owners"]:
        return {"ok": False, "error": f"{owner} is not in this dashboard's owners"}
    return start_session(cfg, owner, repo, num, dry_run, "send", "send")


def make_handler(cfg, board, token, dry_run):
    origins = {f"http://127.0.0.1:{cfg['port']}", f"http://localhost:{cfg['port']}"}
    # A DNS-rebinding page can point its own name at 127.0.0.1 and read the token and the
    # PR list. It cannot fake the Host header, so only loopback names are served.
    hosts = {f"127.0.0.1:{cfg['port']}", f"localhost:{cfg['port']}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, data, ctype="application/json; charset=utf-8"):
            payload = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            # No framing: a hostile page could overlay this one and steer clicks onto Merge.
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.headers.get("Host") not in hosts:
                return self.reply(403, {"error": "Forbidden host."})
            path = self.path.split("?", 1)[0]
            if path == "/api/prs":
                return self.reply(200, board.get(force="force=1" in self.path))
            if path == "/api/stats":
                return self.reply(200, stats(cfg))
            if path == "/api/screens":
                return self.reply(200, screens(cfg, self.path))
            name = "index.html" if path == "/" else path.lstrip("/")
            # Only plain file names inside static/: no folders, no `..`.
            file = LOGO if name == "logo.png" else STATIC / name
            if "/" in name or "\\" in name or not file.is_file() or file.suffix not in TYPES:
                return self.reply(404, {"error": "not found"})
            data = file.read_bytes()
            if name == "index.html":
                data = data.replace(b"__TOKEN__", token.encode()).replace(
                    b"__DRY_RUN__", b"true" if dry_run else b"false")
            self.reply(200, data, TYPES[file.suffix])

        def do_POST(self):
            if (self.headers.get("Host") not in hosts or self.headers.get("Origin") not in origins
                    or self.headers.get("X-Dash-Token") != token):
                return self.reply(403, {"ok": False, "error": "Forbidden. Reload the page."})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 10_000)) or b"{}")
                if self.path == "/api/merge":
                    result = merge(cfg, body, dry_run)
                elif self.path == "/api/review":
                    result = review(cfg, body, dry_run)
                elif self.path == "/api/fixmerge":
                    result = fix_and_merge(cfg, body, dry_run)
                elif self.path == "/api/send":
                    result = send(cfg, body, dry_run)
                else:
                    return self.reply(404, {"ok": False, "error": "not found"})
            except (ValueError, github.GhError) as exc:
                result = {"ok": False, "error": str(exc)}
            if result.get("ok") and self.path == "/api/merge" and not dry_run:
                board.invalidate()
            self.reply(200, result)

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--port", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--watch", action="store_true")
    args = ap.parse_args()
    cfg = load_config(Path(args.config))
    if args.port:
        cfg["port"] = args.port
    token = secrets.token_urlsafe(24)
    board = Board(cfg)
    try:
        server = Server(("127.0.0.1", cfg["port"]), make_handler(cfg, board, token, args.dry_run))
    except OSError:
        raise SystemExit(f"Port {cfg['port']} is already in use. Is the dashboard already running?"
                         " Close that window first, or pass --port.")
    url = f"http://127.0.0.1:{cfg['port']}/"
    log(f"PR dashboard on {url}{'  (dry run: Merge and Send only log)' if args.dry_run else ''}")
    if not args.no_open:
        webbrowser.open(url)
    server.restart_requested = False
    if args.watch:
        threading.Thread(target=watch_code, args=(server,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
    return RESTART if server.restart_requested else 0


if __name__ == "__main__":
    sys.exit(main())
