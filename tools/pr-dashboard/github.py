"""GitHub side of the PR dashboard: one GraphQL query, then plain rules per PR.

Every open PR across the configured owners comes back from a single search query
(paged 50 at a time). Each PR is then turned into one table row: which lane it is in,
what blocks it, how risky it looks and what it does. The rules live here, apart from
the server, so test_github.py can run them against a saved real response.

Only external dependency: the `gh` CLI, authenticated.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import PurePosixPath

FIELDS = """
number title url body createdAt isDraft state headRefOid headRefName baseRefName
repository { name nameWithOwner mergeCommitAllowed squashMergeAllowed rebaseMergeAllowed }
mergeable additions deletions changedFiles
labels(first: 20) { nodes { name } }
commits(last: 1) { nodes { commit { oid statusCheckRollup { state contexts(first: 100) { nodes {
  __typename ... on StatusContext { context state } ... on CheckRun { name status conclusion } } } } } } }
reviews(last: 30) { nodes { author { login } state submittedAt commit { oid } } }
reviewThreads(first: 100) { pageInfo { hasNextPage } nodes { isResolved isOutdated } }
comments { totalCount }
files(first: 100) { nodes { path additions deletions } }
"""

SEARCH_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { %s } }
  }
}
""" % FIELDS

ONE_QUERY = """
query($owner: String!, $name: String!, $num: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $num) { %s } }
}
""" % FIELDS

REVIEW_BOT = "coderabbitai"
# The commit status a Review with Claude session sets on the head commit. A status belongs
# to one commit, so a later push leaves the new head unreviewed again, as it should.
CLAUDE_REVIEW = "claude-review"
VERIFIED, VERIFY_FAILED = "verified", "verify-failed"

# A pythonw.exe parent has no console, so each gh.exe child would flash its own window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# The PR list is fetched under Board's lock, so a hung gh would stall every request.
GH_TIMEOUT = 90


class GhError(RuntimeError):
    pass


def gh(args, input_text=None) -> str:
    try:
        proc = subprocess.run(
            ["gh", *args],
            input=input_text.encode("utf-8") if input_text is not None else None,
            capture_output=True,
            creationflags=NO_WINDOW,
            env={**os.environ, "GH_PAGER": "cat", "NO_COLOR": "1"},
            timeout=GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"gh did not answer within {GH_TIMEOUT}s") from exc
    except OSError as exc:
        # Most often gh is not installed or not on PATH.
        raise GhError(f"could not run gh: {exc}") from exc
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise GhError(err or out or f"gh exited {proc.returncode}")
    return out


def graphql(args) -> dict:
    data = json.loads(gh(["api", "graphql", *args]))
    if data.get("errors"):
        raise GhError("; ".join(e.get("message", "") for e in data["errors"]))
    return data["data"]


def fetch_nodes(owners) -> list:
    """Every open PR for every owner, as raw GraphQL nodes."""
    nodes = []
    for owner in owners:
        after = None
        while True:
            # -f keeps a value a string; -F would turn a repo named `true` or `2024` into JSON.
            args = ["-f", f"q=is:pr is:open archived:false owner:{owner}", "-f", f"query={SEARCH_QUERY}"]
            if after:
                args += ["-f", f"after={after}"]
            search = graphql(args)["search"]
            nodes += [n for n in search["nodes"] if n]
            if not search["pageInfo"]["hasNextPage"]:
                break
            after = search["pageInfo"]["endCursor"]
    return nodes


# --------------------------------------------------------------------------- #
# status columns
# --------------------------------------------------------------------------- #

def rollup_of(node):
    commits = node["commits"]["nodes"]
    return (commits[0]["commit"]["statusCheckRollup"] if commits else None) or {}


CHECK_OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}


def checks_of(node) -> str:
    """CI only. The claude-review status is a review, not a check, so it is left out."""
    rollup = rollup_of(node)
    contexts = (rollup.get("contexts") or {}).get("nodes")
    if contexts is not None:
        states = []
        for c in contexts:
            if c.get("__typename") == "StatusContext":
                if c["context"] != CLAUDE_REVIEW:
                    states.append(c["state"])
            elif c.get("status") != "COMPLETED":
                states.append("PENDING")
            else:
                states.append("SUCCESS" if c.get("conclusion") in CHECK_OK else "FAILURE")
        if not states:
            return "none"
        if any(s in ("FAILURE", "ERROR") for s in states):
            return "fail"
        return "running" if any(s in ("PENDING", "EXPECTED") for s in states) else "pass"
    state = rollup.get("state")
    if state == "SUCCESS":
        return "pass"
    if state in ("FAILURE", "ERROR"):
        return "fail"
    if state in ("PENDING", "EXPECTED"):
        return "running"
    return "none"


def open_threads(node) -> int:
    return sum(1 for t in node["reviewThreads"]["nodes"] if not t["isResolved"] and not t["isOutdated"])


def threads_truncated(node) -> bool:
    # Only the first 100 threads are fetched; an open one past them must still count.
    return bool((node["reviewThreads"].get("pageInfo") or {}).get("hasNextPage"))


def claude_review_of(node) -> str:
    """pass / fail from the claude-review status on the head commit, else none."""
    for c in (rollup_of(node).get("contexts") or {}).get("nodes") or []:
        if c.get("__typename") == "StatusContext" and c["context"] == CLAUDE_REVIEW:
            return {"SUCCESS": "pass", "FAILURE": "fail", "ERROR": "fail"}.get(c["state"], "none")
    return "none"


def review_of(node) -> str:
    """done: CodeRabbit or Claude reviewed the head commit. changes: unresolved threads remain,
    or Claude's review found problems. stale: CodeRabbit only reviewed an older commit.
    pending: no review yet."""
    claude = claude_review_of(node)
    if open_threads(node) or threads_truncated(node) or claude == "fail":
        return "changes"
    if claude == "pass":
        return "done"
    bot = [r for r in node["reviews"]["nodes"] if (r.get("author") or {}).get("login") == REVIEW_BOT]
    if any((r.get("commit") or {}).get("oid") == node["headRefOid"] for r in bot):
        return "done"
    return "stale" if bot else "pending"


def labels_of(node) -> set:
    return {l["name"].lower() for l in node["labels"]["nodes"]}


def verify_of(node) -> str:
    labels = labels_of(node)
    if VERIFY_FAILED in labels:
        return "fail"
    if VERIFIED in labels:
        return "pass"
    return "none"


def conflict_of(node) -> str:
    # GitHub works mergeability out lazily, so UNKNOWN is common right after a push.
    return {"CONFLICTING": "yes", "MERGEABLE": "no"}.get(node["mergeable"], "checking")


def blockers_of(row) -> list:
    """Hard blockers put a PR in Needs a fix. Soft ones only mean it has to wait."""
    out = []
    if row["conflict"] == "yes":
        out.append({"text": "Merge conflict", "hard": True})
    if row["checks"] == "fail":
        out.append({"text": "Checks failed", "hard": True})
    if row["verify"] == "fail":
        out.append({"text": "Verify failed", "hard": True})
    n, more = row["openThreads"], row["threadsTruncated"]
    if n:
        out.append({"text": f"{n}{'+' if more else ''} open review comment{'s' if n != 1 or more else ''}",
                    "hard": True})
    elif more:
        out.append({"text": "Over 100 review threads, check GitHub", "hard": True})
    if row["claudeReview"] == "fail":
        out.append({"text": "Claude review found problems", "hard": True})
    if row["checks"] == "running":
        out.append({"text": "Checks running", "hard": False})
    # A verified PR does not wait on CodeRabbit (see lane_of), so do not say it does.
    if row["review"] == "pending" and row["verify"] != "pass":
        out.append({"text": "No review yet", "hard": False})
    if row["review"] == "stale" and row["verify"] != "pass":
        out.append({"text": "Review is for an older commit", "hard": False})
    if row["conflict"] == "checking":
        out.append({"text": "GitHub is checking for conflicts", "hard": False})
    return out


def lane_of(row) -> str:
    if row["draft"]:
        return "draft"
    if any(b["hard"] for b in row["blockers"]):
        return "fix"
    # A verified label means John ran the PR in the real app, which is a stronger
    # signal than CodeRabbit, so either one clears the review gate.
    reviewed = row["review"] == "done" or row["verify"] == "pass"
    if row["checks"] in ("pass", "none") and reviewed and row["conflict"] == "no":
        return "ready"
    return "wait"


# --------------------------------------------------------------------------- #
# risk and description
# --------------------------------------------------------------------------- #

HIGH_PATHS = [
    (re.compile(r"(^|/)migrations?/|\.sql$", re.I), "touches a database migration or SQL"),
    (re.compile(r"(^|/)schemas?(/|\.)", re.I), "touches a schema"),
    (re.compile(r"(^|/)(auth|authentication|security)(/|\.|[A-Z_-])", re.I), "touches auth or security code"),
    (re.compile(r"^\.github/workflows/", re.I), "changes CI workflows"),
]
CONFIG_PATH = re.compile(
    r"(^|/)(package\.json|appsettings[^/]*\.json|Dockerfile|[^/]+\.csproj|[^/]+\.toml|"
    r"[^/]+\.config\.[cm]?[jt]s|\.env[^/]*|wrangler\.[^/]+|[^/]+\.ya?ml)$", re.I)
DOC_PATH = re.compile(r"\.(md|mdx|txt)$|(^|/)docs/", re.I)
ALERT = re.compile(r"^> \[!(WARNING|CAUTION|IMPORTANT)\]\s*\n((?:>.*\n?)*)", re.M)


def risk_of(node, warning) -> dict:
    paths = [f["path"] for f in node["files"]["nodes"]]
    lines = node["additions"] + node["deletions"]
    reasons = []
    for pattern, why in HIGH_PATHS:
        if any(pattern.search(p) for p in paths):
            reasons.append(why)
    if node["changedFiles"] > 20:
        reasons.append(f"changes {node['changedFiles']} files")
    if warning:
        reasons.append("the description carries a warning")
    if reasons:
        return {"level": "high", "reasons": reasons}
    if paths and all(DOC_PATH.search(p) for p in paths):
        return {"level": "low", "reasons": ["docs only"]}
    if lines > 300:
        reasons.append(f"{lines} changed lines")
    config = sorted({PurePosixPath(p).name for p in paths if CONFIG_PATH.search(p)})
    if config:
        reasons.append("changes config: " + ", ".join(config[:3]))
    if reasons:
        return {"level": "med", "reasons": reasons}
    n = node["changedFiles"]
    return {"level": "low", "reasons": [f"{n} file{'s' if n != 1 else ''}, {lines} lines"]}


CODERABBIT_BLOCK = re.compile(
    r"<!-- This is an auto-generated comment: release notes by coderabbit\.ai -->.*?"
    r"(<!-- end of auto-generated comment: release notes by coderabbit\.ai -->|\Z)", re.S)
FOOTER = re.compile(r"^---\s*\n_Generated by \[Claude Code\].*$", re.M)
COMMENT = re.compile(r"<!--.*?-->", re.S)
# Only real HTML tag names, so prose like List<string> survives.
HTML_TAG = re.compile(r"</?(?:details|summary|p|em|a|blockquote|h[1-6]|ul|ol|li|br|img|code|pre|strong|b|i|div|span|table|thead|tbody|tr|td|th|sup|sub|hr)\b[^>]*>", re.I)
MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
SKIP_PARA = re.compile(r"^(#|>|Notion ticket|Closes |Fixes |Refs? |```)", re.I)


def describe(body) -> dict:
    """Split a PR body into a one-paragraph summary, the author's text, and any alert."""
    text = (body or "").replace("\r\n", "\n")
    text = CODERABBIT_BLOCK.sub("", text)
    text = FOOTER.sub("", text)
    text = COMMENT.sub("", text)
    text = HTML_TAG.sub("", text).strip()
    warning = ""
    m = ALERT.search(text)
    if m:
        warning = " ".join(line.lstrip("> ").strip() for line in m.group(2).splitlines()).strip()
        warning = warning.replace("**", "")
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    summary = next((p for p in paras if not SKIP_PARA.match(p)), paras[0] if paras else "")
    summary = MD_LINK.sub(r"\1", summary).replace("**", "").replace("`", "")
    summary = re.sub(r"\s+", " ", summary)
    if len(summary) > 280:
        summary = summary[:277].rsplit(" ", 1)[0] + "…"
    return {"summary": summary, "description": text[:2400], "warning": warning}


def areas_of(node) -> list:
    """Changed files grouped by top folder, biggest group first."""
    groups = {}
    for f in node["files"]["nodes"]:
        parts = f["path"].split("/")
        name = parts[0] + "/" if len(parts) > 1 else "(root)"
        g = groups.setdefault(name, {"name": name, "count": 0, "paths": []})
        g["count"] += 1
        g["paths"].append(f["path"])
    return sorted(groups.values(), key=lambda g: (-g["count"], g["name"]))


def merge_method_of(repo) -> str | None:
    if repo["squashMergeAllowed"]:
        return "squash"
    if repo["mergeCommitAllowed"]:
        return "merge"
    if repo["rebaseMergeAllowed"]:
        return "rebase"
    return None


# --------------------------------------------------------------------------- #
# rows
# --------------------------------------------------------------------------- #

def build_row(node) -> dict:
    repo = node["repository"]
    owner = repo["nameWithOwner"].split("/")[0]
    desc = describe(node["body"])
    row = {
        "key": f"{repo['nameWithOwner']}#{node['number']}",
        "owner": owner, "repo": repo["name"], "num": node["number"],
        "title": node["title"], "url": node["url"], "created": node["createdAt"],
        "draft": node["isDraft"], "headSha": node["headRefOid"],
        "headBranch": node["headRefName"], "baseBranch": node["baseRefName"],
        "checks": checks_of(node), "review": review_of(node), "openThreads": open_threads(node),
        "threadsTruncated": threads_truncated(node), "claudeReview": claude_review_of(node),
        "verify": verify_of(node), "conflict": conflict_of(node),
        "files": node["changedFiles"], "plus": node["additions"], "minus": node["deletions"],
        "comments": node["comments"]["totalCount"],
        "areas": areas_of(node), "mergeMethod": merge_method_of(repo),
        **desc,
    }
    row["risk"] = risk_of(node, desc["warning"])
    row["blockers"] = blockers_of(row)
    row["lane"] = lane_of(row)
    return row


def build_rows(nodes, exclude_repos=()) -> list:
    skip = {r.lower() for r in exclude_repos}
    rows = [build_row(n) for n in nodes if n["repository"]["name"].lower() not in skip]
    return sorted(rows, key=lambda r: r["created"])


def fetch_row(owner, repo, num) -> dict:
    """Re-read one PR right before acting on it, so a merge never trusts a stale list."""
    data = graphql(["-f", f"owner={owner}", "-f", f"name={repo}", "-F", f"num={num}",
                    "-f", f"query={ONE_QUERY}"])
    node = (data.get("repository") or {}).get("pullRequest")
    if not node or node["state"] != "OPEN":
        raise GhError(f"{owner}/{repo}#{num} is not an open PR any more")
    return build_row(node)
