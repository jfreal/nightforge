"""Which background sessions keep the PC awake (awake.py).

    python -m unittest test_awake.py
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import awake


class Working(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "pheidi").mkdir()
        self.keeper = awake.KeepAwake(self.root, "claude", log=lambda m: None)

    def bg(self, **over):
        s = {"kind": "background", "pid": 71156, "cwd": str(self.root / "pheidi"), "sessionId": "a",
             "status": "busy", "state": "working"}
        s.update(over)
        return {k: v for k, v in s.items() if v is not None}

    def test_a_working_session_under_the_repos_folder_counts(self):
        self.assertEqual(self.keeper.working([self.bg()], 0), 1)

    def test_state_counts_even_after_the_process_exits(self):
        # pid and status are listed only while the process is alive; state is always there.
        self.assertEqual(self.keeper.working([self.bg(pid=None, status=None)], 0), 1)
        self.assertEqual(self.keeper.working([self.bg(pid=None, status=None, state="blocked")], 0), 1)

    def test_finished_interactive_and_elsewhere_do_not(self):
        others = [self.bg(state="done", status="idle"), self.bg(state="failed", pid=None),
                  self.bg(state="stopped", pid=None), self.bg(kind="interactive", state=None),
                  self.bg(cwd=str(Path(tempfile.mkdtemp())))]
        self.assertEqual(self.keeper.working(others, 0), 0)

    def test_without_state_the_process_status_decides(self):
        self.assertEqual(self.keeper.working([self.bg(state=None)], 0), 1)
        self.assertEqual(self.keeper.working([self.bg(state=None, status="idle")], 0), 0)
        self.assertEqual(self.keeper.working([self.bg(state=None, status="waiting")], 0), 1)

    def test_a_blocked_session_counts_only_for_the_grace_period(self):
        blocked = [self.bg(state="blocked", status="waiting")]
        self.assertEqual(self.keeper.working(blocked, 1000), 1)
        self.assertEqual(self.keeper.working(blocked, 1000 + awake.WAIT_GRACE - 1), 1)
        self.assertEqual(self.keeper.working(blocked, 1000 + awake.WAIT_GRACE), 0)

    def test_the_grace_period_restarts_after_the_session_works_again(self):
        self.keeper.working([self.bg(state="blocked")], 0)
        self.keeper.working([self.bg()], 10)
        self.assertEqual(self.keeper.working([self.bg(state="blocked")], awake.WAIT_GRACE + 20), 1)


class FailedPoll(unittest.TestCase):
    def test_a_failed_poll_keeps_the_hold_for_a_while_then_drops_it(self):
        keeper = awake.KeepAwake(".", "claude", log=lambda m: None)
        keeper.held, keeper.last_good = 2, 1000
        with mock.patch.object(awake, "list_sessions", return_value=None):
            self.assertEqual(keeper.poll(1000 + awake.FAIL_GRACE - 1), 2)
            self.assertEqual(keeper.poll(1000 + awake.FAIL_GRACE), 0)

    def test_a_nonzero_exit_or_bad_output_is_a_failure_not_an_empty_list(self):
        for code, out in ((1, b""), (0, b"not json"), (0, b"{}")):
            done = awake.subprocess.CompletedProcess([], code, out, b"")
            with mock.patch.object(awake.subprocess, "run", return_value=done):
                self.assertIsNone(awake.list_sessions("claude"), (code, out))
        done = awake.subprocess.CompletedProcess([], 0, b"[]", b"")
        with mock.patch.object(awake.subprocess, "run", return_value=done):
            self.assertEqual(awake.list_sessions("claude"), [])


if __name__ == "__main__":
    unittest.main()
