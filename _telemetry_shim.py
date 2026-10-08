"""Stand-in for memmon_telemetry until that module lands. Same names, same
contract: every reader raises instead of defaulting. Delete this file once
memmon_telemetry.py is in the tree."""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import time

import memmon_procs

KERNEL_LEVELS = memmon_procs.PRESSURE_LEVELS


class XswUsage(ctypes.Structure):
    _fields_ = [("total", ctypes.c_uint64), ("avail", ctypes.c_uint64),
                ("used", ctypes.c_uint64), ("pagesize", ctypes.c_uint32),
                ("encrypted", ctypes.c_bool)]


def mono() -> float:
    return time.clock_gettime(time.CLOCK_MONOTONIC_RAW)


def uptime() -> float:
    return time.clock_gettime(time.CLOCK_UPTIME_RAW)


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_size_t]
    return libc


def _sysctl_raw(name: str, buf, libc=None) -> int:
    libc = libc or _libc()
    size = ctypes.c_size_t(ctypes.sizeof(buf))
    if libc.sysctlbyname(name.encode(), ctypes.byref(buf), ctypes.byref(size),
                         None, ctypes.c_size_t(0)) != 0:
        raise OSError(ctypes.get_errno(), f"sysctl {name} failed")
    return size.value


def boot() -> str:
    buf = ctypes.create_string_buffer(64)
    _sysctl_raw("kern.bootsessionuuid", buf)
    value = buf.value.decode()
    if not value:
        raise ValueError("kern.bootsessionuuid is empty")
    return value


def read_sysctls() -> dict:
    libc = _libc()
    level = memmon_procs.sysctl_u64("kern.memorystatus_vm_pressure_level", libc)
    if level not in KERNEL_LEVELS:
        raise ValueError(f"unknown pressure level {level}")
    swap = XswUsage()
    if _sysctl_raw("vm.swapusage", swap, libc) < 24:
        raise ValueError("vm.swapusage: short read")
    return {"kernel_level": KERNEL_LEVELS[level],
            "free_pct": memmon_procs.sysctl_u64("kern.memorystatus_level", libc),
            "swap_total": swap.total, "swap_used": swap.used,
            "ram_total": memmon_procs.sysctl_u64("hw.memsize", libc),
            "load": os.getloadavg()[0], "ncpu": os.cpu_count() or 8}


_VM_KEYS = ("Swapins", "Swapouts", "Pageins", "Pageouts", "Anonymous pages",
            "Pages purgeable", "Pages wired down", "Pages occupied by compressor")


def read_vm_stat(timeout: float = 2.0, run=subprocess.run) -> dict:
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
    used = (counts["Anonymous pages"] - counts["Pages purgeable"]
            + counts["Pages wired down"] + counts["Pages occupied by compressor"])
    return {"page_size": page, "swapins": counts["Swapins"],
            "swapouts": counts["Swapouts"], "pageins": counts["Pageins"],
            "pageouts": counts["Pageouts"], "used_bytes": used * page}


def read_pressure_strict(vm_stat_timeout: float = 2.0) -> dict:
    return {**read_sysctls(), **read_vm_stat(vm_stat_timeout)}
