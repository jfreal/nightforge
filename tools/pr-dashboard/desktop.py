"""Claude desktop scheduled tasks: find them on disk, and give each one a report-back footer.

Desktop keeps each task as `~/.claude/scheduled-tasks/<name>/SKILL.md`: YAML frontmatter with
`name` and `description`, then the prompt. The schedule and run history stay inside the app,
and nothing on disk ties a session to the task that started it. So:

- discover() lists every task as a bot, with its name and description. No setup needed.
- What a run did only reaches the page if the run says so. install() appends a short block to
  each prompt telling Claude to run `botlog.py report` as its last step. The block sits between
  markers, so installing again replaces it and remove() takes it out cleanly. Desktop reads
  SKILL.md fresh on every run, so the next run already reports.

https://code.claude.com/docs/en/desktop-scheduled-tasks
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import routines

START = "<!-- nightforge-report:start"
END = "<!-- nightforge-report:end -->"
BLOCK = re.compile(r"\r?\n*" + re.escape(START) + r".*?" + re.escape(END) + r"[ \t]*(\r?\n)?", re.S)
BOTLOG = Path(__file__).resolve().parent / "botlog.py"

FOOTER = """{start} (added by botlog.py desktop; take it out with `botlog.py desktop --remove`) -->
## Report back

When the work above is done, or as soon as you have to stop, run this once as your very last step:

```
"{python}" "{botlog}" report {bot} STATUS "ONE PLAIN SENTENCE" --count NAME=N --link "LABEL=URL"
```

- STATUS is `ok` if there was nothing to do, `found` if you produced something (PRs, issues, findings, a report), `needs-you` if you stopped on a decision only a person can make, and `failed` if you could not do the job.
- Add one `--count` for each number worth tracking, such as `prs=2` or `findings=5`, and one `--link` for each PR, issue or report you made. Leave out the ones you have none of.
- If the command itself fails, say so in your final message. Don't retry it more than once.
{end}"""


def tasks_dir() -> Path:
    """Where Desktop keeps scheduled tasks: CLAUDE_CONFIG_DIR if set, else ~/.claude."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
    return Path(base) / "scheduled-tasks"


def frontmatter(text: str) -> dict:
    """The `key: value` lines between the opening `---` lines, enough for name and description.
    A folded (`>`) or literal (`|`) value is joined from the indented lines under it."""
    m = re.match(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(\r?\n|\Z)", text, re.S)
    if not m:
        return {}
    out, key = {}, None
    for line in m.group(1).splitlines():
        kv = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if kv:
            key, value = kv.group(1), kv.group(2).strip()
            out[key] = "" if value in (">", "|", ">-", "|-") else value.strip("'\"")
        elif key and line.startswith((" ", "\t")):
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def bot_id(folder: Path) -> str:
    return routines.slug(folder.name)


def discover(root: Path | None = None) -> list[dict]:
    """Every task folder holding a SKILL.md, sorted by folder name."""
    root = root or tasks_dir()
    if not root.is_dir():
        return []
    found = []
    for skill in sorted(root.glob("*/SKILL.md")):
        try:
            text = skill.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):   # unreadable, or saved in another encoding
            continue
        meta = frontmatter(text)
        found.append({"id": bot_id(skill.parent), "folder": skill.parent.name, "path": skill,
                      "name": meta.get("name") or skill.parent.name,
                      "description": meta.get("description", ""), "reports": START in text})
    return found


def footer(bot: str, python: str | None = None) -> str:
    # Forward slashes work in both PowerShell and the Git Bash that Claude Code uses on Windows.
    return FOOTER.format(start=START, end=END, bot=bot, botlog=posix(BOTLOG),
                         python=posix(python or sys.executable))


def posix(path) -> str:
    return str(path).replace("\\", "/")


def with_footer(text: str, bot: str, python: str | None = None) -> str:
    nl = "\r\n" if "\r\n" in text else "\n"
    body = without_footer(text).rstrip()
    return body + nl + nl + footer(bot, python).replace("\n", nl) + nl


def without_footer(text: str) -> str:
    nl = "\r\n" if "\r\n" in text else "\n"
    stripped = BLOCK.sub(nl, text)
    return stripped.rstrip() + nl if stripped != text else text


def install(task: dict, apply: bool, python: str | None = None) -> bool:
    """Add or refresh the footer. Returns whether the file would change. The first change keeps
    the original as SKILL.md.nightforge.bak."""
    path = task["path"]
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    new = with_footer(text, task["id"], python)
    if new == text:
        return False
    if apply:
        backup = path.with_name(path.name + ".nightforge.bak")
        if not backup.exists():
            backup.write_bytes(raw)
        path.write_bytes((("﻿" if raw.startswith(b"\xef\xbb\xbf") else "") + new).encode("utf-8"))
    return True


def remove(task: dict, apply: bool) -> bool:
    path = task["path"]
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    new = without_footer(text)
    if new == text:
        return False
    if apply:
        path.write_bytes((("﻿" if raw.startswith(b"\xef\xbb\xbf") else "") + new).encode("utf-8"))
    return True


def collect(store, root: Path | None = None) -> tuple[int, list[str]]:
    """List each task as a bot. It finds no runs: those arrive when a run calls botlog.py.
    Tasks without the footer are named in one line, since they can only ever show "No report"."""
    tasks = discover(root)
    for t in tasks:
        store.ensure_bot_now(t["id"], t["name"], "desktop task", description=t["description"][:300])
    silent = [t["folder"] for t in tasks if not t["reports"]]
    problems = []
    if silent:
        shown = ", ".join(silent[:8]) + (f" and {len(silent) - 8} more" if len(silent) > 8 else "")
        problems.append(f"{len(silent)} desktop task(s) never report what they did ({shown}). "
                        "Run `python botlog.py desktop --apply` to add the report-back step.")
    return 0, problems
