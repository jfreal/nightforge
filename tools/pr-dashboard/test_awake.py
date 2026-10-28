"""Which background sessions keep the PC awake (awake.py).

    python -m unittest test_awake.py
"""

import tempfile
import unittest
from pathlib import Path

import awake


class Working(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "pheidi").mkdir()
        self.keeper = awake.KeepAwake(self.root, "claude", log=lambda m: None)

    def bg(self, **over):
        s = {"kind": "background", "pid": 71156, "cwd": str(self.root / "pheidi"), "sessionId": "a", "status": "busy"}
        s.update(over)
        return s

    def test_a_working_session_under_the_repos_folder_counts(self):
        self.assertEqual(self.keeper.working([self.bg()], 0), 1)

    def test_ended_idle_interactive_and_elsewhere_do_not(self):
        others = [self.bg(pid=None, state="blocked"), self.bg(status="idle"), self.bg(kind="interactive"),
                  self.bg(cwd=str(Path(tempfile.mkdtemp())))]
        self.assertEqual(self.keeper.working(others, 0), 0)

    def test_a_waiting_session_counts_only_for_the_grace_period(self):
        waiting = [self.bg(status="waiting")]
        self.assertEqual(self.keeper.working(waiting, 1000), 1)
        self.assertEqual(self.keeper.working(waiting, 1000 + awake.WAIT_GRACE - 1), 1)
        self.assertEqual(self.keeper.working(waiting, 1000 + awake.WAIT_GRACE), 0)

    def test_the_grace_period_restarts_after_the_session_works_again(self):
        self.keeper.working([self.bg(status="waiting")], 0)
        self.keeper.working([self.bg(status="busy")], 10)
        self.assertEqual(self.keeper.working([self.bg(status="waiting")], awake.WAIT_GRACE + 20), 1)


if __name__ == "__main__":
    unittest.main()
