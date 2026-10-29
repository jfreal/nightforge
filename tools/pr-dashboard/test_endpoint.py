"""endpoint.py and botlog's endpoint commands, with the Worker's answers stubbed.

    python -m unittest test_endpoint.py
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import botlog
import endpoint
import routines

URL, TOKEN = "https://job-reports.example.workers.dev", "admin"


def job(**over):
    j = {"id": "frogdb-daily", "name": "FrogDB daily feature", "every": 1440, "grace": None, "does": "Ships one feature",
         "runsOn": "cloud routine", "schedule": "8:41 ET", "url": "", "paused": False, "registered": True}
    j.update(over)
    return j


def run(seq, **over):
    r = {"seq": seq, "job": "frogdb-daily", "run": f"r{seq}", "finished": f"2026-10-2{seq}T12:00:00Z",
         "status": "found", "summary": f"run {seq}", "counts": {"prs": 1}, "links": [], "details": "", "source": "x"}
    r.update(over)
    return r


class Settings(unittest.TestCase):
    def test_off_without_url(self):
        self.assertIsNone(endpoint.settings({}))

    def test_needs_https_and_a_token(self):
        with self.assertRaisesRegex(endpoint.EndpointError, "https"):
            endpoint.settings({"reportUrl": "http://example.com", "reportToken": "x"})
        with mock.patch.dict("os.environ", {"NF_ADMIN_TOKEN": ""}), self.assertRaisesRegex(endpoint.EndpointError, "token"):
            endpoint.settings({"reportUrl": URL})

    def test_lookalike_local_hosts_are_not_local(self):
        for bad in ("http://localhost.example.com", "http://127.0.0.1.example.com", "ftp://localhost"):
            with self.subTest(url=bad), self.assertRaisesRegex(endpoint.EndpointError, "https"):
                endpoint.settings({"reportUrl": bad, "reportToken": "x"})
        self.assertEqual(endpoint.settings({"reportUrl": "http://localhost:8787", "reportToken": "x"})[0],
                         "http://localhost:8787")

    def test_token_from_environment(self):
        with mock.patch.dict("os.environ", {"NF_ADMIN_TOKEN": "env"}):
            self.assertEqual(endpoint.settings({"reportUrl": URL + "/"}), (URL, "env"))


class Collect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = routines.Store(Path(self.tmp.name) / "r.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_pages_until_done_and_keeps_the_cursor(self):
        pages = [{"jobs": [job()], "runs": [run(1), run(2)], "next": 2, "more": True},
                 {"jobs": [job()], "runs": [run(3)], "next": 3, "more": False}]
        with mock.patch.object(endpoint, "call", side_effect=pages) as call:
            self.assertEqual(endpoint.collect_into(self.store, URL, TOKEN), 3)
        self.assertEqual([c.args[3] for c in call.call_args_list], ["/sync?after=0", "/sync?after=2"])
        self.assertEqual(self.store.cursor(f"endpoint:{URL}"), "3")
        row, = self.store.board()
        self.assertEqual((row["name"], row["kind"], row["health"]["every"], row["runsTotal"]),
                         ("FrogDB daily feature", "cloud routine", 1440, 3))

    def test_endpoint_registration_overwrites_the_local_copy(self):
        self.store.set_bot("frogdb-daily", name="old", every_minutes="1h", paused=True)
        with mock.patch.object(endpoint, "call", return_value={"jobs": [job()], "runs": [], "next": 0}):
            endpoint.collect_into(self.store, URL, TOKEN)
        row, = self.store.board()
        self.assertEqual((row["name"], row["health"]["every"], row["paused"]), ("FrogDB daily feature", 1440, False))

    def test_job_deleted_on_the_endpoint_goes_here_too(self):
        self.store.set_bot("from-elsewhere", name="local only")
        two = {"jobs": [job(), job(id="old", name="Old")], "runs": [], "next": 0}
        one = {"jobs": [job()], "runs": [], "next": 0}
        with mock.patch.object(endpoint, "call", side_effect=[two, one]):
            endpoint.collect_into(self.store, URL, TOKEN)
            endpoint.collect_into(self.store, URL, TOKEN)
        self.assertEqual(sorted(r["id"] for r in self.store.board()), ["frogdb-daily", "from-elsewhere"])

    def test_unregistered_job_is_marked(self):
        page = {"jobs": [job(id="mystery", name="mystery", registered=False, runsOn="", every=None)],
                "runs": [run(1, job="mystery")], "next": 1}
        with mock.patch.object(endpoint, "call", return_value=page):
            endpoint.collect_into(self.store, URL, TOKEN)
        row, = self.store.board()
        self.assertEqual(row["kind"], "not registered")


class Botlog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text(f'{{"reportUrl": "{URL}", "reportToken": "{TOKEN}", "routinesDb": "{Path(self.tmp.name).as_posix()}/r.db"}}')
        self.args = ["--config", str(cfg)]

    def tearDown(self):
        self.tmp.cleanup()

    def test_report_goes_to_the_endpoint_not_the_local_file(self):
        with mock.patch.object(endpoint, "report", return_value={"ok": True, "job": "x", "duplicate": False}) as send:
            self.assertEqual(botlog.main(self.args + ["report", "x", "ok", "fine", "--count", "prs=2"]), 0)
        (url, token, body), _ = send.call_args
        self.assertEqual((url, token, body["job"], body["counts"], body["source"]), (URL, TOKEN, "x", {"prs": 2}, "botlog"))
        self.assertEqual(routines.Store(Path(self.tmp.name) / "r.db").board(), [])

    def test_bad_report_is_refused_before_sending(self):
        with mock.patch.object(endpoint, "report") as send:
            self.assertEqual(botlog.main(self.args + ["report", "x", "ok", "--link", "javascript:1"]), 2)
        send.assert_not_called()

    def test_local_flag_skips_the_endpoint(self):
        with mock.patch.object(endpoint, "report") as send:
            self.assertEqual(botlog.main(self.args + ["report", "x", "ok", "--local"]), 0)
        send.assert_not_called()
        self.assertEqual(len(routines.Store(Path(self.tmp.name) / "r.db").board()), 1)

    def test_remote_report_works_without_a_usable_local_database(self):
        args = ["--config", self.args[1], "--db", str(Path(self.tmp.name) / "no" / "such" / "dir" / "x" / "\0bad.db")]
        with mock.patch.object(endpoint, "report", return_value={"ok": True, "job": "x"}):
            self.assertEqual(botlog.main(args + ["report", "x", "ok"]), 0)

    def test_local_json_report_with_only_job(self):
        f = Path(self.tmp.name) / "r.json"
        f.write_text('{"job": "x", "status": "ok"}')
        self.assertEqual(botlog.main(self.args + ["report", "--json", str(f), "--local"]), 0)

    def test_broken_config_is_an_error_not_a_traceback(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text("{nope")
        self.assertEqual(botlog.main(["--config", str(bad), "list"]), 2)

    def test_desktop_preview_writes_nothing(self):
        d = Path(self.tmp.name) / "tasks" / "a"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: A\n---\nGo.\n")
        botlog.main(self.args + ["desktop", "--dir", str(d.parent)])
        self.assertFalse((Path(self.tmp.name) / "r.db").exists())
        botlog.main(self.args + ["desktop", "--dir", str(d.parent), "--apply"])
        self.assertEqual([r["id"] for r in routines.Store(Path(self.tmp.name) / "r.db").board()], ["a"])

    def test_job_add_sends_only_the_given_fields(self):
        with mock.patch.object(endpoint, "register", return_value={"job": job()}) as reg:
            botlog.main(self.args + ["job", "add", "frogdb-daily", "--every", "1d", "--runs-on", "cloud routine"])
        self.assertEqual(reg.call_args.args, (URL, TOKEN, "frogdb-daily", {"every": "1d", "runsOn": "cloud routine"}))


if __name__ == "__main__":
    unittest.main()
