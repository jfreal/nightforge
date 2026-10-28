"""Screenery side of the PR dashboard: the screenshots a PR's CI build pushed.

Screenery keeps each PR's screenshots on a channel named `pr-<number>` and serves them,
compared with the default branch, as one JSON gallery:

    GET https://app.screenery.dev/v1/projects/<org>/<project>/channels/pr-<n>/gallery

Public projects need no token. A push into a project that is out of channels makes a
build but no `pr-<n>` channel (and so no PR comment either). A channel miss therefore
falls back to the PR's newest build, which has a gallery of its own:

    GET https://app.screenery.dev/v1/projects/<org>/<project>/builds?pr=<n>   (newest first)
    GET https://app.screenery.dev/v1/builds/<id>/gallery

A private project, or a PR whose CI pushed nothing, has neither, which the dashboard
shows as "no screenshots".
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://app.screenery.dev"
TTL = 600          # a PR's screenshots change only when its CI runs again
MISS_TTL = 60      # but a miss may be a CI run still uploading, so ask again soon
NO_BUILD_TTL = 300   # no channel and no build at all: most likely a repo without Screenery
MAX_PAGES = 20     # a safety stop; hitting it marks the gallery incomplete
BUILD_ID = re.compile(r"b_[0-9A-Za-z]+")
# Screenery answers 60 anonymous reads a minute per IP, then 429. Every PR's gallery is one
# to three reads, so they go out one at a time with a gap that stays under that ceiling.
MIN_GAP = 1.5
MAX_RETRY_AFTER = 10
# Changed shots first: they are the ones worth a look before merging.
ORDER = {"changed": 0, "added": 1, "removed": 2, "unchanged": 3}

_cache: dict = {}
_lock = threading.Lock()
_pace = threading.Lock()
_next_call = 0.0


def _get(url) -> dict:
    """One Screenery read, paced, with one retry when Screenery says to wait."""
    global _next_call
    req = urllib.request.Request(url, headers={"User-Agent": "nightforge-pr-dashboard", "Accept": "application/json"})
    with _pace:
        for attempt in (1, 2):
            wait = _next_call - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _next_call = time.monotonic() + MIN_GAP
            try:
                with urllib.request.urlopen(req, timeout=15) as res:
                    return json.loads(res.read())
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or attempt == 2:
                    raise
                try:
                    after = float(exc.headers.get("Retry-After") or MIN_GAP)
                except ValueError:
                    after = MIN_GAP
                _next_call = time.monotonic() + min(max(after, MIN_GAP), MAX_RETRY_AFTER)


def _image(side):
    if not side:
        return None
    return {"url": side["url"], "width": side.get("width"), "height": side.get("height")}


def trim(doc) -> dict:
    """Keep what the gallery draws; drop hashes, flake history and embed links."""
    assets = []
    for group in doc.get("groups", []):
        for a in group.get("assets", []):
            if a.get("ignored"):
                continue
            assets.append({"name": a["name"], "group": group.get("label") or group["name"],
                           "label": a.get("label") or a["name"], "status": a["status"],
                           "to": _image(a.get("to")), "from": _image(a.get("from")),
                           "flaky": bool(a.get("flaky"))})
    assets.sort(key=lambda a: (ORDER.get(a["status"], 9), a["group"], a["label"]))
    return {"ok": True, "summary": doc.get("summary", {}),
            "url": (doc.get("subject") or {}).get("html_url"), "assets": assets}


def newest_build(builds: dict):
    """The id of the newest finalized build in a `builds?pr=` answer, else None."""
    for b in builds.get("builds") or []:
        if b.get("status") == "finalized" and BUILD_ID.fullmatch(b.get("build_id") or ""):
            return b["build_id"]
    return None


def _fetch(base) -> dict:
    doc = _get(base)
    groups, page, pages = list(doc.get("groups", [])), doc.get("page") or {}, 1
    while page.get("has_more") and page.get("next_cursor") and pages < MAX_PAGES:
        more = _get(base + "?cursor=" + urllib.parse.quote(page["next_cursor"]))
        groups += more.get("groups", [])
        page, pages = more.get("page") or {}, pages + 1
    doc["groups"] = groups
    result = trim(doc)
    # Never pass off part of a gallery as all of it.
    result["incomplete"] = bool(page.get("has_more"))
    return result


def gallery(project: str, num: int, api: str = API) -> dict:
    """project is '<org>/<name>' as the repo's screenery/push step names it."""
    key = (project, num)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < hit[2]:
            return hit[1]
    root = f"{api}/v1/projects/{urllib.parse.quote(project, safe='/')}"
    ttl = MISS_TTL
    try:
        try:
            result = _fetch(f"{root}/channels/pr-{num}/gallery")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            found = _get(f"{root}/builds?pr={num}")
            build = newest_build(found)
            if not build:
                # A build still uploading will be there in minutes; no build at all may never be.
                ttl = MISS_TTL if found.get("builds") else NO_BUILD_TTL
                raise
            result = _fetch(f"{api}/v1/builds/{build}/gallery")
    except urllib.error.HTTPError as exc:
        result = {"ok": False, "reason": "none" if exc.code == 404 else f"Screenery answered {exc.code}"}
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        result = {"ok": False, "reason": f"Could not reach Screenery: {exc}"}
    # A refresh that failed (Screenery busy, network down) keeps the gallery it had, so a
    # row never loses its Screenshots button to a 429. A real miss ("none") still clears it.
    fresh = result.get("ok")
    if not fresh and result.get("reason") != "none" and hit and hit[1].get("ok"):
        result = hit[1]
    with _lock:
        _cache[key] = (time.time(), result, TTL if fresh else ttl)
    return result
