"""Rules in github.py, run against hand-built PR nodes shaped like the real GraphQL reply.

The nodes are made up on purpose: this repo is public and real PR bodies from private
repos must not land in it.

    python -m unittest test_github.py
"""

import json
import subprocess
import unittest
from unittest import mock

import github

HEAD = "a" * 40


def node(**over):
    n = {
        "number": 7, "title": "Add a thing", "url": "https://github.com/jfreal/demo/pull/7",
        "body": "Adds a thing.", "createdAt": "2026-10-20T10:00:00Z", "isDraft": False,
        "state": "OPEN", "headRefOid": HEAD, "headRefName": "thing", "baseRefName": "main",
        "repository": {"name": "demo", "nameWithOwner": "jfreal/demo", "mergeCommitAllowed": False,
                       "squashMergeAllowed": True, "rebaseMergeAllowed": False},
        "mergeable": "MERGEABLE", "additions": 10, "deletions": 2, "changedFiles": 1,
        "labels": {"nodes": []},
        "commits": {"nodes": [{"commit": {"oid": HEAD, "statusCheckRollup": {"state": "SUCCESS"}}}]},
        "reviews": {"nodes": [{"author": {"login": "coderabbitai"}, "state": "COMMENTED",
                               "submittedAt": "2026-10-20T11:00:00Z", "commit": {"oid": HEAD}}]},
        "reviewThreads": {"nodes": []},
        "comments": {"totalCount": 0},
        "files": {"nodes": [{"path": "src/thing.py", "additions": 10, "deletions": 2}]},
    }
    n.update(over)
    return n


def labels(*names):
    return {"nodes": [{"name": x} for x in names]}


class Lanes(unittest.TestCase):
    def test_clean_reviewed_pr_is_ready(self):
        row = github.build_row(node())
        self.assertEqual(row["lane"], "ready")
        self.assertEqual(row["blockers"], [])

    def test_draft_wins_over_everything(self):
        self.assertEqual(github.build_row(node(isDraft=True, mergeable="CONFLICTING"))["lane"], "draft")

    def test_conflict_needs_a_fix(self):
        row = github.build_row(node(mergeable="CONFLICTING"))
        self.assertEqual(row["lane"], "fix")
        self.assertIn({"text": "Merge conflict", "hard": True}, row["blockers"])

    def test_failed_checks_need_a_fix(self):
        failed = {"nodes": [{"commit": {"oid": HEAD, "statusCheckRollup": {"state": "FAILURE"}}}]}
        self.assertEqual(github.build_row(node(commits=failed))["lane"], "fix")

    def test_verify_failed_label_needs_a_fix(self):
        self.assertEqual(github.build_row(node(labels=labels("verify-failed")))["lane"], "fix")

    def test_unknown_mergeable_waits(self):
        row = github.build_row(node(mergeable="UNKNOWN"))
        self.assertEqual(row["conflict"], "checking")
        self.assertEqual(row["lane"], "wait")

    def test_running_checks_wait(self):
        running = {"nodes": [{"commit": {"oid": HEAD, "statusCheckRollup": {"state": "PENDING"}}}]}
        self.assertEqual(github.build_row(node(commits=running))["lane"], "wait")


class Review(unittest.TestCase):
    def test_review_of_an_older_commit_is_stale(self):
        old = {"nodes": [{"author": {"login": "coderabbitai"}, "state": "COMMENTED",
                          "submittedAt": "x", "commit": {"oid": "b" * 40}}]}
        row = github.build_row(node(reviews=old))
        self.assertEqual(row["review"], "stale")
        self.assertEqual(row["lane"], "wait")

    def test_no_bot_review_is_pending(self):
        human = {"nodes": [{"author": {"login": "jfreal"}, "state": "COMMENTED",
                            "submittedAt": "x", "commit": {"oid": HEAD}}]}
        self.assertEqual(github.build_row(node(reviews=human))["review"], "pending")

    def test_verified_label_stands_in_for_review(self):
        row = github.build_row(node(reviews={"nodes": []}, labels=labels("verified")))
        self.assertEqual(row["lane"], "ready")
        self.assertEqual(row["blockers"], [])

    def test_threads_past_the_first_page_still_mean_changes_asked(self):
        threads = {"pageInfo": {"hasNextPage": True}, "nodes": [{"isResolved": True, "isOutdated": False}]}
        row = github.build_row(node(reviewThreads=threads))
        self.assertEqual(row["review"], "changes")
        self.assertEqual(row["lane"], "fix")
        self.assertIn({"text": "Over 100 review threads, check GitHub", "hard": True}, row["blockers"])

    def test_open_threads_mean_changes_asked(self):
        threads = {"nodes": [{"isResolved": False, "isOutdated": False},
                             {"isResolved": True, "isOutdated": False},
                             {"isResolved": False, "isOutdated": True}]}
        row = github.build_row(node(reviewThreads=threads))
        self.assertEqual(row["review"], "changes")
        self.assertIn({"text": "1 open review comment", "hard": True}, row["blockers"])


def rollup(*contexts):
    return {"nodes": [{"commit": {"oid": HEAD, "statusCheckRollup": {"state": "SUCCESS", "contexts": {"nodes": list(contexts)}}}}]}


CI_OK = {"__typename": "CheckRun", "name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}


def claude_status(state):
    return {"__typename": "StatusContext", "context": "claude-review", "state": state}


class ClaudeReview(unittest.TestCase):
    no_bot = {"nodes": []}

    def test_passed_claude_review_counts_as_reviewed(self):
        row = github.build_row(node(reviews=self.no_bot, commits=rollup(CI_OK, claude_status("SUCCESS"))))
        self.assertEqual(row["review"], "done")
        self.assertEqual(row["lane"], "ready")

    def test_failed_claude_review_needs_a_fix_but_checks_stay_green(self):
        row = github.build_row(node(reviews=self.no_bot, commits=rollup(CI_OK, claude_status("FAILURE"))))
        self.assertEqual(row["checks"], "pass")
        self.assertEqual(row["lane"], "fix")
        self.assertIn({"text": "Claude review found problems", "hard": True}, row["blockers"])

    def test_claude_review_alone_is_not_a_check(self):
        row = github.build_row(node(commits=rollup(claude_status("SUCCESS"))))
        self.assertEqual(row["checks"], "none")

    def test_failed_or_running_ci_contexts(self):
        failed = {**CI_OK, "conclusion": "TIMED_OUT"}
        running = {"__typename": "CheckRun", "name": "ci", "status": "IN_PROGRESS", "conclusion": None}
        self.assertEqual(github.build_row(node(commits=rollup(failed)))["checks"], "fail")
        self.assertEqual(github.build_row(node(commits=rollup(running)))["checks"], "running")

    def test_no_review_says_so(self):
        row = github.build_row(node(reviews=self.no_bot))
        self.assertIn({"text": "No review yet", "hard": False}, row["blockers"])


class Risk(unittest.TestCase):
    def files(self, *paths):
        return {"nodes": [{"path": p, "additions": 1, "deletions": 0} for p in paths]}

    def test_migration_is_high(self):
        row = github.build_row(node(files=self.files("supabase/migrations/2026_drop.sql")))
        self.assertEqual(row["risk"]["level"], "high")

    def test_workflow_is_high(self):
        row = github.build_row(node(files=self.files(".github/workflows/ci.yml")))
        self.assertEqual(row["risk"]["reasons"], ["changes CI workflows"])

    def test_docs_only_is_low_even_when_big(self):
        row = github.build_row(node(additions=900, files=self.files("docs/a.md", "README.md")))
        self.assertEqual(row["risk"], {"level": "low", "reasons": ["docs only"]})

    def test_many_lines_is_medium(self):
        self.assertEqual(github.build_row(node(additions=400))["risk"]["level"], "med")

    def test_config_is_medium(self):
        row = github.build_row(node(files=self.files("package.json")))
        self.assertEqual(row["risk"], {"level": "med", "reasons": ["changes config: package.json"]})

    def test_warning_in_body_is_high(self):
        body = "> [!WARNING]\n> **Do not apply** until it settles.\n\nDrops a schema."
        row = github.build_row(node(body=body))
        self.assertEqual(row["risk"]["level"], "high")
        self.assertEqual(row["warning"], "Do not apply until it settles.")


class Describe(unittest.TestCase):
    def test_strips_coderabbit_block_footer_and_ticket_line(self):
        body = ("Notion ticket: https://example.com/t\n\n"
                "Trims **repeated** names from the [guides](https://x.y/z).\n\n"
                "---\n_Generated by [Claude Code](https://claude.ai/code/session_1)_\n\n"
                "<!-- This is an auto-generated comment: release notes by coderabbit.ai -->\n"
                "## Summary by CodeRabbit\n* stuff\n"
                "<!-- end of auto-generated comment: release notes by coderabbit.ai -->")
        d = github.describe(body)
        self.assertEqual(d["summary"], "Trims repeated names from the guides.")
        self.assertNotIn("CodeRabbit", d["description"])
        self.assertNotIn("Generated by", d["description"])

    def test_strips_html_tags_but_not_generics(self):
        d = github.describe("Bumps <a href=\"https://x\">lib</a> to 2.0 for List<string> users.")
        self.assertEqual(d["summary"], "Bumps lib to 2.0 for List<string> users.")

    def test_empty_body(self):
        self.assertEqual(github.describe(None)["summary"], "")


class Misc(unittest.TestCase):
    def test_merge_method_follows_repo_settings(self):
        repo = {"name": "r", "nameWithOwner": "o/r", "mergeCommitAllowed": True,
                "squashMergeAllowed": False, "rebaseMergeAllowed": True}
        self.assertEqual(github.build_row(node(repository=repo))["mergeMethod"], "merge")

    def test_rows_sort_oldest_first_and_skip_excluded(self):
        a = node(number=1, createdAt="2026-10-22T00:00:00Z")
        b = node(number=2, createdAt="2026-10-01T00:00:00Z")
        skipped = node(number=3, repository={**node()["repository"], "name": "archive"})
        rows = github.build_rows([a, b, skipped], exclude_repos=["Archive"])
        self.assertEqual([r["num"] for r in rows], [2, 1])

    def test_areas_group_by_top_folder(self):
        files = {"nodes": [{"path": p, "additions": 1, "deletions": 0}
                           for p in ("src/a.py", "src/b.py", "README.md")]}
        areas = github.build_row(node(files=files))["areas"]
        self.assertEqual([(a["name"], a["count"]) for a in areas], [("src/", 2), ("(root)", 1)])


class GhCalls(unittest.TestCase):
    def test_timeout_becomes_gh_error(self):
        with mock.patch.object(github.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 90)):
            with self.assertRaisesRegex(github.GhError, "did not answer"):
                github.gh(["api", "user"])

    def test_missing_gh_becomes_gh_error(self):
        with mock.patch.object(github.subprocess, "run", side_effect=FileNotFoundError("gh")):
            with self.assertRaisesRegex(github.GhError, "could not run gh"):
                github.gh(["api", "user"])

    def test_auth_failure_carries_gh_message(self):
        failed = subprocess.CompletedProcess(["gh"], 4, b"", b"To get started with GitHub CLI, please run: gh auth login")
        with mock.patch.object(github.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(github.GhError, "gh auth login"):
                github.gh(["api", "user"])

    def test_second_page_passes_the_cursor_as_a_string(self):
        page = lambda more, cursor, n: json.dumps({"data": {"search": {
            "pageInfo": {"hasNextPage": more, "endCursor": cursor}, "nodes": [node(number=n)]}}})
        with mock.patch.object(github, "gh", side_effect=[page(True, "CUR1", 1), page(False, None, 2)]) as gh:
            nodes = github.fetch_nodes(["jfreal"])
        self.assertEqual([n["number"] for n in nodes], [1, 2])
        second = gh.call_args_list[1].args[0]
        self.assertIn("after=CUR1", second)
        self.assertEqual(second[second.index("after=CUR1") - 1], "-f")


if __name__ == "__main__":
    unittest.main()
