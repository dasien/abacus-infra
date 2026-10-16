import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class VerifyTest(unittest.TestCase):
    """Negative controls: each verification check must be able to fail."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        git(root, "init", "-q", "--bare", "-b", "main", "remote.git")
        self.repo = root / "repo"
        git(root, "clone", "-q", "remote.git", str(self.repo))
        git(self.repo, "config", "user.email", "t@example.com"); git(self.repo, "config", "user.name", "t")
        (self.repo / "f.txt").write_text("1")
        git(self.repo, "add", "f.txt"); git(self.repo, "commit", "-q", "-m", "one\n\nBead: a-1")
        git(self.repo, "push", "-q", "-u", "origin", "main")
        self.sha = git(self.repo, "rev-parse", "HEAD")
        # A fake `bd` that reports whatever status the test writes.
        fake = root / "fakebin"; fake.mkdir()
        (fake / "bd").write_text("#!/bin/sh\ncat \"$BD_FAKE\"\n")
        (fake / "bd").chmod(0o755)
        self.status_file = root / "bd.json"
        os.environ["PATH"] = f"{fake}:{os.environ['PATH']}"
        os.environ["BD_FAKE"] = str(self.status_file)
        from abacus_infra import verify
        self.verify = verify

    def tearDown(self):
        self.tmp.cleanup()

    def bead(self, status):
        self.status_file.write_text(json.dumps([{"id": "a-1", "status": status, "close_reason": "done"}]))

    def failed(self, res):
        return sorted(c["check"] for c in res["checks"] if not c["ok"])

    def test_all_green(self):
        self.bead("closed")
        res = self.verify.verify(str(self.repo), "a-1", self.sha)
        self.assertTrue(res["verified"], res["note"])

    def test_open_bead_fails(self):
        self.bead("in_progress")
        self.assertEqual(self.failed(self.verify.verify(str(self.repo), "a-1", self.sha)), ["bead-closed"])

    def test_unpushed_commit_fails(self):
        self.bead("closed")
        (self.repo / "f.txt").write_text("2")
        git(self.repo, "commit", "-q", "-am", "two")
        local = git(self.repo, "rev-parse", "HEAD")
        res = self.verify.verify(str(self.repo), "a-1", local)
        self.assertEqual(self.failed(res), ["commit-names-bead", "commit-on-remote", "nothing-unpushed"])

    def test_dirty_tree_fails(self):
        self.bead("closed")
        (self.repo / "f.txt").write_text("dirty")
        self.assertEqual(self.failed(self.verify.verify(str(self.repo), "a-1", self.sha)), ["tree-clean"])

    def test_commit_without_bead_trailer_fails(self):
        self.bead("closed")
        (self.repo / "f.txt").write_text("3")
        git(self.repo, "commit", "-q", "-am", "unrelated")
        git(self.repo, "push", "-q")
        res = self.verify.verify(str(self.repo), "a-1", git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(self.failed(res), ["commit-names-bead"])

    def test_unknown_commit_fails(self):
        self.bead("closed")
        res = self.verify.verify(str(self.repo), "a-1", "deadbeef")
        self.assertIn("commit-on-remote", self.failed(res))


class DraftPrVerifyTest(unittest.TestCase):
    """Draft-pr landing: verified means the bead's commit is the head of its open PR (gh is a fake on PATH)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        git(root, "init", "-q", "--bare", "-b", "main", "remote.git")
        self.repo = root / "repo"
        git(root, "clone", "-q", "remote.git", str(self.repo))
        git(self.repo, "config", "user.email", "t@example.com"); git(self.repo, "config", "user.name", "t")
        (self.repo / "f.txt").write_text("1")
        git(self.repo, "add", "f.txt"); git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "push", "-q", "-u", "origin", "main")
        (self.repo / "f.txt").write_text("2")
        git(self.repo, "commit", "-q", "-am", "Add a line (a-1)\n\nBead: a-1")
        self.sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/abacus/add-a-line-a-1")
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")   # the main checkout stays on main, as after a worker run
        fake = root / "fakebin"; fake.mkdir()
        (fake / "bd").write_text("#!/bin/sh\necho '[{\"id\": \"a-1\", \"status\": \"closed\"}]'\n")
        (fake / "gh").write_text("#!/bin/sh\necho \"$*\" >> \"$GH_FAKE_LOG\"\ncat \"$GH_FAKE\"\n")
        for f in ("bd", "gh"):
            (fake / f).chmod(0o755)
        self.pr = root / "pr.json"
        self.env = {k: os.environ.get(k) for k in ("PATH", "GH_FAKE", "GH_FAKE_LOG")}
        os.environ.update(PATH=f"{fake}:{os.environ['PATH']}", GH_FAKE=str(self.pr), GH_FAKE_LOG=str(root / "gh.log"))
        self.gh_log = root / "gh.log"
        from abacus_infra import verify
        self.verify = verify

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def failed(self, res):
        return sorted(c["check"] for c in res["checks"] if not c["ok"])

    def test_commit_at_the_head_of_its_open_pr_is_verified(self):
        self.pr.write_text(json.dumps({"headRefOid": self.sha, "state": "OPEN", "url": "https://github.com/o/r/pull/3"}))
        res = self.verify.verify(str(self.repo), "a-1", self.sha, landing="draft-pr")
        self.assertTrue(res["verified"], res["note"])
        self.assertIn("pr view abacus/add-a-line-a-1", self.gh_log.read_text())

    def test_a_pr_that_moved_on_or_closed_is_not_verified(self):
        for pr in ({"headRefOid": "f" * 40, "state": "OPEN"}, {"headRefOid": self.sha, "state": "CLOSED"}):
            self.pr.write_text(json.dumps(pr))
            res = self.verify.verify(str(self.repo), "a-1", self.sha, landing="draft-pr")
            self.assertEqual(self.failed(res), ["pr-head"], pr)

    def test_push_mode_still_wants_the_commit_on_the_default_branch(self):
        self.pr.write_text(json.dumps({"headRefOid": self.sha, "state": "OPEN"}))
        res = self.verify.verify(str(self.repo), "a-1", self.sha)
        self.assertIn("commit-on-remote", self.failed(res))


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, ABACUS_HOME=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_lock(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "bin" / "abacus-lock"), *args],
                              env=self.env, capture_output=True, text=True)

    def test_runs_command_and_releases(self):
        self.assertEqual(self.run_lock("--", "true").returncode, 0)
        self.assertEqual(self.run_lock("--", "false").returncode, 1)  # a red command stays red
        self.assertFalse(json.loads(self.run_lock().stdout)["held"])

    def test_contention_is_its_own_outcome(self):
        holder = subprocess.Popen([sys.executable, str(ROOT / "bin" / "abacus-lock"), "--", "sleep", "5"],
                                  env=self.env)
        try:
            for _ in range(50):
                if json.loads(self.run_lock().stdout)["held"]:
                    break
            res = self.run_lock("--budget", "1", "--", "true")
            self.assertEqual(res.returncode, 75)
            self.assertIn("CONTENTION", res.stderr)
        finally:
            holder.kill(); holder.wait()

    def test_stale_lock_is_reclaimed(self):
        lock = Path(self.tmp.name) / "state" / "locks" / "heavy.lock"
        lock.mkdir(parents=True)
        (lock / "owner.json").write_text(json.dumps({"pid": 999999, "started": "never"}))
        self.assertEqual(self.run_lock("--budget", "1", "--", "true").returncode, 0)


if __name__ == "__main__":
    unittest.main()


class WorktreeAwarenessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        git(root, "init", "-q", "--bare", "-b", "main", "remote.git")
        self.repo = root / "repo"
        git(root, "clone", "-q", "remote.git", str(self.repo))
        git(self.repo, "config", "user.email", "t@example.com"); git(self.repo, "config", "user.name", "t")
        (self.repo / "f.txt").write_text("1")
        git(self.repo, "add", "f.txt"); git(self.repo, "commit", "-q", "-m", "one\n\nBead: a-1")
        git(self.repo, "push", "-q", "-u", "origin", "main")
        self.wt = self.repo / ".claude" / "worktrees" / "a-1"
        git(self.repo, "worktree", "add", "-q", str(self.wt), "-b", "worktree-a-1")
        fake = root / "fakebin"; fake.mkdir()
        (fake / "bd").write_text("#!/bin/sh\necho '[{\"id\": \"a-1\", \"status\": \"closed\"}]'\n")
        (fake / "bd").chmod(0o755)
        os.environ["PATH"] = f"{fake}:{os.environ['PATH']}"

    def tearDown(self):
        self.tmp.cleanup()

    def test_verify_flags_unsaved_work_left_in_the_beads_worktree(self):
        from abacus_infra import verify
        sha = git(self.repo, "rev-parse", "HEAD")
        self.assertTrue(verify.verify(str(self.repo), "a-1", sha)["verified"])
        (self.wt / "f.txt").write_text("unsaved")
        res = verify.verify(str(self.repo), "a-1", sha)
        self.assertEqual([c["check"] for c in res["checks"] if not c["ok"]], ["bead-worktree-saved"])

    def test_launcher_sees_unsaved_work_in_worktrees(self):
        from abacus_infra import session
        self.assertIsNone(session.unsaved(self.repo))
        (self.wt / "f.txt").write_text("unsaved")
        self.assertIn("worktree", session.unsaved(self.repo))


class LauncherTest(unittest.TestCase):
    def test_every_session_gets_the_memory_server_and_abacus_on_path(self):
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        repo = home / "repo"; repo.mkdir()
        git(repo, "init", "-q")
        (home / "agents").mkdir()
        (home / "agents" / "fleet.json").write_text(json.dumps({"workers": {"coder:repo": {
            "repo": str(repo), "model": "sonnet", "permission_mode": "auto"}}}))
        os.environ["ABACUS_HOME"] = str(home)
        from abacus_infra import logbook, session
        conn = logbook.connect(str(home / "lb.db"))
        out = session.start("coder:repo", conn, dry_run=True)
        cmd = out["would_run"]
        self.assertIn("--mcp-config", cmd)
        cfg = json.loads(cmd[cmd.index("--mcp-config") + 1])
        self.assertEqual(cfg["mcpServers"]["abacus-memory"]["command"], str(home / "bin" / "abacus-memory"))
        conn.close(); tmp.cleanup()

    def test_a_lane_orchestrator_launches_from_the_one_entry_and_knows_its_lane(self):
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        orch = home / "agents" / "orchestrator"; orch.mkdir(parents=True)
        (home / "agents" / "fleet.json").write_text(json.dumps({"workers": {"agent:orchestrator": {
            "repo": str(orch), "model": "opus", "permission_mode": "auto", "agent": "orchestrator",
            "lanes": True, "prompt": "/recover"}}}))
        os.environ["ABACUS_HOME"] = str(home)
        from abacus_infra import logbook, session
        conn = logbook.connect(str(home / "lb.db"))
        cmd = session.start("agent:orchestrator-android", conn, prompt="/recover, then act on the HANDOFF",
                            dry_run=True)["would_run"]
        self.assertEqual(cmd[cmd.index("-n") + 1], "agent:orchestrator-android")
        self.assertTrue(cmd[-1].startswith("You are agent:orchestrator-android, the orchestrator for lane 'android'. "
                                           "/recover, then act on the HANDOFF"), cmd[-1])
        with self.assertRaises(session.Refused):                          # not a lane-capable entry
            session.start("agent:concierge-2", conn, dry_run=True)
        conn.close(); tmp.cleanup()


class VerifyPreconditionsTest(unittest.TestCase):
    def test_failing_precondition_check_blocks_verification(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        git(root, "init", "-q", "--bare", "-b", "main", "remote.git")
        repo = root / "repo"
        git(root, "clone", "-q", "remote.git", str(repo))
        git(repo, "config", "user.email", "t@example.com"); git(repo, "config", "user.name", "t")
        (repo / "vendor.json").write_text('{"rev": "0db5d96"}')
        git(repo, "add", "vendor.json"); git(repo, "commit", "-q", "-m", "x\n\nBead: b-1")
        git(repo, "push", "-q", "-u", "origin", "main")
        fake = root / "fakebin"; fake.mkdir()
        (fake / "bd").write_text("#!/bin/sh\necho '[{\"id\": \"b-1\", \"status\": \"closed\"}]'\n")
        (fake / "bd").chmod(0o755)
        os.environ["PATH"] = f"{fake}:{os.environ['PATH']}"
        from abacus_infra import verify
        sha = git(repo, "rev-parse", "HEAD")
        self.assertTrue(verify.verify(str(repo), "b-1", sha, preconditions=["grep -q 0db5d96 vendor.json"])["verified"])
        res = verify.verify(str(repo), "b-1", sha, preconditions=["grep -q ffffff vendor.json"])
        self.assertFalse(res["verified"])
        self.assertEqual([c["check"] for c in res["checks"] if not c["ok"]],
                         ["precondition: grep -q ffffff vendor.json"])
        tmp.cleanup()


class ChecksReadTheReportedCommitTest(unittest.TestCase):
    def test_file_checks_see_the_commit_even_when_the_checkout_lags(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        git(root, "init", "-q", "--bare", "-b", "main", "remote.git")
        repo = root / "repo"
        git(root, "clone", "-q", "remote.git", str(repo))
        git(repo, "config", "user.email", "t@example.com"); git(repo, "config", "user.name", "t")
        (repo / "core.py").write_text("def slugify(): pass\n")
        git(repo, "add", "core.py"); git(repo, "commit", "-q", "-m", "init")
        git(repo, "push", "-q", "-u", "origin", "main")
        wt = repo / ".claude" / "worktrees" / "a-1"   # the worker lands its change from a worktree
        git(repo, "worktree", "add", "-q", str(wt), "-b", "worktree-a-1")
        (wt / "core.py").write_text("def slugify(): pass\ndef word_count(): pass\n")
        git(wt, "commit", "-q", "-am", "add\n\nBead: a-1"); git(wt, "push", "-q", "origin", "HEAD:main")
        sha = git(wt, "rev-parse", "HEAD")
        git(repo, "worktree", "remove", "--force", str(wt))
        fake = root / "fakebin"; fake.mkdir()
        (fake / "bd").write_text("#!/bin/sh\necho '[{\"id\": \"a-1\", \"status\": \"closed\"}]'\n")
        (fake / "bd").chmod(0o755)
        os.environ["PATH"] = f"{fake}:{os.environ['PATH']}"
        from abacus_infra import verify
        self.assertNotIn("word_count", (repo / "core.py").read_text())  # the main checkout lags origin
        res = verify.verify(str(repo), "a-1", sha, preconditions=["grep -q 'def word_count' core.py"])
        self.assertTrue(res["verified"], res["note"])
        tmp.cleanup()


class CallerAttributionTest(unittest.TestCase):
    def test_caller_is_the_abacus_session_whose_tool_call_this_is(self):
        from abacus_infra import session
        sessions = [{"name": "agent:orchestrator", "sessionId": "s-orch", "pid": 1},
                    {"name": "my-own-chat", "sessionId": "s-me", "pid": 2}]
        env = dict(os.environ); env.pop("ABACUS_SESSION_NAME", None)
        try:
            os.environ.pop("ABACUS_SESSION_NAME", None)
            os.environ["CLAUDE_CODE_SESSION_ID"] = "s-orch"
            self.assertEqual(session.caller(sessions), "agent:orchestrator")
            os.environ["CLAUDE_CODE_SESSION_ID"] = "s-me"
            self.assertEqual(session.caller(sessions), "operator")   # a non-Abacus session is the operator
            os.environ.pop("CLAUDE_CODE_SESSION_ID")
            self.assertEqual(session.caller(sessions), "operator")
        finally:
            os.environ.clear(); os.environ.update(env)


class PruneSelectionTest(unittest.TestCase):
    def test_never_running_keeps_newest_respects_age(self):
        from abacus_infra import session
        h = 3_600_000
        sessions = [
            {"name": "agent:orchestrator", "id": "o-live", "pid": 9, "startedAt": 50 * h},
            {"name": "agent:orchestrator", "id": "o3", "startedAt": 40 * h, "state": "working"},
            {"name": "agent:orchestrator", "id": "o2", "startedAt": 30 * h, "state": "working"},
            {"name": "agent:orchestrator", "id": "o1", "startedAt": 10 * h, "state": "stopped"},
            {"name": "coder:repo-a", "id": "a1", "startedAt": 45 * h},
            {"name": "my-own-chat", "id": "mine", "startedAt": 1 * h},     # not an Abacus session: never touched
        ]
        ids = lambda xs: sorted(s["id"] for s in xs)
        self.assertEqual(ids(session.select_prunable(sessions, keep=1, now_ms=50 * h)), ["o1", "o2"])
        self.assertEqual(ids(session.select_prunable(sessions, keep=0, now_ms=50 * h)), ["a1", "o1", "o2", "o3"])
        # ages at hour 50: o2 is 20h old, o1 is 40h old
        self.assertEqual(ids(session.select_prunable(sessions, keep=1, older_than_hours=24, now_ms=50 * h)), ["o1"])
        self.assertEqual(ids(session.select_prunable(sessions, keep=1, older_than_hours=45, now_ms=50 * h)), [])


class AgentViewFailureTest(unittest.TestCase):
    """A failed `claude agents --json` is reported (AGENT_VIEW) and logged, never read as "nothing running"."""

    def test_find_live_refuses_and_logs(self):
        from abacus_infra import session
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            (t / "claude").write_text("#!/bin/sh\necho 'API Error: 401 token revoked' >&2\nexit 1\n")
            (t / "claude").chmod(0o755)
            saved = dict(os.environ)
            os.environ.update(PATH=f"{t}{os.pathsep}{os.environ['PATH']}", ABACUS_HOME=str(t / "home"))
            os.environ.pop("ABACUS_DEBUG", None)
            try:
                with self.assertRaises(session.Refused) as r:
                    session.find_live("agent:concierge")
                self.assertEqual(r.exception.code, "AGENT_VIEW")
                self.assertIn("401 token revoked", str(r.exception))
                self.assertEqual(session.live_sessions(), [])  # display paths stay best-effort
                log = (t / "home" / "state" / "agent-view.log").read_text()
                self.assertEqual(log.count("FAILED: exit 1: API Error: 401 token revoked"), 2)
            finally:
                os.environ.clear()
                os.environ.update(saved)


class LaneSessionNameTest(unittest.TestCase):
    """0.10.0: lane orchestrators (agent:orchestrator-<lane>) and their successors come from the one fleet entry."""

    FLEET = {"agent:orchestrator": {"repo": "/x/orch", "lanes": True, "generations": True, "prompt": "/recover"},
             "agent:concierge": {"repo": "/x/conc"},
             "coder:sdk": {"repo": "/x/sdk"}}

    def test_resolve(self):
        from abacus_infra.session import resolve
        f = self.FLEET
        self.assertEqual(resolve("agent:orchestrator", f), (f["agent:orchestrator"], None))
        self.assertEqual(resolve("agent:orchestrator-g2", f), (f["agent:orchestrator"], None))
        self.assertEqual(resolve("agent:orchestrator-android", f), (f["agent:orchestrator"], "android"))
        self.assertEqual(resolve("agent:orchestrator-sdk-heartbeat", f), (f["agent:orchestrator"], "sdk-heartbeat"))
        self.assertEqual(resolve("agent:orchestrator-android-g3", f), (f["agent:orchestrator"], "android"))
        # negative controls: only entries that allow lanes get them, and lane names must be well formed
        for name in ("agent:concierge-extra", "coder:sdk-2", "agent:orchestrator-Android", "agent:orchestrator-",
                     "agent:planner-x"):
            self.assertEqual(resolve(name, f), (None, None), name)
