"""Unmanaged heavy work under pressure (spec S2.11).

The gate checks a heavy command once, when it launches, and the runner only
watches jobs started through `memmon run`. Everything else that grows after
launch is found here: heavy job subtrees, never whole owners, listed while the
machine is under pressure with S1's confirmed targeted stops.

Nothing in this module signals a process or calls act. It reads the inventory,
keeps job-history.json (written only by the sampler) and the notification
episode, and returns rows. A row carries an action token only when the caller
asks for one, which only `owners --json` does."""

from __future__ import annotations

import fcntl
import os

import memmon_owners as mo

MIB = 1 << 20
GIB = 1 << 30
HISTORY_MIN_BYTES = 256 * MIB
HISTORY_MAX_ROOTS = 64
HISTORY_MAX_POINTS = 60
GROWTH_WINDOW_S = 600
IDLE_CORES = 0.02
IDLE_MIN_S = 600
SUGGEST_MAX = 3
SUGGEST_MIN_BYTES = GIB
SUGGEST_MIN_RAM_FRAC = 0.02
EPISODE_CALM_S = 600
NOTIFY_FLOOR_S = 300
GAP_S = 150

NO_HISTORY = "not enough history"
ORPHAN_NOTE = "orphaned · stop it where it was started"
GROUP_NOTE = "runs in its session's process group · stop it where it was started"

STRIP_RUNTIMES = {"node", "bun", "deno"}
SCRIPT_EXTS = (".mjs", ".cjs", ".js", ".ts")   # .ts: bun and deno run it directly
# Owners whose whole is never a suggestion and whose members are not walked:
# managed leases are S2.5's, and a service is a VM or daemon as a whole.
SKIP_OWNER_KINDS = {"job", "service"}


def under_pressure(level: str | None, rates, kernel_level: str | None) -> bool:
    """The one trigger shared by the suggestions, the notification episode and
    the gate's naming. A failed read during a crisis must not switch it off,
    so without rates the kernel's own level decides."""
    if level in ("DANGER", "CRITICAL"):
        return True
    return rates == "unavailable" and kernel_level in ("warning", "critical")


# ----------------------------------------------------------- classification

def heavy_command(inv, pid: int) -> str:
    """The command line S2 classifies. A leading node/bun/deno runtime, its
    options, and the script's directory and .mjs/.cjs/.js extension are
    stripped, so `node --max-old-space-size=8192 …/tsc -b` reads as `tsc -b`
    and `node …/vitest.mjs run` as `vitest run`. bun and deno are stripped only
    before a script path. Anything else reads as S1's process_command does."""
    argv = inv.argv(pid) or []
    base = os.path.basename(argv[0]).lower() if argv else ""
    if base in STRIP_RUNTIMES:
        i = 1
        while i < len(argv) and argv[i].startswith("-") and argv[i] not in mo.RUNTIME_CODE_FLAGS:
            i += 2 if argv[i] in mo.RUNTIME_VALUE_FLAGS else 1
        # bun and deno take subcommands (`bun test`, `bun run build`, `deno
        # task dev`): only a script path is stripped, so a launcher keeps its
        # verb for classify_job's launcher branch.
        is_script = i < len(argv) and (base == "node" or "/" in argv[i]
                                       or argv[i].endswith(SCRIPT_EXTS))
        if is_script and not argv[i].startswith("-"):
            script = os.path.basename(argv[i])
            for ext in SCRIPT_EXTS:
                if script.endswith(ext):
                    script = script[:-len(ext)]
                    break
            return " ".join([script] + argv[i + 1:])
        if base != "node":
            return " ".join([base] + argv[1:])
    return mo.process_command(inv, pid)


class Classifier:
    """classify_job over heavy_command, cached per command line."""

    def __init__(self, classify, commands=None):
        self.classify, self.commands, self.cache = classify, commands, {}

    def __call__(self, inv, pid: int) -> tuple:
        cmd = heavy_command(inv, pid)
        if cmd not in self.cache:
            self.cache[cmd] = mo.classify_job(cmd, self.classify, self.commands)
        return self.cache[cmd]


def heavy_pids(inv, kind_of, pids=None) -> dict:
    """{pid: (kind, label)} for this user's live processes that classify as
    test, build or server."""
    uid = os.getuid()
    out = {}
    for pid in (inv.procs if pids is None else pids):
        p = inv.procs.get(pid)
        if (p is None or not p.visible or p.zombie or p.uid != uid
                or p.comm.lower() not in mo.CANDIDATE_COMMS):
            continue
        kind, label = kind_of(inv, pid)
        if kind in mo.HEAVY_KINDS:
            out[pid] = (kind, label)
    return out


def _has_heavy_ancestor(inv, pid: int, heavy, scope=None) -> bool:
    cur, seen = inv.procs[pid].ppid, set()
    while cur in inv.procs and cur not in seen and (scope is None or cur in scope):
        if cur in heavy:
            return True
        seen.add(cur)
        cur = inv.procs[cur].ppid
    return False


def heavy_roots(inv, heavy: dict, scope=None) -> list:
    """Heavy processes with no heavy ancestor (inside `scope` when given)."""
    return sorted(pid for pid in heavy if not _has_heavy_ancestor(inv, pid, heavy, scope))


# ---------------------------------------------------------------- history

def _cpu_ns(inv, pids) -> int | None:
    numer, denom = inv.source.timebase()
    ticks = [inv.procs[p].cpu_ticks for p in pids if inv.procs[p].cpu_ticks is not None]
    return int(sum(ticks) * numer / denom) if ticks else None


def update_job_history(hist: dict, inv, heavy: dict, awake_s: float,
                       boot: str | None) -> dict:
    """Append one point per heavy root: [ts, awake_s, summed footprint, summed
    CPU ns] over the root and every descendant, so worker processes that do
    not classify as heavy themselves are counted. Roots under 256 MiB are not
    kept, a root is dropped when it exits, and the file holds at most 64 roots
    of 60 points. Points are spaced on awake time (CLOCK_UPTIME_RAW), so a
    sleep reads neither as idle nor as growth."""
    old = (hist or {}).get("roots") if (hist or {}).get("boot") == boot else None
    old = old or {}
    roots = {}
    for pid in heavy_roots(inv, heavy):
        p = inv.procs[pid]
        key = p.key()
        if key is None:
            continue
        members = [pid] + inv.descendants(pid)
        fp = sum(inv.procs[m].footprint or 0 for m in members)
        if fp < HISTORY_MIN_BYTES:
            continue
        point = [round(inv.ts, 1), round(awake_s, 3), fp, _cpu_ns(inv, members)]
        kind, label = heavy[pid]
        pts = ((old.get(key) or {}).get("points") or []) + [point]
        roots[key] = {"kind": kind, "label": label, "points": pts[-HISTORY_MAX_POINTS:]}
    if len(roots) > HISTORY_MAX_ROOTS:
        keep = sorted(roots, key=lambda k: -roots[k]["points"][-1][2])[:HISTORY_MAX_ROOTS]
        roots = {k: roots[k] for k in keep}
    return {"v": 1, "boot": boot, "roots": roots}


def series_stats(points: list | None) -> dict:
    """growth_mb_min over the last 10 min of awake time, and idle_s: how long
    the job has used under 2 % of one core, reported only once that has
    lasted 10 min (0 otherwise). Fewer than 2 points: both null."""
    points = points or []
    if len(points) < 2:
        return {"growth_mb_min": None, "idle_s": None, "history_reason": NO_HISTORY}
    last = points[-1]
    window = [p for p in points if last[1] - p[1] <= GROWTH_WINDOW_S]
    growth = None
    if len(window) >= 2 and last[1] > window[0][1]:
        growth = round((last[2] - window[0][2]) / MIB / ((last[1] - window[0][1]) / 60.0), 1)
    start = last[1]
    for a, b in zip(reversed(points[:-1]), reversed(points[1:])):
        dt = b[1] - a[1]
        if dt <= 0 or a[3] is None or b[3] is None or b[3] < a[3]:
            break
        if (b[3] - a[3]) / 1e9 / dt >= IDLE_CORES:
            break
        start = a[1]
    idle = last[1] - start
    return {"growth_mb_min": growth, "idle_s": int(idle) if idle >= IDLE_MIN_S else 0,
            "history_reason": None if growth is not None else NO_HISTORY}


# ------------------------------------------------------------ suggestions

def _self_and_ancestors(inv, pid: int) -> set:
    """memmon and every ancestor: whoever asked is never a suggestion. Its own
    descendants are its own owner (S1 partitions memmon as "self")."""
    out, cur = set(), pid
    while cur in inv.procs and cur not in out:
        out.add(cur)
        cur = inv.procs[cur].ppid
    return out


def _managed(inv, leases) -> set:
    out = set()
    for row in leases or []:
        cp = inv.procs.get(row.get("child_pid"))
        if cp is not None and cp.start is not None and \
                list(cp.start) == list(row.get("child_start") or []):
            out.add(cp.pid)
            out.update(inv.descendants(cp.pid))
    return out


def _nearest_root(inv, part, pid: int) -> int | None:
    cur, seen = inv.procs[pid].ppid, set()
    while cur in inv.procs and cur not in seen:
        if cur in part.root_owner:
            return cur
        seen.add(cur)
        cur = inv.procs[cur].ppid
    return None


def _place(inv, pid: int) -> str | None:
    project, worktree = mo.git_place(inv.cwd(pid))
    return worktree or project


def suggestions(inv, part, kind_of, *, leases=None, history=None, ram_bytes=None,
                s1_jobs=None, mint=False, now=None, self_pid=None) -> list:
    """The heavy job subtrees worth a human's attention: at least max(1 GiB,
    2 % of RAM), ranked by footprint, at most three. `s1_jobs` maps a job_id
    to S1's own child-job row, whose kind, action and token are reused; the
    rest get S1-format tokens minted here when `mint` is set."""
    now = inv.ts if now is None else now
    degraded = inv.kind == "degraded"
    excluded = _managed(inv, leases) | _self_and_ancestors(
        inv, os.getpid() if self_pid is None else self_pid)
    floor = max(SUGGEST_MIN_BYTES, SUGGEST_MIN_RAM_FRAC * (ram_bytes or 0))
    owner_roots = set(part.root_owner)
    hist_roots = (history or {}).get("roots") or {}
    rows = []
    for owner in part.owners.values():
        if owner.kind in SKIP_OWNER_KINDS or owner.info.get("self"):
            continue
        mine = set(owner.members) - excluded
        heavy = heavy_pids(inv, kind_of, mine)
        oroot = inv.procs.get(owner.root)
        for pid in heavy_roots(inv, heavy, scope=mine):
            p = inv.procs[pid]
            members = [pid] + [d for d in inv.descendants(pid, stop=owner_roots) if d in mine]
            fp = sum(inv.procs[m].footprint or 0 for m in members)
            if fp < floor:
                continue
            kind, task = heavy[pid]
            job_id = p.key()
            stop, note, token = None, None, None
            s1 = (s1_jobs or {}).get(job_id)
            if pid in owner_roots:
                if owner.kind != "unknown":
                    continue                       # a session or app as a whole
                note = ORPHAN_NOTE
            elif owner.kind in mo.ENDABLE_KINDS and oroot is not None and p.pgid == oroot.pgid:
                continue                           # the Conversation
            elif s1 is not None:
                if s1.get("managed"):
                    continue
                if s1.get("kind") in mo.HEAVY_KINDS:
                    kind = s1["kind"]
                stop, token = s1["action"], s1.get("token")
            else:
                rpid = _nearest_root(inv, part, pid)
                if rpid is None or part.root_owner.get(rpid) != owner.owner_id:
                    continue
                rp = inv.procs[rpid]
                if p.pgid == rp.pgid:
                    note = GROUP_NOTE
                else:
                    stop = "stop-server" if kind == "server" else "stop-job"
                    if mint and not degraded:
                        token = mo.mint_token({
                            "v": 1, "action": stop, "owner_id": owner.owner_id,
                            "owner_root": mo.ident(rp), "target": mo.ident(p),
                            "target_pgid": p.pgid, "snapshot_ts": round(inv.ts, 3)})
            if degraded:
                stop, token = None, None
            place = _place(inv, pid)
            stats = series_stats((hist_roots.get(job_id) or {}).get("points"))
            row = {"job_id": job_id, "owner_id": owner.owner_id, "kind": kind,
                   "label": f"{task} in {place}" if place else task,
                   "task": task, "place": place, "footprint": fp,
                   "growth_mb_min": stats["growth_mb_min"], "idle_s": stats["idle_s"],
                   "history_reason": stats["history_reason"],
                   "age_s": int(max(0, now - (p.start[0] + p.start[1] / 1e6))),
                   "stop": stop, "stop_note": note,
                   "root": {**mo.ident(p), "pgid": p.pgid}}
            if mint:
                row["token"] = token
            rows.append(row)
    rows.sort(key=lambda r: -r["footprint"])
    return rows[:SUGGEST_MAX]


def without_tokens(rows: list) -> list:
    """latest.json and pressure.json never store an action token (I-14)."""
    return [{k: v for k, v in r.items() if k != "token"} for r in rows]


# ---------------------------------------------------- notification episode

def new_episode_state(boot: str | None = None) -> dict:
    return {"v": 1, "boot": boot, "active": False, "episode": 0, "started_ts": None,
            "calm_s": 0.0, "last_calm_mono": None, "notified": {},
            "last_notify_ts": None, "last_notify_mono": None}


def episode_step(state: dict, *, now_ts: float, mono: float, boot: str | None,
                 pressured: bool, unknown: bool, gap: bool, rows: list) -> tuple:
    """One sampler run's effect on the episode. Returns (state, rows to
    notify). An episode starts at the first run where under_pressure holds and
    ends after 10 min of readings where it does not; UNKNOWN readings and
    sampling gaps neither extend nor end it. One notification per job per
    episode, never more than one every 5 min. The floor is measured on
    CLOCK_MONOTONIC_RAW, so a wall-clock change neither stalls nor skips it;
    that clock restarts at boot, so a new boot starts with no floor."""
    st = dict(state or {})
    if st.get("v") != 1 or st.get("boot") != boot:
        st = new_episode_state(boot)
    st["notified"] = dict(st.get("notified") or {})
    if gap:
        st["last_calm_mono"] = None
    out = []
    if pressured:
        if not st["active"]:
            st.update(active=True, episode=st["episode"] + 1, started_ts=now_ts,
                      notified={})
        st["calm_s"], st["last_calm_mono"] = 0.0, None
        fresh = [r for r in rows if r["job_id"] not in st["notified"]]
        last = st.get("last_notify_mono")
        if fresh and (last is None or mono - last >= NOTIFY_FLOOR_S):
            out.append(fresh[0])
            st["notified"][fresh[0]["job_id"]] = mono
            st["last_notify_ts"], st["last_notify_mono"] = now_ts, mono
    elif unknown:
        st["last_calm_mono"] = None
    elif st["active"]:
        prev = st.get("last_calm_mono")
        if prev is not None and 0 < mono - prev <= GAP_S:
            st["calm_s"] += mono - prev
        st["last_calm_mono"] = mono
        if st["calm_s"] >= EPISODE_CALM_S:
            st.update(active=False, notified={}, calm_s=0.0, last_calm_mono=None)
    return st, out


class _EpisodeLock:
    """flock on the sibling .lock file; the state itself is replaced atomically."""

    def __init__(self, path: str):
        self.path = path

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        os.close(self.fd)


def run_episode(path: str, **kw) -> tuple:
    """episode_step under the episode lock. Returns (rows to notify, the
    floor's (last_notify_ts, last_notify_mono) before this step), so a failed
    send can be rolled back."""
    with _EpisodeLock(path):
        state = mo.read_json(path, None)
        before = ((state or {}).get("last_notify_ts"), (state or {}).get("last_notify_mono"))
        state, out = episode_step(state, **kw)
        mo.write_json_atomic(path, state)
        return out, before


def rollback_notification(path: str, job_id: str, mono: float, before: tuple) -> None:
    """Undo one send that failed: the job is fresh again and the floor goes
    back, unless a later run has already moved either on."""
    with _EpisodeLock(path):
        st = mo.read_json(path, None)
        if not st:
            return
        notified = dict(st.get("notified") or {})
        if notified.get(job_id) == mono:
            del notified[job_id]
        st["notified"] = notified
        if st.get("last_notify_mono") == mono:
            st["last_notify_ts"], st["last_notify_mono"] = before
        mo.write_json_atomic(path, st)

