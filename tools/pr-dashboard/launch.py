"""Send to Claude: open a Claude Code session on one PR in a new Windows Terminal tab.

The session starts in the repo's local clone, in a fresh worktree so the clone's own
checkout is never touched, with a prompt built from what the dashboard knows.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path


def build_prompt(row) -> str:
    pr = f"{row['owner']}/{row['repo']}#{row['num']}"
    hard = [b["text"] for b in row["blockers"] if b["hard"]]
    soft = [b["text"] for b in row["blockers"] if not b["hard"]]
    start = (f"You are in a fresh git worktree. First run `gh pr checkout {row['num']}` "
             f"so you are on the PR branch ({row['headBranch']}).")
    if hard:
        task = (f"Fix PR {pr} “{row['title']}” ({row['url']}). It is blocked by: {', '.join(hard)}. "
                f"Merge the base branch ({row['baseBranch']}) into it, fix the conflicts and failures, "
                "answer or fix every open review comment, run the tests, and push to the PR branch. "
                "If the verify-failed label is set, read the latest verify comment on the PR to see what broke.")
    elif row["lane"] == "ready":
        task = (f"Review PR {pr} “{row['title']}” ({row['url']}) before I merge it. Read the diff, "
                f"check what could break (risk: {row['risk']['level']}, {'; '.join(row['risk']['reasons'])}), "
                "and tell me plainly whether it is safe to merge and why.")
    else:
        waiting = ", ".join(soft) or "nothing obvious"
        task = (f"Look at PR {pr} “{row['title']}” ({row['url']}). It is waiting on: {waiting}. "
                "Find out what it needs to be ready to merge, do what you can, and push to the PR branch.")
    return f"{task} {start} Do not merge the PR."


def wt_escape(arg: str) -> str:
    # wt.exe splits its own command line on `;`, even inside quotes, unless escaped. It also
    # re-splits the inner command on `"`, so a straight quote in a PR title would cut the
    # prompt into pieces; curly quotes read the same to Claude and pass through whole.
    return arg.replace(";", "\\;").replace('"', "”")


def claude_exe() -> str:
    """wt.exe starts programs directly, so it needs a real .exe, not npm's claude.cmd shim."""
    found = shutil.which("claude.exe")
    if found:
        return found
    shim = shutil.which("claude")
    if shim:
        exe = Path(shim).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if exe.exists():
            return str(exe)
    return "claude"


def session_command(row, clone: Path) -> list:
    title = f"{row['repo']} #{row['num']}"
    worktree = f"pr-{row['num']}-{datetime.now():%m%d%H%M}"
    inner = [claude_exe(), "-n", title, "-w", worktree, build_prompt(row)]
    return ["wt.exe", "-w", "0", "nt", "--title", wt_escape(title), "-d", str(clone),
            *[wt_escape(a) for a in inner]]


def open_session(row, repos_root, dry_run=False) -> dict:
    clone = Path(repos_root) / row["repo"]
    if not (clone / ".git").exists():
        return {"ok": False, "error": f"No local clone at {clone}"}
    cmd = session_command(row, clone)
    if dry_run:
        return {"ok": True, "dryRun": True, "command": cmd}
    try:
        # A list, never a shell string: PR titles are untrusted text.
        subprocess.Popen(cmd, cwd=str(clone), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    except OSError as exc:
        return {"ok": False, "error": f"Could not start Windows Terminal: {exc}"}
    return {"ok": True, "command": cmd}
