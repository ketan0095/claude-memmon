"""Machine-wide memory admission for `memmon run`, used by Claude, Codex and terminals.

Kernel locks are the authority, never PID matching or expiring timestamps.
Commands inherit the locks, so killing their wrapper cannot admit another job.
Only foreground commands are supported: a daemon that closes inherited file
descriptors is outside this contract.

There is no daemon. Each wrapper publishes a ticket, the oldest admissible
ticket polls for its turn, and every decision is taken under one flock
(runner/coord/ledger.lock) against committed memory:

    committed = used + sum(max(0, reservation - footprint))  over admitted jobs
    admit     = estimate <= limit - committed, and pressure has been
                good for 30 s, and the strict reading is fresh

Modes (runner/coord/runner.json): protect (default), observe (v1 admission,
S2 decisions logged as "would hold") and paused (v1 exactly, the rollback).
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import select
from contextlib import contextmanager
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

import memmon_telemetry as telemetry

GiB = 1 << 30
RUN_ID_RE = re.compile(r"[0-9a-f]{32}")
QUEUE_MAX = 32
DEFAULT_ESTIMATE = 4 * GiB
HEADROOM_FRAC = 0.20
TICK_S = 2.0
LEDGER_POLL_S = 0.05
FOOTPRINT_STALE_S = 10.0
CLEAR_S = 30.0               # an intervention clears after this long without its cause
GROWTH_FACTOR = 1.5
PRESSURE_TICKS = 2
FAILED_TICKS = 3
AUTO_CANCEL_TICKS = 5        # CRITICAL for 10 s at one tick per 2 s
POLICY_GRACE_S = 10.0
PEAK_WAIT_S = 10.0           # finish waits this long for ledger.lock to save a peak
PEAKS_KEEP = 10
PEAKS_KEYS = 200
LOG_TRIM_AT = 1 << 20
MODES = ("protect", "observe", "paused")
DEFAULT_MODE = "protect"     # with no runner.json; the v1 tests pin it to paused
WAITING_REASONS = ("queued behind", "resource busy", "waiting for budget",
                   "holding for recovery", "telemetry unavailable")
LAUNCHER_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID",
                "CODEX_SESSION_ID", "TERM_PROGRAM")
HOLD_COPY = "memory must stay at Watch or better for 30 s"


class Cancelled(Exception):
    pass


class LedgerTimeout(Exception):
    pass


# ------------------------------------------------------------------ files

class Paths:
    def __init__(self, state_dir):
        self.state = Path(state_dir)
        self.root = self.state / "runner"
        self.queue = self.root / "queue"
        self.coord = self.root / "coord"
        self.ledger = self.coord / "ledger.lock"
        self.actions = self.coord / "actions.lock"
        self.admission = self.coord / "admission-state.json"
        self.peaks = self.coord / "job-peaks.json"
        self.mode = self.coord / "runner.json"
        self.log = self.coord / "admission-log.jsonl"
        self.config = self.state / "config.json"

    def make(self):
        for d in (self.root, self.queue, self.coord):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)


def _lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _write(path, value):
    """Atomic replace through a name no other writer uses."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as fh:
            json.dump(value, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path, default=None):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _free(path) -> bool:
    """A LOCK_EX|LOCK_NB probe, released at once: the only liveness test."""
    try:
        with open(path, "r") as fh:
            return _lock(fh.fileno())
    except OSError:
        return False


V2_FIELDS = ("reservation_bytes", "estimate", "job_key", "interruptible", "child",
             "footprint_bytes", "footprint_ts", "peak_bytes", "launcher", "queue_position",
             "deadline_ts", "intervention", "state_changed_ts", "ended_by", "mode", "via",
             "started_at", "child_pid", "child_start")


def jobs(state_dir):
    """Live run records: the lease is still locked. Stale records never imply
    ownership. Only `<run_id>.json` files count; coord/ and queue/ hold the
    rest of the runner's state."""
    result = []
    for path in (Path(state_dir) / "runner").glob("*.json"):
        if not RUN_ID_RE.fullmatch(path.stem):
            continue
        try:
            with open(path.with_suffix(".lease"), "r") as lease:
                if _lock(lease.fileno()):
                    continue
                row = json.loads(path.read_text())
                row["elapsed_seconds"] = max(0, int(time.time() - row["created_at"]))
                for key in V2_FIELDS:
                    row.setdefault(key, None)
                result.append(row)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(result, key=lambda row: row["created_at"])


def _prune(root):
    # Unique run IDs are never reused. Never unlink a resource lock: replacing
    # that inode would let two processes believe they own the same resource.
    for path in Path(root).glob("*.lease"):
        try:
            with open(path, "r") as lease:
                if _lock(lease.fileno()):
                    for suffix in (".json", ".tmp", ".lease"):
                        path.with_suffix(suffix).unlink(missing_ok=True)
        except OSError:
            pass


def tickets(queue):
    """Live tickets, oldest first: [(seq, run_id, resource, path)]."""
    out = []
    for path in Path(queue).glob("*.ticket"):
        m = re.fullmatch(r"(\d+)-([0-9a-f]{32})", path.stem)
        if not m or _free(path):
            continue
        meta = _read_json(path, {}) or {}
        out.append((int(m.group(1)), m.group(2), meta.get("resource") or "heavy", path))
    return sorted(out)


def _prune_tickets(queue):
    """Under ledger.lock only, so a ticket being created is never seen half-made."""
    for path in list(Path(queue).glob("*.ticket")) + list(Path(queue).glob("*.creating")):
        if _free(path):
            path.unlink(missing_ok=True)


# ----------------------------------------------------------- mode, config

def read_mode(state_dir) -> tuple:
    """(mode, warning, auto_cancel_interruptible). A runner.json that does not
    parse falls back to protect with a warning (S2.6)."""
    path = Paths(state_dir).mode
    if not path.exists():
        return DEFAULT_MODE, None, False
    data = _read_json(path)
    if not isinstance(data, dict) or data.get("mode", "protect") not in MODES:
        return "protect", "runner.json unreadable; using protect", False
    return data.get("mode", "protect"), None, data.get("auto_cancel_interruptible") is True


def write_mode(state_dir, mode=None, auto_cancel=None):
    p = Paths(state_dir)
    p.make()
    cur, _, auto = read_mode(state_dir)
    _write(p.mode, {"mode": mode or cur,
                    "auto_cancel_interruptible": auto if auto_cancel is None else auto_cancel,
                    "updated_at": round(time.time(), 3)})


def headroom_frac(state_dir) -> float:
    cfg = _read_json(Paths(state_dir).config, {}) or {}
    try:
        v = float(cfg.get("headroom_frac", HEADROOM_FRAC))
    except (TypeError, ValueError):
        return HEADROOM_FRAC
    return v if 0.0 <= v < 1.0 else HEADROOM_FRAC


# ---------------------------------------------------------------- ledger

@contextmanager
def ledger(paths, clock, deadline=None, cancelled=None):
    """flock, never fcntl, taken by LOCK_NB polling: a blocking flock retries
    after EINTR (PEP 475), so SIGTERM could never interrupt a waiter."""
    paths.make()
    fd = os.open(paths.ledger, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if cancelled and cancelled[0]:
                    raise Cancelled()
                if deadline is not None and clock.mono() >= deadline:
                    raise LedgerTimeout()
                clock.sleep(LEDGER_POLL_S)
        yield fd
    finally:
        os.close(fd)


def load_state(paths, boot):
    """Unparseable or other-boot state is empty, which needs a fresh 30 s window."""
    st = _read_json(paths.admission)
    if not isinstance(st, dict) or st.get("boot_id") != boot:
        return telemetry.fresh_state(boot)
    base = telemetry.fresh_state(boot)
    base.update(st)
    return base


def _log(paths, row):
    try:
        if paths.log.exists() and paths.log.stat().st_size > LOG_TRIM_AT:
            lines = paths.log.read_text().splitlines()[-2000:]
            _write_text(paths.log, "\n".join(lines) + "\n")
        with open(paths.log, "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _write_text(path, text):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


# ------------------------------------------------------------- estimates

def command_shape(command, label=None, via=None) -> str:
    """A short shape, never argv: the routed label, or the executable and up
    to two verb-like words after it."""
    if via == "route" and label:
        return label
    words = [os.path.basename(command[0])] if command else []
    for arg in command[1:]:
        if len(words) >= 3:
            break
        if re.fullmatch(r"[A-Za-z][\w:.-]{0,31}", arg) and "/" not in arg:
            words.append(arg)
    return " ".join(words)


def project_of(cwd) -> str:
    """Basename of the git common dir's parent, so worktrees share a key."""
    d = Path(cwd)
    for cur in [d, *d.parents]:
        git = cur / ".git"
        try:
            if git.is_dir():
                return cur.name
            if git.is_file():
                m = re.match(r"gitdir:\s*(.+)", git.read_text().strip())
                if not m:
                    return cur.name
                gitdir = Path(m.group(1))
                if not gitdir.is_absolute():
                    gitdir = (cur / gitdir).resolve()
                common = gitdir
                cd = gitdir / "commondir"
                if cd.is_file():
                    common = (gitdir / cd.read_text().strip()).resolve()
                elif "worktrees" in gitdir.parts:
                    common = Path(*gitdir.parts[:gitdir.parts.index("worktrees")])
                return common.parent.name
        except OSError:
            return ""
    return ""


def job_key(resource, shape, project) -> str:
    return hashlib.sha1("\0".join((resource, shape, project)).encode()).hexdigest()[:16]


def load_peaks(paths) -> dict:
    data = _read_json(paths.peaks)
    if not isinstance(data, dict) or not isinstance(data.get("keys"), dict):
        return {"version": 1, "keys": {}}
    return data


def estimate_for(peaks: dict, key: str, reserve_bytes=None) -> dict:
    if reserve_bytes is not None:
        return {"bytes": int(reserve_bytes), "confidence": "reserved", "samples": None}
    vals = [v for v in ((peaks.get("keys") or {}).get(key) or {}).get("peaks") or []
            if isinstance(v, (int, float)) and v > 0]
    n = len(vals)
    if n >= 3:
        return {"bytes": int(max(vals) * 1.15), "confidence": "learned", "samples": n}
    if n >= 1:
        return {"bytes": int(max(DEFAULT_ESTIMATE, max(vals) * 1.5)), "confidence": "low",
                "samples": n}
    return {"bytes": DEFAULT_ESTIMATE, "confidence": "unknown", "samples": 0}


def record_peak(paths, key: str, peak: int, now: float):
    """Caller holds ledger.lock. Last 10 peaks per key, 200 keys, LRU."""
    data = load_peaks(paths)
    keys = data["keys"]
    row = keys.get(key) or {"peaks": []}
    row = {"peaks": ((row.get("peaks") or []) + [int(peak)])[-PEAKS_KEEP:], "used": now}
    keys[key] = row
    if len(keys) > PEAKS_KEYS:
        keep = sorted(keys, key=lambda k: -(keys[k].get("used") or 0))[:PEAKS_KEYS]
        data["keys"] = {k: keys[k] for k in keep}
    _write(paths.peaks, data)


def touch_peaks(paths, key: str, now: float):
    data = load_peaks(paths)
    if key in data["keys"]:
        data["keys"][key]["used"] = now
        _write(paths.peaks, data)


# ------------------------------------------------------- committed memory

def _tree_footprint(procs, kids, child):
    """Footprint of the job tree rooted at `child` {pid, start}, or None when
    that exact process is not visible."""
    if not child or not child.get("pid"):
        return None
    p = procs.get(child["pid"])
    if (p is None or p.zombie or p.start is None or not child.get("start")
            or list(p.start) != list(child["start"]) or p.footprint is None):
        return None
    total, stack, seen = 0, [p.pid], set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        q = procs.get(pid)
        if q is None or q.zombie:
            continue
        total += q.footprint or 0
        stack.extend(kids.get(pid, ()))
    return total


def _kids(procs):
    out = {}
    for p in procs.values():
        out.setdefault(p.ppid, []).append(p.pid)
    return out


def _child_of(row):
    if row.get("child"):
        return row["child"]
    if row.get("child_pid"):
        return {"pid": row["child_pid"], "start": row.get("child_start")}
    return None


def reservation_of(row) -> int:
    """Leases from older runners carry no reservation: max(default, footprint)."""
    r = row.get("reservation_bytes")
    if isinstance(r, (int, float)) and r >= 0:
        return int(r)
    return max(DEFAULT_ESTIMATE, int(row.get("footprint_bytes") or 0))


def committed(rows, used, ram, frac, before=None, after=None, degraded=False, exclude=None):
    """S2.2. `before`/`after` are process tables sampled around the vm_stat
    read; a job's footprint is the lower of the two. Degraded inventory, or a
    tree that cannot be seen, counts the whole reservation."""
    kb = _kids(before) if before is not None else {}
    ka = _kids(after) if after is not None else {}
    slack = 0
    for row in rows:
        if row.get("id") == exclude or row.get("state") == "waiting":
            continue
        res = reservation_of(row)
        f = 0
        if not degraded and before is not None and after is not None:
            child = _child_of(row)
            fb, fa = _tree_footprint(before, kb, child), _tree_footprint(after, ka, child)
            f = min(fb, fa) if fb is not None and fa is not None else 0
        slack += max(0, res - f)
    limit = int(ram * (1 - frac))
    total = used + slack
    return {"used": int(used), "slack": int(slack), "limit": limit, "free": limit - total,
            "ram": int(ram), "headroom_frac": frac, "over": total > limit,
            "degraded": bool(degraded), "reason": None}


def committed_recorded(rows, used, ram, frac, now, own=None):
    """committed from the footprints the wrappers publish each tick, for
    display and the monitoring tick. A footprint older than 10 s counts the
    whole reservation. `own` is (run_id, footprint) measured just now.
    Admission never uses this: it scans the job trees itself."""
    c = committed([], used, ram, frac)
    slack = 0
    for r in rows:
        if r.get("state") == "waiting":
            continue
        if own and r.get("id") == own[0] and own[1] is not None:
            fp = own[1]
        else:
            fresh = r.get("footprint_ts") and now - r["footprint_ts"] <= FOOTPRINT_STALE_S
            fp = (r.get("footprint_bytes") or 0) if fresh else 0
        slack += max(0, reservation_of(r) - fp)
    c.update(slack=slack, free=c["limit"] - c["used"] - slack, over=c["used"] + slack > c["limit"])
    return c


PROC_PPID_ONLY = 6


def _child_lister(src):
    """proc_listpids(PROC_PPID_ONLY) for a libproc source, so a tick reads
    only its own job tree; None for any other source."""
    lib = getattr(src, "lib", None)
    if lib is None or src.name != "libproc":
        return None
    import ctypes
    fn = lib.proc_listpids
    fn.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    fn.restype = ctypes.c_int
    cap = 1024

    def children(pid):
        buf = (ctypes.c_int * cap)()
        n = fn(PROC_PPID_ONLY, pid, buf, ctypes.sizeof(buf))
        if n < 0 or n >= ctypes.sizeof(buf):
            raise OSError("proc_listpids failed or truncated")
        return [p for p in buf[:n // ctypes.sizeof(ctypes.c_int)] if p > 0]
    return children


def committed_total(c) -> int:
    return c["used"] + c["slack"]


# ------------------------------------------------------------- monitoring

def intervention_cause(m: dict, verdict: dict, footprint, c, reservation):
    """Advance one wrapper's tick counters and name what needs a human:
    pressure (DANGER/CRITICAL for 2 ticks), growth (footprint above 1.5x its
    reservation while committed is over the limit) or telemetry (3 failed
    ticks). None when nothing does."""
    level = verdict.get("level") if verdict.get("ok") else None
    m["failed"] = 0 if verdict.get("ok") else m.get("failed", 0) + 1
    m["bad"] = m.get("bad", 0) + 1 if level in ("DANGER", "CRITICAL") else 0
    m["critical"] = m.get("critical", 0) + 1 if level == "CRITICAL" else 0
    growth = (footprint is not None and c is not None and c["over"]
              and footprint > GROWTH_FACTOR * (reservation or 0))
    if m["bad"] >= PRESSURE_TICKS:
        return "pressure"
    if growth:
        return "growth"
    if m["failed"] >= FAILED_TICKS:
        return "telemetry"
    return None


# --------------------------------------------------------------- the run

class Runner:
    """One `memmon run`. Every collaborator is injectable: the telemetry read,
    the clock, the process source and the policy engine."""

    def __init__(self, command, state_dir, pressure_reader, resource="heavy", timeout=600,
                 label=None, poll_interval=1.0, reserve=None, interruptible=False, via=None,
                 telemetry_read=None, clock=None, source=None, tick_s=TICK_S,
                 hysteresis_s=telemetry.RECOVERY_S, policy_grace_s=POLICY_GRACE_S,
                 engine_factory=None, err=None):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", resource):
            raise ValueError("resource must be 1–64 letters, digits, dots, underscores or hyphens")
        if not command or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("provide a command and a finite, nonnegative wait timeout")
        if reserve is not None and (not math.isfinite(reserve) or reserve < 0):
            raise ValueError("--reserve must be a finite, nonnegative number of GB")
        self.command, self.resource, self.timeout = command, resource, timeout
        self.label = label or Path(command[0]).name
        self.pressure_reader = pressure_reader
        self.poll = poll_interval
        self.reserve = None if reserve is None else int(reserve * GiB)
        self.interruptible, self.via = bool(interruptible), via
        self.read = telemetry_read or (
            lambda: telemetry.read_pressure_strict(source=telemetry.MACH_SOURCE))
        self.clock = clock or telemetry.SYSTEM_CLOCK
        self._source = source
        self.tick_s, self.hysteresis_s = tick_s, hysteresis_s
        self.policy_grace_s = policy_grace_s
        self.engine_factory = engine_factory
        self.err = err or sys.stderr
        self.paths = Paths(state_dir)
        self.state_dir = state_dir
        self.cancelled = [0]
        self.child = None
        self.ticket = None
        self.hold_cause = None
        self.peak = None

    # ------------------------------------------------------- helpers

    def say(self, text):
        print("memmon: " + text, file=self.err, flush=True)

    def source(self):
        if self._source is None:
            import memmon_procs
            self._source = memmon_procs.default_source()
        return self._source

    def scan(self):
        try:
            src = self.source()
            if src.name == "degraded":
                return None
            return src.scan()
        except Exception:
            return None

    def job_tree(self):
        """The child's own process tree, {pid: Proc}, read through libproc's
        per-parent listing; the full table only when that is unavailable."""
        if getattr(self, "_lister", False) is False:
            try:
                self._lister = _child_lister(self.source())
            except Exception:
                self._lister = None
        child = (self.row or {}).get("child") or {}
        if self._lister is None or not child.get("pid"):
            return self.scan()
        src = self.source()
        try:
            out, stack = {}, [child["pid"]]
            while stack and len(out) < 4096:
                pid = stack.pop()
                if pid in out:
                    continue
                p = src.read(pid)
                if p is None:
                    continue
                out[pid] = p
                stack.extend(self._lister(pid))
            return out
        except Exception:
            return self.scan()

    def set_state(self, state, reason, **extra):
        if self.row.get("state") != state:
            self.row["state_changed_ts"] = round(self.clock.wall(), 3)
        self.row.update(state=state, reason=reason, **extra)
        self.write_record()

    def write_record(self):
        _write(self.record, self.row)
        self._written = {k: v for k, v in self.row.items() if k != "footprint_ts"}
        self._written_at = self.clock.mono()

    def write_if_changed(self):
        """A tick rewrites its record only when something a reader uses moved:
        any field, a footprint change of 1 MiB or more, or a footprint_ts
        about to age past the 10 s freshness readers apply."""
        last = getattr(self, "_written", None)
        if last is None or self.clock.mono() - self._written_at >= FOOTPRINT_STALE_S / 2:
            return self.write_record()
        now = {k: v for k, v in self.row.items() if k != "footprint_ts"}
        fp_old, fp_new = last.get("footprint_bytes"), now.get("footprint_bytes")
        last = dict(last, footprint_bytes=None, peak_bytes=None)
        now = dict(now, footprint_bytes=None, peak_bytes=None)
        moved = (fp_old is None) != (fp_new is None) or (
            fp_old is not None and abs(fp_new - fp_old) >= (1 << 20))
        if moved or now != last:
            self.write_record()

    # --------------------------------------------------------- entry

    def run(self):
        if os.environ.get("MEMMON_RUN_ID"):
            self.say("nested runners are not supported; wrap the outer command once")
            return 2
        self.paths.make()
        _prune(self.paths.root)
        self.mode, warning, self.auto_cancel = read_mode(self.state_dir)
        if warning:
            self.say(warning)
        self.run_id = uuid.uuid4().hex
        self.record = self.paths.root / (self.run_id + ".json")
        # Open+lock the unique lease before publishing its path to pruning readers.
        # A temporary name is not considered by _prune/jobs.
        lease_tmp = self.paths.root / (self.run_id + ".creating")
        self.lease = open(lease_tmp, "x+")
        fcntl.flock(self.lease, fcntl.LOCK_EX)
        self.lease_path = self.paths.root / (self.run_id + ".lease")
        os.replace(lease_tmp, self.lease_path)
        self.resource_file = None
        handlers = {}
        launcher = {k: os.environ[k] for k in LAUNCHER_ENV if os.environ.get(k)} or None
        shape = command_shape(self.command, self.label, self.via)
        self.key = job_key(self.resource, shape, project_of(os.getcwd()))
        self.row = dict(id=self.run_id, resource=self.resource, label=self.label,
                        cwd=os.getcwd(), wrapper_pid=os.getpid(), child_pid=None,
                        child_start=None, child=None, created_at=time.time(),
                        started_at=None, state="waiting", reason="acquiring resource",
                        mode=self.mode, via=self.via, interruptible=self.interruptible,
                        reservation_bytes=None, estimate=None, job_key=self.key,
                        footprint_bytes=None, footprint_ts=None, peak_bytes=None,
                        launcher=launcher, deadline_ts=round(time.time() + self.timeout, 3),
                        intervention=None, state_changed_ts=round(time.time(), 3),
                        ended_by=None)

        def cancel(signum, _frame):
            self.cancelled[0] = signum

        try:
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                handlers[signum] = signal.signal(signum, cancel)
            self.resource_file = open(self.paths.root / (self.resource + ".lock"), "a+")
            self.deadline = self.clock.mono() + self.timeout
            if self.mode == "protect":
                code = self.admit_protect()
            else:
                code = self.admit_v1()
            if code is not None:
                return code
            return self.launch_and_monitor()
        finally:
            self.finish(handlers)

    # ----------------------------------------------------- v1 / observe

    def admit_v1(self):
        """paused: v1 exactly (UNKNOWN counts as WATCH, S2.10). observe: the
        same decisions, plus the S2 verdict logged as "would hold"."""
        next_report = 0
        attempted = False
        _write(self.record, self.row)
        while True:
            if self.cancelled[0]:
                return 128 + self.cancelled[0]
            if attempted and self.clock.mono() >= self.deadline:
                return self.timed_out(124)
            attempted = True
            if _lock(self.resource_file.fileno()):
                try:
                    level = self.pressure_reader()["level"]
                except Exception as exc:
                    self.say(f"cannot read pressure ({type(exc).__name__}); command not started")
                    return 125
                if level in ("HEALTHY", "WATCH", "UNKNOWN"):
                    if self.mode == "observe":
                        self.observe_decision()
                    return None
                fcntl.flock(self.resource_file, fcntl.LOCK_UN)
                self.row["reason"] = "memory pressure: " + str(level)
            else:
                owners = [j for j in jobs(self.state_dir)
                          if j["resource"] == self.resource
                          and j["state"] in ("starting", "running", "cancelling",
                                             "intervention_needed", "detached")]
                self.row["reason"] = (
                    f"resource held by {owners[0]['label']} (PID {owners[0].get('child_pid') or owners[0]['wrapper_pid']})"
                    if owners else "resource busy; owner is starting or exiting")
            _write(self.record, self.row)
            now = self.clock.mono()
            if now >= self.deadline:
                return self.timed_out(124)
            if now >= next_report:
                self.say(f"waiting for {self.resource}: {self.row['reason']} ({self.deadline - now:.0f}s left)")
                next_report = now + 15
            self.clock.sleep(min(self.poll, max(0, self.deadline - now)))

    def observe_decision(self):
        try:
            with ledger(self.paths, self.clock, deadline=self.clock.mono() + 2.5):
                est = self.estimate()
                decision, reason, c = self.decide(est)
                _log(self.paths, self.log_row("admit" if decision else "would_hold",
                                              None if decision else reason, est, c))
        except Exception:
            pass

    # ---------------------------------------------------------- protect

    def estimate(self):
        peaks = load_peaks(self.paths)
        return estimate_for(peaks, self.key, self.reserve)

    def take_ticket(self):
        """Under ledger.lock: prune, bound the queue, then .creating -> lock -> .ticket."""
        _prune_tickets(self.paths.queue)
        live = list(self.paths.queue.glob("*.ticket"))
        if len(live) >= QUEUE_MAX:
            return False
        seq_path = self.paths.queue / "seq"
        try:
            seq = int(seq_path.read_text().strip() or 0) + 1
        except (OSError, ValueError):
            seq = 1
        _write_text(seq_path, str(seq))
        stem = f"{seq:08d}-{self.run_id}"
        creating = self.paths.queue / (stem + ".creating")
        fh = open(creating, "x+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        json.dump({"resource": self.resource, "run_id": self.run_id}, fh)
        fh.flush()
        path = self.paths.queue / (stem + ".ticket")
        os.replace(creating, path)
        self.ticket = (fh, path, seq)
        return True

    def drop_ticket(self):
        if self.ticket:
            fh, path, _ = self.ticket
            path.unlink(missing_ok=True)
            fh.close()
            self.ticket = None

    def candidate(self):
        """Oldest live ticket whose resource lock is free right now; tickets
        whose resource is held are skipped. Caller holds ledger.lock, so the
        probes never race a candidate taking its lock."""
        live = tickets(self.paths.queue)
        free = {}
        for i, (seq, rid, res, _) in enumerate(live):
            if res not in free:
                if rid == self.run_id:
                    free[res] = _lock(self.resource_file.fileno())
                    if free[res]:
                        fcntl.flock(self.resource_file, fcntl.LOCK_UN)
                else:
                    free[res] = _free(self.paths.root / (res + ".lock")) if (
                        self.paths.root / (res + ".lock")).exists() else True
            if free[res]:
                return i + 1, rid, live
        return None, None, live

    def decide(self, est):
        """The critical section: leases, footprints, strict telemetry,
        footprints again, the shared hysteresis, then the decision. Caller
        holds ledger.lock. Returns (admit, reason, committed)."""
        clock = self.clock
        boot = clock.boot()
        state = load_state(self.paths, boot)
        telemetry.note_clocks(state, clock.mono(), clock.awake())
        rows = jobs(self.state_dir)
        before = self.scan()
        reading, error = None, None
        try:
            reading = self.read()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        after = self.scan()
        now = clock.mono()
        state, verdict = telemetry.strict_verdict(state, reading, now, error)
        good = telemetry.is_good(verdict)
        state = telemetry.apply_hysteresis(state, good, verdict.get("mono") or now, now)
        rec = telemetry.recovery(state, now, self.hysteresis_s)
        c = None
        if reading is not None:
            c = committed(rows, reading["used_bytes"], reading["ram_total"],
                          headroom_frac(self.state_dir), before, after,
                          degraded=before is None or after is None, exclude=self.run_id)
            state["committed"] = dict(c, ts=round(clock.wall(), 3))
        if not verdict.get("ok"):
            admit, reason = False, f"telemetry unavailable ({verdict.get('reason')})"
            self.hold_cause = "telemetry"
        elif not good:
            level = verdict.get("level")
            if verdict.get("pressure_level") != "normal":
                level = f"kernel pressure {verdict.get('pressure_level')}"
            admit, reason = False, f"holding for recovery: {level}; {HOLD_COPY}"
            self.hold_cause = "pressure"
        elif not rec["open"]:
            admit = False
            reason = f"holding for recovery: {HOLD_COPY} ({rec['remaining_s'] or 0:.0f}s to go)"
            self.hold_cause = "pressure"
        elif est["bytes"] > c["free"]:
            admit = False
            reason = (f"waiting for budget: needs {est['bytes'] / GiB:.1f} GB, "
                      f"{max(0, c['free']) / GiB:.1f} GB free to admit")
            self.hold_cause = "budget"
        else:
            admit, reason = True, None
        state["hold"] = None if admit else {"reason": reason, "mono": now}
        _write(self.paths.admission, state)
        return admit, reason, c

    def log_row(self, decision, reason, est, c):
        return {"ts": round(time.time(), 3), "run_id": self.run_id, "resource": self.resource,
                "mode": self.mode, "decision": decision, "reason": reason,
                "estimate": est["bytes"], "used": c and c["used"], "slack": c and c["slack"],
                "committed": c and committed_total(c), "limit": c and c["limit"],
                "free": c and c["free"]}

    def admit_protect(self):
        clock = self.clock
        try:
            with ledger(self.paths, clock, self.deadline, self.cancelled):
                if not self.take_ticket():
                    self.say(f"admission queue full ({QUEUE_MAX})")
                    return 124
                est = self.estimate()
                self.row.update(estimate=est, reservation_bytes=None)
                _write(self.record, self.row)
        except Cancelled:
            return 128 + self.cancelled[0]
        except LedgerTimeout:
            return self.timed_out(124)
        next_report = 0
        attempted = False           # --timeout 0 still gets one attempt, as in v1
        while True:
            if self.cancelled[0]:
                return 128 + self.cancelled[0]
            if attempted and clock.mono() >= self.deadline:
                return self.timed_out(125 if self.hold_cause == "telemetry" else 124)
            attempted = True
            wait = 2 * self.poll
            try:
                with ledger(self.paths, clock, self.deadline, self.cancelled):
                    pos, cand, live = self.candidate()
                    mine = next((i + 1 for i, t in enumerate(live) if t[1] == self.run_id), None)
                    self.row["queue_position"] = mine
                    if cand == self.run_id and _lock(self.resource_file.fileno()):
                        wait = self.poll
                        est = self.estimate()
                        admit, reason, c = self.decide(est)
                        if admit:
                            self.row.update(state="starting", reason="admitted",
                                            reservation_bytes=est["bytes"], estimate=est,
                                            queue_position=None,
                                            state_changed_ts=round(clock.wall(), 3))
                            _write(self.record, self.row)
                            touch_peaks(self.paths, self.key, clock.wall())
                            self.drop_ticket()
                            _log(self.paths, self.log_row("admit", None, est, c))
                            return None
                        fcntl.flock(self.resource_file, fcntl.LOCK_UN)
                        _log(self.paths, self.log_row("hold", reason, est, c))
                        self.row["reason"] = reason
                    elif cand is None or cand == self.run_id or not self._resource_free():
                        self.row["reason"] = self.busy_reason()
                        self.hold_cause = "resource"
                    else:
                        self.row["reason"] = f"queued behind #{pos}"
                        self.hold_cause = "queue"
                    _write(self.record, self.row)
            except Cancelled:
                return 128 + self.cancelled[0]
            except LedgerTimeout:
                self.row["reason"] = "waiting for budget: admission ledger busy"
                return self.timed_out(125 if self.hold_cause == "telemetry" else 124)
            now = clock.mono()
            if now >= next_report:
                self.say(f"waiting for {self.resource}: {self.row['reason']} ({self.deadline - now:.0f}s left)")
                next_report = now + 15
            clock.sleep(min(wait, max(0, self.deadline - now)))

    def _resource_free(self):
        if _lock(self.resource_file.fileno()):
            fcntl.flock(self.resource_file, fcntl.LOCK_UN)
            return True
        return False

    def busy_reason(self):
        owners = [j for j in jobs(self.state_dir)
                  if j["resource"] == self.resource and j["id"] != self.run_id
                  and j["state"] not in ("waiting",)]
        if owners:
            o = owners[0]
            return f"resource busy: held by {o['label']} (PID {o.get('child_pid') or o['wrapper_pid']})"
        return "resource busy; owner is starting or exiting"

    def timed_out(self, code):
        self.say(f"timed out after {self.timeout:g}s waiting for {self.resource}: "
                 f"{self.row['reason']}; command not started")
        return code

    # ---------------------------------------------------------- running

    def launch_and_monitor(self):
        if self.cancelled[0]:
            return 128 + self.cancelled[0]
        if self.mode != "protect":
            self.row.update(state="starting", reason="resource acquired")
            _write(self.record, self.row)
        env = dict(os.environ, MEMMON_RUN_ID=self.run_id)
        try:
            self.child = subprocess.Popen(self.command, env=env, start_new_session=True,
                                          pass_fds=(self.resource_file.fileno(),
                                                    self.lease.fileno()))
        except FileNotFoundError:
            self.say(f"command not found: {self.command[0]}")
            return 127
        except OSError as exc:
            self.say(f"could not start command: {exc}")
            return 126
        start = _start_of(self.child.pid)
        self.row.update(child_pid=self.child.pid, child_start=start,
                        child={"pid": self.child.pid, "start": start},
                        started_at=time.time())
        self.set_state("running", "command running")
        self.monitor = {"bad": 0, "failed": 0, "critical": 0, "clear_since": None}
        next_tick = self.clock.mono() + self.tick_s
        waiter = _ExitWaiter(self.child.pid)
        while self.child.poll() is None:
            if self.cancelled[0]:
                self.set_state("cancelling", "forwarding cancellation to command group",
                               ended_by="signal")
                _stop(self.child, self.cancelled[0])
                return 128 + self.cancelled[0]
            if self.mode == "protect" and self.clock.mono() >= next_tick:
                next_tick = self.clock.mono() + self.tick_s
                if self.tick() == "cancelled":
                    waiter.close()
                    self.child.wait()
                    self.say(f"cancelled by policy (ended_by=policy, run_id={self.run_id})")
                    return 75
            # Sleep until the child exits, a signal arrives or the next tick
            # is due, instead of waking 20 times a second to poll.
            due = next_tick - self.clock.mono() if self.mode == "protect" else 1.0
            waiter.wait(min(1.0, max(0.0, due)))
        waiter.close()
        code = self.child.returncode
        if self.row.get("ended_by") == "policy":
            self.say(f"cancelled by policy (ended_by=policy, run_id={self.run_id})")
            return 75
        return code if code >= 0 else 128 - code

    def tick(self):
        """One monitoring tick: strict read, own job tree, shared hysteresis."""
        clock = self.clock
        try:
            with ledger(self.paths, clock, clock.mono() + 2.0, self.cancelled):
                boot = clock.boot()
                state = load_state(self.paths, boot)
                telemetry.note_clocks(state, clock.mono(), clock.awake())
                reading, error = None, None
                try:
                    reading = self.read()
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                procs = self.job_tree()
                fp = None
                if procs is not None:
                    fp = _tree_footprint(procs, _kids(procs), self.row["child"])
                now = clock.mono()
                state, v = telemetry.strict_verdict(state, reading, now, error)
                state = telemetry.apply_hysteresis(state, telemetry.is_good(v),
                                                   v.get("mono") or now, now)
                c = None
                if reading is not None:
                    c = committed_recorded(jobs(self.state_dir), reading["used_bytes"],
                                           reading["ram_total"], headroom_frac(self.state_dir),
                                           clock.wall(), own=(self.run_id, fp))
                    state["committed"] = dict(c, ts=round(clock.wall(), 3))
                _write(self.paths.admission, state)
        except (LedgerTimeout, Cancelled):
            return None
        if fp is not None:
            self.peak = max(self.peak or 0, fp)
            self.row.update(footprint_bytes=fp, footprint_ts=round(clock.wall(), 3),
                            peak_bytes=self.peak)
        m = self.monitor
        cause = intervention_cause(m, v, fp, c, self.row.get("reservation_bytes"))
        level = v.get("level")
        state_now = self.row["state"]
        if cause and state_now == "running":
            text = {"pressure": f"memory {level} for {m['bad']} ticks",
                    "growth": "footprint above 1.5x its reservation while committed memory is over the limit",
                    "telemetry": f"telemetry failing for {m['failed']} ticks"}[cause]
            self.row["intervention"] = {"since_ts": round(clock.wall(), 3), "cause": cause}
            self.set_state("intervention_needed", text)
            m["clear_since"] = None
            self.say(f"{self.label} needs attention: {text}. It keeps running; "
                     f"stop it from the menu bar or `memmon owners` if needed (run_id={self.run_id})")
        elif state_now == "intervention_needed":
            if cause:
                m["clear_since"] = None
                self.write_if_changed()
            else:
                m["clear_since"] = m["clear_since"] or now
                if now - m["clear_since"] >= CLEAR_S:
                    self.row["intervention"] = None
                    self.set_state("running", "command running")
                else:
                    self.write_if_changed()
        else:
            self.write_if_changed()
        if (self.interruptible and m["critical"] >= AUTO_CANCEL_TICKS
                and read_mode(self.state_dir)[2]):
            return self.policy_cancel()
        return None

    def policy_cancel(self):
        """I-4 exception (b): S1's engine on our own child, per PID, with a
        grace period and per-PID SIGKILL of observed survivors only.

        The outcome follows the child, which is ours and unreaped, not the
        engine's result: a child that exits on its own keeps its exit code,
        and one that dies after our SIGTERM was cancelled by policy even when
        the engine still reports other members of its group."""
        if self.child.poll() is not None:
            return None
        out, eng = None, None
        try:
            factory = self.engine_factory or _default_engine
            eng, token = factory(self, self.policy_grace_s)
            if token is None:
                out = {"result": "refused", "reason": "degraded_identity"}
            else:
                out = eng.run("stop-managed-job", token)
                if out.get("result") == "partial" and out.get("force_token"):
                    out = eng.run("force", out["force_token"])
        except Exception as exc:
            out = {"result": "error", "reason": f"{type(exc).__name__}: {exc}"}
        signalled = any(pid == self.child.pid and sig == signal.SIGTERM
                        for pid, sig in (getattr(eng, "sent", None) or []))
        if signalled and self.child.poll() is None:
            try:
                self.child.wait(timeout=1.0)        # it may still be exiting
            except subprocess.TimeoutExpired:
                pass
        if self.child.poll() is not None:
            if not signalled:
                return None
            self.row["ended_by"] = "policy"
            self.set_state("cancelled_by_policy", "cancelled by policy: CRITICAL for 10 s")
            return "cancelled"
        if not self.row.get("reason", "").startswith("auto-cancel refused"):
            self.say(f"auto-cancel refused ({out.get('reason') or out.get('result')}); "
                     f"{self.label} keeps running")
            self.row["reason"] = f"auto-cancel refused: {out.get('reason') or out.get('result')}"
            _write(self.record, self.row)
        self.monitor["critical"] = 0
        return None

    # ----------------------------------------------------------- finish

    def save_peak(self, wait_s=PEAK_WAIT_S) -> bool:
        """Record this run's peak, waiting out a slow critical section (about
        2.1 s worst case) and a few waiters behind it. A sample that still
        cannot be written is logged, never silently lost."""
        try:
            # A SIGINT/SIGTERM during the wait ends it at once.
            with ledger(self.paths, self.clock, self.clock.mono() + wait_s, self.cancelled):
                record_peak(self.paths, self.key, self.peak, self.clock.wall())
            return True
        except Exception as exc:
            _log(self.paths, {"ts": round(time.time(), 3), "run_id": self.run_id,
                              "decision": "peak_dropped", "job_key": self.key,
                              "peak": self.peak, "reason": type(exc).__name__})
            return False

    def finish(self, handlers):
        # An exception in status bookkeeping must not abandon a running job.
        if self.child is not None and self.child.poll() is None:
            _stop(self.child, signal.SIGTERM)
        self.drop_ticket()
        if self.resource_file is not None:
            # Close, don't LOCK_UN: an inherited lock must outlive this wrapper
            # if the child still owns it after an uncatchable SIGKILL.
            self.resource_file.close()
        if self.peak and getattr(self, "mode", None) == "protect":
            self.save_peak()
        self.lease.close()
        # The flock probe is the only release test: a grandchild that inherited
        # the lease keeps the job's reservation alive after the child exits.
        try:
            with open(self.lease_path, "r") as probe:
                if _lock(probe.fileno()):
                    self.record.unlink(missing_ok=True)
                    self.lease_path.unlink(missing_ok=True)
                else:
                    self.set_state("detached", "command exited; a descendant still holds its lease")
        except OSError:
            pass
        for signum, previous in handlers.items():
            signal.signal(signum, previous)


class _ExitWaiter:
    """Blocks on kqueue for the child's exit or SIGINT/SIGTERM/SIGHUP (which
    still reach the Python handlers), with a timeout. Without kqueue, or if
    the child is already gone, it falls back to a short sleep."""

    def __init__(self, pid):
        self.kq = None
        try:
            kq = select.kqueue()
            evs = [select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD,
                                 select.KQ_NOTE_EXIT)]
            evs += [select.kevent(s, select.KQ_FILTER_SIGNAL, select.KQ_EV_ADD)
                    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)]
            kq.control(evs, 0, 0)
            self.kq = kq
        except (AttributeError, OSError):
            pass

    def wait(self, timeout):
        if self.kq is None:
            time.sleep(min(timeout, 0.05))
            return
        try:
            self.kq.control(None, 4, timeout)
        except OSError:
            time.sleep(min(timeout, 0.05))

    def close(self):
        if self.kq is not None:
            self.kq.close()
            self.kq = None


def _default_engine(runner, grace_s):
    """The S1 engine, aimed at this wrapper's own child with a token it mints
    for itself. Degraded inventory has no identity, so no token."""
    import memmon_act
    import memmon_owners
    import memmon_procs
    src = runner.source()
    if src.name == "degraded":
        return None, None
    row = dict(runner.row)
    ctx = memmon_owners.Context(leases=[row])
    inv = memmon_procs.snapshot(src)
    part = memmon_owners.partition(inv, ctx)
    cp = inv.procs.get(runner.child.pid)
    if cp is None or cp.start is None:
        return None, None
    root = cp if cp.pid in part.root_owner else inv.procs.get(os.getpid())
    token = memmon_owners.mint_token({
        "v": 1, "action": "stop-managed-job",
        "owner_id": part.owner_of.get(root.pid), "owner_root": memmon_owners.ident(root),
        "target": memmon_owners.ident(cp), "target_pgid": cp.pgid,
        "run_id": runner.run_id, "snapshot_ts": round(inv.ts, 3)})
    eng = memmon_act.Engine(source=src, partition_fn=lambda i: memmon_owners.partition(i, ctx),
                            lock_path=str(runner.paths.actions), leases_fn=lambda: [row],
                            grace_s=grace_s)
    return eng, token


def run(command, state_dir, pressure_reader, resource="heavy", timeout=600,
        label=None, poll_interval=1.0, **kw):
    return Runner(command, state_dir, pressure_reader, resource, timeout, label,
                  poll_interval, **kw).run()


def _start_of(pid):
    """The child's (sec, usec) start, so a stop request can prove it names this
    child and not a later process that reused the PID. The child is ours and
    unreaped, so the PID cannot be reused while this reads it."""
    try:
        import memmon_procs
        proc = memmon_procs.default_source().read(pid)
        return list(proc.start) if proc is not None and proc.start else None
    except Exception:
        return None


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


# -------------------------------------------------------------- snapshot

def snapshot(state_dir, system=None, clock=None) -> dict:
    """`memmon jobs --json` schema 2, also embedded by `owners --json`.
    `system` is a memmon_procs.read_system_strict() result; without one this
    reads its own. Display only: nothing here admits."""
    clock = clock or telemetry.SYSTEM_CLOCK
    paths = Paths(state_dir)
    rows = jobs(state_dir)
    live = tickets(paths.queue) if paths.queue.exists() else []
    pos = {rid: i + 1 for i, (_, rid, _, _) in enumerate(live)}
    for row in rows:
        row["queue_position"] = pos.get(row["id"]) if row.get("state") == "waiting" else None
    mode, warning, auto = read_mode(state_dir)
    frac = headroom_frac(state_dir)
    reason = None
    if system is None:
        try:
            import memmon_procs
            system = memmon_procs.read_system_strict()
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
    if system is not None and system.get("used_bytes") is not None:
        # Recorded footprints approximate the admission scan; a stale one
        # counts the whole reservation, as admission would.
        c = committed_recorded(rows, system["used_bytes"], system["ram_bytes"], frac, time.time())
        c["ts"] = round(time.time(), 3)
    else:
        c = {"used": None, "slack": None, "limit": None, "free": None, "ram": None,
             "headroom_frac": frac, "over": None, "degraded": False,
             "reason": reason or "system memory unavailable", "ts": None}
    st = load_state(paths, clock.boot())
    rec = telemetry.recovery(st, clock.mono())
    hold = (st.get("hold") or {}).get("reason") if live else None
    return {"schema_version": 2, "mode": mode, "mode_warning": warning,
            "auto_cancel_interruptible": auto, "committed": c,
            "admission": {"open": rec["open"],
                          "reason": hold or (None if rec["open"] else "holding for recovery"),
                          "recovery_remaining_s": rec["remaining_s"],
                          "hysteresis_s": telemetry.RECOVERY_S},
            "queue": {"length": len(live), "max": QUEUE_MAX}, "jobs": rows}


# ------------------------------------------------------------------- CLI

def _gib(n):
    return "—" if n is None else f"{n / GiB:.1f} GB"


def cli(argv, state_dir, pressure_reader):
    parser = argparse.ArgumentParser(prog="memmon " + argv[0])
    if argv[0] == "jobs":
        parser.add_argument("--json", action="store_true")
        args = parser.parse_args(argv[1:])
        snap = snapshot(state_dir)
        rows = snap["jobs"]
        if args.json:
            print(json.dumps(snap))
            return 0
        c = snap["committed"]
        print(f"mode: {snap['mode']}" + (f"  ({snap['mode_warning']})" if snap["mode_warning"] else ""))
        if c["used"] is not None:
            if c["over"]:
                print(f"Committed {_gib(c['used'] + c['slack'])} of {_gib(c['limit'])} limit · "
                      f"{_gib(-c['free'])} over; the {int(c['headroom_frac'] * 100)} % headroom "
                      f"target is not currently met")
            else:
                print(f"Committed {_gib(c['used'] + c['slack'])} of {_gib(c['limit'])} limit · "
                      f"{_gib(c['free'])} free to admit")
        if not rows:
            print("No managed jobs. Only commands launched with `memmon run` appear here.")
        for row in rows:
            res = row.get("reservation_bytes")
            extra = f"  reserved {_gib(res)}" if res is not None else ""
            print(f"{row['resource']}  {row['state']}  {row['label']}  {row['elapsed_seconds']}s{extra}\n"
                  f"  {row['reason']} · {row['cwd']}")
        return 0
    if argv[0] == "run-mode":
        parser.add_argument("mode", nargs="?", choices=MODES)
        parser.add_argument("--auto-cancel-interruptible", choices=("on", "off"),
                            help="let CRITICAL cancel jobs started with --interruptible (default off)")
        parser.add_argument("--json", action="store_true")
        args = parser.parse_args(argv[1:])
        if args.mode or args.auto_cancel_interruptible:
            write_mode(state_dir, args.mode, None if args.auto_cancel_interruptible is None
                       else args.auto_cancel_interruptible == "on")
        mode, warning, auto = read_mode(state_dir)
        if args.json:
            print(json.dumps({"mode": mode, "warning": warning, "auto_cancel_interruptible": auto}))
        else:
            print(f"memmon run mode: {mode}" + (f" ({warning})" if warning else "")
                  + f"; auto-cancel of --interruptible jobs: {'on' if auto else 'off'}")
        return 0
    parser.add_argument("--resource", default="heavy", help="machine-local exclusive slot (default: heavy)")
    parser.add_argument("--timeout", type=float, default=600, help="maximum wait before starting, seconds (default: 600)")
    parser.add_argument("--label", help="short task name, visible in jobs and the dashboard; avoid secrets")
    parser.add_argument("--reserve", type=float, metavar="GB",
                        help="memory to reserve instead of the learned estimate")
    parser.add_argument("--interruptible", action="store_true",
                        help="allow auto-cancel at sustained CRITICAL when enabled with run-mode")
    parser.add_argument("--via", choices=("route",), help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- command [arguments]")
    args = parser.parse_args(argv[1:])
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        return run(command, state_dir, pressure_reader, args.resource, args.timeout, args.label,
                   reserve=args.reserve, interruptible=args.interruptible, via=args.via)
    except ValueError as exc:
        parser.error(str(exc))
    except OSError as exc:
        print(f"memmon: runner unavailable: {exc}", file=sys.stderr)
        return 125
