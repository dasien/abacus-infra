import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from abacus_infra import checks


class ChecksTest(unittest.TestCase):
    def test_allowlist_rejects_anything_that_could_write_or_execute(self):
        for bad in ("rm -rf vendor", "python3 -c 'print(1)'", "sh -c 'ls'", "git commit -m x", "git push",
                    "git diff --output=/tmp/x", "git -c core.pager=evil log", "git branch evil",
                    "git log --ext-diff", "tee x", "sed -i s/a/b/ f", "make test", "", "git", "env"):
            with self.assertRaises(checks.BadCheck, msg=bad):
                checks.parse(bad)

    def test_allowlist_accepts_read_only_forms(self):
        for good in ("grep -q 0db5d96c vendor.json", "test -f vendor/textkit/core.py",
                     "git --git-dir ../remotes/repo-a.git grep -q word_count 0db5d96c -- textkit",
                     "git merge-base --is-ancestor abc def", "git branch --list", "cat README.md"):
            checks.parse(good)

    def test_no_shell_means_operators_are_literal(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "f.txt").write_text("hello\n")
            ok, _ = checks.run("grep -q hello f.txt", d)
            self.assertTrue(ok)
            ok, _ = checks.run("cat f.txt > pwned.txt", d)  # '>' is a literal filename argument, not a redirect
            self.assertFalse(ok)
            self.assertFalse(Path(d, "pwned.txt").exists())
            ok, detail = checks.run("grep -q goodbye f.txt", d)
            self.assertFalse(ok)
            self.assertIn("exit 1", detail)
            ok, detail = checks.run("rm f.txt", d)
            self.assertFalse(ok)
            self.assertIn("refused", detail)
            self.assertTrue(Path(d, "f.txt").exists())


if __name__ == "__main__":
    unittest.main()
