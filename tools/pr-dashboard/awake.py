"""Keep this PC from idle-sleeping while a background session the dashboard started is working.

Background sessions (`claude --bg`) run on this PC, so a sleeping PC pauses them, and Remote
Control can't reach them either. Claude desktop's own keep-awake setting only covers its own
sessions, not these. Every minute this asks `claude agents --json` about the background sessions
under the repos folder, and holds a Windows "system required" request while any of them is
working. A session waiting on you (state blocked) holds it for WAIT_GRACE more, so the question
can still be answered from a phone, then lets go so a forgotten question can't keep the PC up
for days. Only idle sleep is held off: closing the lid or picking Sleep still sleeps.

`state` (working, blocked, done, failed, stopped) describes the session and is there even when
its process has exited; `pid` and `status` are there only while the process is alive.
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
from pathlib import Path

EVERY = 60
WAIT_GRACE = 60 * 60
# A poll that fails (timeout, bad output) says nothing about the sessions, so the last answer
# stands for this long before the hold is dropped.
FAIL_GRACE = 10 * 60
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def list_sessions(claude: str) -> list | None:
    """The sessions `claude agents --json` reports, or None if it could not be asked."""
    try:
        proc = subprocess.run([claude, "agents", "--json"], capture_output=True, timeout=30,
                              creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
        data = json.loads(proc.stdout) if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return data if isinstance(data, list) else None


def under(path: str, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


class KeepAwake:
    def __init__(self, repos_root, claude: str, log=print):
        self.root, self.claude, self.log = Path(repos_root), claude, log
        self.waiting_since: dict = {}   # session id -> when it was first seen blocked
        self.held = 0                    # sessions holding the PC awake right now
        self.last_good = 0.0             # when the last poll succeeded

    def working(self, sessions, now: float) -> int:
        """How many background sessions under the repos folder should keep the PC awake."""
        count, waiting = 0, {}
        for s in sessions:
            if s.get("kind") != "background" or not under(s.get("cwd", ""), self.root):
                continue
            state = s.get("state") or ("blocked" if s.get("status") == "waiting" else
                                       "working" if s.get("pid") and s.get("status") != "idle" else "done")
            if state == "working":
                count += 1
            elif state == "blocked":
                sid = s.get("sessionId") or s.get("id")
                waiting[sid] = self.waiting_since.get(sid, now)
                if now - waiting[sid] < WAIT_GRACE:
                    count += 1
        self.waiting_since = waiting
        return count

    def poll(self, now: float) -> int:
        sessions = list_sessions(self.claude)
        if sessions is None:
            return self.held if now - self.last_good < FAIL_GRACE else 0
        self.last_good = now
        return self.working(sessions, now)

    def hold(self, on: bool) -> None:
        if sys.platform == "win32":
            flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
            ctypes.windll.kernel32.SetThreadExecutionState(flags)

    def run(self) -> None:
        # The request belongs to the thread that made it, so this one thread sets and clears it.
        self.last_good = time.time()
        while True:
            n = self.poll(time.time())
            if bool(n) != bool(self.held):
                self.hold(bool(n))
                self.log(f"keep awake: {'on, ' + str(n) + ' session(s) working' if n else 'off'}")
            self.held = n
            time.sleep(EVERY)
