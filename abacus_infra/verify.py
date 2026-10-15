"""Verify a worker's report. A report is a claim, not a state transition.

Read-only checks against the worker's repository:
  1. the bead is closed on the repository's own board
  2. the reported commit is reachable from the remote branch (ls-remote, no fetch)
  3. nothing is left unpushed and the tree is clean

With --record, a passing result is written to the logbook as the dispatch's
verification; a failing one is recorded against the report as not verified.
"""
import argparse
import json
import os
import subprocess
import sys

from . import logbook


def _run(cmd, cwd):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)


def worktrees(repo):
    """[(path, branch ref)] for every linked worktree of repo (excluding the main checkout)."""
    rc, out, _ = _run(["git", "worktree", "list", "--porcelain"], repo)
    items, cur = [], {}
    for line in (out.splitlines() + [""]) if rc == 0 else []:
        if not line:
            if cur.get("worktree"):
                items.append((cur["worktree"], cur.get("branch", "")))
            cur = {}
        elif " " in line:
            k, v = line.split(" ", 1)
            cur[k] = v
    return items[1:]  # the first entry is the main checkout


def branch_name(ref):
    return ref.replace("refs/heads/", "") or "detached"


def verify(repo, bead, commit, branch="main", remote="origin", preconditions=()):
    checks = []

    def check(name, ok, detail):
        checks.append({"check": name, "ok": ok, "detail": detail})
        return ok

    rc, out, err = _run(["bd", "show", bead, "--json"], repo)
    status = None
    if rc == 0:
        try:
            issue = json.loads(out)
            issue = issue[0] if isinstance(issue, list) else issue
            status = issue.get("status")
            check("bead-closed", status == "closed",
                  f"{bead} status={status} close_reason={issue.get('close_reason')!r}")
        except (ValueError, IndexError, AttributeError) as e:
            check("bead-closed", False, f"unparseable bd output: {e}")
    else:
        check("bead-closed", False, f"board unreadable: {err or out}")

    rc, out, err = _run(["git", "ls-remote", remote, f"refs/heads/{branch}"], repo)
    remote_sha = out.split()[0] if rc == 0 and out else None
    check("remote-readable", bool(remote_sha), remote_sha or f"ls-remote failed: {err}")
    if remote_sha and commit:
        rc, full, _ = _run(["git", "rev-parse", "--verify", f"{commit}^{{commit}}"], repo)
        if rc != 0:
            check("commit-on-remote", False, f"commit {commit} not found locally; cannot prove ancestry")
        else:
            rc, _, _ = _run(["git", "merge-base", "--is-ancestor", full, remote_sha], repo)
            check("commit-on-remote", rc == 0,
                  f"{full[:12]} {'is' if rc == 0 else 'is NOT'} an ancestor of {remote}/{branch} {remote_sha[:12]}")
            rc, trailer, _ = _run(["git", "log", "-1", "--format=%(trailers:key=Bead,valueonly)", full], repo)
            check("commit-names-bead", bead in trailer.split(),
                  f"commit trailer Bead: {trailer or '(none)'} (expected {bead})")
        rc, n, _ = _run(["git", "rev-list", "--count", f"{remote_sha}..HEAD"], repo)
        check("nothing-unpushed", rc == 0 and n == "0", f"{n} local commit(s) not on {remote}/{branch}")
    elif not commit:
        check("commit-on-remote", False, "the report named no commit")
    rc, out, _ = _run(["git", "status", "--porcelain", "--untracked-files=no"], repo)
    check("tree-clean", rc == 0 and not out, "clean" if not out else f"modified files:\n{out}")
    # A worker isolates each bead in a worktree named after it; if one is still there it must hold nothing unsaved.
    mine = [(wt, b) for wt, b in worktrees(repo) if b.endswith(f"worktree-{bead}") or wt.rstrip("/").endswith(f"/{bead}")]
    if not mine:
        check("bead-worktree-saved", True, "no worktree left for this bead")
    for wt, branch in mine:
        if branch.endswith(f"worktree-{bead}") or wt.rstrip("/").endswith(f"/{bead}"):
            rc, dirty, _ = _run(["git", "status", "--porcelain", "--untracked-files=no"], wt)
            ahead = ""
            if remote_sha:
                _, ahead, _ = _run(["git", "rev-list", "--count", f"{remote_sha}..HEAD"], wt)
            check("bead-worktree-saved", not dirty and ahead in ("", "0"),
                  f"{wt}: {'dirty' if dirty else 'clean'}, {ahead or 0} commit(s) not on {remote}/{branch_name(branch)}")

    from . import checks as chk
    tree = chk.commit_tree(repo, commit) if preconditions and commit else None
    try:
        for cmd in preconditions:  # dispatch checks: file checks read the reported commit, git checks the repo
            passed, detail = chk.run(cmd, repo, tree=tree)
            check(f"precondition: {cmd}", passed, detail + ("" if tree else " (no commit tree; ran in checkout)"))
    finally:
        if tree:
            import shutil
            shutil.rmtree(tree, ignore_errors=True)
    ok = all(c["ok"] for c in checks)
    note = "; ".join(f"{c['check']}={'ok' if c['ok'] else 'FAIL'} ({c['detail'].splitlines()[0]})"
                     for c in checks)
    return {"verified": ok, "bead": bead, "commit": commit, "checks": checks, "note": note}


def main(argv=None):
    p = argparse.ArgumentParser(prog="abacus-verify", description=__doc__.split("\n")[0])
    p.add_argument("--repo", required=True); p.add_argument("--bead", required=True)
    p.add_argument("--commit"); p.add_argument("--branch", help="default: .abacus/config.json default_branch, else main")
    p.add_argument("--record", action="store_true", help="write the result to the logbook")
    p.add_argument("--check", action="append", default=[], help="extra read-only precondition check (repeatable)")
    a = p.parse_args(argv)
    branch = a.branch
    if not branch:
        try:
            branch = json.load(open(os.path.join(a.repo, ".abacus", "config.json"))).get("default_branch", "main")
        except (OSError, ValueError):
            branch = "main"
    pre = list(a.check)
    try:  # the dispatch's own checks always run; a report is verified against what was dispatched
        pre += [c for c in logbook.dispatch_checks(logbook.connect(), a.bead) if c not in pre]
    except Exception:
        pass
    res = verify(a.repo, a.bead, a.commit, branch, preconditions=pre)
    if a.record:
        conn = logbook.connect()
        try:
            if res["verified"]:
                logbook.dispatch_verify(conn, a.bead, a.commit, res["note"])
                res["recorded"] = "dispatch verified"
            else:
                rep = conn.execute("SELECT id FROM reports WHERE bead = ? ORDER BY id DESC LIMIT 1",
                                   (a.bead,)).fetchone()
                if rep:
                    logbook.report_verified(conn, rep["id"], False, res["note"])
                res["recorded"] = "report marked NOT verified"
        except logbook.Refused as e:
            res["recorded"] = f"REFUSED {e.code}: {e}"
    print(json.dumps(res, indent=2))
    return 0 if res["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
