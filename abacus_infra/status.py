"""abacus-status: one screen (or one line) of what Abacus is doing and what needs the operator.

  abacus-status          needs-you cards, program status, in-flight dispatches, live sessions, recent results
  abacus-status --line   a single line for the Claude Code status line; reads only the logbook and the decisions
                         page (fast, no subprocesses) and prints nothing if Abacus isn't set up
  abacus-status --line --wrap
                         the operator's own status line (saved by `abacus statusline enable --wrap`), run with
                         the same stdin and printed unchanged, with the Abacus segment appended to its last line
"""
import argparse
import json
import re
import sqlite3
import subprocess
import sys

from . import config

LINE_PREFIX = "abacus"
WRAP_SEPARATOR = "  |  "
WRAP_TIMEOUT = 10  # seconds; a hung status line of the operator's must not hang ours too


def decisions():
    """(program status, [needs-you card titles]) parsed from the orchestrator's decisions page."""
    page = config.home() / "agents" / "orchestrator" / "pages" / "decisions.md"
    if not page.exists():
        return None, []
    text = page.read_text()
    status = None
    m = re.search(r"Program status:?\**\s*:?\s*\n?\s*\**\s*([A-Z][A-Z ]+[A-Z])", text)
    if m:
        status = m.group(1).strip()
    cards = []
    sec = re.search(r"## NEEDS YOU[^\n]*\n(.*?)(?=\n## |\Z)", text, re.S)
    head = re.search(r"## NEEDS YOU([^\n]*)", text)
    if sec:
        body = sec.group(1).strip()
        titles = re.findall(r"^###\s+(.+)$", body, re.M)
        if titles:
            cards = titles
        elif body and not re.match(r"^(\(.*?\)\s*)?none\b", body, re.I):
            cards = [body.splitlines()[0][:100]]
    if not cards and head and re.search(r"\b[1-9]\d*\b", head.group(1) or "") and "none" not in head.group(1).lower():
        cards = [head.group(1).strip(" :")]
    return status, cards


def logbook_rows():
    db = config.logbook_path()
    if not db.exists():
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        flight = [dict(r) for r in conn.execute(
            "SELECT bead, worker, status, verified_commit FROM dispatches WHERE status NOT IN ('closed', 'cancelled') "
            "ORDER BY id")]
        recent = [dict(r) for r in conn.execute(
            "SELECT bead, worker, verified_commit, verified_at FROM dispatches WHERE verified_commit IS NOT NULL "
            "ORDER BY verified_at DESC LIMIT 5")]
        try:
            unrelayed = conn.execute(
                "SELECT count(*) FROM dispatches d JOIN rulings r ON r.target_item = d.bead WHERE d.status NOT IN "
                "('closed','cancelled') AND NOT EXISTS (SELECT 1 FROM ruling_relays rl WHERE rl.ruling_id = r.id "
                "AND rl.worker = d.worker)").fetchone()[0]
        except sqlite3.Error:
            unrelayed = 0
        try:
            events = [dict(r) for r in conn.execute(
                "SELECT at, name, action, by, reason FROM session_events ORDER BY id DESC LIMIT 6")]
        except sqlite3.Error:
            events = []
        return {"flight": flight, "recent": recent, "unrelayed": unrelayed, "events": events}
    finally:
        conn.close()


def line():
    status, cards = decisions()
    lb = logbook_rows()
    if lb is None and status is None and not cards:
        return ""  # Abacus not set up here: say nothing rather than clutter the status line
    parts = [LINE_PREFIX]
    if cards:
        parts.append(f"{len(cards)} needs you")
    inflight = len(lb["flight"]) if lb else 0
    if inflight:
        parts.append(f"{inflight} in flight")
    if not cards and not inflight:
        parts.append(status.lower() if status else "idle")
    return " · ".join(parts)


def wrapped(stdin_text):
    """The operator's own status line, unchanged, plus the Abacus segment. Never fails: whichever half works shows."""
    out = ""
    try:
        saved = json.loads((config.state_dir() / "statusline.backup.json").read_text())
        cmd = saved.get("command") if isinstance(saved, dict) else None
        if cmd:
            out = subprocess.run(cmd, shell=True, input=stdin_text, capture_output=True, text=True,
                                 timeout=WRAP_TIMEOUT).stdout.rstrip("\n")
    except (OSError, ValueError, subprocess.SubprocessError):
        out = ""
    try:
        seg = line()
    except Exception:
        seg = ""
    if not seg:
        return out
    return f"{out}{WRAP_SEPARATOR}{seg}" if out else seg


def live_sessions():
    try:
        out = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=20).stdout
        return [s for s in json.loads(out or "[]") if s.get("pid") and
                str(s.get("name", "")).startswith(("agent:", "coder:"))]
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def full():
    status, cards = decisions()
    lb = logbook_rows() or {"flight": [], "recent": [], "unrelayed": 0, "events": []}
    from .logbook import clock
    out = [f"As of {clock()}"]
    if cards:
        out.append(f"Needs you ({len(cards)}), answer in agent:concierge:")
        out += [f"  - {c}" for c in cards]
    else:
        out.append("Needs you: nothing")
    out.append(f"Program: {status or 'no program page yet'}")
    if lb["flight"]:
        out.append("In flight:")
        out += [f"  - {d['bead']} @ {d['worker']}: {d['status']}" +
                (f" (verified {d['verified_commit'][:8]})" if d.get("verified_commit") else "") for d in lb["flight"]]
    else:
        out.append("In flight: nothing")
    if lb["unrelayed"]:
        out.append(f"Rulings not yet relayed to their workers: {lb['unrelayed']}")
    sessions = live_sessions()
    out.append("Sessions: " + (", ".join(f"{s['name']} ({s.get('status') or s.get('state') or '?'})" for s in sessions)
                               if sessions else "none running"))
    if lb.get("events"):
        out.append("Session starts/stops (newest first):")
        out += [f"  - {e['at'][11:16]} {e['name']} {e['action']} by {e['by']}" +
                (f": {e['reason']}" if e.get("reason") else "") for e in lb["events"]]
    if lb["recent"]:
        out.append("Recently verified:")
        out += [f"  - {d['bead']} at {d['verified_commit'][:8]} ({(d.get('verified_at') or '')[:16]})" for d in lb["recent"]]
    return "\n".join(out)


def main(argv=None):
    p = argparse.ArgumentParser(prog="abacus-status", description=__doc__.split("\n")[0])
    p.add_argument("--line", action="store_true", help="one line, for the Claude Code status line")
    p.add_argument("--wrap", action="store_true", help="with --line: append to the operator's own status line")
    a = p.parse_args(argv)
    if a.line:
        stdin_text = ""
        if not sys.stdin.isatty():
            try:
                stdin_text = sys.stdin.read()  # Claude Code passes session JSON on stdin
            except OSError:
                pass
        if a.wrap:
            print(wrapped(stdin_text))
            return 0
        try:
            print(line())
        except Exception:
            print("")
        return 0
    print(full())
    return 0


if __name__ == "__main__":
    sys.exit(main())
