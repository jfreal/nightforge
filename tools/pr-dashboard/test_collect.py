"""collect.py with gh and PowerShell stubbed: made-up comments, workflow runs and tasks.

    python -m unittest test_collect.py
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import collect
import routines

BLOCK = '{"bot": "aeo-audit", "status": "found", "summary": "3 PRs", "counts": {"prs": 3}}'


def comment(cid, body, login="jfreal", created="2026-10-28T12:53:00Z"):
    return {"id": cid, "body": body, "user": {"login": login}, "created_at": created, "updated_at": created,
            "html_url": f"https://github.com/jfreal/bot-reports/issues/1#issuecomment-{cid}"}


def fenced(text):
    return f"Done.\n\n```nightforge-report\n{text}\n```\n"


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = routines.Store(Path(self.tmp.name) / "r.db")

    def tearDown(self):
        self.tmp.cleanup()


class Inbox(Store):
    def run_inbox(self, comments):
        with mock.patch.object(collect.github, "gh", return_value=json.dumps(comments)) as gh:
            result = collect.collect_inbox(self.store, "jfreal/bot-reports", ["jfreal"])
        return result, gh

    def test_report_block_becomes_a_run_with_a_link_back(self):
        (added, problems), gh = self.run_inbox([comment(1, fenced(BLOCK))])
        self.assertEqual((added, problems), (1, []))
        run, = self.store.runs("aeo-audit")
        self.assertEqual((run["status"], run["run"], run["finished"]), ("found", "comment-1", "2026-10-28T12:53:00Z"))
        self.assertEqual(run["links"][-1]["label"], "report")
        self.assertIn("since=", gh.call_args[0][0][-1])

    def test_someone_elses_comment_is_ignored(self):
        (added, _), _ = self.run_inbox([comment(1, fenced(BLOCK), login="stranger")])
        self.assertEqual(added, 0)

    def test_bad_json_is_a_problem_not_a_crash(self):
        (added, problems), _ = self.run_inbox([comment(1, fenced("{nope")), comment(2, fenced(BLOCK))])
        self.assertEqual(added, 1)
        self.assertIn("issuecomment-1", problems[0])

    def test_cursor_moves_and_rereads_are_not_counted_twice(self):
        self.run_inbox([comment(1, fenced(BLOCK))])
        self.assertEqual(self.store.cursor("inbox:jfreal/bot-reports"), "2026-10-28T12:53:00Z")
        (added, _), gh = self.run_inbox([comment(1, fenced(BLOCK))])
        self.assertEqual(added, 0)
        self.assertIn("since=2026-10-28T12:53:00Z", gh.call_args[0][0][-1])

    def test_paginated_arrays_are_all_read(self):
        text = json.dumps([comment(1, fenced(BLOCK))]) + "\n" + json.dumps([comment(2, fenced(BLOCK))])
        with mock.patch.object(collect.github, "gh", return_value=text):
            added, _ = collect.collect_inbox(self.store, "jfreal/bot-reports", ["jfreal"])
        self.assertEqual(added, 2)


def wf_run(rid, conclusion="success", path=".github/workflows/app-insights-triage.yml"):
    return {"id": rid, "name": "App Insights triage", "path": path, "conclusion": conclusion, "run_number": rid,
            "run_started_at": "2026-10-28T06:00:00Z", "updated_at": "2026-10-28T06:03:00Z",
            "html_url": f"https://github.com/jfreal/pheidi/actions/runs/{rid}"}


class Actions(Store):
    def test_runs_map_to_one_bot_per_workflow(self):
        data = {"workflow_runs": [wf_run(2, "failure"), wf_run(1)]}
        with mock.patch.object(collect.github, "gh", return_value=json.dumps(data)):
            self.assertEqual(collect.collect_actions(self.store, "jfreal/pheidi"), 2)
        row, = self.store.board()
        self.assertEqual((row["id"], row["kind"], row["last"]["status"]), ("pheidi-app-insights-triage", "github action", "failed"))
        self.assertEqual(row["url"], "https://github.com/jfreal/pheidi/actions/workflows/app-insights-triage.yml")

    def test_skipped_counts_as_ok(self):
        self.assertEqual(collect.action_report("jfreal/pheidi", wf_run(1, "skipped"))["status"], "ok")


def task(last=1793188800, result=0, state="Ready", nxt=1793192400):
    return {"name": "CodeRabbit Sweep", "path": "\\", "state": state, "last": last, "result": result, "next": nxt}


class Windows(Store):
    def test_codes(self):
        self.assertEqual(collect.task_report(task())["status"], "ok")
        self.assertEqual(collect.task_report(task(result=1))["summary"], "Exit code 0x1")
        self.assertIsNone(collect.task_report(task(result=0x41301)), "still running")
        self.assertIsNone(collect.task_report(task(last=943920000)), "never ran (1999)")
        self.assertEqual(collect.task_report(task(result=-2147024894))["summary"], "Exit code 0x80070002")

    def test_collect_records_run_and_next_time(self):
        proc = mock.Mock(returncode=0, stdout=json.dumps([task()]).encode(), stderr=b"")
        with mock.patch.object(collect.subprocess, "run", return_value=proc) as run:
            added, problems = collect.collect_windows(self.store, ["CodeRabbit Sweep"])
        self.assertEqual((added, problems), (1, []))
        self.assertEqual(json.loads(run.call_args.kwargs["env"]["NF_TASKS"]), ["CodeRabbit Sweep"])
        self.assertEqual(run.call_args.args[0][-1], collect.PS_TASKS, "names must not follow -Command")
        row, = self.store.board()
        self.assertEqual((row["id"], row["nextAt"]), ("coderabbit-sweep", "2026-10-28T13:00:00Z"))

    def test_nothing_matched_comes_back_as_null(self):
        proc = mock.Mock(returncode=0, stdout=b"[null]", stderr=b"")
        with mock.patch.object(collect.subprocess, "run", return_value=proc):
            self.assertEqual(collect.collect_windows(self.store, ["Nope*"]), (0, []))

    def test_missing_task_is_a_problem(self):
        proc = mock.Mock(returncode=0, stdout=b'[{"name": "Nope", "error": "No MSFT_ScheduledTask objects found"}]', stderr=b"")
        with mock.patch.object(collect.subprocess, "run", return_value=proc):
            added, problems = collect.collect_windows(self.store, ["Nope"])
        self.assertEqual(added, 0)
        self.assertIn("Nope", problems[0])


class All(Store):
    def test_one_failing_source_does_not_hide_the_others(self):
        cfg = {"owners": ["jfreal"], "reportInbox": "jfreal/bot-reports", "actionsRepos": ["jfreal/pheidi"],
               "desktopTasks": False}

        def gh(args):
            if "issues/comments" in args[-1]:
                raise collect.github.GhError("HTTP 404")
            return json.dumps({"workflow_runs": [wf_run(1)]})

        with mock.patch.object(collect.github, "gh", side_effect=gh):
            status = collect.collect_all(self.store, cfg, log=lambda m: None)
        self.assertEqual([(s["source"], s["added"], bool(s["error"])) for s in status],
                         [("inbox jfreal/bot-reports", 0, True), ("actions jfreal/pheidi", 1, False)])


if __name__ == "__main__":
    unittest.main()
