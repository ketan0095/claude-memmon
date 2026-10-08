"""Process inventory: one libproc pass over every PID, with identity.

libproc is ~170x cheaper than `top` and gives what `top` cannot: each
process's start time to the microsecond, which is what makes (pid, start) an
identity a reused PID cannot impersonate. `top`/`ps` remain only as a degraded
fallback, and a degraded inventory is never trusted to act on.

libproc.h is marked private, so nothing here assumes the struct layouts are
right. Sizes are checked on every call, and an import-time self-check on our
own PID compares the readings with values Python obtains independently. Any
mismatch falls back to the degraded path rather than reading garbage.

A field that could not be read is None, never 0. Processes of other users and
root return EPERM; they are `visible=False` and get no numbers at all.
"""

from __future__ import annotations

import ctypes
import errno
import os
import re
import subprocess
import time
from dataclasses import dataclass

PROC_PIDTBSDINFO = 3
PROC_PIDVNODEPATHINFO = 9
RUSAGE_INFO_V4 = 4
PROC_PIDPATHINFO_MAXSIZE = 4096
SZOMB = 5                      # sys/proc.h process status
VNODEPATHINFO_SIZE = 2352      # struct proc_vnodepathinfo: 2 x vnode_info_path
CDIR_PATH_OFFSET = 152         # pvi_cdir.vip_path, after struct vnode_info
CTL_KERN = 1
KERN_PROCARGS2 = 49


class BSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class RUsageV4(ctypes.Structure):
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [(name, ctypes.c_uint64) for name in (
        "ri_user_time", "ri_system_time", "ri_pkg_idle_wkups", "ri_interrupt_wkups",
        "ri_pageins", "ri_wired_size", "ri_resident_size", "ri_phys_footprint",
        "ri_proc_start_abstime", "ri_proc_exit_abstime", "ri_child_user_time",
        "ri_child_system_time", "ri_child_pkg_idle_wkups", "ri_child_interrupt_wkups",
        "ri_child_pageins", "ri_child_elapsed_abstime", "ri_diskio_bytesread",
        "ri_diskio_byteswritten", "ri_cpu_time_qos_default",
        "ri_cpu_time_qos_maintenance", "ri_cpu_time_qos_background",
        "ri_cpu_time_qos_utility", "ri_cpu_time_qos_legacy",
        "ri_cpu_time_qos_user_initiated", "ri_cpu_time_qos_user_interactive",
        "ri_billed_system_time", "ri_serviced_system_time", "ri_logical_writes",
        "ri_lifetime_max_phys_footprint", "ri_instructions", "ri_cycles",
        "ri_billed_energy", "ri_serviced_energy", "ri_interval_max_phys_footprint",
        "ri_runnable_time")]


class Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


# The layouts above are the ABI this module depends on. A header change that
# moved a field would shift every reading silently, so the sizes are pinned.
assert ctypes.sizeof(BSDInfo) == 136
assert ctypes.sizeof(RUsageV4) == 296


@dataclass
class Proc:
    pid: int
    start: tuple | None = None          # (sec, usec): the identity with pid
    ppid: int | None = None
    pgid: int | None = None
    uid: int | None = None
    zombie: bool = False
    footprint: int | None = None        # ri_phys_footprint, what top calls MEM
    resident: int | None = None
    lifetime_max: int | None = None
    cpu_ticks: int | None = None        # user + system, Mach ticks
    comm: str = ""
    visible: bool = True
    argv: list | None = None

    def key(self) -> str | None:
        """Identity string used by persisted baselines: "pid.sec.usec"."""
        if self.start is None:
            return None
        return f"{self.pid}.{self.start[0]}.{self.start[1]}"


class ProcSource:
    """The only seam tests replace. A source answers for one process at a time
    (`read`) or for all of them (`scan`); argv, executable path and cwd are read
    lazily because they cost a syscall each and only candidate owners need them."""

    name = "libproc"

    def scan(self) -> dict:
        raise NotImplementedError

    def read(self, pid: int) -> Proc | None:
        """Fresh identity for one PID; None when it no longer exists."""
        raise NotImplementedError

    def argv(self, pid: int) -> list | None:
        return None

    def path(self, pid: int) -> str | None:
        return None

    def cwd(self, pid: int) -> str | None:
        return None

    def responsible(self, pid: int) -> int | None:
        """The process macOS holds responsible for `pid` (for an XPC service,
        the app that asked for it), or None."""
        return None

    def timebase(self) -> tuple:
        return (1, 1)


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


class LibprocSource(ProcSource):
    name = "libproc"

    def __init__(self, lib, libc):
        self.lib = lib
        self.libc = libc
        self.my_uid = os.getuid()
        tb = Timebase()
        libc.mach_timebase_info(ctypes.byref(tb))
        self._timebase = (tb.numer, tb.denom)
        size = ctypes.c_int(0)
        sz = ctypes.c_size_t(ctypes.sizeof(size))
        if libc.sysctlbyname(b"kern.argmax", ctypes.byref(size), ctypes.byref(sz),
                             None, ctypes.c_size_t(0)) != 0:
            size.value = 1024 * 1024
        self._argmax = size.value

    def timebase(self) -> tuple:
        return self._timebase

    def list_pids(self) -> list:
        n = self.lib.proc_listallpids(None, 0)
        if n <= 0:
            raise OSError(ctypes.get_errno(), "proc_listallpids failed")
        buf = (ctypes.c_int * (n + 256))()
        n = self.lib.proc_listallpids(buf, ctypes.sizeof(buf))
        if n <= 0:
            raise OSError(ctypes.get_errno(), "proc_listallpids failed")
        return [p for p in buf[:n] if p > 0]

    def read(self, pid: int) -> Proc | None:
        bi = BSDInfo()
        ctypes.set_errno(0)
        n = self.lib.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, ctypes.byref(bi),
                                  ctypes.sizeof(bi))
        if n != ctypes.sizeof(bi):
            err = ctypes.get_errno()
            if n == 0 and err == errno.ESRCH:
                return None
            # EPERM (another user, root) or a short read: present, unreadable.
            return Proc(pid=pid, visible=False)
        proc = Proc(
            pid=pid, start=(int(bi.pbi_start_tvsec), int(bi.pbi_start_tvusec)),
            ppid=int(bi.pbi_ppid), pgid=int(bi.pbi_pgid), uid=int(bi.pbi_uid),
            zombie=bi.pbi_status == SZOMB,
            comm=_cstr(bi.pbi_name) or _cstr(bi.pbi_comm),
            visible=int(bi.pbi_uid) == self.my_uid,
        )
        if not proc.visible:
            return proc
        ru = RUsageV4()
        if self.lib.proc_pid_rusage(pid, RUSAGE_INFO_V4, ctypes.byref(ru)) == 0:
            proc.footprint = int(ru.ri_phys_footprint)
            proc.resident = int(ru.ri_resident_size)
            proc.lifetime_max = int(ru.ri_lifetime_max_phys_footprint)
            proc.cpu_ticks = int(ru.ri_user_time) + int(ru.ri_system_time)
        return proc

    def scan(self) -> dict:
        out = {}
        for pid in self.list_pids():
            proc = self.read(pid)
            if proc is not None:
                out[pid] = proc
        return out

    def argv(self, pid: int) -> list | None:
        size = ctypes.c_size_t(self._argmax)
        buf = ctypes.create_string_buffer(self._argmax)
        mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
        if self.libc.sysctl(mib, 3, buf, ctypes.byref(size), None,
                            ctypes.c_size_t(0)) != 0:
            return None
        raw = buf.raw[:size.value]
        if len(raw) < 4:
            return None
        argc = int.from_bytes(raw[:4], "little")
        rest = raw[4:]
        end = rest.find(b"\0")              # the executable path comes first
        if end < 0:
            return None
        i = end
        while i < len(rest) and rest[i] == 0:
            i += 1
        args = rest[i:].split(b"\0")[:argc]
        return [a.decode("utf-8", "replace") for a in args]

    def path(self, pid: int) -> str | None:
        buf = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
        n = self.lib.proc_pidpath(pid, buf, PROC_PIDPATHINFO_MAXSIZE)
        return buf.value.decode("utf-8", "replace") if n > 0 else None

    def responsible(self, pid: int) -> int | None:
        fn = getattr(self.libc, "responsibility_get_pid_responsible_for_pid", None)
        if fn is None:
            return None
        fn.argtypes, fn.restype = [ctypes.c_int], ctypes.c_int
        r = fn(pid)
        return r if r > 0 else None

    def cwd(self, pid: int) -> str | None:
        buf = ctypes.create_string_buffer(VNODEPATHINFO_SIZE)
        n = self.lib.proc_pidinfo(pid, PROC_PIDVNODEPATHINFO, 0, buf,
                                  VNODEPATHINFO_SIZE)
        if n != VNODEPATHINFO_SIZE:
            return None
        return _cstr(buf.raw[CDIR_PATH_OFFSET:CDIR_PATH_OFFSET + 1024]) or None


LSTART_FORMATS = ("%a %b %d %H:%M:%S %Y", "%a %d %b %H:%M:%S %Y")


def _parse_lstart(text: str) -> int | None:
    text = " ".join(text.split())
    for fmt in LSTART_FORMATS:
        try:
            return int(time.mktime(time.strptime(text, fmt)))
        except ValueError:
            continue
    return None


class PsTopSource(ProcSource):
    """Degraded inventory: ps for the tree, top for memory. Identity drops to
    lstart's one-second resolution, so nothing may act on it."""

    name = "degraded"

    def __init__(self, run=subprocess.run):
        self._run = run
        self.my_uid = os.getuid()
        self._args: dict | None = None

    def _out(self, cmd: list) -> str:
        try:
            return self._run(cmd, capture_output=True, text=True, timeout=15,
                             env=dict(os.environ, LC_ALL="C")).stdout
        except Exception:
            return ""

    def _table(self, extra: list) -> dict:
        out = {}
        for line in self._out(["ps", "-o", "pid=,ppid=,pgid=,uid=,lstart=,stat=,comm="]
                              + extra).splitlines():
            parts = line.split(None, 10)
            if len(parts) < 11 or not parts[0].isdigit():
                continue
            pid, ppid, pgid, uid = (int(x) for x in parts[:4])
            sec = _parse_lstart(" ".join(parts[4:9]))
            out[pid] = Proc(pid=pid, start=(sec, 0) if sec is not None else None,
                            ppid=ppid, pgid=pgid, uid=uid, zombie="Z" in parts[9],
                            comm=os.path.basename(parts[10]),
                            visible=uid == self.my_uid)
        return out

    def scan(self) -> dict:
        procs = self._table(["-ax"])
        mem = {}
        started = False
        for line in self._out(["top", "-l", "1", "-stats", "pid,mem"]).splitlines():
            if line.startswith("PID"):
                started = True
                continue
            parts = line.split()
            if started and len(parts) >= 2 and parts[0].isdigit():
                mem[int(parts[0])] = _parse_size(parts[1])
        for pid, proc in procs.items():
            if proc.visible:
                proc.footprint = mem.get(pid)
        self._args = None
        return procs

    def read(self, pid: int) -> Proc | None:
        return self._table(["-p", str(pid)]).get(pid)

    def argv(self, pid: int) -> list | None:
        if self._args is None:
            self._args = {}
            for line in self._out(["ps", "-axo", "pid=,args="]).splitlines():
                parts = line.strip().split(None, 1)
                if len(parts) == 2 and parts[0].isdigit():
                    self._args[int(parts[0])] = parts[1].split()
        return self._args.get(pid)


def _parse_size(tok: str) -> int | None:
    tok = tok.strip().rstrip("+-")
    mult = {"B": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}.get(
        tok[-1:].upper())
    try:
        return int(float(tok[:-1]) * mult) if mult else int(float(tok))
    except ValueError:
        return None


LIBPROC = "/usr/lib/libproc.dylib"


def _load_libproc():
    # The direct path skips importing ctypes.util (~12 ms per process).
    try:
        lib = ctypes.CDLL(LIBPROC, use_errno=True)
    except OSError:
        from ctypes.util import find_library
        path = find_library("proc")
        if not path:
            return None, None
        lib = ctypes.CDLL(path, use_errno=True)
    lib.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.proc_listallpids.restype = ctypes.c_int
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                 ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    lib.proc_pid_rusage.restype = ctypes.c_int
    lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    lib.proc_pidpath.restype = ctypes.c_int
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_size_t]
    libc.sysctl.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    return lib, libc


def sysctl_u64(name: str, libc=None) -> int:
    """Strict sysctl integer read: raises instead of defaulting."""
    libc = libc or ctypes.CDLL(None, use_errno=True)
    val = ctypes.c_uint64(0)
    size = ctypes.c_size_t(ctypes.sizeof(val))
    if libc.sysctlbyname(name.encode(), ctypes.byref(val), ctypes.byref(size),
                         None, ctypes.c_size_t(0)) != 0:
        raise OSError(ctypes.get_errno(), f"sysctl {name} failed")
    if size.value == 4:
        return val.value & 0xFFFFFFFF
    if size.value != 8:
        raise OSError(errno.EINVAL, f"sysctl {name}: unexpected size {size.value}")
    return val.value


def self_check(source: LibprocSource, memsize: int | None = None) -> str | None:
    """Validate the libproc readings against ones Python gets independently.
    Returns None when they agree, else the first disagreement."""
    pid = os.getpid()
    proc = source.read(pid)
    if proc is None or not proc.visible or proc.start is None:
        return "own process unreadable"
    if proc.footprint is None or proc.cpu_ticks is None:
        return "rusage unreadable"
    if proc.pid != pid or proc.ppid != os.getppid():
        return "pbi_pid/pbi_ppid mismatch"
    if proc.start[0] > time.time() + 1:
        return "start time in the future"
    memsize = memsize or sysctl_u64("hw.memsize", source.libc)
    if not 0 < proc.footprint <= memsize:
        return "footprint out of range"
    if proc.lifetime_max is None or proc.lifetime_max < proc.footprint:
        return "lifetime max below footprint"
    numer, denom = source.timebase()
    cpu_s = proc.cpu_ticks * numer / denom / 1e9
    expect = time.process_time()
    # Both numbers come from the same kernel task times (measured identical
    # to the microsecond), so a unit error of any size shows; 41.7x is what
    # a wrong timebase would be on arm64.
    if abs(cpu_s - expect) > 0.002 + 0.05 * expect:
        return f"cpu ticks disagree ({cpu_s:.3f}s vs {expect:.3f}s)"
    return None


def _probe():
    try:
        lib, libc = _load_libproc()
        if lib is None:
            return None, "libproc not found"
        src = LibprocSource(lib, libc)
        problem = self_check(src)
        return (None, problem) if problem else (src, None)
    except Exception as exc:          # a missing symbol is a degradation, not a crash
        return None, f"libproc unusable: {type(exc).__name__}"


_LIBPROC, DEGRADED_REASON = _probe()


def default_source() -> ProcSource:
    """libproc when the self-check passed, else the degraded ps/top source.
    MEMMON_INVENTORY=top forces the degraded path (the rollback switch)."""
    if os.environ.get("MEMMON_INVENTORY") == "top" or _LIBPROC is None:
        return PsTopSource()
    return _LIBPROC


def degraded_reason(source: ProcSource) -> str | None:
    if source.name != "degraded":
        return None
    if os.environ.get("MEMMON_INVENTORY") == "top":
        return "MEMMON_INVENTORY=top"
    return DEGRADED_REASON or "libproc unavailable"


class Inventory:
    """One pass over every PID, plus lazy per-PID extras cached for its life."""

    def __init__(self, procs: dict, source: ProcSource, ts: float, mono_ns: int):
        self.procs = procs
        self.source = source
        self.ts = ts
        self.mono_ns = mono_ns
        self._kids: dict | None = None
        self._path: dict = {}
        self._cwd: dict = {}

    @property
    def kind(self) -> str:
        return "degraded" if self.source.name == "degraded" else "libproc"

    def children(self) -> dict:
        if self._kids is None:
            kids: dict = {}
            for pid, p in self.procs.items():
                if p.ppid is not None and p.ppid != pid:
                    kids.setdefault(p.ppid, []).append(pid)
            self._kids = kids
        return self._kids

    def descendants(self, pid: int, stop=frozenset()) -> list:
        """Every process below `pid`, not descending into any PID in `stop`."""
        out, stack, seen = [], [pid], {pid}
        kids = self.children()
        while stack:
            for k in kids.get(stack.pop(), ()):
                if k in seen or k in stop:
                    continue
                seen.add(k)
                out.append(k)
                stack.append(k)
        return out

    def argv(self, pid: int) -> list | None:
        p = self.procs.get(pid)
        if p is None or not p.visible:
            return None
        if p.argv is None:
            p.argv = self.source.argv(pid) or []
        return p.argv

    def cmdline(self, pid: int) -> str:
        return " ".join(self.argv(pid) or [])

    def path(self, pid: int) -> str | None:
        if pid not in self._path:
            p = self.procs.get(pid)
            self._path[pid] = (self.source.path(pid)
                               if p is not None and p.visible else None)
        return self._path[pid]

    def responsible(self, pid: int) -> int | None:
        p = self.procs.get(pid)
        return self.source.responsible(pid) if p is not None and p.visible else None

    def cwd(self, pid: int) -> str | None:
        if pid not in self._cwd:
            p = self.procs.get(pid)
            self._cwd[pid] = (self.source.cwd(pid)
                              if p is not None and p.visible else None)
        return self._cwd[pid]


def mono_ns() -> int:
    """CLOCK_MONOTONIC, which on macOS keeps counting through sleep."""
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


def snapshot(source: ProcSource | None = None, clock=None, mono=None) -> Inventory:
    source = source or default_source()
    procs = source.scan()
    return Inventory(procs, source, (clock or time.time)(), (mono or mono_ns)())


def identity_of(proc: Proc | None) -> tuple | None:
    """(pid, sec, usec) for a live, non-zombie process, else None."""
    if proc is None or proc.zombie or proc.start is None:
        return None
    return (proc.pid, proc.start[0], proc.start[1])


def cpu_cores(prev: dict, cur: Inventory, prev_mono_ns: int) -> dict:
    """Per-PID cores between a baseline {"pid.sec.usec": ticks} and `cur`.

    A PID missing from either side, or whose start changed, is absent from the
    result: its CPU is unknown ("warming up"), never zero."""
    dt = cur.mono_ns - prev_mono_ns
    if dt <= 0:
        return {}
    numer, denom = cur.source.timebase()
    out = {}
    for pid, p in cur.procs.items():
        if p.cpu_ticks is None:
            continue
        before = prev.get(p.key())
        if before is None or p.cpu_ticks < before:
            continue
        out[pid] = (p.cpu_ticks - before) * numer / denom / dt
    return out


def tick_table(inv: Inventory, cap: int = 4096) -> dict:
    """The {"pid.sec.usec": ticks} baseline for `inv`, largest CPU users kept
    first when the cap applies."""
    rows = [(p.key(), p.cpu_ticks) for p in inv.procs.values()
            if p.visible and p.cpu_ticks is not None and p.key()]
    rows.sort(key=lambda kv: -kv[1])
    return dict(rows[:cap])


PRESSURE_LEVELS = {1: "normal", 2: "warning", 4: "critical"}
_VM_KEYS = ("Anonymous pages", "Pages purgeable", "Pages wired down",
            "Pages occupied by compressor")


def read_system_strict(run=subprocess.run, sysctl=sysctl_u64,
                       timeout: float = 2.0) -> dict:
    """Memory in use and the kernel pressure level, or an exception.

    used = (anonymous - purgeable) + wired + compressor-occupied pages. It
    excludes "Pages stored in compressor", which is a logical size. Unlike
    pressure(), nothing here defaults: a timeout, a non-zero exit, a parse
    failure or a missing field raises."""
    level = sysctl("kern.memorystatus_vm_pressure_level")
    if level not in PRESSURE_LEVELS:
        raise ValueError(f"unknown pressure level {level}")
    ram = sysctl("hw.memsize")
    proc = run(["vm_stat"], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise OSError(f"vm_stat exited {proc.returncode}")
    m = re.search(r"page size of (\d+) bytes", proc.stdout)
    if not m:
        raise ValueError("vm_stat: no page size")
    page = int(m.group(1))
    counts = {}
    for line in proc.stdout.splitlines():
        km = re.match(r'"?([^:"]+)"?:\s+(\d+)\.?\s*$', line.strip())
        if km:
            counts[km.group(1).strip()] = int(km.group(2))
    missing = [k for k in _VM_KEYS if k not in counts]
    if missing:
        raise KeyError(f"vm_stat missing {', '.join(missing)}")
    pages = (counts["Anonymous pages"] - counts["Pages purgeable"]
             + counts["Pages wired down"] + counts["Pages occupied by compressor"])
    return {"ram_bytes": ram, "used_bytes": pages * page,
            "pressure_level": PRESSURE_LEVELS[level]}
