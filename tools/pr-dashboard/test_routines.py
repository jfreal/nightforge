"""routines.py: report checks, durations, the late/due rules and the store, on a temp database.

    python -m unittest test_routines.py
"""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import routines
from routines import ReportError, iso, parse_time

NOW = parse_time("2026-10-28T12:00:00Z")


def report(**over):
    r = {"bot": "notion-pickup", "status": "found", "summary": "2 PRs opened", "finished": "2026-10-28T11:00:00Z"}
    r.update(over)
    return r


class CleanReport(unittest.TestCase):
    def test_good_report_keeps_its_fields(self):
        r = routines.clean_report(report(counts={"prs": 2}, links=[{"label": "#655", "url": "https://github.com/x/y/pull/655"}]))
        self.assertEqual((r["bot"], r["status"], r["counts"]), ("notion-pickup", "found", {"prs": 2}))
        self.assertEqual(r["links"][0]["label"], "#655")
        self.assertEqual(r["finished"], "2026-10-28T11:00:00Z")

    def test_unknown_status_names_the_choices(self):
        with self.assertRaisesRegex(ReportError, "status: 'done'"):
            routines.clean_report(report(status="done"))

    def test_bad_bot_id_refused(self):
        with self.assertRaisesRegex(ReportError, "bot:"):
            routines.clean_report(report(bot="Notion Pickup"))

    def test_javascript_link_refused(self):
        # The page puts links in href, so only http(s) may be stored.
        with self.assertRaisesRegex(ReportError, "links:"):
            routines.clean_report(report(links=[{"label": "x", "url": "javascript:alert(1)"}]))

    def test_bare_string_link_is_its_own_label(self):
        r = routines.clean_report(report(links=["https://example.com/a"]))
        self.assertEqual(r["links"], [{"label": "https://example.com/a", "url": "https://example.com/a"}])

    def test_fractional_or_text_count_refused(self):
        for bad in (1.5, "2", True):
            with self.subTest(bad=bad), self.assertRaisesRegex(ReportError, "counts.prs"):
                routines.clean_report(report(counts={"prs": bad}))

    def test_long_summary_cut_and_whitespace_folded(self):
        r = routines.clean_report(report(summary="a\n  b " + "x" * 400))
        self.assertTrue(r["summary"].startswith("a b x"))
        self.assertEqual(len(r["summary"]), routines.MAX_SUMMARY)

    def test_run_id_defaults_to_finish_time(self):
        self.assertEqual(routines.clean_report(report())["run"], "2026-10-28T11:00:00Z")


class Durations(unittest.TestCase):
    def test_forms(self):
        cases = {"15m": 15, "2h": 120, "1d": 1440, "1d12h": 2160, "1w": 10080, "90": 90, 45: 45, "": None, None: None}
        for text, mins in cases.items():
            with self.subTest(text=text):
                self.assertEqual(routines.parse_minutes(text), mins)

    def test_nonsense_refused(self):
        with self.assertRaises(ReportError):
            routines.parse_minutes("soon")

    def test_times(self):
        self.assertEqual(iso(parse_time("2026-10-28T08:00:00-04:00")), "2026-10-28T12:00:00Z")
        self.assertEqual(iso(parse_time(1793188800)), "2026-10-28T12:00:00Z")
        self.assertEqual(iso(parse_time("2026-10-28T12:00:00")), "2026-10-28T12:00:00Z")


def bot(**over):
    b = {"every_minutes": 60, "grace_minutes": 30, "paused": 0, "created_at": "2026-10-01T00:00:00Z"}
    b.update(over)
    return b


def last(mins_ago, status="ok"):
    return {"finished": iso(NOW - timedelta(minutes=mins_ago)), "status": status}


class Health(unittest.TestCase):
    def test_timing_by_age(self):
        cases = [(30, "ontime"), (75, "due"), (95, "late")]
        for ago, timing in cases:
            with self.subTest(ago=ago):
                self.assertEqual(routines.health(bot(), last(ago), NOW)["timing"], timing)

    def test_late_is_broken_and_counts_overdue_minutes(self):
        h = routines.health(bot(), last(100), NOW)
        self.assertEqual((h["lane"], h["overdueMinutes"]), ("broken", 10))

    def test_needs_you_outranks_on_time(self):
        self.assertEqual(routines.health(bot(), last(5, "needs-you"), NOW)["lane"], "you")

    def test_failed_on_time_is_still_broken(self):
        self.assertEqual(routines.health(bot(), last(5, "failed"), NOW)["lane"], "broken")

    def test_never_reported_counts_from_when_it_was_added(self):
        self.assertEqual(routines.health(bot(), None, NOW)["timing"], "never")
        fresh = bot(created_at=iso(NOW - timedelta(minutes=10)))
        h = routines.health(fresh, None, NOW)
        self.assertEqual((h["timing"], h["lane"]), ("waiting", "healthy"))

    def test_paused_never_alarms(self):
        h = routines.health(bot(paused=1), last(10_000, "failed"), NOW)
        self.assertEqual(h["lane"], "paused")

    def test_no_cadence_never_late(self):
        h = routines.health(bot(every_minutes=None), last(10_000), NOW)
        self.assertEqual((h["timing"], h["lane"]), ("unscheduled", "healthy"))

    def test_guessed_cadence_used_and_flagged(self):
        h = routines.health(bot(every_minutes=None, grace_minutes=None), last(200), NOW, guessed=60)
        self.assertEqual((h["timing"], h["every"], h["everyGuessed"], h["grace"]), ("late", 60, True, 30))

    def test_default_grace_is_half_within_bounds(self):
        self.assertEqual(routines.default_grace(10), 15)
        self.assertEqual(routines.default_grace(120), 60)
        self.assertEqual(routines.default_grace(10080), 240)


class Guess(unittest.TestCase):
    def test_median_gap(self):
        times = [iso(NOW - timedelta(hours=h)) for h in (0, 1, 2, 3, 10)]
        self.assertEqual(routines.guess_every(times), 60)

    def test_two_runs_is_not_enough(self):
        self.assertIsNone(routines.guess_every([iso(NOW), iso(NOW - timedelta(hours=1))]))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = routines.Store(Path(self.tmp.name) / "sub" / "r.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_run_twice_is_stored_once(self):
        self.assertTrue(self.store.add_run(report(run="a"), "cli"))
        self.assertFalse(self.store.add_run(report(run="a", summary="changed"), "cli"))
        self.assertEqual([r["summary"] for r in self.store.runs("notion-pickup")], ["2 PRs opened"])

    def test_board_rows_carry_last_run_counts_and_strip(self):
        for i, status in enumerate(["ok", "found", "failed"]):
            self.store.add_run(report(run=str(i), status=status, finished=iso(NOW - timedelta(hours=3 - i)),
                                      counts={"prs": i}), "cli")
        row, = self.store.board(NOW)
        self.assertEqual(row["last"]["status"], "failed")
        self.assertEqual([s["status"] for s in row["strip"]], ["ok", "found", "failed"])
        self.assertEqual((row["runsTotal"], row["runs7d"], row["counts7d"]), (3, 3, {"prs": 3}))
        self.assertEqual(row["health"]["lane"], "broken")

    def test_set_bot_wins_over_a_collector_fill(self):
        self.store.set_bot("ci", name="CI nightly", every_minutes="1d")
        self.store.add_run(report(bot="ci", name="ignored"), "actions", kind="github action", url="https://x")
        b, = self.store.board(NOW)
        self.assertEqual((b["name"], b["kind"], b["url"], b["health"]["every"]), ("CI nightly", "github action", "https://x", 1440))

    def test_board_is_worst_first(self):
        self.store.set_bot("healthy", every_minutes="1h")
        self.store.add_run(report(bot="healthy", status="ok", finished=iso(NOW - timedelta(minutes=5))), "cli")
        self.store.set_bot("asks", every_minutes="1h")
        self.store.add_run(report(bot="asks", status="needs-you", finished=iso(NOW - timedelta(minutes=5))), "cli")
        self.store.set_bot("quiet", every_minutes="1h")
        self.store.add_run(report(bot="quiet", status="ok", finished=iso(NOW - timedelta(days=2))), "cli")
        self.assertEqual([r["id"] for r in self.store.board(NOW)], ["asks", "quiet", "healthy"])

    def test_remove_drops_runs_too(self):
        self.store.add_run(report(), "cli")
        self.assertTrue(self.store.remove_bot("notion-pickup"))
        self.assertEqual((self.store.board(NOW), self.store.runs("notion-pickup")), ([], []))

    def test_cursor_round_trip(self):
        self.assertIsNone(self.store.cursor("inbox:x/y"))
        self.store.set_cursor("inbox:x/y", "2026-10-28T00:00:00Z")
        self.store.set_cursor("inbox:x/y", "2026-10-29T00:00:00Z")
        self.assertEqual(self.store.cursor("inbox:x/y"), "2026-10-29T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
