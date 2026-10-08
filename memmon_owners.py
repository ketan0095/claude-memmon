"""Who owns each process: a partition of every visible PID into owners.

Every visible PID belongs to exactly one owner, by the first rule that
matches; when roots nest, the nearest root wins and an outer owner's
membership stops at the inner owner's root. Totals are sums over the
partition only, so nothing is counted twice.

  1 Claude session     ~/.claude/sessions/<pid>.json or a cc-socks socket,
                       each bound to the process start time
  2 Codex exec         a `codex exec` process (thread inferred)
  3 Codex app-server   the shared daemon; threads are listed, never split
  3a Codex frontend    an interactive codex connected to the daemon, with no
                       thread of its own and no children: a pointer row
  4 Managed job        a live runner lease's child, unless 1-3 encloses it
  5 GUI app            an executable inside an Applications/<Name>.app (or its
                       code-signing clone); instances run the main executable
  6 Shared service     a VM or its helpers (Lima, Colima, qemu)
  7 Unattributed       every other top-level subtree

Nothing here signals anything; memmon_act owns that. "Idle" is a label only.
"""

from __future__ import annotations

import base64
import calendar
import json
import os
import re
import time
from dataclasses import dataclass, field

import memmon_procs

HOME = os.path.expanduser("~")
CLAUDE_SESSIONS_DIR = os.path.join(HOME, ".claude", "sessions")
CC_SOCKS_DIR = "/tmp/cc-socks"
CLAUDE_JOBS_DIR = os.path.join(HOME, ".claude", "jobs")
CLAUDE_ROSTER = os.path.join(HOME, ".claude", "daemon", "roster.json")
CODEX_HOME = os.path.join(HOME, ".codex")

TOKEN_TTL_S = 120
WORKING_CORES = 0.2
HISTORY_MAX_SAMPLES = 60
HISTORY_MAX_OWNERS = 200
BASELINE_MAX_PROCS = 4096
MAX_GAP_S = 180
GROWTH_MIN_SAMPLES = 5
GROWTH_MIN_SPAN_S = 600
GROWTH_WINDOW_S = 900
WAKE_SLACK_NS = 2_000_000_000

AGENT_OF = {"claude": "claude", "codex": "codex", "codex-app": "codex",
            "codex-ui": "codex", "job": "job", "app": "app",
            "service": "service", "unknown": "unknown"}
SHELLS = {"sh", "bash", "zsh", "dash", "fish", "ksh", "tcsh", "csh"}
CODEX_NONINTERACTIVE = {"login", "logout", "mcp", "mcp-server", "completion",
                        "apply", "debug", "sandbox", "features", "help",
                        "proto", "cloud"}
APP_BUNDLE_RE = re.compile(r"^(.*?/Applications(?:/[^/]+)?/[^/]+\.app)/")
CLAUDE_RUNTIME = ("bg-pty-host", "bg-spare", "daemon run", "--chrome-native-host")
VM_PATH = "com.apple.Virtualization.VirtualMachine"
ROLLOUT_RE = re.compile(r"rollout-[0-9T:-]+-([0-9a-f]{8}-[0-9a-f-]{27})\.jsonl$")
LOCK_RE = re.compile(r"thread-writer-locks/([0-9a-f]{8}-[0-9a-f-]{27})\.lock$")
CLAIM_SOCK_RE = re.compile(r"(\S+\.claim\.sock)")
EVAL_RE = re.compile(r"\beval '((?:[^']|'\\'')*)'")
OWN_BUNDLE_IDS = {"dev.memmon.bar"}
# Apps whose job is running a container VM are shared services (rule 6) that
# can be quit as apps: quitting them stops the VM and every container in it.
GUI_VM_APPS = {"com.docker.docker": "docker-desktop", "dev.kdrag0n.MacVirt": "orbstack"}
# Terminals and editors run shells, so they may host sessions memmon cannot see
# as their descendants (a root-owned `login` cuts the visible lineage). Quitting
# one is allowed, with a warning.
SHELL_HOSTS = {"com.apple.Terminal", "com.googlecode.iterm2", "com.mitchellh.ghostty",
               "dev.warp.Warp-Stable", "net.kovidgoyal.kitty", "org.alacritty",
               "com.github.wez.wezterm", "com.microsoft.VSCode",
               "com.todesktop.230313mzl4w4u92", "dev.zed.Zed"}
HOSTED_KINDS = ("claude", "codex", "codex-ui")
ENDABLE_KINDS = ("claude", "codex")         # owners an end-session can stop


# ------------------------------------------------------------------ tokens

def mint_token(body: dict) -> str:
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_token(token: str) -> dict:
    """Raises ValueError on anything that is not a v1 token object."""
    try:
        pad = "=" * (-len(token) % 4)
        body = json.loads(base64.urlsafe_b64decode(token + pad))
    except Exception as exc:
        raise ValueError("token is not valid base64 JSON") from exc
    if not isinstance(body, dict) or body.get("v") != 1:
        raise ValueError("unsupported token version")
    return body


def ident(proc) -> dict:
    return {"pid": proc.pid, "start": list(proc.start)}


# ----------------------------------------------------------------- context

@dataclass
class Context:
    """Everything outside the process table, injectable as a whole."""
    # Read when a Context is made, so tests can point the defaults elsewhere.
    sessions_dir: str = field(default_factory=lambda: CLAUDE_SESSIONS_DIR)
    socks_dir: str = field(default_factory=lambda: CC_SOCKS_DIR)
    codex_home: str = field(default_factory=lambda: CODEX_HOME)
    jobs_dir: str = field(default_factory=lambda: CLAUDE_JOBS_DIR)
    roster_path: str = field(default_factory=lambda: CLAUDE_ROSTER)
    titles_by_job: dict = field(default_factory=dict)     # short id -> title
    titles_by_sid: dict = field(default_factory=dict)     # session id -> title
    leases: list = field(default_factory=list)            # runner job rows
    classify: object = None                               # cmd -> classification
    rv_map: object = None                                 # () -> {pid: short}
    lsof: object = None                                   # (args) -> stdout
    commands: object = None                               # cmd -> [executable tokens]
    listening: set | None = None                          # pids with TCP LISTEN


def _utc_ctime(text: str) -> int | None:
    try:
        return calendar.timegm(time.strptime(" ".join(text.split()),
                                             "%a %b %d %H:%M:%S %Y"))
    except (ValueError, TypeError):
        return None


def _same_start(record: dict, proc) -> bool:
    """A Claude sessions record names a process only together with the start
    time it recorded (second resolution, UTC). No parseable procStart, no
    identity: a reused PID must never inherit the record."""
    recorded = _utc_ctime(record.get("procStart") or "")
    return (recorded is not None and proc is not None and proc.start is not None
            and abs(recorded - proc.start[0]) <= 2)


def spare_is_idle(cmd: str) -> bool:
    """An unclaimed prewarm advertises itself on a .claim.sock; claiming it
    removes the socket. A claimed spare is a real session and stays one."""
    m = CLAIM_SOCK_RE.search(cmd)
    return bool(m) and os.path.exists(m.group(1))


def find_claude_roots(inv, ctx: Context) -> dict:
    """pid -> {job_id, session_id, cwd, name} for live Claude workers.

    The sessions file is trusted only when its pid is alive with the start
    time it recorded (second resolution, UTC); a stale file for a reused PID,
    or one without a start time, is ignored. A spare from the prewarm pool is
    not a session until it is claimed."""
    roots: dict = {}
    try:
        names = os.listdir(ctx.sessions_dir)
    except OSError:
        names = []
    for fn in names:
        if not fn.endswith(".json") or not fn[:-5].isdigit():
            continue
        try:
            with open(os.path.join(ctx.sessions_dir, fn)) as fh:
                d = json.load(fh)
        except Exception:
            continue
        pid = int(fn[:-5])
        p = inv.procs.get(pid)
        if p is None or not p.visible or p.zombie or p.start is None:
            continue
        if d.get("spare") is True or not _same_start(d, p):
            continue
        roots[pid] = {"job_id": d.get("jobId") or None,
                      "session_id": d.get("sessionId") or None,
                      "cwd": d.get("cwd") or None, "name": d.get("name") or None}
    try:
        socks = os.listdir(ctx.socks_dir)
    except OSError:
        socks = []
    pending = []
    for fn in socks:
        if not fn.endswith(".sock") or not fn[:-5].isdigit():
            continue
        pid = int(fn[:-5])
        p = inv.procs.get(pid)
        if pid in roots or p is None or not p.visible or p.zombie:
            continue
        cmd = inv.cmdline(pid)
        if "claude" not in cmd or "bg-pty-host" in cmd or spare_is_idle(cmd):
            continue
        # The socket is named by PID only: it must be no older than the
        # process holding that PID, or it belongs to a process that died.
        try:
            st = os.stat(os.path.join(ctx.socks_dir, fn))
        except OSError:
            continue
        born = getattr(st, "st_birthtime", st.st_mtime)
        if p.start is None or born < p.start[0] - 2:
            continue
        roots[pid] = {"job_id": None, "session_id": None, "cwd": None, "name": None}
        pending.append(pid)
    if pending and ctx.rv_map:
        try:
            mapping = ctx.rv_map()
        except Exception:
            mapping = {}
        for pid in pending:
            roots[pid]["job_id"] = mapping.get(pid)
    return roots


class RespawnWatch:
    """Did the Claude daemon restart a worker memmon just ended?

    The daemon revives a worker that dies mid-turn under the same job id, with
    a new pid and start (observed ~12 s after the exit). The primary signal is
    a sessions file for the job naming a live process with another identity;
    the roster's attempt counter or pty-host pid moving is the secondary one.
    The job settling (state done/stopped, or its roster entry removed) ends
    the watch early. Read-only: a revived worker is a new owner, never a
    target."""

    SETTLED = ("done", "stopped", "killed")

    def __init__(self, ctx: Context):
        self.ctx = ctx

    def _roster(self, job_id: str):
        try:
            with open(self.ctx.roster_path) as fh:
                workers = json.load(fh).get("workers") or {}
        except (OSError, ValueError, AttributeError):
            return None
        return workers.get(job_id) or {}

    def baseline(self, job_id: str) -> dict:
        entry = self._roster(job_id)
        return {"roster": entry is not None and bool(entry),
                "attempt": (entry or {}).get("attempt"), "pid": (entry or {}).get("pid")}

    def status(self, inv, job_id: str, old: tuple, base: dict) -> tuple:
        """("respawned", {pid, start} | None), ("settled", None) or ("pending", None)."""
        try:
            names = os.listdir(self.ctx.sessions_dir)
        except OSError:
            names = []
        for fn in names:
            if not fn.endswith(".json") or not fn[:-5].isdigit():
                continue
            try:
                with open(os.path.join(self.ctx.sessions_dir, fn)) as fh:
                    d = json.load(fh)
            except Exception:
                continue
            if d.get("jobId") != job_id or d.get("spare") is True:
                continue
            p = inv.procs.get(int(fn[:-5]))
            if p is None or p.zombie or not p.visible or not _same_start(d, p):
                continue
            if (p.pid, p.start[0], p.start[1]) != tuple(old):
                return "respawned", {"pid": p.pid, "start": list(p.start)}
        entry = self._roster(job_id)
        if entry and base.get("roster") and (
                entry.get("attempt") != base.get("attempt") or entry.get("pid") != base.get("pid")):
            return "respawned", None
        if base.get("roster") and entry is not None and not entry:
            return "settled", None
        try:
            with open(os.path.join(self.ctx.jobs_dir, job_id, "state.json")) as fh:
                state = json.load(fh).get("state")
        except (OSError, ValueError, AttributeError):
            state = None
        if state in self.SETTLED:
            return "settled", None
        return "pending", None


def _codex_role(inv, pid: int) -> str | None:
    p = inv.procs[pid]
    if p.comm != "codex" and os.path.basename(inv.path(pid) or "") != "codex":
        return None
    argv = inv.argv(pid) or []
    rest = _skip_codex_globals(argv[1:])
    if "app-server" in argv[1:]:
        return "app-server"
    if rest[:1] in (["exec"], ["e"]):
        return "exec"
    if rest[:1] and rest[0] in CODEX_NONINTERACTIVE:
        return None
    return "tui"


CODEX_VALUE_FLAGS = {"-c", "--config", "-m", "--model", "-p", "--profile", "-C", "--cd",
                     "-s", "--sandbox", "-a", "--ask-for-approval", "-i", "--image",
                     "--enable", "--disable", "--local-provider", "--remote"}


def _skip_codex_globals(args: list) -> list:
    """The subcommand follows any global flags: `codex -c x mcp-server` is
    the MCP server, not an interactive session."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 1 if "=" in args[i] or args[i] not in CODEX_VALUE_FLAGS else 2
    return args[i:]


HELPER_FRAMEWORK_RE = re.compile(r"^/Contents/Frameworks/[^/]+\.framework/")
# Chrome (and others) re-exec from a private copy of themselves:
# …/<bundle id>.code_sign_clone/<random>/<Name>.app.bundle/Contents/MacOS/<exe>
CLONE_RE = re.compile(r"^(.*/([^/]+)\.code_sign_clone/[^/]+/[^/]+\.app\.bundle)"
                      r"/Contents/MacOS/[^/]+$")


def app_bundle(path: str) -> str | None:
    """The app an executable belongs to: the OUTERMOST <Name>.app under an
    Applications folder, and only for an executable in a Contents/MacOS
    directory (the app's own, or a nested helper app's).

    A nested .app reached through a .framework counts only in the helper
    layout, <App>.app/Contents/Frameworks/<F>.framework/…/<Helper>.app (as
    Chrome ships its helpers). Any other framework-embedded .app is a language
    runtime — Python.app inside Xcode's Python3.framework — and the program
    it runs belongs to whoever started it, not to the outer app.

    A code-signing clone is the app it was cloned from only when its
    directory names the same bundle id its own Info.plist declares."""
    clone = CLONE_RE.match(path or "")
    if clone:
        return clone.group(1) if bundle_info(clone.group(1))["bundle_id"] == clone.group(2) \
            else None
    m = APP_BUNDLE_RE.match(path or "")
    if not m or not os.path.dirname(path).endswith("/Contents/MacOS"):
        return None
    inner = path[len(m.group(1)):]
    fw = re.search(r"/[^/]+\.framework/", inner)
    if fw and ".app/" in inner[fw.end():] and not HELPER_FRAMEWORK_RE.match(inner):
        return None
    return m.group(1)


def _bundle_of(inv, pid: int) -> str | None:
    path = inv.path(pid) or ""
    bundle = app_bundle(path)
    if not bundle:
        return None
    cmd = inv.cmdline(pid)
    if "ClaudeCode.app" in path or any(mk in cmd for mk in CLAUDE_RUNTIME):
        return None
    return bundle


_plist_cache: dict = {}


def _path_hash(path: str) -> str:
    import hashlib
    return hashlib.sha1(path.encode()).hexdigest()[:12]


def bundle_info(bundle: str) -> dict:
    if bundle not in _plist_cache:
        import plistlib
        info = {}
        try:
            with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as fh:
                info = plistlib.load(fh)
        except Exception:
            pass
        base = re.sub(r"\.app(\.bundle)?$", "", os.path.basename(bundle))
        name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or base
        _plist_cache[bundle] = {"bundle_id": info.get("CFBundleIdentifier"),
                                "name": str(name),
                                "executable": str(info.get("CFBundleExecutable") or base)}
    return _plist_cache[bundle]


def app_key(bundle: str) -> str:
    """Apps are one owner per bundle id, wherever their executables live."""
    return bundle_info(bundle)["bundle_id"] or bundle


def is_app_instance(inv, pid: int) -> bool:
    """An instance is a process running the bundle's main executable, the
    thing NSRunningApplication can quit. Helpers, app extensions, XPC
    services and login items are members, never instances."""
    bundle = _bundle_of(inv, pid)
    return bool(bundle) and inv.path(pid) == os.path.join(
        bundle, "Contents", "MacOS", bundle_info(bundle)["executable"])


def _service_name(inv, pid: int) -> str | None:
    p = inv.procs[pid]
    path = inv.path(pid) or ""
    if VM_PATH in path:
        return "virtualization"
    comm = p.comm
    if comm.startswith("qemu-system"):
        return "qemu"
    if comm in ("limactl", "colima"):
        argv = inv.argv(pid) or []
        if "hostagent" in argv:
            for i, a in enumerate(argv):
                if a == "--pidfile" and i + 1 < len(argv):
                    return os.path.basename(os.path.dirname(argv[i + 1])) or "lima"
            return argv[-1] if argv and not argv[-1].startswith("-") else "lima"
        return "lima-helper" if comm == "limactl" else "colima-cli"
    return None


GENERIC_SERVICES = ("virtualization", "qemu", "lima", "lima-helper", "colima-cli")


def stop_command_for(name: str) -> str | None:
    if name == "colima":
        return "colima stop"
    if name.startswith("colima-"):
        return f"colima stop -p {name[len('colima-'):]}"
    if name in GENERIC_SERVICES or name.startswith(("virtualization-", "qemu-")):
        return None
    return f"limactl stop {name}"


@dataclass
class Owner:
    owner_id: str
    kind: str
    confidence: str
    roots: list
    title: str = ""
    members: list = field(default_factory=list)
    info: dict = field(default_factory=dict)

    @property
    def root(self) -> int:
        return self.roots[0]


@dataclass
class Partition:
    owners: dict            # owner_id -> Owner
    owner_of: dict          # pid -> owner_id
    root_owner: dict        # root pid -> owner_id
    hidden: int             # PIDs we cannot read (other users, root)


def partition(inv, ctx: Context) -> Partition:
    procs = {pid: p for pid, p in inv.procs.items() if p.visible and not p.zombie}
    rule: dict = {}             # root pid -> (kind, key, info)

    claude = find_claude_roots(inv, ctx)
    for pid, info in claude.items():
        key = info["job_id"] or info["session_id"]
        rule[pid] = ("claude", key, info)

    roles = {}
    for pid in procs:
        if pid not in rule:
            role = _codex_role(inv, pid)
            if role:
                roles[pid] = role
    handles = codex_handles(roles, ctx)
    servers = [p for p, r in roles.items() if r == "app-server"]
    daemon_addrs = {a for p in servers for a in handles.get(p, {}).get("addrs", ())}
    for pid, role in roles.items():
        h = handles.get(pid, {})
        if role == "exec":
            rule[pid] = ("codex", None, {"thread_id": h.get("thread")})
        elif role == "app-server":
            rule[pid] = ("codex-app", None, {})
        else:
            # A TUI is only a pointer into the daemon with positive evidence:
            # connected to it and holding no thread of its own. Otherwise it
            # hosts its thread in-process and is the thread's owner.
            connected = (bool(set(h.get("peers", ())) & daemon_addrs)
                         or "--remote" in (inv.argv(pid) or []))
            has_kids = any(k in procs for k in inv.children().get(pid, ()))
            if connected and not h.get("thread") and not has_kids:
                rule[pid] = ("codex-ui", None, {})
            else:
                rule[pid] = ("codex", None, {"thread_id": h.get("thread"), "tui": True})
    # The daemon's pid-update loop is an app-server too; only the topmost
    # app-server in a chain is a root, and it lists every thread the chain holds.
    nested_servers = set()
    for pid in servers:
        anc = procs[pid].ppid
        while anc in procs:
            if rule.get(anc, ("",))[0] == "codex-app":
                del rule[pid]
                nested_servers.add(pid)
                break
            anc = procs[anc].ppid
    for pid in servers:
        top = pid
        while top not in rule and procs[top].ppid in procs:
            top = procs[top].ppid
        if rule.get(top, ("",))[0] == "codex-app" and pid in handles:
            threads = rule[top][2].setdefault("threads", [])
            threads.extend(t for t in handles.get(pid, {}).get("threads", ())
                           if t not in threads)

    leases = {}
    for row in ctx.leases or []:
        cpid, cstart = row.get("child_pid"), row.get("child_start")
        p = procs.get(cpid) if cpid else None
        if p is None or not cstart or list(p.start) != list(cstart):
            continue
        anc, enclosed = p.ppid, False
        while anc in procs:
            if rule.get(anc, ("",))[0] in ("claude", "codex", "codex-app", "codex-ui"):
                enclosed = True
                break
            anc = procs[anc].ppid
        leases[cpid] = row
        if not enclosed:
            rule[cpid] = ("job", row.get("id"), {"lease": row})

    # memmon itself is never a member of someone else's owner: a quit-app on
    # the terminal it runs in must not count it as that app's process.
    if os.getpid() in procs and os.getpid() not in rule:
        rule[os.getpid()] = ("unknown", None, {"self": True})

    for pid in procs:
        if pid in rule or pid in nested_servers:   # a nested server is its daemon's
            continue
        bundle = _bundle_of(inv, pid)
        if bundle:
            bid = bundle_info(bundle)["bundle_id"]
            if bid in OWN_BUNDLE_IDS:
                continue
            main = is_app_instance(inv, pid)
            if bid in GUI_VM_APPS:
                rule[pid] = ("service", GUI_VM_APPS[bid], {"bundle": bundle, "main": main})
            else:
                rule[pid] = ("app", app_key(bundle), {"bundle": bundle, "main": main})
            continue
        svc = _service_name(inv, pid)
        if svc:
            rule[pid] = ("service", svc, {})

    # Nearest root wins: walk up until a root, else the top-level ancestor.
    assign: dict = {}
    for pid in procs:
        chain, cur = [], pid
        while True:
            if cur in assign:
                root = assign[cur]
                break
            chain.append(cur)
            if cur in rule:
                root = cur
                break
            parent = procs[cur].ppid
            if parent not in procs or parent == cur:
                root = cur
                rule[cur] = ("unknown", None, {})
                break
            cur = parent
        for c in chain:
            assign[c] = root

    _merge_vm_hosts(procs, rule, assign, inv)

    owners: dict = {}
    root_owner: dict = {}
    for root, (kind, key, info) in sorted(rule.items(), key=lambda kv: procs[kv[0]].start):
        p = procs[root]
        if kind == "claude":
            oid = f"claude:{key}" if key else f"claude:proc.{p.pid}.{p.start[0]}"
            conf = "exact"
        elif kind == "codex":
            thread = info.get("thread_id")
            oid = f"codex:{thread}" if thread else f"codex-proc:{p.pid}.{p.start[0]}"
            conf = "inferred"
        elif kind == "codex-app":
            oid, conf = f"codex-app:{p.pid}.{p.start[0]}", "shared"
        elif kind == "codex-ui":
            oid, conf = f"codex-ui:{p.pid}.{p.start[0]}", "shared"
        elif kind == "job":
            oid, conf = f"job:{key}", "exact"
        elif kind == "app":
            bid = bundle_info(info["bundle"])["bundle_id"]
            oid = "app:" + (bid or _path_hash(key))
            conf = "exact"
        elif kind == "service":
            oid, conf = f"service:vm:{info.get('vm', key)}", "shared"
        else:
            oid, conf = f"unknown:{p.pid}.{p.start[0]}", "unknown"
        if oid in owners and kind not in ("app", "service"):
            oid = f"{oid}.{p.pid}"
        owner = owners.get(oid)
        if owner is None:
            owner = owners[oid] = Owner(oid, kind, conf, [], info=dict(info))
            if kind == "service":
                owner.info["vm"] = info.get("vm", key)
        # The main executable's bundle names the app (a helper may live in
        # the original bundle while the main runs from a signing clone).
        if info.get("bundle") and (info.get("main") or "bundle" not in owner.info):
            owner.info["bundle"] = info["bundle"]
        owner.roots.append(root)
        root_owner[root] = oid
    owner_of = {}
    for pid, root in assign.items():
        oid = root_owner[root]
        owner_of[pid] = oid
        owners[oid].members.append(pid)
    hidden = sum(1 for p in inv.procs.values() if not p.visible)
    return Partition(owners, owner_of, root_owner, hidden)


def hosted_by(part: Partition, inv) -> dict:
    """app owner_id -> owner_ids of the agent sessions running under it.
    Quitting such an app would end those sessions, so it is not offered."""
    out: dict = {}
    for o in part.owners.values():
        if o.kind not in HOSTED_KINDS:
            continue
        a, seen = inv.procs[o.root].ppid, set()
        while a in inv.procs and a not in seen:
            seen.add(a)
            host = part.owners.get(part.owner_of.get(a))
            if host is not None and host.kind == "app":
                out.setdefault(host.owner_id, []).append(o.owner_id)
                break
            a = inv.procs[a].ppid
    return out


def fresh_roots(inv, pids, ctx: Context) -> dict:
    """pid -> owner_id for each of `pids` that is itself an owner root by
    rules 1-3, 5 or 6. A stop checks the processes it discovers mid-action
    with this, so an owner that appears during the grace period is kept, not
    captured. Rule 4 is left out: a `memmon run` started inside the target
    belongs to the target."""
    want = {p for p in pids if p in inv.procs and inv.procs[p].visible}
    out: dict = {}
    if not want:
        return out
    for pid, info in find_claude_roots(inv, ctx).items():
        if pid in want:
            out[pid] = f"claude:{info['job_id'] or info['session_id'] or f'proc.{pid}'}"
    for pid in want - set(out):
        p = inv.procs[pid]
        role = _codex_role(inv, pid)
        bundle = None if role else _bundle_of(inv, pid)
        if role:
            kind = "codex-app" if role == "app-server" else "codex-proc"
            out[pid] = f"{kind}:{pid}.{p.start[0]}"
        elif bundle:
            bid = bundle_info(bundle)["bundle_id"]
            if bid not in OWN_BUNDLE_IDS:
                out[pid] = (f"service:vm:{GUI_VM_APPS[bid]}" if bid in GUI_VM_APPS
                            else "app:" + (bid or _path_hash(bundle)))
        elif _service_name(inv, pid):
            out[pid] = f"service:vm:{_service_name(inv, pid)}"
    return out


def _merge_vm_hosts(procs: dict, rule: dict, assign: dict, inv) -> None:
    """A Virtualization VM process is spawned by launchd, so nothing in the
    tree links it to the Lima instance that started it. Pair each with the
    hostagent that started closest to it (within 60 s); that is a heuristic,
    which is why service owners are 'shared', never 'exact'. A VM joins
    Docker Desktop or OrbStack only with evidence: macOS names a process of
    that app as responsible for it."""
    hosts = [(procs[p].start[0], key) for p, (k, key, _) in rule.items()
             if k == "service" and key not in GENERIC_SERVICES
             and key not in GUI_VM_APPS.values()]
    for pid, (kind, key, info) in list(rule.items()):
        if kind != "service" or key not in ("virtualization", "qemu"):
            continue
        best = min(hosts, key=lambda h: abs(h[0] - procs[pid].start[0]), default=None)
        resp = inv.responsible(pid)
        owner_rule = rule.get(assign.get(resp), ("", None))
        if best and abs(best[0] - procs[pid].start[0]) <= 60:
            rule[pid] = (kind, key, {**info, "vm": best[1]})
        elif owner_rule[0] == "service" and owner_rule[1] in GUI_VM_APPS.values():
            rule[pid] = (kind, key, {**info, "vm": owner_rule[1]})
        else:
            rule[pid] = (kind, key, {**info, "vm": f"{key}-{procs[pid].start[0]}"})


# ----------------------------------------------------------- presentation

def git_place(cwd: str | None) -> tuple:
    """(project, worktree) for a working directory. The project is the main
    repository's name; the worktree is the linked worktree's directory name,
    or None for the main checkout."""
    if not cwd:
        return None, None
    d = cwd.rstrip("/") or "/"
    for _ in range(40):
        dotgit = os.path.join(d, ".git")
        if os.path.isdir(dotgit):
            return os.path.basename(d), None
        if os.path.isfile(dotgit):
            try:
                with open(dotgit) as fh:
                    m = re.match(r"gitdir:\s*(.+)", fh.read().strip())
            except OSError:
                m = None
            if m and "/.git/worktrees/" in m.group(1):
                main = m.group(1).split("/.git/worktrees/")[0]
                return os.path.basename(main), os.path.basename(d)
            return os.path.basename(d), None
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.path.basename(cwd.rstrip("/")) or None, None


def _scrub(text: str) -> str:
    text = re.sub(r"/Users/[^/\s]+/", "~/", text)
    text = re.sub(r"\b[A-Z][A-Z0-9_]*=\S+\s*", "", text)
    text = " ".join(text.split())
    return text[:59] + "…" if len(text) > 60 else text


def job_command(inv, pid: int) -> str:
    """The command a tool shell is running: Claude wraps it in `eval '…'`,
    Codex passes it after -c/-lc; anything else is its own argv."""
    argv = inv.argv(pid) or []
    cmd = " ".join(argv)
    m = EVAL_RE.search(cmd)
    if m:
        return m.group(1).replace("'\\''", "'")
    base = os.path.basename(argv[0]) if argv else ""
    if base in SHELLS:
        for i, a in enumerate(argv[1:], 1):
            if a in ("-c", "-lc", "-ic") and i + 1 < len(argv):
                return argv[i + 1]
    return cmd


LAUNCHERS = {"pnpm", "npm", "yarn", "bun", "turbo"}
DEV_TOOLS = {"next", "vite", "nuxt", "astro", "remix"}
TEST_WORDS = {"test", "vitest", "jest", "playwright", "pytest"}
HEAVY_KINDS = ("build", "test", "server")
RUNTIMES = {"node", "bun", "deno", "python", "python3"}


def _is_server(toks: list) -> bool:
    exe = os.path.basename(toks[0]).lower() if toks else ""
    args = [a.lower() for a in toks[1:]]
    if exe in DEV_TOOLS and "dev" in args[:2]:
        return True
    if exe == "expo" and args[:1] == ["start"]:
        return True
    if exe in ("serve", "http-server"):
        return True
    if exe.startswith("python") and args[:2] == ["-m", "http.server"]:
        return True
    if exe in LAUNCHERS:
        verbs = [a for a in args if not a.startswith("-")]
        if verbs[:1] == ["run"]:
            verbs = verbs[1:]
        return verbs[:1] in (["dev"], ["start"], ["serve"])
    return False


def classify_job(command: str, classify, commands=None) -> tuple:
    """(kind, label) for a command line. Only executables decide: `tail -f
    test.log`, `grep -rn test` and `echo "pnpm dev"` are not test or server
    work. `classify` is the gate's position-aware classifier; `commands`
    splits a line into the token lists of its executables."""
    c = (classify(command) if classify else None) or {}
    shape = c.get("shape") or ""
    segments = commands(command) if commands else [command.split()]
    if any(_is_server(toks) for toks in segments if toks):
        kind = "server"
    elif c.get("matched") and set(shape.lower().split()) & TEST_WORDS:
        kind = "test"
    elif c.get("matched"):
        kind = "build"
    else:
        kind = "other"
    if c.get("matched") and shape:
        label = shape.split()[-1]
    else:
        label = _scrub(command) or "command"
    return kind, label


def process_command(inv, pid: int) -> str:
    """What a process is running, as a command line the classifier can read:
    a shell's -c/eval text, a runtime's script (`node …/vitest run` reads as
    `vitest run`), else its argv."""
    argv = inv.argv(pid) or []
    base = os.path.basename(argv[0]) if argv else ""
    if base in SHELLS:
        return job_command(inv, pid)
    if base.lower() in RUNTIMES and len(argv) > 1 and not argv[1].startswith("-"):
        return " ".join([os.path.basename(argv[1])] + argv[2:])
    return " ".join([base] + argv[1:])


VERB = {"build": "Building", "test": "Testing", "server": "Serving", "other": "Running"}


@dataclass
class Sample:
    """Everything needed to render one owners payload."""
    inv: object
    part: Partition
    cpu: dict                   # pid -> cores
    cpu_reason: str | None      # why CPU is unknown for all, when it is


def _sum(values) -> int | None:
    vals = [v for v in values if v is not None]
    return sum(vals) if vals else None


UNATTRIBUTED = "unattributed"


def owner_footprints(part: Partition, inv) -> dict:
    return {oid: _sum(inv.procs[p].footprint for p in o.members)
            for oid, o in part.owners.items()}


def history_footprints(part: Partition, inv) -> dict:
    """What the sampler records: one series per attributed owner, and a single
    series for everything unattributed (the UI shows those as one row), so
    hundreds of short-lived roots cannot crowd real owners out of the cap."""
    fps = owner_footprints(part, inv)
    out = {oid: fp for oid, fp in fps.items() if part.owners[oid].kind != "unknown"}
    out[UNATTRIBUTED] = _sum(fp for oid, fp in fps.items()
                             if part.owners[oid].kind == "unknown")
    return out


def _cpu(members, cpu: dict) -> tuple:
    measured = [cpu[p] for p in members if p in cpu]
    if not measured:
        return None, None
    return sum(measured), round(len(measured) / max(len(members), 1), 3)


def _titles(owner: Owner, inv, ctx: Context, codex: dict) -> None:
    info = owner.info
    if owner.kind == "claude":
        job, sid = info.get("job_id"), info.get("session_id")
        owner.title = (ctx.titles_by_job.get(job) or ctx.titles_by_sid.get(sid)
                       or info.get("name") or (job or (sid or "")[:8])
                       or f"Claude · {owner.root}")
    elif owner.kind == "codex":
        thread = info.get("thread_id")
        name = codex.get("names", {}).get(thread) if thread else None
        owner.title = name or f"Codex · {(thread or str(owner.root))[:8]}"
    elif owner.kind == "codex-app":
        owner.title = "Codex app" if "/Applications/" in (inv.path(owner.root) or "") \
            else "Codex daemon"
    elif owner.kind == "codex-ui":
        owner.title = "Codex thread · runs in Codex daemon"
    elif owner.kind == "job":
        lease = info.get("lease") or {}
        owner.title = str(lease.get("label") or "Managed job")
    elif owner.kind == "app":
        owner.title = bundle_info(info["bundle"])["name"]
    elif owner.kind == "service" and info.get("bundle"):
        owner.title = bundle_info(info["bundle"])["name"]
    elif owner.kind == "service":
        owner.title = f"VM · {info['vm']}" if not info["vm"].startswith(
            ("virtualization-", "qemu-")) else "Container VM"
    else:
        p = inv.procs[owner.root]
        owner.title = p.comm or f"process {owner.root}"


def codex_handles(roles: dict, ctx: Context) -> dict:
    """What each Codex process holds open, from one lsof over the candidates.

    pid -> {"thread": uuid of a writer lock or rollout it holds, "threads":
    every such uuid, "addrs": its daemon sockets (codex-daemon-<uid>,
    app-server-control), "peers": the sockets its unix fds connect to}. Only an open handle counts: lock files outlive a killed TUI."""
    out: dict = {}
    pids = sorted(p for p, r in roles.items() if r in ("exec", "tui", "app-server"))
    if not pids or not ctx.lsof or not any(roles[p] != "app-server" for p in pids):
        return out
    try:
        # -O -b: no helper fork, no blocking kernel calls (half the cost).
        text = ctx.lsof(["-O", "-b", "-w", "-nP", "-a", "-p", ",".join(map(str, pids)),
                         "-Fpftdn"])
    except Exception:
        return out
    pid, ftype, dev = None, None, None
    for line in text.splitlines():
        tag, val = line[:1], line[1:]
        if tag == "p":
            pid = int(val) if val.isdigit() else None
            if pid is not None:
                out.setdefault(pid, {"thread": None, "threads": [], "addrs": [], "peers": []})
        elif tag == "f":
            ftype, dev = None, None
        elif tag == "t":
            ftype = val
        elif tag == "d":
            dev = val
        elif tag == "n" and pid is not None:
            row = out[pid]
            if ftype == "unix":
                if dev and ("codex-daemon-" in val or "app-server-control" in val):
                    row["addrs"].append(dev)
                if val.startswith("->"):
                    row["peers"].append(val[2:])
                continue
            m = ROLLOUT_RE.search(val) or LOCK_RE.search(val)
            if m and m.group(1) not in row["threads"]:
                row["threads"].append(m.group(1))
                row["thread"] = row["thread"] or m.group(1)
    return out


def _codex_names(part: Partition, ctx: Context) -> dict:
    """Display names for exec threads and the daemon's live threads."""
    out: dict = {"names": {}}
    if not any(o.kind in ("codex", "codex-app") for o in part.owners.values()):
        return out
    wanted = {o.info["thread_id"] for o in part.owners.values()
              if o.info.get("thread_id")}
    wanted |= {t for o in part.owners.values() for t in o.info.get("threads") or ()}
    if wanted:
        try:
            with open(os.path.join(ctx.codex_home, "session_index.jsonl")) as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("id") in wanted and row.get("thread_name"):
                        out["names"][row["id"]] = " ".join(str(row["thread_name"]).split())[:80]
        except OSError:
            pass
        missing = wanted - set(out["names"])
        if missing:
            out["names"].update(_sqlite_titles(ctx.codex_home, missing))
    return out


def _sqlite_titles(codex_home: str, ids: set) -> dict:
    import sqlite3
    path = os.path.join(codex_home, "state_5.sqlite")
    if not os.path.exists(path):
        return {}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)
        try:
            marks = ",".join("?" * len(ids))
            rows = con.execute(f"SELECT id, title FROM threads WHERE id IN ({marks})",
                               sorted(ids)).fetchall()
        finally:
            con.close()
    except Exception:
        return {}
    return {i: " ".join(str(t).split())[:80] for i, t in rows if t}


def child_jobs(owner: Owner, inv, part: Partition, ctx: Context) -> list:
    """Tool-shell subtrees of an agent root. Claude's tool shells lead their own
    process group; the worker and the MCP helpers sharing its group are the
    Conversation, which cannot be stopped on its own."""
    if owner.kind not in ENDABLE_KINDS:
        return []
    root = inv.procs[owner.root]
    mine = set(owner.members)
    roots = set(part.root_owner)
    lease_of = {r.get("child_pid"): r for r in ctx.leases or []}
    jobs = []
    for c in sorted(inv.children().get(owner.root, ())):
        p = inv.procs.get(c)
        if c not in mine or p is None or p.zombie or p.pgid == root.pgid:
            continue
        argv = inv.argv(c) or []
        if owner.kind == "codex" and os.path.basename(argv[0] if argv else "") not in SHELLS:
            continue
        members = [c] + [d for d in inv.descendants(c, stop=roots) if d in mine]
        kind, label = classify_job(job_command(inv, c), ctx.classify, ctx.commands)
        if ctx.listening and any(m in ctx.listening for m in members):
            kind = "server"
        lease = next((lease_of[m] for m in members if m in lease_of
                      and list(inv.procs[m].start) == list(lease_of[m].get("child_start") or [])),
                     None)
        jobs.append({"root": c, "members": members, "kind": kind, "label": label,
                     "lease": lease})
    return jobs


# ------------------------------------------------------- history and CPU

def read_json(path: str, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json_atomic(path: str, value) -> None:
    import tempfile
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as fh:
            # dumps, not dump: only the one-shot path uses the C encoder.
            fh.write(json.dumps(value, separators=(",", ":")))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_history(hist: dict, fps: dict, ts: float) -> dict:
    """Append one [ts, footprint] per owner; keep 60 samples per owner and the
    200 most recently seen owners. An unmeasured owner gets no sample."""
    owners = dict(hist.get("owners") or {})
    for oid, fp in fps.items():
        if fp is None:
            continue
        row = owners.get(oid) or {"samples": []}
        samples = (row.get("samples") or []) + [[round(ts, 1), fp]]
        owners[oid] = {"last_seen": round(ts, 1), "samples": samples[-HISTORY_MAX_SAMPLES:]}
    if len(owners) > HISTORY_MAX_OWNERS:
        # Most recently seen first; among owners seen in the same tick, the
        # largest keep their history.
        keep = sorted(owners, key=lambda k: (-owners[k].get("last_seen", 0),
                                             -(owners[k]["samples"] or [[0, 0]])[-1][1]))
        owners = {k: owners[k] for k in keep[:HISTORY_MAX_OWNERS]}
    return {"version": 1, "owners": owners}


def growth(samples: list, now: float) -> tuple:
    """Least-squares slope over the last 10-15 minutes, in bytes per 10 min.

    Needs >= 5 samples spanning >= 10 minutes with no gap over 180 s. A gap
    (sleep, a sampler outage) resets the window: only the contiguous tail
    after it counts."""
    pts = [s for s in samples or [] if now - s[0] <= GROWTH_WINDOW_S]
    if not pts:
        return None, "not enough history"
    tail = [pts[-1]]
    for prev in reversed(pts[:-1]):
        if tail[0][0] - prev[0] > MAX_GAP_S:
            break
        tail.insert(0, prev)
    if now - tail[-1][0] > MAX_GAP_S:
        return None, "not enough history"
    if len(tail) < GROWTH_MIN_SAMPLES or tail[-1][0] - tail[0][0] < GROWTH_MIN_SPAN_S:
        return None, "not enough history"
    n = len(tail)
    mx = sum(t for t, _ in tail) / n
    my = sum(v for _, v in tail) / n
    den = sum((t - mx) ** 2 for t, _ in tail)
    if den <= 0:
        return None, "not enough history"
    slope = sum((t - mx) * (v - my) for t, v in tail) / den
    return int(slope * 600), None


def awake_ns() -> int:
    """Time since boot excluding sleep; diverges from CLOCK_MONOTONIC on wake."""
    return time.clock_gettime_ns(time.CLOCK_UPTIME_RAW)


def boot_id() -> str | None:
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        buf = (ctypes.c_int64 * 2)()
        size = ctypes.c_size_t(16)
        if libc.sysctlbyname(b"kern.boottime", buf, ctypes.byref(size), None,
                             ctypes.c_size_t(0)) != 0:
            return None
        return f"{buf[0]}.{buf[1] & 0xFFFFFFFF}"
    except Exception:
        return None


def baseline_problem(base: dict, inv, boot: str | None, awake: int) -> str | None:
    """Why a persisted CPU baseline cannot be used, or None when it can."""
    if not base or "procs" not in base:
        return "warming up"
    if base.get("boot_id") != boot or boot is None:
        return "warming up"
    gap = inv.mono_ns - int(base.get("mono_ns", 0))
    if gap <= 0 or gap > MAX_GAP_S * 1_000_000_000:
        return "warming up"
    slept = gap - (awake - int(base.get("awake_ns", 0)))
    if slept > WAKE_SLACK_NS:
        return "warming up"
    return None


def make_baseline(inv, boot: str | None, awake: int) -> dict:
    return {"boot_id": boot, "wall_ts": inv.ts, "mono_ns": inv.mono_ns,
            "awake_ns": awake,
            "procs": memmon_procs.tick_table(inv, BASELINE_MAX_PROCS)}


# ---------------------------------------------------------------- payload

def _row_extra_title(rows: list) -> None:
    """Rows that still collide on title, project and worktree get a
    'started HH:MM' suffix so they can be told apart without a PID."""
    seen: dict = {}
    for r in rows:
        if r["kind"] == "unknown":          # collapsed into one row by the UI
            continue
        seen.setdefault((r["title"], r["project"], r["worktree"]), []).append(r)
    for group in seen.values():
        if len(group) < 2:
            continue
        for r in group:
            started = time.strftime("%H:%M", time.localtime(r["root"]["start"][0]))
            r["title"] = f"{r['title']} · started {started}"


def owners_payload(sample: Sample, ctx: Context, *, history: dict | None = None,
                   now: float | None = None, cpu_window_s: float | None = None,
                   system: dict | None = None, protection: dict | None = None,
                   gate: dict | None = None, source: str = "live",
                   used_by: dict | None = None) -> dict:
    inv, part = sample.inv, sample.part
    now = inv.ts if now is None else now
    degraded = inv.kind == "degraded"
    codex = _codex_names(part, ctx) if not degraded else {}
    hosts = hosted_by(part, inv)
    hist_owners = (history or {}).get("owners") or {}
    rows = []
    for owner in part.owners.values():
        _titles(owner, inv, ctx, codex)
        root = inv.procs[owner.root]
        fp = _sum(inv.procs[p].footprint for p in owner.members)
        cores, coverage = _cpu(owner.members, sample.cpu)
        if owner.kind == "unknown":
            g, g_reason = None, "tracked as Unattributed"
        else:
            g, g_reason = growth((hist_owners.get(owner.owner_id) or {}).get("samples"), now)
        cwd = owner.info.get("cwd") or (inv.cwd(owner.root)
                                        if owner.kind in ("claude", "codex", "job") else None)
        project, worktree = git_place(cwd) if cwd else (None, None)
        jobs = child_jobs(owner, inv, part, ctx)
        job_rows = []
        for j in jobs:
            jp = inv.procs[j["root"]]
            action = ("stop-managed-job" if j["lease"] else
                      "stop-server" if j["kind"] == "server" else "stop-job")
            target = jp
            if j["lease"]:
                target = inv.procs.get(j["lease"]["child_pid"]) or jp
            body = {"v": 1, "action": action, "owner_id": owner.owner_id,
                    "owner_root": ident(root), "target": ident(target),
                    "target_pgid": target.pgid, "snapshot_ts": round(inv.ts, 3)}
            if j["lease"]:
                body["run_id"] = j["lease"].get("id")
            job_rows.append({
                "job_id": f"{jp.pid}.{jp.start[0]}.{jp.start[1]}",
                "kind": j["kind"], "label": j["label"],
                "footprint_bytes": _sum(inv.procs[m].footprint for m in j["members"]),
                "member_count": len(j["members"]),
                "managed": bool(j["lease"]), "action": action,
                "root": {**ident(jp), "pgid": jp.pgid},
                "token": None if degraded else mint_token(body),
            })
        job_rows.sort(key=lambda r: -(r["footprint_bytes"] or 0))
        in_jobs = {m for j in jobs for m in j["members"]}
        convo = [m for m in owner.members if m not in in_jobs]

        conversation = None
        if owner.kind in ENDABLE_KINDS:
            conversation = {
                "job_id": f"conversation.{root.pid}.{root.start[0]}", "kind": "conversation",
                "label": "Conversation",
                "footprint_bytes": _sum(inv.procs[m].footprint for m in convo),
                "member_count": len(convo), "managed": False, "action": None,
                "root": {**ident(root), "pgid": root.pgid}, "token": None}

        if job_rows:
            j0 = job_rows[0]
            activity = f"{VERB[j0['kind']]} · {j0['label']}"
        elif cores is None:
            activity = None
        elif cores > WORKING_CORES:
            activity = "Working"
        else:
            activity = "Idle"

        actions, token = [], None
        if owner.kind in ENDABLE_KINDS:
            actions = ["end-session"]
            token = mint_token({"v": 1, "action": "end-session",
                                "owner_id": owner.owner_id, "owner_root": ident(root),
                                "target": ident(root), "snapshot_ts": round(inv.ts, 3)})
        elif owner.kind == "job":
            lease = owner.info["lease"]
            actions = ["stop-managed-job"]
            token = mint_token({"v": 1, "action": "stop-managed-job",
                                "owner_id": owner.owner_id, "owner_root": ident(root),
                                "target": ident(root), "run_id": lease.get("id"),
                                "snapshot_ts": round(inv.ts, 3)})
        instances = None
        if owner.info.get("bundle"):
            bundle = owner.info["bundle"]
            insts = sorted(r for r in owner.roots if is_app_instance(inv, r))
            instances = [{"pid": r, "start": list(inv.procs[r].start),
                          "launch_date": inv.procs[r].start[0] + inv.procs[r].start[1] / 1e6}
                         for r in insts]
            if instances and owner.owner_id not in hosts:
                # A widget- or helper-only row has nothing to quit; an app
                # hosting sessions would end them.
                actions = ["quit-app"]
                token = mint_token({"v": 1, "action": "quit-app", "owner_id": owner.owner_id,
                                    "bundle_id": bundle_info(bundle)["bundle_id"],
                                    "instances": instances, "snapshot_ts": round(inv.ts, 3)})
        shared_with = None
        if owner.kind == "codex-app" and "threads" in owner.info:
            shared_with = [codex["names"].get(t) or f"Codex · {t[:8]}"
                           for t in owner.info["threads"]][:10]
        if degraded:
            token, actions = None, []
        row = {
            "owner_id": owner.owner_id, "kind": owner.kind,
            "agent": AGENT_OF[owner.kind], "title": owner.title,
            "project": project, "worktree": worktree, "activity": activity,
            "confidence": owner.confidence,
            "footprint_bytes": fp,
            "footprint_reason": None if fp is not None else "not measured",
            "cpu_cores": None if cores is None else round(cores, 3),
            "cpu_coverage": coverage,
            "cpu_reason": None if cores is not None else (sample.cpu_reason or "not measured"),
            "growth_bytes_per_10min": g, "growth_reason": g_reason,
            "member_count": len(owner.members),
            "root": {**ident(root), "pgid": root.pgid},
            "started_at": root.start[0] + root.start[1] / 1e6,
            "token": token, "jobs": ([conversation] if conversation else []) + job_rows,
            "actions": actions, "instances": instances, "shared_with": shared_with,
            "stop_command": (stop_command_for(owner.info["vm"])
                             if owner.kind == "service" and not owner.info.get("bundle")
                             else None),
            "used_by": None,
            "hosts": sorted(hosts.get(owner.owner_id, [])),
            "hosts_shells": bool(owner.info.get("bundle")) and bundle_info(
                owner.info["bundle"])["bundle_id"] in SHELL_HOSTS,
        }
        if owner.kind == "service" and used_by and owner.owner_id in used_by:
            row["used_by"] = used_by[owner.owner_id]
        rows.append(row)
    if used_by:
        titles = {r["owner_id"]: r["title"] for r in rows}
        for r in rows:
            if r["used_by"] is not None:
                r["used_by"] = sorted({titles.get(o, o) for o in r["used_by"]})
    _row_extra_title(rows)
    rows.sort(key=lambda r: (r["footprint_bytes"] is None, -(r["footprint_bytes"] or 0)))
    unknown = [r for r in rows if r["kind"] == "unknown"]
    ug, ug_reason = growth((hist_owners.get(UNATTRIBUTED) or {}).get("samples"), now)
    return {
        "schema_version": 2, "ts": round(inv.ts, 3), "source": source,
        "inventory": inv.kind,
        "inventory_reason": memmon_procs.degraded_reason(inv.source),
        "cpu_window_s": cpu_window_s,
        "system": system, "protection": protection, "gate": gate,
        "hidden_process_count": part.hidden,
        "unattributed": {
            "owner_count": len(unknown),
            "member_count": sum(r["member_count"] for r in unknown),
            "footprint_bytes": _sum(r["footprint_bytes"] for r in unknown),
            "growth_bytes_per_10min": ug, "growth_reason": ug_reason},
        "owners": rows,
    }


CANDIDATE_COMMS = {"node", "bun", "deno", "python", "python3", "zsh", "bash", "sh",
                   "pnpm", "npm", "npx", "yarn", "turbo", "tsc", "vitest", "jest",
                   "pytest", "cargo", "make", "gradle", "java", "docker", "go",
                   "esbuild", "next-server", "webpack", "playwright", "xcodebuild"}


def unmanaged_heavy(sample: Sample, ctx: Context) -> int:
    """Heavy work (build, test, dev server) running outside `memmon run`,
    under any owner: a session's job, a terminal app, an orphan. Counted once
    per subtree, so a pnpm → node → tsc chain is one."""
    inv = sample.inv
    managed: set = set()
    for row in ctx.leases or []:
        cp = inv.procs.get(row.get("child_pid"))
        if cp is not None and list(cp.start) == list(row.get("child_start") or []):
            managed.add(cp.pid)
            managed.update(inv.descendants(cp.pid))
    heavy, kinds = set(), {}                 # MCP servers repeat per session
    for pid, p in inv.procs.items():
        if (not p.visible or p.zombie or pid in managed or pid == os.getpid()
                or p.comm.lower() not in CANDIDATE_COMMS):
            continue
        cmd = process_command(inv, pid)
        if cmd not in kinds:
            kinds[cmd] = classify_job(cmd, ctx.classify, ctx.commands)[0]
        if kinds[cmd] in HEAVY_KINDS:
            heavy.add(pid)
    return sum(1 for pid in heavy if inv.procs[pid].ppid not in heavy)
