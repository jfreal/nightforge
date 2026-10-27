#!/usr/bin/env python3
"""Keep the PR dashboard running: start server.py, and start it again whenever it stops.

server.py runs with --watch, so it exits on purpose when its own code changes (a git pull,
say) and comes straight back here with the new code. A crash comes back too, after a pause
that grows if it keeps crashing, so a broken checkout does not spin the CPU.

Run it with pythonw.exe for no window; install-startup.ps1 makes Windows do that at log on.
Everything goes to dashboard.log next to this file.

    pythonw supervise.py              # what the logon task runs
    python supervise.py --dry-run     # extra arguments are passed to server.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from server import RESTART, log  # noqa: E402  (same log file and restart code as the server)

QUICK = 30          # a run shorter than this counts as a crash loop
MAX_PAUSE = 300


def main() -> int:
    cmd = [sys.executable, str(HERE / "server.py"), "--no-open", "--watch", *sys.argv[1:]]
    pause = 2
    log("supervisor started")
    while True:
        started = time.time()
        try:
            code = subprocess.run(cmd, cwd=str(HERE), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode
        except KeyboardInterrupt:
            return 0
        except OSError as exc:
            log(f"could not start server.py: {exc}")
            code = -1
        if code == RESTART:
            pause = 2
            continue
        ran = time.time() - started
        pause = min(pause * 2, MAX_PAUSE) if ran < QUICK else 2
        log(f"server.py exited with {code} after {ran:.0f}s; starting it again in {pause}s")
        time.sleep(pause)


if __name__ == "__main__":
    sys.exit(main())
