"""Strict memory telemetry for admission, and the clocks it is measured on.

pressure() in memmon.py is advisory: a failed read there defaults to a
healthy-looking value, which is right for a fail-open gate and wrong for
admission. Everything here raises instead. Nothing goes through memmon's
_sh, which swallows errors, and the module imports nothing from memmon, so
the runner and the sampler can both load it cheaply.

Clocks, all injectable through Clock:
  mono    CLOCK_MONOTONIC. On macOS it keeps counting while asleep, so a
          deadline measured on it is wall time. Deadlines, freshness and
          hysteresis use it.
  awake   time.monotonic (mach_absolute_time), which stops while asleep. A
          wake shows up as mono advancing more than WAKE_S beyond it.
  raw     CLOCK_MONOTONIC_RAW and uptime CLOCK_UPTIME_RAW: the sampler's gap
          record, where raw - uptime is the time asleep.
  boot    kern.bootsessionuuid. Every clock above restarts at boot.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import threading
import time

MB = 1024 * 1024
HEADROOM_FLOOR = 20          # the free-% level the runway projects toward
RATE_MIN_S = 2.0             # a rate over less than this is noise
BASELINE_MAX_S = 300.0       # an older baseline only re-seeds
FREE_DELTA_MIN_S = 30.0      # free_pct is whole percent: 1 point over 2 s reads as 30 %/min
CACHE_FRESH_S = 5.0          # cached rates older than this are a failed read
FRESH_S = 5.0                # a reading admits only while this young
WAKE_S = 5.0
GAP_S = 150.0
RECOVERY_S = 30.0
GOOD_GAP_S = 4.0             # a longer silence between good readings restarts recovery

PRESSURE_LEVELS = {1: "normal", 2: "warning", 4: "critical"}
_COUNTERS = {"Pageins": "pageins", "Pageouts": "pageouts",
             "Swapins": "swapins", "Swapouts": "swapouts"}
_USED_KEYS = ("Anonymous pages", "Pages purgeable", "Pages wired down",
              "Pages occupied by compressor")


class TelemetryError(Exception):
    """The reading cannot be trusted. Admission holds; it never defaults."""


# ------------------------------------------------------------------ clocks

def _libc():
    lib = ctypes.CDLL(None, use_errno=True)
    lib.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_void_p, ctypes.c_size_t]
    return lib


_LIBC = None


def libc():
    global _LIBC
    if _LIBC is None:
        _LIBC = _libc()
    return _LIBC


def sysctl_raw(name: str, size: int) -> bytes:
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(size)
    if libc().sysctlbyname(name.encode(), buf, ctypes.byref(n), None, ctypes.c_size_t(0)) != 0:
        raise TelemetryError(f"sysctl {name} failed (errno {ctypes.get_errno()})")
    return buf.raw[:n.value]


def sysctl_int(name: str) -> int:
    raw = sysctl_raw(name, 8)
    if len(raw) not in (4, 8):
        raise TelemetryError(f"sysctl {name}: unexpected size {len(raw)}")
    return int.from_bytes(raw, "little", signed=len(raw) == 4)


def boot_session() -> str | None:
    try:
        return sysctl_raw("kern.bootsessionuuid", 64).split(b"\0", 1)[0].decode() or None
    except Exception:
        return None


class Clock:
    def mono(self) -> float:
        return time.clock_gettime(time.CLOCK_MONOTONIC)

    def awake(self) -> float:
        return time.monotonic()

    def raw(self) -> float:
        return time.clock_gettime(time.CLOCK_MONOTONIC_RAW)

    def uptime(self) -> float:
        return time.clock_gettime(time.CLOCK_UPTIME_RAW)

    def wall(self) -> float:
        return time.time()

    def boot(self) -> str | None:
        return boot_session()

    def sleep(self, s: float) -> None:
        time.sleep(max(0.0, s))


SYSTEM_CLOCK = Clock()


# ------------------------------------------------------------------ source

class _XswUsage(ctypes.Structure):
    _fields_ = [("total", ctypes.c_uint64), ("avail", ctypes.c_uint64),
                ("used", ctypes.c_uint64), ("pagesize", ctypes.c_uint32),
                ("encrypted", ctypes.c_int32)]


class TelemetrySource:
    """The kernel inputs pressure() reads, each read strictly."""

    def pressure_level(self) -> int:
        return sysctl_int("kern.memorystatus_vm_pressure_level")

    def free_pct(self) -> int:
        return sysctl_int("kern.memorystatus_level")

    def memsize(self) -> int:
        return sysctl_int("hw.memsize")

    def swapusage(self) -> tuple:
        raw = sysctl_raw("vm.swapusage", ctypes.sizeof(_XswUsage))
        if len(raw) != ctypes.sizeof(_XswUsage):
            raise TelemetryError("vm.swapusage: unexpected size")
        x = _XswUsage.from_buffer_copy(raw)
        return int(x.total), int(x.used)

    def loadavg(self) -> float:
        return os.getloadavg()[0]

    def ncpu(self) -> int:
        n = os.cpu_count()
        if not n:
            raise TelemetryError("cpu count unavailable")
        return n

    def vm_stat(self, timeout: float) -> str:
        try:
            proc = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TelemetryError(f"vm_stat timed out after {timeout:g}s")
        except OSError as exc:
            raise TelemetryError(f"vm_stat failed: {exc}")
        if proc.returncode != 0:
            raise TelemetryError(f"vm_stat exited {proc.returncode}")
        return proc.stdout


DEFAULT_SOURCE = TelemetrySource()


def _bounded(fn, budget_s: float):
    """Run fn with a hard wall budget. A hung read (a stuck sysctl, a fake
    that never returns) leaves a daemon thread behind, never a caller that
    holds ledger.lock past its bound."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:          # re-raised in the caller
            box["error"] = exc
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(budget_s)
    if t.is_alive():
        raise TelemetryError(f"telemetry read exceeded {budget_s:g}s")
    if "error" in box:
        err = box["error"]
        if isinstance(err, TelemetryError):
            raise err
        raise TelemetryError(f"{type(err).__name__}: {err}") from err
    return box["value"]


def parse_vm_stat(text: str) -> dict:
    m = re.search(r"page size of (\d+) bytes", text or "")
    if not m:
        raise TelemetryError("vm_stat: no page size")
    counts = {}
    for line in text.splitlines():
        km = re.match(r'"?([^:"]+)"?:\s+(\d+)\.?\s*$', line.strip())
        if km:
            counts[km.group(1).strip()] = int(km.group(2))
    missing = [k for k in (*_COUNTERS, *_USED_KEYS) if k not in counts]
    if missing:
        raise TelemetryError(f"vm_stat missing {', '.join(missing)}")
    page = int(m.group(1))
    out = {v: counts[k] for k, v in _COUNTERS.items()}
    out["page_size"] = page
    out["used_bytes"] = page * (counts["Anonymous pages"] - counts["Pages purgeable"]
                                + counts["Pages wired down"]
                                + counts["Pages occupied by compressor"])
    return out


def _instant(source, clock) -> dict:
    level = source.pressure_level()
    if level not in PRESSURE_LEVELS:
        raise TelemetryError(f"unknown kernel pressure level {level}")
    free = source.free_pct()
    if not 0 <= free <= 100:
        raise TelemetryError(f"memorystatus_level out of range: {free}")
    swap_total, swap_used = source.swapusage()
    ram = source.memsize()
    if ram <= 0:
        raise TelemetryError("hw.memsize is zero")
    return {"pressure_level": PRESSURE_LEVELS[level], "free_pct": free,
            "ram_total": ram, "swap_total": swap_total, "swap_used": swap_used,
            "load": float(source.loadavg()), "ncpu": int(source.ncpu()),
            "boot": clock.boot()}


def read_instant(source=None, clock=None, budget_s: float = 2.0) -> dict:
    """The sysctl-only signals (free %, swap, load, kernel level): the
    sampler's lower bound when vm_stat fails. No counters, so no rates."""
    source, clock = source or DEFAULT_SOURCE, clock or SYSTEM_CLOCK

    def go():
        out = _instant(source, clock)
        out.update(mono=clock.mono(), awake=clock.awake(), wall=clock.wall())
        return out
    return _bounded(go, budget_s)


def read_pressure_strict(source=None, clock=None, vm_stat_timeout: float = 2.0,
                         budget_s: float = 2.0) -> dict:
    """Every input pressure() reads, or TelemetryError: a timeout, a non-zero
    exit, a parse failure or a missing field all raise. `mono` is when the
    counters were taken (the vm_stat return)."""
    source, clock = source or DEFAULT_SOURCE, clock or SYSTEM_CLOCK
    start = clock.mono()

    def go():
        out = _instant(source, clock)
        left = budget_s - (clock.mono() - start)
        if left <= 0:
            raise TelemetryError(f"telemetry read exceeded {budget_s:g}s")
        out.update(parse_vm_stat(source.vm_stat(min(vm_stat_timeout, left))))
        out.update(mono=clock.mono(), awake=clock.awake(), wall=clock.wall())
        return out
    reading = _bounded(go, budget_s + 0.05)
    if reading["mono"] - start > budget_s:
        raise TelemetryError(f"telemetry read exceeded {budget_s:g}s")
    return reading


# ------------------------------------------------------------------ rates

def rates_between(prev: dict, cur: dict) -> dict:
    """Paging and swap-growth rates over a baseline at least RATE_MIN_S old."""
    dt = cur["mono"] - prev["mono"]
    if dt < RATE_MIN_S:
        raise ValueError(f"baseline only {dt:.2f}s old")
    page = cur.get("page_size") or prev.get("page_size")
    return {
        "swapin_mbs": max(0, cur["swapins"] - prev["swapins"]) * page / dt / 1e6,
        "swapout_mbs": max(0, cur["swapouts"] - prev["swapouts"]) * page / dt / 1e6,
        "swap_growth_mbmin": (cur["swap_used"] - prev["swap_used"]) / MB * 60.0 / dt,
    }


def free_delta_between(prev: dict | None, cur: dict) -> float | None:
    """free_pct change per minute, only over a 30-300 s baseline from the
    same boot. Anything shorter turns one whole-percent tick into a slope."""
    if not prev or prev.get("boot") != cur.get("boot") or prev.get("free_pct") is None:
        return None
    dt = cur["mono"] - prev["mono"]
    if not FREE_DELTA_MIN_S <= dt <= BASELINE_MAX_S:
        return None
    return (cur["free_pct"] - prev["free_pct"]) * 60.0 / dt


def score(vm: dict, rates: dict | None, free_delta_min: float | None,
          prev_lh_streak: int = 0, level_reason: str | None = None,
          advance: bool = True) -> dict:
    """The legacy pressure() verdict for the same inputs (B26 parity).

    rates None: only instantaneous signals score, and a result that would be
    HEALTHY is UNKNOWN instead (I-13); WATCH or worse stands as a lower bound.
    free_delta_min None: no runway, and the low-headroom streak passes through
    unchanged. advance=False re-scores a cached reading without moving the
    streak a second time."""
    r = rates or {}
    swapin, swapout = r.get("swapin_mbs", 0.0), r.get("swapout_mbs", 0.0)
    growth = r.get("swap_growth_mbmin", 0.0)
    free = vm.get("free_pct", 100)
    thrash = swapin + swapout
    swap_ratio = vm.get("swap_used", 0) / max(vm.get("ram_total", 1), 1)
    reasons, pts = [], 0

    if swap_ratio >= 1.0:
        pts += 4; reasons.append(f"swap {swap_ratio:.1f}x RAM size")
    elif swap_ratio >= 0.5:
        pts += 2; reasons.append(f"swap {swap_ratio:.1f}x RAM size")
    elif swap_ratio >= 0.25:
        pts += 1; reasons.append(f"swap {int(swap_ratio * 100)}% of RAM size")

    if thrash >= 150:
        pts += 4; reasons.append(f"heavy thrashing {thrash:.0f} MB/s")
    elif thrash >= 50:
        pts += 2; reasons.append(f"paging {thrash:.0f} MB/s")
    elif thrash >= 10:
        pts += 1; reasons.append(f"paging {thrash:.0f} MB/s")

    if free <= 12:
        pts += 3; reasons.append(f"kernel headroom down to {free}%")
    elif free <= 20:
        pts += 2; reasons.append(f"kernel headroom {free}%")

    if growth >= 500:
        pts += 2; reasons.append(f"swap growing {growth:.0f} MB/min")
    elif growth >= 150:
        pts += 1; reasons.append(f"swap growing {growth:.0f} MB/min")

    ncpu = vm.get("ncpu", 8)
    load_ratio = vm.get("load", 0) / max(ncpu, 1)
    if load_ratio >= 3:
        pts += 2; reasons.append(f"load {vm.get('load', 0):.0f} on {ncpu} cores")
    elif load_ratio >= 1.75:
        pts += 1; reasons.append(f"load {vm.get('load', 0):.0f}")

    headroom = None
    if free_delta_min is not None:
        drop = -free_delta_min
        if drop > 0.5 and free > HEADROOM_FLOOR:
            headroom = (free - HEADROOM_FLOOR) / drop

    if pts >= 7:
        level, color = "CRITICAL", "red"
    elif pts >= 4:
        level, color = "DANGER", "red"
    elif pts >= 2:
        level, color = "WATCH", "yellow"
    else:
        level, color = "HEALTHY", "green"

    low = headroom is not None and headroom < 5
    if free_delta_min is None or not advance:
        streak = prev_lh_streak
    else:
        streak = prev_lh_streak + 1 if low else 0
    if low and streak >= 2 and level == "WATCH":
        level, color = "DANGER", "red"
        reasons.append(f"headroom falling, ~{headroom:.0f} min to {HEADROOM_FLOOR}%")

    nxt, to_next = None, None
    for name, need in (("WATCH", 2), ("DANGER", 4), ("CRITICAL", 7)):
        if pts < need:
            nxt, to_next = name, need - pts
            break
    if level == "HEALTHY":
        headroom = None
    if rates is None and level == "HEALTHY":
        level, color = "UNKNOWN", "grey"
        level_reason = level_reason or "no rate baseline"
    return {"level": level, "color": color, "score": pts, "reasons": reasons,
            "headroom_min": headroom, "next_level": nxt, "to_next": to_next,
            "lh_streak": streak, "thrash_mbs": thrash, "swapin_mbs": swapin,
            "swapout_mbs": swapout, "swap_growth_mbmin": growth,
            "free_delta_min": free_delta_min,
            "rates": "ok" if rates is not None else "unavailable",
            "level_reason": level_reason if rates is None else None}


# ------------------------------------------------------------------ gaps

def gap_record(prev: dict | None, cur: dict, threshold_s: float = GAP_S) -> dict | None:
    """Why rows are missing between prev and cur, or None when they are not.
    mono is CLOCK_MONOTONIC_RAW (counts sleep), uptime CLOCK_UPTIME_RAW (does
    not), so mono - uptime is the time asleep inside the gap."""
    if not prev or prev.get("mono") is None:
        return None
    if prev.get("boot") != cur.get("boot"):
        return {"cause": "reboot", "gap_s": None, "asleep_s": None, "awake_s": None}
    dm = cur["mono"] - prev["mono"]
    if dm <= threshold_s:
        return None
    du = max(0.0, min(dm, cur["uptime"] - prev["uptime"]))
    asleep = dm - du
    out = {"cause": "sleep" if du <= threshold_s else "starved",
           "gap_s": round(dm, 1), "asleep_s": round(asleep, 1), "awake_s": round(du, 1)}
    return out


# ----------------------------------------------- shared rate cache (S2.1)

def _counters(r: dict) -> dict:
    return {k: r[k] for k in ("mono", "boot", "swapins", "swapouts", "swap_used",
                              "page_size", "free_pct")}


def fresh_state(boot: str | None) -> dict:
    return {"boot_id": boot, "last_bad_mono": None, "last_good_mono": None,
            "recovery_start_mono": None, "last_wake_mono": None, "clock_pair": None,
            "rate_baseline": None, "free_baseline": None, "rates": None,
            "rates_mono": None, "free_delta_min": None, "lh_streak": 0}


def note_clocks(state: dict, now_mono: float, now_awake: float) -> dict:
    """Detect a wake from the shared (mono, awake) pair. Both clocks are
    system-wide, so a pair one process stored is comparable in another."""
    pair = state.get("clock_pair")
    if pair and (now_mono - pair[0]) - (now_awake - pair[1]) > WAKE_S:
        state["last_wake_mono"] = now_mono
        # The window restarts from the first good reading after the wake.
        state["last_bad_mono"] = now_mono
        state["recovery_start_mono"] = None
    state["clock_pair"] = [now_mono, now_awake]
    return state


def strict_verdict(state: dict, reading: dict | None, now_mono: float,
                   error: str | None = None) -> tuple:
    """Turn one strict reading into a verdict through the shared baseline.
    The caller holds ledger.lock and persists `state`. Returns (state,
    verdict); verdict["ok"] is False for anything that must hold."""
    def failed(reason):
        return state, {"ok": False, "reason": reason, "level": None,
                       "pressure_level": (reading or {}).get("pressure_level"),
                       "mono": (reading or {}).get("mono")}

    if reading is None:
        return failed(error or "telemetry unavailable")
    if state.get("boot_id") != reading.get("boot") or reading.get("boot") is None:
        state = fresh_state(reading.get("boot"))
        state["rate_baseline"] = _counters(reading)
        state["free_baseline"] = _counters(reading)
        return failed("new boot: rate baseline seeded")
    wake = state.get("last_wake_mono")
    if wake is not None and reading["mono"] < wake:
        return failed("reading predates the last wake")

    base = state.get("rate_baseline")
    fd_base = state.get("free_baseline")
    stale_base = (base is None or base.get("boot") != reading["boot"]
                  or reading["mono"] - base["mono"] > BASELINE_MAX_S
                  or (wake is not None and base["mono"] < wake))
    if fd_base is None or fd_base.get("boot") != reading["boot"] or (
            wake is not None and fd_base["mono"] < wake) or (
            reading["mono"] - fd_base["mono"] > BASELINE_MAX_S):
        state["free_baseline"] = fd_base = _counters(reading)
    if stale_base:
        state["rate_baseline"] = _counters(reading)
        return failed("rate baseline re-seeded")

    advance = False
    if reading["mono"] - base["mono"] >= RATE_MIN_S:
        rates = rates_between(base, reading)
        state["rate_baseline"] = _counters(reading)
        fd = None
        if reading["mono"] - fd_base["mono"] >= FREE_DELTA_MIN_S:
            fd = free_delta_between(fd_base, reading)
            state["free_baseline"] = _counters(reading)
        state.update(rates=rates, rates_mono=reading["mono"])
        advance = fd is not None
        if advance:
            state["free_delta_min"] = fd
    elif state.get("rates") is not None and state.get("rates_mono") is not None and (
            now_mono - state["rates_mono"] <= CACHE_FRESH_S):
        rates = state["rates"]
    else:
        return failed("rate cache stale")

    v = score(reading, rates, state.get("free_delta_min"), state.get("lh_streak", 0),
              advance=advance)
    if advance:
        state["lh_streak"] = v["lh_streak"]
    v.update(ok=True, reason=None, pressure_level=reading["pressure_level"],
             mono=reading["mono"], used_bytes=reading.get("used_bytes"),
             ram_total=reading.get("ram_total"))
    if now_mono - reading["mono"] > FRESH_S:
        v.update(ok=False, reason="reading is stale")
    return state, v


def is_good(verdict: dict | None) -> bool:
    return bool(verdict and verdict.get("ok") and verdict.get("level") in ("HEALTHY", "WATCH")
                and verdict.get("pressure_level") == "normal")


def apply_hysteresis(state: dict, good: bool, sample_mono: float, now_mono: float) -> dict:
    """S2.4 shared hysteresis, applied in sample-time order. A late bad
    sample still closes a window that started before it; a late good sample
    never opens one."""
    last_bad = state.get("last_bad_mono")
    if not good:
        state["last_bad_mono"] = sample_mono if last_bad is None else max(last_bad, sample_mono)
        state["recovery_start_mono"] = None
        return state
    if last_bad is not None and sample_mono <= last_bad:
        return state
    prev_good = state.get("last_good_mono")
    if (state.get("recovery_start_mono") is None or prev_good is None
            or sample_mono - prev_good > GOOD_GAP_S):
        state["recovery_start_mono"] = sample_mono
    state["last_good_mono"] = sample_mono if prev_good is None else max(prev_good, sample_mono)
    return state


def recovery(state: dict, now_mono: float, window_s: float = RECOVERY_S) -> dict:
    start = state.get("recovery_start_mono")
    bad = state.get("last_bad_mono")
    if start is None or (bad is not None and bad >= start):
        return {"open": False, "remaining_s": None}
    left = window_s - (now_mono - start)
    return {"open": left <= 0, "remaining_s": max(0.0, round(left, 1))}
