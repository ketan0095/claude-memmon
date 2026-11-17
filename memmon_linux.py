"""Linux readings behind the same seams the macOS code uses.

Everything memmon reads on a Mac through libproc, sysctl, vm_stat and top is
read here from /proc. The rest of memmon (owners, gate, runner, sampler,
scorer) is shared, so this module only answers the questions those seams
ask, in the units they already expect. Nothing in it signals a process.

Choices, each made for a reason:

  Footprint   RssAnon + VmSwap from /proc/<pid>/status. macOS's
              phys_footprint is a process's own dirty memory, compressed or
              swapped pages included, and leaves out clean file pages. RssAnon
              plus swap is the nearest Linux figure. /proc/<pid>/smaps_rollup
              gives PSS, which is fairer for shared memory, but it walks every
              mapping under the process's mmap lock: a full scan of a box with
              ten agents costs tens of milliseconds and stalls the process being
              read. status costs one small read per process.
  Identity    (pid, start) as on macOS. start is the boot time from /proc/stat
              plus field 22 of /proc/<pid>/stat (clock ticks since boot), so it
              is epoch seconds with 10 ms resolution. The raw ticks are kept
              too, because Claude Code writes them as `procStart` on Linux.
  CPU         utime + stime in clock ticks; timebase() turns ticks into ns.
  Free %      MemAvailable / MemTotal. It is the kernel's own estimate of what
              can be handed out without swapping, the closest thing to macOS's
              kern.memorystatus_level, so the shared 20 % and 12 % thresholds
              keep their meaning.
  Paging      pswpin / pswpout from /proc/vmstat, in pages: the same swap-in
              and swap-out counters vm_stat reports.
  Kernel      PSI (/proc/pressure/memory) stands in for
  level       kern.memorystatus_vm_pressure_level. See psi_level().
  Boot        /proc/sys/kernel/random/boot_id, a new uuid at every boot.
"""

from __future__ import annotations

import os
import sys
import time

IS_LINUX = sys.platform.startswith("linux")
CLK_TCK = os.sysconf("SC_CLK_TCK") if IS_LINUX else 100
PAGE = os.sysconf("SC_PAGE_SIZE")
PROC = "/proc"

# Clocks with the macOS meanings. On a Mac, CLOCK_MONOTONIC and
# CLOCK_MONOTONIC_RAW keep counting through sleep and CLOCK_UPTIME_RAW stops.
# On Linux CLOCK_BOOTTIME counts through suspend and CLOCK_MONOTONIC stops.
CLOCK_SLEEPS = getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC)
CLOCK_AWAKE = time.CLOCK_MONOTONIC

# PSI thresholds, in percent of the last 10 seconds (avg10).
#   some: at least one task was stalled waiting for memory. 10 % means reclaim
#         is visibly slowing work; 25 % means most commands feel it.
#   full: every non-idle task was stalled at once. 5 % is already rare on a
#         healthy box; 15 % is the state just before the 17-Nov-2026 freezes
#         of devbox-derek, where sessions and ssh stopped answering.
PSI_SOME_WARN = 10.0
PSI_SOME_HIGH = 25.0
PSI_FULL_WARN = 5.0
PSI_FULL_CRIT = 15.0


# ------------------------------------------------------------------ files

def _read(path: str) -> str:
    with open(path) as fh:
        return fh.read()


def _kv_kb(text: str) -> dict:
    """`Key:   123 kB` lines as bytes (lines without kB as plain ints)."""
    out = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts or not parts[0].isdigit():
            continue
        n = int(parts[0])
        out[key.strip()] = n * 1024 if len(parts) > 1 and parts[1] == "kB" else n
    return out


def meminfo(text: str | None = None) -> dict:
    return _kv_kb(text if text is not None else _read(f"{PROC}/meminfo"))


def vmstat(text: str | None = None) -> dict:
    text = text if text is not None else _read(f"{PROC}/vmstat")
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def psi(text: str | None = None) -> dict:
    """{"some": avg10, "full": avg10} from /proc/pressure/memory, or {} when
    the kernel has PSI turned off."""
    try:
        text = text if text is not None else _read(f"{PROC}/pressure/memory")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        fields = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
        try:
            out[parts[0]] = float(fields["avg10"])
        except (KeyError, ValueError):
            continue
    return out


def psi_level(p: dict) -> int:
    """PSI as the macOS kernel level: 1 normal, 2 warning, 4 critical."""
    some, full = p.get("some", 0.0), p.get("full", 0.0)
    if full >= PSI_FULL_CRIT:
        return 4
    if some >= PSI_SOME_WARN or full >= PSI_FULL_WARN:
        return 2
    return 1


def psi_points(vm: dict) -> tuple:
    """Extra score points from PSI, and the reason. Only Linux rows carry
    psi_some, so a Mac reading scores exactly as before."""
    some, full = vm.get("psi_some"), vm.get("psi_full")
    if some is None and full is None:
        return 0, None
    some, full = some or 0.0, full or 0.0
    if full >= PSI_FULL_CRIT:
        return 4, f"memory stall {full:.0f}% (all tasks)"
    if full >= PSI_FULL_WARN or some >= PSI_SOME_HIGH:
        return 2, f"memory stall {max(some, full):.0f}%"
    if some >= PSI_SOME_WARN:
        return 1, f"memory stall {some:.0f}%"
    return 0, None


def free_pct(mi: dict) -> int:
    total = mi.get("MemTotal") or 0
    if total <= 0:
        raise ValueError("MemTotal missing")
    return int(mi.get("MemAvailable", 0) * 100 // total)


def boot_id() -> str | None:
    try:
        return _read(f"{PROC}/sys/kernel/random/boot_id").strip() or None
    except OSError:
        return None


_btime: int | None = None


def btime() -> int:
    """Boot time in epoch seconds, read once per process."""
    global _btime
    if _btime is None:
        for line in _read(f"{PROC}/stat").splitlines():
            if line.startswith("btime "):
                _btime = int(line.split()[1])
                break
        else:
            raise OSError("btime missing from /proc/stat")
    return _btime


def ticks_to_start(ticks: int) -> tuple:
    """Clock ticks since boot as (epoch sec, usec)."""
    return (btime() + ticks // CLK_TCK, (ticks % CLK_TCK) * (1_000_000 // CLK_TCK))


def parse_stat(text: str) -> dict:
    """/proc/<pid>/stat. comm sits in parentheses and may hold spaces or
    parentheses itself, so split at the last ')'."""
    lp, rp = text.index("("), text.rindex(")")
    rest = text[rp + 2:].split()
    # rest[0] is field 3 (state); field N is rest[N - 3].
    return {"comm": text[lp + 1:rp], "state": rest[0], "ppid": int(rest[1]),
            "pgid": int(rest[2]), "utime": int(rest[11]), "stime": int(rest[12]),
            "starttime": int(rest[19])}


# ------------------------------------------------------------------ system

def read_vm() -> dict:
    """The keys memmon.read_vm() fills on macOS, from /proc."""
    mi, vs, p = meminfo(), vmstat(), psi()
    total = mi.get("MemTotal", 0)
    avail = mi.get("MemAvailable", 0)
    vm = {
        "swap_total": mi.get("SwapTotal", 0),
        "swap_used": max(0, mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)),
        "free_pct": free_pct(mi),
        "kernel_level": {1: "normal", 2: "warning", 4: "critical"}[psi_level(p)],
        "boot": boot_id(),
        "ram_total": total,
        "ram_used": max(0, total - avail),
        "wired": mi.get("Unevictable", 0) + mi.get("Mlocked", 0),
        "compressor": 0,
        "unused": mi.get("MemFree", 0),
        "load": os.getloadavg()[0],
        "nprocs": sum(1 for n in os.listdir(PROC) if n.isdigit()),
        "swapins": vs.get("pswpin", 0),
        "swapouts": vs.get("pswpout", 0),
        "pageins": vs.get("pgmajfault", 0),
        "pageouts": vs.get("pgpgout", 0) * 1024 // PAGE,
        "ncpu": os.cpu_count() or 8,
        "psi_some": p.get("some"),
        "psi_full": p.get("full"),
        "page_size": PAGE,
    }
    # No compressor on Linux: what is not in RAM is in swap, all of it.
    vm["swap_frac"] = 1.0 if vm["swap_used"] else 0.0
    return vm


def counters() -> dict:
    """memmon_telemetry's vm counters: pages, plus used bytes."""
    mi, vs = meminfo(), vmstat()
    return {"pageins": vs.get("pgmajfault", 0),
            "pageouts": vs.get("pgpgout", 0) * 1024 // PAGE,
            "swapins": vs["pswpin"], "swapouts": vs["pswpout"], "page_size": PAGE,
            "used_bytes": max(0, mi["MemTotal"] - mi["MemAvailable"])}


def read_system_strict() -> dict:
    """memmon_procs.read_system_strict() on Linux. Raises on a missing field."""
    mi = meminfo()
    for key in ("MemTotal", "MemAvailable", "MemFree"):
        if key not in mi:
            raise KeyError(f"/proc/meminfo missing {key}")
    ram, avail = mi["MemTotal"], mi["MemAvailable"]
    used = max(0, ram - avail)
    idle = min(mi["MemFree"], ram - used)
    return {"ram_bytes": ram, "used_bytes": used,
            "pressure_level": {1: "normal", 2: "warning", 4: "critical"}[psi_level(psi())],
            "idle_bytes": idle, "cache_bytes": max(ram - used - idle, 0)}


# ------------------------------------------------------------------ processes

def top_table(limit: int = 300) -> dict:
    """memmon.read_top()'s {pid: {"mem", "cmprs"}} for this user's processes,
    largest first. cmprs is the swapped part (Linux has no compressor)."""
    me, rows = os.getuid(), []
    for name in os.listdir(PROC):
        if not name.isdigit():
            continue
        try:
            if os.stat(f"{PROC}/{name}").st_uid != me:
                continue
            st = _kv_kb(_read(f"{PROC}/{name}/status"))
        except OSError:
            continue
        if "VmRSS" not in st:
            continue
        swap = st.get("VmSwap", 0)
        rows.append((int(name), st.get("RssAnon", 0) + swap, swap))
    rows.sort(key=lambda r: -r[1])
    return {pid: {"mem": mem, "cmprs": swap} for pid, mem, swap in rows[:limit]}


def ps_table() -> dict:
    """memmon.read_ps()'s {pid: {"ppid", "age", "cmd"}}."""
    out, now = {}, time.time()
    for name in os.listdir(PROC):
        if not name.isdigit():
            continue
        try:
            st = parse_stat(_read(f"{PROC}/{name}/stat"))
            with open(f"{PROC}/{name}/cmdline", "rb") as fh:
                raw = fh.read()
        except (OSError, ValueError):
            continue
        cmd = " ".join(a.decode("utf-8", "replace") for a in raw.split(b"\0") if a)
        start = ticks_to_start(st["starttime"])
        out[int(name)] = {"ppid": st["ppid"], "age": int(max(0, now - start[0])),
                          "cmd": cmd or f"[{st['comm']}]"}
    return out


def nook_session(pid: int) -> str | None:
    """The Nook session id (s3, s4, ...) a process runs under, from its
    environment, or None. Only this user's processes are readable."""
    try:
        with open(f"{PROC}/{pid}/environ", "rb") as fh:
            env = fh.read()
    except OSError:
        return None
    for item in env.split(b"\0"):
        if item.startswith(b"NOOK_SESSION="):
            return item[13:].decode("utf-8", "replace") or None
    return None


_reapers: tuple = (0.0, frozenset({1}))


def reapers() -> frozenset:
    """PIDs an orphan is handed to. On macOS that is launchd, pid 1. On Linux
    a dead parent's children go to the nearest child subreaper, which under a
    login session is the user's own `systemd --user` manager, not pid 1. The
    kernel exposes no subreaper flag in /proc, so this lists pid 1 plus every
    process named systemd whose parent is pid 1. Cached for 5 s."""
    global _reapers
    now = time.monotonic()
    if now - _reapers[0] < 5.0:
        return _reapers[1]
    out = {1}
    for name in os.listdir(PROC):
        if not name.isdigit():
            continue
        try:
            st = parse_stat(_read(f"{PROC}/{name}/stat"))
        except (OSError, ValueError):
            continue
        if st["ppid"] == 1 and st["comm"] == "systemd":
            out.add(int(name))
    _reapers = (now, frozenset(out))
    return _reapers[1]
