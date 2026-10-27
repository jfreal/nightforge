"""Send to Claude: open a Claude Code session on one PR in a new Windows Terminal tab.

The session starts in the repo's local clone, in a fresh worktree so the clone's own
checkout is never touched, with a prompt built from what the dashboard knows.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path


def build_prompt(row, merge=False) -> str:
    pr = f"{row['owner']}/{row['repo']}#{row['num']}"
    hard = [b["text"] for b in row["blockers"] if b["hard"]]
    soft = [b["text"] for b in row["blockers"] if not b["hard"]]
    start = (f"You are in a fresh git worktree. First run `gh pr checkout {row['num']}` "
             f"so you are on the PR branch ({row['headBranch']}).")
    if merge:
        return merge_prompt(row, pr, hard, soft, start)
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


def merge_prompt(row, pr, hard, soft, start) -> str:
    """Fix and merge: the one prompt that lets Claude merge, so it carries the stop rules."""
    num, method = row["num"], row["mergeMethod"] or "squash"
    steps = [f"Get PR {pr} “{row['title']}” ({row['url']}) merged. {start}"]
    if hard:
        steps.append(f"It is blocked by: {', '.join(hard)}. Merge the base branch ({row['baseBranch']}) in, "
                     "fix the conflicts and failures, answer or fix every open review comment, run the tests, "
                     "and push. If the verify-failed label is set, read the latest verify comment to see what broke.")
    if soft:
        steps.append(f"It is also waiting on: {', '.join(soft)}.")
    if row["draft"]:
        steps.append(f"It is a draft. Once it is in shape, mark it ready with `gh pr ready {num}`.")
    steps.append(f"Wait for checks with `gh pr checks {num} --watch`. When every check passes and GitHub "
                 f"shows no conflict, merge with `gh pr merge {num} --{method} --match-head-commit <head sha>`.")
    stop = []
    if row["risk"]["level"] == "high":
        stop.append(f"the risk is high ({'; '.join(row['risk']['reasons'])})")
    stop += ["the description warns against merging yet",
             "a fix needs a judgement call you are not sure of",
             "checks still fail after two fix attempts"]
    steps.append("Stop and ask me before merging if " + ", or ".join(stop) + ".")
    return " ".join(steps)


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


def session_command(row, clone: Path, merge=False) -> list:
    title = f"{row['repo']} #{row['num']}"
    worktree = f"pr-{row['num']}-{datetime.now():%m%d%H%M}"
    inner = [claude_exe(), "-n", title, "-w", worktree, build_prompt(row, merge)]
    return ["wt.exe", "-w", "0", "nt", "--title", wt_escape(title), "-d", str(clone),
            *[wt_escape(a) for a in inner]]


def open_session(row, repos_root, dry_run=False, merge=False) -> dict:
    clone = Path(repos_root) / row["repo"]
    if not (clone / ".git").exists():
        return {"ok": False, "error": f"No local clone at {clone}"}
    cmd = session_command(row, clone, merge)
    if dry_run:
        return {"ok": True, "dryRun": True, "command": cmd}
    try:
        # A list, never a shell string: PR titles are untrusted text.
        subprocess.Popen(cmd, cwd=str(clone), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    except OSError as exc:
        return {"ok": False, "error": f"Could not start Windows Terminal: {exc}"}
    return {"ok": True, "command": cmd}
