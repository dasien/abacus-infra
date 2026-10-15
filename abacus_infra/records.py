"""Record profiles shared by harvest, ingest and the validator.

A record is a Markdown file with a flat frontmatter header. Every enum is a
closed vocabulary: extending one needs a decision record in abacus-standards.
"""
import re

DOC_TYPES = ("retro", "postmortem", "adr", "decision", "bead", "outcome", "mined-retro")
TRIGGERS = ("intractable", "fix-loop-cap", "gate-red-on-main", "escalation", "manual",
            "incident", "release", "audit", "mined", "unknown")
SURFACES = ("workflow", "build", "tests", "deps", "infra", "design", "process", "data",
            "retrieval", "security", "cost", "docs", "unknown")
SEVERITIES = ("sev1", "sev2", "sev3", "sev4", "unknown")
RESOLVED = ("true", "false", "partial", "unknown")
PROFILE_KEYS = ("id", "project", "issue", "date", "doc_type", "trigger", "surface",
                "severity", "resolved", "tags")
FACETS = ("project", "doc_type", "surface", "trigger", "severity", "resolved")
HARVEST_ONLY = ("era", "source_path", "source_format", "inferred")

DISCOVERY_DIRS = ("docs/retros", "docs/adr", "docs/decisions", "docs/postmortems")

_FM = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


def parse(text):
    """Return (header dict, body). Supports the flat subset our records use."""
    m = _FM.match(text)
    if not m:
        return {}, text
    header = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            header.setdefault("__malformed__", []).append(line)
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith("[") and v.endswith("]"):
            v = [t.strip().strip("'\"") for t in v[1:-1].split(",") if t.strip()]
        elif len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        header[k.strip()] = v
    return header, m.group(2)


def validate(header):
    """Return a list of problems (empty means valid). Unknown values are named."""
    problems = []
    if "__malformed__" in header:
        problems.append(f"malformed header lines: {header['__malformed__']}")
    missing = [k for k in PROFILE_KEYS if k not in header]
    if missing:
        problems.append(f"missing keys: {', '.join(missing)}")
    extra = [k for k in header if k not in PROFILE_KEYS and k != "__malformed__"]
    for k in extra:
        if k in HARVEST_ONLY:
            problems.append(f"{k} is a harvest fact; never write it in a record")
        else:
            problems.append(f"unknown key: {k}")
    for key, vocab in (("doc_type", DOC_TYPES), ("trigger", TRIGGERS), ("surface", SURFACES),
                       ("severity", SEVERITIES), ("resolved", RESOLVED)):
        v = header.get(key)
        if v is not None and v not in vocab:
            problems.append(f"{key}={v!r} is not in the closed vocabulary {vocab}")
    if "project" in header and not re.fullmatch(r"[a-z0-9-]+", str(header["project"])):
        problems.append("project must be lowercase [a-z0-9-]")
    if "date" in header and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(header["date"])):
        problems.append("date must be YYYY-MM-DD")
    if "tags" in header and not isinstance(header["tags"], list):
        problems.append("tags must be a [list]")
    return problems
