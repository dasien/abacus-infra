"""abacus-session: the controlled launch path for worker and role sessions.

Every session is named <role>:<repo> (e.g. coder:repo-a, agent:orchestrator),
started in the background in Agent View from the repository it owns, with the
model and permission mode the registry assigns. The launcher refuses by named
code rather than improvising:

  ROLE       the name is not in the deployed fleet registry
  ATTENDED   the operator is driving that repository (watch, do not orchestrate)
  BUSY       a live session with that name already exists
  UNSAVED    the tree is dirty or has unpushed commits (start/stop both refuse)
  AUTHORITY  bypassPermissions requested but the repository has not earned it
             (abacus-doctor does not report its guards ADOPTED)
  MODEL      the registry names a model outside the allowed set
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import config, logbook

MODELS = ("haiku", "sonnet", "opus", "fable", "inherit")
MODES = ("default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan")


class Refused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def live_sessions():
    try:
        out = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=30)
        return json.loads(out.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def caller(sessions=None):
    """Who is running this command: the Abacus session (by name) whose tool call this is, else the operator.

    Background sessions are spawned by Agent View's supervisor, so variables the launcher sets don't reach their
    commands. Claude Code does export CLAUDE_CODE_SESSION_ID to every tool call; map it to the session's name.
    """
    if os.environ.get("ABACUS_SESSION_NAME"):
        return os.environ["ABACUS_SESSION_NAME"]
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        for s in (live_sessions() if sessions is None else sessions):
            if s.get("sessionId") == sid:
                name = str(s.get("name") or "")
                return name if name.startswith(("agent:", "coder:")) else "operator"
    return "operator"


def find_live(name):
    """A running session with this name. Stopped sessions stay listed (no pid) so they can be attached later."""
    return next((s for s in live_sessions() if s.get("name") == name and s.get("pid")), None)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _unsaved_in(path):
    dirty = _git(path, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        return f"modified files:\n{dirty}"
    ahead = _git(path, "rev-list", "--count", "@{u}..HEAD")
    if ahead.returncode == 0 and ahead.stdout.strip() not in ("", "0"):
        return f"{ahead.stdout.strip()} unpushed commit(s)"
    return None


def unsaved(repo):
    """Unsaved work in the checkout or in any of its worktrees (workers isolate each bead in one)."""
    if _git(repo, "rev-parse", "--git-dir").returncode != 0:
        return None
    problems = []
    why = _unsaved_in(repo)
    if why:
        problems.append(why)
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    paths = [l.split(" ", 1)[1] for l in out.splitlines() if l.startswith("worktree ")][1:]
    for wt in paths:
        if not os.path.isdir(wt):
            continue
        why = _unsaved_in(wt)
        if why:
            problems.append(f"worktree {wt}: {why}")
    return "\n".join(problems) or None


def earned_bypass(repo, entry):
    """A repository earns bypassPermissions only when its guards are verified present."""
    if entry.get("authority_check") == "deployed":
        manifest = config.home() / "agents" / ".deployed"
        hooks = Path(repo) / ".claude" / "settings.json"
        return manifest.exists() and hooks.exists(), "agents space deployed with guards wired"
    doctor = shutil.which("abacus-doctor")
    if not doctor:
        return False, "abacus-doctor not on PATH"
    p = subprocess.run([doctor, "--json", "--check", "guards"], cwd=repo, capture_output=True, text=True)
    try:
        verdict = json.loads(p.stdout)["checks"][0]["verdict"]
    except (ValueError, KeyError, IndexError):
        return False, f"doctor output unreadable: {p.stderr.strip()[:200]}"
    return verdict == "ADOPTED", f"doctor guards verdict: {verdict}"


def start(name, conn, prompt=None, dry_run=False, reason=None):
    fleet = config.fleet()
    entry = fleet.get(name)
    m = re.match(r"^(.+)-g(\d+)$", name)
    if entry is None and m and fleet.get(m.group(1), {}).get("generations"):
        entry = fleet[m.group(1)]  # a handover successor: same role, new generation name
    if entry is None:
        raise Refused("ROLE", f"{name} is not in the fleet registry ({config.home() / 'agents' / 'fleet.json'})")
    repo = Path(os.path.expanduser(entry["repo"])).resolve()
    if not repo.is_dir():
        raise Refused("ROLE", f"{name}: repository {repo} does not exist")
    w = conn.execute("SELECT mode FROM workers WHERE name = ?", (name,)).fetchone()
    if w and w["mode"] == "attended":
        raise Refused("ATTENDED", f"the operator is driving {name}")
    if find_live(name):
        raise Refused("BUSY", f"a live session named {name} already exists (see `claude agents`)")
    why = unsaved(repo)
    if why:
        raise Refused("UNSAVED", f"{repo} has unsaved work; a fresh worker would inherit it:\n{why}")
    model = entry.get("model", "sonnet")
    if model not in MODELS:
        raise Refused("MODEL", f"model {model!r} not in {MODELS}")
    mode = entry.get("permission_mode", "auto")
    if mode not in MODES:
        raise Refused("AUTHORITY", f"permission mode {mode!r} not in {MODES}")
    if mode == "bypassPermissions":
        ok, detail = earned_bypass(repo, entry)
        if not ok:
            raise Refused("AUTHORITY", f"{name} has not earned bypassPermissions: {detail}")
    cmd = ["claude", "--bg", "-n", name, "--permission-mode", mode]
    # Hand every session the shared-memory server explicitly: project .mcp.json discovery is not reliable for
    # every directory (it never loaded in the orchestrator's), and background sessions cannot answer prompts.
    memory = str(config.home() / "bin" / "abacus-memory")
    cmd += ["--mcp-config", json.dumps({"mcpServers": {"abacus-memory": {
        "type": "stdio", "command": memory, "args": ["serve-mcp"]}}})]
    if model != "inherit":
        cmd += ["--model", model]
    if entry.get("agent"):
        cmd += ["--agent", entry["agent"]]
    cmd.append(prompt or entry.get("prompt") or f"You are {name}. Run /abacus-worker and wait for dispatches.")
    if dry_run:
        return {"would_run": cmd, "cwd": str(repo)}
    env = dict(os.environ, PATH=f"{config.home() / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
               ABACUS_SESSION_NAME=name)  # so this session's own start/stop calls are attributed to it
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, env=env)
    if p.returncode != 0:
        err = p.stderr.strip() or p.stdout.strip()
        if "not trusted" in err.lower():
            raise Refused("TRUST", f"Claude Code has not been told to trust {repo}. Trusting a workspace is the "
                          f"operator's call: run `claude` there once in a terminal, accept the trust prompt, exit, "
                          f"then retry. Trust is per path, so it survives re-creating the demo.")
        if "disclaimer" in err:
            raise Refused("OPERATOR", "bypassPermissions needs the operator's one-time acceptance of Claude Code's "
                          "disclaimer: run `claude --dangerously-skip-permissions` once in a terminal, accept, exit, "
                          "then retry. (Or set this worker's permission_mode to \"auto\" in ~/.abacus/fleet.json.)")
        raise Refused("LAUNCH", f"claude --bg failed: {err}")
    by = caller()
    logbook.worker_upsert(conn, name, str(repo), started_by=by, state="idle")
    logbook.session_event(conn, name, "started", by, reason)
    return {"started": name, "cwd": str(repo), "model": model, "permission_mode": mode,
            "output": p.stdout.strip()}


def stop(name, conn, force_unsaved=False, reason=None):
    entry = config.fleet().get(name, {})
    s = find_live(name)
    repo = entry.get("repo") or (s or {}).get("cwd")
    if repo and not force_unsaved:
        why = unsaved(Path(os.path.expanduser(repo)))
        if why:
            raise Refused("UNSAVED", f"{name} has unsaved work; ask it to record a resume point first:\n{why}")
    if s is None:
        result = {"stopped": name, "note": "no live session found"}
    else:
        job = s.get("id") or s["sessionId"]  # `claude stop` takes the short background job id, not the session uuid
        by = caller()
        # Record first: a session stopping itself may be killed before this process could write afterwards.
        logbook.session_event(conn, name, "stopped", by, reason)
        p = subprocess.run(["claude", "stop", job], capture_output=True, text=True)
        if p.returncode != 0:
            err = p.stderr.strip() or p.stdout.strip()
            logbook.session_event(conn, name, "stop_failed", by, err[:200])
            raise Refused("STOP", err)
        result = {"stopped": name, "job": job}
    try:
        logbook.worker_state(conn, name, "gone")
    except logbook.Refused:
        pass
    return result


def select_prunable(sessions, keep=1, older_than_hours=0.0, now_ms=None):
    """Stopped Abacus session records to remove: never a running one (it has a pid), always keeping the `keep` most
    recent stopped records per name, and only those older than `older_than_hours`."""
    import time
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    by_name = {}
    for s in sessions:
        name = str(s.get("name") or "")
        if name.startswith(("agent:", "coder:")) and not s.get("pid"):
            by_name.setdefault(name, []).append(s)
    out = []
    for name, recs in by_name.items():
        recs.sort(key=lambda s: s.get("startedAt") or 0, reverse=True)
        for s in recs[keep:]:
            if (now_ms - (s.get("startedAt") or 0)) / 3_600_000 >= older_than_hours:
                out.append(s)
    return out


def prune(keep=1, older_than_hours=0.0, dry_run=False):
    """Remove Agent View records of stopped Abacus sessions (Agent View keeps them, and labels sessions stopped
    mid-turn as "working"). Durable state lives in the logbook, retros, boards and git, not in these records.
    `claude rm` refuses to delete a worktree holding unsaved work; such records are reported and kept."""
    try:
        out = subprocess.run(["claude", "agents", "--json", "--all"], capture_output=True, text=True, timeout=60)
        sessions = json.loads(out.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise Refused("LIST", f"could not list sessions: {e}")
    removed, kept = [], []
    for s in select_prunable(sessions, keep, older_than_hours):
        label = f"{s.get('name')} ({s.get('id')}, {s.get('state') or 'stopped'})"
        if dry_run:
            removed.append(label); continue
        p = subprocess.run(["claude", "rm", str(s.get("id"))], capture_output=True, text=True)
        (removed if p.returncode == 0 else kept).append(
            label if p.returncode == 0 else f"{label}: {(p.stderr or p.stdout).strip()[:160]}")
    live = sorted(str(s.get("name")) for s in sessions if s.get("pid") and str(s.get("name", "")).startswith(("agent:", "coder:")))
    return {"would_remove" if dry_run else "removed": removed, "kept_with_unsaved_work": kept, "running": live}


def liveness(conn):
    """Reconcile Agent View (authoritative for live sessions) into the logbook."""
    fleet = config.fleet()
    live = {s.get("name"): s for s in live_sessions() if s.get("pid")}
    out = []
    state_map = {"busy": "busy", "idle": "idle", "waiting": "waiting"}
    for name, entry in sorted(fleet.items()):
        s = live.get(name)
        state = state_map.get((s or {}).get("status"), "idle") if s else "gone"
        known = conn.execute("SELECT mode FROM workers WHERE name = ?", (name,)).fetchone()
        if known:
            logbook.worker_state(conn, name, state)
        out.append({"name": name, "repo": entry["repo"], "agent_view": (s or {}).get("status", "absent"),
                    "logbook_state": state, "mode": known["mode"] if known else "unregistered"})
    return out


def main(argv=None):
    p = argparse.ArgumentParser(prog="abacus-session", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start"); s.add_argument("name"); s.add_argument("--prompt")
    s.add_argument("--reason", help="why this session is being started (recorded)")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("stop"); s.add_argument("name")
    s.add_argument("--reason", help="why this session is being stopped (recorded, shown in abacus-status)")
    s.add_argument("--force-unsaved", action="store_true", help="operator only: discard the unsaved-work refusal")
    sub.add_parser("list", help="fleet liveness from Agent View, reconciled into the logbook")
    s = sub.add_parser("prune", help="remove Agent View records of stopped Abacus sessions (never running ones)")
    s.add_argument("--keep", type=int, default=1, help="stopped records to keep per session name (default 1)")
    s.add_argument("--older-than", type=float, default=0.0, metavar="HOURS", help="only records older than this")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("attend", help="the operator is now driving this session")
    s.add_argument("name")
    s = sub.add_parser("release", help="hand an attended session back to the orchestrator")
    s.add_argument("name")
    a = p.parse_args(argv)
    conn = logbook.connect()
    try:
        if a.cmd == "start":
            res = start(a.name, conn, a.prompt, a.dry_run, a.reason)
        elif a.cmd == "stop":
            res = stop(a.name, conn, a.force_unsaved, a.reason)
        elif a.cmd == "list":
            res = liveness(conn)
        elif a.cmd == "prune":
            res = prune(a.keep, a.older_than, a.dry_run)
        else:
            entry = config.fleet().get(a.name)
            if entry is None:
                raise Refused("ROLE", f"{a.name} is not in the fleet registry")
            logbook.worker_upsert(conn, a.name, entry["repo"],
                                  mode="attended" if a.cmd == "attend" else "orchestrated")
            res = {a.name: "attended" if a.cmd == "attend" else "orchestrated"}
    except (Refused, logbook.Refused) as e:
        print(f"REFUSED {e.code}: {e}", file=sys.stderr)
        return 3
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
