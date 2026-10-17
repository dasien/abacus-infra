import json
import os
import sqlite3
import tempfile
import unittest

from abacus_infra import logbook as lb


class LogbookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = lb.connect(os.path.join(self.tmp.name, "logbook.db"))
        lb.worker_upsert(self.conn, "coder:repo-a", "/w/repo-a")
        lb.worker_upsert(self.conn, "coder:repo-b", "/w/repo-b")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_second_open_dispatch_for_one_bead_is_refused(self):
        lb.dispatch_add(self.conn, "a-1", "/w/repo-a", "coder:repo-a")
        with self.assertRaises(lb.Refused) as e:
            lb.dispatch_add(self.conn, "a-1", "/w/repo-a", "coder:repo-a")
        self.assertEqual(e.exception.code, "DUPLICATE")

    def test_closed_dispatch_allows_a_new_one(self):
        lb.dispatch_add(self.conn, "a-1", "/w/repo-a", "coder:repo-a")
        lb.dispatch_status(self.conn, "a-1", "cancelled")
        lb.dispatch_add(self.conn, "a-1", "/w/repo-a", "coder:repo-a")

    def test_unique_index_backstops_the_check(self):
        lb.dispatch_add(self.conn, "a-1", "/w/repo-a", "coder:repo-a")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO dispatches(bead, repo, worker, status, created_at, updated_at) "
                "VALUES ('a-1', 'r', 'w', 'queued', 'x', 'x')")

    def test_fleet_paused_fails_closed(self):
        for value in ("true", "yes", "", "False", "1"):
            lb.fact_set(self.conn, "fleet.paused", value)
            with self.assertRaises(lb.Refused, msg=value) as e:
                lb.dispatch_add(self.conn, "a-1", "/w/repo-a", "coder:repo-a")
            self.assertEqual(e.exception.code, "PAUSED")
        lb.fact_set(self.conn, "fleet.paused", "false")
        lb.dispatch_add(self.conn, "a-1", "/w/repo-a", "coder:repo-a")

    def test_attended_worker_is_refused(self):
        lb.worker_upsert(self.conn, "coder:repo-a", "/w/repo-a", mode="attended")
        with self.assertRaises(lb.Refused) as e:
            lb.dispatch_add(self.conn, "a-1", "/w/repo-a", "coder:repo-a")
        self.assertEqual(e.exception.code, "ATTENDED")

    def test_dependant_waits_for_verified_prerequisite(self):
        lb.dispatch_add(self.conn, "a-1", "/w/repo-a", "coder:repo-a")
        with self.assertRaises(lb.Refused) as e:
            lb.dispatch_add(self.conn, "b-1", "/w/repo-b", "coder:repo-b", requires_verified=["a-1"])
        self.assertEqual(e.exception.code, "PREREQUISITE")
        lb.report_add(self.conn, "coder:repo-a", "a-1", "CLOSED", commit="abc123")
        with self.assertRaises(lb.Refused):  # a report is a claim, not a verification
            lb.dispatch_add(self.conn, "b-1", "/w/repo-b", "coder:repo-b", requires_verified=["a-1"])
        lb.dispatch_verify(self.conn, "a-1", "abc123", "bead closed; commit on origin/main; 0 unpushed")
        lb.dispatch_add(self.conn, "b-1", "/w/repo-b", "coder:repo-b", requires_verified=["a-1"])

    def test_unsent_dispatch_cannot_be_verified_and_reports_never_unverify(self):
        lb.dispatch_add(self.conn, "a-1", "/w/repo-a", "coder:repo-a")
        with self.assertRaises(lb.Refused):
            lb.dispatch_verify(self.conn, "a-1", "abc", "checked")
        lb.dispatch_status(self.conn, "a-1", "sent")
        lb.dispatch_verify(self.conn, "a-1", "abc", "checked")
        lb.report_add(self.conn, "coder:repo-a", "a-1", "PROGRESS")
        st = self.conn.execute("SELECT status FROM dispatches WHERE bead='a-1'").fetchone()[0]
        self.assertEqual(st, "verified")

    def test_dispatch_to_wrong_repo_refused(self):
        with self.assertRaises(lb.Refused) as e:
            lb.dispatch_add(self.conn, "a-1", "/w/repo-b", "coder:repo-a")
        self.assertEqual(e.exception.code, "WRONG_REPO")

    def test_report_cannot_be_verified_without_a_note(self):
        rid = lb.report_add(self.conn, "coder:repo-a", "a-1", "CLOSED", commit="abc")
        for note in ("", "   ", None):
            with self.assertRaises(lb.Refused):
                lb.report_verified(self.conn, rid, True, note)
        with self.assertRaises(lb.Refused):
            lb.dispatch_verify(self.conn, "a-1", "abc", "")

    def test_event_tables_are_insert_only(self):
        lb.ruling_add(self.conn, "go with b", "chat")
        for stmt in ("UPDATE rulings SET words = 'x'", "DELETE FROM rulings"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(stmt)

    def test_ruling_words_kept_verbatim(self):
        words = 'b, but "keep" the old flag | for now\nthanks'
        rid = lb.ruling_add(self.conn, words, "chat")
        got = self.conn.execute("SELECT words FROM rulings WHERE id = ?", (rid,)).fetchone()[0]
        self.assertEqual(got, words)

    def test_digest_never_exceeds_limit(self):
        lb.fact_set(self.conn, "fleet.paused", "false")
        for i in range(200):
            name = f"coder:repo-{i:03d}"
            lb.worker_upsert(self.conn, name, f"/w/{i}")
            lb.dispatch_add(self.conn, f"bead-with-a-long-identifier-{i}", f"/w/{i}", name)
        out = lb.digest(self.conn)
        self.assertLessEqual(len(out.encode()), lb.DIGEST_LIMIT)
        self.assertIn("binds now:", out)

    def test_handover_state_machine(self):
        hid = lb.handover(self.conn, "launched", "agent:orchestrator", "agent:orchestrator-2")
        with self.assertRaises(lb.Refused):
            lb.handover(self.conn, "finished", hid=hid)  # must recover first
        lb.handover(self.conn, "recovered", hid=hid)
        lb.handover(self.conn, "finished", hid=hid)


if __name__ == "__main__":
    unittest.main()


class RelayAndChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = lb.connect(os.path.join(self.tmp.name, "logbook.db"))
        lb.worker_upsert(self.conn, "coder:repo-a", "/w/repo-a")
        lb.worker_upsert(self.conn, "coder:repo-b", "/w/repo-b")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_dispatch_refused_until_covering_ruling_is_relayed_to_that_worker(self):
        rid = lb.ruling_add(self.conn, "A then B, approved", "chat", target="a-1")
        lb.ruling_recorded(self.conn, rid, "b-1")  # recorded on B too: it covers both beads
        for bead, worker, repo in (("a-1", "coder:repo-a", "/w/repo-a"), ("b-1", "coder:repo-b", "/w/repo-b")):
            with self.assertRaises(lb.Refused) as e:
                lb.dispatch_add(self.conn, bead, repo, worker)
            self.assertEqual(e.exception.code, "UNRELAYED")
        lb.ruling_relayed(self.conn, rid, "coder:repo-a")
        lb.dispatch_add(self.conn, "a-1", "/w/repo-a", "coder:repo-a")
        with self.assertRaises(lb.Refused):  # relaying to A does not count for B
            lb.dispatch_add(self.conn, "b-1", "/w/repo-b", "coder:repo-b")

    def test_unrelayed_view_and_digest_flag_rulings_recorded_after_dispatch(self):
        lb.dispatch_add(self.conn, "a-1", "/w/repo-a", "coder:repo-a")
        rid = lb.ruling_add(self.conn, "change of plan", "chat", target="a-1")
        rows = lb.show(self.conn, "rulings-unrelayed")
        self.assertEqual([(r["bead"], r["worker"], r["ruling"]) for r in rows], [("a-1", "coder:repo-a", rid)])
        self.assertIn("unrelayed 1", lb.digest(self.conn))
        lb.ruling_relayed(self.conn, rid, "coder:repo-a")
        self.assertEqual(lb.show(self.conn, "rulings-unrelayed"), [])

    def test_relays_are_insert_only(self):
        rid = lb.ruling_add(self.conn, "x", "chat")
        lb.ruling_relayed(self.conn, rid, "coder:repo-a")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM ruling_relays")

    def test_checks_are_validated_and_stored(self):
        with self.assertRaises(lb.Refused) as e:
            lb.dispatch_add(self.conn, "a-1", "/w/repo-a", "coder:repo-a", checks=["rm -rf vendor"])
        self.assertEqual(e.exception.code, "BAD_CHECK")
        lb.dispatch_add(self.conn, "a-1", "/w/repo-a", "coder:repo-a", checks=["grep -q abc vendor.json"])
        self.assertEqual(lb.dispatch_checks(self.conn, "a-1"), ["grep -q abc vendor.json"])

    def test_old_logbook_gains_checks_column(self):
        path = os.path.join(self.tmp.name, "old.db")
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE dispatches (id INTEGER PRIMARY KEY, bead TEXT NOT NULL, repo TEXT NOT NULL, "
                    "worker TEXT NOT NULL, pipeline TEXT, program TEXT, status TEXT NOT NULL DEFAULT 'queued', "
                    "order_note TEXT, preconditions TEXT, report_to TEXT NOT NULL DEFAULT 'agent:orchestrator', "
                    "verified_commit TEXT, verified_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        old.commit(); old.close()
        conn = lb.connect(path)
        self.assertIn("checks", [r[1] for r in conn.execute("PRAGMA table_info(dispatches)")])
        conn.close()


class MultiTargetRulingTest(unittest.TestCase):
    def test_one_utterance_covers_several_beads_and_each_worker_needs_the_relay(self):
        tmp = tempfile.TemporaryDirectory()
        conn = lb.connect(os.path.join(tmp.name, "lb.db"))
        lb.worker_upsert(conn, "coder:repo-a", "/w/repo-a"); lb.worker_upsert(conn, "coder:repo-b", "/w/repo-b")
        rid = lb.ruling_add(conn, "A then B, approved", "chat", target=["a-1", "b-1"])
        self.assertEqual(conn.execute("SELECT count(*) FROM rulings").fetchone()[0], 1)
        for bead, worker, repo in (("a-1", "coder:repo-a", "/w/repo-a"), ("b-1", "coder:repo-b", "/w/repo-b")):
            with self.assertRaises(lb.Refused):
                lb.dispatch_add(conn, bead, repo, worker)
            lb.ruling_relayed(conn, rid, worker)
            lb.dispatch_add(conn, bead, repo, worker)
        conn.close(); tmp.cleanup()


class SessionEventsTest(unittest.TestCase):
    def test_events_record_who_and_why_and_are_insert_only(self):
        tmp = tempfile.TemporaryDirectory()
        conn = lb.connect(os.path.join(tmp.name, "lb.db"))
        lb.session_event(conn, "coder:repo-a", "stopped", "agent:orchestrator", "program complete")
        ev = lb.show(conn, "session-events")[0]
        self.assertEqual((ev["name"], ev["action"], ev["by"], ev["reason"]),
                         ("coder:repo-a", "stopped", "agent:orchestrator", "program complete"))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM session_events")
        conn.close(); tmp.cleanup()

    def test_rulings_view_lists_every_target(self):
        tmp = tempfile.TemporaryDirectory()
        conn = lb.connect(os.path.join(tmp.name, "lb.db"))
        lb.ruling_add(conn, "A then B", "agent:concierge", target=["a-1", "b-1"])
        self.assertEqual(lb.show(conn, "rulings")[0]["targets"], "a-1, b-1")
        conn.close(); tmp.cleanup()


class NoItemRulingTest(unittest.TestCase):
    """0w0: a ruling that covers no bead ("start the orchestrator") can be closed out, so the unrecorded count
    shows real gaps only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "logbook.db")
        self.conn = lb.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def unrecorded(self):
        return [r["id"] for r in lb.show(self.conn, "rulings-unrecorded")]

    def test_no_item_ruling_closes_out_with_a_reason(self):
        rid = lb.ruling_add(self.conn, "start the orchestrator for the decision demo", "agent:concierge")
        self.assertEqual(self.unrecorded(), [rid])
        self.assertIn("rulings unrecorded 1", lb.digest(self.conn))
        lb.ruling_recorded(self.conn, rid, lb.NO_ITEM, "an instruction to start a session; ACK HANDOFF received")
        self.assertEqual(self.unrecorded(), [])
        self.assertIn("rulings unrecorded 0", lb.digest(self.conn))
        row = lb.show(self.conn, "rulings")[0]
        self.assertEqual(row["recorded_on"], "none")

    def test_refused_when_it_covers_a_bead_or_has_no_reason(self):
        covered = lb.ruling_add(self.conn, "go with b", "chat", target=["a-1", "b-1"])
        with self.assertRaises(lb.Refused) as e:
            lb.ruling_recorded(self.conn, covered, lb.NO_ITEM, "nothing to record")
        self.assertEqual(e.exception.code, "HAS_TARGETS")
        self.assertIn("a-1, b-1", str(e.exception))
        bare = lb.ruling_add(self.conn, "start it", "chat")
        for reason in (None, "", "   "):
            with self.assertRaises(lb.Refused) as e:
                lb.ruling_recorded(self.conn, bare, lb.NO_ITEM, reason)
            self.assertEqual(e.exception.code, "REASON")
        with self.assertRaises(lb.Refused) as e:
            lb.ruling_recorded(self.conn, 999, "a-1")
        self.assertEqual(e.exception.code, "UNKNOWN")
        self.assertEqual(self.unrecorded(), [covered, bare])     # nothing was closed out by a refusal

    def test_logbooks_from_before_get_the_reason_column(self):
        self.conn.close()
        old = os.path.join(self.tmp.name, "old.db")
        c = sqlite3.connect(old)
        c.execute("CREATE TABLE ruling_records (id INTEGER PRIMARY KEY, ruling_id INTEGER NOT NULL, at TEXT NOT NULL, "
                  "recorded_on TEXT NOT NULL)")
        c.commit(); c.close()
        self.conn = lb.connect(old)
        rid = lb.ruling_add(self.conn, "start it", "chat")
        lb.ruling_recorded(self.conn, rid, lb.NO_ITEM, "session instruction")
        self.assertEqual(self.unrecorded(), [])


class PrerequisiteRecordTest(unittest.TestCase):
    """A dispatch row records the prerequisites the gate checked, so every run can be audited afterwards."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = lb.connect(os.path.join(self.tmp.name, "logbook.db"))
        lb.worker_upsert(self.conn, "coder:sdk", "/w/sdk")
        lb.worker_upsert(self.conn, "coder:cli", "/w/cli")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_the_gate_and_its_commit_are_on_the_row(self):
        lb.dispatch_add(self.conn, "sdk-1", "/w/sdk", "coder:sdk")
        with self.assertRaises(lb.Refused) as e:                      # the gate itself still refuses
            lb.dispatch_add(self.conn, "cli-1", "/w/cli", "coder:cli", requires_verified=["sdk-1"])
        self.assertEqual(e.exception.code, "PREREQUISITE")
        self.conn.execute("UPDATE dispatches SET status = 'verified', verified_commit = 'abc123' WHERE bead = 'sdk-1'")
        lb.dispatch_add(self.conn, "cli-1", "/w/cli", "coder:cli", requires_verified=["sdk-1"])
        rows = {r["bead"]: r for r in lb.show(self.conn, "dispatches")}
        self.assertEqual(json.loads(rows["cli-1"]["requires_verified"]),
                         [{"bead": "sdk-1", "verified_commit": "abc123"}])
        self.assertIsNone(rows["sdk-1"]["requires_verified"])          # no gate used: visibly empty
        self.assertIn("requires_verified", lb.show(self.conn, "in-flight")[0])

    def test_older_logbooks_gain_the_column(self):
        self.conn.close()
        old = os.path.join(self.tmp.name, "old.db")
        c = sqlite3.connect(old)
        c.execute("CREATE TABLE dispatches (id INTEGER PRIMARY KEY, bead TEXT NOT NULL, repo TEXT NOT NULL, "
                  "worker TEXT NOT NULL, pipeline TEXT, program TEXT, status TEXT NOT NULL DEFAULT 'queued', "
                  "order_note TEXT, preconditions TEXT, report_to TEXT NOT NULL DEFAULT 'agent:orchestrator', "
                  "verified_commit TEXT, verified_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        c.commit(); c.close()
        self.conn = lb.connect(old)
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(dispatches)")]
        self.assertIn("requires_verified", cols)


class RecordedOnBeadTest(unittest.TestCase):
    """3zg: 'ruling recorded --on <bead>' is refused unless the bead's notes quote the ruling's words."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = lb.connect(os.path.join(self.tmp.name, "logbook.db"))
        self.notes = {"a-1": 'JUDGEMENT 2026-10-14: ... | ruled: "go with b, approved" | agreed: yes | ruling: #1',
                      "a-2": ""}
        self.notes_of = lambda bead: self.notes.get(bead)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_recorded_only_where_the_row_really_is(self):
        rid = lb.ruling_add(self.conn, "go with b,  approved", "agent:concierge", target=["a-1", "a-2"])
        lb.ruling_recorded(self.conn, rid, "a-1", notes_of=self.notes_of)        # whitespace-insensitive match
        with self.assertRaises(lb.Refused) as e:
            lb.ruling_recorded(self.conn, rid, "a-2", notes_of=self.notes_of)
        self.assertEqual(e.exception.code, "NOT_ON_BEAD")
        with self.assertRaises(lb.Refused) as e:
            lb.ruling_recorded(self.conn, rid, "zz-9", notes_of=self.notes_of)
        self.assertEqual(e.exception.code, "UNKNOWN_BEAD")
        recorded = lb.show(self.conn, "rulings")[0]["recorded_on"]
        self.assertEqual(recorded, "a-1")                                          # refusals recorded nothing

    def test_no_item_rulings_keep_their_rule(self):
        rid = lb.ruling_add(self.conn, "start the orchestrator", "agent:concierge")
        lb.ruling_recorded(self.conn, rid, lb.NO_ITEM, "a session instruction", notes_of=self.notes_of)

    def test_the_cli_checks_the_real_board(self):
        import json, subprocess, sys
        from pathlib import Path
        t = Path(self.tmp.name).resolve()
        repo, home = t / "repo-a", t / "home"
        repo.mkdir(); (home / "agents").mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["bd", "init", "--prefix", "a", "--quiet", "--skip-agents", "--skip-hooks", "--non-interactive"],
                       cwd=repo, check=True, capture_output=True)
        bead = subprocess.run(["bd", "create", "x", "--silent"], cwd=repo, check=True, capture_output=True,
                              text=True).stdout.strip()
        (home / "agents" / "fleet.json").write_text(json.dumps({"workers": {"coder:repo-a": {"repo": str(repo)}}}))
        env = dict(os.environ, ABACUS_HOME=str(home), ABACUS_LOGBOOK_DB=str(t / "cli.db"),
                   PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        cli = lambda *a: subprocess.run([sys.executable, "-m", "abacus_infra.logbook", *a], env=env,
                                        capture_output=True, text=True)
        rid = json.loads(cli("ruling", "add", "--words", "approve both", "--where", "agent:concierge",
                             "--target", bead).stdout)["ruling"]
        p = cli("ruling", "recorded", str(rid), "--on", bead)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("NOT_ON_BEAD", p.stdout + p.stderr)
        subprocess.run(["bd", "update", bead, "--append-notes",
                        'STANDING 2026-10-14: words: "approve both" | scope: x | '
                        f'source: y | review: z by 2026-11-01 | ruling: #{rid}'],
                       cwd=repo, check=True, capture_output=True)
        self.assertEqual(cli("ruling", "recorded", str(rid), "--on", bead).returncode, 0)


class LaneTest(unittest.TestCase):
    """0.10.0: independent lanes of work, each with its own orchestrator; one active lane per repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.conn = lb.connect(os.path.join(t, "logbook.db"))
        self.android, self.sdk = os.path.join(t, "android"), os.path.join(t, "sdk")
        for r in (self.android, self.sdk):
            os.makedirs(r)
        lb.worker_upsert(self.conn, "coder:android", self.android)
        lb.worker_upsert(self.conn, "coder:sdk", self.sdk)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_promises_carry_their_lane(self):
        """abacus-infra-f8q: one lane's open promise was read as stale by the other lane's orchestrator."""
        lb.lane_open(self.conn, "android", "build the login fix", repos=[self.android], items=["and-1"])
        lb.lane_open(self.conn, "sdk", "build the heartbeat", repos=[self.sdk], items=["sdk-1"])
        mine = lb.promise_add(self.conn, "verify and-1 report", work_item="and-1", lane="android")
        inferred = lb.promise_add(self.conn, "verify sdk-1 report", work_item="sdk-1")    # forgot --lane
        loose = lb.promise_add(self.conn, "tidy up", work_item="other-9")
        self.assertEqual([r["id"] for r in lb.show(self.conn, "asks", lane="android")], [mine])
        self.assertEqual([r["id"] for r in lb.show(self.conn, "asks", lane="sdk")], [inferred])
        self.assertIn("open promises 3 (android 1, sdk 1, no lane 1)", lb.digest(self.conn))
        with self.assertRaises(lb.Refused) as e:                       # a lane can't close owing a promise
            lb.lane_close(self.conn, "sdk")
        self.assertEqual(e.exception.code, "OPEN_WORK")
        self.assertIn(str(inferred), str(e.exception))
        lb.promise_close(self.conn, inferred, "done", "verified")
        lb.lane_close(self.conn, "sdk")
        with self.assertRaises(lb.Refused) as e:                       # only an active lane takes promises
            lb.promise_add(self.conn, "x", lane="sdk")
        self.assertEqual(e.exception.code, "NO_LANE")
        lb.promise_close(self.conn, mine, "done", "verified")
        lb.lane_rename(self.conn, "android", "login")
        self.assertEqual(self.conn.execute("SELECT lane FROM promises WHERE id = ?", (mine,)).fetchone()[0], "login")
        self.assertIsNone(self.conn.execute("SELECT lane FROM promises WHERE id = ?", (loose,)).fetchone()[0])

    def test_lanes_own_their_repositories(self):
        lb.lane_open(self.conn, "android", "build the login fix", repos=[self.android])
        lb.lane_open(self.conn, "sdk", "build the heartbeat", repos=[self.sdk])
        self.assertEqual(lb.lane_owner(self.conn, self.android), "android")
        with self.assertRaises(lb.Refused) as e:                       # one active lane per repository
            lb.lane_add(self.conn, "sdk", repos=[self.android])
        self.assertEqual(e.exception.code, "LANE")
        with self.assertRaises(lb.Refused) as e:                       # a second lane can't open on it either
            lb.lane_open(self.conn, "android-2", "something else", repos=[self.android])
        self.assertEqual(e.exception.code, "LANE")
        with self.assertRaises(lb.Refused) as e:
            lb.lane_open(self.conn, "android", "again")
        self.assertEqual(e.exception.code, "NAME_TAKEN")

    def test_a_dispatch_claims_for_its_lane_and_another_lanes_repo_is_refused(self):
        lb.lane_open(self.conn, "android", "build the login fix")
        lb.dispatch_add(self.conn, "and-1", self.android, "coder:android", lane="android",
                        report_to="agent:orchestrator-android")
        self.assertEqual(lb.lane_owner(self.conn, self.android), "android")   # claimed by dispatching
        self.assertIn("and-1", lb.lanes(self.conn)[0]["items"])
        lb.lane_open(self.conn, "sdk", "build the heartbeat")
        with self.assertRaises(lb.Refused) as e:
            lb.dispatch_add(self.conn, "and-2", self.android, "coder:android", lane="sdk")
        self.assertEqual(e.exception.code, "LANE")
        with self.assertRaises(lb.Refused) as e:                       # nor without a lane at all
            lb.dispatch_add(self.conn, "and-3", self.android, "coder:android")
        self.assertEqual(e.exception.code, "LANE")
        rows = lb.show(self.conn, "in-flight", lane="android")
        self.assertEqual([r["bead"] for r in rows], ["and-1"])
        self.assertEqual(lb.show(self.conn, "in-flight", lane="sdk"), [])

    def test_closing_releases_repositories_but_not_with_open_work(self):
        lb.lane_open(self.conn, "android", "build the login fix")
        lb.dispatch_add(self.conn, "and-1", self.android, "coder:android", lane="android")
        with self.assertRaises(lb.Refused) as e:
            lb.lane_close(self.conn, "android")
        self.assertEqual(e.exception.code, "OPEN_WORK")
        lb.dispatch_status(self.conn, "and-1", "cancelled")
        lb.lane_close(self.conn, "android")
        self.assertIsNone(lb.lane_owner(self.conn, self.android))
        lb.lane_open(self.conn, "sdk-and-android", "joint work", repos=[self.android, self.sdk])  # free again
        lb.lane_open(self.conn, "android", "a new android program")     # a closed lane's name can be reused
        self.assertEqual(len(lb.lanes(self.conn)), 2)

    def test_rename_carries_repositories_items_and_dispatches(self):
        lb.lane_open(self.conn, "android", "build the login fix", items=["https://github.com/o/android/issues/7"])
        lb.dispatch_add(self.conn, "and-1", self.android, "coder:android", lane="android")
        with self.assertRaises(lb.Refused) as e:                       # not while its orchestrator has work out
            lb.lane_rename(self.conn, "android", "login-fix")
        self.assertEqual(e.exception.code, "OPEN_WORK")
        lb.dispatch_status(self.conn, "and-1", "closed")
        lb.lane_rename(self.conn, "android", "login-fix")
        self.assertEqual(lb.lane_owner(self.conn, self.android), "login-fix")
        self.assertEqual(lb.show(self.conn, "dispatches")[0]["lane"], "login-fix")
        self.assertIn("https://github.com/o/android/issues/7", lb.lanes(self.conn)[0]["items"])
        for bad in ("Login", "x" * 41, "android-g2", ""):
            with self.assertRaises(lb.Refused, msg=bad):
                lb.lane_rename(self.conn, "login-fix", bad)

    def test_find_a_lane_by_name_repository_item_or_goal(self):
        lb.lane_open(self.conn, "android", "build the login fix", repos=[self.android])
        lb.lane_open(self.conn, "sdk-heartbeat", "build the heartbeat feature", repos=[self.sdk],
                     items=["https://github.com/dasien/bwcli-rs/issues/1"])
        names = lambda q: [l["name"] for l in lb.lane_find(self.conn, q)]
        self.assertEqual(names("how are we doing with the android work?"), ["android"])
        self.assertEqual(names("the login fix"), ["android"])
        self.assertEqual(names("dasien/bwcli-rs/issues/1"), ["sdk-heartbeat"])
        self.assertEqual(names("heartbeat"), ["sdk-heartbeat"])
        self.assertEqual(names("payments"), [])

    def test_older_logbooks_gain_the_lane_column(self):
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(dispatches)")]
        self.assertIn("lane", cols)
        self.assertIn("lanes: none", lb.digest(self.conn))
