"""The job-reports endpoint (tools/job-reports), from this PC: register jobs, report, pull runs.

Every job, wherever it runs, reports to that one endpoint. The dashboard pulls what is new into
its local SQLite file (collect_into), so the Routines page keeps working the same way, and still
shows the last known state when the endpoint can't be reached.

Config: `reportUrl` (the Worker's https address) and `reportToken` (its ADMIN_TOKEN). The token
can also come from the NF_ADMIN_TOKEN variable, so it never has to sit in a file.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

import routines

TIMEOUT = 30
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
MAX_PAGES = 50       # 500 runs a page; anything past this waits for the next collect


class EndpointError(RuntimeError):
    pass


def settings(cfg: dict) -> tuple[str, str] | None:
    url = (cfg.get("reportUrl") or "").rstrip("/")
    token = cfg.get("reportToken") or os.environ.get("NF_ADMIN_TOKEN") or ""
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    # The admin token rides on every call, so plain http is allowed only to this machine.
    if not parts.hostname or not (parts.scheme == "https"
                                  or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS)):
        raise EndpointError(f"reportUrl must be https (or localhost for testing): {url}")
    if not token:
        raise EndpointError("reportUrl is set but there is no token: set reportToken or NF_ADMIN_TOKEN")
    return url, token


def call(url: str, token: str, method: str, path: str, body=None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "User-Agent": "nightforge-dashboard"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return json.loads(res.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        # The Worker answers errors as {"ok": false, "error": "..."}; pass its words on.
        try:
            msg = json.loads(exc.read().decode("utf-8")).get("error")
        except (ValueError, OSError):
            msg = None
        raise EndpointError(f"{method} {path}: HTTP {exc.code}" + (f": {msg}" if msg else "")) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise EndpointError(f"{method} {path}: {exc}") from None


def kind_of(job: dict) -> str:
    return job.get("runsOn") or ("" if job.get("registered") else "not registered")


def collect_into(store, url: str, token: str) -> int:
    """Pull jobs and every run received since the last pull. The endpoint is where jobs are
    registered, so its fields overwrite the local copy."""
    key, known_key = f"endpoint:{url}", f"endpoint-jobs:{url}"
    after = int(store.cursor(key) or 0)
    known = set(json.loads(store.cursor(known_key) or "[]"))
    added, seen = 0, None
    for _ in range(MAX_PAGES):
        page = call(url, token, "GET", f"/sync?after={after}")
        # Every page carries the endpoint's whole job list, so one page is enough to know it.
        seen = {job["id"] for job in page.get("jobs", [])}
        for job in page.get("jobs", []):
            store.set_bot(job["id"], name=job.get("name") or job["id"], kind=kind_of(job),
                          schedule=job.get("schedule") or "", every_minutes=job.get("every"),
                          grace_minutes=job.get("grace"), description=job.get("does") or "",
                          url=job.get("url") or "", paused=bool(job.get("paused")))
        for run in page.get("runs", []):
            try:
                added += store.add_run({**run, "bot": run["job"]}, "endpoint")
            except routines.ReportError:
                pass   # the Worker already checked it; a run this side can't store is skipped, not fatal
        after = page.get("next", after)
        # Save as we go, so a failure on page 3 doesn't re-read pages 1 and 2.
        store.set_cursor(key, str(after))
        if not page.get("more"):
            break
    if seen is not None:
        # A job deleted on the endpoint goes here too. Only jobs this collector put here are
        # removed, so rows from other sources are left alone.
        for gone in known - seen:
            store.remove_bot(gone)
        store.set_cursor(known_key, json.dumps(sorted(seen)))
    return added


def report(url: str, token: str, report_body: dict) -> dict:
    return call(url, token, "POST", "/report", report_body)


def register(url: str, token: str, job_id: str, fields: dict) -> dict:
    return call(url, token, "PUT", "/jobs/" + urllib.parse.quote(job_id, safe=""), fields)


def remove(url: str, token: str, job_id: str) -> dict:
    return call(url, token, "DELETE", "/jobs/" + urllib.parse.quote(job_id, safe=""))


def jobs(url: str, token: str) -> list[dict]:
    return call(url, token, "GET", "/jobs").get("jobs", [])
