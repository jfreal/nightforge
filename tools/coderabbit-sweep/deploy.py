#!/usr/bin/env python3
"""deploy — copy this folder's scripts to the folder the scheduled task actually runs.

The repo is usually checked out in a transient git worktree, so the Windows task
"CodeRabbit Sweep" runs a *copy* of these files under the state directory. Editing the
repo copy alone changes nothing about what runs at the next tick; this closes that gap.

The target is read from `config.json`'s `stateDir`, so there is no second place to keep
a path in step. Only the known script files are copied. Everything the task owns —
`ledger.json`, `board.html`, `runs.json`, `sweep.log`, `reports/` — is live state and is
never touched.

Usage:
    python deploy.py                 # preview what would change
    python deploy.py --apply         # copy the changed files
    python deploy.py --apply --config-too   # also overwrite the target's config.json
"""

from __future__ import annotations

import argparse
import filecmp
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    # Same reason as sweep.py: a cp1252 console mangles the dashes in these messages.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# The script files. `config.json` is handled separately — the target's copy is the
# machine's own and overwriting it by default would be a surprise.
PAYLOAD = [
    "sweep.py",
    "ensure-priority-label.py",
    "deploy.py",
    "board-template.html",
    "run.cmd",
    "deploy.cmd",
    "README.md",
    "config.example.json",
]

LOCK_STALE_MINUTES = 25


def lock_holder(target: Path):
    """Whoever holds the sweep lock right now, or None.

    Copying `sweep.py` out from under a live run is how you get a half-old, half-new
    pipeline deciding whether to spend the hour's review.
    """
    path = target / "sweep.lock"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        at = datetime.fromisoformat((data.get("at") or "").replace("Z", "+00:00"))
    except (json.JSONDecodeError, OSError, ValueError, AttributeError):
        return "unreadable lock file"
    if datetime.now(timezone.utc) - at > timedelta(minutes=LOCK_STALE_MINUTES):
        return None                      # stale; the sweep clears these itself
    return f"a sweep started {at.isoformat().replace('+00:00', 'Z')} (pid {data.get('pid')})"


def main():
    ap = argparse.ArgumentParser(description="deploy the sweep scripts to the scheduled task")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    ap.add_argument("--apply", action="store_true", help="copy; without it nothing is written")
    ap.add_argument("--config-too", action="store_true",
                    help="also overwrite the target's config.json with this folder's")
    ap.add_argument("--target", help="override the destination (default: the config's stateDir)")
    ap.add_argument("--force", action="store_true",
                    help="deploy even while a sweep holds the lock")
    args = ap.parse_args()

    src = Path(__file__).resolve().parent
    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"no config at {cfg_path} — copy config.example.json to config.json first")
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    target = Path(args.target or cfg.get("stateDir") or ".").resolve()

    print(f"from  {src}")
    print(f"to    {target}")
    if not target.exists():
        print("target does not exist — create it, or pass --target")
        return 1
    if src == target:
        print("source and target are the same folder; nothing to deploy")
        return 0

    held = lock_holder(target)
    if held and not args.force:
        print(f"REFUSING: {held} is running. Wait for it, or pass --force.")
        return 1
    if held:
        print(f"WARNING: {held} is running; --force given, copying anyway")

    files = list(PAYLOAD) + (["config.json"] if args.config_too else [])
    copied = same = missing = 0
    for name in files:
        s, t = src / name, target / name
        if not s.exists():
            print(f"  missing  {name} — not in this folder, skipped")
            missing += 1
            continue
        if t.exists() and filecmp.cmp(s, t, shallow=False):
            same += 1
            continue
        verb = "update" if t.exists() else "add"
        if not args.apply:
            print(f"  would {verb}  {name}")
            continue
        shutil.copy2(s, t)
        copied += 1
        print(f"  {'updated' if verb == 'update' else 'added'}  {name}")

    if not args.config_too:
        s, t = src / "config.json", target / "config.json"
        if s.exists() and t.exists() and not filecmp.cmp(s, t, shallow=False):
            # Not an error: the target's config is the machine's own. But a new key
            # added to this folder's config never reaches the task without a word here.
            print("  NOTE     config.json differs between the two folders — left alone. "
                  "Pass --config-too to overwrite it.")

    print(f"\n{copied} copied, {same} already identical, {missing} missing")
    if not args.apply:
        print("preview only — re-run with --apply to write")
    else:
        print("deployed. The next scheduled tick runs the new code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
