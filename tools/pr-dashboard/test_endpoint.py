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

    def test_job_add_sends_only_the_given_fields(self):
        with mock.patch.object(endpoint, "register", return_value={"job": job()}) as reg:
            botlog.main(self.args + ["job", "add", "frogdb-daily", "--every", "1d", "--runs-on", "cloud routine"])
        self.assertEqual(reg.call_args.args, (URL, TOKEN, "frogdb-daily", {"every": "1d", "runsOn": "cloud routine"}))


if __name__ == "__main__":
    unittest.main()
