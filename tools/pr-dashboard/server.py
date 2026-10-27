#!/usr/bin/env python3
"""pr-dashboard — a local page listing every open PR, oldest first, with Merge and Send to Claude.

Serves static/ and three endpoints on 127.0.0.1 only:

    GET  /api/prs     every open PR as a row (cached for refreshSeconds/2)
    POST /api/merge   re-check one PR, then `gh pr merge` it with the repo's merge method
    POST /api/send    open a Claude Code session on one PR (see launch.py)

Both POSTs need the per-start token the page carries, so no other website can drive them
through the browser.

Usage:
    python server.py                 # uses config.json next to this file if present
    python server.py --dry-run       # Merge and Send log their command instead of running it
    python server.py --no-open       # do not open the browser
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import github
import launch

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
}


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULTS)
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    if not cfg["owners"]:
        raise SystemExit("config error: 'owners' must name at least one GitHub owner")
    return cfg


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
            return {"rows": self.rows, "error": self.error,
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


def send(cfg, body, dry_run) -> dict:
    owner, repo, num = parse_key(body.get("key"))
    if owner not in cfg["owners"]:
        return {"ok": False, "error": f"{owner} is not in this dashboard's owners"}
    result = launch.open_session(github.fetch_row(owner, repo, num), cfg["reposRoot"], dry_run)
    log(("dry run, would run: " if dry_run else "sent: ") + " ".join(result.get("command", [])[:8])
        if result["ok"] else f"send failed: {result['error']}")
    return result


def make_handler(cfg, board, token, dry_run):
    origins = {f"http://127.0.0.1:{cfg['port']}", f"http://localhost:{cfg['port']}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, data, ctype="application/json; charset=utf-8"):
            payload = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/prs":
                return self.reply(200, board.get(force="force=1" in self.path))
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
            if self.headers.get("Origin") not in origins or self.headers.get("X-Dash-Token") != token:
                return self.reply(403, {"ok": False, "error": "Forbidden. Reload the page."})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 10_000)) or b"{}")
                if self.path == "/api/merge":
                    result = merge(cfg, body, dry_run)
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
    args = ap.parse_args()
    cfg = load_config(Path(args.config))
    if args.port:
        cfg["port"] = args.port
    token = secrets.token_urlsafe(24)
    board = Board(cfg)
    server = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), make_handler(cfg, board, token, args.dry_run))
    url = f"http://127.0.0.1:{cfg['port']}/"
    log(f"PR dashboard on {url}{'  (dry run: Merge and Send only log)' if args.dry_run else ''}")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
