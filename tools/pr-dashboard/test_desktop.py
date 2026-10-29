"""desktop.py on a made-up scheduled-tasks folder: discovery, and the footer going in and out.

    python -m unittest test_desktop.py
"""

import tempfile
import unittest
from pathlib import Path

import desktop
import routines


class Tasks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "scheduled-tasks"

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, folder, text, raw=None):
        d = self.root / folder
        d.mkdir(parents=True)
        (d / "SKILL.md").write_bytes(raw if raw is not None else text.encode("utf-8"))
        return d / "SKILL.md"


class Discover(Tasks):
    def test_name_description_and_folded_values(self):
        self.task("error-sweep-pheidi", "---\nname: Error sweep\ndescription: >\n  Nightly sweep\n  for Pheidi\n---\nRun it.\n")
        self.task("docs-sweep", "---\r\nname: 'Docs sweep'\r\ndescription: Weekly\r\n---\r\nGo.\r\n")
        found = desktop.discover(self.root)
        self.assertEqual([(t["id"], t["name"], t["description"]) for t in found],
                         [("docs-sweep", "Docs sweep", "Weekly"), ("error-sweep-pheidi", "Error sweep", "Nightly sweep for Pheidi")])

    def test_no_frontmatter_falls_back_to_folder_name(self):
        self.task("My Task", "Just a prompt.\n")
        t, = desktop.discover(self.root)
        self.assertEqual((t["id"], t["name"], t["description"]), ("my-task", "My Task", ""))

    def test_file_in_another_encoding_is_skipped(self):
        self.task("bad", "", raw="---\nname: \u201cquoted\u201d\n---\n".encode("cp1252"))
        self.task("good", "---\nname: Good\n---\nGo.\n")
        self.assertEqual([t["id"] for t in desktop.discover(self.root)], ["good"])

    def test_missing_folder_is_empty(self):
        self.assertEqual(desktop.discover(self.root / "nope"), [])


class Footer(Tasks):
    def test_install_is_idempotent_and_remove_restores_bytes(self):
        original = b"\xef\xbb\xbf---\r\nname: x\r\n---\r\nDo the thing.\r\n"
        path = self.task("x", "", raw=original)
        t, = desktop.discover(self.root)
        self.assertTrue(desktop.install(t, apply=False))
        self.assertEqual(path.read_bytes(), original, "preview writes nothing")
        self.assertTrue(desktop.install(t, apply=True, python="C:\\Python\\python.exe"))
        text = path.read_bytes()
        self.assertTrue(text.startswith(b"\xef\xbb\xbf"), "BOM kept")
        self.assertIn(b'"C:/Python/python.exe"', text)
        self.assertIn(b" report x STATUS ", text)
        self.assertNotIn(b"\n\n", text.replace(b"\r\n", b""), "line endings stay CRLF")
        t, = desktop.discover(self.root)
        self.assertTrue(t["reports"])
        self.assertFalse(desktop.install(t, apply=True, python="C:\\Python\\python.exe"), "second install is a no-op")
        self.assertEqual(path.with_name("SKILL.md.nightforge.bak").read_bytes(), original)
        self.assertTrue(desktop.remove(t, apply=True))
        self.assertEqual(path.read_bytes(), original)

    def test_install_replaces_an_older_footer(self):
        path = self.task("x", "Prompt.\n\n<!-- nightforge-report:start old -->\nold words\n<!-- nightforge-report:end -->\n")
        t, = desktop.discover(self.root)
        desktop.install(t, apply=True)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("old words", text)
        self.assertEqual(text.count(desktop.START), 1)
        self.assertTrue(text.startswith("Prompt.\n\n<!-- nightforge-report:start"))


class Collect(Tasks):
    def test_tasks_become_bots_and_silent_ones_are_named(self):
        self.task("a", "---\nname: Alpha\ndescription: first\n---\nGo.\n")
        self.task("b", "---\nname: Beta\n---\nGo.\n")
        desktop.install(desktop.discover(self.root)[0], apply=True)
        store = routines.Store(Path(self.tmp.name) / "r.db")
        added, problems = desktop.collect(store, self.root)
        self.assertEqual(added, 0)
        self.assertEqual(len(problems), 1)
        self.assertIn("1 desktop task(s) never report", problems[0])
        self.assertIn("(b)", problems[0])
        rows = {r["id"]: r for r in store.board()}
        self.assertEqual((rows["a"]["name"], rows["a"]["kind"], rows["a"]["description"]), ("Alpha", "desktop task", "first"))


if __name__ == "__main__":
    unittest.main()
