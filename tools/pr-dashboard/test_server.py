"""server.merge's gates, with GitHub stubbed out: the re-fetch and the `gh` call.

    python -m unittest test_server.py
"""

import unittest
from unittest import mock

import server

CFG = {**server.DEFAULTS, "owners": ["jfreal"]}
HEAD = "a" * 40


def row(**over):
    r = {"key": "jfreal/demo#7", "headSha": HEAD, "draft": False, "conflict": "no", "checks": "pass",
         "mergeMethod": "squash", "blockers": []}
    r.update(over)
    return r


class HeadGate(unittest.TestCase):
    def merge(self, fresh, sent_sha=HEAD):
        with mock.patch.object(server.github, "fetch_row", return_value=fresh), \
             mock.patch.object(server.github, "gh", return_value="") as gh:
            result = server.merge(CFG, {"key": "jfreal/demo#7", "headSha": sent_sha}, dry_run=False)
        return result, gh

    def test_new_push_since_page_loaded_refuses_without_calling_gh(self):
        result, gh = self.merge(row(headSha="b" * 40))
        self.assertFalse(result["ok"])
        self.assertIn("someone pushed", result["error"])
        gh.assert_not_called()

    def test_matching_head_merges_with_that_sha(self):
        result, gh = self.merge(row())
        self.assertTrue(result["ok"])
        gh.assert_called_once_with(["pr", "merge", "7", "-R", "jfreal/demo", "--squash",
                                    "--match-head-commit", HEAD])


class OtherGates(unittest.TestCase):
    def refused(self, **over):
        with mock.patch.object(server.github, "fetch_row", return_value=row(**over)), \
             mock.patch.object(server.github, "gh") as gh:
            result = server.merge(CFG, {"key": "jfreal/demo#7", "headSha": HEAD}, dry_run=False)
        gh.assert_not_called()
        return result

    def test_verify_failed_refuses(self):
        result = self.refused(blockers=[{"text": "Verify failed", "hard": True}])
        self.assertEqual(result["error"], "Not merged: verify failed.")

    def test_conflict_refuses(self):
        self.assertIn("merge conflict", self.refused(conflict="yes")["error"])

    def test_unknown_owner_refuses_before_fetching(self):
        with mock.patch.object(server.github, "fetch_row") as fetch:
            result = server.merge(CFG, {"key": "someone/demo#7", "headSha": HEAD}, dry_run=False)
        self.assertFalse(result["ok"])
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
