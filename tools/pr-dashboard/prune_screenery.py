"""Free the Screenery channels that closed pull requests left behind.

Each CI push makes two channels: `pr-<n>` and one named after the PR's branch. Nothing
deletes them when the PR closes, and a project holds at most 100. Once a project is
full, a new PR's screenshots still upload but get no channel, so the dashboard's
Screenshots button and the PR comment both show nothing. The CI log says:

    No channel was created for "pr-1256" (channel_limit).

This deletes only channels that belong to a closed PR: `pr-<n>` of a closed or merged PR,
and a channel named after a closed PR's branch when no open PR uses that branch. It never
touches protected channels, the default branch, `latest`, or any other name. A deleted
channel's `@v_...` version URLs keep working; only the channel URL stops.

Dry run unless --apply. The token is a Screenery publish token for the project, read
from SCREENERY_TOKEN so it never lands in shell history or this repo:

    $env:SCREENERY_TOKEN = "<token>"
    python prune_screenery.py jfreal/pheidi jfreal/auxf
    python prune_screenery.py jfreal/pheidi jfreal/auxf --apply

Afterwards it lists the open PRs that still have no channel. Their next CI run creates one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import screenery

PR_CHANNEL = re.compile(r"^pr-(\d+)$")
ALWAYS_KEEP = {"latest", "main", "master"}
LIMIT = 100


def channel_name(branch: str) -> str:
    # Compared loosely, so a branch like `claude/fix-x` still matches however Screenery
    # spelled its channel; the name still has to belong to a closed PR to be deleted.
    return re.sub(r"[^a-z0-9._-]+", "-", branch.lower()).strip("-")


def plan(channels, prs, default_branch) -> tuple[list, list]:
    """(names to delete, open PR numbers with no channel). prs: [{number, state, headRefName}]."""
    open_nums = {p["number"] for p in prs if p["state"] == "OPEN"}
    closed_nums = {p["number"] for p in prs if p["state"] != "OPEN"}
    open_branches = {channel_name(p["headRefName"]) for p in prs if p["state"] == "OPEN"}
    closed_branches = {channel_name(p["headRefName"]) for p in prs if p["state"] != "OPEN"}
    keep = ALWAYS_KEEP | {channel_name(default_branch)} | open_branches
    delete = []
    for c in channels:
        name = c["name"]
        if c.get("protected") or name in keep:
            continue
        m = PR_CHANNEL.match(name)
        if m:
            if int(m.group(1)) in closed_nums:
                delete.append(name)
        elif channel_name(name) in closed_branches:
            delete.append(name)
    have = {c["name"] for c in channels}
    missing = sorted(n for n in open_nums if f"pr-{n}" not in have)
    return delete, missing


def gh_json(args):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(out.stdout)


def api(method, url, token):
    req = urllib.request.Request(url, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json",
        "User-Agent": "nightforge-prune-screenery"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.loads(res.read() or b"{}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Delete Screenery channels left by closed PRs.")
    ap.add_argument("repos", nargs="+", help="owner/repo; the Screenery project has the same name")
    ap.add_argument("--apply", action="store_true", help="delete; without it, only list")
    args = ap.parse_args(argv)
    token = os.environ.get("SCREENERY_TOKEN", "")
    if not token:
        print("Set SCREENERY_TOKEN to a Screenery publish token for the project first.")
        return 2
    failed = False
    for slug in args.repos:
        base = f"{screenery.API}/v1/projects/{urllib.parse.quote(slug, safe='/')}/channels"
        try:
            channels = api("GET", base, token)["channels"]
        except urllib.error.HTTPError as exc:
            print(f"{slug}: Screenery answered {exc.code} listing channels. Is the token for this project?")
            failed = True
            continue
        prs = gh_json(["pr", "list", "-R", slug, "--state", "all", "--limit", "5000",
                       "--json", "number,state,headRefName"])
        default = gh_json(["api", f"repos/{slug}"])["default_branch"]
        delete, missing = plan(channels, prs, default)
        print(f"{slug}: {len(channels)} of {LIMIT} channels used, {len(delete)} belong to closed PRs.")
        for name in delete:
            if not args.apply:
                print(f"  would delete {name}")
                continue
            try:
                api("DELETE", f"{base}/{urllib.parse.quote(name, safe='')}", token)
                print(f"  deleted {name}")
            except urllib.error.HTTPError as exc:
                print(f"  could not delete {name}: Screenery answered {exc.code}")
                failed = True
        if missing:
            print(f"  open PRs with no channel yet: {', '.join(f'#{n}' for n in missing)}")
    if not args.apply:
        print("Dry run: nothing was deleted. Run again with --apply to delete.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
