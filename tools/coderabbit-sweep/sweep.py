#!/usr/bin/env python3
"""coderabbit-sweep — deterministic implementation of the coderabbit-sweep pipeline.

Finds every open PR across an owner's repos whose CodeRabbit review is missing,
throttled, or stale against the head commit, picks the single oldest one, and
spends the account's one available review on it.

This is a straight port of skills/coderabbit-sweep/SKILL.md. Every rule that file
states is implemented here; the section it comes from is named in a comment.

Only external dependency: the `gh` CLI, authenticated.

Usage:
    python sweep.py --config config.json
    python sweep.py --config config.json --dry-run     # classify + board, never fire
    python sweep.py --config config.json --no-poll     # fire, skip the 5-minute poll
    python sweep.py --config config.json --open        # open the board when done
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    # SKILL step 3: CodeRabbit bodies are UTF-8 and cp1252 chokes on 0x8d.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BOT = "coderabbitai[bot]"
HOUR = timedelta(minutes=60)
FIRE_MARGIN = timedelta(seconds=60)          # SKILL step 2: never fire at the computed edge
MARKER_CAP = timedelta(minutes=60)           # SKILL step 2: cap review-in-progress windows

VERBOSE = False
LOG_FH = None            # set once the config names a state directory
LOG_MAX_BYTES = 2_000_000


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #

def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def iso(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def human_delta(delta: timedelta) -> str:
    secs = int(abs(delta).total_seconds())
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def log(msg: str) -> None:
    line = f"[{iso(now_utc())}] {msg}"
    print(line, flush=True)
    if LOG_FH is not None:
        # A scheduled run has no console; the log file is the only trace of it.
        LOG_FH.write(line + "\n")
        LOG_FH.flush()


LOCK_STALE = timedelta(minutes=25)


def acquire_lock(state_dir: Path):
    """Refuse to start while another live sweep holds the lock.

    The scheduled task sets MultipleInstances=IgnoreNew, so it cannot overlap itself,
    but an on-demand run started while a tick is mid-poll can. Two live sweeps would
    each derive a gate from a ledger the other has not written yet and could each fire,
    breaking the one-trigger-per-run guarantee.
    """
    path = state_dir / "sweep.lock"
    import atexit

    payload = json.dumps({"pid": os.getpid(), "at": iso(now_utc())})
    for attempt in (0, 1):
        try:
            # O_EXCL makes create-if-absent one atomic step. exists()-then-write is a
            # race two simultaneous runs can both win, which is the thing being prevented.
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            held = None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):          # a non-dict would break .get()
                    held = parse_ts(data.get("at"))
            except (json.JSONDecodeError, OSError, ValueError):
                held = None
            if held and (now_utc() - held) < LOCK_STALE:
                return None, held
            if attempt == 0:
                # Stale, or unreadable and therefore untrustworthy. Clear it and retry once.
                vlog("clearing a stale or unreadable lock")
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            return None, held
        except OSError as exc:
            # Distinct from "someone holds it": the caller would otherwise log
            # "another sweep has been running since " with an empty timestamp.
            log(f"cannot create the sweep lock at {path}: {exc} — not firing")
            return None, None
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            atexit.register(lambda: path.unlink(missing_ok=True))
            return path, None
    return None, None


def open_log(state_dir: Path):
    """Append this run to stateDir/sweep.log, rotating once it gets fat."""
    global LOG_FH
    path = state_dir / "sweep.log"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            backup = path.with_suffix(".log.1")
            backup.unlink(missing_ok=True)
            path.rename(backup)
        LOG_FH = path.open("a", encoding="utf-8")
    except OSError:
        LOG_FH = None
    return path


def vlog(msg: str) -> None:
    if VERBOSE:
        log("  " + msg)


# --------------------------------------------------------------------------- #
# gh plumbing
# --------------------------------------------------------------------------- #

class GhError(RuntimeError):
    pass


TRANSIENT = ("dial tcp", "connection reset", "timeout", "TLS handshake",
             "502 Bad Gateway", "503", "EOF", "temporary failure")

# GitHub answers a secondary rate limit with 403 or 429. These are retryable, but
# only after a real pause — retrying one immediately just spends the next allowance.
THROTTLED = ("secondary rate limit", "rate limit exceeded", "429",
             "abuse detection", "You have exceeded")

# Under pythonw.exe the parent has no console, so every gh.exe child would allocate
# its own visible window — ~20 of them per run. CREATE_NO_WINDOW stops that at the
# source. It does not exist off Windows, hence the getattr.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def gh_raw(args, check=True, attempts=3) -> str:
    """Run gh and return stdout as text. SKILL step 3: a zero-byte body is a hard stop.

    A network wobble must never look like "this repo has no starved PRs", so transient
    failures are retried and a persistent one is raised for the caller to report loudly.
    """
    last = ""
    for attempt in range(attempts):
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            creationflags=NO_WINDOW,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "GH_PAGER": "cat", "NO_COLOR": "1"},
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode == 0:
            if check and len(proc.stdout) == 0:
                # SKILL step 3: `[]` is 2 bytes and meaningful; zero bytes is an errored call.
                last = f"gh {' '.join(args)} returned a zero-byte body: {err[:200]}"
            else:
                return out
        else:
            last = f"gh {' '.join(args)} -> exit {proc.returncode}: {err[:400]}"
            if not check:
                return ""
        if attempt < attempts - 1:
            # Classify on stderr alone. `last` embeds the command line, so a PR numbered
            # 429 or a branch containing "timeout" would be read as a rate limit.
            low = err.lower()
            if any(t.lower() in low for t in THROTTLED):
                pause = 20 * (attempt + 1)
                vlog(f"gh hit a GitHub rate limit, waiting {pause}s: {last[:120]}")
                time.sleep(pause)
                continue
            if any(t.lower() in low for t in TRANSIENT):
                vlog(f"transient gh failure, retrying: {last[:120]}")
                time.sleep(2 * (attempt + 1))
                continue
        break
    raise GhError(last)


def parse_arrays(s: str):
    """SKILL step 3: --paginate emits concatenated JSON arrays; decode, never regex."""
    dec = json.JSONDecoder()
    out = []
    i = 0
    n = len(s)
    while i < n:
        while i < n and s[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        obj, i = dec.raw_decode(s, i)
        out += obj if isinstance(obj, list) else [obj]
    return out


def gh_json(args):
    return json.loads(gh_raw(args))


def gh_paginated(args):
    return parse_arrays(gh_raw(["api", "--paginate", *args]))


_commit_date_cache: dict = {}


def commit_date(slug: str, sha: str):
    """SKILL step 2: attempt time floor comes from the reviewed commit's committer date."""
    key = (slug, sha)
    if key not in _commit_date_cache:
        try:
            raw = gh_raw(["api", f"repos/{slug}/commits/{sha}", "--jq", ".commit.committer.date"]).strip()
            _commit_date_cache[key] = parse_ts(raw.strip('"'))
        except GhError as exc:
            vlog(f"commit date lookup failed for {slug}@{sha[:8]}: {exc}")
            _commit_date_cache[key] = None
    return _commit_date_cache[key]


# --------------------------------------------------------------------------- #
# CodeRabbit body parsing
# --------------------------------------------------------------------------- #

# SKILL step 2: both wordings, spaces/asterisks between every token, unit captured.
NEXT_REVIEW_RE = re.compile(
    r"[Nn]ext[\s*]+(?:included[\s*]+)?review available[\s*:in]*?[\s*]*(\d+)[\s*]*(minutes?|seconds?|hours?)"
)
# SKILL step 3: the count lives INSIDE the bold.
FINDINGS_RE = re.compile(r"\*\*Actionable comments posted:\s*(\d+)\*\*")
SHA_RE = re.compile(r"\b([0-9a-f]{40})\b")

RATE_START = "<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->"
RATE_END = "<!-- end of auto-generated comment: rate limited by coderabbit.ai -->"
RECENT_START = "<!-- recent_review_start -->"
RECENT_END = "<!-- recent_review_end -->"

MARKERS = {
    "rate_limited": "rate limited by coderabbit.ai",
    "skip_review": "skip review by coderabbit.ai",
    "walkthrough": "walkthrough_start",
    "recent_review": "recent_review_start",
    "in_progress": "review in progress by coderabbit.ai",
    "paused": "review paused by coderabbit.ai",
    "failure": "failure by coderabbit.ai",
}

UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600}


def slice_between(body: str, start: str, end: str):
    i = body.find(start)
    if i < 0:
        return None
    j = body.find(end, i)
    return body[i: j + len(end)] if j >= 0 else body[i:]


def is_pass(review) -> bool:
    """SKILL step 3, the completeness contract. Empty-bodied reviews are thread replies."""
    body = review.get("body") or ""
    if not body.strip():
        return False
    return body.startswith("**Actionable comments posted:") or "Outside diff range comments" in body


def parse_countdown(block: str):
    """Return (seconds, is_refusal). SKILL step 2: the size refusal carries no countdown."""
    refusal = ("Review skipped" in block) or ("Too many files" in block)
    m = NEXT_REVIEW_RE.search(block)
    if not m:
        return None, refusal
    value = int(m.group(1))
    unit = m.group(2).rstrip("s")
    if unit not in UNIT_SECONDS:
        raise GhError(f"unparseable countdown unit {unit!r}")
    return value * UNIT_SECONDS[unit], refusal


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #

class PR:
    """One classified pull request."""

    def __init__(self, slug, number):
        self.slug = slug
        self.number = number
        self.key = f"{slug}#{number}"

        self.title = ""
        self.url = ""
        self.head = ""
        self.created_at = None
        self.state = ""
        self.merged = False
        self.draft = False
        self.changed_files = 0
        self.additions = 0
        self.deletions = 0
        self.author = ""

        self.bot_reviews = []
        self.passes = []
        self.empty_bodied = 0
        self.newest_pass = None
        self.newest_pass_at = None
        self.newest_pass_sha = None
        self.findings = None

        self.summary = None            # the bot summary comment dict
        self.summary_updated_at = None
        self.summary_url = ""
        self.markers = set()

        self.rate_block_at = None      # summary comment updated_at when a block is present
        self.rate_block_seconds = None
        self.rate_block_reset = None
        self.rate_block_names_head = False
        self.rate_block_refusal = False

        self.recent_review_shas = []
        self.recent_review_at = None

        self.trigger_at = None         # newest @coderabbitai trigger comment created_at
        self.in_progress_since = None

        self.failure = False
        self.tier = "never"            # never | stale | current
        self.complete_at = None        # newest completion evidence timestamp at head
        self.complete_url = ""
        self.complete_kind = ""
        self.errors = []

    # -- derived ---------------------------------------------------------- #

    @property
    def is_complete(self):
        return self.tier == "current"

    @property
    def short_head(self):
        return self.head[:8]

    def attempt_floor(self, sha=None):
        """SKILL step 2: attempt = max(commit committer date, PR createdAt)."""
        cd = commit_date(self.slug, sha or self.head)
        candidates = [d for d in (cd, self.created_at) if d]
        return max(candidates) if candidates else None


def classify(slug: str, number: int, trigger_phrase: str) -> PR:
    pr = PR(slug, number)

    meta = gh_json(["api", f"repos/{slug}/pulls/{number}"])
    pr.title = meta.get("title") or ""
    pr.url = meta.get("html_url") or f"https://github.com/{slug}/pull/{number}"
    pr.head = (meta.get("head") or {}).get("sha") or ""
    pr.created_at = parse_ts(meta.get("created_at"))
    pr.state = meta.get("state") or ""
    pr.merged = bool(meta.get("merged"))
    pr.draft = bool(meta.get("draft"))
    pr.changed_files = meta.get("changed_files") or 0
    pr.additions = meta.get("additions") or 0
    pr.deletions = meta.get("deletions") or 0
    pr.author = ((meta.get("user") or {}).get("login")) or ""

    # -- review objects ---------------------------------------------------- #
    reviews = gh_paginated([f"repos/{slug}/pulls/{number}/reviews"])
    pr.bot_reviews = [r for r in reviews if (r.get("user") or {}).get("login") == BOT]
    for r in pr.bot_reviews:
        if is_pass(r):
            pr.passes.append(r)
        elif not (r.get("body") or "").strip():
            pr.empty_bodied += 1
    pr.passes.sort(key=lambda r: parse_ts(r.get("submitted_at")) or datetime.min.replace(tzinfo=timezone.utc))
    if pr.passes:
        newest = pr.passes[-1]
        pr.newest_pass = newest
        pr.newest_pass_at = parse_ts(newest.get("submitted_at"))
        pr.newest_pass_sha = newest.get("commit_id") or ""
        m = FINDINGS_RE.search(newest.get("body") or "")
        pr.findings = int(m.group(1)) if m else None

    # -- comments (deliberately NOT bot-filtered; the trigger is human-written) -- #
    comments = gh_paginated([f"repos/{slug}/issues/{number}/comments"])
    bot_comments = [c for c in comments if (c.get("user") or {}).get("login") == BOT]

    for c in bot_comments:
        body = c.get("body") or ""
        if "auto-generated comment: summarize by coderabbit.ai" in body or MARKERS["walkthrough"] in body:
            pr.summary = c
    if pr.summary is None and bot_comments:
        # Fall back to the oldest bot comment; it is the one carrying the markers.
        pr.summary = bot_comments[0]

    if pr.summary is not None:
        body = pr.summary.get("body") or ""
        pr.summary_updated_at = parse_ts(pr.summary.get("updated_at"))
        pr.summary_url = pr.summary.get("html_url") or pr.url
        for name, needle in MARKERS.items():
            if needle in body:
                pr.markers.add(name)

        if "rate_limited" in pr.markers:
            block = slice_between(body, RATE_START, RATE_END) or body
            secs, refusal = parse_countdown(block)
            pr.rate_block_refusal = refusal
            pr.rate_block_at = pr.summary_updated_at
            if secs is not None:
                pr.rate_block_seconds = secs
                if pr.rate_block_at:
                    pr.rate_block_reset = pr.rate_block_at + timedelta(seconds=secs)
            elif not refusal:
                # SKILL step 2: markers present but no countdown parsed is a parse bug.
                pr.errors.append("rate limited marker with no parseable countdown and no refusal text")
            pr.rate_block_names_head = bool(pr.head and pr.head in block)

        if "recent_review" in pr.markers:
            block = slice_between(body, RECENT_START, RECENT_END) or ""
            pr.recent_review_shas = SHA_RE.findall(block)
            pr.recent_review_at = pr.summary_updated_at

        if "failure" in pr.markers:
            pr.failure = True

    # -- trigger comment (starts the review-in-progress window) ------------- #
    phrase_core = trigger_phrase.strip().lower()
    for c in comments:
        body = (c.get("body") or "").strip().lower()
        if phrase_core and phrase_core in body:
            at = parse_ts(c.get("created_at"))
            if at and (pr.trigger_at is None or at > pr.trigger_at):
                pr.trigger_at = at
    if "in_progress" in pr.markers:
        pr.in_progress_since = pr.trigger_at or pr.summary_updated_at

    # -- parse-integrity assertions (SKILL step 3) -------------------------- #
    if (
        "walkthrough" in pr.markers
        and "recent_review" not in pr.markers
        and not pr.bot_reviews
        and not pr.rate_block_at
        and "skip_review" not in pr.markers
    ):
        pr.errors.append("walkthrough marker but zero parsed review objects — possible parse bug")

    # -- the completeness contract ----------------------------------------- #
    pass_at_head = any((r.get("commit_id") or "") == pr.head for r in pr.passes)
    recent_at_head = pr.head in pr.recent_review_shas

    if pass_at_head or recent_at_head:
        pr.tier = "current"
        if pass_at_head:
            at_head = [r for r in pr.passes if (r.get("commit_id") or "") == pr.head]
            newest = at_head[-1]
            pr.complete_at = parse_ts(newest.get("submitted_at"))
            pr.complete_url = newest.get("html_url") or pr.url
            pr.complete_kind = "review-object"
        else:
            pr.complete_at = pr.recent_review_at
            pr.complete_url = pr.summary_url
            pr.complete_kind = "summary-comment"
    elif pr.passes or pr.recent_review_shas:
        pr.tier = "stale"
        if pr.newest_pass_at:
            pr.complete_at = pr.newest_pass_at
            pr.complete_url = pr.newest_pass.get("html_url") or pr.url
            pr.complete_kind = "review-object"
        else:
            pr.complete_at = pr.recent_review_at
            pr.complete_url = pr.summary_url
            pr.complete_kind = "summary-comment"
    else:
        pr.tier = "never"

    return pr


def reviewed_sha(pr: PR) -> str:
    if pr.tier == "current":
        return pr.head
    if pr.newest_pass_sha:
        return pr.newest_pass_sha
    if pr.recent_review_shas:
        return pr.recent_review_shas[-1]
    return ""


# --------------------------------------------------------------------------- #
# the throttle gate  (SKILL step 2)
# --------------------------------------------------------------------------- #

class Gate:
    def __init__(self):
        self.sources = []            # (when, description)

    def add(self, when, description):
        if when:
            self.sources.append((when, description))

    @property
    def value(self):
        return max((w for w, _ in self.sources), default=None)

    @property
    def reason(self):
        if not self.sources:
            return "no gate source — allowance believed free"
        when, desc = max(self.sources, key=lambda s: s[0])
        return f"{iso(when)} ({desc})"

    def describe(self):
        return [f"{iso(w)}  {d}" for w, d in sorted(self.sources, key=lambda s: s[0], reverse=True)]


def derive_gate(prs, ledger, now) -> Gate:
    gate = Gate()

    # 1. the ledger's window
    tu = parse_ts(ledger.get("throttledUntil"))
    gate.add(tu, "ledger throttledUntil")

    for pr in prs:
        # 2. the newest rate-limit block's reset
        if pr.rate_block_reset and not pr.rate_block_refusal:
            gate.add(pr.rate_block_reset,
                     f"{pr.key} rate-limit block {iso(pr.rate_block_at)} +{pr.rate_block_seconds // 60}min")

        # 3. the newest completed review's attempt + 60min
        if pr.newest_pass_at and pr.newest_pass_sha:
            attempt = pr.attempt_floor(pr.newest_pass_sha)
            if attempt:
                gate.add(attempt + HOUR, f"{pr.key} pass {iso(pr.newest_pass_at)}, attempt {iso(attempt)} +60min")
        elif pr.tier == "current" and pr.complete_kind == "summary-comment" and pr.recent_review_at:
            attempt = pr.attempt_floor(pr.head)
            if attempt:
                gate.add(attempt + HOUR, f"{pr.key} clean pass, attempt {iso(attempt)} +60min")

        # 4. a live review-in-progress marker, capped at 60 minutes
        if pr.in_progress_since and (now - pr.in_progress_since) < MARKER_CAP:
            gate.add(pr.in_progress_since + HOUR, f"{pr.key} review-in-progress since {iso(pr.in_progress_since)}")

        # 5. a failed attempt
        if pr.failure:
            attempt = pr.attempt_floor(pr.head)
            if attempt:
                gate.add(attempt + HOUR, f"{pr.key} failed attempt, attempt {iso(attempt)} +60min")

    return gate


def countdown_assertion(prs):
    """SKILL step 2: markers present but zero countdowns parsed is a bug, never an open slot."""
    markers = [p for p in prs if "rate_limited" in p.markers]
    parsed = [p for p in markers if p.rate_block_seconds is not None]
    refusals = [p for p in markers if p.rate_block_refusal and p.rate_block_seconds is None]
    return len(markers), len(parsed), len(refusals)


# --------------------------------------------------------------------------- #
# enumeration
# --------------------------------------------------------------------------- #

def search_open(cfg):
    args = ["search", "prs", "--state", "open", "--limit", str(cfg["searchLimit"]),
            "--json", "repository,number,title,createdAt,isDraft,url"]
    for owner in cfg["owners"]:
        args += ["--owner", owner]
    rows = json.loads(gh_raw(args))
    truncated = len(rows) >= cfg["searchLimit"]
    return rows, truncated


def search_recent(cfg, since: datetime, limit: int = 60):
    args = ["search", "prs", "--limit", str(limit),
            "--json", "repository,number,state,updatedAt",
            "--updated", ">" + since.strftime("%Y-%m-%dT%H:%M:%S+00:00")]
    for owner in cfg["owners"]:
        args += ["--owner", owner]
    rows = json.loads(gh_raw(args))
    # Silent truncation here hides a spend on a since-merged PR, which is the exact
    # blind spot this sweep exists to cover.
    return rows, len(rows) >= limit


def eligible(cfg, row):
    slug = row["repository"]["nameWithOwner"]
    repo = slug.split("/", 1)[1]
    number = row["number"]
    if repo in cfg["excludeRepos"]:
        return False, "excluded repo"
    if f"{repo}#{number}" in cfg["excludePRs"] or f"{slug}#{number}" in cfg["excludePRs"]:
        return False, "excluded PR"
    if row.get("isDraft") and not cfg["includeDrafts"]:
        return False, "draft"
    return True, ""


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #

def load_ledger(path: Path):
    if not path.exists():
        return {"fired": []}, "ledger missing — treated as empty"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("fired", [])
        return data, ""
    except (json.JSONDecodeError, OSError) as exc:
        return {"fired": []}, f"ledger unparseable ({exc}) — treated as empty"


def save_ledger(path: Path, ledger, retention: int):
    ledger["fired"] = ledger.get("fired", [])[-retention:]
    for entry in ledger["fired"]:
        if entry.get("note"):
            entry["note"] = " ".join(str(entry["note"]).split())[:400]
        # SKILL step 0: drop a baseline once its entry is reconciled.
        if entry.get("outcome") in ("reviewed", "skipped") and "baseline" in entry:
            entry.pop("baseline", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=1), encoding="utf-8")


def in_cooldown(ledger, pr: PR, cooldown: timedelta, now):
    for entry in ledger.get("fired", []):
        if entry.get("repo") == pr.slug and entry.get("pr") == pr.number:
            at = parse_ts(entry.get("at"))
            if at and (now - at) < cooldown:
                return at
    return None


def gave_up(ledger, pr: PR):
    if pr.key in ledger.get("gaveUp", []):
        return True
    return any(e.get("repo") == pr.slug and e.get("pr") == pr.number and e.get("giveUp")
               for e in ledger.get("fired", []))


def mark_refusal(ledger, entry):
    """SKILL step 3: give up on a PR that refuses *twice*, not once.

    One refusal is often situational — a draft, a bot-authored PR CodeRabbit wants
    a manual trigger for. Retiring the PR after a single skip drops it permanently,
    including across later pushes that might well review fine.
    """
    key = f"{entry.get('repo')}#{entry.get('pr')}"

    # Counted OUTSIDE `fired`, which is trimmed to the last `retention` entries
    # fleet-wide. A second refusal arrives at least a cooldown later, by which time
    # the first entry is usually evicted — so a count derived from `fired` resets to
    # 1 forever and the PR is retriggered indefinitely.
    counts = ledger.setdefault("refusals", {})
    prior = int(counts.get(key, 0) or 0)
    # Tolerate a ledger written before this field existed.
    prior = max(prior, sum(
        1 for e in ledger.get("fired", [])
        if e is not entry
        and e.get("repo") == entry.get("repo")
        and e.get("pr") == entry.get("pr")
        and e.get("outcome") == "skipped"
    ))
    counts[key] = prior + 1
    entry["refusals"] = prior + 1
    if entry["refusals"] >= 2:
        entry["giveUp"] = True
        retired = ledger.setdefault("gaveUp", [])
        if key not in retired:
            retired.append(key)          # survives trimming; `fired` does not
    return entry["refusals"]


def baseline_of(pr: PR):
    return {
        "head": pr.head,
        "summaryUpdatedAt": iso(pr.summary_updated_at),
        "reviewObjTotal": len(pr.bot_reviews),
        "passes": len(pr.passes),
        "emptyBodied": pr.empty_bodied,
        "newestPassAt": iso(pr.newest_pass_at),
        "newestPassSha": pr.newest_pass_sha or "",
        "recentReviewHead": pr.recent_review_shas[-1] if pr.recent_review_shas else "",
        "rateLimitBlock": iso(pr.rate_block_at),
    }


# --------------------------------------------------------------------------- #
# fire + poll  (SKILL steps 5 and 6)
# --------------------------------------------------------------------------- #

def evaluate_outcome(pr: PR, baseline, fired_head):
    """Judge against the step-5 baseline, on the step-3 contract, at the head we fired at."""
    base_pass_at = parse_ts(baseline.get("newestPassAt"))
    base_summary_at = parse_ts(baseline.get("summaryUpdatedAt"))

    fresh_passes = [
        r for r in pr.passes
        if (base_pass_at is None or (parse_ts(r.get("submitted_at")) or now_utc()) > base_pass_at)
    ]
    at_fired = [r for r in fresh_passes if (r.get("commit_id") or "") == fired_head]
    if at_fired:
        r = at_fired[-1]
        m = FINDINGS_RE.search(r.get("body") or "")
        return "reviewed", r.get("html_url") or pr.url, "review-object", (int(m.group(1)) if m else None)

    summary_moved = base_summary_at is None or (pr.summary_updated_at and pr.summary_updated_at > base_summary_at)

    if fired_head in pr.recent_review_shas and summary_moved:
        return "reviewed", pr.summary_url or pr.url, "summary-comment", None

    if summary_moved and "skip_review" in pr.markers:
        return "skipped", pr.summary_url or pr.url, "summary-comment", None

    if summary_moved and "rate_limited" in pr.markers and pr.rate_block_names_head and not pr.rate_block_refusal:
        return "throttled", pr.summary_url or pr.url, "summary-comment", None

    return "pending", pr.summary_url or pr.url, "summary-comment", None


def poll(pr_slug, pr_number, baseline, fired_head, trigger_phrase, rounds, interval):
    """SKILL step 6: last act is a fetch, never a sleep."""
    outcome = ("pending", "", "summary-comment", None)
    fresh = None
    for i in range(rounds):
        fresh = classify(pr_slug, pr_number, trigger_phrase)
        outcome = evaluate_outcome(fresh, baseline, fired_head)
        vlog(f"poll {i + 1}/{rounds}: {outcome[0]}")
        if outcome[0] != "pending":
            break
        if i < rounds - 1:
            time.sleep(interval)
    return outcome, fresh


# --------------------------------------------------------------------------- #
# board  (SKILL step 7)
# --------------------------------------------------------------------------- #

def esc(s):
    return html.escape(str(s), quote=True)


def swept_by_sweep(ledger, pr: PR, review_at):
    if not review_at:
        return False
    for entry in ledger.get("fired", []):
        if entry.get("repo") == pr.slug and entry.get("pr") == pr.number:
            at = parse_ts(entry.get("at"))
            if at and at <= review_at and (review_at - at) < timedelta(minutes=90):
                return True
    return False


def board_row(pr: PR, ledger, now, verdicts):
    cls = {"never": "c-crit", "stale": "c-warn", "current": "c-ok"}[pr.tier]
    age = human_delta(now - pr.created_at) if pr.created_at else "?"
    rsha = reviewed_sha(pr)
    if rsha and rsha != pr.head:
        head_cell = f'{esc(pr.short_head)} &larr; <span class="was">{esc(rsha[:8])}</span>'
    elif rsha:
        head_cell = esc(pr.short_head)
    else:
        head_cell = f'{esc(pr.short_head)} &larr; <span class="was">&mdash;</span>'

    findings = str(pr.findings) if pr.findings is not None else '<span class="dim">&mdash;</span>'

    # Re-reviewed — CodeRabbit answered.
    if "in_progress" in pr.markers and pr.in_progress_since and (now - pr.in_progress_since) < MARKER_CAP:
        rev_cell = '<span class="tag run">running</span>'
        if pr.summary_url:
            rev_cell += f'<a class="rev" href="{esc(pr.summary_url)}">summary</a>'
    elif pr.complete_at:
        tag = "tag" if swept_by_sweep(ledger, pr, pr.complete_at) else "tag auto"
        label = "sweep" if tag == "tag" else "auto"
        rev_cell = f'<span class="{tag}">{label} {esc(human_delta(now - pr.complete_at))} ago</span>'
        if pr.complete_url:
            rev_cell += f'<a class="rev" href="{esc(pr.complete_url)}">review</a>'
    else:
        rev_cell = '<span class="dim">none</span>'

    # Throttle notice — two tests: names head AND newer than the newest completion at head.
    notice = '<span class="dim">&mdash;</span>'
    if pr.rate_block_at and pr.rate_block_names_head and not pr.rate_block_refusal:
        newer_than_review = pr.complete_at is None or pr.rate_block_at > pr.complete_at
        if newer_than_review:
            if pr.rate_block_reset and pr.rate_block_reset > now:
                body = f'<span class="hold">window open {esc(human_delta(pr.rate_block_reset - now))}</span>'
            else:
                body = f'<span class="hold">waiting {esc(human_delta(now - pr.rate_block_at))}</span>'
            notice = body + f'<a class="rev" href="{esc(pr.summary_url or pr.url)}">block</a>'
    elif pr.rate_block_refusal and pr.rate_block_names_head:
        notice = f'<span class="hold">refused &middot; {pr.changed_files} files</span>'

    # Sweep verdict — what THIS run decided about the PR, so the board answers
    # "why was it skipped?" without opening the run log.
    v = verdicts.get(pr.key)
    if v:
        kind, txt = v
        if kind == "fired":
            sweep_cell = f'<span class="tag">{esc(txt)}</span>'
        elif kind == "held":
            sweep_cell = f'<span class="hold">{esc(txt)}</span>'
        else:                       # queued / blocked — plain mono text
            sweep_cell = f'<span class="num">{esc(txt)}</span>'
    elif pr.is_complete:
        sweep_cell = '<span class="dim">covers head</span>'
    else:
        sweep_cell = '<span class="dim">&mdash;</span>'

    return (
        "        <tr>\n"
        f'          <td class="st {cls}">{pr.tier}</td>\n'
        f'          <td><a class="pr" href="{esc(pr.url)}">{esc(pr.key)}</a></td>\n'
        f'          <td class="ttl" title="{esc(pr.title)}">{esc(pr.title)}</td>\n'
        f'          <td class="num rt">{esc(age)}</td>\n'
        f'          <td class="num rt">+{pr.additions} &minus;{pr.deletions}</td>\n'
        f'          <td class="num rt">{findings}</td>\n'
        f'          <td class="sha">{head_cell}</td>\n'
        f'          <td class="sep">{rev_cell}</td>\n'
        f'          <td class="sep">{notice}</td>\n'
        f'          <td class="sep">{sweep_cell}</td>\n'
        "        </tr>"
    )


def render_board(cfg, prs, drafts, ledger, gate, gated, decision, now, run_log_href, verdicts):
    tmpl = Path(cfg["boardTemplate"]).read_text(encoding="utf-8")

    unmerged = [p for p in prs if not p.merged]
    order = {"never": 0, "stale": 1, "current": 2}
    unmerged.sort(key=lambda p: (order[p.tier], p.created_at or now))

    n_never = sum(1 for p in unmerged if p.tier == "never")
    n_stale = sum(1 for p in unmerged if p.tier == "stale")
    n_current = sum(1 for p in unmerged if p.tier == "current")
    n_inflight = sum(1 for p in unmerged
                     if "in_progress" in p.markers and p.in_progress_since
                     and (now - p.in_progress_since) < MARKER_CAP)
    n_throttle = sum(1 for p in unmerged
                     if p.rate_block_at and p.rate_block_names_head and not p.rate_block_refusal
                     and (p.complete_at is None or p.rate_block_at > p.complete_at))
    n_swept = sum(1 for p in unmerged if p.complete_at and swept_by_sweep(ledger, p, p.complete_at))

    gate_value = gate.value
    if gated:
        slot_state = "held"
        slot_until = esc(iso(gate_value)) if gate_value else "unknown"
    else:
        slot_state = "open"
        slot_until = "now"

    fired = decision.get("fired")
    if fired:
        fired_line = f"fired {esc(fired['key'])} &rarr; {esc(fired['outcome'])}"
    else:
        fired_line = f"no fire &middot; {esc(decision.get('reason', 'n/a'))}"

    rows = "\n".join(board_row(p, ledger, now, verdicts) for p in unmerged) or \
        '        <tr><td colspan="10" class="dim">no unmerged PRs</td></tr>'

    foot_rows = []
    for d in drafts:
        foot_rows.append(
            f'      <div class="mono">draft</div><div><a href="{esc(d["url"])}">{esc(d["key"])}</a> '
            f'&mdash; {esc(d["title"])}</div>'
        )
    for entry in reversed(ledger.get("fired", [])[-6:]):
        key = f"{entry.get('repo')}#{entry.get('pr')}"
        if any(p.key == key for p in unmerged):
            continue
        url = entry.get("reviewUrl") or f"https://github.com/{entry.get('repo')}/pull/{entry.get('pr')}"
        foot_rows.append(
            f'      <div class="mono">swept</div><div><a href="{esc(url)}">{esc(key)}</a> '
            f'&mdash; {esc(entry.get("outcome", "?"))} at {esc(entry.get("at", "?"))}</div>'
        )
    foot_rows.append(
        f'      <div class="mono">log</div><div><a href="{esc(run_log_href)}">run log</a> '
        f'&mdash; the last runs, newest first</div>'
    )

    counts = {}
    for entry in ledger.get("fired", []):
        counts[entry.get("outcome", "?")] = counts.get(entry.get("outcome", "?"), 0) + 1
    tally = ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))

    run_note = f"Gate {esc(gate.reason)}. {esc(decision.get('note', ''))} Last {len(ledger.get('fired', []))} fires: {esc(tally)}."

    filled = tmpl
    for key, value in {
        "OWNER": ", ".join(cfg["owners"]),
        "N_UNMERGED": str(len(unmerged)),
        "N_SWEPT": str(n_swept),
        "N_INFLIGHT": str(n_inflight),
        "N_THROTTLE": str(n_throttle),
        "N_CURRENT": str(n_current),
        "N_STALE": str(n_stale),
        "N_NEVER": str(n_never),
        "SLOT_STATE": slot_state,
        "SLOT_UNTIL": slot_until,
        "STAMP": iso(now),
        "FIRED_LINE": fired_line,
        "ROWS": rows,
        "RUN_NOTE": run_note,
        "FOOTNOTE_SUMMARY": f"{len(drafts)} draft, {max(0, len(foot_rows) - len(drafts) - 1)} swept-then-merged",
        "FOOTNOTE_ROWS": "\n".join(foot_rows),
    }.items():
        filled = filled.replace("{{" + key + "}}", value)

    # The template documents its row shape in a trailing HTML comment; strip it.
    i = filled.find("<!-- ROW TEMPLATE")
    if i >= 0:
        filled = filled[:i].rstrip() + "\n"

    head, sep, body = filled.partition('<div class="wrap">')
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{head}</head>\n<body>\n{sep}{body}\n</body>\n</html>\n"
    )


RUNLOG_CSS = """
:root{--ground:#F6F8F6;--surface:#FFF;--ink:#14201C;--muted:#5B6B64;--faint:#8A9A92;
--line:#DCE4DF;--accent:#0C6B5B;--ok:#1E7A4C;--warn:#96600C;--crit:#9E3444;--hold:#2A5C7C}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--ground:#0D1311;--surface:#121A17;
--ink:#E7EDEA;--muted:#94A49C;--faint:#6F7F78;--line:#24302C;--accent:#52D3B6;--ok:#55C08A;
--warn:#D7A445;--crit:#E4808C;--hold:#7FB6D8}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font:13px/1.45 "IBM Plex Sans",ui-sans-serif,system-ui,"Segoe UI",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:18px 20px 40px;display:flex;flex-direction:column;gap:11px}
.bar{display:flex;flex-wrap:wrap;gap:4px 16px;font:600 11.5px/1.4 "IBM Plex Mono",ui-monospace,monospace;
letter-spacing:.09em;text-transform:uppercase;color:var(--accent);padding-bottom:9px;border-bottom:1px solid var(--line)}
.bar span{color:var(--faint);font-weight:400;letter-spacing:0;text-transform:none}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;background:var(--surface);font-variant-numeric:tabular-nums}
th{position:sticky;top:0;background:var(--ground);text-align:left;font:600 10px/1.4 "IBM Plex Mono",ui-monospace,monospace;
letter-spacing:.09em;text-transform:uppercase;color:var(--faint);padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:5px 10px;vertical-align:baseline;border-bottom:1px solid var(--line)}
tr:nth-child(even){background:rgba(128,128,128,.045)}
td.mono{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11px;color:var(--muted);white-space:nowrap}
td.note{color:var(--muted);font-size:12px}
.d{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11px;font-weight:500}
.d-fired{color:var(--ok)}.d-gated{color:var(--hold)}.d-idle{color:var(--faint)}.d-error{color:var(--crit)}
a{color:var(--accent)}
details.audit summary{cursor:pointer;list-style:none;color:var(--muted)}
details.audit summary::-webkit-details-marker{display:none}
details.audit summary::before{content:"+ ";font-family:"IBM Plex Mono",ui-monospace,monospace;color:var(--faint)}
details.audit[open] summary::before{content:"\\2212 "}
ul.audit{margin:5px 0 2px;padding-left:16px}
ul.audit li{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11px;color:var(--muted);margin:2px 0}
ul.audit li.prob{color:var(--crit)}
"""


def render_run_log(cfg, runs, board_href):
    rows = []
    for r in runs[:120]:
        d = r.get("decision", "idle")
        cls = {"fired": "d-fired", "gated": "d-gated", "idle": "d-idle"}.get(d, "d-error")
        target = r.get("target") or ""
        if target and r.get("targetUrl"):
            target = f'<a href="{esc(r["targetUrl"])}">{esc(target)}</a>'
        else:
            target = esc(target) or "&mdash;"
        # The audit trail: why every candidate was passed over, and what went wrong.
        queue = r.get("queue") or []
        probs = r.get("problems") or []
        note_cell = esc(r.get("note", ""))
        if queue or probs:
            items = "".join(f"<li>{esc(q)}</li>" for q in queue)
            items += "".join(f'<li class="prob">{esc(p)}</li>' for p in probs)
            note_cell = (f'<details class="audit"><summary>{note_cell or "detail"}</summary>'
                         f'<ul class="audit">{items}</ul></details>')
        rows.append(
            "<tr>"
            f'<td class="mono">{esc(r.get("at", ""))}</td>'
            f'<td class="d {cls}">{esc(d)}</td>'
            f'<td class="mono">{target}</td>'
            f'<td class="mono">{esc(r.get("outcome", "")) or "&mdash;"}</td>'
            f'<td class="mono">{esc(r.get("gate", "")) or "&mdash;"}</td>'
            f'<td class="note">{note_cell}</td>'
            "</tr>"
        )
    body = "\n".join(rows) or '<tr><td colspan="6">no runs recorded</td></tr>'
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CodeRabbit Sweep Run Log</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>{RUNLOG_CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="bar">CodeRabbit sweep &middot; run log
    <span>{esc(", ".join(cfg["owners"]))}</span>
    <span>{len(runs)} runs recorded</span>
    <span><a href="{esc(board_href)}">back to the board</a></span>
  </div>
  <div class="scroll">
    <table>
      <thead><tr><th>When (UTC)</th><th>Decision</th><th>Target</th><th>Outcome</th><th>Gate</th><th>Note</th></tr></thead>
      <tbody>
{body}
      </tbody>
    </table>
  </div>
</div>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def append_report(path: Path, block: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(block)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

DEFAULTS = {
    "owners": [],
    "excludeRepos": [],
    "excludePRs": [],
    "includeDrafts": False,
    "triggerPhrase": "@coderabbitai full review",
    "cooldownMinutes": 90,
    "searchLimit": 1000,
    "retention": 12,
    "oversizeFiles": 300,
    "pollRounds": 11,
    "pollInterval": 30,
    "stateDir": ".",
    "ledger": "ledger.json",
    "boardTemplate": "board-template.html",
    "board": "board.html",
    "runLog": "runs.html",
    "runsData": "runs.json",
    "reportsDir": "reports",
}


def load_config(path: Path):
    cfg = dict(DEFAULTS)
    cfg.update(json.loads(path.read_text(encoding="utf-8")))
    base = Path(cfg["stateDir"])
    if not base.is_absolute():
        base = (path.parent / base).resolve()
    cfg["stateDir"] = str(base)
    for key in ("ledger", "boardTemplate", "board", "runLog", "runsData", "reportsDir"):
        p = Path(cfg[key])
        cfg[key] = str(p if p.is_absolute() else base / p)
    if not cfg["owners"]:
        raise SystemExit("config error: 'owners' must name at least one GitHub owner")
    cfg["excludeRepos"] = set(cfg["excludeRepos"])
    cfg["excludePRs"] = set(cfg["excludePRs"])
    return cfg


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description="CodeRabbit re-review sweep")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    ap.add_argument("--dry-run", action="store_true", help="classify and render, never fire, never write the ledger")
    ap.add_argument("--no-poll", action="store_true", help="fire but skip the 5-minute confirmation poll")
    ap.add_argument("--open", dest="open_board", action="store_true", help="open the board when done")
    ap.add_argument("--only", metavar="OWNER/REPO#N",
                    help="fire at this PR instead of the ranked pick. Every other guard still "
                         "applies: the throttle gate, fail-closed, cooldown and give-up.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    cfg = load_config(Path(args.config))
    open_log(Path(cfg["stateDir"]))
    log(f"--- run start (dry-run={args.dry_run}) ---")

    if not args.dry_run:
        _lock, held_since = acquire_lock(Path(cfg["stateDir"]))
        if _lock is None:
            log(f"another sweep has been running since {iso(held_since)} — exiting without firing")
            print("skipped: another sweep is already running")
            return 0

    now = now_utc()
    cooldown = timedelta(minutes=cfg["cooldownMinutes"])
    trigger = cfg["triggerPhrase"]
    problems = []

    ledger, ledger_note = load_ledger(Path(cfg["ledger"]))
    if ledger_note:
        problems.append(ledger_note)
        log(ledger_note)

    # ---- step 1: enumerate ------------------------------------------------ #
    log(f"enumerating open PRs for {', '.join(cfg['owners'])}")
    # Firing needs a COMPLETE picture of the fleet. Anything that leaves the picture
    # partial goes in here, and the run refuses to fire. Erring toward "a slot was
    # spent" costs one idle tick; erring the other way burns the trigger.
    fail_closed = []

    rows, truncated = search_open(cfg)
    if truncated:
        problems.append(f"search hit the {cfg['searchLimit']} limit — the fleet list is truncated")
        fail_closed.append("open-PR search truncated")
    log(f"{len(rows)} open PRs found")

    drafts, dropped = [], []
    targets = []
    for row in rows:
        slug = row["repository"]["nameWithOwner"]
        ok, why = eligible(cfg, row)
        if not ok:
            entry = {"key": f"{slug}#{row['number']}", "title": row.get("title", ""),
                     "url": row.get("url", ""), "why": why}
            (drafts if why == "draft" else dropped).append(entry)
            continue
        targets.append((slug, row["number"]))

    prs = []
    for slug, number in targets:
        try:
            pr = classify(slug, number, trigger)
            prs.append(pr)
            vlog(f"{pr.key}: {pr.tier} head={pr.short_head} passes={len(pr.passes)} "
                 f"empty={pr.empty_bodied} markers={sorted(pr.markers)}")
            problems.extend(f"{pr.key}: {e}" for e in pr.errors)
        except (GhError, json.JSONDecodeError) as exc:
            problems.append(f"{slug}#{number}: classification failed — {exc}")
            # An unclassified PR contributes no rate-limit block, no in-progress marker
            # and no pass to derive_gate, so the gate silently reads earlier than it is.
            fail_closed.append(f"{slug}#{number} did not classify")
            log(f"ERROR {slug}#{number}: {exc}")

    n_markers, n_parsed, n_refusals = countdown_assertion(prs)
    if n_markers and n_parsed + n_refusals < n_markers:
        problems.append(f"countdown assertion: {n_markers} rate-limit markers, only "
                        f"{n_parsed} countdowns + {n_refusals} refusals parsed — treat as a parse bug")
        fail_closed.append("countdown assertion failed")

    # ---- step 6 (deferred): reconcile the previous run's entries ---------- #
    # An entry younger than a full poll may belong to a sweep that is still running.
    # Scoring that "lost" turns a healthy in-flight fire into a fake failure.
    settle = timedelta(seconds=max(600, cfg["pollRounds"] * cfg["pollInterval"] + 300))
    for entry in ledger.get("fired", []):
        if entry.get("outcome") not in ("pending", "unknown"):
            continue
        at = parse_ts(entry.get("at"))
        if at and (now - at) < settle:
            vlog(f"skipping reconcile of {entry.get('repo')}#{entry.get('pr')} — may still be in flight")
            continue
        key = f"{entry.get('repo')}#{entry.get('pr')}"
        base = entry.get("baseline")
        if not base:
            # Without the step-5 baseline there is nothing to judge "new" against, and
            # guessing off live state upgrades a lost trigger into a fake success.
            entry.setdefault("reconciledOutcome", "unresolved — no baseline recorded")
            continue
        try:
            live = next((p for p in prs if p.key == key), None)
            if live is None:
                live = classify(entry["repo"], entry["pr"], trigger)
            outcome, url, kind, findings = evaluate_outcome(live, base, base.get("head") or live.head)
            if outcome == "pending":
                entry["reconciledOutcome"] = "lost"        # SKILL step 6
            else:
                was = entry.get("outcome")
                entry["outcome"] = outcome
                entry["reviewUrl"] = url
                entry["reviewUrlKind"] = kind
                if findings is not None:
                    entry["findings"] = findings
                if outcome == "skipped":
                    n = mark_refusal(ledger, entry)
                    log(f"{key}: refusal {n}" + (" — giving up on it" if n >= 2 else ""))
                # The note was written by the poll and still says what the poll saw.
                entry["note"] = f"{entry.get('note', '')} Reconciled {iso(now)}: {was} -> {outcome}.".strip()
            log(f"reconciled {key}: {entry.get('outcome')} / {entry.get('reconciledOutcome', '-')}")
        except (GhError, json.JSONDecodeError) as exc:
            problems.append(f"reconcile {key} failed — {exc}")

    # ---- step 2: the throttle gate --------------------------------------- #
    gate = derive_gate(prs, ledger, now)
    gate_value = gate.value

    closed_checked = []
    if gate_value is None or now >= gate_value + FIRE_MARGIN:
        # SKILL step 2: before firing on an expired window, classify recently-touched
        # PRs including closed ones — a spend on a since-merged PR is otherwise invisible.
        log("open-fleet gate expired — running the closed-PR sweep")
        try:
            recent, recent_truncated = search_recent(cfg, now - timedelta(minutes=90))
            if recent_truncated:
                problems.append("closed-PR search truncated — a hidden spend may be unseen")
                fail_closed.append("closed-PR search truncated")
            known = {p.key for p in prs}
            for row in recent:
                slug = row["repository"]["nameWithOwner"]
                key = f"{slug}#{row['number']}"
                if key in known or slug.split("/", 1)[1] in cfg["excludeRepos"]:
                    continue
                try:
                    extra = classify(slug, row["number"], trigger)
                    closed_checked.append(extra)
                    vlog(f"closed sweep {extra.key}: {extra.tier} merged={extra.merged}")
                except (GhError, json.JSONDecodeError) as exc:
                    problems.append(f"closed sweep {key} failed — {exc}")
                    fail_closed.append(f"{key} did not classify in the closed sweep")
        except (GhError, json.JSONDecodeError) as exc:
            problems.append(f"closed-PR search failed — {exc}")
            fail_closed.append("closed-PR search failed")
        gate = derive_gate(prs + closed_checked, ledger, now)
        gate_value = gate.value

    gated = gate_value is not None and now_utc() < gate_value + FIRE_MARGIN
    if fail_closed:
        gated = True
        log("FAIL-CLOSED, not firing: " + "; ".join(fail_closed[:4]))
    log(f"gate {iso(gate_value) or 'none'} — {'GATED' if gated else 'open'}")
    for line in gate.describe()[:6]:
        vlog(line)

    # ---- step 4: pick one ------------------------------------------------- #
    incomplete = [p for p in prs if not p.is_complete]
    candidates, blocked = [], []
    for pr in incomplete:
        if gave_up(ledger, pr):
            blocked.append((pr, "give-up flag set after two refusals"))
            continue
        cd = in_cooldown(ledger, pr, cooldown, now)
        if cd:
            blocked.append((pr, f"in cooldown until {iso(cd + cooldown)}"))
            continue
        candidates.append(pr)

    # --only narrows the queue to one PR. It overrides the RANKING, never a guard —
    # the gate, fail-closed, cooldown and give-up all still decide whether it fires.
    only_reason = ""
    if args.only:
        want = args.only.strip()
        picked = [p for p in candidates if p.key == want]
        if not picked:
            why = "not a candidate"
            named = next((p for p in prs if p.key == want), None)
            if named is None:
                why = "not in the open fleet this run"
            elif named.is_complete:
                why = "already covers its head"
            elif gave_up(ledger, named):
                why = "carries the give-up flag"
            elif in_cooldown(ledger, named, cooldown, now):
                why = "inside its cooldown"
            problems.append(f"--only {want}: {why} — nothing fired")
            log(f"--only {want}: {why}")
            only_reason = f"--only {want}: {why}"
        candidates = picked

    tier_rank = {"never": 0, "stale": 1}
    candidates.sort(key=lambda p: (
        tier_rank[p.tier],
        1 if p.changed_files > cfg["oversizeFiles"] else 0,   # oversize ranks last within its tier
        p.created_at or now,
    ))

    decision = {"note": "", "reason": ""}
    fired_entry = None

    if gated:
        if fail_closed:
            # Say which it was. "gated until <blank>" would read as a clean throttle.
            decision["reason"] = "fail-closed: " + "; ".join(fail_closed[:3])
            decision["note"] = (f"Incomplete fleet picture, nothing fired; "
                                f"{len(candidates)} candidate(s) waiting.")
        else:
            decision["reason"] = f"gated until {iso(gate_value)}"
            decision["note"] = f"Gated; {len(candidates)} candidate(s) waiting."
        log(f"nothing fired — {decision['reason']}")
    elif not candidates:
        if only_reason:
            decision["reason"] = only_reason
        elif incomplete:
            decision["reason"] = "every candidate is in cooldown or gave up"
        else:
            decision["reason"] = "queue empty — every PR covers head"
        decision["note"] = decision["reason"].capitalize() + "."
        log(f"no fire: {decision['reason']}")
    else:
        # ---- re-scan immediately before firing (SKILL step 2) ------------- #
        target = candidates[0]
        try:
            rescan_rows, rescan_truncated = search_open(cfg)
            if rescan_truncated:
                problems.append("pre-fire re-scan truncated — the fleet list is incomplete")
                fail_closed.append("pre-fire re-scan truncated")
            newest = None
            for row in rescan_rows:
                slug = row["repository"]["nameWithOwner"]
                key = f"{slug}#{row['number']}"
                ok, _why = eligible(cfg, row)
                if not ok:
                    continue
                if key not in {p.key for p in prs}:
                    created = parse_ts(row["createdAt"])
                    if newest is None or (created and created > newest[0]):
                        newest = (created, slug, row["number"])
            if newest:
                log(f"re-scan found a new PR {newest[1]}#{newest[2]} — classifying")
                fresh = classify(newest[1], newest[2], trigger)
                prs.append(fresh)
                gate = derive_gate(prs + closed_checked, ledger, now_utc())
                gate_value = gate.value
        except (GhError, json.JSONDecodeError) as exc:
            problems.append(f"pre-fire re-scan failed — {exc}")
            fail_closed.append("pre-fire re-scan failed")

        if fail_closed:
            gated = True
            decision["reason"] = "fail-closed: " + "; ".join(fail_closed[:3])
            decision["note"] = "Incomplete fleet picture; nothing fired."
            log(decision["reason"])
        elif gate_value is not None and now_utc() < gate_value + FIRE_MARGIN:
            gated = True
            decision["reason"] = f"re-scan moved the gate to {iso(gate_value)}"
            decision["note"] = "Gate arrived during the run; nothing fired."
            log(decision["reason"])
        elif args.dry_run:
            decision["reason"] = f"dry run — would fire {target.key}"
            decision["note"] = f"Dry run. Would fire {target.key} ({target.tier})."
            log(decision["reason"])
        else:
            # ---- step 5: reserve, then fire ------------------------------- #
            fire_at = now_utc()
            fired_entry = {
                "repo": target.slug,
                "pr": target.number,
                "at": iso(fire_at),
                "outcome": "unknown",
                "baseline": baseline_of(target),
            }
            ledger["fired"].append(fired_entry)
            ledger["throttledUntil"] = iso(fire_at + HOUR)
            save_ledger(Path(cfg["ledger"]), ledger, cfg["retention"])
            log(f"reserved {target.key}; posting the trigger")

            try:
                gh_raw(["pr", "comment", str(target.number), "--repo", target.slug, "--body", trigger])
                posted_at = now_utc()
                ledger["throttledUntil"] = iso(posted_at + HOUR)
                log(f"fired {target.key} at {iso(posted_at)}")
            except GhError as exc:
                fired_entry["outcome"] = "error"
                fired_entry["note"] = f"trigger post failed: {exc}"
                problems.append(f"trigger post failed on {target.key} — {exc}")
                posted_at = None

            if posted_at is not None:
                rounds = 1 if args.no_poll else cfg["pollRounds"]
                outcome, url, kind, findings = ("pending", target.summary_url, "summary-comment", None)
                try:
                    (outcome, url, kind, findings), fresh = poll(
                        target.slug, target.number, fired_entry["baseline"], target.head,
                        trigger, rounds, cfg["pollInterval"])
                    if fresh is not None:
                        prs = [fresh if p.key == fresh.key else p for p in prs]
                        target = fresh
                except (GhError, json.JSONDecodeError) as exc:
                    problems.append(f"poll of {target.key} failed — {exc}")

                fired_entry["outcome"] = outcome
                fired_entry["reviewUrl"] = url or target.url
                fired_entry["reviewUrlKind"] = kind
                if findings is not None:
                    fired_entry["findings"] = findings
                if outcome == "skipped":
                    n = mark_refusal(ledger, fired_entry)
                    log(f"{target.key}: refusal {n}" + (" — giving up on it" if n >= 2 else ""))
                if outcome == "throttled" and target.rate_block_reset:
                    # SKILL step 6: let the vendor's number overwrite our window.
                    ledger["throttledUntil"] = iso(target.rate_block_reset)
                fired_entry["note"] = (
                    f"reserved {iso(fire_at)}, trigger posted {iso(posted_at)}; "
                    f"fired head {target.head[:8]}; outcome {outcome}"
                )
                if outcome in ("reviewed", "skipped"):
                    fired_entry.pop("baseline", None)

                decision["fired"] = {"key": target.key, "outcome": outcome, "url": url}
                decision["reason"] = f"fired {target.key} -> {outcome}"
                decision["note"] = (
                    f"Fired {target.key} ({target.tier}, opened {iso(target.created_at)}, "
                    f"{target.changed_files} files) -> {outcome}"
                    + (f", {findings} findings" if findings is not None else "") + "."
                )
                log(decision["reason"])

        for pr, why in blocked:
            vlog(f"held back {pr.key}: {why}")

    # ---- audit: one sweep verdict per incomplete PR ----------------------- #
    # The board and the run log both read this, so a human can see why every
    # candidate was passed over without reconstructing the ranking by hand.
    verdicts = {}
    fired_key = decision.get("fired", {}).get("key", "")
    for pos, p in enumerate(candidates, start=1):
        if p.key == fired_key:
            verdicts[p.key] = ("fired", f"fired now -> {decision['fired'].get('outcome', '?')}")
        elif not gated and args.dry_run and pos == 1:
            verdicts[p.key] = ("queued", "#1 in queue (dry run)")
        elif gated:
            if fail_closed:
                verdicts[p.key] = ("held", f"#{pos} held: fail-closed")
            elif gate_value and gate_value + FIRE_MARGIN > now_utc():
                verdicts[p.key] = ("held", f"#{pos} held: gate opens in {human_delta(gate_value + FIRE_MARGIN - now_utc())}")
            else:
                verdicts[p.key] = ("held", f"#{pos} held: gated")
        else:
            verdicts[p.key] = ("queued", f"#{pos} in queue")
        if p.changed_files > cfg["oversizeFiles"]:
            kind, txt = verdicts[p.key]
            verdicts[p.key] = (kind, f"{txt} - oversize {p.changed_files} files")
    for p, why in blocked:
        verdicts[p.key] = ("blocked", why)
    for p in prs:
        if not p.is_complete and p.key not in verdicts:
            verdicts[p.key] = ("queued", "not ranked this run")

    # ---- step 7: ledger, board, report ----------------------------------- #
    board_path = Path(cfg["board"])
    runlog_path = Path(cfg["runLog"])
    if args.dry_run:
        board_path = board_path.with_name(board_path.stem + "-dryrun" + board_path.suffix)
        runlog_path = runlog_path.with_name(runlog_path.stem + "-dryrun" + runlog_path.suffix)

    runs_path = Path(cfg["runsData"])
    runs = []
    if runs_path.exists():
        try:
            runs = json.loads(runs_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            runs = []

    run_record = {
        "at": iso(now),
        "decision": "fired" if decision.get("fired") else ("gated" if gated else "idle"),
        "target": decision.get("fired", {}).get("key", ""),
        "targetUrl": decision.get("fired", {}).get("url", ""),
        "outcome": decision.get("fired", {}).get("outcome", ""),
        "gate": iso(gate_value),
        "note": " ".join((decision.get("note", "") + " " + "; ".join(problems)).split())[:600],
        # Per-PR audit trail — rendered as the expandable detail on the run log.
        "queue": [f"{p.key}: {verdicts[p.key][1]} ({p.tier}, opened {iso(p.created_at)})"
                  for p in prs if p.key in verdicts],
        "problems": [str(p)[:300] for p in problems[:10]],
    }
    if problems and run_record["decision"] == "idle":
        run_record["decision"] = "error"
    runs.insert(0, run_record)
    runs = runs[:500]

    ledger["lastRun"] = {
        "at": iso(now),
        "decision": decision.get("reason", ""),
        "gate": iso(gate_value),
        "gateSource": gate.reason,
        "note": run_record["note"],
    }
    ledger["boardPath"] = str(board_path)

    if not args.dry_run:
        save_ledger(Path(cfg["ledger"]), ledger, cfg["retention"])
        runs_path.parent.mkdir(parents=True, exist_ok=True)
        runs_path.write_text(json.dumps(runs, indent=1), encoding="utf-8")

    try:
        board_html = render_board(cfg, prs, drafts, ledger, gate, gated, decision, now_utc(),
                                  runlog_path.name, verdicts)
        board_path.parent.mkdir(parents=True, exist_ok=True)
        board_path.write_text(board_html, encoding="utf-8")
        runlog_path.write_text(render_run_log(cfg, runs, board_path.name), encoding="utf-8")
        log(f"board  -> {board_path}")
        log(f"runlog -> {runlog_path}")
    except (OSError, KeyError) as exc:
        problems.append(f"board render failed — {exc}")
        log(f"ERROR board render failed: {exc}")
        # run_record was persisted before this handler ran, so a render failure
        # would otherwise leave no trace in runs.json — the one place a human
        # looks to learn why a board is missing or stale.
        run_record["problems"] = [str(p)[:300] for p in problems[:10]]
        if run_record["decision"] == "idle":
            run_record["decision"] = "error"
        if not args.dry_run:
            try:
                runs_path.write_text(json.dumps(runs, indent=1), encoding="utf-8")
            except OSError as exc2:
                log(f"ERROR runs.json rewrite failed: {exc2}")

    report_path = Path(cfg["reportsDir"]) / f"{now.strftime('%Y-%m-%d')}.md"
    lines = [
        f"\n## {iso(now)}",
        "",
        f"- gate: {iso(gate_value) or 'none'} — {'GATED' if gated else 'open'} ({gate.reason})",
        f"- decision: {decision.get('reason', 'n/a')}",
    ]
    if decision.get("fired"):
        lines.append(f"- result: {decision['fired']['outcome']} — {decision['fired'].get('url', '')}")
    lines.append(f"- fleet: {len(prs)} classified, "
                 f"{sum(1 for p in prs if p.tier == 'never')} never / "
                 f"{sum(1 for p in prs if p.tier == 'stale')} stale / "
                 f"{sum(1 for p in prs if p.tier == 'current')} current, "
                 f"{len(drafts)} draft excluded")
    for pr in candidates[1:]:
        lines.append(f"  - not fired: {pr.key} ({pr.tier}, opened {iso(pr.created_at)}, {pr.changed_files} files)")
    for pr, why in blocked:
        lines.append(f"  - held back: {pr.key} — {why}")
    for p in problems:
        lines.append(f"  - PROBLEM: {p}")
    lines.append(f"- board: {board_path}")
    lines.append("")
    if not args.dry_run:
        append_report(report_path, "\n".join(lines))

    print()
    print(f"gate      {iso(gate_value) or 'none'} ({'gated' if gated else 'open'})")
    print(f"decision  {decision.get('reason', 'n/a')}")
    print(f"board     {board_path.as_uri()}")
    if problems:
        print(f"problems  {len(problems)} — see the report")
        for p in problems:
            print(f"          - {p}")

    if args.open_board:
        webbrowser.open(board_path.as_uri())

    return 0


def guarded():
    """A scheduled run dies silently unless the crash is written down somewhere."""
    try:
        return main()
    except SystemExit:
        raise
    except BaseException:
        import traceback
        tb = traceback.format_exc()
        sys.stderr.write(tb)
        target = Path(__file__).parent
        for i, a in enumerate(sys.argv):
            if a == "--config" and i + 1 < len(sys.argv):
                try:
                    target = Path(load_config(Path(sys.argv[i + 1]))["stateDir"])
                except Exception:
                    pass
        try:
            target.mkdir(parents=True, exist_ok=True)
            with (target / "sweep.log").open("a", encoding="utf-8") as fh:
                fh.write(f"[{iso(now_utc())}] CRASHED\n{tb}\n")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(guarded())
