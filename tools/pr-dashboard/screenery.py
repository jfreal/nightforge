"""Screenery side of the PR dashboard: the screenshots a PR's CI build pushed.

Screenery keeps each PR's screenshots on a channel named `pr-<number>` and serves them,
compared with the default branch, as one JSON gallery:

    GET https://app.screenery.dev/v1/projects/<org>/<project>/channels/pr-<n>/gallery

Public projects need no token. A private project, or a PR whose CI pushed nothing,
answers 404, which the dashboard shows as "no screenshots".
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://app.screenery.dev"
TTL = 600          # a PR's screenshots change only when its CI runs again
MISS_TTL = 60      # but a miss may be a CI run still uploading, so ask again soon
MAX_PAGES = 20     # a safety stop; hitting it marks the gallery incomplete
# Changed shots first: they are the ones worth a look before merging.
ORDER = {"changed": 0, "added": 1, "removed": 2, "unchanged": 3}

_cache: dict = {}
_lock = threading.Lock()


def _get(url) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "nightforge-pr-dashboard", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.loads(res.read())


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


def gallery(project: str, num: int, api: str = API) -> dict:
    """project is '<org>/<name>' as the repo's screenery/push step names it."""
    key = (project, num)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < (TTL if hit[1].get("ok") else MISS_TTL):
            return hit[1]
    base = f"{api}/v1/projects/{urllib.parse.quote(project, safe='/')}/channels/pr-{num}/gallery"
    try:
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
    except urllib.error.HTTPError as exc:
        result = {"ok": False, "reason": "none" if exc.code == 404 else f"Screenery answered {exc.code}"}
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        result = {"ok": False, "reason": f"Could not reach Screenery: {exc}"}
    with _lock:
        _cache[key] = (time.time(), result)
    return result
