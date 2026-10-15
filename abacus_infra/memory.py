"""Shared long-term memory: harvest -> ingest -> consult.

Write path: `harvest` copies published records from one repository into a
staging tree (read-only against the source), `ingest` normalizes them into
faceted rows through ONE write door, idempotent on a content hash.

Read path: `consult` (and the `consult_memory` MCP tool) answers with hits or
one of three named non-hits, always with the floor, the best pool score and
per-doc-type corpus coverage, so "nothing came back" is a reportable answer.

The engine is SQLite FTS5 (BM25). The interface is engine-agnostic: swapping to
Postgres + pgvector changes this module, not its callers.
"""
import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import config, records

SCHEMA_VERSION = 1
DEFAULT_FLOOR = 0.0  # BM25 scales with corpus size: abstain only once calibrated
MIGRATIONS = {
    1: """
    CREATE TABLE docs (
      identifier TEXT PRIMARY KEY,      -- <repo>:<repo-relative path>, the supersession key
      emitted_id TEXT,                  -- the record's own id; may renumber
      doc_type TEXT NOT NULL, project TEXT NOT NULL, surface TEXT, trigger TEXT,
      severity TEXT, resolved TEXT, tags TEXT, era TEXT, inferred INTEGER NOT NULL DEFAULT 0,
      title TEXT, body TEXT NOT NULL, content_hash TEXT NOT NULL, ingested_at TEXT NOT NULL
    );
    CREATE VIRTUAL TABLE docs_fts USING fts5(identifier UNINDEXED, title, body, tags, tokenize='porter unicode61');
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
}


class Refused(Exception):
    pass


def staging_dir():
    d = config.state_dir() / "harvest"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- database

def connect(path=None, migrate=False):
    conn = sqlite3.connect(str(path or config.memory_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT max(version) FROM schema_version").fetchone()
    have = row[0] or 0
    if have < SCHEMA_VERSION:
        if not migrate:
            raise Refused(f"memory database is at schema {have}, needs {SCHEMA_VERSION}: "
                          f"run `abacus-memory migrate` (it never self-migrates)")
        for v in range(have + 1, SCHEMA_VERSION + 1):
            conn.executescript(MIGRATIONS[v])
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (v,))
        conn.commit()
    return conn


def get_floor(conn):
    r = conn.execute("SELECT value FROM settings WHERE key = 'floor'").fetchone()
    return float(r[0]) if r else DEFAULT_FLOOR


def floor_note(conn):
    r = conn.execute("SELECT value FROM settings WHERE key = 'floor_note'").fetchone()
    return r[0] if r else "default floor; not yet calibrated (run `abacus-memory calibrate`)"


# ---------------------------------------------------------------- harvest

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _published(repo, rel):
    """A record is published when it is committed and reachable from the upstream."""
    if _git(repo, "status", "--porcelain", "--", rel).stdout.strip():
        return False, "uncommitted"
    if not _git(repo, "log", "-1", "--format=%H", "--", rel).stdout.strip():
        return False, "never committed"
    up = _git(repo, "rev-parse", "--abbrev-ref", "@{u}")
    if up.returncode != 0:
        return False, "no upstream branch"
    if _git(repo, "log", "--format=%H", "@{u}..HEAD", "--", rel).stdout.strip():
        return False, "unpushed"
    return True, ""


def harvest(repo):
    """Stage one repository's published records. Read-only against the source."""
    repo = Path(repo).resolve()
    top = _git(repo, "rev-parse", "--show-toplevel").stdout.strip()
    if not top:
        raise Refused(f"{repo} is not a git repository")
    repo = Path(top)
    name = repo.name
    dest = staging_dir() / name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    report = {"repo": name, "classified": [], "refused": [], "unrecognized": [], "found_nothing": [],
              "unreadable": []}
    manifest = []
    for d in records.DISCOVERY_DIRS:
        src = repo / d
        if not src.exists():
            report["found_nothing"].append(d)
            continue
        files = sorted(src.glob("*.md"))
        if not files:
            report["found_nothing"].append(d)
        for f in files:
            rel = str(f.relative_to(repo))
            try:
                text = f.read_text()
            except OSError as e:
                report["unreadable"].append(f"{rel}: {e}")
                continue
            ok, why = _published(repo, rel)
            if not ok:
                report["refused"].append(f"{rel}: {why}")
                continue
            header, _ = records.parse(text)
            if not header:
                report["unrecognized"].append(f"{rel}: no profile header")
                continue
            out = dest / rel.replace("/", "__")
            out.write_text(text)
            manifest.append({"identifier": f"{name}:{rel}", "file": out.name,
                             "sha256": hashlib.sha256(text.encode()).hexdigest()})
            report["classified"].append(rel)
    for sub in sorted((repo / "docs").glob("*")) if (repo / "docs").exists() else []:
        if sub.is_dir() and f"docs/{sub.name}" not in records.DISCOVERY_DIRS:
            report["unrecognized"].append(f"docs/{sub.name}/ (not a discovery directory)")
    # Closed beads: their JUDGEMENT lines and close reasons are settled evidence.
    try:
        beads = (subprocess.run(["bd", "list", "--all", "--status", "closed", "-n", "0", "--json"],
                                cwd=repo, capture_output=True, text=True)
                 if (repo / ".beads").exists() else None)
    except OSError as e:
        beads = subprocess.CompletedProcess([], 127, "", f"bd not runnable: {e}")
    if beads is None:
        report["found_nothing"].append(".beads (no board)")
    elif beads.returncode != 0:
        report["unreadable"].append(f"beads board: {beads.stderr.strip()[:200] or 'bd failed'}")
    else:
        try:
            items = json.loads(beads.stdout or "[]")
        except ValueError:
            items = []
            report["unreadable"].append("beads board: unparseable JSON")
        for it in items:
            rel = f".beads/{it.get('id')}"
            text = json.dumps(it, sort_keys=True)
            out = dest / f"bead__{it.get('id')}.json"
            out.write_text(text)
            manifest.append({"identifier": f"{name}:{rel}", "file": out.name,
                             "sha256": hashlib.sha256(text.encode()).hexdigest()})
            report["classified"].append(rel)
    (dest / "manifest.json").write_text(json.dumps({"repo": name, "path": str(repo), "records": manifest},
                                                   indent=2))
    return report


# ---------------------------------------------------------------- ingest

def _normalize(repo, entry, path):
    text = path.read_text()
    if path.suffix == ".json":
        it = json.loads(text)
        judgements = [ln for ln in (it.get("notes") or "").splitlines()
                      if ln.startswith(("JUDGEMENT", "STANDING", "OUTCOME"))]
        body = "\n".join(filter(None, [
            it.get("description"), "Acceptance:\n" + (it.get("acceptance_criteria") or ""),
            "Close reason: " + (it.get("close_reason") or ""), "\n".join(judgements)]))
        return {"doc_type": "bead", "project": repo, "surface": None, "trigger": None,
                "severity": None, "resolved": "true", "tags": ",".join(it.get("labels") or []),
                "emitted_id": it.get("id"), "title": it.get("title"), "body": body,
                "era": (it.get("closed_at") or "")[:7], "inferred": 1}
    header, body = records.parse(text)
    problems = records.validate(header)
    if problems:
        raise Refused(f"{entry['identifier']}: " + "; ".join(problems))
    title = next((ln.lstrip("# ").strip() for ln in body.splitlines() if ln.startswith("#")), header["id"])
    return {"doc_type": header["doc_type"], "project": header["project"], "surface": header["surface"],
            "trigger": header["trigger"], "severity": header["severity"], "resolved": header["resolved"],
            "tags": ",".join(header["tags"]), "emitted_id": header["id"], "title": title, "body": body,
            "era": str(header["date"])[:7], "inferred": 0}


def ingest(conn, allow_refusals=None):
    """Ingest every staged repo. Idempotent: the hash covers facets and text."""
    result = {"written": 0, "unchanged": 0, "refused": []}
    pending = []
    for mf in sorted(staging_dir().glob("*/manifest.json")):
        m = json.loads(mf.read_text())
        for entry in m["records"]:
            try:
                row = _normalize(m["repo"], entry, mf.parent / entry["file"])
            except (Refused, ValueError, KeyError) as e:
                result["refused"].append(str(e))
                continue
            pending.append((entry["identifier"], row))
    if result["refused"] and allow_refusals != len(result["refused"]):
        raise Refused(f"{len(result['refused'])} record(s) refused; nothing ingested. Fix them, or accept a "
                      f"partial corpus with --allow-refusals {len(result['refused'])}:\n  "
                      + "\n  ".join(result["refused"]))
    from .logbook import now
    for ident, row in pending:
        cols = ("doc_type", "project", "surface", "trigger", "severity", "resolved", "tags", "title", "body")
        h = hashlib.sha256(json.dumps([row[c] for c in cols]).encode()).hexdigest()
        cur = conn.execute("SELECT content_hash FROM docs WHERE identifier = ?", (ident,)).fetchone()
        if cur and cur[0] == h:
            result["unchanged"] += 1
            continue
        conn.execute("DELETE FROM docs WHERE identifier = ?", (ident,))
        conn.execute("DELETE FROM docs_fts WHERE identifier = ?", (ident,))
        conn.execute(
            "INSERT INTO docs(identifier, emitted_id, doc_type, project, surface, trigger, severity, resolved, "
            "tags, era, inferred, title, body, content_hash, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ident, row["emitted_id"], row["doc_type"], row["project"], row["surface"], row["trigger"],
             row["severity"], row["resolved"], row["tags"], row["era"], row["inferred"], row["title"],
             row["body"], h, now()))
        conn.execute("INSERT INTO docs_fts(identifier, title, body, tags) VALUES (?, ?, ?, ?)",
                     (ident, row["title"] or "", row["body"], row["tags"] or ""))
        result["written"] += 1
    conn.commit()
    return result


# ---------------------------------------------------------------- consult

def coverage(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT doc_type, count(*) FROM docs GROUP BY doc_type")}


def _fts_query(q):
    terms = [t for t in re.findall(r"[A-Za-z0-9_]+", q.lower()) if len(t) > 2]
    return " OR ".join(f'"{t}"' for t in terms)


def consult(conn, query, limit=5, project=None, doc_type=None, surface=None, facets=None):
    facets = dict(facets or {})
    for k, v in (("project", project), ("doc_type", doc_type), ("surface", surface)):
        if v:
            facets[k] = v
    bad = [k for k in facets if k not in records.FACETS]
    if bad:
        raise Refused(f"unknown facet(s) {bad}; valid facets are {records.FACETS}")
    where = " AND ".join(f"d.{k} = :{k}" for k in facets) or "1=1"
    floor = get_floor(conn)
    base = {"floor": floor, "floor_note": floor_note(conn), "query": query, "filters": facets}
    pool = conn.execute(f"SELECT count(*) FROM docs d WHERE {where}", facets).fetchone()[0]
    if pool == 0:
        return dict(base, outcome="no_rows_matched_filters", pool_max=None, corpus_coverage=coverage(conn),
                    advice="relax the filters (drop the project filter to see other repositories)")
    fq = _fts_query(query)
    found = []
    if fq:
        found = conn.execute(
            f"SELECT d.identifier, d.emitted_id, d.doc_type, d.project, d.surface, d.trigger, d.title, "
            f"-bm25(docs_fts) AS score, snippet(docs_fts, 2, '[', ']', ' ... ', 24) AS snippet "
            f"FROM docs_fts JOIN docs d ON d.identifier = docs_fts.identifier "
            f"WHERE docs_fts MATCH :q AND {where} ORDER BY score DESC LIMIT :limit",
            dict(facets, q=fq, limit=limit)).fetchall()
    if not found:
        return dict(base, outcome="index_recall", pool_max=None, corpus_coverage=coverage(conn),
                    advice=f"{pool} row(s) match the filters but the index recalled none for this query; "
                           f"this is not the same as 'no lesson exists' -- rephrase with the problem's shape")
    pool_max = found[0]["score"]
    if pool_max < floor:
        return dict(base, outcome="abstained_below_floor", pool_max=round(pool_max, 3),
                    corpus_coverage=coverage(conn), advice="best match is below the floor; rephrase")
    hits = [dict(r, score=round(r["score"], 3)) for r in found]
    return dict(base, outcome="hits", pool_max=round(pool_max, 3),
                answer_shape=f"{len(hits)} distinct document(s)", hits=hits,
                note="open each hit's identifier (<repo>:<path>) and read the source before relying on it")


def calibrate(conn, on_target, off_topic):
    """Place the floor in the gap between the lowest on-target and highest off-topic score."""
    def best(q):
        fq = _fts_query(q)
        if not fq:
            return 0.0
        r = conn.execute("SELECT max(-bm25(docs_fts)) FROM docs_fts WHERE docs_fts MATCH ?", (fq,)).fetchone()
        return r[0] or 0.0
    on = {q: best(q) for q in on_target}
    off = {q: best(q) for q in off_topic}
    lo, hi = min(on.values()), max(off.values()) if off else 0.0
    if lo <= hi:
        raise Refused(f"no gap: lowest on-target {lo:.3f} <= highest off-topic {hi:.3f}; the slate cannot "
                      f"separate them. Improve the probes; never move the floor to chase one query.")
    floor = round((lo + hi) / 2, 3)
    from .logbook import now
    note = f"calibrated {now()} on {len(on)} on-target / {len(off)} off-topic probes: gap {hi:.3f}..{lo:.3f}"
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('floor', ?)", (str(floor),))
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('floor_note', ?)", (note,))
    conn.commit()
    return {"floor": floor, "on_target": on, "off_topic": off, "note": note}


# ---------------------------------------------------------------- MCP server

CONSULT_TOOL = {
    "name": "consult_memory",
    "description": ("Search Abacus shared memory (retros, decisions, postmortems, closed beads from every "
                    "repository). Query the SHAPE of the problem, not a bead id; omit project to learn from "
                    "other repositories. Always report what came back, including a non-hit outcome."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 5},
            "project": {"type": "string"},
            "doc_type": {"type": "string", "enum": list(records.DOC_TYPES)},
            "surface": {"type": "string", "enum": list(records.SURFACES)},
            "facets": {"type": "object", "additionalProperties": {"type": "string"}},
        },
        "required": ["query"],
    },
}


def serve_mcp():
    """Minimal MCP stdio server (newline-delimited JSON-RPC 2.0), read-only."""
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        mid, method = req.get("id"), req.get("method")
        if mid is None:
            continue  # notification
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": req.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "abacus-memory", "version": "0.1.0"}}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [CONSULT_TOOL]}})
        elif method == "tools/call":
            params = req.get("params", {})
            if params.get("name") != "consult_memory":
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}})
                continue
            a = params.get("arguments", {})
            try:
                conn = connect()
                out = consult(conn, a.get("query", ""), int(a.get("limit", 5)), a.get("project"),
                              a.get("doc_type"), a.get("surface"), a.get("facets"))
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": json.dumps(out, indent=2)}]}})
            except (Refused, sqlite3.Error) as e:
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": f"REFUSED: {e}"}], "isError": True}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}})


# ---------------------------------------------------------------- CLI

def main(argv=None):
    p = argparse.ArgumentParser(prog="abacus-memory", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="create or upgrade the memory database")
    h = sub.add_parser("harvest", help="stage one repository's published records")
    h.add_argument("--repo", default=".")
    i = sub.add_parser("ingest", help="ingest everything staged")
    i.add_argument("--allow-refusals", type=int, help="accept a partial corpus: give the exact refusal count")
    me = sub.add_parser("harvest-me", help="harvest the current repository, then ingest")
    me.add_argument("--allow-refusals", type=int)
    c = sub.add_parser("consult", help="query shared memory")
    c.add_argument("query"); c.add_argument("--limit", type=int, default=5)
    c.add_argument("--project"); c.add_argument("--doc-type"); c.add_argument("--surface")
    c.add_argument("--facet", action="append", default=[], metavar="KEY=VALUE")
    cal = sub.add_parser("calibrate", help="set the abstention floor from a probe slate")
    cal.add_argument("--on", action="append", required=True, help="on-target probe (repeatable)")
    cal.add_argument("--off", action="append", required=True, help="off-topic probe (repeatable)")
    sub.add_parser("status", help="corpus coverage and floor")
    sub.add_parser("serve-mcp", help="run the read-only consult_memory MCP server on stdio")
    a = p.parse_args(argv)
    try:
        if a.cmd == "serve-mcp":
            serve_mcp(); return 0
        if a.cmd == "migrate":
            connect(migrate=True); print(f"memory schema {SCHEMA_VERSION} at {config.memory_path()}"); return 0
        if a.cmd == "harvest":
            print(json.dumps(harvest(a.repo), indent=2)); return 0
        conn = connect()
        if a.cmd == "ingest":
            print(json.dumps(ingest(conn, a.allow_refusals), indent=2))
        elif a.cmd == "harvest-me":
            rep = harvest(".")
            print(json.dumps({"harvest": rep, "ingest": ingest(conn, a.allow_refusals)}, indent=2))
        elif a.cmd == "consult":
            facets = dict(f.split("=", 1) for f in a.facet)
            print(json.dumps(consult(conn, a.query, a.limit, a.project, a.doc_type, a.surface, facets),
                             indent=2))
        elif a.cmd == "calibrate":
            print(json.dumps(calibrate(conn, a.on, a.off), indent=2))
        elif a.cmd == "status":
            print(json.dumps({"coverage": coverage(conn), "floor": get_floor(conn),
                              "floor_note": floor_note(conn)}, indent=2))
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
