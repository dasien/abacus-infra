"""abacus-status: one screen (or one line) of what Abacus is doing and what needs the operator.

  abacus-status          needs-you cards, a line per lane, in-flight dispatches, live sessions, recent results
  abacus-status --lane L only lane L: its goal and repositories, its cards, its work, its sessions
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


def pages_dir():
    return config.home() / "agents" / "orchestrator" / "pages"


def page_for(lane=None):
    """Each lane keeps its own decisions page; work outside any lane uses decisions.md."""
    return pages_dir() / (f"decisions-{lane}.md" if lane else "decisions.md")


def decisions(lane=None):
    """(program status, [needs-you card titles]) parsed from a decisions page (the lane's, if given)."""
    page = page_for(lane)
    if not page.exists():
        return None, []
    return parse_page(page.read_text())


def all_decisions():
    """{lane or None: (status, cards)} for every decisions page."""
    out = {}
    d = pages_dir()
    if d.is_dir():
        for p in sorted(d.glob("decisions*.md")):
            lane = p.stem[len("decisions-"):] if p.stem.startswith("decisions-") else None
            out[lane] = parse_page(p.read_text())
    return out


def parse_page(text):
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


def logbook_rows(lane=None):
    db = config.logbook_path()
    if not db.exists():
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        has_lane = "lane" in [r[1] for r in conn.execute("PRAGMA table_info(dispatches)")]
        lane_col = "lane" if has_lane else "NULL AS lane"
        where = " AND lane = :lane" if lane and has_lane else (" AND 0" if lane else "")
        flight = [dict(r) for r in conn.execute(
            f"SELECT bead, worker, status, verified_commit, {lane_col} FROM dispatches "
            f"WHERE status NOT IN ('closed', 'cancelled'){where} ORDER BY id", {"lane": lane})]
        recent = [dict(r) for r in conn.execute(
            f"SELECT bead, worker, verified_commit, verified_at, {lane_col} FROM dispatches "
            f"WHERE verified_commit IS NOT NULL{where} ORDER BY verified_at DESC LIMIT 5", {"lane": lane})]
        try:
            lanes = []
            for l in conn.execute("SELECT name, goal FROM lanes WHERE status = 'active' ORDER BY created_at"):
                repos = [r[0] for r in conn.execute("SELECT repo FROM lane_repos WHERE lane = ?", (l["name"],))]
                lanes.append({"name": l["name"], "goal": l["goal"], "repos": repos})
        except sqlite3.Error:
            lanes = []
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
        return {"flight": flight, "recent": recent, "unrelayed": unrelayed, "events": events, "lanes": lanes}
    finally:
        conn.close()


def line():
    pages = all_decisions()
    lb = logbook_rows()
    if lb is None and not pages:
        return ""  # Abacus not set up here: say nothing rather than clutter the status line
    cards = sum(len(c) for _, c in pages.values())
    inflight = len(lb["flight"]) if lb else 0
    active = len(lb["lanes"]) if lb else 0
    parts = [LINE_PREFIX]
    if active > 1:
        parts.append(f"{active} lanes")
    if cards:
        parts.append(f"{cards} needs you")
    if inflight:
        parts.append(f"{inflight} in flight")
    if not cards and not inflight:
        status = (pages.get(None) or (None, []))[0] if active <= 1 else None
        if active == 1 and not status:
            status = (pages.get(lb["lanes"][0]["name"]) or (None, []))[0]
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


def _sessions_line(sessions):
    return "Sessions: " + (", ".join(f"{s['name']} ({s.get('status') or s.get('state') or '?'})" for s in sessions)
                           if sessions else "none running")


def full(lane=None):
    """One screen for the operator. With a lane, only that lane: the operator asked about that work, nothing else."""
    from .logbook import clock
    lb = logbook_rows(lane) or {"flight": [], "recent": [], "unrelayed": 0, "events": [], "lanes": []}
    out = [f"As of {clock()}"]
    if lane:
        info = next((l for l in lb["lanes"] if l["name"] == lane), None)
        if info is None:
            names = ", ".join(l["name"] for l in lb["lanes"]) or "none"
            return "\n".join(out + [f"No active lane called '{lane}'. Active lanes: {names}."])
        out.append(f"Lane {lane}: {info['goal']}")
        out.append("Repositories: " + (", ".join(r.rsplit('/', 1)[-1] for r in info["repos"]) or "none yet"))
        status, cards = decisions(lane)
        if cards:
            out.append(f"Needs you ({len(cards)}), answer in agent:concierge:")
            out += [f"  - {c}" for c in cards]
        else:
            out.append("Needs you: nothing")
        out.append(f"Program: {status or 'no page yet'}")
        mine = {f"agent:orchestrator-{lane}"} | {f"coder:{r.rsplit('/', 1)[-1]}" for r in info["repos"]}
        sessions = [s for s in live_sessions() if s["name"] in mine or s["name"].startswith(f"agent:orchestrator-{lane}-g")]
    else:
        pages = all_decisions()
        needs = [(l, c) for l, (_, cs) in pages.items() for c in cs]
        if needs:
            out.append(f"Needs you ({len(needs)}), answer in agent:concierge:")
            out += [f"  - {'[' + l + '] ' if l else ''}{c}" for l, c in needs]
        else:
            out.append("Needs you: nothing")
        if lb["lanes"]:
            out.append("Lanes:")
            for l in lb["lanes"]:
                st, cs = pages.get(l["name"], (None, []))
                n = sum(1 for d in lb["flight"] if d.get("lane") == l["name"])
                out.append(f"  - {l['name']}: {n} in flight, {(st or 'starting').lower()}"
                           + (f", {len(cs)} needs you" if cs else "") + f" ({l['goal'][:60]})")
        if None in pages or not lb["lanes"]:
            out.append(f"Program: {(pages.get(None) or (None, []))[0] or 'no program page yet'}")
        sessions = live_sessions()
    if lb["flight"]:
        out.append("In flight:")
        out += [f"  - {d['bead']} @ {d['worker']}: {d['status']}" + (f" [{d['lane']}]" if d.get("lane") and not lane else "")
                + (f" (verified {d['verified_commit'][:8]})" if d.get("verified_commit") else "") for d in lb["flight"]]
    else:
        out.append("In flight: nothing")
    if lb["unrelayed"] and not lane:
        out.append(f"Rulings not yet relayed to their workers: {lb['unrelayed']}")
    out.append(_sessions_line(sessions))
    if lb.get("events") and not lane:
        out.append("Session starts/stops (newest first):")
        out += [f"  - {e['at'][11:16]} {e['name']} {e['action']} by {e['by']}" +
                (f": {e['reason']}" if e.get("reason") else "") for e in lb["events"]]
    if lb["recent"]:
        out.append("Recently verified:")
        out += [f"  - {d['bead']} at {d['verified_commit'][:8]} ({(d.get('verified_at') or '')[:16]})"
                for d in lb["recent"]]
    return "\n".join(out)


def main(argv=None):
    p = argparse.ArgumentParser(prog="abacus-status", description=__doc__.split("\n")[0])
    p.add_argument("--line", action="store_true", help="one line, for the Claude Code status line")
    p.add_argument("--lane", help="only this lane")
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
    print(full(a.lane))
    return 0


if __name__ == "__main__":
    sys.exit(main())
