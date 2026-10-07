"""Identity-checked stops. The only code in memmon that sends a signal.

macOS has no pidfd, so a PID is only a number until its start time is read.
Every signal here goes to one PID, immediately after re-reading that PID's
(pid, start_sec, start_usec) and finding it unchanged. There is no killpg:
a process group can hold processes nobody observed, and setsid() children
leave it anyway.

What remains is a residual race, stated rather than hidden: a PID that exits
and is reused between the re-read and kill() is signalled regardless, since
the kernel takes only the number. The window is short but unbounded under
scheduling.

Graceful first. SIGKILL needs a partial result, a force token naming the
observed survivors, and a separate explicit call.
"""

from __future__ import annotations

import fcntl
import os
import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import memmon_owners
import memmon_procs

TOKEN_TTL_S = memmon_owners.TOKEN_TTL_S
LOCK_WAIT_S = 5.0
GRACE_S = 10.0
POLL_S = 0.2
FORCE_WAIT_S = 3.0
FUTURE_SLACK_S = 5.0

RESPAWN_WINDOW_S = 20.0
RESPAWN_POLL_S = 0.5

# respawned is "needs attention": the stop worked but the daemon restarted it.
EXIT_CODES = {"stopped": 0, "force_stopped": 0, "already_exited": 0,
              "verified": 0, "partial": 3, "respawned": 3, "refused": 4,
              "error": 1}
PROCESS_ACTIONS = ("stop-job", "stop-server", "stop-managed-job", "end-session")
TOKEN_ACTION = {"verify-app": "quit-app", "force": "force"}
ENDABLE_KINDS = ("claude", "codex")


class Refused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def outcome(result: str, reason: str | None = None, **extra) -> dict:
    out = {"result": result, "reason": reason, "exited": 0, "remaining": [],
           "kept": [], "used_bytes_before": None, "used_bytes_after": None,
           "measured_at": None}
    out.update(extra)
    return out


def exit_code(out: dict) -> int:
    return EXIT_CODES.get(out.get("result"), 1)


@contextmanager
def action_lock(path: str, lock_fd: int | None = None, wait_s: float = LOCK_WAIT_S,
                sleep=time.sleep, mono=time.monotonic):
    """The one flock that serialises every action. With `lock_fd` the caller
    already holds it on that open file description, so taking it again
    succeeds at once; it is then never unlocked here, because unlocking a
    shared description would release the caller's hold too."""
    own = lock_fd is None
    if own:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    else:
        try:
            a, b = os.fstat(lock_fd), os.stat(path)
        except OSError:
            raise Refused("bad_lock_fd")
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise Refused("bad_lock_fd")
        fd = lock_fd
    deadline = mono() + wait_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if mono() >= deadline:
                    raise Refused("busy")
                sleep(0.05)
        yield fd
    finally:
        if own:
            os.close(fd)


def _same(proc, start) -> bool:
    return (proc is not None and not proc.zombie and proc.start is not None
            and list(proc.start) == list(start))


@dataclass
class Engine:
    source: object
    partition_fn: object                        # inventory -> Partition
    lock_path: str
    system_reader: object = None                # () -> {"used_bytes": …}
    leases_fn: object = None                    # () -> live runner rows
    clock: object = time.time
    mono: object = time.monotonic
    sleep: object = time.sleep
    kill: object = os.kill
    grace_s: float = GRACE_S
    poll_s: float = POLL_S
    force_wait_s: float = FORCE_WAIT_S
    respawn: object = None                      # memmon_owners.RespawnWatch
    respawn_window_s: float = RESPAWN_WINDOW_S
    after_capture: object = None                # test hook: (captured pids) -> None
    sent: list = field(default_factory=list)    # (pid, signal) actually sent

    # ------------------------------------------------------------ signals

    def _signal(self, pid: int, start, sig: int) -> bool:
        """Re-read this PID's identity and signal it only if unchanged."""
        if not _same(self.source.read(pid), start):
            return False
        try:
            self.kill(pid, sig)
        except ProcessLookupError:
            return False
        self.sent.append((pid, sig))
        return True

    def _alive(self, pid: int, start) -> bool:
        return _same(self.source.read(pid), start)

    def _self_tree(self) -> set:
        """memmon itself and every ancestor: whoever asked must survive."""
        out, pid = set(), os.getpid()
        while pid and pid not in out:
            out.add(pid)
            p = self.source.read(pid)
            pid = p.ppid if p is not None and p.ppid != pid else None
        return out

    def _used(self) -> int | None:
        if not self.system_reader:
            return None
        try:
            return self.system_reader()["used_bytes"]
        except Exception:
            return None

    # ------------------------------------------------------------- entry

    def run(self, action: str, token: str, lock_fd: int | None = None,
            origin: str | None = None) -> dict:
        try:
            try:
                body = memmon_owners.decode_token(token)
            except ValueError:
                raise Refused("bad_token")
            if body.get("action") != TOKEN_ACTION.get(action, action):
                raise Refused("wrong_action")
            age = self.clock() - float(body.get("snapshot_ts") or 0)
            if age > TOKEN_TTL_S or age < -FUTURE_SLACK_S:
                raise Refused("stale_token")
            if self.source.name == "degraded":
                raise Refused("degraded_identity")
            with action_lock(self.lock_path, lock_fd, sleep=self.sleep, mono=self.mono):
                if action == "verify-app":
                    return self._verify_app(body)
                if action == "force":
                    return self._force(body, origin)
                if action in PROCESS_ACTIONS:
                    return self._process(action, body)
                raise Refused("unknown_action")
        except Refused as r:
            return outcome("refused", r.reason)
        except Exception as exc:
            return outcome("error", f"{type(exc).__name__}: {exc}")

    # -------------------------------------------------- process actions

    def _process(self, action: str, body: dict) -> dict:
        target, root = body.get("target") or {}, body.get("owner_root") or {}
        inv = memmon_procs.snapshot(self.source, clock=self.clock)
        tp = inv.procs.get(target.get("pid"))
        if tp is None or tp.zombie:
            return outcome("already_exited", None,
                           measured_at=self.clock())
        if list(tp.start) != list(target.get("start") or []):
            raise Refused("target_changed")
        rp = inv.procs.get(root.get("pid"))
        if not _same(rp, root.get("start") or []):
            raise Refused("ownership_changed")
        cur, hops = tp, 0
        while cur.pid != rp.pid:
            cur = inv.procs.get(cur.ppid)
            hops += 1
            if cur is None or cur.zombie or hops > 4096:
                raise Refused("ownership_changed")

        part = self.partition_fn(inv)
        mine = self._self_tree()
        if tp.pid == 1 or not tp.visible or tp.uid != os.getuid() or tp.pid in mine:
            raise Refused("protected")
        root_ancestors, a = set(), rp.ppid
        while a in inv.procs and a not in root_ancestors:
            root_ancestors.add(a)
            a = inv.procs[a].ppid
        if tp.pid in root_ancestors:
            raise Refused("protected")
        root_kind = part.owners[part.root_owner[rp.pid]].kind \
            if rp.pid in part.root_owner else None
        if action in ("stop-job", "stop-server"):
            if tp.pid == rp.pid or tp.pgid == rp.pgid or tp.pid in part.root_owner:
                raise Refused("protected")
        elif action == "end-session":
            if tp.pid != rp.pid:
                raise Refused("protected")
            if root_kind not in ENDABLE_KINDS:
                raise Refused("not_stoppable")
        elif action == "stop-managed-job":
            live = [r for r in (self.leases_fn() if self.leases_fn else [])
                    if r.get("id") == body.get("run_id")]
            if (not live or live[0].get("child_pid") != tp.pid
                    or list(live[0].get("child_start") or []) != list(tp.start)):
                raise Refused("lease_mismatch")
            standalone = tp.pid == rp.pid and root_kind == "job"
            if not standalone and (tp.pid == rp.pid or tp.pgid == rp.pgid
                                   or tp.pid in part.root_owner):
                raise Refused("protected")

        owner_id = part.root_owner.get(rp.pid)
        job_id = (part.owners[owner_id].info.get("job_id")
                  if owner_id and root_kind == "claude" else None)
        watch = self.respawn if action == "end-session" and job_id else None
        base = watch.baseline(job_id) if watch else None
        out = self._graceful(inv, part, [tp.pid], owner_id, action, body, mine)
        if watch and out["result"] == "stopped":
            self._watch_respawn(out, watch, job_id, (rp.pid, *rp.start), base)
        return out

    def _watch_respawn(self, out: dict, watch, job_id: str, old: tuple, base: dict):
        """Keep reading (never signalling) until 20 s after the first SIGTERM,
        or until the job settles, to report a daemon restart truthfully."""
        deadline = self._first_term + self.respawn_window_s
        while True:
            inv = memmon_procs.snapshot(self.source, clock=self.clock)
            state, who = watch.status(inv, job_id, old, base)
            if state == "respawned":
                out.update(result="respawned", reason="daemon_restarted_worker",
                           respawned_as=who, measured_at=self.clock())
                out.pop("force_token", None)
                return
            if state == "settled" or self.mono() >= deadline:
                return
            self.sleep(RESPAWN_POLL_S)

    def _graceful(self, inv, part, targets: list, owner_id, origin: str,
                  body: dict | None, mine: set) -> dict:
        """TERM every observed member, watch for new descendants, then report
        exactly what is still alive. Never escalates on its own."""
        stop = set(part.root_owner) - set(targets)
        captured: dict = {}
        depth: dict = {}
        for t in targets:
            for pid in [t] + inv.descendants(t, stop=stop):
                p = inv.procs.get(pid)
                if pid in mine or p is None or p.zombie or not p.visible:
                    continue
                captured[pid] = p.start
                depth[pid] = _depth(inv, pid, t)
        kept = sorted({part.root_owner[r] for t in targets
                       for r in inv.descendants(t) if r in stop and r in part.root_owner})
        pgids = {inv.procs[t].pgid for t in targets}
        if self.after_capture:
            self.after_capture(list(captured))
        used_before = self._used()
        self._first_term = self.mono()
        for pid in sorted(captured, key=lambda q: -depth[q]):
            self._signal(pid, captured[pid], signal.SIGTERM)

        deadline = self.mono() + self.grace_s
        while True:
            alive = [p for p in captured if self._alive(p, captured[p])]
            fresh = []
            if alive:
                now_inv = memmon_procs.snapshot(self.source, clock=self.clock)
                for a in alive:
                    for d in now_inv.descendants(a, stop=stop):
                        dp = now_inv.procs[d]
                        if (d in captured or d in mine or dp.zombie or not dp.visible
                                or dp.uid != os.getuid()):
                            continue
                        captured[d] = dp.start
                        fresh.append(d)
                for d in fresh:
                    self._signal(d, captured[d], signal.SIGTERM)
            if not alive and not fresh:
                break
            if self.mono() >= deadline:
                break
            self.sleep(self.poll_s)

        final = memmon_procs.snapshot(self.source, clock=self.clock)
        survivors = [p for p in captured if _same(final.procs.get(p), captured[p])]
        remaining = {p: final.procs[p] for p in survivors}
        for s in survivors:
            for d in final.descendants(s, stop=stop):
                dp = final.procs[d]
                if not _same(dp, captured.get(d, ())) and d not in mine and not dp.zombie:
                    remaining.setdefault(d, dp)
        # The sweep reports, never signals. It skips only what this action
        # deliberately leaves alone: kept nested owners and the targets' own
        # ancestors. Anything else still in the group is a known survivor.
        ancestors = set()
        for t in targets:
            a = inv.procs[t].ppid
            while a in inv.procs and a not in ancestors:
                ancestors.add(a)
                a = inv.procs[a].ppid
        kept_set = set(kept)
        for pid, p in final.procs.items():
            if (p.pgid in pgids and not _same(p, captured.get(pid, ())) and pid not in mine
                    and pid not in ancestors and not p.zombie and p.visible
                    and part.owner_of.get(pid) not in kept_set):
                remaining.setdefault(pid, p)

        exited = sum(1 for p in captured if p not in survivors)
        rows = [{"pid": p.pid, "start": list(p.start),
                 "argv0": os.path.basename((final.argv(p.pid) or [p.comm])[0] or p.comm)}
                for p in sorted(remaining.values(), key=lambda q: q.pid)]
        out = outcome("stopped" if not rows else "partial",
                      None if not rows else "survivors",
                      exited=exited, remaining=rows, kept=kept,
                      used_bytes_before=used_before, used_bytes_after=self._used(),
                      measured_at=self.clock())
        out["captured"] = len(captured)
        if survivors:
            out["force_token"] = memmon_owners.mint_token({
                "v": 1, "action": "force", "origin": origin, "owner_id": owner_id,
                "survivors": [{"pid": p, "start": list(captured[p])} for p in survivors],
                "snapshot_ts": round(self.clock(), 3)})
        return out

    # ------------------------------------------------------------- force

    def _force(self, body: dict, origin: str | None = None) -> dict:
        if origin and body.get("origin") != origin:
            raise Refused("wrong_action")
        mine = self._self_tree()
        used_before = self._used()
        live = []
        for s in body.get("survivors") or []:
            pid, start = s.get("pid"), s.get("start") or []
            p = self.source.read(pid)
            if not _same(p, start):
                continue
            if pid == 1 or pid in mine or not p.visible or p.uid != os.getuid():
                continue
            if self._signal(pid, start, signal.SIGKILL):
                live.append((pid, start))
        deadline = self.mono() + self.force_wait_s
        while live and self.mono() < deadline:
            live = [(p, s) for p, s in live if self._alive(p, s)]
            if live:
                self.sleep(self.poll_s)
        final = memmon_procs.snapshot(self.source, clock=self.clock)
        rows = [{"pid": p, "start": list(s),
                 "argv0": os.path.basename((final.argv(p) or [""])[0])}
                for p, s in live]
        n = len(body.get("survivors") or [])
        return outcome("force_stopped" if not rows else "partial",
                       None if not rows else "survivors",
                       exited=n - len(rows), remaining=rows,
                       used_bytes_before=used_before, used_bytes_after=self._used(),
                       measured_at=self.clock())

    def force(self, token: str, origin: str | None = None) -> dict:
        """The explicit second step. Signals only the identities the token
        names, each re-read first, and never anything that appeared later."""
        return self.run("force", token, origin=origin)

    # -------------------------------------------------------------- reap

    def reap(self, pids: list, still_selected) -> dict:
        """Legacy reap through the same engine. `still_selected(inv, pid)`
        re-applies the orphan or stale-prewarm selector to fresh data; a
        target that no longer matches, or that has become attributed, is
        refused rather than signalled."""
        try:
            if self.source.name == "degraded":
                raise Refused("degraded_identity")
            with action_lock(self.lock_path, sleep=self.sleep, mono=self.mono):
                inv = memmon_procs.snapshot(self.source, clock=self.clock)
                part = self.partition_fn(inv)
                mine = self._self_tree()
                targets, refused = [], []
                for pid in pids:
                    p = inv.procs.get(pid)
                    if p is None or p.zombie:
                        continue
                    owner = part.owner_of.get(pid)
                    if (pid == 1 or pid in mine or not p.visible
                            or p.uid != os.getuid()):
                        refused.append({"pid": pid, "reason": "protected"})
                    elif owner is None or part.owners[owner].kind != "unknown":
                        refused.append({"pid": pid, "reason": "attributed"})
                    elif not still_selected(inv, pid):
                        refused.append({"pid": pid, "reason": "target_changed"})
                    else:
                        targets.append(pid)
                if not targets:
                    out = outcome("refused" if refused else "already_exited",
                                  refused[0]["reason"] if refused else None)
                    out["refused"] = refused
                    return out
                # Targets nested inside another target are covered by it.
                tset = set(targets)
                top = [t for t in targets if not _has_ancestor_in(inv, t, tset)]
                out = self._graceful(inv, part, top, None, "reap", None, mine)
                out["refused"] = refused
                return out
        except Refused as r:
            return outcome("refused", r.reason)
        except Exception as exc:
            return outcome("error", f"{type(exc).__name__}: {exc}")

    # -------------------------------------------------------- verify-app

    def _verify_app(self, body: dict) -> dict:
        inv = memmon_procs.snapshot(self.source, clock=self.clock)
        mine = self._self_tree()
        rows, running = [], 0
        for inst in body.get("instances") or []:
            p = inv.procs.get(inst.get("pid"))
            if p is None or p.zombie:
                rows.append({**inst, "status": "exited"})
                continue
            if list(p.start) != list(inst.get("start") or []):
                raise Refused("instance_changed")
            if p.pid in mine or not p.visible or p.uid != os.getuid():
                raise Refused("protected")
            bundle = memmon_owners._bundle_of(inv, p.pid)
            bid = memmon_owners.bundle_info(bundle)["bundle_id"] if bundle else None
            if bid != body.get("bundle_id"):
                raise Refused("instance_changed")
            launched = p.start[0] + p.start[1] / 1e6
            if abs(launched - float(inst.get("launch_date") or 0)) > 1.0:
                raise Refused("instance_changed")
            running += 1
            rows.append({**inst, "status": "running"})
        if not running:
            return outcome("already_exited", None, instances=rows,
                           measured_at=self.clock())
        return outcome("verified", None, instances=rows,
                       used_bytes_before=self._used(), measured_at=self.clock())


def _depth(inv, pid: int, top: int) -> int:
    d, cur = 0, pid
    while cur != top and cur in inv.procs and d < 4096:
        cur = inv.procs[cur].ppid
        d += 1
    return d


def _has_ancestor_in(inv, pid: int, pids: set) -> bool:
    cur, seen = inv.procs[pid].ppid, set()
    while cur in inv.procs and cur not in seen:
        if cur in pids:
            return True
        seen.add(cur)
        cur = inv.procs[cur].ppid
    return False
