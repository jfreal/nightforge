import unittest

import prune_screenery as ps


def pr(n, state, branch):
    return {"number": n, "state": state, "headRefName": branch}


class PlanTest(unittest.TestCase):
    PRS = [pr(1, "MERGED", "old-fix"), pr(2, "CLOSED", "claude/dead-end"),
           pr(3, "OPEN", "live-work"), pr(4, "OPEN", "no-shots-yet"),
           pr(5, "MERGED", "live-work")]   # a closed PR whose branch an open PR reuses

    def test_deletes_only_what_closed_prs_left(self):
        channels = [{"name": n} for n in ("pr-1", "pr-2", "pr-3", "old-fix", "claude-dead-end",
                                          "live-work", "main", "latest", "docs", "pr-99")]
        delete, missing = ps.plan(channels, self.PRS, "main")
        self.assertEqual(delete, ["pr-1", "pr-2", "old-fix", "claude-dead-end"])
        self.assertEqual(missing, [4])

    def test_never_deletes_protected_or_the_default_branch(self):
        prs = [pr(1, "MERGED", "master"), pr(2, "MERGED", "release")]
        channels = [{"name": "master"}, {"name": "release", "protected": True}, {"name": "pr-1"}]
        delete, _ = ps.plan(channels, prs, "master")
        self.assertEqual(delete, ["pr-1"])

    def test_branch_names_compare_loosely(self):
        self.assertEqual(ps.channel_name("Claude/Fix_x"), "claude-fix_x")


if __name__ == "__main__":
    unittest.main()
