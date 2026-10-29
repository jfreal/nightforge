"""Routines side of the dashboard: every bot, its runs, and whether it is keeping its schedule.

One SQLite file holds three tables:

    bots     one row per bot: name, where it runs, how often it should report
    runs     one row per finished run: when, status, a one-line summary, counts, links
    cursors  how far each collector has read, so a poll only fetches what is new

Runs arrive two ways, and both end in `Store.add_run`:
- pushed: the bot runs `botlog.py report ...` when it finishes (local scripts and tasks)
- pulled: collect.py reads GitHub Actions, Windows Task Scheduler and the report inbox

The database lives outside the repo (`~/.nightforge/routines.db` by default), because run
summaries carry whatever the bots found, and that is not for a public repo.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB = Path.home() / ".nightforge" / "routines.db"


def db_path(cfg: dict | None = None) -> Path:
    """config.json's `routinesDb`, else NIGHTFORGE_DB, else ~/.nightforge/routines.db."""
    chosen = (cfg or {}).get("routinesDb") or os.environ.get("NIGHTFORGE_DB") or DEFAULT_DB
    return Path(chosen).expanduser()

# What a run says about itself. Everything else on the page (late, never reported) is
# worked out from timestamps, never reported by the bot.
STATUSES = {
    "ok": "OK",
    "found": "Found something",
    "needs-you": "Needs you",
    "failed": "Failed",
}
BOT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
MAX_SUMMARY, MAX_DETAILS, MAX_LINKS = 300, 20_000, 20
STRIP = 14            # runs drawn as squares in the Track record column
HISTORY = 50          # runs listed when a row is opened
GUESS_FROM = 10       # recent runs used to guess a cadence nobody set

SCHEMA = """
CREATE TABLE IF NOT EXISTS bots (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT '',
  schedule      TEXT NOT NULL DEFAULT '',
  every_minutes INTEGER,
  grace_minutes INTEGER,
  description   TEXT NOT NULL DEFAULT '',
  url           TEXT NOT NULL DEFAULT '',
  paused        INTEGER NOT NULL DEFAULT 0,
  next_at       TEXT,
  created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  bot_id      TEXT NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
  run_id      TEXT NOT NULL,
  source      TEXT NOT NULL,
  started_at  TEXT,
  finished_at TEXT NOT NULL,
  status      TEXT NOT NULL,
  summary     TEXT NOT NULL DEFAULT '',
  counts      TEXT NOT NULL DEFAULT '{}',
  links       TEXT NOT NULL DEFAULT '[]',
  details     TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (bot_id, run_id)
);
CREATE INDEX IF NOT EXISTS runs_by_time ON runs (bot_id, finished_at DESC);
CREATE TABLE IF NOT EXISTS cursors (source TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

EDITABLE = ("name", "kind", "schedule", "every_minutes", "grace_minutes", "description", "url",
            "paused", "next_at")


class ReportError(ValueError):
    """A report or bot edit that cannot be stored as given. The message says which field."""


# ---- time and duration -------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value) -> datetime:
    """ISO 8601 (with Z or an offset) or Unix seconds. A time with no zone is taken as UTC,
    because every source that writes one (GitHub, our own CLI) means UTC."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    text = str(value).strip()
    if re.fullmatch(r"\d{9,11}(\.\d+)?", text):
        return datetime.fromtimestamp(float(text), timezone.utc)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReportError(f"not a time: {value!r} (use ISO 8601, like 2026-10-28T13:05:00Z)") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_minutes(value) -> int | None:
    """'15m', '2h', '1d', '1d12h' or a bare number of minutes. '' or None clears it."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    text = str(value).strip().lower().replace(" ", "")
    if text.isdigit():
        return int(text) or None
    parts = re.fullmatch(r"(?:(\d+)w)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?", text)
    if not parts or not any(parts.groups()):
        raise ReportError(f"not a duration: {value!r} (use 15m, 2h, 1d or 1w)")
    w, d, h, m = (int(g or 0) for g in parts.groups())
    return (((w * 7 + d) * 24 + h) * 60 + m) or None


def slug(text: str) -> str:
    """A bot id from a display name: 'CodeRabbit Sweep' -> 'coderabbit-sweep'."""
    return re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-")[:80] or "bot"


# ---- a report, checked ------------------------------------------------------------------

def clean_report(report: dict) -> dict:
    """Check one run report and bring it to the stored shape. Raises ReportError naming the
    field. Bots write these, so nothing in one is trusted: links must be http(s), counts must be
    whole numbers, and text is cut to size."""
    if not isinstance(report, dict):
        raise ReportError("a report is a JSON object")
    bot = str(report.get("bot") or report.get("job") or "").strip()   # the endpoint says "job"
    if not BOT_ID.match(bot):
        raise ReportError(f"bot: {bot!r} is not an id (lowercase letters, digits, '-', '.', '_')")
    status = str(report.get("status", "")).strip().lower()
    if status not in STATUSES:
        raise ReportError(f"status: {status!r} is not one of {', '.join(STATUSES)}")
    finished = parse_time(report["finished"]) if report.get("finished") else utcnow()
    started = parse_time(report["started"]) if report.get("started") else None
    counts = {}
    for k, v in (report.get("counts") or {}).items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
            raise ReportError(f"counts.{k}: {v!r} is not a whole number")
        counts[str(k)[:40]] = int(v)
    links = []
    for link in (report.get("links") or [])[:MAX_LINKS]:
        if isinstance(link, str):
            link = {"label": link, "url": link}
        url = str(link.get("url", ""))
        # The page puts these in href, so only plain web links get through.
        if not re.match(r"^https?://", url, re.I):
            raise ReportError(f"links: {url!r} is not an http(s) link")
        links.append({"label": str(link.get("label") or url)[:120], "url": url[:2000]})
    summary = " ".join(str(report.get("summary", "")).split())
    return {
        "bot": bot,
        "name": str(report.get("name") or "").strip()[:120],
        "run": str(report.get("run") or iso(finished))[:200],
        "status": status,
        "summary": summary[:MAX_SUMMARY - 1] + "…" if len(summary) > MAX_SUMMARY else summary,
        "counts": counts,
        "links": links,
        "details": str(report.get("details", ""))[:MAX_DETAILS],
        "started": iso(started) if started else None,
        "finished": iso(finished),
    }


# ---- whether a bot is keeping its schedule -----------------------------------------------

def guess_every(finished_times: list[str]) -> int | None:
    """The median gap between recent runs, in minutes, for a bot nobody gave a cadence. Needs
    three runs; two points are one gap, and one gap is a coincidence."""
    times = sorted(parse_time(t) for t in finished_times[:GUESS_FROM])
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:])]
    gaps = [g for g in gaps if g >= 1]
    return round(statistics.median(gaps)) if len(gaps) >= 2 else None


def default_grace(every: int) -> int:
    """How late a run may be before the page calls it late: half the cadence, kept between a
    quarter of an hour and four hours."""
    return max(15, min(240, every // 2))


def health(bot: dict, last: dict | None, now: datetime, guessed: int | None = None) -> dict:
    """Where a bot stands. `timing` comes from the clock, `result` from the bot's last report,
    and `lane` is the page's filter: you (waiting on you), broken, healthy, paused."""
    every = bot.get("every_minutes") or guessed
    grace = bot.get("grace_minutes") or (default_grace(every) if every else None)
    result = last["status"] if last else None
    due_at = late_at = None
    if bot.get("paused"):
        timing = "paused"
    elif not every:
        timing = "unscheduled" if last else "waiting"
    else:
        since = parse_time(last["finished"]) if last else parse_time(bot["created_at"])
        due_at, late_at = since + timedelta(minutes=every), since + timedelta(minutes=every + grace)
        if now > late_at:
            timing = "late" if last else "never"
        elif now > due_at:
            timing = "due"
        else:
            timing = "ontime" if last else "waiting"
    if timing == "paused":
        lane = "paused"
    elif result == "needs-you":
        lane = "you"
    elif result == "failed" or timing in ("late", "never"):
        lane = "broken"
    else:
        lane = "healthy"
    overdue = max(0, int((now - late_at).total_seconds() // 60)) if late_at else 0
    return {"timing": timing, "result": result, "lane": lane, "every": every,
            "everyGuessed": bool(every and not bot.get("every_minutes")), "grace": grace,
            "dueAt": iso(due_at) if due_at else None, "lateAt": iso(late_at) if late_at else None,
            "overdueMinutes": overdue}


LANE_ORDER = {"you": 0, "broken": 1, "healthy": 2, "paused": 3}


def sort_key(row: dict):
    """Worst first: waiting on you, then broken (latest-overdue first), then the rest by name."""
    h = row["health"]
    return (LANE_ORDER[h["lane"]], -h["overdueMinutes"], row["name"].lower())


# ---- the store -------------------------------------------------------------------------

class Store:
    """The SQLite file. Each call opens its own connection, so the server's threads and a
    `botlog.py` started by a bot can all use it at once; WAL mode lets readers carry on while
    one of them writes."""

    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextlib.contextmanager
    def connect(self):
        """Commit on success, roll back on error, and always close. sqlite3's own `with` only
        commits, and on Windows an open handle keeps the file locked."""
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    # bots

    def ensure_bot(self, db, bot_id: str, name: str = "", kind: str = "", **extra) -> None:
        """Make the bot if it is new. A known bot is left as it is: what a person set with
        `botlog.py bot` outranks what a collector guesses."""
        db.execute("INSERT OR IGNORE INTO bots (id, name, kind, created_at) VALUES (?, ?, ?, ?)",
                   (bot_id, name or bot_id, kind, iso(utcnow())))
        fill = {k: v for k, v in {"kind": kind, **extra}.items() if k in EDITABLE and v not in (None, "")}
        for key, value in fill.items():
            # Only fill blanks, never overwrite a value somebody chose.
            db.execute(f"UPDATE bots SET {key} = ? WHERE id = ? AND ({key} IS NULL OR {key} = '')",
                       (value, bot_id))

    def ensure_bot_now(self, bot_id: str, name: str = "", kind: str = "", **extra) -> None:
        """ensure_bot on its own connection: list a bot before its first run arrives."""
        with self.connect() as db:
            self.ensure_bot(db, bot_id, name, kind, **extra)

    def set_next(self, bot_id: str, next_at: str | None) -> None:
        """The source's own next run time. The source owns it, so it overwrites every poll."""
        with self.connect() as db:
            db.execute("UPDATE bots SET next_at = ? WHERE id = ?", (next_at, bot_id))

    def set_bot(self, bot_id: str, **fields) -> dict:
        if not BOT_ID.match(bot_id):
            raise ReportError(f"{bot_id!r} is not a bot id (lowercase letters, digits, '-', '.', '_')")
        bad = set(fields) - set(EDITABLE)
        if bad:
            raise ReportError(f"unknown field(s): {', '.join(sorted(bad))}")
        for key in ("every_minutes", "grace_minutes"):
            if key in fields:
                fields[key] = parse_minutes(fields[key])
        if "paused" in fields:
            fields["paused"] = 1 if fields["paused"] else 0
        with self.connect() as db:
            self.ensure_bot(db, bot_id, fields.get("name") or bot_id)
            for key, value in fields.items():
                db.execute(f"UPDATE bots SET {key} = ? WHERE id = ?", (value, bot_id))
            return dict(db.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)).fetchone())

    def remove_bot(self, bot_id: str) -> bool:
        with self.connect() as db:
            return db.execute("DELETE FROM bots WHERE id = ?", (bot_id,)).rowcount > 0

    # runs

    def add_run(self, report: dict, source: str, kind: str = "", **bot_extra) -> bool:
        """Store one run. Returns False when that run is already stored: every source re-reads
        some overlap, so the same run arriving twice is normal, not an error."""
        r = clean_report(report)
        with self.connect() as db:
            self.ensure_bot(db, r["bot"], r["name"], kind, **bot_extra)
            cur = db.execute(
                "INSERT OR IGNORE INTO runs (bot_id, run_id, source, started_at, finished_at, status,"
                " summary, counts, links, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["bot"], r["run"], source, r["started"], r["finished"], r["status"], r["summary"],
                 json.dumps(r["counts"]), json.dumps(r["links"]), r["details"]))
            return cur.rowcount > 0

    def runs(self, bot_id: str, limit: int = HISTORY) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM runs WHERE bot_id = ? ORDER BY finished_at DESC LIMIT ?",
                              (bot_id, limit)).fetchall()
        return [run_dict(r, details=True) for r in rows]

    # cursors

    def cursor(self, source: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM cursors WHERE source = ?", (source,)).fetchone()
        return row["value"] if row else None

    def set_cursor(self, source: str, value: str) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO cursors (source, value) VALUES (?, ?)"
                       " ON CONFLICT(source) DO UPDATE SET value = excluded.value", (source, value))

    # the page

    def board(self, now: datetime | None = None) -> list[dict]:
        """One row per bot, worst first, with everything the table shows."""
        now = now or utcnow()
        week_ago = iso(now - timedelta(days=7))
        with self.connect() as db:
            bots = [dict(b) for b in db.execute("SELECT * FROM bots")]
            out = []
            for bot in bots:
                recent = db.execute(
                    "SELECT run_id, source, started_at, finished_at, status, summary, counts, links"
                    " FROM runs WHERE bot_id = ? ORDER BY finished_at DESC LIMIT ?",
                    (bot["id"], max(STRIP, GUESS_FROM))).fetchall()
                total, week = db.execute(
                    "SELECT COUNT(*), SUM(finished_at >= ?) FROM runs WHERE bot_id = ?",
                    (week_ago, bot["id"])).fetchone()
                counts7 = {}
                for (c,) in db.execute("SELECT counts FROM runs WHERE bot_id = ? AND finished_at >= ?",
                                       (bot["id"], week_ago)):
                    for k, v in json.loads(c).items():
                        counts7[k] = counts7.get(k, 0) + v
                last = run_dict(recent[0]) if recent else None
                guessed = None if bot["every_minutes"] else guess_every([r["finished_at"] for r in recent])
                out.append({
                    "id": bot["id"], "name": bot["name"], "kind": bot["kind"], "schedule": bot["schedule"],
                    "description": bot["description"], "url": bot["url"], "paused": bool(bot["paused"]),
                    "nextAt": bot["next_at"], "createdAt": bot["created_at"],
                    "health": health(bot, last, now, guessed), "last": last,
                    "strip": [{"status": r["status"], "finished": r["finished_at"], "summary": r["summary"]}
                              for r in reversed(recent[:STRIP])],
                    "runsTotal": total, "runs7d": week or 0, "counts7d": counts7,
                })
        return sorted(out, key=sort_key)


def run_dict(row, details=False) -> dict:
    d = {"run": row["run_id"], "source": row["source"], "started": row["started_at"],
         "finished": row["finished_at"], "status": row["status"], "summary": row["summary"],
         "counts": json.loads(row["counts"]), "links": json.loads(row["links"])}
    if details:
        d["details"] = row["details"]
    return d
