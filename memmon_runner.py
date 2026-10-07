"""Opt-in, machine-local command serialization for Claude, Codex and terminals.

Kernel locks are the authority, never PID matching or expiring timestamps.
Commands inherit the locks so killing their wrapper cannot admit another job.
Only foreground commands are supported: a daemon that closes inherited file
descriptors is outside this contract. This is bounded contention, not FIFO.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid


def _lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _write(path, value):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(value, fh)
    os.replace(tmp, path)


def jobs(state_dir):
    """Return a best-effort live snapshot; stale records never imply ownership."""
    result = []
    for path in (Path(state_dir) / "runner").glob("*.json"):
        try:
            with open(path.with_suffix(".lease"), "r") as lease:
                if _lock(lease.fileno()):
                    continue
                row = json.loads(path.read_text())
                row["elapsed_seconds"] = max(0, int(time.time() - row["created_at"]))
                result.append(row)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(result, key=lambda row: row["created_at"])


def _prune(root):
    # Unique run IDs are never reused. Never unlink a resource lock: replacing
    # that inode would let two processes believe they own the same resource.
    for path in root.glob("*.lease"):
        try:
            with open(path, "r") as lease:
                if _lock(lease.fileno()):
                    for suffix in (".json", ".tmp", ".lease"):
                        path.with_suffix(suffix).unlink(missing_ok=True)
        except OSError:
            pass


def run(command, state_dir, pressure_reader, resource="heavy", timeout=600,
        label=None, poll_interval=1.0):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", resource):
        raise ValueError("resource must be 1–64 letters, digits, dots, underscores or hyphens")
    if not command or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("provide a command and a finite, nonnegative wait timeout")
    if os.environ.get("MEMMON_RUN_ID"):
        print("memmon: nested runners are not supported; wrap the outer command once", file=sys.stderr)
        return 2

    root = Path(state_dir) / "runner"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _prune(root)
    run_id = uuid.uuid4().hex
    record = root / (run_id + ".json")
    # Open+lock the unique lease before publishing its path to pruning readers.
    # A temporary name is not considered by _prune/jobs.
    lease_tmp = root / (run_id + ".creating")
    lease = open(lease_tmp, "x+")
    fcntl.flock(lease, fcntl.LOCK_EX)
    lease_path = root / (run_id + ".lease")
    os.replace(lease_tmp, lease_path)
    resource_file = None
    child = None
    cancelled = [0]
    handlers = {}
    row = dict(id=run_id, resource=resource, label=label or Path(command[0]).name,
               cwd=os.getcwd(), wrapper_pid=os.getpid(), child_pid=None,
               created_at=time.time(), state="waiting", reason="acquiring resource")

    def cancel(signum, _frame):
        cancelled[0] = signum

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            handlers[signum] = signal.signal(signum, cancel)
        resource_file = open(root / (resource + ".lock"), "a+")
        deadline = time.monotonic() + timeout
        next_report = 0
        attempted = False
        while True:
            if cancelled[0]:
                return 128 + cancelled[0]
            if attempted and time.monotonic() >= deadline:
                print(f"memmon: timed out after {timeout:g}s waiting for {resource}: {row['reason']}; command not started", file=sys.stderr)
                return 124
            attempted = True
            acquired = _lock(resource_file.fileno())
            if acquired:
                try:
                    level = pressure_reader()["level"]
                except Exception as exc:
                    print(f"memmon: cannot read pressure ({type(exc).__name__}); command not started", file=sys.stderr)
                    return 125
                if level in ("HEALTHY", "WATCH"):
                    break
                fcntl.flock(resource_file, fcntl.LOCK_UN)
                row["reason"] = "memory pressure: " + str(level)
            else:
                owners = [j for j in jobs(state_dir)
                          if j["resource"] == resource and j["state"] in ("starting", "running", "cancelling")]
                row["reason"] = (f"resource held by {owners[0]['label']} (PID {owners[0].get('child_pid') or owners[0]['wrapper_pid']})"
                                 if owners else "resource busy; owner is starting or exiting")
            _write(record, row)
            now = time.monotonic()
            if now >= deadline:
                print(f"memmon: timed out after {timeout:g}s waiting for {resource}: {row['reason']}; command not started", file=sys.stderr)
                return 124
            if now >= next_report:
                print(f"memmon: waiting for {resource}: {row['reason']} ({deadline - now:.0f}s left)", file=sys.stderr, flush=True)
                next_report = now + 15
            time.sleep(min(poll_interval, max(0, deadline - now)))

        if cancelled[0]:
            return 128 + cancelled[0]
        row.update(state="starting", reason="resource acquired")
        _write(record, row)
        env = dict(os.environ, MEMMON_RUN_ID=run_id)
        try:
            child = subprocess.Popen(command, env=env, start_new_session=True,
                                     pass_fds=(resource_file.fileno(), lease.fileno()))
        except FileNotFoundError:
            print(f"memmon: command not found: {command[0]}", file=sys.stderr)
            return 127
        except OSError as exc:
            print(f"memmon: could not start command: {exc}", file=sys.stderr)
            return 126
        row.update(state="running", reason="command running", child_pid=child.pid,
                   started_at=time.time())
        _write(record, row)
        while child.poll() is None:
            if cancelled[0]:
                row.update(state="cancelling", reason="forwarding cancellation to command group")
                _write(record, row)
                _stop(child, cancelled[0])
                return 128 + cancelled[0]
            time.sleep(0.1)
        return child.returncode if child.returncode >= 0 else 128 - child.returncode
    finally:
        # An exception in status bookkeeping must not abandon a running job.
        if child is not None and child.poll() is None:
            _stop(child, signal.SIGTERM)
        if resource_file is not None:
            # Close, don't LOCK_UN: an inherited lock must outlive this wrapper
            # if the child still owns it after an uncatchable SIGKILL.
            resource_file.close()
        record.unlink(missing_ok=True)
        lease_path.unlink(missing_ok=True)
        lease.close()
        for signum, previous in handlers.items():
            signal.signal(signum, previous)


def _stop(child, signum):
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def cli(argv, state_dir, pressure_reader):
    parser = argparse.ArgumentParser(prog="memmon " + argv[0])
    if argv[0] == "jobs":
        parser.add_argument("--json", action="store_true")
        args = parser.parse_args(argv[1:])
        rows = jobs(state_dir)
        if args.json:
            print(json.dumps({"schema_version": 1, "jobs": rows}))
        elif not rows:
            print("No managed jobs. Only commands launched with `memmon run` appear here.")
        for row in ([] if args.json else rows):
            print(f"{row['resource']}  {row['state']}  {row['label']}  {row['elapsed_seconds']}s\n"
                  f"  {row['reason']} · {row['cwd']}")
        return 0
    parser.add_argument("--resource", default="heavy", help="machine-local exclusive slot (default: heavy)")
    parser.add_argument("--timeout", type=float, default=600, help="maximum wait before starting, seconds (default: 600)")
    parser.add_argument("--label", help="short task name, visible in jobs and the dashboard; avoid secrets")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- command [arguments]")
    args = parser.parse_args(argv[1:])
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        return run(command, state_dir, pressure_reader, args.resource, args.timeout, args.label)
    except ValueError as exc:
        parser.error(str(exc))
    except OSError as exc:
        print(f"memmon: runner unavailable: {exc}", file=sys.stderr)
        return 125
