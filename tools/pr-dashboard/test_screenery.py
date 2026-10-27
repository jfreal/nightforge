"""screenery.trim against a hand-built gallery shaped like Screenery's real reply.

    python -m unittest test_screenery.py
"""

import unittest

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
