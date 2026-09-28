#!/usr/bin/env python3
"""ensure-priority-label — create the sweep's priority label in every repo it sweeps.

GitHub labels are per-repo, so `priorityLabels` in config.json does nothing until the
label actually exists on each repo. This walks the same owners and the same
`excludeRepos` the sweep uses, so the two can never disagree about which repos are in
the fleet, and creates any label that is missing.

Idempotent: a repo that already carries the label is left alone unless --update is
given, in which case its colour and description are brought in line.

Usage:
    python ensure-priority-label.py                      # preview, writes nothing
    python ensure-priority-label.py --apply              # create the missing labels
    python ensure-priority-label.py --apply --update     # also fix colour/description
    python ensure-priority-label.py --apply --repo web-app --repo api
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep import NO_WINDOW, GhError, gh_raw, load_config    # noqa: E402  (path set above)

DEFAULT_COLOR = "B60205"                                 # GitHub's own red
DEFAULT_DESC = "CodeRabbit sweep: review this PR next"


def gh_write(args):
    """Run a writing gh command and report the exit code honestly.

    `gh_raw` cannot be used here: it treats an empty stdout as a failed call, and
    `gh label create` writes its whole success message to stderr. Reading that as an
    error would report every created label as a failure.
    """
    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        creationflags=NO_WINDOW,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "GH_PAGER": "cat", "NO_COLOR": "1"},
    )
    err = proc.stderr.decode("utf-8", errors="replace").strip()
    return proc.returncode == 0, err


def repos_for(owner: str, include_archived: bool):
    """Every repo the owner owns, in the same shape the sweep's search covers."""
    out = gh_raw(["repo", "list", owner, "--limit", "1000",
                  "--json", "name,nameWithOwner,isArchived,isFork,viewerPermission"])
    rows = json.loads(out)
    if not include_archived:
        rows = [r for r in rows if not r.get("isArchived")]
    return rows


def labels_on(slug: str):
    """Lowercased label names already on the repo, or None if they cannot be read."""
    try:
        out = gh_raw(["label", "list", "--repo", slug, "--limit", "200", "--json", "name"])
    except GhError as exc:
        # A repo with Issues disabled has no label API at all. That is not a failure of
        # this script, and it is not a repo the sweep can be helped on either.
        return None, str(exc)
    return {(row.get("name") or "").strip().lower() for row in json.loads(out)}, ""


def main():
    ap = argparse.ArgumentParser(description="create the sweep's priority label across the fleet")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    ap.add_argument("--apply", action="store_true",
                    help="actually create the labels; without it nothing is written")
    ap.add_argument("--update", action="store_true",
                    help="also bring an existing label's colour and description in line")
    ap.add_argument("--label", action="append", metavar="NAME",
                    help="label to ensure, repeatable. Default: the config's priorityLabels")
    ap.add_argument("--owner", action="append", metavar="OWNER",
                    help="limit to these owners. Default: the config's owners")
    ap.add_argument("--repo", action="append", metavar="NAME",
                    help="limit to these repo names (not slugs), repeatable")
    ap.add_argument("--color", default=DEFAULT_COLOR, help=f"hex, no #. Default {DEFAULT_COLOR}")
    ap.add_argument("--description", default=DEFAULT_DESC)
    ap.add_argument("--include-archived", action="store_true",
                    help="archived repos are skipped by default; they reject label writes")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    owners = args.owner or cfg["owners"]
    wanted = [n.strip() for n in (args.label or sorted(cfg["priorityLabels"])) if n.strip()]
    only = {r.strip().lower() for r in (args.repo or [])}

    if not wanted:
        print("nothing to do: priorityLabels is empty and no --label was given")
        return 0

    print(f"owners:  {', '.join(owners)}")
    print(f"labels:  {', '.join(wanted)}  (#{args.color})")
    print(f"skipping excluded repos: {', '.join(sorted(cfg['excludeRepos'])) or 'none'}")
    print(f"mode:    {'APPLY' if args.apply else 'preview — nothing will be written'}\n")

    created = existing = updated = skipped = 0
    failures = []

    for owner in owners:
        try:
            rows = repos_for(owner, args.include_archived)
        except (GhError, json.JSONDecodeError) as exc:
            failures.append(f"{owner}: cannot list repos — {exc}")
            continue

        for row in sorted(rows, key=lambda r: r["name"].lower()):
            name, slug = row["name"], row["nameWithOwner"]
            if name in cfg["excludeRepos"]:
                skipped += 1
                continue
            if only and name.lower() not in only:
                skipped += 1
                continue
            # Creating a label needs push access. Saying so up front beats one 403 per repo.
            if (row.get("viewerPermission") or "") in ("READ", "TRIAGE"):
                skipped += 1
                print(f"  skip   {slug} — read-only access")
                continue

            have, why = labels_on(slug)
            if have is None:
                failures.append(f"{slug}: cannot read labels — {why}")
                continue

            for label in wanted:
                if label.lower() in have and not args.update:
                    existing += 1
                    print(f"  have   {slug}  {label}")
                    continue
                verb = "update" if label.lower() in have else "create"
                if not args.apply:
                    print(f"  would {verb} {slug}  {label}")
                    continue
                # --force is create-or-update in one call, so an existing label with the
                # wrong colour converges instead of erroring.
                cmd = ["label", "create", label, "--repo", slug,
                       "--color", args.color, "--description", args.description]
                if args.update:
                    cmd.append("--force")
                ok, err = gh_write(cmd)
                if ok:
                    if verb == "update":
                        updated += 1
                    else:
                        created += 1
                    print(f"  {verb}d {slug}  {label}")
                elif "already exists" in err.lower():
                    # Someone added it between the read and the write. Not a failure.
                    existing += 1
                    print(f"  have   {slug}  {label}")
                else:
                    failures.append(f"{slug} {label}: {err[:200]}")

    print(f"\n{created} created, {updated} updated, {existing} already present, "
          f"{skipped} repo(s) skipped")
    if not args.apply:
        print("preview only — re-run with --apply to write")
    for line in failures:
        print(f"FAILED {line}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
