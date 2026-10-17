"""The logbook: durable operating state for the Abacus orchestrator.

One SQLite database behind one CLI. The orchestrator never runs SQL; every
write goes through a verb here so the status vocabulary and preconditions stay
constrained and testable.

Two kinds of tables:
  * current-state tables, updated in place: workers, programs, dispatches,
    promises, facts, handovers
  * event tables, insert-only (enforced by triggers): messages, reports,
    report_verifications, observations, checkins, rulings, ruling_records, ruling_relays

The repository's Beads board is always authoritative for the work itself. The
logbook records what the fleet *believed* was happening so a fresh session can
reconcile it against the boards.
"""
import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys

from . import config

DISPATCH_STATUSES = (
    "queued", "sent", "in_progress", "reported", "verified",
    "blocked", "needs_ruling", "paused", "closed", "cancelled",
)
OPEN_EXCLUDES = ("closed", "cancelled")
REPORT_STATUSES = ("CLOSED", "BLOCKED", "NEEDS-RULING", "PROGRESS", "OTHER")
WORKER_STATES = ("busy", "idle", "waiting", "gone")
WORKER_MODES = ("orchestrated", "attended")
HANDOVER_STATES = ("launched", "recovered", "finished", "aborted", "adopted")
DIGEST_LIMIT = 1000

INSERT_ONLY = (
    "messages", "reports", "report_verifications", "observations",
    "rulings", "ruling_records", "ruling_targets", "ruling_relays", "checkin_events", "session_events",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
  name TEXT PRIMARY KEY,
  repo TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'orchestrated',
  started_by TEXT,
  last_state TEXT NOT NULL DEFAULT 'idle',
  paused INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS programs (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  goal TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lanes (
  name TEXT PRIMARY KEY,
  goal TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
  created_at TEXT NOT NULL,
  closed_at TEXT
);
CREATE TABLE IF NOT EXISTS lane_repos (
  lane TEXT NOT NULL REFERENCES lanes(name) ON UPDATE CASCADE,
  repo TEXT NOT NULL,
  PRIMARY KEY (lane, repo)
);
CREATE TABLE IF NOT EXISTS lane_items (
  lane TEXT NOT NULL REFERENCES lanes(name) ON UPDATE CASCADE,
  item TEXT NOT NULL,
  PRIMARY KEY (lane, item)
);
CREATE TABLE IF NOT EXISTS dispatches (
  id INTEGER PRIMARY KEY,
  bead TEXT NOT NULL,
  repo TEXT NOT NULL,
  worker TEXT NOT NULL,
  pipeline TEXT,
  program TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  order_note TEXT,
  preconditions TEXT,
  report_to TEXT NOT NULL DEFAULT 'agent:orchestrator',
  verified_commit TEXT,
  verified_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_dispatch_per_bead
  ON dispatches(bead) WHERE status NOT IN ('closed', 'cancelled');
CREATE TABLE IF NOT EXISTS promises (
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,
  trigger TEXT,
  due_at TEXT,
  work_item TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  resolution TEXT,
  created_at TEXT NOT NULL,
  lane TEXT
);
CREATE TABLE IF NOT EXISTS facts (
  key TEXT PRIMARY KEY,
  value TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handovers (
  id INTEGER PRIMARY KEY,
  from_session TEXT NOT NULL,
  to_session TEXT NOT NULL,
  state TEXT NOT NULL,
  working_set TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkins (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkin_events (
  id INTEGER PRIMARY KEY,
  checkin_id INTEGER NOT NULL REFERENCES checkins(id),
  at TEXT NOT NULL,
  kind TEXT NOT NULL,
  summary TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
  peer TEXT NOT NULL,
  first_line TEXT NOT NULL,
  body TEXT NOT NULL,
  delivery TEXT
);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  worker TEXT NOT NULL,
  bead TEXT NOT NULL,
  status TEXT NOT NULL,
  commit_sha TEXT,
  pushed INTEGER,
  evidence TEXT,
  ruling_needed TEXT,
  next TEXT
);
CREATE TABLE IF NOT EXISTS report_verifications (
  id INTEGER PRIMARY KEY,
  report_id INTEGER NOT NULL REFERENCES reports(id),
  at TEXT NOT NULL,
  verified INTEGER NOT NULL,
  note TEXT NOT NULL CHECK (length(trim(note)) > 0)
);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  checkin_id INTEGER,
  subject TEXT NOT NULL,
  text TEXT NOT NULL,
  gap INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rulings (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  words TEXT NOT NULL CHECK (length(trim(words)) > 0),
  said_where TEXT NOT NULL,
  question TEXT,
  target_item TEXT
);
CREATE TABLE IF NOT EXISTS ruling_records (
  id INTEGER PRIMARY KEY,
  ruling_id INTEGER NOT NULL REFERENCES rulings(id),
  at TEXT NOT NULL,
  recorded_on TEXT NOT NULL,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS session_events (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  name TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('started', 'stopped', 'stop_failed')),
  by TEXT NOT NULL,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS ruling_targets (
  id INTEGER PRIMARY KEY,
  ruling_id INTEGER NOT NULL REFERENCES rulings(id),
  bead TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ruling_relays (
  id INTEGER PRIMARY KEY,
  ruling_id INTEGER NOT NULL REFERENCES rulings(id),
  at TEXT NOT NULL,
  worker TEXT NOT NULL
);
"""

# A ruling "covers" a bead when it targets it or was recorded on it; every worker dispatched on a covered bead
# must have been sent the ruling (a RULING message) before the dispatch is recorded.
COVERING = ("SELECT r.id FROM rulings r WHERE r.target_item = :bead OR EXISTS "
            "(SELECT 1 FROM ruling_records rr WHERE rr.ruling_id = r.id AND rr.recorded_on = :bead) OR EXISTS "
            "(SELECT 1 FROM ruling_targets rt WHERE rt.ruling_id = r.id AND rt.bead = :bead)")
UNRELAYED = ("SELECT d.bead, d.worker, r.id AS ruling, r.words FROM dispatches d JOIN rulings r ON "
             "(r.target_item = d.bead OR EXISTS (SELECT 1 FROM ruling_records rr WHERE rr.ruling_id = r.id "
             "AND rr.recorded_on = d.bead) OR EXISTS (SELECT 1 FROM ruling_targets rt WHERE rt.ruling_id = r.id "
             "AND rt.bead = d.bead)) WHERE d.status NOT IN ('closed', 'cancelled') AND NOT EXISTS "
             "(SELECT 1 FROM ruling_relays rl WHERE rl.ruling_id = r.id AND rl.worker = d.worker) ORDER BY d.id, r.id")


class Refused(Exception):
    """A precondition refused the write. The code is stable and machine-readable."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def clock():
    """The current time in UTC (what the logbook records) and local time (what the operator sees)."""
    utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    local = utc.astimezone()
    return f"{utc.strftime('%Y-%m-%d %H:%M:%S')} UTC ({local.strftime('%H:%M %Z')} local)"


def connect(path=None):
    path = path or config.logbook_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(dispatches)")]
    if "checks" not in cols:  # in-place migration for logbooks created before checkable preconditions
        conn.execute("ALTER TABLE dispatches ADD COLUMN checks TEXT")
    if "requires_verified" not in cols:  # before prerequisites were recorded on the dispatch
        conn.execute("ALTER TABLE dispatches ADD COLUMN requires_verified TEXT")
    if "lane" not in cols:  # before lanes
        conn.execute("ALTER TABLE dispatches ADD COLUMN lane TEXT")
    if "lane" not in [r[1] for r in conn.execute("PRAGMA table_info(promises)")]:  # before promises had lanes
        conn.execute("ALTER TABLE promises ADD COLUMN lane TEXT")
    if "reason" not in [r[1] for r in conn.execute("PRAGMA table_info(ruling_records)")]:  # before no-item rulings
        conn.execute("ALTER TABLE ruling_records ADD COLUMN reason TEXT")
    for table in INSERT_ONLY:
        for op in ("UPDATE", "DELETE"):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} "
                f"BEFORE {op} ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{table} is insert-only: a correction is a new row'); END"
            )
    conn.commit()
    return conn


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------- facts

def fact_get(conn, key):
    r = conn.execute("SELECT value FROM facts WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def fact_set(conn, key, value):
    conn.execute(
        "INSERT INTO facts(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value, now()),
    )
    conn.commit()


def fleet_paused(conn):
    """Fail closed: only an absent value or exactly 'false' allows dispatch."""
    v = fact_get(conn, "fleet.paused")
    return not (v is None or v == "false")


# ---------------------------------------------------------------- workers

def worker_upsert(conn, name, repo, mode="orchestrated", started_by=None, state="idle"):
    if mode not in WORKER_MODES:
        raise Refused("BAD_MODE", f"mode must be one of {WORKER_MODES}")
    if state not in WORKER_STATES:
        raise Refused("BAD_STATE", f"state must be one of {WORKER_STATES}")
    conn.execute(
        "INSERT INTO workers(name, repo, mode, started_by, last_state, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET repo = excluded.repo, "
        "mode = excluded.mode, started_by = COALESCE(excluded.started_by, workers.started_by), "
        "last_state = excluded.last_state, updated_at = excluded.updated_at",
        (name, repo, mode, started_by, state, now()),
    )
    conn.commit()


def worker_state(conn, name, state):
    if state not in WORKER_STATES:
        raise Refused("BAD_STATE", f"state must be one of {WORKER_STATES}")
    cur = conn.execute(
        "UPDATE workers SET last_state = ?, updated_at = ? WHERE name = ?", (state, now(), name)
    )
    if cur.rowcount == 0:
        raise Refused("UNKNOWN_WORKER", f"no worker named {name}")
    conn.commit()


# ---------------------------------------------------------------- dispatches

# ---------------------------------------------------------------- lanes
# A lane is an independent line of work with its own orchestrator (agent:orchestrator-<lane>) and decisions page.
# A repository belongs to at most one active lane, so lanes never compete for a worker.

LANE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def _repo_key(repo):
    return os.path.realpath(os.path.expanduser(str(repo)))


def _lane_name_ok(name):
    if not LANE_NAME.match(name or "") or re.search(r"-g\d+$", name):
        raise Refused("BAD_NAME", f"lane names are lowercase letters, digits and dashes, and can't end in -g<n> "
                                  f"(that names a handover successor): {name!r}")


def lane_owner(conn, repo):
    r = conn.execute("SELECT lr.lane FROM lane_repos lr JOIN lanes l ON l.name = lr.lane "
                     "WHERE l.status = 'active' AND lr.repo = ?", (_repo_key(repo),)).fetchone()
    return r["lane"] if r else None


def _claim(conn, name, repos, items):
    for repo in repos:
        owner = lane_owner(conn, repo)
        if owner and owner != name:
            raise Refused("LANE", f"{_repo_key(repo)} belongs to the active lane '{owner}'; give this work to that "
                                  f"lane, or wait until it closes")
    for repo in repos:
        conn.execute("INSERT OR IGNORE INTO lane_repos(lane, repo) VALUES (?, ?)", (name, _repo_key(repo)))
    for item in items:
        conn.execute("INSERT OR IGNORE INTO lane_items(lane, item) VALUES (?, ?)", (name, item))


def lane_open(conn, name, goal, repos=(), items=()):
    _lane_name_ok(name)
    if not (goal or "").strip():
        raise Refused("NO_GOAL", "a lane needs its goal (the operator's words that started it)")
    row = conn.execute("SELECT status FROM lanes WHERE name = ?", (name,)).fetchone()
    if row and row["status"] == "active":
        raise Refused("NAME_TAKEN", f"an active lane is already called '{name}'; pick a more specific name")
    for repo in repos:  # check before writing anything
        owner = lane_owner(conn, repo)
        if owner:
            raise Refused("LANE", f"{_repo_key(repo)} belongs to the active lane '{owner}'; give this work to that "
                                  f"lane, or wait until it closes")
    if row:  # a closed lane's name is reused: the old dispatches keep their history
        conn.execute("UPDATE lanes SET goal = ?, status = 'active', created_at = ?, closed_at = NULL WHERE name = ?",
                     (goal, now(), name))
        conn.execute("DELETE FROM lane_items WHERE lane = ?", (name,))
    else:
        conn.execute("INSERT INTO lanes(name, goal, created_at) VALUES (?, ?, ?)", (name, goal, now()))
    _claim(conn, name, repos, items)
    conn.commit()
    return name


def _active_lane(conn, name):
    row = conn.execute("SELECT status FROM lanes WHERE name = ?", (name,)).fetchone()
    if not row or row["status"] != "active":
        raise Refused("NO_LANE", f"no active lane called '{name}'")


def lane_add(conn, name, repos=(), items=()):
    _active_lane(conn, name)
    _claim(conn, name, repos, items)
    conn.commit()


def lane_close(conn, name):
    _active_lane(conn, name)
    open_ = [r["bead"] for r in conn.execute(
        "SELECT bead FROM dispatches WHERE lane = ? AND status NOT IN ('closed', 'cancelled')", (name,))]
    if open_:
        raise Refused("OPEN_WORK", f"lane '{name}' still has open dispatches: {', '.join(open_)}; close or cancel "
                                   f"them first")
    owed = [str(r["id"]) for r in conn.execute(
        "SELECT id FROM promises WHERE lane = ? AND status = 'open'", (name,))]
    if owed:
        raise Refused("OPEN_WORK", f"lane '{name}' still has open promises: {', '.join(owed)}; mark them done or "
                                   f"drop them first")
    conn.execute("UPDATE lanes SET status = 'closed', closed_at = ? WHERE name = ?", (now(), name))
    conn.execute("DELETE FROM lane_repos WHERE lane = ?", (name,))  # release its repositories
    conn.commit()


def lane_rename(conn, old, new):
    """Renames a lane between programs; a running orchestrator's session name can't change, so not mid-work."""
    _active_lane(conn, old)
    _lane_name_ok(new)
    if conn.execute("SELECT 1 FROM dispatches WHERE lane = ? AND status NOT IN ('closed', 'cancelled')",
                    (old,)).fetchone():
        raise Refused("OPEN_WORK", f"lane '{old}' has open dispatches; rename it once they're closed (its running "
                                   f"orchestrator keeps the name agent:orchestrator-{old})")
    if conn.execute("SELECT 1 FROM lanes WHERE name = ?", (new,)).fetchone():
        raise Refused("NAME_TAKEN", f"a lane called '{new}' already exists")
    conn.execute("UPDATE lanes SET name = ? WHERE name = ?", (new, old))  # repos and items follow (ON UPDATE CASCADE)
    conn.execute("UPDATE dispatches SET lane = ? WHERE lane = ?", (new, old))
    conn.execute("UPDATE promises SET lane = ? WHERE lane = ?", (new, old))
    conn.commit()


def lanes(conn, include_closed=False):
    out = []
    q = "SELECT * FROM lanes" + ("" if include_closed else " WHERE status = 'active'") + " ORDER BY created_at"
    for l in rows(conn.execute(q)):
        n = l["name"]
        l["repos"] = [r[0] for r in conn.execute("SELECT repo FROM lane_repos WHERE lane = ? ORDER BY repo", (n,))]
        l["items"] = [r[0] for r in conn.execute("SELECT item FROM lane_items WHERE lane = ? ORDER BY item", (n,))]
        l["in_flight"] = conn.execute("SELECT count(*) FROM dispatches WHERE lane = ? AND status NOT IN "
                                      "('closed', 'cancelled')", (n,)).fetchone()[0]
        l["orchestrator"] = f"agent:orchestrator-{n}"
        out.append(l)
    return out


def lane_find(conn, query):
    """Active lanes a phrase could mean: by name, a repository's folder name, a bead or source, or the goal's words."""
    q = query.strip().lower()
    words = [w for w in re.findall(r"[a-z0-9#/._-]+", q) if len(w) > 2 and w not in ("the", "work", "lane", "with")]
    hits = []
    for l in lanes(conn):
        hay = [l["name"]] + [os.path.basename(r).lower() for r in l["repos"]] + [i.lower() for i in l["items"]]
        if q in hay or any(w == h or w in h for w in words for h in hay) or \
                any(w in l["goal"].lower() for w in words if len(w) > 3):
            hits.append(l)
    return hits


def dispatch_add(conn, bead, repo, worker, pipeline=None, program=None,
                 order_note=None, preconditions=None, report_to="agent:orchestrator",
                 requires_verified=(), checks=(), lane=None):
    """Record a dispatch BEFORE the message is sent. Refusals stop the send."""
    from . import checks as chk
    for c in checks:
        try:
            chk.parse(c)
        except chk.BadCheck as e:
            raise Refused("BAD_CHECK", f"check {c!r}: {e}")
    if fleet_paused(conn):
        raise Refused("PAUSED", "fleet.paused is set (fail closed): no dispatch until it reads 'false'")
    w = conn.execute("SELECT * FROM workers WHERE name = ?", (worker,)).fetchone()
    if w is None:
        raise Refused("UNKNOWN_WORKER", f"no worker named {worker}; start it through abacus-session first")
    if w["mode"] == "attended":
        raise Refused("ATTENDED", f"the operator is driving {worker}; watch it, do not orchestrate it")
    if w["paused"]:
        raise Refused("PAUSED", f"worker {worker} is paused")
    if w["repo"] and repo and str(w["repo"]).rstrip("/") != str(repo).rstrip("/"):
        raise Refused("WRONG_REPO", f"{worker} owns {w['repo']}, not {repo}")
    open_ = conn.execute(
        "SELECT id, status FROM dispatches WHERE bead = ? AND status NOT IN ('closed', 'cancelled')",
        (bead,),
    ).fetchone()
    if open_:
        raise Refused("DUPLICATE", f"bead {bead} already has open dispatch #{open_['id']} ({open_['status']})")
    prereqs = []  # recorded on the row, so a finished run shows which gates held and at which commits
    for dep in requires_verified:
        d = conn.execute(
            "SELECT status, verified_commit FROM dispatches WHERE bead = ? "
            "ORDER BY id DESC LIMIT 1", (dep,),
        ).fetchone()
        if d is None or d["status"] not in ("verified", "closed") or not d["verified_commit"]:
            raise Refused("PREREQUISITE", f"prerequisite {dep} is not verified; do not release dependants")
        prereqs.append({"bead": dep, "verified_commit": d["verified_commit"]})
    owner = lane_owner(conn, repo)
    if owner and owner != lane:
        raise Refused("LANE", f"{_repo_key(repo)} belongs to the active lane '{owner}'"
                              + (f", not '{lane}'" if lane else "") + "; that lane's orchestrator dispatches there")
    if lane:
        _active_lane(conn, lane)
    missing = [r["id"] for r in conn.execute(COVERING, {"bead": bead})
               if not conn.execute("SELECT 1 FROM ruling_relays WHERE ruling_id = ? AND worker = ?",
                                   (r["id"], worker)).fetchone()]
    if missing:
        raise Refused("UNRELAYED", f"ruling(s) {missing} cover {bead} but were never sent to {worker}; relay them "
                                   f"first (RULING message; the SendMessage hook records the relay)")
    ts = now()
    cur = conn.execute(
        "INSERT INTO dispatches(bead, repo, worker, pipeline, program, status, order_note, "
        "preconditions, report_to, checks, requires_verified, lane, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)",
        (bead, repo, worker, pipeline, program, order_note, preconditions, report_to,
         json.dumps(list(checks)) if checks else None, json.dumps(prereqs) if prereqs else None, lane, ts, ts),
    )
    if lane:  # the lane now owns this repository (if it didn't already) and tracks the bead
        _claim(conn, lane, [repo], [bead])
    conn.commit()
    return cur.lastrowid


def _open_dispatch(conn, bead):
    d = conn.execute(
        "SELECT * FROM dispatches WHERE bead = ? AND status NOT IN ('closed', 'cancelled')", (bead,)
    ).fetchone()
    if d is None:
        raise Refused("NO_DISPATCH", f"no open dispatch for {bead}")
    return d


def dispatch_status(conn, bead, status):
    if status not in DISPATCH_STATUSES:
        raise Refused("BAD_STATUS", f"status must be one of {DISPATCH_STATUSES}")
    if status == "verified":
        raise Refused("USE_VERIFY", "use `dispatch verify` so the commit and note are recorded")
    if status == "sent" and fleet_paused(conn):
        raise Refused("PAUSED", "fleet.paused is set (fail closed)")
    d = _open_dispatch(conn, bead)
    conn.execute("UPDATE dispatches SET status = ?, updated_at = ? WHERE id = ?", (status, now(), d["id"]))
    conn.commit()


def dispatch_verify(conn, bead, commit, note):
    if not commit or not note or not note.strip():
        raise Refused("NO_EVIDENCE", "verification needs the commit and a note saying what was checked")
    d = _open_dispatch(conn, bead)
    if d["status"] in ("queued", "paused"):
        raise Refused("NOT_SENT", f"dispatch for {bead} is {d['status']}; "
                      "it was never sent, so there is nothing to verify")
    ts = now()
    conn.execute(
        "UPDATE dispatches SET status = 'verified', verified_commit = ?, verified_at = ?, updated_at = ? "
        "WHERE id = ?", (commit, ts, ts, d["id"]),
    )
    rep = conn.execute(
        "SELECT id FROM reports WHERE bead = ? ORDER BY id DESC LIMIT 1", (bead,)
    ).fetchone()
    if rep:
        conn.execute(
            "INSERT INTO report_verifications(report_id, at, verified, note) VALUES (?, ?, 1, ?)",
            (rep["id"], ts, note),
        )
    conn.commit()


# ---------------------------------------------------------------- events

def msg(conn, direction, peer, body, delivery=None):
    first = body.strip().splitlines()[0] if body.strip() else ""
    cur = conn.execute(
        "INSERT INTO messages(at, direction, peer, first_line, body, delivery) VALUES (?, ?, ?, ?, ?, ?)",
        (now(), direction, peer, first[:200], body, delivery),
    )
    conn.commit()
    return cur.lastrowid


def report_add(conn, worker, bead, status, commit=None, pushed=None, evidence=None,
               ruling_needed=None, next_=None):
    if status not in REPORT_STATUSES:
        raise Refused("BAD_STATUS", f"report status must be one of {REPORT_STATUSES}")
    cur = conn.execute(
        "INSERT INTO reports(at, worker, bead, status, commit_sha, pushed, evidence, ruling_needed, next) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now(), worker, bead, status, commit, pushed, evidence, ruling_needed, next_),
    )
    d = conn.execute(
        "SELECT id FROM dispatches WHERE bead = ? AND status NOT IN ('closed', 'cancelled', 'verified')", (bead,)
    ).fetchone()
    if d:  # a later report never un-verifies a dispatch
        new = {"CLOSED": "reported", "BLOCKED": "blocked", "NEEDS-RULING": "needs_ruling",
               "PROGRESS": "in_progress"}.get(status)
        if new:
            conn.execute("UPDATE dispatches SET status = ?, updated_at = ? WHERE id = ?", (new, now(), d["id"]))
    conn.commit()
    return cur.lastrowid


def report_verified(conn, report_id, verified, note):
    if not note or not note.strip():
        raise Refused("NO_EVIDENCE", "a report cannot be marked verified without a verify note")
    conn.execute(
        "INSERT INTO report_verifications(report_id, at, verified, note) VALUES (?, ?, ?, ?)",
        (report_id, now(), 1 if verified else 0, note),
    )
    conn.commit()


def ruling_add(conn, words, where, question=None, target=None):
    """Store the operator's words verbatim BEFORE anything else happens to them.

    `target` is one bead or a list: one utterance stays one ruling however many beads it covers.
    """
    targets = [t for t in ([target] if isinstance(target, str) else (target or [])) if t]
    cur = conn.execute(
        "INSERT INTO rulings(at, words, said_where, question, target_item) VALUES (?, ?, ?, ?, ?)",
        (now(), words, where, question, targets[0] if targets else None),
    )
    for t in targets:
        conn.execute("INSERT INTO ruling_targets(ruling_id, bead) VALUES (?, ?)", (cur.lastrowid, t))
    conn.commit()
    return cur.lastrowid


def session_event(conn, name, action, by, reason=None):
    """Record who started or stopped a session, and why, so anyone reading state can tell what happened."""
    conn.execute("INSERT INTO session_events(at, name, action, by, reason) VALUES (?, ?, ?, ?, ?)",
                 (now(), name, action, by, reason))
    conn.commit()


def ruling_relayed(conn, ruling_id, worker):
    if conn.execute("SELECT 1 FROM rulings WHERE id = ?", (ruling_id,)).fetchone() is None:
        raise Refused("UNKNOWN", f"no ruling {ruling_id}")
    conn.execute("INSERT INTO ruling_relays(ruling_id, at, worker) VALUES (?, ?, ?)", (ruling_id, now(), worker))
    conn.commit()


def dispatch_checks(conn, bead):
    """Checks carried by the bead's open dispatch (most recent dispatch if none is open)."""
    r = conn.execute("SELECT checks FROM dispatches WHERE bead = ? ORDER BY "
                     "(status IN ('closed', 'cancelled')), id DESC LIMIT 1", (bead,)).fetchone()
    return json.loads(r["checks"]) if r and r["checks"] else []


NO_ITEM = "none"


def fleet_bead_notes(bead):
    """The notes of a bead on whichever fleet board holds it, or None if no board does."""
    for w in config.fleet().values():
        repo = os.path.expanduser(str(w.get("repo", "")))
        if not os.path.isdir(os.path.join(repo, ".beads")):
            continue
        p = subprocess.run(["bd", "show", bead, "--json"], cwd=repo, capture_output=True, text=True)
        if p.returncode != 0:
            continue
        try:
            data = json.loads(p.stdout or "null")
        except ValueError:
            continue
        data = data[0] if isinstance(data, list) and data else data
        if isinstance(data, dict) and data.get("id") == bead:
            return data.get("notes") or ""
    return None


def ruling_recorded(conn, ruling_id, on, reason=None, notes_of=None):
    """Record where the ruling was written down. `on` is a bead, or NO_ITEM for a ruling that covers no bead
    (e.g. "start the orchestrator"): there is nothing to write it on, so the reason closes it out instead."""
    r = conn.execute("SELECT target_item FROM rulings WHERE id = ?", (ruling_id,)).fetchone()
    if r is None:
        raise Refused("UNKNOWN", f"no ruling {ruling_id}")
    if on == NO_ITEM:
        beads = sorted({b for (b,) in conn.execute("SELECT bead FROM ruling_targets WHERE ruling_id = ?",
                                                   (ruling_id,))} | ({r["target_item"]} if r["target_item"] else set()))
        if beads:
            raise Refused("HAS_TARGETS", f"ruling {ruling_id} covers {', '.join(beads)}; record it on those beads")
        if not (reason or "").strip():
            raise Refused("REASON", "a ruling recorded on no bead needs --reason "
                          "(why there is nothing to record it on)")
    elif notes_of is not None:
        # 3zg: "recorded on <bead>" must be true - the bead's notes quote the operator's words in a row
        notes = notes_of(on)
        if notes is None:
            raise Refused("UNKNOWN_BEAD", f"{on} isn't on any fleet board; record the ruling where the bead lives")
        words = conn.execute("SELECT words FROM rulings WHERE id = ?", (ruling_id,)).fetchone()["words"]
        if " ".join(words.split()) not in " ".join(notes.split()):
            raise Refused("NOT_ON_BEAD", f"{on} has no row quoting ruling {ruling_id}'s words. Write the "
                                         f"JUDGEMENT/STANDING row on the bead first (/relay-ruling), then record it")
    conn.execute("INSERT INTO ruling_records(ruling_id, at, recorded_on, reason) VALUES (?, ?, ?, ?)",
                 (ruling_id, now(), on, reason))
    conn.commit()


def promise_add(conn, text, trigger=None, due_at=None, work_item=None, lane=None):
    """A promise belongs to a lane, so another lane's orchestrator doesn't read it as stale. Without --lane, a work item
    that an active lane owns puts the promise in that lane."""
    if lane is not None:
        _active_lane(conn, lane)
    elif work_item:
        row = conn.execute("SELECT i.lane FROM lane_items i JOIN lanes l ON l.name = i.lane "
                           "WHERE i.item = ? AND l.status = 'active'", (work_item,)).fetchone()
        lane = row["lane"] if row else None
    cur = conn.execute(
        "INSERT INTO promises(text, trigger, due_at, work_item, created_at, lane) VALUES (?, ?, ?, ?, ?, ?)",
        (text, trigger, due_at, work_item, now(), lane),
    )
    conn.commit()
    return cur.lastrowid


def promise_close(conn, pid, status, resolution):
    if status not in ("done", "dropped"):
        raise Refused("BAD_STATUS", "promise closes as done or dropped")
    if not resolution:
        raise Refused("NO_RESOLUTION", "say how the promise was resolved")
    cur = conn.execute(
        "UPDATE promises SET status = ?, resolution = ? WHERE id = ? AND status = 'open'",
        (status, resolution, pid),
    )
    if cur.rowcount == 0:
        raise Refused("NOT_OPEN", f"promise {pid} is not open")
    conn.commit()


def checkin_start(conn):
    cur = conn.execute("INSERT INTO checkins(started_at) VALUES (?)", (now(),))
    conn.execute(
        "INSERT INTO checkin_events(checkin_id, at, kind) VALUES (?, ?, 'start')", (cur.lastrowid, now())
    )
    conn.commit()
    return cur.lastrowid


def checkin_finish(conn, cid, summary):
    conn.execute(
        "INSERT INTO checkin_events(checkin_id, at, kind, summary) VALUES (?, ?, 'finish', ?)",
        (cid, now(), summary),
    )
    conn.commit()


def observe(conn, subject, text, gap=False, checkin_id=None):
    conn.execute(
        "INSERT INTO observations(at, checkin_id, subject, text, gap) VALUES (?, ?, ?, ?, ?)",
        (now(), checkin_id, subject, text, 1 if gap else 0),
    )
    conn.commit()


# ---------------------------------------------------------------- handover

def handover(conn, action, from_session=None, to_session=None, working_set=None, hid=None):
    """Handover states: launched -> recovered -> finished, or aborted / adopted."""
    if action == "launched":
        cur = conn.execute(
            "INSERT INTO handovers(from_session, to_session, state, working_set, updated_at) "
            "VALUES (?, ?, 'launched', ?, ?)", (from_session, to_session, working_set, now()),
        )
        conn.commit()
        return cur.lastrowid
    if action not in HANDOVER_STATES:
        raise Refused("BAD_STATE", f"handover state must be one of {HANDOVER_STATES}")
    allowed = {"recovered": ("launched",), "finished": ("recovered",),
               "aborted": ("launched",), "adopted": ("launched", "recovered")}[action]
    h = conn.execute("SELECT * FROM handovers WHERE id = ?", (hid,)).fetchone()
    if h is None:
        raise Refused("UNKNOWN", f"no handover {hid}")
    if h["state"] not in allowed:
        raise Refused("BAD_TRANSITION", f"cannot go {h['state']} -> {action}")
    conn.execute("UPDATE handovers SET state = ?, updated_at = ? WHERE id = ?", (action, now(), hid))
    conn.commit()
    return hid


# ---------------------------------------------------------------- views

VIEWS = {
    "in-flight": "SELECT id, bead, repo, worker, pipeline, status, order_note, preconditions, checks, "
                 "requires_verified, lane, verified_commit, updated_at FROM dispatches "
                 "WHERE status NOT IN ('closed', 'cancelled') ORDER BY id",
    "dispatches": "SELECT id, bead, worker, status, lane, requires_verified, verified_commit, created_at, verified_at "
                  "FROM dispatches ORDER BY id",
    "overdue": "SELECT id, text, trigger, due_at, work_item, lane FROM promises WHERE status = 'open' "
               "AND due_at IS NOT NULL AND due_at < :now ORDER BY due_at",
    "asks": "SELECT id, text, trigger, due_at, work_item, lane FROM promises WHERE status = 'open' ORDER BY id",
    "liveness": "SELECT name, repo, mode, last_state, paused, updated_at FROM workers ORDER BY name",
    "rulings": "SELECT r.id, r.at, r.words, r.said_where, r.question, r.target_item, "
               "(SELECT group_concat(bead, ', ') FROM ruling_targets t WHERE t.ruling_id = r.id) AS targets, "
               "(SELECT group_concat(recorded_on, ', ') FROM ruling_records rr WHERE rr.ruling_id = r.id) "
               "AS recorded_on FROM rulings r ORDER BY r.id",
    "rulings-unrelayed": UNRELAYED,
    "rulings-unrecorded": "SELECT id, at, words, question, target_item FROM rulings r WHERE NOT EXISTS "
                          "(SELECT 1 FROM ruling_records rr WHERE rr.ruling_id = r.id) ORDER BY id",
    "reports": "SELECT r.id, r.at, r.worker, r.bead, r.status, r.commit_sha, r.evidence, "
               "(SELECT verified FROM report_verifications v WHERE v.report_id = r.id ORDER BY v.id DESC LIMIT 1) "
               "AS verified FROM reports r ORDER BY r.id DESC LIMIT 50",
    "messages": "SELECT id, at, direction, peer, first_line FROM messages ORDER BY id DESC LIMIT 50",
    "handovers": "SELECT * FROM handovers ORDER BY id DESC LIMIT 10",
    "session-events": "SELECT at, name, action, by, reason FROM session_events ORDER BY id DESC LIMIT 30",
}


def show(conn, view, lane=None):
    if view not in VIEWS:
        raise Refused("BAD_VIEW", f"view must be one of {sorted(VIEWS)}")
    out = rows(conn.execute(VIEWS[view], {"now": now()}))
    if lane is not None and out and "lane" in out[0]:
        out = [r for r in out if r["lane"] == lane]
    return out


def digest(conn):
    """A <1 KB SessionStart digest: an index into durable state, not a replacement."""
    c = lambda q, *a: conn.execute(q, a).fetchone()[0]
    live = c("SELECT count(*) FROM workers WHERE last_state != 'gone'")
    inflight = c("SELECT count(*) FROM dispatches WHERE status NOT IN ('closed', 'cancelled')")
    overdue = c("SELECT count(*) FROM promises WHERE status = 'open' AND due_at IS NOT NULL AND due_at < ?", now())
    promises = c("SELECT count(*) FROM promises WHERE status = 'open'")
    by_lane = conn.execute("SELECT coalesce(lane, 'no lane'), count(*) FROM promises WHERE status = 'open' "
                           "GROUP BY lane ORDER BY lane IS NULL, lane").fetchall()
    if any(r[0] != "no lane" for r in by_lane):
        promises = f"{promises} ({', '.join(f'{r[0]} {r[1]}' for r in by_lane)})"
    rulings = c("SELECT count(*) FROM rulings r WHERE NOT EXISTS "
                "(SELECT 1 FROM ruling_records rr WHERE rr.ruling_id = r.id)")
    unrelayed = len(conn.execute(UNRELAYED).fetchall())
    last = conn.execute("SELECT max(at) FROM checkin_events WHERE kind = 'finish'").fetchone()[0] or "never"
    counts = (f"logbook: workers live {live} | in flight {inflight} | overdue {overdue} | "
              f"open promises {promises} | rulings unrecorded {rulings} | unrelayed {unrelayed} | last check-in {last}")
    attended = [r[0] for r in conn.execute("SELECT name FROM workers WHERE mode = 'attended'")]
    paused = "PAUSED" if fleet_paused(conn) else "running"
    scope = fact_get(conn, "fleet.scope") or "unset"
    binding = (f"binds now: fleet {paused} | scope {scope} | operator driving: "
               f"{', '.join(attended) or 'none'} | standing rulings: `abacus-logbook show rulings`, bead notes")
    # Sessions reading this have no clock of their own: say when "now" is, so leases and due dates can be judged.
    active = lanes(conn)
    lane_line = "lanes: " + (", ".join(f"{l['name']} ({l['in_flight']} in flight)" for l in active) or "none")
    lines = [f"as of {clock()}", counts, binding, lane_line]
    flight = rows(conn.execute(
        "SELECT bead, worker, status FROM dispatches WHERE status NOT IN ('closed', 'cancelled') ORDER BY id"))
    detail = "in flight: " + "; ".join(f"{d['bead']}@{d['worker']}={d['status']}" for d in flight)
    out = "\n".join(lines + ([detail] if flight else []))
    if len(out.encode()) > DIGEST_LIMIT:
        # Drop the in-flight list first; keep the binding line whole.
        out = "\n".join(lines)
    if len(out.encode()) > DIGEST_LIMIT:
        out = binding[:DIGEST_LIMIT]
    return out


# ---------------------------------------------------------------- rebuild

def rebuild(conn, repos):
    """Reconstruct in-flight dispatches from the boards with read-only tracker reads.

    Event history cannot be rebuilt: that is the part of the logbook to back up.
    """
    restored = []
    for worker, repo in repos.items():
        try:
            out = subprocess.run(
                ["bd", "list", "--status", "in_progress", "--json"], cwd=repo,
                capture_output=True, text=True, timeout=60, check=True,
            ).stdout
            items = json.loads(out or "[]")
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            restored.append({"worker": worker, "error": f"REFUSED board unreadable: {e}"})
            continue
        for it in items:
            bead = it.get("id")
            exists = conn.execute(
                "SELECT 1 FROM dispatches WHERE bead = ? AND status NOT IN ('closed', 'cancelled')", (bead,)
            ).fetchone()
            if exists:
                continue
            ts = now()
            conn.execute(
                "INSERT INTO dispatches(bead, repo, worker, status, order_note, created_at, updated_at) "
                "VALUES (?, ?, ?, 'in_progress', 'rebuilt from board', ?, ?)", (bead, repo, worker, ts, ts),
            )
            restored.append({"worker": worker, "bead": bead})
    conn.commit()
    return restored


# ---------------------------------------------------------------- CLI

def _emit(obj, as_json):
    if as_json or not isinstance(obj, (str, int)):
        print(json.dumps(obj, indent=2, default=str))
    else:
        print(obj)


def build_parser():
    p = argparse.ArgumentParser(prog="abacus-logbook", description=__doc__.split("\n")[0])
    p.add_argument("--json", action="store_true", help="JSON output")
    sub = p.add_subparsers(dest="noun", required=True)

    sub.add_parser("init", help="create the database")
    sub.add_parser("digest", help="print the <1 KB session-start digest")
    s = sub.add_parser("show", help="read-only views")
    s.add_argument("view", choices=sorted(VIEWS))
    s.add_argument("--lane", help="only rows for this lane (views that have a lane column)")

    ln = sub.add_parser("lane", help="independent lines of work, each with its own orchestrator").add_subparsers(
        dest="verb", required=True)
    a = ln.add_parser("open"); a.add_argument("name"); a.add_argument("--goal", required=True)
    a.add_argument("--repo", action="append", default=[]); a.add_argument("--item", action="append", default=[])
    a = ln.add_parser("add"); a.add_argument("name")
    a.add_argument("--repo", action="append", default=[]); a.add_argument("--item", action="append", default=[])
    a = ln.add_parser("close"); a.add_argument("name")
    a = ln.add_parser("rename"); a.add_argument("old"); a.add_argument("new")
    a = ln.add_parser("show"); a.add_argument("name", nargs="?"); a.add_argument("--all", action="store_true")
    a = ln.add_parser("find"); a.add_argument("query")
    a = ln.add_parser("owner"); a.add_argument("repo")

    w = sub.add_parser("worker").add_subparsers(dest="verb", required=True)
    a = w.add_parser("upsert")
    a.add_argument("name"); a.add_argument("--repo", required=True)
    a.add_argument("--mode", default="orchestrated", choices=WORKER_MODES)
    a.add_argument("--started-by"); a.add_argument("--state", default="idle", choices=WORKER_STATES)
    a = w.add_parser("state"); a.add_argument("name"); a.add_argument("state", choices=WORKER_STATES)

    d = sub.add_parser("dispatch").add_subparsers(dest="verb", required=True)
    a = d.add_parser("add")
    a.add_argument("bead"); a.add_argument("--repo", required=True); a.add_argument("--worker", required=True)
    a.add_argument("--pipeline"); a.add_argument("--program"); a.add_argument("--order-note")
    a.add_argument("--preconditions"); a.add_argument("--report-to", default="agent:orchestrator")
    a.add_argument("--requires-verified", action="append", default=[],
                   help="bead id whose dispatch must be verified first (repeatable)")
    a.add_argument("--check", action="append", default=[], dest="checks",
                   help="read-only command abacus-verify must see exit 0 in the worker's repo (repeatable)")
    a.add_argument("--lane", help="the lane dispatching (it claims the repository; another lane's is refused)")
    a = d.add_parser("sent"); a.add_argument("bead")
    a = d.add_parser("status"); a.add_argument("bead"); a.add_argument("status", choices=DISPATCH_STATUSES)
    a = d.add_parser("verify"); a.add_argument("bead")
    a.add_argument("--commit", required=True); a.add_argument("--note", required=True)

    m = sub.add_parser("msg").add_subparsers(dest="verb", required=True)
    for v in ("out", "in"):
        a = m.add_parser(v); a.add_argument("--peer", required=True)
        a.add_argument("--body", help="message body (default: stdin)"); a.add_argument("--delivery")

    r = sub.add_parser("report").add_subparsers(dest="verb", required=True)
    a = r.add_parser("add")
    a.add_argument("bead"); a.add_argument("--worker", required=True)
    a.add_argument("--status", required=True, choices=REPORT_STATUSES)
    a.add_argument("--commit"); a.add_argument("--pushed", type=int, choices=(0, 1))
    a.add_argument("--evidence"); a.add_argument("--ruling-needed"); a.add_argument("--next")
    a = r.add_parser("verified"); a.add_argument("report_id", type=int)
    a.add_argument("--failed", action="store_true"); a.add_argument("--note", required=True)

    pr = sub.add_parser("promise").add_subparsers(dest="verb", required=True)
    a = pr.add_parser("add"); a.add_argument("text"); a.add_argument("--trigger")
    a.add_argument("--due-at"); a.add_argument("--work-item")
    a.add_argument("--lane", help="the lane it belongs to (default: the active lane that owns --work-item)")
    for v in ("done", "drop"):
        a = pr.add_parser(v); a.add_argument("id", type=int); a.add_argument("--resolution", required=True)

    ru = sub.add_parser("ruling").add_subparsers(dest="verb", required=True)
    a = ru.add_parser("add"); a.add_argument("--words", required=True); a.add_argument("--where", required=True)
    a.add_argument("--question"); a.add_argument("--target", action="append", default=[],
                                                 help="bead the ruling covers (repeatable: one utterance, one ruling)")
    a = ru.add_parser("recorded"); a.add_argument("id", type=int)
    a.add_argument("--on", required=True, help=f"the bead it was written on, or '{NO_ITEM}' when it covers no bead")
    a.add_argument("--reason", help=f"required with --on {NO_ITEM}: why there is nothing to record it on")
    a = ru.add_parser("relayed"); a.add_argument("id", type=int); a.add_argument("--to", required=True)

    c = sub.add_parser("checkin").add_subparsers(dest="verb", required=True)
    c.add_parser("start")
    a = c.add_parser("finish"); a.add_argument("id", type=int); a.add_argument("--summary", required=True)

    a = sub.add_parser("observe"); a.add_argument("subject"); a.add_argument("text")
    a.add_argument("--gap", action="store_true"); a.add_argument("--checkin", type=int)

    f = sub.add_parser("fact").add_subparsers(dest="verb", required=True)
    a = f.add_parser("get"); a.add_argument("key")
    a = f.add_parser("set"); a.add_argument("key"); a.add_argument("value")

    h = sub.add_parser("handover").add_subparsers(dest="verb", required=True)
    a = h.add_parser("launched"); a.add_argument("--from", dest="from_session", required=True)
    a.add_argument("--to", dest="to_session", required=True); a.add_argument("--working-set")
    for v in ("recovered", "finished", "aborted", "adopted"):
        a = h.add_parser(v); a.add_argument("id", type=int)

    sub.add_parser("rebuild", help="reconstruct in-flight dispatches from the fleet's boards")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        conn = connect()
    except sqlite3.Error as e:
        if args.noun == "digest":
            print(f"logbook unreachable ({e}); recover from the repository boards instead")
            return 0
        raise
    j = args.json
    try:
        n, v = args.noun, getattr(args, "verb", None)
        if n == "init":
            _emit(f"logbook ready at {config.logbook_path()}", j)
        elif n == "digest":
            print(digest(conn))
        elif n == "show":
            _emit(show(conn, args.view, args.lane), True)
        elif n == "lane" and v == "open":
            _emit({"lane": lane_open(conn, args.name, args.goal, args.repo, args.item),
                   "orchestrator": f"agent:orchestrator-{args.name}"}, True)
        elif n == "lane" and v == "add":
            lane_add(conn, args.name, args.repo, args.item); _emit("ok", j)
        elif n == "lane" and v == "close":
            lane_close(conn, args.name); _emit("ok", j)
        elif n == "lane" and v == "rename":
            lane_rename(conn, args.old, args.new); _emit("ok", j)
        elif n == "lane" and v == "show":
            ls = lanes(conn, args.all)
            _emit([l for l in ls if l["name"] == args.name] if args.name else ls, True)
        elif n == "lane" and v == "find":
            _emit(lane_find(conn, args.query), True)
        elif n == "lane" and v == "owner":
            _emit({"repo": _repo_key(args.repo), "lane": lane_owner(conn, args.repo)}, True)
        elif n == "worker" and v == "upsert":
            worker_upsert(conn, args.name, args.repo, args.mode, args.started_by, args.state)
            _emit(f"worker {args.name} recorded", j)
        elif n == "worker" and v == "state":
            worker_state(conn, args.name, args.state); _emit("ok", j)
        elif n == "dispatch" and v == "add":
            i = dispatch_add(conn, args.bead, args.repo, args.worker, args.pipeline, args.program,
                             args.order_note, args.preconditions, args.report_to, args.requires_verified,
                             args.checks, args.lane)
            _emit({"dispatch": i, "status": "queued", "next": "send the message, then `dispatch sent`"}, True)
        elif n == "dispatch" and v == "sent":
            dispatch_status(conn, args.bead, "sent"); _emit("ok", j)
        elif n == "dispatch" and v == "status":
            dispatch_status(conn, args.bead, args.status); _emit("ok", j)
        elif n == "dispatch" and v == "verify":
            dispatch_verify(conn, args.bead, args.commit, args.note); _emit("verified", j)
        elif n == "msg":
            body = args.body if args.body is not None else sys.stdin.read()
            _emit({"message": msg(conn, v, args.peer, body, args.delivery)}, True)
        elif n == "report" and v == "add":
            i = report_add(conn, args.worker, args.bead, args.status, args.commit, args.pushed,
                           args.evidence, args.ruling_needed, args.next)
            _emit({"report": i, "verified": False, "next": "verify before acting: abacus-verify"}, True)
        elif n == "report" and v == "verified":
            report_verified(conn, args.report_id, not args.failed, args.note); _emit("ok", j)
        elif n == "promise" and v == "add":
            _emit({"promise": promise_add(conn, args.text, args.trigger, args.due_at, args.work_item, args.lane)}, True)
        elif n == "promise":
            promise_close(conn, args.id, "done" if v == "done" else "dropped", args.resolution); _emit("ok", j)
        elif n == "ruling" and v == "add":
            _emit({"ruling": ruling_add(conn, args.words, args.where, args.question, args.target)}, True)
        elif n == "ruling" and v == "recorded":
            ruling_recorded(conn, args.id, args.on, args.reason, notes_of=fleet_bead_notes); _emit("ok", j)
        elif n == "ruling" and v == "relayed":
            ruling_relayed(conn, args.id, args.to); _emit("ok", j)
        elif n == "checkin" and v == "start":
            _emit({"checkin": checkin_start(conn)}, True)
        elif n == "checkin" and v == "finish":
            checkin_finish(conn, args.id, args.summary); _emit("ok", j)
        elif n == "observe":
            observe(conn, args.subject, args.text, args.gap, args.checkin); _emit("ok", j)
        elif n == "fact" and v == "get":
            val = fact_get(conn, args.key)
            _emit("" if val is None else val, j)
        elif n == "fact" and v == "set":
            fact_set(conn, args.key, args.value); _emit("ok", j)
        elif n == "handover":
            i = handover(conn, v, getattr(args, "from_session", None), getattr(args, "to_session", None),
                         getattr(args, "working_set", None), getattr(args, "id", None))
            _emit({"handover": i, "state": v}, True)
        elif n == "rebuild":
            repos = {name: w["repo"] for name, w in config.fleet().items()}
            _emit(rebuild(conn, repos), True)
    except Refused as e:
        print(f"REFUSED {e.code}: {e}", file=sys.stderr)
        return 3
    except sqlite3.IntegrityError as e:
        print(f"REFUSED INTEGRITY: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
