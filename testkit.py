"""Shared test fixtures: a scripted process table, isolated state paths, and a
registry of real synthetic processes that is the only thing tests may signal.

Real-process tests spawn `sys.executable -c` sleepers and nothing else. Every
PID they spawn is registered with its start time; the engine they use gets a
kill function that refuses any PID that is neither registered nor a live
descendant of a registered one, and teardown reaps exactly the registered
identities."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time

import memmon
import memmon_act
import memmon_owners
import memmon_procs
from memmon_procs import Proc

MB = 1 << 20
PY = sys.executable

PATH_CONSTANTS = ("STATE_DIR", "HISTORY", "SNAPSHOT", "GATE_LOG", "PROFILE",
                  "SHELL_STATE", "PAUSE", "PENDING", "OWNERS_HISTORY",
                  "CPU_BASELINE", "ACTIONS_LOCK", "JOBS_DIR", "PROJECTS_DIR",
                  "CLAUDE_SESSIONS_DIR", "CC_SOCKS_DIR", "CODEX_HOME",
                  "CLAUDE_ROSTER", "PRESSURE_FILE", "JOB_HISTORY", "PRESSURE_EPISODE")


class TempState:
    """Points every memmon path constant at a temp directory."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        # A constant this memmon.py does not define yet is skipped.
        self.saved = {n: getattr(memmon, n) for n in PATH_CONSTANTS if hasattr(memmon, n)}
        r = self.root
        values = {
            "STATE_DIR": r, "HISTORY": f"{r}/history.jsonl",
            "SNAPSHOT": f"{r}/latest.json", "GATE_LOG": f"{r}/gate.jsonl",
            "PROFILE": f"{r}/profile.json", "SHELL_STATE": f"{r}/learned.zsh",
            "PAUSE": f"{r}/paused.json", "PENDING": f"{r}/blocked.json",
            "OWNERS_HISTORY": f"{r}/owners-history.json",
            "CPU_BASELINE": f"{r}/cpu-baseline.json",
            "ACTIONS_LOCK": f"{r}/runner/coord/actions.lock",
            "JOBS_DIR": f"{r}/jobs", "PROJECTS_DIR": f"{r}/projects",
            "CLAUDE_SESSIONS_DIR": f"{r}/sessions", "CC_SOCKS_DIR": f"{r}/socks",
            "CODEX_HOME": f"{r}/codex", "CLAUDE_ROSTER": f"{r}/daemon/roster.json",
            "PRESSURE_FILE": f"{r}/pressure.json", "JOB_HISTORY": f"{r}/job-history.json",
            "PRESSURE_EPISODE": f"{r}/runner/coord/pressure-episode.json",
        }
        for n, v in values.items():
            if n in self.saved:
                setattr(memmon, n, v)
        # memmon_owners has its own defaults for a bare Context().
        owner_paths = {"CLAUDE_SESSIONS_DIR": values["CLAUDE_SESSIONS_DIR"],
                       "CC_SOCKS_DIR": values["CC_SOCKS_DIR"],
                       "CODEX_HOME": values["CODEX_HOME"],
                       "CLAUDE_JOBS_DIR": values["JOBS_DIR"],
                       "CLAUDE_ROSTER": values["CLAUDE_ROSTER"]}
        self.saved_owners = {n: getattr(memmon_owners, n) for n in owner_paths}
        for n, v in owner_paths.items():
            setattr(memmon_owners, n, v)
        os.makedirs(values["CLAUDE_SESSIONS_DIR"])

    def close(self):
        for n, v in self.saved.items():
            setattr(memmon, n, v)
        for n, v in self.saved_owners.items():
            setattr(memmon_owners, n, v)
        self.tmp.cleanup()


# ----------------------------------------------------------- fake source

def P(pid, ppid=1, start=None, pgid=None, comm="proc", fp=10 * MB, ticks=0,
      uid=None, visible=True, zombie=False):
    return Proc(pid=pid, start=start or (1_700_000_000 + pid, pid % 1_000_000),
                ppid=ppid, pgid=pgid if pgid is not None else pid,
                uid=os.getuid() if uid is None else uid, zombie=zombie,
                footprint=fp if visible else None, resident=fp if visible else None,
                lifetime_max=fp if visible else None,
                cpu_ticks=ticks if visible else None, comm=comm, visible=visible)


class FakeSource(memmon_procs.ProcSource):
    """A scripted process table. kill() delivers the signal the way the
    scripted process would react to it: by default it exits."""

    name = "libproc"

    def __init__(self, procs, argv=None, paths=None, cwds=None, ignore_term=(),
                 responsible=None):
        self.table = {p.pid: p for p in procs}
        self._responsible = dict(responsible or {})
        self._argv = dict(argv or {})
        self._paths = dict(paths or {})
        self._cwds = dict(cwds or {})
        self.ignore_term = set(ignore_term)
        self.signals = []
        self.on_kill = None

    def scan(self):
        return {pid: self._copy(p) for pid, p in self.table.items()}

    def read(self, pid):
        p = self.table.get(pid)
        return self._copy(p) if p else None

    @staticmethod
    def _copy(p):
        return Proc(**{k: getattr(p, k) for k in p.__dataclass_fields__})

    def argv(self, pid):
        return list(self._argv.get(pid, [self.table[pid].comm] if pid in self.table else []))

    def path(self, pid):
        return self._paths.get(pid)

    def cwd(self, pid):
        return self._cwds.get(pid)

    def responsible(self, pid):
        return self._responsible.get(pid)

    def timebase(self):
        return (125, 3)

    def kill(self, pid, sig):
        if pid not in self.table:
            raise ProcessLookupError(pid)
        self.signals.append((pid, sig))
        if self.on_kill:
            self.on_kill(pid, sig)
        if sig == signal.SIGTERM and pid in self.ignore_term:
            return
        if sig in (signal.SIGTERM, signal.SIGKILL):
            del self.table[pid]
            for p in self.table.values():
                if p.ppid == pid:
                    p.ppid = 1


class FakeClock:
    """A monotonic clock that only moves when the engine sleeps, firing any
    events scheduled for the time it passes."""

    def __init__(self, t=1000.0):
        self.t = t
        self.events = []

    def mono(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.05)
        due = [e for e in self.events if e[0] <= self.t]
        self.events = [e for e in self.events if e[0] > self.t]
        for _, fn in sorted(due, key=lambda e: e[0]):
            fn()

    def at(self, t, fn):
        self.events.append((t, fn))


def session_file(ctx_dir, pid, start_sec, job_id=None, session_id=None,
                 cwd=None, spare=None, status=None):
    """A ~/.claude/sessions/<pid>.json as Claude writes it (procStart in UTC)."""
    d = {"pid": pid, "jobId": job_id, "sessionId": session_id, "cwd": cwd,
         "procStart": time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(start_sec))}
    if spare is not None:
        d["spare"] = spare
    if status is not None:
        d["status"] = status
    with open(os.path.join(ctx_dir, f"{pid}.json"), "w") as fh:
        json.dump(d, fh)


def fake_engine(source, ctx, lock_path, **kw):
    kw.setdefault("grace_s", 0.5)
    kw.setdefault("poll_s", 0.0)
    kw.setdefault("force_wait_s", 0.2)
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("system_reader", lambda: {"used_bytes": 10 * MB})
    kw.setdefault("root_rule_fn", lambda inv, pids: memmon_owners.fresh_roots(inv, pids, ctx))
    return memmon_act.Engine(source=source, kill=source.kill,
                             partition_fn=lambda inv: memmon_owners.partition(inv, ctx),
                             lock_path=lock_path, **kw)


# ------------------------------------------------------- real processes

SLEEP = "import time; time.sleep(120)"
# Every synthetic process records its own identity, read by itself, or its
# parent records it for a child it just started (lineage to a registered
# identity), so the registry never adopts whatever process happens to hold a
# PID later. The helper reaches grandchildren through the environment.
NOTE_SRC = ("import os, sys\n"
            "sys.path.insert(0, os.environ['MEMMON_TEST_REPO'])\n"
            "import memmon_procs\n"
            "def note_child(tag, pid):\n"
            "    s = memmon_procs.default_source().read(pid).start\n"
            "    with open(sys.argv[1], 'a') as fh:\n"
            "        fh.write(f'{tag} {pid} {s[0]} {s[1]}\\n')\n"
            "def note(tag):\n"
            "    note_child(tag, os.getpid())\n")
NOTE = "import os\nexec(os.environ['MEMMON_NOTE'])\n"


class Registry:
    """The processes a test spawned. Nothing else may be signalled."""

    def __init__(self, testcase):
        self.tc = testcase
        self.src = memmon_procs.default_source()
        assert self.src.name == "libproc", "real-process tests need libproc"
        self.ids = {}            # pid -> start
        self.popens = []
        self.dir = tempfile.mkdtemp(prefix="memmon-test-")
        self.notes = os.path.join(self.dir, "pids.txt")

    def spawn(self, code, *args, **kw):
        env = dict(os.environ, MEMMON_NOTE=NOTE_SRC,
                   MEMMON_TEST_REPO=os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.Popen([PY, "-c", code, self.notes, *args],
                                stdin=subprocess.DEVNULL, env=env, **kw)
        self.popens.append(proc)
        self.register(proc.pid)       # our unreaped child: its PID cannot be reused
        return proc

    def register(self, pid, start=None):
        """Record an identity once. Without `start` the PID must be our own
        unreaped child; anything else brings the start it recorded itself."""
        if pid in self.ids:
            return pid
        if start is None:
            p = self.src.read(pid)
            start = p.start if p is not None else None
        if start:
            self.ids[pid] = tuple(start)
        return pid

    def wait_notes(self, count, timeout=10):
        """Wait until `count` processes have written their tag; register each."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.read_notes()
            if len(rows) >= count:
                for _, pid, start in rows:
                    self.register(pid, start)
                return {tag: pid for tag, pid, _ in rows}
            time.sleep(0.02)
        raise AssertionError(f"only {len(self.read_notes())} of {count} processes started")

    def read_notes(self):
        try:
            with open(self.notes) as fh:
                return [(t, int(p), (int(sec), int(usec)))
                        for t, p, sec, usec in (line.split() for line in fh if line.strip())]
        except FileNotFoundError:
            return []

    def identity(self, pid):
        p = self.src.read(pid)
        return None if p is None or p.zombie else tuple(p.start)

    def alive(self, pid):
        return self.identity(pid) == tuple(self.ids[pid])

    def _covered(self, pid):
        """A registered identity, or a live descendant of one."""
        cur, seen = self.src.read(pid), set()
        while cur is not None and cur.pid not in seen and cur.pid > 1:
            if cur.pid in self.ids and tuple(cur.start) == tuple(self.ids[cur.pid]):
                return True
            seen.add(cur.pid)
            cur = self.src.read(cur.ppid)
        return False

    def guarded_kill(self, pid, sig):
        if not self._covered(pid):
            raise AssertionError(f"engine tried to signal unregistered pid {pid}")
        os.kill(pid, sig)

    def engine(self, ctx, lock_path, **kw):
        kw.setdefault("grace_s", 3.0)
        kw.setdefault("poll_s", 0.05)
        kw.setdefault("force_wait_s", 2.0)
        kw.setdefault("system_reader", memmon_procs.read_system_strict)
        kw.setdefault("root_rule_fn", lambda inv, pids: memmon_owners.fresh_roots(inv, pids, ctx))
        eng = GuardedEngine(source=self.src, kill=self.guarded_kill,
                            partition_fn=lambda inv: memmon_owners.partition(inv, ctx),
                            lock_path=lock_path, **kw)
        eng.registry = self
        return eng

    def token(self, action, root, target, owner_id="claude:test", **extra):
        body = {"v": 1, "action": action, "owner_id": owner_id,
                "owner_root": {"pid": root, "start": list(self.ids[root])},
                "target": {"pid": target, "start": list(self.ids[target])},
                "snapshot_ts": round(time.time(), 3), **extra}
        return memmon_owners.mint_token(body)

    def cleanup(self):
        for pid, start in list(self.ids.items()):
            p = self.src.read(pid)
            if p is not None and not p.zombie and tuple(p.start) == tuple(start):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for proc in self.popens:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        try:
            os.remove(self.notes)
        except OSError:
            pass
        try:
            os.rmdir(self.dir)
        except OSError:
            pass


class GuardedEngine(memmon_act.Engine):
    """The fixture that holds tests to their own processes: every token handed
    to act must name a registered root and target."""

    registry: Registry = None

    def run(self, action, token, lock_fd=None, origin=None, still_selected=None):
        body = memmon_owners.decode_token(token)
        reg = self.registry
        if action == "force":
            for s in body.get("survivors") or []:
                assert (reg.ids.get(s["pid"]) == tuple(s["start"])
                        or reg._covered(s["pid"])), s
        else:
            for key in ("owner_root", "target"):
                who = body.get(key) or {}
                assert reg.ids.get(who.get("pid")) == tuple(who.get("start") or ()), \
                    f"{key} {who} is not a registered identity"
        return super().run(action, token, lock_fd=lock_fd, origin=origin,
                           still_selected=still_selected)

    def reap(self, targets, still_selected, selector=None, dry_run=False):
        for item in targets:
            pid = item[0] if isinstance(item, (tuple, list)) else item
            assert pid in self.registry.ids, f"reap target {pid} is not registered"
        return super().reap(targets, still_selected, selector, dry_run)


# ------------------------------------------------------- S2 telemetry fakes

GiB = 1 << 30
PAGE = 16384
# Instantaneous signals that score each level with no rates needed:
# swap at 1x RAM is +4 (DANGER), plus kernel headroom 10 % is +3 (CRITICAL).
LEVELS = {"HEALTHY": {"free_pct": 80, "swap_ratio": 0.0},
          "WATCH": {"free_pct": 18, "swap_ratio": 0.0},
          "DANGER": {"free_pct": 80, "swap_ratio": 1.0},
          "CRITICAL": {"free_pct": 10, "swap_ratio": 1.0}}


class FakeTelemetry:
    """Scripted kernel inputs for memmon_telemetry. Values come from keyword
    arguments, or from a JSON file re-read on every call so a test can steer
    worker processes. Keys: level (HEALTHY/WATCH/DANGER/CRITICAL), kernel
    (1 normal, 2 warning, 4 critical), memsize, used, swapins, swapouts,
    swapin_rate (pages/s added on every read, against mono), fail
    ("sysctl"|"vm_stat"), hang (seconds vm_stat sleeps)."""

    def __init__(self, path=None, clock=None, **values):
        self.path, self.clock = path, clock
        self.values = values
        self.reads = 0

    def v(self):
        if self.path:
            with open(self.path) as fh:
                return json.load(fh)
        return self.values

    def set(self, **kw):
        if self.path:
            cur = self.v()
            cur.update(kw)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(cur, fh)
            os.replace(tmp, self.path)
        else:
            self.values.update(kw)

    def _lvl(self):
        return LEVELS[self.v().get("level", "HEALTHY")]

    def pressure_level(self):
        if self.v().get("fail") == "sysctl":
            import memmon_telemetry
            raise memmon_telemetry.TelemetryError("injected sysctl failure")
        return self.v().get("kernel", 1)

    def free_pct(self):
        return self.v().get("free_pct", self._lvl()["free_pct"])

    def memsize(self):
        return int(self.v().get("memsize", 48 * GiB))

    def swapusage(self):
        used = int(self._lvl()["swap_ratio"] * self.memsize())
        return 64 * GiB, self.v().get("swap_used", used)

    def loadavg(self):
        return 1.0

    def ncpu(self):
        return 8

    def vm_stat(self, timeout):
        import memmon_telemetry
        v = self.v()
        self.reads += 1
        if v.get("hang"):
            time.sleep(v["hang"])
        if v.get("fail") == "vm_stat":
            raise memmon_telemetry.TelemetryError("injected vm_stat failure")
        used = int(v.get("used", 8 * GiB)) // PAGE
        swapins = int(v.get("swapins", 1000))
        if v.get("swapin_rate"):
            t = (self.clock or memmon_telemetry.SYSTEM_CLOCK).mono()
            swapins += int(v["swapin_rate"] * t)
        lines = [f"Mach Virtual Memory Statistics: (page size of {PAGE} bytes)",
                 f"Anonymous pages: {used}.", "Pages purgeable: 0.",
                 "Pages wired down: 0.", "Pages occupied by compressor: 0.",
                 "Pageins: 10.", "Pageouts: 10.",
                 f"Swapins: {swapins}.", f"Swapouts: {int(v.get('swapouts', 1000))}."]
        return "\n".join(lines) + "\n"


class FakeTelemetryClock(FakeClock):
    """FakeClock with the telemetry module's clocks. machine_sleep() moves
    the sleep-counting clocks without the awake ones, as a wake would."""

    def __init__(self, t=1000.0, boot="boot-a"):
        super().__init__(t)
        self.asleep = 0.0
        self.boot_id = boot

    def awake(self):
        return self.t - self.asleep

    def raw(self):
        return self.t

    def uptime(self):
        return self.t - self.asleep

    def wall(self):
        return 1_700_000_000.0 + self.t

    def boot(self):
        return self.boot_id

    def machine_sleep(self, s):
        self.t += s
        self.asleep += s


def seed_admission(state_dir, source, clock=None, age_s=3.0):
    """A rate baseline age_s old, so the first strict read already has rates.
    Without it every protect test would wait out the 2 s baseline."""
    import memmon_telemetry as tm
    clock = clock or tm.SYSTEM_CLOCK
    reading = tm.read_pressure_strict(source=source, clock=clock)
    reading["mono"] -= age_s
    st = tm.fresh_state(reading["boot"])
    st["rate_baseline"] = tm._counters(reading)
    st["free_baseline"] = tm._counters(reading)
    path = os.path.join(state_dir, "runner", "coord", "admission-state.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(st, fh)
    return st
