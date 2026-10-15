"""Locate Abacus home, state directory and fleet configuration.

Everything Abacus keeps outside a repository lives under ABACUS_HOME
(default ~/.abacus). Repositories never hold control-plane state.
"""
import json
import os
from pathlib import Path


def home():
    return Path(os.environ.get("ABACUS_HOME", Path.home() / ".abacus")).expanduser()


def state_dir():
    d = home() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load():
    """Return ~/.abacus/config.json as a dict ({} when absent)."""
    p = home() / "config.json"
    if not p.exists():
        return {}
    with p.open() as f:
        return json.load(f)


def logbook_path():
    return Path(os.environ.get("ABACUS_LOGBOOK_DB", state_dir() / "logbook.db"))


def memory_path():
    return Path(os.environ.get("ABACUS_MEMORY_DB", state_dir() / "memory.db"))


def workspace_root():
    cfg = load()
    root = os.environ.get("ABACUS_WORKSPACE") or cfg.get("workspace_root")
    return Path(root).expanduser().resolve() if root else None


def fleet():
    """The worker registry: {"<role>:<repo>": {"repo": path, ...}}.

    The registry is authored in the orchestrator repo and deployed to
    ABACUS_HOME/agents/fleet.json, out of the running orchestrator's reach.
    """
    p = home() / "agents" / "fleet.json"
    if not p.exists():
        return {}
    with p.open() as f:
        return json.load(f).get("workers", {})
