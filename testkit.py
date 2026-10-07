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
                  "CLAUDE_SESSIONS_DIR", "CC_SOCKS_DIR", "CODEX_HOME")


class TempState:
    """Points every memmon path constant at a temp directory."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.saved = {n: getattr(memmon, n) for n in PATH_CONSTANTS}
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
            "CODEX_HOME": f"{r}/codex",
        }
        for n, v in values.items():
            setattr(memmon, n, v)
        os.makedirs(values["CLAUDE_SESSIONS_DIR"])

    def close(self):
        for n, v in self.saved.items():
            setattr(memmon, n, v)
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

    def __init__(self, procs, argv=None, paths=None, cwds=None, ignore_term=()):
        self.table = {p.pid: p for p in procs}
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


def session_file(ctx_dir, pid, start_sec, job_id=None, session_id=None,
                 cwd=None, spare=None):
    """A ~/.claude/sessions/<pid>.json as Claude writes it (procStart in UTC)."""
    d = {"pid": pid, "jobId": job_id, "sessionId": session_id, "cwd": cwd,
         "procStart": time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(start_sec))}
    if spare is not None:
        d["spare"] = spare
    with open(os.path.join(ctx_dir, f"{pid}.json"), "w") as fh:
        json.dump(d, fh)


def fake_engine(source, ctx, lock_path, **kw):
    kw.setdefault("grace_s", 0.5)
    kw.setdefault("poll_s", 0.0)
    kw.setdefault("force_wait_s", 0.2)
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("system_reader", lambda: {"used_bytes": 10 * MB})
    return memmon_act.Engine(source=source, kill=source.kill,
                             partition_fn=lambda inv: memmon_owners.partition(inv, ctx),
                             lock_path=lock_path, **kw)


# ------------------------------------------------------- real processes

SLEEP = "import time; time.sleep(120)"
NOTE = ("import os,sys\n"
        "def note(tag):\n"
        "    with open(sys.argv[1], 'a') as fh: fh.write(f'{tag} {os.getpid()}\\n')\n")


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
        proc = subprocess.Popen([PY, "-c", code, self.notes, *args],
                                stdin=subprocess.DEVNULL, **kw)
        self.popens.append(proc)
        self.register(proc.pid)
        return proc

    def register(self, pid):
        p = self.src.read(pid)
        if p is not None and p.start:
            self.ids[pid] = p.start
        return pid

    def wait_notes(self, count, timeout=10):
        """Wait until `count` processes have written their tag; register each."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = self.read_notes()
            if len(rows) >= count:
                for _, pid in rows:
                    self.register(pid)
                return {tag: pid for tag, pid in rows}
            time.sleep(0.02)
        raise AssertionError(f"only {len(self.read_notes())} of {count} processes started")

    def read_notes(self):
        try:
            with open(self.notes) as fh:
                return [(t, int(p)) for t, p in (line.split() for line in fh if line.strip())]
        except FileNotFoundError:
            return []

    def identity(self, pid):
        p = self.src.read(pid)
        return None if p is None or p.zombie else tuple(p.start)

    def alive(self, pid):
        return self.identity(pid) == tuple(self.ids[pid])

    def _covered(self, pid):
        if pid in self.ids:
            return True
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

    def run(self, action, token, lock_fd=None, origin=None):
        body = memmon_owners.decode_token(token)
        if action == "force":
            for s in body.get("survivors") or []:
                assert self.registry._covered(s["pid"]), s
        else:
            for key in ("owner_root", "target"):
                pid = (body.get(key) or {}).get("pid")
                assert pid in self.registry.ids, f"{key} {pid} is not a registered pid"
        return super().run(action, token, lock_fd=lock_fd, origin=origin)

    def reap(self, pids, still_selected):
        for pid in pids:
            assert pid in self.registry.ids, f"reap target {pid} is not registered"
        return super().reap(pids, still_selected)
