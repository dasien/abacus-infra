"""Checkable dispatch preconditions: small read-only commands abacus-verify runs in the worker's repo.

A dispatch can carry checks such as
    grep -q 0db5d96c87918ccaf1bf6c1aa12c8c87d882002b vendor.json
    git --git-dir ../remotes/repo-a.git grep -q word_count 0db5d96c -- textkit
so "B pins A's verified commit" is verified by a machine, not by the orchestrator reading files.

verify runs outside any guarded session, so a check must be unable to change anything:
  * it is executed directly (no shell): `|`, `>`, `;`, `$(...)` are literal text, never operators
  * the program must be on a read-only allowlist; git only with read subcommands and none of the
    options that write files or run external programs
  * it runs with a minimal environment and a timeout
A check passes when it exits 0.
"""
import os
import shlex
import subprocess

ALLOWED = {"test", "[", "grep", "egrep", "fgrep", "cat", "head", "tail", "wc", "ls", "cmp", "diff", "stat", "git"}
GIT_READ = {"show", "log", "cat-file", "rev-parse", "merge-base", "ls-remote", "ls-files", "ls-tree", "grep", "diff",
            "describe", "rev-list", "show-ref", "for-each-ref", "status", "name-rev", "branch", "tag"}
GIT_GLOBAL_WITH_VALUE = {"-C", "--git-dir", "--work-tree", "--namespace"}
FORBIDDEN_PREFIXES = ("--output", "--ext-diff", "--textconv", "--exec-path", "--upload-pack", "--receive-pack",
                      "--open-files-in-pager", "-O")
TIMEOUT = 30


class BadCheck(ValueError):
    pass


def parse(cmd):
    """Validate a check and return its argv. Raises BadCheck with a reason."""
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        raise BadCheck(f"cannot parse check ({e})")
    if not argv:
        raise BadCheck("empty check")
    prog = os.path.basename(argv[0])
    if prog not in ALLOWED:
        raise BadCheck(f"`{prog}` is not an allowed read-only check program ({', '.join(sorted(ALLOWED))})")
    for a in argv[1:]:
        if a.startswith(FORBIDDEN_PREFIXES):
            raise BadCheck(f"option {a!r} can write files or run programs; not allowed in a check")
    if prog == "git":
        i = 1
        while i < len(argv) and argv[i].startswith("-"):
            a = argv[i]
            if a == "-c" or a.startswith("--config"):
                raise BadCheck("git -c / --config can run arbitrary programs; not allowed in a check")
            if a in GIT_GLOBAL_WITH_VALUE:
                i += 2
                continue
            i += 1
        sub = argv[i] if i < len(argv) else ""
        if sub not in GIT_READ:
            raise BadCheck(f"`git {sub}` is not a read-only git subcommand")
        rest = argv[i + 1:]
        if sub in ("branch", "tag") and any(not a.startswith("-") for a in rest) and \
                not any(a in ("-l", "--list", "--contains", "--points-at", "--merged") for a in rest):
            raise BadCheck(f"`git {sub}` with a name creates refs; use --list/--contains")
    return argv


def commit_tree(repo, commit):
    """Extract the files of `commit` into a temp dir with `git archive` (read-only for the repo).

    Workers commit from per-bead worktrees, so the main checkout's files can lag origin; a check about the work
    must read the reported commit, not whatever happens to be checked out. Returns the dir, or None.
    """
    import tarfile, tempfile, io
    p = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit], capture_output=True)
    if p.returncode != 0:
        return None
    d = tempfile.mkdtemp(prefix="abacus-check-")
    with tarfile.open(fileobj=io.BytesIO(p.stdout)) as t:
        t.extractall(d)
    return d


def run(cmd, cwd, tree=None):
    """Run a validated check. Returns (passed, detail).

    File checks (grep, cat, test, ...) run in `tree` (the reported commit's files) when given; git checks always
    run in the repository `cwd`, where history and remotes live.
    """
    try:
        argv = parse(cmd)
    except BadCheck as e:
        return False, f"refused: {e}"
    if tree and os.path.basename(argv[0]) != "git":
        cwd = tree
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": os.environ.get("HOME", "/tmp"),
           "LANG": "C", "GIT_PAGER": "cat", "PAGER": "cat", "GIT_TERMINAL_PROMPT": "0"}
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {TIMEOUT}s"
    except OSError as e:
        return False, f"could not run: {e}"
    out = (p.stdout or p.stderr).strip().splitlines()
    return p.returncode == 0, f"exit {p.returncode}" + (f": {out[0][:120]}" if out else "")
