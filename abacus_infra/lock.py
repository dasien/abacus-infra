"""The heavy lock: one machine-wide lock taken by every gate and push.

An atomic `mkdir` under ABACUS_HOME/state/locks (outside every repository).
Stale detection uses pid AND process start time, so a recycled pid is not
mistaken for the holder. Waiters retry under a budget below the tool timeout,
name the holder, and refuse rather than hang. Contention exits 75 and is
reported as its own outcome, never as a red gate.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

from . import config

CONTENTION = 75


def _lockdir(name):
    d = config.state_dir() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.lock"


def _start_time(pid):
    p = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    return p.stdout.strip() or None


def _holder(lock):
    try:
        return json.loads((lock / "owner.json").read_text())
    except (OSError, ValueError):
        return None


def _stale(holder):
    if not holder:
        return True
    started = _start_time(holder.get("pid", -1))
    return started is None or started != holder.get("started")


def acquire(name, budget, label):
    lock = _lockdir(name)
    deadline = time.monotonic() + budget
    while True:
        try:
            lock.mkdir()
            pid = os.getppid() if os.environ.get("ABACUS_LOCK_PARENT") else os.getpid()
            (lock / "owner.json").write_text(json.dumps(
                {"pid": pid, "started": _start_time(pid), "label": label, "cwd": os.getcwd()}))
            return lock
        except FileExistsError:
            holder = _holder(lock)
            if _stale(holder):
                shutil.rmtree(lock, ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"CONTENTION: heavy lock '{name}' held by pid {holder.get('pid')} "
                                   f"({holder.get('label')}) in {holder.get('cwd')}; this is contention, "
                                   f"not a failing gate -- retry later")
            time.sleep(2)


def main(argv=None):
    p = argparse.ArgumentParser(prog="abacus-lock", description=__doc__.split("\n")[0])
    p.add_argument("--name", default="heavy")
    p.add_argument("--budget", type=int, default=int(os.environ.get("ABACUS_LOCK_BUDGET", "420")),
                   help="seconds to wait (keep below the tool timeout)")
    p.add_argument("--label", default="")
    p.add_argument("cmd", nargs=argparse.REMAINDER, help="-- command to run under the lock")
    a = p.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        holder = _holder(_lockdir(a.name))
        print(json.dumps({"held": bool(holder) and not _stale(holder), "holder": holder}))
        return 0
    try:
        lock = acquire(a.name, a.budget, a.label or " ".join(cmd)[:80])
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        return CONTENTION
    try:
        return subprocess.call(cmd)
    finally:
        shutil.rmtree(lock, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
