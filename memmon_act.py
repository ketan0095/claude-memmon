"""Identity-checked stops. The only code in memmon that signals processes
memmon did not start; the runner signals only its own child's group, and the
menu bar asks apps to quit through NSRunningApplication after verify-app.

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
survivors that were signalled, and a separate explicit call. Protection is
re-derived at every signal decision, never trusted from an earlier snapshot.
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
OBSERVED_CAP = 64

RESPAWN_WINDOW_S = 20.0
RESPAWN_POLL_S = 0.5
ACT_BUDGET_S = 22.0

# respawned is "needs attention": the stop worked but the daemon restarted it.
EXIT_CODES = {"stopped": 0, "force_stopped": 0, "already_exited": 0,
              "verified": 0, "would_stop": 0, "partial": 3, "respawned": 3,
              "refused": 4, "error": 1}
PROCESS_ACTIONS = ("stop-job", "stop-server", "stop-managed-job", "end-session")
TOKEN_ACTION = {"verify-app": "quit-app", "force": "force"}
ENDABLE_KINDS = memmon_owners.ENDABLE_KINDS


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
    root_rule_fn: object = None                 # (inventory, pids) -> {pid: owner_id}
    respawn_window_s: float = RESPAWN_WINDOW_S
    after_capture: object = None                # test hook: (captured pids) -> None
    sent: list = field(default_factory=list)    # (pid, signal) actually sent
    _pending_watch: object = None
    _still_selected: object = None
    _first_term: float = 0.0

    # ------------------------------------------------------------ signals

    def _signal(self, pid: int, start, sig: int) -> bool:
        """Re-read this PID's identity and signal it only if unchanged."""
        if not _same(self.source.read(pid), start):
            return False
        try:
            self.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
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
            self._pending_watch = None
            with action_lock(self.lock_path, lock_fd, sleep=self.sleep, mono=self.mono):
                if action == "verify-app":
                    return self._verify_app(body)
                if action == "force":
                    return self._force(body, origin)
                if action not in PROCESS_ACTIONS:
                    raise Refused("unknown_action")
                out = self._process(action, body)
            # The respawn watch only reads, so it runs after the lock is
            # released: holding it would refuse other stops as busy for 20 s.
            if self._pending_watch:
                self._watch_respawn(out, *self._pending_watch)
            return out
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
            return self._target_gone(inv, action, body)
        if tp.start is None or list(tp.start) != list(target.get("start") or []):
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
        own_root = {"pid": rp.pid, "start": list(rp.start)}
        job_id = (part.owners[owner_id].info.get("job_id")
                  if owner_id and root_kind == "claude" else None)
        watch = self.respawn if action == "end-session" and job_id else None
        base = watch.baseline(job_id) if watch else None
        out = self._graceful(inv, part, [tp.pid], owner_id, action, body, mine,
                             owner_root=own_root)
        if watch and out["result"] == "stopped":
            self._pending_watch = (watch, job_id, (rp.pid, *rp.start), base)
        return out

    def _target_gone(self, inv, action: str, body: dict) -> dict:
        """The job's target exited before the action ran. Its reparented
        children may still run in its process group: report them (never
        signal them) instead of claiming the job is gone."""
        pgid = body.get("target_pgid")
        root = inv.procs.get((body.get("owner_root") or {}).get("pid"))
        if action not in ("stop-job", "stop-server") or not pgid or (
                root is not None and root.pgid == pgid):
            return outcome("already_exited", None, measured_at=self.clock())
        part = self.partition_fn(inv)
        mine = self._self_tree()
        rows = [{"pid": p.pid, "start": list(p.start), "forceable": False,
                 "argv0": os.path.basename((inv.argv(p.pid) or [p.comm])[0] or p.comm)}
                for p in sorted(inv.procs.values(), key=lambda q: q.pid)
                if p.pgid == pgid and p.visible and not p.zombie and p.uid == os.getuid()
                and p.pid not in mine
                and part.owners[part.owner_of[p.pid]].kind == "unknown"]
        if not rows:
            return outcome("already_exited", None, measured_at=self.clock())
        return outcome("partial", "root_exited", remaining=rows, forceable=0,
                       observed=len(rows), measured_at=self.clock())

    def _watch_respawn(self, out: dict, watch, job_id: str, old: tuple, base: dict):
        """Keep reading (never signalling) until 20 s after the first SIGTERM,
        or until the job settles, to report a daemon restart truthfully. The
        whole call stays under 22 s from this process's start, inside the
        menu bar's 25 s timeout."""
        deadline = self._first_term + self.respawn_window_s
        me = self.source.read(os.getpid())
        if me is not None and me.start:
            started = me.start[0] + me.start[1] / 1e6
            deadline = min(deadline, self.mono() + (ACT_BUDGET_S - (self.clock() - started)))
        try:
            self._watch_loop(out, watch, job_id, old, base, deadline)
        except Exception as exc:
            out["watch_error"] = f"{type(exc).__name__}: {exc}"

    def _watch_loop(self, out, watch, job_id, old, base, deadline):
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

    def _fresh_roots(self, inv, pids) -> dict:
        if not self.root_rule_fn or not pids:
            return {}
        return self.root_rule_fn(inv, list(dict.fromkeys(pids)))

    def _guarding_wrappers(self, inv, part, captured: dict, kept: dict) -> set:
        """`memmon run` wrappers that must not get SIGTERM: on cancellation a
        wrapper signals its child's whole process group, and that group holds
        a kept owner. Their child is still stopped per PID, after which the
        wrapper exits on its own."""
        if not self.leases_fn or not kept:
            return set()
        kept_ids = set(kept)
        out = set()
        for row in self.leases_fn() or []:
            w, c = row.get("wrapper_pid"), row.get("child_pid")
            cp = inv.procs.get(c)
            if w not in captured or cp is None or list(cp.start) != list(row.get("child_start") or []):
                continue
            if any(q.pgid == cp.pgid and (part.root_owner.get(pid) in kept_ids
                                         or part.owner_of.get(pid) in kept_ids)
                   for pid, q in inv.procs.items()):
                out.add(w)
        return out

    def _graceful(self, inv, part, targets: list, owner_id, origin: str,
                  body: dict | None, mine: set, owner_root=None, selected=()) -> dict:
        """TERM every observed member, watch for new descendants, then report
        exactly what is still alive. Never escalates on its own.

        Protection is re-derived as the tree changes: a parent is walked only
        while its identity is unchanged, and a process that turns out to be
        another owner's root (found by the root rules, applied only to newly
        seen PIDs) is kept with its subtree."""
        stop = set(part.root_owner) - set(targets)
        kept: dict = {}                     # owner_id -> (pid, start)
        excluded: set = set()               # never signalled, never walked
        captured: dict = {}
        depth: dict = {}
        for t in targets:
            for pid in [t] + inv.descendants(t, stop=stop):
                p = inv.procs.get(pid)
                if pid in mine or p is None or p.zombie or not p.visible:
                    continue
                captured[pid] = p.start
                depth[pid] = _depth(inv, pid, t)
            for r in inv.descendants(t):
                if r in stop and r in part.root_owner:
                    kept.setdefault(part.root_owner[r], (r, inv.procs[r].start))
        for w in self._guarding_wrappers(inv, part, captured, kept):
            excluded.add(w)
            captured.pop(w, None)
        pgids = {inv.procs[t].pgid for t in targets}
        if self.after_capture:
            self.after_capture(list(captured))
        used_before = self._used()
        self._first_term = self.mono()
        for pid in sorted(captured, key=lambda q: -depth[q]):
            self._signal(pid, captured[pid], signal.SIGTERM)

        def discover(snap, parents) -> list:
            """Uncaptured descendants of `parents` that are not another
            owner's root; such roots and their subtrees join `excluded`."""
            cand = []
            for a in parents:
                if not _same(snap.procs.get(a), captured[a]):
                    continue                # gone or reused: not our parent any more
                for d in snap.descendants(a, stop=stop | excluded):
                    dp = snap.procs[d]
                    if (_same(dp, captured.get(d, ())) or d in mine or dp.zombie
                            or not dp.visible or dp.uid != os.getuid()):
                        continue
                    cand.append(d)
            for r, oid in self._fresh_roots(snap, cand).items():
                kept.setdefault(oid, (r, snap.procs[r].start))
                excluded.add(r)
                excluded.update(snap.descendants(r))
            return [d for d in dict.fromkeys(cand) if d not in excluded]

        deadline = self.mono() + self.grace_s
        while True:
            alive = [p for p in captured if self._alive(p, captured[p])]
            fresh = []
            if alive:
                now_inv = memmon_procs.snapshot(self.source, clock=self.clock)
                fresh = discover(now_inv, alive)
                for d in fresh:
                    captured[d] = now_inv.procs[d].start
                    self._signal(d, captured[d], signal.SIGTERM)
            if not alive and not fresh:
                break
            if self.mono() >= deadline:
                break
            self.sleep(self.poll_s)

        final = memmon_procs.snapshot(self.source, clock=self.clock)
        survivors = [p for p in captured if _same(final.procs.get(p), captured[p])]
        remaining = {p: final.procs[p] for p in survivors}
        for d in discover(final, survivors):
            remaining.setdefault(d, final.procs[d])
        # The sweep reports, never signals. It skips only what this action
        # deliberately leaves alone: kept owners, guarding wrappers and the
        # targets' own ancestors. Anything else still in the group is a known
        # survivor.
        ancestors = set()
        for t in targets:
            a = inv.procs[t].ppid
            while a in inv.procs and a not in ancestors:
                ancestors.add(a)
                a = inv.procs[a].ppid
        for pid, p in final.procs.items():
            if (p.pgid in pgids and not _same(p, captured.get(pid, ())) and pid not in mine
                    and pid not in ancestors and pid not in excluded and not p.zombie
                    and p.visible and part.owner_of.get(pid) not in kept):
                remaining.setdefault(pid, p)

        forceable = set(survivors)
        exited = sum(1 for p in captured if p not in forceable)
        rows = [{"pid": p.pid, "start": list(p.start), "forceable": p.pid in forceable,
                 "argv0": os.path.basename((final.argv(p.pid) or [p.comm])[0] or p.comm)}
                for p in sorted(remaining.values(), key=lambda q: q.pid)]
        alive_kept = sorted(oid for oid, (pid, start) in kept.items()
                            if _same(final.procs.get(pid), start))
        out = outcome("stopped" if not rows else "partial",
                      None if not rows else "survivors",
                      exited=exited, remaining=rows, kept=alive_kept,
                      used_bytes_before=used_before, used_bytes_after=self._used(),
                      measured_at=self.clock())
        out["captured"] = len(captured)
        out["forceable"] = len(forceable)
        out["observed"] = len(rows) - len(forceable)
        if survivors:
            out["force_token"] = memmon_owners.mint_token({
                "v": 1, "action": "force", "origin": origin, "owner_id": owner_id,
                "owner_root": owner_root,
                "survivors": [{"pid": p, "start": list(captured[p]),
                               "selected": p in selected} for p in survivors],
                "observed": [{"pid": r["pid"], "start": r["start"]}
                             for r in rows if not r["forceable"]][:OBSERVED_CAP],
                "snapshot_ts": round(self.clock(), 3)})
        return out

    # ------------------------------------------------------------- force

    def _force(self, body: dict, origin: str | None = None) -> dict:
        """SIGKILL the survivors a partial result named, each re-read first.
        A survivor that has since become another owner's root, that is in
        memmon's own tree, or (for reap) is no longer an unattributed selected
        orphan, is left alone and reported. Processes the action observed but
        never signalled keep the result partial: force never claims them."""
        if origin and body.get("origin") != origin:
            raise Refused("wrong_action")
        mine = self._self_tree()
        used_before = self._used()
        inv = memmon_procs.snapshot(self.source, clock=self.clock)
        survivors = body.get("survivors") or []
        own_root = (body.get("owner_root") or {}).get("pid")
        roots = self._fresh_roots(inv, [s.get("pid") for s in survivors])
        part = self.partition_fn(inv) if body.get("origin") == "reap" else None
        live, skipped = [], []
        for s in survivors:
            pid, start = s.get("pid"), s.get("start") or []
            p = inv.procs.get(pid)
            if not _same(p, start):
                continue
            protected = (pid == 1 or pid in mine or not p.visible or p.uid != os.getuid()
                         or (pid in roots and pid != own_root))
            if part is not None:
                oid = part.owner_of.get(pid)
                protected = protected or oid is None or part.owners[oid].kind != "unknown"
                if s.get("selected") and self._still_selected:
                    protected = protected or not self._still_selected(inv, pid)
            if protected or not self._signal(pid, start, signal.SIGKILL):
                skipped.append((pid, start))
                continue
            live.append((pid, start))
        deadline = self.mono() + self.force_wait_s
        while live and self.mono() < deadline:
            live = [(p, s) for p, s in live if self._alive(p, s)]
            if live:
                self.sleep(self.poll_s)
        final = memmon_procs.snapshot(self.source, clock=self.clock)
        still = [(p, s) for p, s in live + skipped if _same(final.procs.get(p), s)]
        observed = [(o.get("pid"), o.get("start") or []) for o in body.get("observed") or []
                    if _same(final.procs.get(o.get("pid")), o.get("start") or [])]
        rows = [{"pid": p, "start": list(s), "forceable": False,
                 "argv0": os.path.basename((final.argv(p) or [""])[0])}
                for p, s in still + observed]
        reason = None
        if still:
            reason = "survivors"
        elif observed:
            reason = "outside_force"
        return outcome("force_stopped" if not rows else "partial", reason,
                       exited=len(survivors) - len(still), remaining=rows,
                       named=len(survivors), forceable=0, observed=len(rows),
                       used_bytes_before=used_before, used_bytes_after=self._used(),
                       measured_at=self.clock())

    def force(self, token: str, origin: str | None = None, still_selected=None) -> dict:
        """The explicit second step. Signals only the identities the token
        names, each re-read first, and never anything that appeared later."""
        self._still_selected = still_selected
        try:
            return self.run("force", token, origin=origin)
        finally:
            self._still_selected = None

    # -------------------------------------------------------------- reap

    def reap(self, targets: list, still_selected, selector: str | None = None,
             dry_run: bool = False) -> dict:
        """Legacy reap through the same engine. `targets` are (pid, start)
        pairs read when the list was shown; `still_selected(inv, pid)`
        re-applies the orphan or stale-prewarm selector to fresh data. A
        target whose identity changed, that no longer matches, or that has
        become attributed is refused rather than signalled. A dry run makes
        the same decisions and signals nothing."""
        try:
            if self.source.name == "degraded":
                raise Refused("degraded_identity")
            with action_lock(self.lock_path, sleep=self.sleep, mono=self.mono):
                inv = memmon_procs.snapshot(self.source, clock=self.clock)
                part = self.partition_fn(inv)
                mine = self._self_tree()
                chosen, refused = [], []
                for item in targets:
                    pid, start = item if isinstance(item, (tuple, list)) else (item, None)
                    p = inv.procs.get(pid)
                    if p is None or p.zombie:
                        continue
                    owner = part.owner_of.get(pid)
                    if start is not None and (p.start is None or list(p.start) != list(start)):
                        refused.append({"pid": pid, "reason": "target_changed"})
                    elif (pid == 1 or pid in mine or not p.visible
                            or p.uid != os.getuid()):
                        refused.append({"pid": pid, "reason": "protected"})
                    elif owner is None or part.owners[owner].kind != "unknown":
                        refused.append({"pid": pid, "reason": "attributed"})
                    elif not still_selected(inv, pid):
                        refused.append({"pid": pid, "reason": "target_changed"})
                    else:
                        chosen.append(pid)
                if not chosen:
                    out = outcome("refused" if refused else "already_exited",
                                  refused[0]["reason"] if refused else None)
                    out["refused"] = refused
                    return out
                # Targets nested inside another target are covered by it.
                tset = set(chosen)
                top = [t for t in chosen if not _has_ancestor_in(inv, t, tset)]
                if dry_run:
                    stop = set(part.root_owner) - tset
                    would = sorted({d for t in top for d in [t] + inv.descendants(t, stop=stop)
                                    if d not in mine and inv.procs[d].visible
                                    and not inv.procs[d].zombie})
                    out = outcome("would_stop", None, refused=refused)
                    out["would_signal"] = [{"pid": d, "start": list(inv.procs[d].start),
                                            "argv0": os.path.basename(
                                                (inv.argv(d) or [inv.procs[d].comm])[0])}
                                           for d in would]
                    return out
                out = self._graceful(inv, part, top, None, "reap", None, mine,
                                     selected=tset)
                if out.get("force_token") and selector:
                    body = memmon_owners.decode_token(out["force_token"])
                    out["force_token"] = memmon_owners.mint_token({**body, "selector": selector})
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
        part = self.partition_fn(inv)
        hosting = {p for o in part.owners.values() if o.kind in memmon_owners.HOSTED_KINDS
                   for p in _ancestors(inv, o.root)}
        rows, running = [], 0
        for inst in body.get("instances") or []:
            p = inv.procs.get(inst.get("pid"))
            if p is None or p.zombie:
                rows.append({**inst, "status": "exited"})
                continue
            if p.start is None or list(p.start) != list(inst.get("start") or []):
                raise Refused("instance_changed")
            if p.pid in mine or not p.visible or p.uid != os.getuid():
                raise Refused("protected")
            if p.pid in hosting:
                raise Refused("hosts_sessions")
            bundle = memmon_owners._bundle_of(inv, p.pid)
            bid = memmon_owners.bundle_info(bundle)["bundle_id"] if bundle else None
            if bid != body.get("bundle_id") or not memmon_owners.is_app_instance(inv, p.pid):
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


def _ancestors(inv, pid: int) -> set:
    out, cur = set(), inv.procs[pid].ppid if pid in inv.procs else None
    while cur in inv.procs and cur not in out:
        out.add(cur)
        cur = inv.procs[cur].ppid
    return out


def _has_ancestor_in(inv, pid: int, pids: set) -> bool:
    cur, seen = inv.procs[pid].ppid, set()
    while cur in inv.procs and cur not in seen:
        if cur in pids:
            return True
        seen.add(cur)
        cur = inv.procs[cur].ppid
    return False
