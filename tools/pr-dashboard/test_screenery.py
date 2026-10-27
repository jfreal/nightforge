"""screenery.trim against a hand-built gallery shaped like Screenery's real reply.

    python -m unittest test_screenery.py
"""

import unittest
import urllib.error
from unittest import mock

import screenery


def asset(name, status, before=True, ignored=False):
    side = lambda v: {"version": v, "url": f"https://cdn.test/{name}@{v}.png", "width": 1440, "height": 900}
    return {"name": name, "label": name.split("/")[-1], "status": status, "to": side("v_new"),
            "from": side("v_old") if before else None, "ignored": ignored, "flaky": None}


DOC = {
    "subject": {"html_url": "https://app.test/jfreal/demo/channels?channel=pr-7"},
    "summary": {"total": 4, "changed": 1, "added": 1, "unchanged": 1},
    "groups": [
        {"name": "app", "label": "app", "assets": [asset("app/a", "unchanged"), asset("app/b", "changed")]},
        {"name": "marketing", "label": "marketing", "assets": [
            asset("marketing/c", "added", before=False), asset("marketing/d", "changed", ignored=True)]},
    ],
}


class Paging(unittest.TestCase):
    def setUp(self):
        screenery._cache.clear()

    def pages(self, n, more_after_last=False):
        out = []
        for i in range(n):
            last = i == n - 1
            out.append({**DOC, "groups": [{"name": f"g{i}", "label": f"g{i}", "assets": [asset(f"g{i}/a", "changed")]}],
                        "page": {"has_more": (not last) or more_after_last, "next_cursor": f"c{i + 1}"}})
        return out

    def test_follows_every_cursor(self):
        with mock.patch.object(screenery, "_get", side_effect=self.pages(3)) as get:
            got = screenery.gallery("jfreal/demo", 7)
        self.assertEqual(len(got["assets"]), 3)
        self.assertFalse(got["incomplete"])
        self.assertTrue(get.call_args_list[2].args[0].endswith("?cursor=c2"))

    def test_safety_stop_marks_incomplete(self):
        with mock.patch.object(screenery, "MAX_PAGES", 2), \
             mock.patch.object(screenery, "_get", side_effect=self.pages(2, more_after_last=True)):
            self.assertTrue(screenery.gallery("jfreal/demo", 7)["incomplete"])

    def test_miss_is_asked_again_after_a_minute(self):
        miss = urllib.error.HTTPError("u", 404, "nf", {}, None)
        with mock.patch.object(screenery, "_get", side_effect=[miss, *self.pages(1)]), \
             mock.patch.object(screenery.time, "time", side_effect=[0, 61, 61]):
            self.assertFalse(screenery.gallery("jfreal/demo", 7)["ok"])
            self.assertTrue(screenery.gallery("jfreal/demo", 7)["ok"])


class Trim(unittest.TestCase):
    def test_changed_first_then_added_then_unchanged(self):
        got = screenery.trim(DOC)
        self.assertEqual([a["name"] for a in got["assets"]], ["app/b", "marketing/c", "app/a"])

    def test_ignored_assets_are_dropped(self):
        self.assertNotIn("marketing/d", [a["name"] for a in screenery.trim(DOC)["assets"]])

    def test_new_asset_has_no_before_image(self):
        added = next(a for a in screenery.trim(DOC)["assets"] if a["status"] == "added")
        self.assertIsNone(added["from"])
        self.assertEqual(added["to"]["url"], "https://cdn.test/marketing/c@v_new.png")

    def test_keeps_link_and_summary(self):
        got = screenery.trim(DOC)
        self.assertEqual(got["url"], DOC["subject"]["html_url"])
        self.assertEqual(got["summary"]["changed"], 1)


if __name__ == "__main__":
    unittest.main()
