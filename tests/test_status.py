import json
import os
import tempfile
import unittest
from pathlib import Path

from abacus_infra import status

PAGES = {  # decisions-page shapes the orchestrator actually wrote in runs 2-7
    "run2_card": ("## NEEDS YOU (1 of cap 5)\n\n### repo-b-759: the worker can't finish\n"
                  "**Context.** ...\n\n## DECIDED\n", 1, None),
    "run4_inline": ("## NEEDS YOU 1: follow-up beads for issues coder:repo-b reported\n"
                    "Context: ...\n\n## DECIDED\n", 1, None),
    "run5_none": ("## NEEDS YOU\nNone open (cap 5).\n\n## DECIDED\n\n"
                  "## Program status: COMPLETE\n- a\n", 0, "COMPLETE"),
    "run6_none": ("## NEEDS YOU\nNone.\n\n## DECIDED\n## Program status\nCOMPLETE\n- repo-a\n", 0, "COMPLETE"),
    "running": ("## NEEDS YOU\nNone.\n\n## Program status\n**RUNNING**: repo-a in flight\n", 0, "RUNNING"),
}


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ABACUS_HOME"] = self.tmp.name
        os.environ.pop("ABACUS_LOGBOOK_DB", None)
        self.page = Path(self.tmp.name) / "agents" / "orchestrator" / "pages" / "decisions.md"
        self.page.parent.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_decisions_page_shapes(self):
        for name, (text, n_cards, prog) in PAGES.items():
            self.page.write_text(text)
            got_status, cards = status.decisions()
            self.assertEqual(len(cards), n_cards, name)
            self.assertEqual(got_status, prog, name)

    def test_line_says_nothing_when_abacus_is_not_set_up(self):
        self.assertEqual(status.line(), "")

    def test_line_leads_with_what_needs_the_operator(self):
        self.page.write_text(PAGES["run2_card"][0])
        self.assertEqual(status.line(), "abacus · 1 needs you")
        self.page.write_text(PAGES["run6_none"][0])
        self.assertEqual(status.line(), "abacus · complete")

    def test_line_counts_in_flight_dispatches(self):
        from abacus_infra import logbook as lb
        conn = lb.connect()  # resolves (and creates) $ABACUS_HOME/state/logbook.db
        lb.worker_upsert(conn, "coder:repo-a", "/w/a")
        lb.dispatch_add(conn, "a-1", "/w/a", "coder:repo-a")
        conn.close()
        self.page.write_text(PAGES["running"][0])
        self.assertEqual(status.line(), "abacus · 1 in flight")

    def test_full_view_explains_who_stopped_a_session(self):
        from abacus_infra import logbook as lb
        conn = lb.connect()
        lb.session_event(conn, "coder:repo-a", "stopped", "agent:orchestrator", "program complete")
        conn.close()
        self.assertIn("coder:repo-a stopped by agent:orchestrator: program complete", status.full())


if __name__ == "__main__":
    unittest.main()


class WrapTest(unittest.TestCase):
    """huy: the operator's own status line keeps working, with the Abacus segment appended."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ABACUS_HOME"] = self.tmp.name
        os.environ.pop("ABACUS_LOGBOOK_DB", None)
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()
        self.page = Path(self.tmp.name) / "agents" / "orchestrator" / "pages" / "decisions.md"
        self.page.parent.mkdir(parents=True)
        self.page.write_text(PAGES["run2_card"][0])          # Abacus has something to say: "1 needs you"

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, command):
        (self.state / "statusline.backup.json").write_text(json.dumps({"type": "command", "command": command}))

    def test_runs_the_operators_command_with_the_same_stdin_and_appends(self):
        self.save("python3 -c \"import sys, json; print('model=' + json.load(sys.stdin)['model'])\"")
        self.assertEqual(status.wrapped('{"model": "opus"}'), "model=opus  |  abacus · 1 needs you")

    def test_multi_line_output_gets_the_segment_on_its_last_line(self):
        self.save("printf 'one\\ntwo\\n'")
        self.assertEqual(status.wrapped(""), "one\ntwo  |  abacus · 1 needs you")

    def test_whichever_half_works_still_shows(self):
        self.save("exit 3")                                  # the operator's command fails: Abacus alone
        self.assertEqual(status.wrapped(""), "abacus · 1 needs you")
        self.save("echo mine")
        self.page.unlink()                                   # Abacus not set up: theirs, untouched
        self.assertEqual(status.wrapped(""), "mine")
        (self.state / "statusline.backup.json").unlink()     # nothing saved: never crash
        self.assertEqual(status.wrapped(""), "")

    def test_a_hung_command_cannot_hang_the_status_line(self):
        self.save("sleep 30")
        old = status.WRAP_TIMEOUT
        status.WRAP_TIMEOUT = 1
        try:
            self.assertEqual(status.wrapped(""), "abacus · 1 needs you")
        finally:
            status.WRAP_TIMEOUT = old


class ClockTest(unittest.TestCase):
    """Heartbeat run 2: the concierge said 'I don't know the current time' and couldn't judge a lease."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ABACUS_HOME"] = self.tmp.name
        os.environ.pop("ABACUS_LOGBOOK_DB", None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_and_digest_say_when_now_is(self):
        from abacus_infra import logbook
        self.assertRegex(status.full().splitlines()[0], r"^As of \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC \(")
        conn = logbook.connect(os.path.join(self.tmp.name, "lb.db"))
        self.assertRegex(logbook.digest(conn).splitlines()[0], r"^as of \d{4}-\d\d-\d\d .* UTC")
        conn.close()


class LaneStatusTest(unittest.TestCase):
    """0.10.0: 'how are we doing with the android work?' reports the android lane and nothing about the sdk lane."""

    def setUp(self):
        from abacus_infra import logbook
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        os.environ["ABACUS_HOME"] = str(t)
        os.environ.pop("ABACUS_LOGBOOK_DB", None)
        (t / "state").mkdir()
        conn = logbook.connect(str(t / "state" / "logbook.db"))
        android, sdk = str(t / "android"), str(t / "sdk")
        logbook.worker_upsert(conn, "coder:android", android)
        logbook.worker_upsert(conn, "coder:sdk", sdk)
        logbook.lane_open(conn, "android", "build the login fix")
        logbook.lane_open(conn, "sdk", "build the heartbeat")
        logbook.dispatch_add(conn, "and-1", android, "coder:android", lane="android")
        logbook.dispatch_add(conn, "sdk-1", sdk, "coder:sdk", lane="sdk")
        conn.close()
        pages = t / "agents" / "orchestrator" / "pages"
        pages.mkdir(parents=True)
        (pages / "decisions-android.md").write_text(
            "## NEEDS YOU\n\n### and-1: which login provider?\n\n## Program status: RUNNING\n")
        (pages / "decisions-sdk.md").write_text("## NEEDS YOU\nNone.\n\n## Program status: RUNNING\n")
        self.saved = status.live_sessions
        status.live_sessions = lambda: [{"name": "agent:orchestrator-android", "status": "busy"},
                                        {"name": "agent:orchestrator-sdk", "status": "busy"},
                                        {"name": "coder:android", "status": "busy"}]

    def tearDown(self):
        status.live_sessions = self.saved
        self.tmp.cleanup()

    def test_a_lane_question_gets_only_that_lane(self):
        out = status.full("android")
        for want in ("Lane android: build the login fix", "and-1: which login provider?", "and-1 @ coder:android",
                     "agent:orchestrator-android", "coder:android"):
            self.assertIn(want, out)
        for unwanted in ("sdk", "heartbeat"):
            self.assertNotIn(unwanted, out)

    def test_the_general_answer_has_a_line_per_lane(self):
        out = status.full()
        self.assertIn("[android] and-1: which login provider?", out)
        self.assertIn("  - android: 1 in flight, running, 1 needs you", out)
        self.assertIn("  - sdk: 1 in flight, running", out)

    def test_status_line_counts_across_lanes_and_unknown_lanes_are_named(self):
        self.assertEqual(status.line(), "abacus · 2 lanes · 1 needs you · 2 in flight")
        self.assertIn("No active lane called 'payments'. Active lanes: android, sdk.", status.full("payments"))
