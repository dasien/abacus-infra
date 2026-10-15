import os
import subprocess
import tempfile
import unittest
from pathlib import Path

RETRO = """---
id: repo-a-2026-10-10-001
project: repo-a
issue: repo-a-1
date: 2026-10-10
doc_type: retro
trigger: fix-loop-cap
surface: tests
severity: sev3
resolved: partial
tags: [flaky, timeout]
---
# Retro: integration tests hit the fix-loop cap

## Insights
The practice of sharing one database between parallel test workers caused intermittent
deadlocks because migrations ran concurrently.
"""


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["ABACUS_HOME"] = str(root / "home")
        os.environ.pop("ABACUS_MEMORY_DB", None)
        from abacus_infra import memory
        self.memory = memory
        remote = root / "remote.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(remote))
        self.repo = root / "repo-a"
        git(root, "clone", "-q", str(remote), str(self.repo))
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "docs" / "retros").mkdir(parents=True)
        (self.repo / "docs" / "retros" / "repo-a-2026-10-10-001.md").write_text(RETRO)
        git(self.repo, "add", "docs")
        git(self.repo, "commit", "-q", "-m", "retro")
        git(self.repo, "push", "-q", "-u", "origin", "main")
        self.conn = memory.connect(migrate=True)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_unmigrated_database_is_refused(self):
        with self.assertRaises(self.memory.Refused):
            self.memory.connect(path=Path(self.tmp.name) / "fresh.db")

    def test_harvest_ingest_consult_roundtrip_and_idempotence(self):
        rep = self.memory.harvest(self.repo)
        self.assertIn("docs/retros/repo-a-2026-10-10-001.md", rep["classified"])
        first = self.memory.ingest(self.conn)
        self.assertEqual(first["written"], 1)
        second = self.memory.ingest(self.conn)
        self.assertEqual(second["written"], 0)
        self.assertEqual(second["unchanged"], 1)
        hit = self.memory.consult(self.conn, "parallel test workers deadlock on shared database")
        self.assertEqual(hit["outcome"], "hits")
        self.assertEqual(hit["hits"][0]["identifier"], "repo-a:docs/retros/repo-a-2026-10-10-001.md")

    def test_unpublished_record_is_refused_by_harvest(self):
        (self.repo / "docs" / "retros" / "draft.md").write_text(RETRO.replace("001", "002"))
        rep = self.memory.harvest(self.repo)
        self.assertTrue(any("draft.md: uncommitted" in r for r in rep["refused"]))

    def test_non_hits_are_named(self):
        self.memory.harvest(self.repo)
        self.memory.ingest(self.conn)
        out = self.memory.consult(self.conn, "kubernetes helm chart", project="repo-a")
        self.assertEqual(out["outcome"], "index_recall")
        self.assertEqual(out["corpus_coverage"], {"retro": 1})
        out = self.memory.consult(self.conn, "deadlock", project="no-such-repo")
        self.assertEqual(out["outcome"], "no_rows_matched_filters")
        self.conn.execute("INSERT OR REPLACE INTO settings VALUES ('floor', '1000')")
        out = self.memory.consult(self.conn, "deadlock")
        self.assertEqual(out["outcome"], "abstained_below_floor")

    def test_misspelled_facet_fails_loudly(self):
        with self.assertRaises(self.memory.Refused):
            self.memory.consult(self.conn, "x", facets={"surfce": "tests"})

    def test_invalid_record_blocks_ingest_unless_count_is_given(self):
        bad = RETRO.replace("surface: tests", "surface: vibes")
        (self.repo / "docs" / "retros" / "bad.md").write_text(bad)
        git(self.repo, "add", "docs"); git(self.repo, "commit", "-q", "-m", "bad")
        git(self.repo, "push", "-q")
        self.memory.harvest(self.repo)
        with self.assertRaises(self.memory.Refused):
            self.memory.ingest(self.conn)
        res = self.memory.ingest(self.conn, allow_refusals=1)
        self.assertEqual(res["written"], 1)


if __name__ == "__main__":
    unittest.main()
