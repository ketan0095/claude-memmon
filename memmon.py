#!/usr/bin/env python3
"""memmon — live RAM/swap monitor that attributes memory to named Claude sessions.

Why this exists: `ps` RSS under-reports by ~25x on Apple Silicon once pages are
compressed or swapped (a 3.5G tsc process shows as 133M). Everything here reads
`top`'s MEM+CMPRS columns instead, which is what Activity Monitor shows.

Modes:
  memmon                 live dashboard (default)
  memmon --once          one snapshot, then exit
  memmon --json          machine-readable snapshot
  memmon --statusline    single line, for the Claude Code statusline
  memmon --log           append a sample to history (for launchd/cron)
  memmon --report        per-owner averages from logged history
  memmon --reap          list reclaimable orphans (add --apply to stop them)
  memmon owners --json   every process partitioned into one owner (schema 2)
  memmon act ACTION --target TOKEN   identity-checked stop of one owner or job
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
# argparse and shutil are imported lazily where used. Neither is reachable
# from gate(), which runs on every Bash tool call; importing them unconditionally
# measured ~6ms of its ~85ms budget, and shutil alone pulls in bz2 and lzma.
from collections import defaultdict

from memmon_common import spare_is_idle

HOME = os.path.expanduser("~")
JOBS_DIR = os.path.join(HOME, ".claude", "jobs")
STATE_DIR = os.path.join(HOME, ".claude", "memmon")
HISTORY = os.path.join(STATE_DIR, "history.jsonl")
SNAPSHOT = os.path.join(STATE_DIR, "latest.json")
# The one state file written from three functions was the only one
# without a constant.
GATE_LOG = os.path.join(STATE_DIR, "gate.jsonl")
# Owner view state. Only the sampler writes these two.
OWNERS_HISTORY = os.path.join(STATE_DIR, "owners-history.json")
CPU_BASELINE = os.path.join(STATE_DIR, "cpu-baseline.json")
# Serialises every stop, including the menu bar's quit-app, across processes.
ACTIONS_LOCK = os.path.join(STATE_DIR, "runner", "coord", "actions.lock")
# Sampler state (S2.10, S2.11). The sampler is the only writer of all three.
PRESSURE_FILE = os.path.join(STATE_DIR, "pressure.json")
JOB_HISTORY = os.path.join(STATE_DIR, "job-history.json")
PRESSURE_EPISODE = os.path.join(STATE_DIR, "runner", "coord", "pressure-episode.json")
CLAUDE_SESSIONS_DIR = os.path.join(HOME, ".claude", "sessions")
CC_SOCKS_DIR = "/tmp/cc-socks"
CLAUDE_ROSTER = os.path.join(HOME, ".claude", "daemon", "roster.json")
CODEX_HOME = os.path.join(HOME, ".codex")

# A process is a reap candidate only if it matches one of these shapes. Being an
# orphan is not enough on its own — plenty of legitimate daemons have ppid 1.
REAPABLE = re.compile(
    r"(typescript/bin/tsc|/turbo/bin/turbo|turbo-darwin|"
    r"esbuild|jest-worker|vitest|next-server|webpack)"
)
SESSION_ID_RE = re.compile(r"--session-id\s+([0-9a-f-]{36})")

# Project layout differs per machine, so the two patterns that encode it are
# configurable. Write ~/.claude/memmon/config.json to override:
#   {"project_roots": ["~/code", "~/Desktop/Work"],
#    "worktree_pattern": "monorepo(?:-([A-Za-z0-9._-]+))?",
#    "ticket_pattern": "[A-Z]{2,6}-\\d+"}
DEFAULT_CONFIG = {
    # Directories whose immediate children are checkouts/worktrees. Preferred
    # over the regex because it needs no naming convention at all.
    "project_roots": [],
    "worktree_pattern": r"monorepo(?:-([A-Za-z0-9._-]+))?",
    "ticket_pattern": r"[A-Z]{2,6}-\d+",
    # memmon run's admission limit is RAM x (1 - headroom_frac).
    "headroom_frac": 0.20,
    # false turns S2.11 off: no suggestion scan, card, notification or naming.
    "pressure_suggestions": True,
    # false silences the sampler's notifications (and MemmonBar's).
    "notifications": True,
    # The gate's policy when MEMMON_GATE is not set in the hook's environment.
    "gate_mode": None,
}


def _load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(os.path.join(STATE_DIR, "config.json")) as fh:
            cfg.update(json.load(fh))
    except Exception:
        pass
    cfg["project_roots"] = [os.path.expanduser(p).rstrip("/")
                            for p in cfg.get("project_roots") or []]
    return cfg


CONFIG = _load_config()
try:
    WORKTREE_RE = re.compile(CONFIG["worktree_pattern"])
    TICKET_RE = re.compile("(" + CONFIG["ticket_pattern"] + ")")
except re.error:
    WORKTREE_RE = re.compile(DEFAULT_CONFIG["worktree_pattern"])
    TICKET_RE = re.compile("(" + DEFAULT_CONFIG["ticket_pattern"] + ")")

MB = 1024 * 1024
GB = 1024 * MB


# ---------------------------------------------------------------- collectors

def _sh(cmd: list[str], timeout: int = 15) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        ).stdout
    except Exception:
        return ""


def parse_size(tok: str) -> int:
    """Parse top's size tokens: 3510M, 2.1G, 512K, 0B."""
    tok = tok.strip().rstrip("+-")
    if not tok or tok == "N/A":
        return 0
    mult = {"B": 1, "K": 1024, "M": MB, "G": GB, "T": 1024 * GB}.get(tok[-1].upper())
    if mult is None:
        try:
            return int(float(tok))
        except ValueError:
            return 0
    try:
        return int(float(tok[:-1]) * mult)
    except ValueError:
        return 0


def read_top(limit: int = 300) -> tuple[dict[int, dict], str]:
    """True per-process memory, plus the header block `top` prints above it.

    Returns the header so callers do not spawn a second `top -l 1 -n 0` purely
    for PhysMem/Load Avg — that second spawn cost ~445ms, as much as this one.
    MEM is the footprint; CMPRS is how much of it has been compressed."""
    out = _sh(["top", "-l", "1", "-n", str(limit), "-o", "mem",
               "-stats", "pid,mem,cmprs"])
    procs: dict[int, dict] = {}
    started = False
    for line in out.splitlines():
        if line.startswith("PID"):
            started = True
            continue
        if not started:
            continue
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        procs[int(parts[0])] = {
            "mem": parse_size(parts[1]),
            "cmprs": parse_size(parts[2]),
        }
    return procs, out


def etime_to_sec(s: str) -> int:
    s = s.strip()
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    bits = [int(x) for x in s.split(":")]
    while len(bits) < 3:
        bits.insert(0, 0)
    return days * 86400 + bits[0] * 3600 + bits[1] * 60 + bits[2]


def read_ps() -> dict[int, dict]:
    out = _sh(["ps", "-Ao", "pid=,ppid=,etime=,command="])
    procs: dict[int, dict] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        try:
            age = etime_to_sec(parts[2])
        except Exception:
            age = 0
        procs[int(parts[0])] = {
            "ppid": int(parts[1]),
            "age": age,
            "cmd": parts[3],
        }
    return procs


KERNEL_LEVELS = {1: "normal", 2: "warning", 4: "critical"}


def read_vm(fast: bool = False, header: str = "", vm_stat_text: str | None = None) -> dict:
    """System-wide memory picture.

    free_pct is `kern.memorystatus_level` — NOT "unused RAM". macOS deliberately
    uses nearly all memory for cache and the compressor, so unused RAM is always
    near zero and means nothing. memorystatus_level is the kernel's own headroom
    figure, the one it consults when deciding whether to start killing
    processes, which is why it is the only free-memory number worth scoring.

    `fast` skips the `top` header, which costs ~400ms — far too slow for the
    PreToolUse gate, which runs on every tool call. Everything the pressure
    model needs is available from sysctl and vm_stat in ~10ms; only the
    cosmetic ram_used/compressor/nprocs figures require top."""
    vm: dict = {}
    # One sysctl spawn for both values, and os.* for the three that are constants
    # for the life of the machine. This runs on every gated Bash command, where
    # six forks measured 18ms of the ~85ms budget.
    # The boot session and the kernel's own level ride on the same spawn: a
    # rate baseline is only valid within one boot, and S2.11's trigger reads
    # the kernel level when the rates are unavailable.
    out = _sh(["sysctl", "vm.swapusage", "kern.memorystatus_level",
               "kern.memorystatus_vm_pressure_level", "kern.bootsessionuuid"])
    sysctls = dict(line.split(": ", 1) for line in out.splitlines() if ": " in line)
    m = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M\s+free = ([\d.]+)M",
                  sysctls.get("vm.swapusage", ""))
    if m:
        vm["swap_total"] = int(float(m.group(1)) * MB)
        vm["swap_used"] = int(float(m.group(2)) * MB)
    lvl = sysctls.get("kern.memorystatus_level", "").strip()
    vm["free_pct"] = int(lvl) if lvl.isdigit() else 0
    kern = sysctls.get("kern.memorystatus_vm_pressure_level", "").strip()
    vm["kernel_level"] = KERNEL_LEVELS.get(int(kern)) if kern.isdigit() else None
    vm["boot"] = sysctls.get("kern.bootsessionuuid", "").strip() or None
    vm["ram_total"] = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")

    if fast:
        vm["load"] = os.getloadavg()[0]
        vm["nprocs"] = 0
    else:
        hdr = header or _sh(["top", "-l", "1", "-n", "0"])
        m = re.search(r"PhysMem:\s+(\S+) used \((\S+) wired, (\S+) compressor\)"
                      r"(?:, (\S+) unused)?", hdr)
        if m:
            vm["ram_used"] = parse_size(m.group(1))
            vm["wired"] = parse_size(m.group(2))
            vm["compressor"] = parse_size(m.group(3))
            vm["unused"] = parse_size(m.group(4)) if m.group(4) else 0
            # top prints the "used" figure in whole gigabytes and TRUNCATES it,
            # while giving "unused" to the megabyte. On a 16 GB machine sitting
            # at 15.90 GiB it prints "15G" — understating by 0.9 GiB, always
            # downward, on every sample. Derive it from the precise pair
            # instead. Measured 2026-08-18: "15G used … 98M unused" against a
            # true 15.904 GiB, and in 8,226 recorded samples the truncated
            # field never once printed 16.
            if m.group(4) and vm.get("ram_total"):
                vm["ram_used"] = max(0, vm["ram_total"] - vm["unused"])
        m = re.search(r"Load Avg:\s+([\d.]+)", hdr)
        vm["load"] = float(m.group(1)) if m else 0.0
        m = re.search(r"Processes:\s+(\d+)", hdr)
        vm["nprocs"] = int(m.group(1)) if m else 0

    # Cumulative counters. Their *rate* is what predicts a freeze — a high
    # swapin rate means the working set no longer fits in RAM and the machine is
    # reading pages back as fast as it evicts them.
    if vm_stat_text is None:
        vm_stat_text = _sh(["vm_stat"])
    page_size(vm_stat_text)
    for line in vm_stat_text.splitlines():
        m = re.match(r'"?([^:"]+)"?:\s+(\d+)', line.strip())
        if not m:
            continue
        key = m.group(1).strip()
        if key in ("Swapins", "Swapouts", "Pageins", "Pageouts"):
            vm[key.lower()] = int(m.group(2))

    vm["ncpu"] = os.cpu_count() or 8

    # macOS exposes no per-process swap counter: pages are compressed into
    # segments, and whole segments are written to swap. So the best available
    # attribution is proportional — what share of all compressed bytes currently
    # lives on disk rather than in the in-RAM compressor.
    comp_ram = vm.get("compressor", 0)
    denom = comp_ram + vm.get("swap_used", 0)
    # Only meaningful when the compressor figure was actually collected. Without
    # it the ratio degenerates to 1.0 and every per-process swap estimate becomes
    # the maximum possible value, with nothing marking it as unreliable.
    if fast or "compressor" not in vm:
        vm["degraded"] = True
    else:
        vm["swap_frac"] = (vm.get("swap_used", 0) / denom) if denom else 0.0
    return vm


# NOT a constant: 16384 on Apple Silicon, 4096 on Intel — and macOS 13, which
# install.sh accepts, still runs on Intel. Hardcoding the ARM value would
# overstate every paging rate 4x there, tripping DANGER/CRITICAL at a quarter of
# the real thrash and blocking work that was never a problem. vm_stat states it
# on its own first line, so read it rather than assume the machine.
# Claude Code's own convention: green = completed, grey = working, amber = idle
# (its "blocked" means awaiting input, not memory-blocked), blue = terminal.
# Both front ends read this; the terminal previously painted working green and
# done grey — the exact inverse of the menu bar, for the same session.
STATE_COLOR = {"done": "green", "stopped": "green", "working": "grey",
               "blocked": "yellow", "terminal": "blue"}
STATE_LABEL = {"done": "completed", "stopped": "completed", "working": "working",
               "blocked": "idle", "terminal": "terminal"}


_page: int | None = None


def page_size(vm_stat_text: str | None = None) -> int:
    """Read once per process, from whichever vm_stat output arrives first,
    rather than spawning vm_stat at import just for its header."""
    global _page
    if _page is None:
        text = vm_stat_text if vm_stat_text is not None else _sh(["vm_stat"])
        m = re.search(r"page size of (\d+) bytes", text)
        _page = int(m.group(1)) if m else os.sysconf("SC_PAGE_SIZE")
    return _page


# The free-% level the runway estimate projects toward. See pressure().
HEADROOM_FLOOR = 20

# Rate baselines. A rate needs two readings 2-300 s apart on CLOCK_MONOTONIC_RAW
# and from the same kern.bootsessionuuid. free_pct is a whole percentage, so one
# tick over 2 s would read as 30 %/min: its delta needs a baseline at least 30 s
# old, kept separately from the 2 s one. Rates computed less than 5 s ago may be
# reused by a reader that comes too soon after its baseline.
RATE_MIN_S, RATE_MAX_S = 2, 300
FREE_MIN_S = 30
RATE_CACHE_S = 5


def mono_now() -> float:
    """CLOCK_MONOTONIC_RAW: keeps counting while the Mac sleeps."""
    return time.clock_gettime(time.CLOCK_MONOTONIC_RAW)


def uptime_now() -> float:
    """CLOCK_UPTIME_RAW: the same clock, stopped while the Mac sleeps."""
    return time.clock_gettime(time.CLOCK_UPTIME_RAW)


def _has_counters(vm: dict) -> bool:
    return all(isinstance(vm.get(k), (int, float)) for k in ("swapins", "swapouts"))


def score_pressure(vm: dict, rates: dict | None, free_delta_min: float | None = None,
                   carried_streak: int = 0, rates_source: str | None = None,
                   reason: str | None = None, advance: bool = True) -> dict:
    """How close is this machine to the freeze, and how fast is it getting
    there: memmon_telemetry.score(), the one scorer every reader shares (B26).

    `rates` is None when no valid baseline exists; free_delta_min is None
    when the free_pct baseline is younger than 30 s. Without rates a verdict
    of WATCH or worse stands as a lower bound and anything milder is UNKNOWN:
    a reading never says HEALTHY without rates (I-13)."""
    import memmon_telemetry
    why = reason or "no valid rate baseline"
    out = memmon_telemetry.score(vm, rates, free_delta_min, carried_streak,
                                 level_reason=why, advance=advance)
    if rates is None and out["level"] != "UNKNOWN":
        out["level_reason"] = f"lower bound: {why}"
    if rates is None:
        out["thrash_mbs"] = out["swapin_mbs"] = out["swapout_mbs"] = None
        out["swap_growth_mbmin"] = None
    out["rates_source"] = rates_source if rates is not None else None
    out["kernel_level"] = vm.get("kernel_level")
    return out


_prev_vm: dict = {}       # rate baseline: counters, _mono, _boot, _lh_streak
_free_base: dict = {}     # free_pct baseline: free_pct, _mono, _boot
_last_rates: dict = {}    # rates, _mono, _boot: reused for up to 5 s


def _read_row(path: str) -> dict | None:
    try:
        with open(path) as fh:
            row = json.load(fh)
        return row if isinstance(row, dict) else None
    except Exception:
        return None


RATE_FIELDS = ("swapin_mbs", "swapout_mbs", "swap_growth_mbmin")


def _legacy_baseline(row: dict, vm: dict, now: float) -> float | None:
    """The mono-equivalent time of a v1 latest.json row (no mono, no boot),
    which is what the installed sampler writes until the first S2 run. It is
    aged by wall ts as v1 did, 2-300 s. Without a boot id, counters that went
    backwards against this read mean a reboot came in between: refused."""
    ts = row.get("ts")
    if not isinstance(ts, (int, float)) or not _has_counters(row) \
            or row.get("rates") == "unavailable" or not _has_counters(vm):
        return None
    age = time.time() - ts
    if not RATE_MIN_S <= age <= RATE_MAX_S:
        return None
    if any(vm[k] < row[k] for k in ("swapins", "swapouts")):
        return None
    return now - age


def _seed_from_files(now: float, boot: str | None, vm: dict | None = None) -> None:
    """A one-shot reader has no in-process history, so it seeds from the
    sampler: pressure.json first, then latest.json, each only when it is from
    this boot and 2-300 s old by CLOCK_MONOTONIC_RAW. A file younger than 2 s
    lends its own in-run rates instead, as the 5 s cache. A v1 latest.json row
    seeds by its wall age instead (see _legacy_baseline)."""
    global _prev_vm, _free_base, _last_rates
    if boot is None:
        return
    for path in (PRESSURE_FILE, SNAPSHOT):
        row = _read_row(path)
        if (row and path == SNAPSHOT and "mono" not in row and "boot" not in row
                and vm is not None):
            mono = _legacy_baseline(row, vm, now)
            if mono is not None:
                streak = row.get("_lh_streak", 0) or 0
                _prev_vm = {**row, "_mono": mono, "_boot": boot, "_lh_streak": streak}
                _free_base = {"free_pct": row.get("free_pct"), "mono": mono, "boot": boot}
                return
            continue
        if not row or row.get("boot") != boot or not isinstance(
                row.get("mono"), (int, float)):
            continue
        age = now - row["mono"]
        streak = row.get("lh_streak", row.get("_lh_streak", 0)) or 0
        base = {**row, "_mono": row["mono"], "_boot": boot, "_lh_streak": streak}
        free = {"free_pct": row.get("free_pct"), "mono": row["mono"], "boot": boot}
        if (0 <= age < RATE_MIN_S and row.get("rates") == "ok"
                and all(isinstance(row.get(k), (int, float)) for k in RATE_FIELDS)):
            _last_rates = {"rates": {k: row[k] for k in RATE_FIELDS},
                           "_mono": row["mono"], "_boot": boot}
            _prev_vm, _free_base = base, free
            return
        # A row whose own rates were unavailable has no trustworthy counters.
        if (RATE_MIN_S <= age <= RATE_MAX_S and _has_counters(row)
                and row.get("rates") != "unavailable"):
            _prev_vm, _free_base = base, free
            return


def pressure(vm: dict) -> dict:
    """score_pressure() for every reader except the sampler, with the rates
    taken from an in-process baseline, else one seeded from the sampler's
    files. See score_pressure() for the verdict itself."""
    global _prev_vm, _free_base, _last_rates
    import memmon_telemetry
    now = mono_now()
    boot = vm.get("boot")
    if not _prev_vm:
        _seed_from_files(now, boot, vm)
    prev = _prev_vm
    same_boot = bool(prev) and boot is not None and prev.get("_boot") == boot
    carried = (prev.get("_lh_streak", 0) or 0) if same_boot else 0
    rates, source, reason, advance = None, None, "no valid rate baseline", True
    dt = now - prev["_mono"] if same_boot else None
    if not _has_counters(vm):
        reason = "vm_stat unavailable"
    elif dt is not None and RATE_MIN_S <= dt <= RATE_MAX_S and _has_counters(prev):
        rates = memmon_telemetry.rates_between(
            {**prev, "mono": prev["_mono"], "page_size": page_size()},
            {**vm, "mono": now, "page_size": page_size()})
        source = "baseline"
    elif (dt is not None and 0 <= dt < RATE_MIN_S and _last_rates
          and _last_rates.get("_boot") == boot
          and 0 <= now - _last_rates["_mono"] <= RATE_CACHE_S):
        rates, source, advance = dict(_last_rates["rates"]), "cached", False
    elif dt is not None and 0 <= dt < RATE_MIN_S:
        reason, advance = "baseline under 2 s old", False
    elif dt is not None and dt > RATE_MAX_S:
        reason = "baseline over 300 s old"
    elif prev and not same_boot:
        reason = "baseline from another boot"

    free_delta = None
    cur = {"free_pct": vm.get("free_pct"), "mono": now, "boot": boot}
    if source == "baseline":
        free_delta = memmon_telemetry.free_delta_between(_free_base, cur)
    fb = _free_base
    if (free_delta is not None or not fb or fb.get("boot") != boot
            or not 0 <= now - fb.get("mono", now) <= RATE_MAX_S):
        _free_base = cur

    out = score_pressure(vm, rates, free_delta, carried, source, reason, advance)
    if advance:
        _prev_vm = {**vm, "_mono": now, "_boot": boot, "_lh_streak": out["lh_streak"]}
        if source == "baseline":
            _last_rates = {"rates": dict(rates), "_mono": now, "_boot": boot}
    return out


# The VM is Docker; folding it here rather than in one of two front ends means
# the terminal and the popover agree without either knowing the special case.
SERVICE_ALIAS = {"Docker VM": "Docker"}


PROFILE = os.path.join(STATE_DIR, "profile.json")
SHELL_STATE = os.path.join(STATE_DIR, "learned.zsh")
PAUSE = os.path.join(STATE_DIR, "paused.json")
PROFILE_VERSION = 2
# A command has to cost this much before it is worth gating.
LEARN_HEAVY_AT = 1500 * MB
# Peaks below this are noise from whatever else the session was doing.
LEARN_MIN_SAMPLES = 2

_CMD_NOISE = re.compile(
    r"""^(sudo|nohup|time|timeout|caffeinate|env|exec|command|bash|sh|zsh)$""")
# Can never be the thing holding gigabytes; recording them only adds noise.
_TRIVIAL = {"cd", "echo", "export", "true", "false", ":", "printf", "pwd",
            # Splitting on ';' turns `for x in …; do …; done` into fragments
            # that are not commands at all.
            "for", "do", "done", "while", "if", "then", "fi", "else", "elif",
            "case", "esac", "function", "return", "local", "set", "unset",
            # Attribution charges a session's whole footprint to whatever it is
            # running, so a big session running `cp` marks `cp` heavy. These
            # cannot plausibly hold gigabytes, so exclude them rather than let
            # one coincidence add 60ms to every file copy.
            "cp", "mv", "rm", "ln", "mkdir", "touch", "chmod", "chown",
            "ls", "cat", "head", "tail", "wc", "sort", "uniq", "cut", "tr",
            "grep", "rg", "sed", "awk", "find", "which", "date", "sleep", "ps",
            "kill", "pgrep", "pkill", "open", "diff", "basename", "dirname"}
_SHELL_WORDS = {"for", "do", "done", "while", "if", "then", "fi", "else",
                "elif", "case", "esac", "function", "return", "local", "in",
                "select", "until", "coproc", "time", "!", "{"}
# These are real executables, but the current sampler cannot distinguish their
# own cost from the already-running session around them. Learning one would turn
# cheap control/API/read operations into warnings. Built-in pytest/node tooling
# is still classified explicitly below.
_NEVER_LEARN = {"git", "caffeinate", "python", "python3", "node", "ruby",
                "perl", "curl", "wget", "ssh", "scp", "rsync"}
_VERBS = {"build", "test", "typecheck", "install", "dev", "lint", "check",
          "compile", "start", "bundle", "package", "e2e"}
# Transparent: `turbo run typecheck` is a typecheck, not a "run". Used only when
# nothing more specific follows, so `codex.sh run` still keeps its subcommand.
_PASSTHROUGH = {"run", "watch", "exec"}


def _without_heredoc_bodies(cmd: str) -> str:
    """Remove here-document data so examples/prompts cannot become commands."""
    def delimiters(line: str) -> list[tuple[str, bool]]:
        found, i, quote = [], 0, ""
        while i < len(line):
            ch = line[i]
            if quote:
                if ch == quote:
                    quote = ""
                elif ch == "\\" and quote == '"':
                    i += 1
                i += 1
                continue
            if ch in "'\"":
                quote = ch; i += 1; continue
            if ch == "\\":
                i += 2; continue
            if line[i:i + 2] != "<<":
                i += 1; continue
            i += 2
            strip_tabs = i < len(line) and line[i] == "-"
            i += int(strip_tabs)
            while i < len(line) and line[i] in " \t":
                i += 1
            q = line[i] if i < len(line) and line[i] in "'\"" else ""
            i += int(bool(q))
            start = i
            while i < len(line) and ((q and line[i] != q)
                                     or (not q and line[i] not in " \t;|&<>()\r\n")):
                i += 1
            delim = line[start:i]
            if delim:
                found.append((delim, strip_tabs))
            if q and i < len(line):
                i += 1
        return found

    kept, pending = [], []
    for line in (cmd or "").splitlines(keepends=True):
        if pending:
            delim, strip_tabs = pending[0]
            candidate = line.rstrip("\r\n")
            if strip_tabs:
                candidate = candidate.lstrip("\t")
            if candidate == delim:
                pending.pop(0)
                kept.append("\n")
            continue
        kept.append(line)
        pending.extend(delimiters(line))
    return "".join(kept)


def shell_commands(cmd: str) -> list[list[str]]:
    """Return shell command segments without splitting quoted metacharacters.

    This is intentionally a small shell lexer, not a shell evaluator. `shlex`
    gives us the property the gate needs: a `|`, `;`, `&&`, or `||` inside a
    quoted grep pattern remains an argument, while a real operator starts the
    next executable position. A parse failure returns no commands, which keeps
    the gate fail-open.
    """
    try:
        import shlex                 # heavy-path only; keep gate startup lean
        lex = shlex.shlex(_without_heredoc_bodies(cmd), posix=True,
                         punctuation_chars=";&|(){}\n")
        lex.commenters = ""
        lex.whitespace = " \t\r"   # newline is a command separator, not space
        lex.whitespace_split = True
        toks = list(lex)
    except Exception:
        return []

    out, current = [], []
    for tok in toks:
        if tok and all(ch in ";&|(){}\n" for ch in tok):
            if current:
                out.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        out.append(current)
    return out[:16]


def _command_tokens(tokens: list[str]) -> list[str]:
    """Remove shell syntax, assignments, and transparent launch wrappers."""
    toks = list(tokens)
    while toks and (toks[0] in _SHELL_WORDS
                    or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0])):
        toks.pop(0)
    # A wrapper can itself be preceded by assignments or options. We only need
    # the executable position; ambiguity means no match, never a guess.
    while toks and _CMD_NOISE.match(os.path.basename(toks[0])):
        wrapper = os.path.basename(toks.pop(0))
        while toks and (toks[0].startswith("-")
                        or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0])):
            toks.pop(0)
        if wrapper in ("time", "timeout") and toks and re.fullmatch(r"[0-9.]+", toks[0]):
            toks.pop(0)
    return toks


def _plausible_shape(shape: str) -> bool:
    parts = shape.split()
    if not parts or len(parts) > 2:
        return False
    exe = parts[0]
    if (exe in _TRIVIAL or exe in _NEVER_LEARN or exe in _SHELL_WORDS
            or "…" in shape or not any(c.islower() for c in exe)
            or not re.fullmatch(r"[A-Za-z0-9_.@+-]{2,64}", exe)):
        return False
    return all(re.fullmatch(r"[A-Za-z0-9_.:@+-]{1,64}", p) for p in parts[1:])


def normalise_cmd(cmd: str) -> list[str]:
    """Reduce a command line to comparable shapes.

    Raw command text is unique every time — paths, flags, quoted prompts — so it
    can never accumulate evidence. `bash ~/.claude/skills/codex/scripts/codex.sh
    run "implement ABC-1234…"` has to become `codex.sh run` before a second
    occurrence counts as the same thing.

    Returns one shape per chained segment, because `cd x && pnpm build` is two
    commands and only the second one matters."""
    shapes = []
    for segment in shell_commands(cmd):
        toks = _command_tokens(segment)
        if not toks:
            continue
        exe = os.path.basename(toks[0])
        if exe in _TRIVIAL:
            continue
        # Prefer a recognisable verb anywhere in the line. Taking the first
        # non-flag token instead made `pnpm --filter dashboard typecheck` into
        # `pnpm dashboard`, so every package became its own shape and no shape
        # ever accumulated enough evidence to be learned.
        rest = [t for t in toks[1:] if re.match(r"^[\w:.@/-]+$", t)]
        sub = next((t for t in rest if t.split(":")[0] in _VERBS), "")
        if not sub:
            sub = next((t for t in rest if t in _PASSTHROUGH), "")
        if not sub:
            sub = next((t for t in rest if not t.startswith("-")), "")
        shape = f"{exe} {sub}" if sub and len(sub) < 32 and "/" not in sub else exe
        if _plausible_shape(shape):
            shapes.append(shape)
    return shapes[:4]


def load_profile() -> dict:
    """Load the v2 profile, quarantining the unsafe unversioned learner once."""
    try:
        with open(PROFILE) as fh:
            raw = json.load(fh)
    except Exception:
        return {}
    if raw.get("version") == PROFILE_VERSION and isinstance(raw.get("commands"), dict):
        return raw["commands"]

    # v1 learned from quote-broken display strings and published first-token
    # globs such as *git* and *python3*. It cannot be repaired safely: retain it
    # for inspection, start clean, and immediately clear the shell prefilter.
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        os.replace(PROFILE, f"{PROFILE}.quarantined-v1-{stamp}")
        save_profile({})
        _write_learned_glob({})
    except Exception:
        pass
    return {}


def save_profile(prof: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = PROFILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"version": PROFILE_VERSION, "commands": prof}, fh)
    os.replace(tmp, PROFILE)


def profile_says_heavy(cmd: str) -> bool:
    """True if ANY segment of the command has been observed to cost memory.

    Learning is purely additive: it can promote a command the regex misses, but
    it must never demote one the regex catches. The earlier version returned the
    verdict of the FIRST shape with enough samples, so a cheap leading segment
    masked an expensive one — `python3 -c "…" && pnpm --filter web typecheck`
    evaluated as light because `python3` was learned light, silently ungating the
    exact command the block message recommends. Under-matching disables the gate
    without a symptom; over-matching costs one Python start."""
    prof = load_profile()
    return any((e := prof.get(shape))
               and e.get("n", 0) >= LEARN_MIN_SAMPLES
               and e.get("peak", 0) >= LEARN_HEAVY_AT
               for shape in normalise_cmd(cmd))


def learn(snap: dict) -> None:
    """Attribute each session's current memory to whatever it is running.

    Uses the session's OWN subtree, never system memory: with ten sessions live,
    a system-wide delta would blame whichever command happened to be running
    when someone else started a build. Sampling once a minute means anything
    finishing inside a minute is never learned — which is correct, because a
    command that short is not the problem."""
    prof = load_profile()
    changed = False
    for s in snap.get("sessions") or []:
        # `doing` is presentation text: paths are shortened and it is truncated.
        # Only the untouched active Bash detail is trusted as learning input.
        command = s.get("learning_cmd") or ""
        if not command:
            continue
        for shape in normalise_cmd(command):
            e = prof.setdefault(shape, {"n": 0, "peak": 0, "last": 0})
            e["n"] += 1
            e["peak"] = max(e["peak"], s.get("mem", 0))
            e["last"] = int(time.time())
            changed = True
    if not changed:
        return
    # Forget shapes untouched for a month so a retired script stops being gated.
    cutoff = time.time() - 30 * 86400
    prof = {k: v for k, v in prof.items() if v.get("last", 0) >= cutoff}
    try:
        save_profile(prof)
        _write_learned_glob(prof)
    except Exception:
        pass


def pause_until() -> float:
    """0 if the gate is active, else the epoch it resumes (inf = indefinite)."""
    try:
        with open(PAUSE) as fh:
            until = json.load(fh).get("until", 0)
    except Exception:
        return 0
    if until == "forever":
        return float("inf")
    return until if until > time.time() else 0


def _write_learned_glob(prof: dict | None = None) -> None:
    """Publish everything the shell fast-path needs, in one file.

    Both the learned patterns and the pause flag live here because a single
    writer cannot clobber the other's half — an earlier split would have let a
    learn() cycle silently re-arm a paused gate.

    Without the learned half the wrapper would exit before Python ever saw a
    learned-heavy command, so the profile could never take effect."""
    if prof is None:
        prof = load_profile()
    heavy = sorted(k for k, v in prof.items()
                   if v.get("n", 0) >= LEARN_MIN_SAMPLES
                   and v.get("peak", 0) >= LEARN_HEAVY_AT)
    # Publish the full learned shape (`*codex.sh*run*`), never its first token.
    # A first-token *python3* or *git* glob was broad enough to erase the fast
    # path for unrelated work.
    pats_out = []
    for k in heavy:
        if not _plausible_shape(k):
            continue
        words = k.split()
        pat = "*" + "*".join(words) + "*"
        if pat not in pats_out:
            pats_out.append(pat)
    pats = "|".join(sorted(pats_out))
    paused = pause_until()
    tmp = SHELL_STATE + ".tmp"
    with open(tmp, "w") as fh:
        fh.write("# generated by memmon — do not edit\n")
        # MUST be quoted. Unquoted, zsh tries to expand `*pnpm*` as a filename
        # glob, fails with "no matches found", and that error aborts the rest of
        # the sourced file — silently unsetting the learned patterns AND every
        # line after it, which is how the pause flag below stopped working.
        fh.write(f"MEMMON_LEARNED='{pats or '__never_matches__'}'\n")
        # Checked first in the wrapper: a paused gate must cost nothing at all,
        # not merely decline to act.
        fh.write(f"MEMMON_PAUSED={'1' if paused else ''}\n")
    os.replace(tmp, SHELL_STATE)


# Tags that really are a build fanning out, as opposed to something long-lived
# that merely lives in a worktree.
BUILD_TAGS = ("tsc", "turbo", "pnpm", "npm", "yarn", "vitest", "jest",
              "webpack", "esbuild", "cargo", "gradle", "bazel", "next")


def is_build_tag(tag: str) -> bool:
    return any(t in (tag or "") for t in BUILD_TAGS)


def top_consumers(snap: dict, limit: int = 3) -> list[dict]:
    """Every heavy holder on the machine, ranked, regardless of what it is.

    The verdict is about whether the MACHINE is safe to work on, so the thing to
    name is whatever is actually holding the memory. Ranking only Claude's own
    worktrees meant the advice could point at a 2.3G build while a browser held
    5.7G — more than every session combined — and never mention it."""
    out = []
    for w in snap.get("worktrees") or []:
        out.append({"name": w["name"], "mem": w["mem"], "n": w["n"],
                    "tag": w.get("tag", ""),
                    "kind": "build" if is_build_tag(w.get("tag", "")) else "resident"})
    for name, v in (snap.get("apps") or {}).items():
        out.append({"name": name, "mem": v["mem"], "n": v["n"],
                    "tag": name, "kind": "app"})
    out.sort(key=lambda x: -x["mem"])
    return out[:limit]


def describe_consumer(c: dict, session_total: float = 0) -> str:
    """One clause naming a holder in terms its owner can act on."""
    if c["kind"] == "build":
        return (f"{c['name']} is running {c['tag']} "
                f"({human(c['mem'])} across {c['n']} processes)")
    if c["kind"] == "resident":
        return f"{c['name']} has {c['tag']} resident holding {human(c['mem'])}"
    tail = ""
    if session_total and c["mem"] > session_total:
        tail = " — more than every Claude session combined"
    return (f"{c['name']} is holding {human(c['mem'])} across "
            f"{c['n']} processes{tail}")


def build_advice(snap: dict) -> str:
    """One sentence naming what is actually causing this and what to do.

    Canned advice ("avoid starting a full-repo build") is unactionable — it does
    not say which build, or whose. This names the real offender from the same
    snapshot the score came from."""
    p = snap.get("pressure") or {}
    level = p.get("level", "HEALTHY")
    ranked = [c for c in top_consumers(snap) if c["mem"] > 1.5 * GB]
    top = ranked[0] if ranked else None
    session_total = sum(s.get("mem", 0) for s in snap.get("sessions") or [])
    hot = [s for s in snap.get("sessions") or []
           if s.get("swap", 0) > 0.5 * max(s.get("mem", 1), 1)]

    if level == "UNKNOWN":
        return (f"Pressure unknown: {p.get('level_reason') or 'no valid rate baseline'}. "
                "No verdict until the next reading has a baseline.")
    if level == "HEALTHY":
        if p.get("to_next"):
            return (f"Safe to start work — {p['to_next']} more point"
                    f"{'s' if p['to_next'] != 1 else ''} would make this "
                    f"{p.get('next_level', '')}.")
        return "Safe to start work."

    # Telling someone to `--filter` a browser, or a resident dev server, is
    # advice they cannot act on.
    if top:
        who = describe_consumer(top, session_total)
    elif hot:
        who = f"{len(hot)} session(s) are more than half swapped out"
    else:
        who = ""

    building = bool(top) and top["kind"] == "build"
    if level == "CRITICAL":
        tail = "Stop starting anything and free memory now."
    elif level == "DANGER":
        tail = ("Let it finish before starting another build." if building else
                "Free memory there before starting another build."
                if top else "Stop a build before starting another.")
    else:
        if building or not top:
            tail = ("Scope new work to one package (`pnpm --filter <pkg>`) rather "
                    "than the whole repo.")
        elif top["kind"] == "app":
            tail = "Closing it would free more than scoping any build."
        else:
            tail = "Check whether it is still needed before starting a build."
    return f"{who}. {tail}" if who else tail


def read_sessions() -> list[dict]:
    """Claude Code background sessions, from the job state files the daemon writes."""
    sessions = []
    if not os.path.isdir(JOBS_DIR):
        return sessions
    for short in os.listdir(JOBS_DIR):
        path = os.path.join(JOBS_DIR, short, "state.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as fh:
                d = json.load(fh)
        except Exception:
            continue
        raw_detail = str(d.get("detail") or "")
        learning_cmd = (raw_detail[len("[shell]"):].strip()
                        if raw_detail.startswith("[shell]") else "")
        doing = raw_detail or d.get("displayIntent") or d.get("intent") or ""
        fan = d.get("fan") or []
        if fan and isinstance(fan, list):
            label = (fan[0] or {}).get("label")
            if label:
                doing = f"[{(fan[0] or {}).get('kind', 'tool')}] {label}"
        doing = " ".join(str(doing).split())
        # In-flight shell labels are raw command lines — env-var preambles and
        # absolute paths make them unreadable and crowd out everything else.
        doing = re.sub(r"/Users/[^/\s]+/", "~/", doing)
        doing = re.sub(r"\b[A-Z][A-Z0-9_]*=\S+\s*", "", doing)
        if len(doing) > 150:
            doing = doing[:149] + "…"
        # The daemon names a session lazily, so fall back to the opening words
        # of the prompt rather than showing a meaningless hex id.
        name = d.get("name")
        if not name:
            words = str(d.get("intent") or "").split()
            name = " ".join(words[:5]) if words else short
        sessions.append({
            "short": short,
            "name": name,
            "state": d.get("state") or "?",
            "doing": doing,
            "learning_cmd": learning_cmd,
            "cwd": d.get("cwd") or "",
            "session_id": d.get("sessionId") or "",
            "updated": os.path.getmtime(path),
            "intent": " ".join(str(d.get("intent") or "").split()),
        })
    return sessions


PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")

# Commands that spawn something long-lived and expensive. Matching one of these
# in a transcript is how a Docker VM or a dev server gets traced back to the
# session that actually asked for it.
SERVICE_CMDS = {
    "Docker": re.compile(r"\bdocker(?:\s+compose|-compose)?\s+(?:up|run|start|build)"
                         r"|\bcolima start|testcontainers"),
    "dev server": re.compile(r"pnpm\s+(?:run\s+)?dev\b|next\s+dev|turbo\s+run\s+dev"),
    "typecheck": re.compile(r"(?:pnpm|turbo).*\btypecheck\b|\btsc\b"),
    "tests": re.compile(r"(?:pnpm|turbo|npx)\s+(?:run\s+)?(?:test|vitest|jest)"),
    "build": re.compile(r"(?:pnpm|turbo)\s+(?:run\s+)?build\b"),
}

_tcache: dict[str, tuple[float, dict]] = {}


def tail_bytes(path: str, nbytes: int = 24_000_000) -> list[str]:
    """Read the tail of a transcript. The cap is generous because tool_result
    blocks are huge — a 96KB tail covered only 51 of 361 lines in practice."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > nbytes:
                fh.seek(size - nbytes)
                fh.readline()  # discard the partial line
            return fh.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return []


def _text_of(msg) -> str:
    c = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def read_transcript(path: str) -> dict:
    """Pull cwd, the last user prompt, and recent shell commands out of a session
    transcript. Only the tail is parsed — these files reach tens of MB."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    hit = _tcache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]

    meta = {"cwd": "", "last_prompt": "", "cmds": [], "mtime": mtime}
    for line in tail_bytes(path):
        # Cheap prefilter: only these lines can carry anything we want, and
        # json-parsing every tool_result line is what makes this slow.
        if ('"cwd"' not in line and '"tool_use"' not in line
                and '"type":"user"' not in line and '"type": "user"' not in line):
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("cwd"):
            meta["cwd"] = r["cwd"]
        ts = r.get("timestamp", "")
        if r.get("type") == "user" and not r.get("isSidechain"):
            t = _text_of(r.get("message") or {})
            if t.strip() and not t.startswith("<"):
                meta["last_prompt"] = " ".join(t.split())
        if r.get("type") == "assistant":
            for b in (r.get("message") or {}).get("content") or []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                if b.get("name") != "Bash":
                    continue
                cmd = (b.get("input") or {}).get("command", "")
                if cmd:
                    meta["cmds"].append((ts, " ".join(cmd.split())[:200]))
    meta["cmds"] = meta["cmds"][-60:]
    _tcache[path] = (mtime, meta)
    return meta


def read_subagents(transcript_path: str) -> list[dict]:
    """Subagents run inside the parent's process, so they never appear in `ps`.
    Their transcripts are the only way to see them."""
    base = transcript_path[:-6] if transcript_path.endswith(".jsonl") else transcript_path
    sub_dir = os.path.join(base, "subagents")
    if not os.path.isdir(sub_dir):
        return []
    now, out = time.time(), []
    for fn in os.listdir(sub_dir):
        if not fn.endswith(".jsonl"):
            continue
        p = os.path.join(sub_dir, fn)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        # The sidecar .meta.json carries the agent type and a short description,
        # so the multi-MB transcript never has to be opened.
        kind, goal, model = "", "", ""
        try:
            with open(p[:-6] + ".meta.json") as fh:
                md = json.load(fh)
            kind = md.get("agentType") or ""
            goal = md.get("description") or ""
            model = md.get("model") or ""
        except Exception:
            pass
        out.append({
            "id": fn.replace("agent-", "").replace(".jsonl", "")[:8],
            "kind": kind or "agent", "goal": goal, "model": model,
            "age": int(now - mt), "active": (now - mt) < 180,
        })
    out.sort(key=lambda a: a["age"])
    return out


RV_SOCK_RE = re.compile(r"/rv/([0-9a-f]{8})\.sock")


def map_pids_to_jobs() -> dict[int, str]:
    """pid -> job short id, via the daemon's per-job rendezvous socket.

    This is the only reliable link for a session that was claimed from the
    prewarm pool: such a process keeps the pool's `bg-spare` command line and
    never carries a --session-id, so cmdline parsing alone cannot see it."""
    out = _sh(["lsof", "-U", "-Fpn"], timeout=20)
    mapping: dict[int, str] = {}
    pid = None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("n") and pid:
            m = RV_SOCK_RE.search(line)
            if m:
                mapping[pid] = m.group(1)
    return mapping


def find_transcripts(max_age_h: int = 12) -> dict[str, str]:
    """session-id -> transcript path, for sessions touched recently."""
    found: dict[str, str] = {}
    if not os.path.isdir(PROJECTS_DIR):
        return found
    cutoff = time.time() - max_age_h * 3600
    for proj in os.listdir(PROJECTS_DIR):
        pdir = os.path.join(PROJECTS_DIR, proj)
        if not os.path.isdir(pdir):
            continue
        try:
            entries = os.listdir(pdir)
        except OSError:
            continue
        for fn in entries:
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(pdir, fn)
            try:
                if os.path.getmtime(p) < cutoff:
                    continue
            except OSError:
                continue
            found[fn[:-6]] = p
    return found


# ------------------------------------------------------------- attribution

def build_tree(ps: dict[int, dict]) -> dict[int, list[int]]:
    kids: dict[int, list[int]] = defaultdict(list)
    for pid, info in ps.items():
        kids[info["ppid"]].append(pid)
    return kids


def descendants(root: int, kids: dict[int, list[int]], cap: int = 4000) -> list[int]:
    seen, stack = [], [root]
    while stack and len(seen) < cap:
        pid = stack.pop()
        for k in kids.get(pid, ()):
            if k not in seen and k != pid:
                seen.append(k)
                stack.append(k)
    return seen


def tag_for(cmd: str) -> str:
    """Short human label for what a process actually is."""
    if "--session-id" in cmd or "/versions/" in cmd or "ClaudeCode.app" in cmd:
        if "bg-spare" in cmd:
            return "claude prewarm"
        if "bg-pty-host" in cmd:
            return "claude pty host"
        return "claude"
    if "typescript/bin/tsc" in cmd:
        return "tsc typecheck"
    if "turbo" in cmd and "run" in cmd:
        return "turbo run"
    if "vitest" in cmd:
        return "vitest"
    if re.search(r"\bcodex\b", cmd):
        return "codex"
    if "next-server" in cmd or "next dev" in cmd:
        return "next dev"
    if "esbuild" in cmd:
        return "esbuild"
    if re.search(r"pnpm.*(typecheck|build|test|install)", cmd):
        m = re.search(r"pnpm.*?(typecheck|build|test|install)", cmd)
        return f"pnpm {m.group(1)}"
    if "bg-spare" in cmd:
        return "claude prewarm" if spare_is_idle(cmd) else "claude (session)"
    if "bg-pty-host" in cmd:
        return "claude pty"
    return os.path.basename(cmd.split()[0]) if cmd.split() else "?"


def worktree_of(cmd: str) -> str:
    """Which checkout a build process belongs to.

    Prefers configured project roots — taking the next path segment needs no
    naming convention at all — and falls back to the configurable regex."""
    for root in CONFIG["project_roots"]:
        i = cmd.find(root + "/")
        if i < 0:
            continue
        seg = cmd[i + len(root) + 1:].split("/", 1)[0]
        if seg:
            return seg
    m = WORKTREE_RE.search(cmd)
    if not m:
        return ""
    return (m.group(1) if m.groups() and m.group(1) else m.group(0))


def app_group(cmd: str) -> str:
    for needle, name in (
        ("Brave Browser", "Brave"), ("Slack", "Slack"), ("Docker", "Docker"),
        ("com.apple.Virtual", "Docker VM"), ("Cursor", "Cursor"),
        ("Code Helper", "VS Code"), ("Spotify", "Spotify"),
        ("Notion", "Notion"), ("zoom.us", "Zoom"), ("Obsidian", "Obsidian"),
        ("WindowServer", "WindowServer"), ("Figma", "Figma"),
        # Browsers, terminals and editors, by their bundle's path, so a
        # helper (Google Chrome Helper (Renderer), …) groups under its app.
        # Names are memmon_common.APP_NAMES display names. Canary before Chrome.
        ("Google Chrome Canary", "Google Chrome Canary"),
        ("Google Chrome", "Google Chrome"), ("Safari.app", "Safari"),
        ("Arc.app", "Arc"), ("Firefox.app", "Firefox"),
        ("Microsoft Edge", "Microsoft Edge"), ("Ghostty.app", "Ghostty"),
        ("iTerm.app", "iTerm2"), ("Terminal.app", "Terminal"), ("Warp.app", "Warp"),
        ("Zed.app", "Zed"),
        # Not Xcode.app/Contents/Developer: git, clang and swift run from there.
        ("Xcode.app/Contents/MacOS", "Xcode"),
        ("Xcode.app/Contents/SharedFrameworks", "Xcode"),
        ("OrbStack", "OrbStack"),
    ):
        if needle in cmd:
            return name
    return ""


def collect(pres: dict | None = None) -> dict:
    from memmon_runner import jobs
    ps = read_ps()
    top, top_header = read_top()
    vm = read_vm(header=top_header)
    sessions = read_sessions()
    kids = build_tree(ps)

    def mem(pid: int) -> int:
        return top.get(pid, {}).get("mem", 0)

    def cmprs(pid: int) -> int:
        return top.get(pid, {}).get("cmprs", 0)

    frac = vm.get("swap_frac", 0.0)

    def swapped(pid: int) -> int:
        """Estimated share of this process's footprint whose pages are on disk.

        Only compressed pages can be on disk, so the estimate scales CMPRS by
        the system-wide on-disk share (see read_vm). Measured in original page
        bytes — the copy on disk is compressed, so actual swapfile usage is
        smaller. Capped at the footprint so the split can never exceed it."""
        return min(int(cmprs(pid) * frac), mem(pid))

    def ram(pid: int) -> int:
        """The rest of the footprint: pages held in physical memory, whether
        plain resident or compressed in the in-RAM compressor.

        Deliberately not `ps` RSS — RSS counts shared pages the footprint
        excludes, so RSS + compressed can exceed the footprint and the two
        columns would not sum."""
        return max(0, mem(pid) - swapped(pid))

    # Locate each session's root process by its --session-id, then claim the
    # whole subtree beneath it. That is what makes a codex-spawned tsc show up
    # against the session that asked for it.
    sid_to_pid: dict[str, int] = {}
    for pid, info in ps.items():
        m = SESSION_ID_RE.search(info["cmd"])
        if m:
            sid_to_pid[m.group(1)] = pid
    # Sessions claimed from the prewarm pool are only visible via the daemon
    # socket, so this is what finds most of them.
    short_to_pid = {short: pid for pid, short in map_pids_to_jobs().items()}

    # Background jobs have a state.json; sessions you started in a terminal do
    # not. Recover those from their transcripts so every running session is named.
    transcripts = find_transcripts()
    by_sid: dict[str, dict] = {}
    for s in sessions:
        s["origin"] = "bg-job"
        if s["session_id"]:
            by_sid[s["session_id"]] = s
    for sid in sid_to_pid:
        if sid in by_sid:
            continue
        meta = read_transcript(transcripts[sid]) if sid in transcripts else {}
        cwd = meta.get("cwd", "")
        by_sid[sid] = {
            "short": sid[:8], "state": "terminal", "origin": "terminal",
            "name": os.path.basename(cwd.rstrip("/")) or sid[:8],
            "doing": meta.get("last_prompt", ""), "cwd": cwd,
            "session_id": sid, "updated": meta.get("mtime", 0),
            "intent": meta.get("last_prompt", ""),
        }

    # Which session most recently ran a command that starts each service.
    service_owner: dict[str, dict] = {}
    for sid, s in by_sid.items():
        tp = transcripts.get(sid)
        if not tp:
            continue
        started = {}
        for ts, cmd in read_transcript(tp).get("cmds", []):
            for svc, rx in SERVICE_CMDS.items():
                if rx.search(cmd):
                    started[svc] = (ts, cmd)
        s["started"] = started
        for svc, (ts, cmd) in started.items():
            prev = service_owner.get(svc)
            if prev is None or ts > prev["ts"]:
                service_owner[svc] = {"ts": ts, "cmd": cmd, "session": s["name"],
                                      "sid": sid}

    sessions = list(by_sid.values())
    claimed: set[int] = set()
    live_sessions = []
    for s in sessions:
        root = short_to_pid.get(s.get("short")) or sid_to_pid.get(s["session_id"])
        if root is None:
            s["alive"] = False
            s["mem"] = s["cmprs"] = s["nproc"] = 0
            s["top_children"] = []
            continue
        tree = [root] + descendants(root, kids)
        claimed.update(tree)
        children = []
        for pid in tree:
            # The session's own process is not informative as a "child" — its
            # size is already the bulk of the session total shown above.
            if pid == root or mem(pid) < 80 * MB:
                continue
            children.append({
                "pid": pid, "mem": mem(pid), "cmprs": cmprs(pid),
                "ram": ram(pid), "swap": swapped(pid),
                "age": ps[pid]["age"], "tag": tag_for(ps[pid]["cmd"]),
                "worktree": worktree_of(ps[pid]["cmd"]),
            })
        children.sort(key=lambda c: -c["mem"])
        tp = transcripts.get(s["session_id"])
        subs = read_subagents(tp) if tp else []
        s.update({
            "alive": True, "root": root,
            "mem": sum(mem(p) for p in tree),
            "cmprs": sum(cmprs(p) for p in tree),
            "ram": sum(ram(p) for p in tree),
            "swap": sum(swapped(p) for p in tree),
            "nproc": len(tree),
            "age": ps[root]["age"],
            "top_children": children[:4],
            "subagents": subs,
            "subagents_active": [a for a in subs if a["active"]],
        })
        live_sessions.append(s)

    # Orphans: heavy build processes reparented to launchd (ppid 1). Nothing
    # will ever reap these — the shell that started them is gone.
    orphans = []
    for pid, info in ps.items():
        if pid in claimed or mem(pid) < 100 * MB:
            continue
        if not REAPABLE.search(info["cmd"]):
            continue
        parent_dead = info["ppid"] == 1
        if not parent_dead and info["age"] < 3600:
            continue
        orphans.append({
            "pid": pid, "mem": mem(pid), "cmprs": cmprs(pid),
            "age": info["age"], "ppid": info["ppid"],
            "orphaned": parent_dead,
            "tag": tag_for(info["cmd"]),
            "worktree": worktree_of(info["cmd"]),
            "eng": (TICKET_RE.search(info["cmd"]).group(1)
                    if TICKET_RE.search(info["cmd"]) else ""),
        })
    orphans.sort(key=lambda o: -o["mem"])

    # Blame an orphan on whichever session mentions its ENG id / worktree.
    for o in orphans:
        o["blame"] = ""
        needle = o["eng"] or o["worktree"]
        if not needle:
            continue
        for s in live_sessions:
            hay = f"{s['name']} {s['doing']} {s['intent']}"
            if needle and needle.lower() in hay.lower():
                o["blame"] = s["name"]
                break

    orphan_pids = {o["pid"] for o in orphans}

    # Claude's own runtime pool: prewarm spares and pty hosts belong to no
    # session, so without this they would vanish from the accounting entirely.
    overhead = {"mem": 0, "n": 0, "oldest": 0, "spares": 0, "spare_mem": 0,
                "stale": 0, "stale_mem": 0, "items": [],
                "claimed": 0, "claimed_mem": 0}
    apps: dict[str, dict] = defaultdict(lambda: {"mem": 0, "n": 0})
    other_heavy = []
    for pid, info in ps.items():
        if pid in claimed or pid in orphan_pids:
            continue
        cmd = info["cmd"]
        if ("bg-spare" in cmd or "bg-pty-host" in cmd or "daemon run" in cmd
                or "ClaudeCode.app" in cmd or "/versions/" in cmd):
            overhead["mem"] += mem(pid)
            overhead["n"] += 1
            overhead["oldest"] = max(overhead["oldest"], info["age"])
            if "bg-spare" in cmd:
                if not spare_is_idle(cmd):
                    # Claimed: a live session we could not name. Never reclaimable.
                    overhead["claimed"] += 1
                    overhead["claimed_mem"] += mem(pid)
                    continue
                overhead["spares"] += 1
                overhead["spare_mem"] += mem(pid)
                stale = info["age"] > STALE_SPARE
                if stale:
                    overhead["stale"] += 1
                    overhead["stale_mem"] += mem(pid)
                overhead["items"].append({
                    "pid": pid, "mem": mem(pid), "age": info["age"], "stale": stale,
                })
            continue
        g = app_group(cmd)
        if g:
            apps[g]["mem"] += mem(pid)
            apps[g]["n"] += 1
            continue
        if mem(pid) >= 300 * MB:
            other_heavy.append({
                "pid": pid, "mem": mem(pid), "age": info["age"],
                "tag": tag_for(cmd), "worktree": worktree_of(cmd),
            })
    other_heavy.sort(key=lambda o: -o["mem"])

    # Build work rolled up by worktree — the unit that actually explains a
    # spike, since one `turbo run typecheck` fans out ~10 multi-GB tsc workers.
    wt_roll: dict[str, dict] = defaultdict(
        lambda: {"mem": 0, "ram": 0, "swap": 0, "n": 0, "orphans": 0,
                 "tags": defaultdict(int), "oldest": 0})
    for pid, info in ps.items():
        if pid in claimed:
            continue
        w = worktree_of(info["cmd"])
        if not w or mem(pid) < 50 * MB:
            continue
        r = wt_roll[w]
        r["mem"] += mem(pid)
        r["ram"] += ram(pid)
        r["swap"] += swapped(pid)
        r["n"] += 1
        r["oldest"] = max(r["oldest"], info["age"])
        r["tags"][tag_for(info["cmd"])] += 1
        if pid in orphan_pids:
            r["orphans"] += 1
    worktrees = []
    for name, r in wt_roll.items():
        top_tag = max(r["tags"].items(), key=lambda kv: kv[1])[0] if r["tags"] else "?"
        worktrees.append({"name": name, "mem": r["mem"], "ram": r["ram"],
                          "swap": r["swap"], "n": r["n"],
                          "orphans": r["orphans"], "tag": top_tag,
                          "oldest": r["oldest"]})
    worktrees.sort(key=lambda x: -x["mem"])

    live_sessions.sort(key=lambda s: -s["mem"])
    snap = {
        "ts": time.time(),
        "vm": vm,
        "pressure": dict(pres) if pres is not None else pressure(vm),
        "blocked": load_pending(),
        "gate": gate_stats(),
        "jobs": jobs(STATE_DIR),
        "sessions": live_sessions,
        "idle_sessions": [s for s in sessions if not s.get("alive")],
        "orphans": orphans,
        "orphan_total": sum(o["mem"] for o in orphans),
        "overhead": overhead,
        "service_owner": service_owner,
        "worktrees": worktrees,
        "other_heavy": [o for o in other_heavy if not o["worktree"]][:6],
        "apps": dict(sorted(apps.items(), key=lambda kv: -kv[1]["mem"])),
    }
    # Needs the finished snapshot: the advice names the worktree and sessions.
    snap["pressure"]["advice"] = build_advice(snap)
    return snap


# ------------------------------------------------------------------ render

C = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "mag": "\033[35m", "cyan": "\033[36m", "grey": "\033[90m",
}


def col(s: str, c: str, on: bool = True) -> str:
    return f"{C[c]}{s}{C['reset']}" if on else s


def human(n: int) -> str:
    if n >= GB:
        return f"{n / GB:.1f}G"
    if n >= MB:
        return f"{n / MB:.0f}M"
    return f"{n}B"


def dur(sec: int) -> str:
    if sec >= 86400:
        return f"{sec // 86400}d{(sec % 86400) // 3600}h"
    if sec >= 3600:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
    return f"{sec // 60}m"


def iso_ago(ts: str) -> str:
    """Relative age from a transcript ISO timestamp."""
    if not ts:
        return "?"
    try:
        t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        t -= time.timezone if not time.localtime().tm_isdst else time.altzone
        return dur(max(0, int(time.time() - t))) + " ago"
    except Exception:
        return "?"


def bar(frac: float, width: int, color: str, on: bool) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    return col("█" * filled, color, on) + col("░" * (width - filled), "grey", on)


def clip(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def _build(snap: dict, on: bool = True, child_cap: int = 4,
           sub_cap: int = 3) -> list[str]:
    import shutil
    vm, w = snap["vm"], shutil.get_terminal_size((110, 40)).columns
    w = max(80, min(w, 160))
    L: list[str] = []
    # One verdict only, from the pressure model. The swap bar deliberately shows
    # no severity of its own: used/total is near 100% whenever macOS has sized
    # the swapfile to demand, which said CRITICAL while the machine was idle.
    sev_c = (snap.get("pressure") or {}).get("color", "green")

    ram_t = vm.get("ram_total", 1)
    ram_u = vm.get("ram_used", 0)
    sw_t = max(vm.get("swap_total", 1), 1)
    sw_u = vm.get("swap_used", 0)
    bw = max(18, w - 62)

    title = f" MEMMON  {human(ram_t)} · {vm.get('ncpu', 8)} cores "
    L.append(col(title.ljust(w - 22, "─"), "cyan", on)
             + col(time.strftime(" %H:%M:%S ").rjust(22, "─"), "grey", on))
    L.append(
        f" RAM   {bar(ram_u / ram_t, bw, 'blue', on)} "
        f"{human(ram_u)}/{human(ram_t)}  "
        + col(f"compressed {human(vm.get('compressor', 0))}", "grey", on)
    )
    L.append(
        f" SWAP  {bar(sw_u / sw_t, bw, sev_c, on)} "
        f"{human(sw_u)}/{human(sw_t)}  "
        + col(f"{sw_u / max(vm.get('ram_total', 1), 1):.2f}x RAM size"
              f" · {vm.get('swap_frac', 0) * 100:.0f}% of compressed bytes"
              f" on disk", "grey", on)
    )
    load = vm.get("load", 0)
    load_c = "red" if load > vm.get("ncpu", 8) * 1.5 else (
        "yellow" if load > vm.get("ncpu", 8) else "grey")
    L.append(
        col(f" load {load:.1f}", load_c, on)
        + col(f" · {vm.get('nprocs', 0)} procs · {vm.get('free_pct', 0)}% free"
              f" · wired {human(vm.get('wired', 0))}", "grey", on)
    )

    p = snap.get("pressure") or {}
    if p:
        detail = " · ".join(p.get("reasons") or []) or "no pressure signals"
        nxt = (f"  {p['to_next']} pt → {p['next_level']}"
               if p.get("to_next") else "")
        if p.get("level") == "UNKNOWN":
            detail, nxt = p.get("level_reason") or "no valid rate baseline", ""
        elif p.get("level_reason"):
            detail += f" ({p['level_reason']})"
        head = f" ▌ {p['level']:<9}"
        L.append(col(head, p["color"], on)
                 + col(detail, "grey" if p["level"] == "HEALTHY" else p["color"], on)
                 + col(nxt, "grey", on))
        room = p.get("headroom_min")
        room_s = (f" · about {room:.0f} min until free memory reaches the "
                  f"{HEADROOM_FLOOR} % floor (trend estimate)"
                  if room is not None and room < 120 else "")
        L.append(col(f" {'':<10}{p.get('advice', '')}{room_s}", "grey", on))
    L.append("")

    # ---- explicitly managed commands (Claude, Codex or terminal)
    if snap.get("jobs"):
        L.append(col(" MANAGED JOBS", "bold", on))
        for job in snap["jobs"]:
            L.append(clip(f"  {job['resource']} · {job['state']} · {job['label']} "
                          f"({job['elapsed_seconds']}s) — {job['reason']}", w))
        L.append("")

    # ---- sessions
    L.append(col(" CLAUDE SESSIONS".ljust(w, " "), "bold", on))
    L.append(col(f"  {'NAME':<24}{'TOTAL':>7}{'RAM':>7}{'SWAP~':>7} "
                 f"{'PROC':>4} {'AGE':>6}  {'STATE':<8} DOING", "grey", on))
    if not snap["sessions"]:
        L.append(col("  (no live sessions)", "grey", on))
    for s in snap["sessions"]:
        dot_c = STATE_COLOR.get(s["state"], "yellow")
        mem_c = "red" if s["mem"] > 4 * GB else (
            "yellow" if s["mem"] > 1500 * MB else "reset")
        pct = int(100 * s.get("swap", 0) / max(s["mem"], 1))
        swap_c = "red" if pct >= 50 else ("yellow" if pct >= 25 else "grey")
        head = (f"  {col('●', dot_c, on)} {clip(s['name'], 22):<22}"
                f"{col(human(s['mem']).rjust(7), mem_c, on)}"
                f"{col(human(s.get('ram', 0)).rjust(7), 'green', on)}"
                f"{col(human(s.get('swap', 0)).rjust(7), swap_c, on)} "
                f"{s['nproc']:>4} {dur(s['age']):>6}  "
                f"{STATE_LABEL.get(s['state'], s['state']):<9} ")
        L.append(head + col(clip(s["doing"], max(10, w - 76)), "grey", on))
        for c in s["top_children"][:child_cap]:
            wt = f" {c['worktree']}" if c["worktree"] else ""
            L.append(col(f"      ├ {clip(c['tag'] + wt, 42):<42}"
                         f"{human(c['mem']):>7}  {dur(c['age']):>6}"
                         f"  pid {c['pid']}", "grey", on))
        subs = s.get("subagents") or []
        act = s.get("subagents_active") or []
        if subs and (child_cap or act):
            L.append(col(f"      ├ subagents  {len(act)} active · "
                         f"{len(subs) - len(act)} finished   "
                         + col("(run in-process — no pid of their own)", "grey", on),
                         "mag" if act else "grey", on))
            for a in act[:sub_cap]:
                L.append(col(f"      │    · {clip(a['kind'], 30):<30} "
                             f"{clip(a['goal'], max(10, w - 56))}", "grey", on))
        started = s.get("started") or {}
        if started and child_cap:
            bits = [f"{k} ({iso_ago(v[0])})" for k, v in
                    sorted(started.items(), key=lambda kv: kv[1][0], reverse=True)]
            L.append(col(f"      └ started    {clip(' · '.join(bits), w - 20)}",
                         "cyan", on))

    ov = snap.get("overhead") or {}
    if ov.get("n"):
        note = (f"  claude runtime pool   {human(ov['mem'])}  "
                f"({ov['n']} procs · {ov['spares']} idle prewarm "
                f"{human(ov['spare_mem'])})")
        L.append(col(note, "grey", on))
        if ov.get("claimed"):
            L.append(col(f"    {ov['claimed']} claimed session(s) holding "
                         f"{human(ov['claimed_mem'])} — working, not reclaimable",
                         "grey", on))
        if ov.get("stale"):
            L.append(col(f"    ⚠ {ov['stale']} prewarm procs idle >4h holding "
                         f"{human(ov['stale_mem'])} — oldest {dur(ov['oldest'])}"
                         f"   → memmon --reap-spares", "yellow", on))
    L.append("")

    # ---- commands we refused that nobody has re-run
    pend = snap.get("blocked") or []
    if pend:
        clear = (snap.get("pressure") or {}).get("level") in ("HEALTHY", "WATCH")
        L.append(col(f" BLOCKED — AWAITING RE-RUN ({len(pend)})".ljust(w, " "),
                     "green" if clear else "yellow", on))
        for b in pend[-5:]:
            L.append(f"  {clip(b.get('session', '?'), 22):<24}"
                     + col(clip(b.get("cmd", ""), max(10, w - 40)), "grey", on))
        L.append(col("  memory is clear — safe to re-run these now" if clear
                     else "  still under pressure — wait before re-running",
                     "green" if clear else "yellow", on))
        L.append("")

    # ---- orphans
    if snap["orphans"]:
        L.append(col(f" ORPHANS / RUNAWAYS   {human(snap['orphan_total'])}"
                     f" reclaimable".ljust(w, " "), "red" if on else "reset", on))
        L.append(col(f"  {'WHAT':<34}{'MEM':>7} {'AGE':>7}  {'PID':>7}  WHY / BLAME",
                     "grey", on))
        for o in snap["orphans"][:12]:
            why = "orphan (parent dead)" if o["orphaned"] else f"stale {dur(o['age'])}"
            if o["blame"]:
                why += f" · {clip(o['blame'], 22)}"
            wt = f" {o['worktree']}" if o["worktree"] else ""
            L.append(f"  {clip(o['tag'] + wt, 32):<34}"
                     + col(human(o["mem"]).rjust(7), "red", on)
                     + f" {dur(o['age']):>7}  {o['pid']:>7}  "
                     + col(clip(why, max(10, w - 62)), "grey", on))
        L.append(col("  → memmon --reap           preview what would be stopped", "grey", on))
        L.append(col("  → memmon --reap --apply   SIGTERM them; anything left is reported", "grey", on))
        L.append("")

    # ---- build work rolled up by worktree
    if snap.get("worktrees"):
        L.append(col(" WORK BY WORKTREE".ljust(w, " "), "bold", on))
        L.append(col(f"  {'WORKTREE':<32}{'TOTAL':>7}{'RAM':>7}{'SWAP~':>7}"
                     f" {'PROC':>4} {'OLDEST':>7}  WHAT", "grey", on))
        for r in snap["worktrees"][:8]:
            mem_c = "red" if r["mem"] > 6 * GB else (
                "yellow" if r["mem"] > 2 * GB else "reset")
            note = r["tag"]
            if r["n"] >= 5 and "tsc" in r["tag"]:
                note += "  ← full-repo typecheck fan-out"
            if r["orphans"]:
                note += f"  ({r['orphans']} orphaned)"
            spct = int(100 * r.get("swap", 0) / max(r["mem"], 1))
            sc = "red" if spct >= 50 else ("yellow" if spct >= 25 else "grey")
            L.append(f"  {clip(r['name'], 30):<32}"
                     + col(human(r["mem"]).rjust(7), mem_c, on)
                     + col(human(r.get("ram", 0)).rjust(7), "green", on)
                     + col(human(r.get("swap", 0)).rjust(7), sc, on)
                     + f" {r['n']:>4} {dur(r['oldest']):>7}  "
                     + col(clip(note, max(10, w - 76)), "grey", on))
        L.append("")

    # ---- heavy processes belonging to no session, worktree, or known app
    if snap.get("other_heavy"):
        L.append(col(" UNATTRIBUTED HEAVY", "bold", on))
        for o in snap["other_heavy"]:
            L.append(f"  {clip(o['tag'], 34):<36}{human(o['mem']):>7}"
                     f" {dur(o['age']):>7}  " + col(f"pid {o['pid']}", "grey", on))
        L.append("")

    # ---- other apps
    if snap["apps"]:
        owners = snap.get("service_owner") or {}
        L.append(col(" OTHER APPS".ljust(w, " "), "bold", on))
        for name, v in list(snap["apps"].items())[:8]:
            o = owners.get(SERVICE_ALIAS.get(name, name))
            if o:
                why = (col(f'started by "{clip(o["session"], 26)}"', "cyan", on)
                       + col(f"  ·  {clip(o['cmd'], max(10, w - 76))}"
                             f"  {iso_ago(o['ts'])}", "grey", on))
            else:
                why = col("user-launched", "grey", on)
            L.append(f"  {clip(name, 20):<22}{human(v['mem']):>7}"
                     f" {v['n']:>3}p  " + why)
    return L


def render(snap: dict, on: bool = True, max_lines: int | None = None) -> str:
    """Fit the dashboard to the window. Anything taller than the terminal scrolls
    into scrollback on redraw, which reads as a new page being appended rather
    than the display updating — so drop detail before allowing that."""
    L = _build(snap, on)
    if max_lines:
        # Shed the cheapest detail first rather than collapsing everything the
        # moment we are one line over.
        for child_cap, sub_cap in ((4, 3), (3, 2), (2, 1), (1, 1), (1, 0), (0, 0)):
            L = _build(snap, on, child_cap, sub_cap)
            if len(L) <= max_lines:
                break
    if max_lines and len(L) > max_lines:
        hidden = len(L) - max_lines + 1
        L = L[: max_lines - 1] + [
            col(f"  … {hidden} more lines — enlarge the window or use --once", "grey", on)
        ]
    return "\n".join(L)


LEVEL_ICON = {"HEALTHY": "🟢", "WATCH": "🟠", "DANGER": "🔴", "CRITICAL": "🔴"}
# A cached sample older than this is stale for display (not for rates).
STALE_DISPLAY_S = 180


def _status_text(level: str, swap_used: int, reclaimable: int) -> str:
    if level == "UNKNOWN":
        return "memmon: pressure unknown"
    s = f"{LEVEL_ICON.get(level, '')} {human(swap_used)} swap"
    if level != "HEALTHY":
        s += f" · {level}"
    if reclaimable > GB:
        s += f" · {human(reclaimable)} reclaimable"
    return s


def statusline(snap: dict) -> str:
    level = (snap.get("pressure") or {}).get("level", "HEALTHY")
    return _status_text(level, snap["vm"].get("swap_used", 0), snap["orphan_total"])


def cached_statusline(row: dict, now: float) -> str:
    """The status line from the sampler's row; it never blocks on `top`."""
    age = now - row["ts"]
    if age > STALE_DISPLAY_S:
        return f"memmon: no sample for {int(age // 60)} min"
    return _status_text(row.get("pressure", "HEALTHY"), row.get("swap_used", 0),
                        row.get("orphan", 0))


# ------------------------------------------------------------- history/report

class SamplerBudget(BaseException):
    """The sampler run's 40 s wall budget ran out. A BaseException, so _sh's
    `except Exception` can never swallow it."""


SAMPLER_BUDGET_S = 40
SAMPLER_VM_STAT_S = 10
GAP_S = 150                 # a sampling gap: more than this since the previous row
STARVED_NOTICE_S = 300      # awake time a gap needs before it is notified


def notify(text: str, title: str = "memmon", subtitle: str = "",
           run=subprocess.run) -> None:
    """Post one notification. The text reaches osascript only as `on run argv`
    arguments, never inside the script, so a label or a command can never be
    read as AppleScript. Nothing is posted when settings turn notifications off."""
    if CONFIG.get("notifications", True) is False:
        return
    run(["osascript", "-e", "on run argv",
         "-e", "display notification (item 1 of argv) with title (item 2 of argv) "
               "subtitle (item 3 of argv)",
         "-e", "end run", text, title, subtitle],
        capture_output=True, timeout=10)


def gap_record(prev: dict | None, cur: dict) -> dict | None:
    """The sampling gap between the previous row and this reading, or None.

    CLOCK_MONOTONIC_RAW keeps counting through sleep and CLOCK_UPTIME_RAW does
    not, so within one boot their deltas split a gap exactly into time asleep
    and unsampled awake time, however many sleeps it holds. Both clocks start
    afresh at boot, so a changed kern.bootsessionuuid is a reboot and cannot
    be split. Wall ts is for display only, and nothing is back-filled."""
    import memmon_telemetry
    if not prev or prev.get("boot") is None or cur.get("boot") is None:
        return None
    try:
        gap = memmon_telemetry.gap_record(prev, cur, GAP_S)
    except (KeyError, TypeError):
        return None
    if gap is not None:
        gap.update(from_ts=prev.get("ts"), to_ts=cur.get("ts"))
    return gap


def notifiable_gap(gap: dict | None) -> bool:
    """A starved gap with at least 5 min of unsampled awake time."""
    return bool(gap) and gap.get("cause") == "starved" and \
        (gap.get("awake_s") or 0) >= STARVED_NOTICE_S


def _previous_row(boot: str | None) -> dict | None:
    """The newest earlier reading: this boot's pressure.json or latest.json,
    whichever is later by mono; else any row, which then marks a reboot."""
    rows = [r for r in (_read_row(PRESSURE_FILE), _read_row(SNAPSHOT))
            if r and r.get("boot") and isinstance(r.get("mono"), (int, float))]
    same = [r for r in rows if r["boot"] == boot]
    if same:
        return max(same, key=lambda r: r["mono"])
    return rows[0] if rows else None


READING_KEYS = ("free_pct", "swap_used", "swap_total", "ram_total", "load", "ncpu",
                "swapins", "swapouts", "pageins", "pageouts", "page_size",
                "used_bytes", "kernel_level")


def sampler_reading(source=None, clock=None) -> dict:
    """The sampler's pressure phase: two strict reads at least 2 s apart, so
    the paging and swap-growth rates come from inside this run even after a
    gap. sysctls go through ctypes; vm_stat gets 10 s, and when a read fails
    the instantaneous sysctl signals still score as a lower bound. free_delta
    needs the previous pressure file, from this boot and 30-300 s old. The
    result is written to pressure.json atomically before anything slower runs.

    Rates use the reads' own CLOCK_MONOTONIC; the row's mono is
    CLOCK_MONOTONIC_RAW, the gap clock, as is pressure.json's."""
    import memmon_owners
    import memmon_pressure
    import memmon_telemetry as T
    clock = clock or T.SYSTEM_CLOCK
    boot = clock.boot()
    prev_file = _read_row(PRESSURE_FILE)
    prev_row = _previous_row(boot)
    vm, rates, reason = {}, None, None
    try:
        r1 = T.read_pressure_strict(source, clock, vm_stat_timeout=SAMPLER_VM_STAT_S,
                                    budget_s=SAMPLER_VM_STAT_S)
        clock.sleep(max(0.0, RATE_MIN_S - (clock.mono() - r1["mono"])))
        vm = T.read_pressure_strict(source, clock, vm_stat_timeout=SAMPLER_VM_STAT_S,
                                    budget_s=SAMPLER_VM_STAT_S)
        rates = T.rates_between(r1, vm)
    except (T.TelemetryError, ValueError) as exc:
        rates, reason = None, f"strict read failed: {exc}"
        try:
            vm = T.read_instant(source, clock)
        except T.TelemetryError as exc2:
            vm, reason = {}, f"strict read failed: {exc2}"
    vm = {**vm, "kernel_level": vm.get("pressure_level")}
    mono, up, ts = clock.raw(), clock.uptime(), clock.wall()
    same = bool(prev_file) and boot is not None and prev_file.get("boot") == boot \
        and isinstance(prev_file.get("mono"), (int, float))
    age = mono - prev_file["mono"] if same else None
    carried = (prev_file.get("lh_streak", 0) or 0) if same and 0 <= age <= RATE_MAX_S else 0
    free_delta = (T.free_delta_between(prev_file, {**vm, "mono": mono, "boot": boot})
                  if rates is not None and same else None)
    pres = score_pressure(vm, rates, free_delta, carried,
                          "in_run" if rates is not None else None, reason)
    reading = {"ts": ts, "mono": mono, "uptime": up, "boot": boot,
               **{k: vm[k] for k in READING_KEYS if vm.get(k) is not None}}
    gap = gap_record(prev_row, reading)
    record = {**reading, **{k: pres[k] for k in (
        "level", "score", "reasons", "headroom_min", "rates", "rates_source",
        "level_reason", "lh_streak", *RATE_FIELDS, "free_delta_min")},
        "under_pressure": memmon_pressure.under_pressure(
            pres["level"], pres["rates"], vm.get("kernel_level")),
        "gap": gap, "last_gap": gap or (prev_file or {}).get("last_gap"),
        # Kept apart so a later sleep or reboot gap cannot end its 24 h notice.
        "last_starved_gap": gap if notifiable_gap(gap) else (
            (prev_file or {}).get("last_starved_gap"))}
    memmon_owners.write_json_atomic(PRESSURE_FILE, record)
    return {"record": record, "pressure": pres, "vm": vm}


def suggestions_enabled() -> bool:
    return CONFIG.get("pressure_suggestions", True) is not False


def sampler_owners(snap: dict, reading: dict, source=None, ctx=None, clock=None,
                   mono=None) -> dict | None:
    """The sampler's libproc share: S1's owner history and CPU baseline, then
    S2.11's job history on every run, and the suggestions, episode and
    notification only while under_pressure holds. Never calls act."""
    import memmon_owners
    import memmon_pressure as mp
    tick = owners_sampler_tick(source, ctx, clock=clock, mono=mono)
    if tick is None or not suggestions_enabled():
        return tick
    inv, part = tick["inv"], tick["part"]
    prof = load_profile()
    kind_of = mp.Classifier(lambda cmd: classify_command(cmd, prof), job_tokens)
    rec = reading["record"]
    hist = mp.update_job_history(memmon_owners.read_json(JOB_HISTORY, {}), inv,
                                 mp.heavy_pids(inv, kind_of), rec["uptime"], rec["boot"])
    memmon_owners.write_json_atomic(JOB_HISTORY, hist)
    rows = []
    if rec["under_pressure"]:
        from memmon_runner import jobs
        rows = mp.suggestions(inv, part, kind_of, leases=jobs(STATE_DIR), history=hist,
                              ram_bytes=rec.get("ram_total"))
    snap["pressure_suggestions"] = mp.without_tokens(rows)
    gap = rec.get("gap")
    sends, before = mp.run_episode(
        PRESSURE_EPISODE, now_ts=rec["ts"], mono=rec["mono"], boot=rec["boot"],
        pressured=rec["under_pressure"], unknown=rec["level"] == "UNKNOWN",
        gap=bool(gap and gap["cause"] != "reboot"), rows=rows)
    for row in sends:
        # The state says "sent" before the send (at most once). A send that
        # fails gives the job and the 5 min floor back, so the next run retries.
        try:
            notify(suggestion_text(row), "memmon · memory under pressure",
                   "Open memmon to review it; nothing was stopped")
        except Exception:
            mp.rollback_notification(PRESSURE_EPISODE, row["job_id"], rec["mono"], before)
    return tick


def _log_row(snap: dict, reading: dict | None) -> dict:
    vm = snap["vm"]
    p = snap.get("pressure") or {}
    rec = (reading or {}).get("record") or {}
    row = {
        "ts": int(snap["ts"]),
        "ram_used": vm.get("ram_used", 0), "swap_used": vm.get("swap_used", 0),
        "swap_total": vm.get("swap_total", 0), "free_pct": vm.get("free_pct", 0),
        "load": vm.get("load", 0), "orphan": snap["orphan_total"],
        # Counters, so the next run can compute paging rates against this point.
        # null when vm_stat was not read: a defaulted 0 reads as since-boot thrash.
        "swapins": vm.get("swapins"), "swapouts": vm.get("swapouts"),
        "pressure": p.get("level", "?"),
        # Carries the low-headroom streak across process boundaries: every CLI
        # invocation is a fresh process, so without this the streak could never
        # reach 2 and the escalation would never fire outside the live loop.
        "_lh_streak": p.get("lh_streak", 0),
        "sessions": {s["name"]: s["mem"] for s in snap["sessions"]},
        "apps": {k: v["mem"] for k, v in snap["apps"].items()},
        "worktrees": {f"build:{r['name']}": r["mem"] for r in snap.get("worktrees", [])},
        "worktree_tags": {r["name"]: r.get("tag", "") for r in snap.get("worktrees", [])},
        "overhead": (snap.get("overhead") or {}).get("mem", 0),
    }
    row.update(_s2_fields(p, rec, vm))
    if reading is not None:
        row["pressure_suggestions"] = snap.get("pressure_suggestions") or []
        row["suggestions_ts"] = row["ts"]
    else:
        # The live dashboard never scans for suggestions. It carries the
        # sampler's list, the under_pressure it was computed under and when,
        # rather than writing an empty list the gate would trust.
        prev = _read_row(SNAPSHOT) or {}
        row["under_pressure"] = bool(prev.get("under_pressure"))
        row["pressure_suggestions"] = prev.get("pressure_suggestions") or []
        row["suggestions_ts"] = prev.get("suggestions_ts")
    return row


def _s2_fields(p: dict, rec: dict, vm: dict) -> dict:
    """What every row gains in S2: the gap clocks and the honesty fields."""
    import memmon_pressure
    out = {"mono": rec.get("mono", mono_now()), "uptime": rec.get("uptime", uptime_now()),
           "boot": rec.get("boot", vm.get("boot")),
           "level_reason": p.get("level_reason"), "rates": p.get("rates"),
           "rates_source": p.get("rates_source"), "lh_streak": p.get("lh_streak", 0),
           "kernel_level": p.get("kernel_level"),
           "under_pressure": memmon_pressure.under_pressure(
               p.get("level"), p.get("rates"), p.get("kernel_level"))}
    if rec.get("gap"):
        out["gap"] = rec["gap"]
    if rec.get("used_bytes") is not None:
        # The strict reader's "used" (the health card's basis), for usage.
        out["used_bytes"] = rec["used_bytes"]
    return out


def _append_row(row: dict, reading: dict | None = None) -> None:
    import memmon_owners
    import signal
    os.makedirs(STATE_DIR, exist_ok=True)
    # The append and the flag are one step as far as the budget goes: SIGALRM
    # is held until both are done, so an expiry either precedes the row (and a
    # partial row follows) or only re-publishes it, never both.
    held = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    try:
        with open(HISTORY, "a") as fh:
            fh.write(json.dumps(row) + "\n")
        if reading is not None:
            reading["row"] = row
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, held)
    memmon_owners.write_json_atomic(SNAPSHOT, row)


def log_sample(snap: dict, reading: dict | None = None) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    row = _log_row(snap, reading)
    # Notify once on the falling edge, when the machine becomes usable again and
    # something is still waiting to be re-run. Only on the transition, so it
    # cannot nag every minute.
    try:
        with open(SNAPSHOT) as fh:
            was = json.load(fh).get("pressure", "HEALTHY")
    except Exception:
        was = "HEALTHY"
    now_level = row["pressure"]
    pend = load_pending()
    if (was in ("DANGER", "CRITICAL") and now_level in ("HEALTHY", "WATCH")
            and pend):
        try:
            notify(f"{len(pend)} blocked command(s) can be retried", "memmon",
                   "Memory pressure cleared")
        except Exception:
            pass
    try:
        learn(snap)
    except Exception:
        pass

    _append_row(row, reading)
    _trim_history()


def gap_notice(gap: dict) -> str:
    """The copy never says why sampling stopped: an unloaded sampler looks
    the same as a starved one."""
    def hm(ts):
        return time.strftime("%H:%M", time.localtime(ts)) if ts else "?"
    return (f"memmon couldn't sample for {round(gap['awake_s'] / 60)} min while the "
            f"Mac was awake ({hm(gap.get('from_ts'))}–{hm(gap.get('to_ts'))}). "
            "Readings around the gap may be incomplete.")


def write_partial(reading: dict | None) -> dict:
    """The row a budget-killed run leaves: whatever the pressure phase knew."""
    rec = (reading or {}).get("record") or {}
    row = {"ts": int(time.time()), "partial": True,
           "partial_reason": "sampler budget exceeded",
           "mono": rec.get("mono", mono_now()), "uptime": rec.get("uptime", uptime_now()),
           "boot": rec.get("boot")}
    if rec:
        row.update({k: rec[k] for k in ("free_pct", "swap_used", "swap_total", "load",
                                        "swapins", "swapouts", "kernel_level") if k in rec})
        row.update(pressure=rec["level"], _lh_streak=rec.get("lh_streak", 0),
                   lh_streak=rec.get("lh_streak", 0), level_reason=rec.get("level_reason"),
                   rates=rec.get("rates"), rates_source=rec.get("rates_source"),
                   under_pressure=rec.get("under_pressure", False))
        if rec.get("gap"):
            row["gap"] = rec["gap"]
    else:
        row.update(pressure="UNKNOWN", level_reason="sampler budget exceeded",
                   rates="unavailable", under_pressure=False)
    _append_row(row)
    return row


def sampler_run(budget_s: float = SAMPLER_BUDGET_S, source=None, clock=None) -> int:
    """One `memmon --log` run under a 40 s wall budget. launchd never starts a
    second instance while one runs, so without the budget a run whose
    subprocess timeouts add up would hide every interval behind it. On expiry
    the partial row is written atomically and the run still exits 0."""
    import signal

    def expired(signum, frame):
        raise SamplerBudget()
    old = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, budget_s)
    reading, done = None, False
    try:
        reading = sampler_reading(source, clock)
        # Posted as soon as pressure.json holds the gap: the slow collection
        # below is exactly what a starved machine may not finish. A gap is
        # discovered by one run only, so this posts once.
        gap = reading["record"].get("gap")
        if notifiable_gap(gap):
            try:
                notify(gap_notice(gap), "memmon", "Sampling gap")
            except Exception:
                pass
        snap = collect(pres=reading["pressure"])
        try:
            sampler_owners(snap, reading)
        except Exception as exc:
            print(f"owners sample failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        log_sample(snap, reading)
        done = True
    except SamplerBudget:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if (reading or {}).get("row"):
            import memmon_owners
            memmon_owners.write_json_atomic(SNAPSHOT, reading["row"])
        elif not done:
            write_partial(reading)
            print("memmon: sampler budget exceeded; partial row written",
                  file=sys.stderr)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    return 0


# Retention. Rows are ~690 bytes, so 1 sample/min is ~1 MB/day.
# Trimming by COUNT rather than by age is deliberate: an age cutoff that is
# further out than the size gate removes nothing once the gate is reached, so
# the sampler rewrites the whole file every single minute and it never shrinks.
# Keeping the last N rows always reduces the file, so a trim happens rarely.
HISTORY_TRIM_AT = 12 * MB          # ~12 days
HISTORY_KEEP_ROWS = 10_080         # 7 days at 1/min
ERRLOG_TRIM_AT = 1 * MB


def _replace_lines(path: str, lines: list) -> None:
    """Rewrite a file through a unique temp file and os.replace, so an
    interruption (the sampler's budget is an asynchronous BaseException)
    leaves either the old file or the new one, never a truncated one."""
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.writelines(lines)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _trim_history() -> None:
    try:
        if os.path.getsize(HISTORY) >= HISTORY_TRIM_AT:
            with open(HISTORY) as fh:
                rows = fh.readlines()
            _replace_lines(HISTORY, rows[-HISTORY_KEEP_ROWS:])
    except Exception:
        pass
    # launchd appends the sampler's stderr forever; nothing else bounds it.
    try:
        err = os.path.join(STATE_DIR, "sampler.err")
        if os.path.getsize(err) > ERRLOG_TRIM_AT:
            with open(err) as fh:
                tail = fh.readlines()[-200:]
            _replace_lines(err, tail)
    except Exception:
        pass

def report(days: int) -> str:
    if not os.path.isfile(HISTORY):
        return "No history yet. Run `memmon --log` on a schedule first."
    cutoff = time.time() - days * 86400
    rows = []
    with open(HISTORY) as fh:
        for ln in fh:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("ts", 0) >= cutoff:
                rows.append(r)
    if not rows:
        return f"No samples in the last {days}d."

    agg: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        for src in ("sessions", "apps", "worktrees"):
            for k, v in (r.get(src) or {}).items():
                agg[k].append(v)

    L = [f"memmon report · {len(rows)} samples over {days}d "
         f"({time.strftime('%Y-%m-%d %H:%M', time.localtime(rows[0]['ts']))} → "
         f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(rows[-1]['ts']))})", ""]
    # A partial row (sampler budget exceeded) lacks what it never collected,
    # so each average runs over the rows that carry its field.
    swapped = [r for r in rows if isinstance(r.get("swap_used"), (int, float))]
    if swapped:
        swaps = [r["swap_used"] for r in swapped]
        peak = max(swapped, key=lambda r: r["swap_used"])
        L.append(f"  swap   avg {human(sum(swaps) // len(swaps))}   "
                 f"peak {human(peak['swap_used'])} at "
                 f"{time.strftime('%b %d %H:%M', time.localtime(peak['ts']))}")
    # Report the recorded verdict, not a free_pct threshold that has never been
    # crossed on this machine (min observed 18 across 2,487 rows). UNKNOWN is
    # not a score, so it counts as unscored.
    bad = sum(1 for r in rows if r.get("pressure") in ("DANGER", "CRITICAL"))
    known = sum(1 for r in rows
                if r.get("pressure") in ("HEALTHY", "WATCH", "DANGER", "CRITICAL"))
    unscored = len(rows) - known
    if known:
        L.append(f"  time at DANGER or worse: {bad * 100 // known}% "
                 f"of {known} scored samples"
                 + (f" ({unscored} unscored)" if unscored else ""))
    partial = sum(1 for r in rows if r.get("partial"))
    if partial:
        L.append(f"  {partial} partial sample(s): the sampler ran out of its 40 s budget")
    gaps = [r["gap"] for r in rows if isinstance(r.get("gap"), dict)]
    if gaps:
        L.append(f"  sampling gaps ({len(gaps)}):")
        for g in gaps:
            frm = (time.strftime("%b %d %H:%M", time.localtime(g["from_ts"]))
                   if g.get("from_ts") else "?")
            to = (time.strftime("%H:%M", time.localtime(g["to_ts"]))
                  if g.get("to_ts") else "?")
            if g.get("cause") == "reboot":
                L.append(f"    {frm} → {to}  reboot")
            else:
                L.append(f"    {frm} → {to}  {g.get('cause')}: "
                         f"{dur(int(g.get('awake_s', 0)))} awake unsampled"
                         + (f", {dur(int(g.get('asleep_s') or 0))} asleep"
                            if g.get("asleep_s") else ""))
    L.append("")
    L.append(f"  {'OWNER':<32}{'AVG':>8}{'PEAK':>8}{'SEEN':>7}")
    for name, vals in sorted(agg.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        if len(vals) < 2:
            continue
        L.append(f"  {clip(name, 30):<32}"
                 f"{human(sum(vals) // len(vals)):>8}{human(max(vals)):>8}"
                 f"{len(vals) * 100 // len(rows):>6}%")
    return "\n".join(L)


# ------------------------------------------------------------------- reaping

STALE_SPARE = 4 * 3600


def _orphan_still_selected(inv, pid: int) -> bool:
    """The orphan selector, re-applied to fresh data at stop time."""
    p = inv.procs.get(pid)
    if p is None or not REAPABLE.search(inv.cmdline(pid)):
        return False
    return p.ppid == 1 or inv.ts - p.start[0] >= 3600


def _spare_still_selected(inv, pid: int) -> bool:
    """The stale-prewarm selector, re-applied: still an unclaimed spare, still
    older than the cutoff. A spare claimed since the listing is never touched."""
    p = inv.procs.get(pid)
    cmd = inv.cmdline(pid)
    return (p is not None and "bg-spare" in cmd and spare_is_idle(cmd)
            and inv.ts - p.start[0] > STALE_SPARE)


def _stop_report(out: dict, what: str) -> str:
    """Plain-text account of an engine outcome for the legacy commands."""
    r = out.get("result")
    if r == "refused":
        return "\n".join([f"refused: {out.get('reason')}"]
                         + [f"left alone: pid {row['pid']} ({row['reason']})"
                            for row in out.get("refused") or []])
    if r == "error":
        return f"error: {out.get('reason')}"
    if r == "already_exited":
        return f"{what}: already exited — nothing was signalled"
    if r == "would_stop":
        L = [f"{what}: would send SIGTERM to {len(out['would_signal'])} process(es):"]
        L += [f"  {row['pid']:>7}  {row['argv0']}" for row in out["would_signal"]]
        L += [f"  {row['pid']:>7}  {row['argv0']}  (memmon run wrapper: would stop once its "
              "job ends)" for row in out.get("would_hold") or []]
        L += [f"would keep running: {row}" for row in out.get("kept") or []]
        out = {**out, "kept": []}
    elif "named" in out:                     # a force result
        gone, killed = out.get("exited", 0), out.get("killed", 0)
        alone = out.get("exited_unsignalled", 0)
        L = [f"{what}: {r} — {gone} of {out['named']} named survivor(s) gone: "
             f"{killed} killed by SIGKILL, {gone - killed - alone} had already exited"]
        if alone:
            L.append(f"{alone} ended on {'its' if alone == 1 else 'their'} own while protected")
    elif out.get("reason") == "root_exited":
        L = [f"{what}: the job had already exited, but processes it started are "
             "still running in its group (nothing was signalled)"]
    else:
        L = [f"{what}: {r} — {out.get('exited', 0)} of {out.get('captured', 0)} "
             f"process(es) exited after SIGTERM"]
    before, after = out.get("used_bytes_before"), out.get("used_bytes_after")
    if before is not None and after is not None:
        delta = before - after
        L.append(f"used memory {human(abs(delta))} {'lower' if delta >= 0 else 'higher'}"
                 " at the next sample (measured; other apps also change)")
    for row in out.get("kept") or []:
        L.append(f"kept running: {row}")
    for row in out.get("refused") or []:
        L.append(f"left alone: pid {row['pid']} ({row['reason']})")
    if out.get("remaining"):
        L.append(f"{len(out['remaining'])} still running:")
        for row in out["remaining"]:
            note = ""
            if row.get("role") == "runner":
                note = "  (memmon run wrapper, never signalled: it exits once its job ends)"
            elif row.get("skip_reason") == "signal_failed":
                note = "  (SIGKILL could not be delivered)"
            elif row.get("skip_reason") == "protected":
                note = "  (not force-killed: it is protected now)"
            elif not row.get("signalled", row.get("forceable")):
                note = ("  (signalled, but the signal was not delivered)" if row.get("forceable")
                        else "  (observed, never signalled)")
            L.append(f"  {row['pid']:>7}  {row['argv0']}{note}")
    if out.get("observed_unlisted"):
        L.append(f"{out['observed_unlisted']} more still running that memmon could not list "
                 "(re-counted in the groups they were seen in)")
    if out.get("watch_error"):
        L.append(f"the respawn watch stopped early ({out['watch_error']}); a daemon "
                 "restart after this point would not be reported")
    if out.get("reason") == "outside_force":
        L.append("Force only touches the survivors the stop signalled; the rest "
                 "were observed in the group and are left alone.")
    return "\n".join(L)


def _force_hint(out: dict, command: str) -> str:
    import memmon_act
    return (f"\nNothing was force-killed. To force the {out.get('forceable', 0)} captured "
            f"survivor(s) (the token expires in {memmon_act.TOKEN_TTL_S} s):\n"
            f"  {command} {out['force_token']}")


SELECTORS = {"orphan": lambda inv, pid: _orphan_still_selected(inv, pid),
             "spare": lambda inv, pid: _spare_still_selected(inv, pid)}


def _reap_run(pids: list, selector: str, what: str, apply: bool, engine=None) -> tuple:
    """Run the reap selection through the engine: a dry run reports what
    --apply would signal and refuse; --apply stops at partial. The listing
    comes from ps and has no start time, so each target's identity is read
    here, once, and the engine refuses a PID that holds another process by
    the time it looks; a PID reused before this read is caught only by the
    selector, which the engine re-applies too."""
    eng = engine or _engine()
    ids = []
    for pid in pids:
        p = eng.source.read(pid)
        ids.append((pid, list(p.start)) if p is not None and p.start else (pid, None))
    out = eng.reap(ids, SELECTORS[selector], selector, dry_run=not apply)
    text = _stop_report(out, what)
    if out.get("force_token"):
        text += _force_hint(out, "memmon reap --force")
    return text, out


def reap_spares_report(snap: dict, apply: bool, engine=None) -> tuple:
    """Idle prewarm processes older than 4h. The daemon keeps a warm pool and is
    meant to recycle it; when it doesn't, these just hold memory. Stopping one is
    safe — the pool respawns on demand — so only the stale ones are targeted."""
    ov = snap.get("overhead") or {}
    items = ov.get("items", [])
    stale = [i for i in items if i["stale"]]
    if not stale:
        oldest = max((i["age"] for i in items), default=0)
        return (f"No stale prewarms. Idle pool: {ov.get('spares', 0)} spares, "
                f"{human(ov.get('spare_mem', 0))}, oldest {dur(oldest)}.\n"
                f"{ov.get('claimed', 0)} claimed session(s) holding "
                f"{human(ov.get('claimed_mem', 0))} are working and excluded."), None
    L = [f"{'PID':>7}  {'MEM':>7} {'IDLE':>7}"]
    for i in sorted(stale, key=lambda x: -x["mem"]):
        L.append(f"{i['pid']:>7}  {human(i['mem']):>7} {dur(i['age']):>7}")
    total = sum(i["mem"] for i in stale)
    L.append("")
    L.append(f"{len(stale)} idle prewarm procs · {human(total)} reclaimable")
    text, out = _reap_run([i["pid"] for i in stale], "spare", "prewarm reap", apply, engine)
    L.append(text)
    if not apply and out.get("result") == "would_stop":
        L.append("dry run — re-run with --apply to stop these.")
    return "\n".join(L), out


def reap_report(snap: dict, apply: bool, engine=None) -> tuple:
    """Orphaned or stale build processes. --apply sends SIGTERM, identity-checked
    per PID, and stops at a partial result: anything still running is listed
    with a `memmon reap --force` token that names exactly those processes."""
    targets = snap["orphans"]
    if not targets:
        return "Nothing to reap — no orphaned or stale build processes.", None
    L = [f"{'PID':>7}  {'MEM':>7} {'AGE':>7}  WHAT"]
    for o in targets:
        L.append(f"{o['pid']:>7}  {human(o['mem']):>7} {dur(o['age']):>7}  "
                 f"{o['tag']} {o['worktree']}"
                 + ("  [orphan]" if o["orphaned"] else "  [stale]"))
    L.append("")
    L.append(f"total reclaimable: {human(snap['orphan_total'])}")
    text, out = _reap_run([o["pid"] for o in targets], "orphan", "reap", apply, engine)
    L.append(text)
    if not apply and out.get("result") == "would_stop":
        L.append("dry run — re-run with --apply to stop these.")
    return "\n".join(L), out


def reap(snap: dict, apply: bool, engine=None) -> str:
    return reap_report(snap, apply, engine)[0]


def reap_spares(snap: dict, apply: bool, engine=None) -> str:
    return reap_spares_report(snap, apply, engine)[0]


def _apply_exit(out, apply: bool) -> int:
    import memmon_act
    return memmon_act.exit_code(out) if apply and out is not None else 0


# -------------------------------------------------------------------- the gate

# Commands worth gating: each can add gigabytes. Classification is deliberately
# position-aware. A tool name inside `cat vitest.config.ts`, an echo string, a
# grep pattern, or a path is data, not an executable.
_PACKAGE_LAUNCHERS = {"pnpm", "npm", "yarn", "bun", "turbo"}
_PACKAGE_VERBS = {"typecheck", "build", "test", "install", "dev", "lint"}
_OPTION_TAKES_VALUE = {"--filter", "--dir", "--cwd", "--workspace", "-w", "-C",
                       "--scope", "--since", "--concurrency"}
_DIRECT_TOOLS = {"tsc", "vitest", "jest", "playwright", "pytest", "gradle",
                 "gradlew", "bazel", "xcodebuild", "webpack", "make"}


def _classification(source: str = "none", rule: str | None = None,
                    shape: str | None = None, samples: int | None = None,
                    peak: int | None = None, block_eligible: bool = False) -> dict:
    return {
        "matched": source != "none", "source": source, "rule": rule,
        "shape": shape, "samples": samples, "observed_peak_bytes": peak,
        "block_eligible": block_eligible,
    }


def _skip_options(args: list[str], start: int = 0) -> int:
    i = start
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return i + 1
        if not arg.startswith("-"):
            return i
        name = arg.split("=", 1)[0]
        i += 2 if name in _OPTION_TAKES_VALUE and "=" not in arg else 1
    return i


def _builtin_for_tokens(tokens: list[str]) -> dict | None:
    toks = _command_tokens(tokens)
    if not toks:
        return None
    exe = os.path.basename(toks[0]).lower()
    args = toks[1:]

    if exe in _DIRECT_TOOLS:
        return _classification("builtin", exe, exe, block_eligible=True)

    if exe in _PACKAGE_LAUNCHERS:
        i = _skip_options(args)
        # `run` and `watch` are transparent package-runner subcommands; options
        # may appear on either side of them.
        if i < len(args) and args[i].lower() in ("run", "watch"):
            i = _skip_options(args, i + 1)
        if i < len(args):
            verb = args[i].lower().split(":", 1)[0]
            if verb in _PACKAGE_VERBS:
                return _classification("builtin", f"{exe} … {verb}",
                                       f"{exe} {verb}", block_eligible=True)
            if verb in _DIRECT_TOOLS:
                return _classification("builtin", verb, f"{exe} {verb}",
                                       block_eligible=True)

    if exe == "npx":
        i = _skip_options(args)
        if i < len(args):
            tool = os.path.basename(args[i]).lower()
            if tool in _DIRECT_TOOLS:
                return _classification("builtin", tool, f"npx {tool}",
                                       block_eligible=True)

    if exe == "docker":
        i = _skip_options(args)
        words = [a.lower() for a in args[i:i + 3]]
        if words and words[0] == "compose":
            words = words[1:]
        if words and words[0] in ("up", "build", "run"):
            is_compose = bool(args[i:i + 1]) and args[i].lower() == "compose"
            rule = "docker " + ("compose " if is_compose else "") + words[0]
            return _classification("builtin", rule, rule, block_eligible=True)

    if exe == "cargo" and args and args[0].lower() in ("build", "test"):
        rule = f"cargo {args[0].lower()}"
        return _classification("builtin", rule, rule, block_eligible=True)
    if exe == "colima" and args and args[0].lower() == "start":
        return _classification("builtin", "colima start", "colima start",
                               block_eligible=True)
    if exe == "next" and args and args[0].lower() == "build":
        return _classification("builtin", "next build", "next build",
                               block_eligible=True)
    if exe == "expo" and args and args[0].lower() in ("start", "run"):
        rule = f"expo {args[0].lower()}"
        return _classification("builtin", rule, rule, block_eligible=True)
    return None


def classify_command(cmd: str, profile: dict | None = None) -> dict:
    """Describe why a command is checked, or return source=none.

    Built-ins are block-eligible. Learned rules are always warning-only: the
    sampler's evidence can add a useful caution but can never refuse work.
    """
    commands = shell_commands(cmd)
    for tokens in commands:
        if hit := _builtin_for_tokens(tokens):
            return hit

    prof = load_profile() if profile is None else profile
    for shape in normalise_cmd(cmd):
        entry = prof.get(shape) or {}
        if (entry.get("n", 0) >= LEARN_MIN_SAMPLES
                and entry.get("peak", 0) >= LEARN_HEAVY_AT):
            return _classification("learned", shape, shape,
                                   entry.get("n"), entry.get("peak"), False)
    shape = next(iter(normalise_cmd(cmd)), None)
    return _classification(shape=shape)


def is_heavy(cmd: str) -> bool:
    """Compatibility predicate; new callers should retain classify_command()."""
    return bool(classify_command(cmd)["matched"])


class _PositionAwareHeavyMatcher:
    """Compatibility for diagnostics that previously called HEAVY_CMD.search."""
    def search(self, cmd: str):
        return next((hit for tokens in shell_commands(cmd)
                     if (hit := _builtin_for_tokens(tokens))), None)


HEAVY_CMD = _PositionAwareHeavyMatcher()


def suggestion_text(row: dict) -> str:
    """One pressure suggestion as a sentence: "vitest in acme-web holds 8.8 GB
    and has been idle 31 min." Idle and growing are labels, never reasons to
    stop anything."""
    bits = [f"{row['label']} holds {human(row['footprint'])}"]
    if row.get("idle_s"):
        bits.append(f"has been idle {row['idle_s'] // 60} min")
    elif (row.get("growth_mb_min") or 0) > 0:
        bits.append(f"is growing {row['growth_mb_min']:.0f} MB/min")
    return " and ".join(bits) + "."


def top_suggestion(cached: dict, now: float) -> dict | None:
    """The sampler's top suggestion, only while its row is at most 180 s old
    and says under_pressure. The gate reads nothing else, so its budget holds."""
    if not suggestions_enabled() or not cached.get("under_pressure"):
        return None
    made = cached.get("suggestions_ts", cached.get("ts")) or 0
    if not 0 <= now - made <= STALE_DISPLAY_S:
        return None
    rows = cached.get("pressure_suggestions") or []
    return rows[0] if rows and isinstance(rows[0], dict) else None


def gate_decision(tool: str, cmd: str, pres: dict, cached: dict,
                  mode: str, classification: dict | None = None,
                  now: float | None = None) -> tuple[str, str]:
    """Pure decision, so it can be tested without provoking real memory pressure.

    Returns (action, message) where action is allow | warn | block."""
    classification = classification or classify_command(cmd)
    if mode == "off" or tool != "Bash" or not classification["matched"]:
        return "allow", ""

    level = pres.get("level", "HEALTHY")
    # UNKNOWN allows silently and injects nothing: the gate fails open.
    if level in ("HEALTHY", "UNKNOWN"):
        return "allow", ""

    why = " · ".join(pres.get("reasons") or []) or level
    bits = [f"System memory pressure is {level} ({why})."]

    # Name whatever is actually holding memory, so the agent knows this is not
    # its own doing — including a browser, which is routinely larger than every
    # session put together and which no amount of scoping a build will help.
    tags = cached.get("worktree_tags") or {}
    holders = []
    for name, mem in (cached.get("worktrees") or {}).items():
        clean = name.replace("build:", "")
        holders.append((clean, mem, tags.get(clean, ""), "worktree"))
    for name, mem in (cached.get("apps") or {}).items():
        holders.append((name, mem, name, "app"))
    holders.sort(key=lambda h: -h[1])
    hot = holders[:2]
    any_build = False
    for name, mem, tag, kind in hot:
        if mem <= 2 * GB:
            continue
        if kind == "app":
            bits.append(f"{name} is holding {human(mem)}.")
        elif is_build_tag(tag):
            any_build = True
            bits.append(f"{name} is running {tag} holding {human(mem)}.")
        elif tag:
            bits.append(f"{name} has {tag} resident holding {human(mem)}.")
        else:
            bits.append(f"{name} is holding {human(mem)}.")
    top = top_suggestion(cached, time.time() if now is None else now)
    if top is not None:
        bits.append(suggestion_text(top) + " Ask the user before stopping it; "
                    "do not stop it yourself.")
    room = pres.get("headroom_min")
    if room is not None and room < 30:
        bits.append(f"At the current rate, about {room:.0f} min until free memory "
                    f"reaches the {HEADROOM_FLOOR} % floor (trend estimate).")

    # "warn" never blocks, whatever the level — that is the point of the mode.
    blocking = classification.get("block_eligible", False) and (
        (mode in ("block", "block-critical") and level == "CRITICAL")
        or (mode == "block" and level == "DANGER"))
    if blocking:
        bits.append(
            "Do NOT start this command now — it would likely freeze the machine "
            "and lose work in every session. Either wait and retry, or scope it "
            "down (for example `pnpm --filter <package> typecheck` instead of a "
            "full-repo run). Check with `memmon --once`.")
        return "block", " ".join(bits)

    bits.append("Prefer a scoped command (`pnpm --filter <package> …`) or wait "
                "for the other build to finish." if any_build or not hot else
                "Consider whether that process is still needed before starting "
                "more work.")
    return "warn", " ".join(bits)


PENDING = os.path.join(STATE_DIR, "blocked.json")
# A pressure block is temporary: after this long the session has retried in
# some form or moved on, so the entry stops asking to be re-run.
PENDING_TTL_S = 2 * 3600
# Launch wrappers the short form strips, with the options of each that take a value.
WRAPPERS = {"timeout": {"-s", "-k", "--signal", "--kill-after"},
            "gtimeout": {"-s", "-k", "--signal", "--kill-after"},
            "nice": {"-n"}, "time": set(), "caffeinate": {"-t", "-w"},
            "env": {"-u", "-S", "-P", "--unset"}, "command": set()}


def session_name_for(session_id: str) -> str:
    """Job short id is the first segment of the session uuid."""
    short = (session_id or "")[:8]
    return lookup_session_name(short) or short


def lookup_session_name(session_id: str) -> str | None:
    """Name at this instant, or None; historical callers must not re-resolve."""
    short = (session_id or "")[:8]
    try:
        with open(os.path.join(JOBS_DIR, short, "state.json")) as fh:
            state = json.load(fh)
        if state.get("name"):
            return state["name"]
        words = str(state.get("intent") or "").split()
        return " ".join(words[:5]) if words else None
    except Exception:
        return None


def display_command(cmd: str) -> str:
    """Compact presentation copy while retaining `cmd` unchanged in the event."""
    text = " ".join((cmd or "").split())
    # A leading directory change is context, not the operation the user needs to
    # recognise. Only remove it when it is its own `&&` segment.
    text = re.sub(r"^cd\s+(?:'[^']*'|\"[^\"]*\"|\S+)\s*&&\s*", "", text, count=1)
    if HOME:
        text = text.replace(HOME + "/", "~/")
    return text


def gate_installed() -> bool:
    """True only when a PreToolUse hook points at memmon-gate."""
    try:
        with open(os.path.join(HOME, ".claude", "settings.json")) as fh:
            settings = json.load(fh)
        return any("memmon-gate" in hook.get("command", "")
                   for entry in settings.get("hooks", {}).get("PreToolUse", [])
                   for hook in entry.get("hooks", []))
    except Exception:
        return False


def _read_gate_rows(limit: int | None = 400) -> list[dict]:
    """Parsed gate-log rows, newest last. One reader, so the CLI and the menu
    bar can never disagree about which rows they counted."""
    try:
        with open(GATE_LOG) as fh:
            lines = fh.readlines()
    except Exception:
        return []
    if limit:
        lines = lines[-limit:]          # slice before parsing, not after
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


GATE_MODES = ("block-critical", "block", "warn", "off")


def gate_mode() -> tuple:
    """(mode, source). MEMMON_GATE in the hook's environment wins, then
    config.json's gate_mode (`memmon settings set gate_mode …`), then the
    default. config.json is already loaded at import, so this costs nothing."""
    env = os.environ.get("MEMMON_GATE")
    if env is not None:
        return env, "env"
    cfg = CONFIG.get("gate_mode")
    if cfg in GATE_MODES:
        return cfg, "config"
    return "block-critical", "default"


def gate_stats(limit: int | None = None) -> dict:
    """Retained decisions plus inspectable warning/stop events for the UI."""
    rows = _read_gate_rows(limit)
    acts = defaultdict(int)
    for r in rows:
        acts[r.get("action", "?")] += 1
    lat = sorted(r.get("ms", 0) for r in rows if r.get("action") != "error")
    paused = pause_until()
    mode = gate_mode()[0]
    if mode not in GATE_MODES:
        mode = "block-critical"
    pending = load_pending()
    def is_pending(sid: str, cmd: str) -> bool:
        return any((p.get("session_id") or "")[:8] == sid
                   and (p.get("cmd", "") == cmd
                        or p.get("cmd", "").startswith(cmd)
                        or cmd.startswith(p.get("cmd", "")))
                   for p in pending if cmd and p.get("cmd"))

    events = []
    for r in rows:
        if r.get("action") not in ("warn", "block"):
            continue
        sid = (r.get("session") or "")[:8]
        cmd = r.get("cmd") or ""
        classification = r.get("classification")
        legacy = not isinstance(classification, dict)
        events.append({
            "ts": r.get("ts", 0), "action": r.get("action", "warn"),
            "mode": r.get("mode") or "block-critical",
            # Never resolve an old job here: a missing historical name must stay
            # unknown rather than being explained with mutable current state.
            "session": {"id": sid, "name": r.get("session_name")},
            "command": {"raw": cmd,
                        "display": r.get("cmd_display") or display_command(cmd),
                        "short": short_command(cmd)},
            "classification": None if legacy else classification,
            "legacy": legacy,
            "pressure": {"level": r.get("level") or "?",
                         "score": r.get("score"),
                         "reasons": r.get("reasons") or []},
            "retry_status": ("waiting" if is_pending(sid, cmd)
                             else "not_waiting"),
            "ms": r.get("ms", 0),
        })

    pending_json = [{
        "ts": p.get("ts", 0),
        "session": {"id": (p.get("session_id") or "")[:8],
                    "name": p.get("session")},
        "id": pending_id(p),
        "command": {"raw": p.get("cmd", ""),
                    "display": display_command(p.get("cmd", "")),
                    "short": short_command(p.get("cmd", ""))},
        "pressure_level": p.get("level") or "?",
        "event_retained": any(
            e["retry_status"] == "waiting"
            and e["session"]["id"] == (p.get("session_id") or "")[:8]
            and (p.get("cmd", "") == e["command"]["raw"]
                 or p.get("cmd", "").startswith(e["command"]["raw"])
                 or e["command"]["raw"].startswith(p.get("cmd", "")))
            for e in events),
    } for p in pending]
    first_ts = rows[0].get("ts", 0) if rows else time.time()
    last_ts = rows[-1].get("ts", 0) if rows else None
    evaluated = acts["allow"] + acts["warn"] + acts["block"]
    result = {
        "installed": gate_installed(),
        "paused": bool(paused),
        "paused_until": (None if paused in (0, float("inf")) else paused),
        "policy": {"mode": mode},
        "counts": {"since": first_ts, "complete": False,
                   "evaluated": evaluated, "warned": acts["warn"],
                   "stopped": acts["block"], "errors": acts["error"]},
        "history": {"from": first_ts, "to": last_ts,
                    "truncated": bool(rows), "evaluated": evaluated,
                    "warned": acts["warn"], "stopped": acts["block"],
                    "events": events},
        "pending_retry": pending_json,
        # Retained for the diagnostic CLI; the popover intentionally omits
        # silent-pass counts and latency.
        "total": len(rows), "allow": acts["allow"], "warn": acts["warn"],
        "block": acts["block"], "error": acts["error"],
        "healthy": acts["error"] == 0,
        "span_s": int((last_ts or first_ts) - first_ts),
        "p50_ms": lat[len(lat) // 2] if lat else 0,
        "p95_ms": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0,
    }
    return result


def load_pending() -> list[dict]:
    """Outstanding blocked commands, without any older than PENDING_TTL_S."""
    try:
        with open(PENDING) as fh:
            items = json.load(fh)
    except Exception:
        return []
    now = time.time()
    return [i for i in items if now - (i.get("ts") or 0) < PENDING_TTL_S]


def pending_id(item: dict) -> str:
    return f"{int((item.get('ts') or 0) * 1000)}.{(item.get('session_id') or '')[:8]}"


def dismiss_pending(item_id: str) -> bool:
    """Drop one outstanding entry by its id. False when nothing matched."""
    items = load_pending()
    kept = [i for i in items if pending_id(i) != item_id]
    if len(kept) == len(items):
        return False
    save_pending(kept)
    return True


def short_command(cmd: str, limit: int = 60) -> str:
    """The operation itself, e.g. `pnpm test:affected`: the heavy segment of a
    pipeline, without wrappers such as `timeout 1500` or its redirections."""
    commands = shell_commands(cmd)
    plain = [t for t in commands if t and t[0] != "cd"] or commands
    tokens = next((t for t in commands if _builtin_for_tokens(t)),
                  plain[0] if plain else [])
    out: list[str] = []
    for t in tokens:
        if re.match(r"^\d*[<>&|]", t):
            break
        out.append(t)
    while out and (out[0] in WRAPPERS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", out[0])):
        head = out.pop(0)
        takes = WRAPPERS.get(head, set())
        while out and out[0].startswith("-"):
            if out.pop(0) in takes and out:
                out.pop(0)
        if head in ("timeout", "gtimeout") and out and re.match(r"^[\d.]+[smhd]?$", out[0]):
            out.pop(0)
    # A path is shown by its last component: `../../node_modules/.bin/tsc`
    # reads as `tsc`, and a test file as its file name.
    out = [os.path.basename(t.rstrip("/")) or t if "/" in t and not t.startswith("-") else t
           for t in out]
    text = " ".join(out) or display_command(cmd)
    if HOME:
        text = text.replace(HOME + "/", "~/")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def save_pending(items: list[dict]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = PENDING + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(items[-50:], fh)
    os.replace(tmp, PENDING)  # atomic: several sessions may write at once


def record_block(payload: dict, cmd: str, level: str) -> None:
    """Remember a command we refused, so it is not silently lost. This is the
    queue that answers 'what do I need to re-run once memory frees up'."""
    sid = payload.get("session_id", "")
    items = load_pending()
    if any(i["cmd"] == cmd and i["session_id"] == sid for i in items):
        return
    items.append({"ts": time.time(), "session_id": sid,
                  "session": session_name_for(sid), "cmd": cmd,
                  "cwd": payload.get("cwd", ""), "level": level})
    save_pending(items)


def clear_pending(payload: dict, cmd: str) -> None:
    """The same session ran the same command and we allowed it — it is no longer
    outstanding, so drop it rather than nagging forever."""
    sid = payload.get("session_id", "")
    items = load_pending()
    kept = [i for i in items if not (i["cmd"] == cmd and i["session_id"] == sid)]
    if len(kept) != len(items):
        save_pending(kept)


def gate() -> int:
    """PreToolUse hook entry point. Fails open on absolutely everything: a
    monitoring tool must never be the reason a session cannot work."""
    # The wrapper stamps the real start; timing from here would hide Python
    # interpreter startup, which is most of what a session actually waits for.
    try:
        t0 = float(os.environ.get("MEMMON_T0") or 0) or time.time()
    except ValueError:
        t0 = time.time()
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else "{}"
        payload = json.loads(raw or "{}")
        tool = payload.get("tool_name", "")
        cmd = (payload.get("tool_input") or {}).get("command", "")

        if pause_until():
            return 0
        # Expired pause: the shell file still says paused, and on a gate-only
        # install nothing else ever rewrites it. Repair it here or `--off 8h`
        # becomes permanent.
        if os.path.exists(PAUSE):
            try:
                os.remove(PAUSE)
                _write_learned_glob()
            except Exception:
                pass
        classification = classify_command(cmd)
        if tool != "Bash" or not classification["matched"]:
            return 0
        # Heavy path only: the policy (env, then config.json, then default).
        mode, mode_source = gate_mode()
        if mode == "off":
            return 0

        vm = read_vm(fast=True)
        pres = pressure(vm)
        try:
            with open(SNAPSHOT) as fh:
                cached = json.load(fh)
        except Exception:
            cached = {}

        action, msg = gate_decision(tool, cmd, pres, cached, mode, classification)

        # Rolling record of what every session asked to run and what we decided.
        # This is the only cross-session audit trail of the gate, and it is
        # written only for commands heavy enough to be evaluated.
        try:
            path = GATE_LOG
            event_ts = time.time()
            sid = payload.get("session_id", "")[:8]
            with open(path, "a") as fh:
                fh.write(json.dumps({
                    "ts": event_ts, "cmd": cmd, "cmd_display": display_command(cmd),
                    "mode": mode, "mode_source": mode_source,
                    "level": pres.get("level"), "action": action,
                    "session": sid, "session_name": lookup_session_name(sid),
                    "cwd": payload.get("cwd", ""),
                    "score": pres.get("score"),
                    "reasons": pres.get("reasons", []),
                    "level_reason": pres.get("level_reason"),
                    "classification": {k: v for k, v in classification.items()
                                       if k != "matched"},
                    # Measured, so "is this slowing anyone down" is answerable
                    # from data rather than from my estimate.
                    "ms": round((time.time() - t0) * 1000),
                }) + "\n")
            if os.path.getsize(path) > 256_000:
                with open(path) as fh:
                    tail = fh.readlines()[-500:]
                with open(path, "w") as fh:
                    fh.writelines(tail)
        except Exception:
            pass

        if action == "block":
            record_block(payload, cmd, pres.get("level", "?"))
            # Exit code 2 blocks the call and feeds stderr back to the model.
            sys.stderr.write(
                msg + "\n\nThis command has been recorded as outstanding "
                "(`memmon --blocked`); re-run it once pressure clears.\n")
            return 2
        clear_pending(payload, cmd)
        if action == "warn":
            # Plain stdout on exit 0 is NOT fed back to the model — verified by
            # a warn firing in a live session with nothing reaching the
            # transcript. additionalContext is the supported channel for
            # injecting text without blocking the call.
            json.dump({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": msg,
            }}, sys.stdout)
            sys.stdout.write("\n")
        return 0
    except Exception as exc:
        # Still fails open — but silently failing open is indistinguishable from
        # "no heavy commands ran", which makes "is the gate working?"
        # unanswerable. Record it, then get out of the way.
        try:
            with open(GATE_LOG, "a") as fh:
                fh.write(json.dumps({
                    "ts": time.time(), "action": "error",
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                    "cmd": "", "level": "?", "session": "", "ms": 0,
                }) + "\n")
        except Exception:
            pass
        return 0


def end_session(pid: int, apply: bool, engine=None) -> str:
    return end_session_report(pid, apply, engine)[0]


def end_session_report(pid: int, apply: bool, engine=None) -> tuple:
    """End a Claude session (or a codex exec) by hand, tree and all.

    Refuses any pid that is not currently such an owner's ROOT — the caller
    passes a number, and a stale or mistyped one must never reach a process.
    SIGTERM only, identity-checked per PID; nested owners are kept."""
    import memmon_act
    import memmon_owners
    import memmon_procs
    eng = engine or _engine()
    inv = memmon_procs.snapshot(eng.source, clock=eng.clock)
    part = eng.partition_fn(inv)
    oid = part.root_owner.get(pid)
    owner = part.owners.get(oid) if oid else None
    if owner is None or owner.kind not in memmon_owners.ENDABLE_KINDS:
        live = ", ".join(f"{o.owner_id}={o.root}" for o in part.owners.values()
                         if o.kind in memmon_owners.ENDABLE_KINDS)
        return (f"refused: pid {pid} is not a live session root.\n"
                f"live sessions: {live or 'none'}"), memmon_act.outcome("refused", "not_stoppable")
    fp = sum(inv.procs[p].footprint or 0 for p in owner.members)
    if not apply:
        return (f"would end {owner.owner_id} — {human(fp)} across "
                f"{len(owner.members)} process(es), root pid {pid}\n"
                f"re-run with --apply to do it."), None
    root = inv.procs[pid]
    token = memmon_owners.mint_token({
        "v": 1, "action": "end-session", "owner_id": oid,
        "owner_root": memmon_owners.ident(root), "target": memmon_owners.ident(root),
        "snapshot_ts": round(inv.ts, 3)})
    out = eng.run("end-session", token)
    text = _stop_report(out, f"end {oid}")
    if out.get("force_token"):
        text += _force_hint(out, "memmon act force --target")
    return text, out


def wait_safe(timeout: int) -> int:
    """Block until pressure clears. An explicit 'pause until it is safe'
    primitive an agent can call, rather than guessing how long to sleep."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pres = pressure(read_vm(fast=True))
        if pres["level"] in ("HEALTHY", "WATCH"):
            print(f"clear: {pres['level']}")
            return 0
        left = int(deadline - time.time())
        why = " · ".join(pres["reasons"]) or pres.get("level_reason") or ""
        print(f"{pres['level']} — {why} (waiting, {left}s left)", flush=True)
        time.sleep(15)
    print("timed out still under pressure")
    return 1


# ------------------------------------------------------------------- owners

def job_tokens(cmd: str) -> list:
    """The executables of a command line as token lists, a package
    launcher's own options skipped the way the gate skips them
    (`pnpm --filter web dev` runs `dev`)."""
    import memmon_owners
    out = []
    for t in shell_commands(cmd):
        toks = _command_tokens(t)
        if toks and os.path.basename(toks[0]) in memmon_owners.LAUNCHERS:
            toks = toks[:1] + toks[_skip_options(toks, 1):]
        out.append(toks)
    return out


def _owners_ctx(titles: bool = True, leases: list | None = None):
    import memmon_owners
    from memmon_runner import jobs
    ctx = memmon_owners.Context(
        sessions_dir=CLAUDE_SESSIONS_DIR, socks_dir=CC_SOCKS_DIR,
        codex_home=CODEX_HOME, jobs_dir=JOBS_DIR, roster_path=CLAUDE_ROSTER,
        leases=jobs(STATE_DIR) if leases is None else leases, rv_map=map_pids_to_jobs,
        lsof=lambda args: _sh(["lsof", *args], timeout=5))
    if titles:
        prof = load_profile()
        ctx.classify = lambda cmd: classify_command(cmd, prof)
        ctx.commands = job_tokens
        for sess in read_sessions():
            ctx.titles_by_job[sess["short"]] = sess["name"]
            if sess["session_id"]:
                ctx.titles_by_sid[sess["session_id"]] = sess["name"]
    return ctx


def _engine(**kw):
    import memmon_act
    import memmon_owners
    import memmon_procs
    from memmon_runner import jobs
    kw.setdefault("source", memmon_procs.default_source())
    kw.setdefault("partition_fn", lambda inv: memmon_owners.partition(
        inv, _owners_ctx(titles=False)))
    ctx = _owners_ctx(titles=False)
    kw.setdefault("respawn", memmon_owners.RespawnWatch(ctx))
    kw.setdefault("root_rule_fn", lambda inv, pids: memmon_owners.fresh_roots(inv, pids, ctx))
    kw.setdefault("lock_path", ACTIONS_LOCK)
    kw.setdefault("system_reader", memmon_procs.read_system_strict)
    kw.setdefault("leases_fn", lambda: jobs(STATE_DIR))
    return memmon_act.Engine(**kw)


def system_block(reader=None) -> dict:
    """The health card's numbers: the strict reader's "used" and the kernel
    level, plus the existing score verdict. A failed strict read is null with
    its reason, never a healthy-looking default."""
    import memmon_procs
    out = {"ram_bytes": None, "used_bytes": None, "pressure_level": None,
           "score_level": None, "reason": None, "level_reason": None,
           "rates": "unavailable", "rates_source": None}
    shared = []

    def vm_stat_once(cmd, **kw):
        # One vm_stat serves both readers; the strict one still sees its
        # failures as failures.
        if not shared:
            shared.append(subprocess.run(cmd, **kw))
        return shared[0]
    try:
        out.update((reader or (lambda: memmon_procs.read_system_strict(run=vm_stat_once)))())
    except Exception as exc:
        out["reason"] = f"{type(exc).__name__}: {exc}"
    text = shared[0].stdout if shared and shared[0].returncode == 0 else None
    try:
        vm = read_vm(fast=True, vm_stat_text=text)
        p = pressure(vm)
        out.update(score_level=p["level"], level_reason=p.get("level_reason"),
                   rates=p.get("rates", "unavailable"), rates_source=p.get("rates_source"))
        if out["pressure_level"] is None:
            out["kernel_level"] = vm.get("kernel_level")
    except Exception:
        pass
    return out


def runner_snapshot(system: dict) -> dict:
    """`memmon jobs --json` (schema 2), reusing the health card's strict read.
    Display only: nothing here admits."""
    try:
        import memmon_runner
        strict = ({k: system[k] for k in ("ram_bytes", "used_bytes", "pressure_level")}
                  if system.get("used_bytes") is not None else None)
        return memmon_runner.snapshot(STATE_DIR, system=strict)
    except Exception as exc:
        return {"reason": f"{type(exc).__name__}: {exc}"}


def sampler_block(now: float | None = None) -> dict:
    """When the sampler last ran and the last sampling gap it recorded. The
    gap shows only once the next run discovers it; until then, age_s grows."""
    rec = _read_row(PRESSURE_FILE) or {}
    last = rec.get("ts")
    age = (time.time() if now is None else now) - last if isinstance(
        last, (int, float)) else None
    return {"last_ts": last, "age_s": None if age is None else round(age, 1),
            "stale": age is None or age > STALE_DISPLAY_S,
            "last_gap": rec.get("last_gap"),
            "last_starved_gap": rec.get("last_starved_gap")}


def pressure_suggestions(sample, ctx, payload: dict, history=None) -> list:
    """owners --json's suggestion rows, the only place their tokens exist.
    S1's child-job rows are reused by job_id; the rest get S1-format tokens."""
    import memmon_owners
    import memmon_pressure as mp
    s1_jobs = {j["job_id"]: j for o in payload["owners"] for j in o.get("jobs") or []
               if j.get("kind") != "conversation"}
    return mp.suggestions(
        sample.inv, sample.part, mp.Classifier(ctx.classify, ctx.commands),
        leases=ctx.leases, ram_bytes=(payload.get("system") or {}).get("ram_bytes"),
        history=memmon_owners.read_json(JOB_HISTORY, {}) if history is None else history,
        s1_jobs=s1_jobs, mint=True)


def route_status() -> dict:
    """memmon route's own status; "off" whenever it cannot be read."""
    try:
        import memmon_route
        st = memmon_route.status(STATE_DIR)
        return {"state": st.get("state", "off"), "line": st.get("line") or "Route off"}
    except Exception:
        return {"state": "off", "line": "Route off"}


def protection_block(unmanaged: int) -> dict:
    mode = gate_mode()[0]
    if not gate_installed() or mode == "off":
        gate_state = "off"
    elif pause_until():
        gate_state = "paused"
    else:
        gate_state = "on"
    summary = (gate_state if gate_state != "on"
               else "partial" if unmanaged else "on")
    return {"summary": summary, "gate": gate_state, "route": route_status()["state"],
            "unmanaged_heavy": unmanaged}


def coverage_lines(protection: dict) -> list:
    """Truthful coverage (S2.7). It never says "all apps"."""
    n = protection.get("unmanaged_heavy") or 0
    return [route_status()["line"],
            f"{n} heavy process{'es' if n != 1 else ''} not started through memmon run",
            "Codex, other apps and terminals are covered only when they call memmon run"]


def owners_sample(cpu_window: float, source=None, ctx=None, sleep=time.sleep):
    """One owners sample. CPU comes from two snapshots `cpu_window` seconds
    apart, or with a window of 0 from the sampler's persisted baseline when
    that is still valid. Returns (sample, actual window or None)."""
    import memmon_owners
    import memmon_procs
    src = source or memmon_procs.default_source()
    first = memmon_procs.snapshot(src)
    ctx = ctx or _owners_ctx()
    cpu, window, reason = {}, None, "warming up"
    if first.kind == "degraded":
        inv, reason = first, "not measured"
    elif cpu_window > 0:
        sleep(cpu_window)
        inv = memmon_procs.snapshot(src)
        cpu = memmon_procs.cpu_cores(memmon_procs.tick_table(first, 1 << 30),
                                     inv, first.mono_ns)
        window = round((inv.mono_ns - first.mono_ns) / 1e9, 3)
    else:
        inv = first
        base = memmon_owners.read_json(CPU_BASELINE, {})
        if not memmon_owners.baseline_problem(base, inv, memmon_owners.boot_id(),
                                              memmon_owners.awake_ns()):
            cpu = memmon_procs.cpu_cores(base["procs"], inv, int(base["mono_ns"]))
            window = round((inv.mono_ns - int(base["mono_ns"])) / 1e9, 3)
    part = memmon_owners.partition(inv, ctx)
    return memmon_owners.Sample(inv, part, cpu, reason), window


def owners_json(cpu_window: float = 1.0, expand: list | None = None,
                source=None, ctx=None, system_reader=None) -> dict:
    import memmon_owners
    system = system_block(system_reader)
    runner = runner_snapshot(system)
    # The partition and runner_jobs see the same v2 rows.
    ctx = ctx or _owners_ctx(leases=runner.get("jobs"))
    sample, window = owners_sample(cpu_window, source, ctx)
    used_by = None
    if expand:
        owners = sample.part.owners
        # Only agent jobs have a server kind to settle.
        wanted = [p for oid in expand if oid in owners
                  and owners[oid].kind in memmon_owners.ENDABLE_KINDS
                  for p in owners[oid].members]
        ctx.listening = _listening(wanted)
        used_by = {oid: _service_users(sample, oid) for oid in expand
                   if oid in owners and owners[oid].kind == "service"}
    hist = memmon_owners.read_json(OWNERS_HISTORY, {})
    payload = memmon_owners.owners_payload(
        sample, ctx, history=hist, cpu_window_s=window,
        system=system,
        protection=protection_block(memmon_owners.unmanaged_heavy(sample, ctx)),
        gate=gate_stats(), used_by=used_by)
    # A machine-wide CPU figure is only a sum when every member was measured;
    # counted in whole members, never from the rounded per-owner fractions.
    members = [p for o in payload["owners"] if o["owner_id"] in sample.part.owners
               for p in sample.part.owners[o["owner_id"]].members]
    seen = [sample.cpu[p] for p in members if p in sample.cpu]
    system = payload["system"]
    system["ncpu"] = os.cpu_count()
    complete = bool(seen) and len(seen) == len(members)
    system["cpu_coverage"] = memmon_owners.coverage(len(seen), len(members)) if seen else None
    system["cpu_cores"] = round(sum(seen), 3) if complete else None
    if not seen:
        system["cpu_reason"] = sample.cpu_reason or "not measured"
    payload["runner_jobs"] = ctx.leases       # v2 rows, a strict superset of v1
    payload["runner"] = {k: v for k, v in runner.items() if k != "jobs"}
    payload["coverage"] = coverage_lines(payload["protection"])
    import memmon_pressure
    payload["under_pressure"] = memmon_pressure.under_pressure(
        system.get("score_level"), system.get("rates"),
        system.get("pressure_level") or system.get("kernel_level"))
    payload["pressure_suggestions"] = (
        pressure_suggestions(sample, ctx, payload)
        if payload["under_pressure"] and suggestions_enabled() else [])
    payload["sampler"] = sampler_block()
    return payload


def _service_users(sample, oid: str):
    """Sessions that appear to use a VM: agent processes with a TCP connection
    to a port the VM side listens on (Lima's forwards, Docker Desktop's
    backend). Inferred, computed only on demand; None when it cannot be."""
    import memmon_owners
    part = sample.part
    host = list(part.owners[oid].members)
    agents = {p: o.owner_id for o in part.owners.values()
              if o.kind in memmon_owners.ENDABLE_KINDS for p in o.members}
    if not host or not agents:
        return [] if host else None
    listen = _sh(["lsof", "-O", "-b", "-w", "-nP", "-a", "-iTCP", "-sTCP:LISTEN", "-Fn",
                  "-p", ",".join(map(str, sorted(set(host))))], timeout=5)
    ports = {line.rsplit(":", 1)[-1] for line in listen.splitlines()
             if line.startswith("n") and ":" in line}
    if not ports:
        return []
    est = _sh(["lsof", "-O", "-b", "-w", "-nP", "-a", "-iTCP", "-sTCP:ESTABLISHED", "-Fpn",
               "-p", ",".join(map(str, sorted(agents)))], timeout=5)
    users, pid = set(), None
    for line in est.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and "->" in line and pid in agents:
            if line.rsplit(":", 1)[-1] in ports:
                users.add(agents[pid])
    return sorted(users)


def _listening(pids: list) -> set:
    """PIDs among `pids` holding a TCP LISTEN socket (one lsof, on demand)."""
    if not pids:
        return set()
    out = _sh(["lsof", "-O", "-b", "-w", "-nP", "-a", "-iTCP", "-sTCP:LISTEN", "-Fp",
               "-p", ",".join(str(p) for p in sorted(set(pids)))], timeout=5)
    return {int(line[1:]) for line in out.splitlines()
            if line.startswith("p") and line[1:].isdigit()}


def owners_sampler_tick(source=None, ctx=None, clock=None, mono=None) -> dict | None:
    """The sampler's share of the owner view: append one footprint per owner to
    owners-history.json and persist the CPU baseline the next tick (another
    process, a minute later) measures against."""
    import memmon_owners as mo
    import memmon_procs
    src = source or memmon_procs.default_source()
    inv = memmon_procs.snapshot(src, clock=clock, mono=mono)
    if inv.kind == "degraded":
        return None
    part = mo.partition(inv, ctx or _owners_ctx(titles=False))
    boot, awake = mo.boot_id(), mo.awake_ns()
    base = mo.read_json(CPU_BASELINE, {})
    problem = mo.baseline_problem(base, inv, boot, awake)
    cpu = {} if problem else memmon_procs.cpu_cores(base["procs"], inv,
                                                    int(base["mono_ns"]))
    hist = mo.update_history(mo.read_json(OWNERS_HISTORY, {}),
                             mo.history_footprints(part, inv), inv.ts)
    for oid, owner in part.owners.items():
        if oid in hist["owners"]:
            cores, cov = mo._cpu(owner.members, cpu)
            hist["owners"][oid]["cpu"] = [round(inv.ts, 1),
                                          None if cores is None else round(cores, 3),
                                          cov]
    mo.write_json_atomic(OWNERS_HISTORY, hist)
    mo.write_json_atomic(CPU_BASELINE, mo.make_baseline(inv, boot, awake))
    return {"cpu": cpu, "baseline_problem": problem, "owners": len(part.owners),
            "inv": inv, "part": part}


def owners_text(payload: dict) -> str:
    L = [f"{'OWNER':<34}{'KIND':<10}{'MEMORY':>9}{'CPU':>7}  CONFIDENCE"]
    for o in payload["owners"]:
        mem = human(o["footprint_bytes"]) if o["footprint_bytes"] is not None else "—"
        cpu = f"{o['cpu_cores']:.1f}" if o["cpu_cores"] is not None else "—"
        L.append(f"{clip(o['title'], 32):<34}{o['kind']:<10}{mem:>9}{cpu:>7}  "
                 f"{o['confidence']}")
        for j in o["jobs"]:
            jm = human(j["footprint_bytes"]) if j["footprint_bytes"] is not None else "—"
            L.append(f"  └ {clip(j['kind'] + ' · ' + j['label'], 30):<40}{jm:>9}")
    if payload["inventory"] == "degraded":
        L.append(f"inventory degraded ({payload.get('inventory_reason')}): "
                 "memory from top, actions refused")
    L.append(f"system and other users: not itemised "
             f"({payload['hidden_process_count']} processes)")
    rows = payload.get("pressure_suggestions") or []
    if rows:
        L.append("")
        L.append("Under pressure — heavy work memmon run did not start "
                 "(stop only with the user's say-so):")
        for r in rows:
            how = (f"memmon act {r['stop']} --target <token from owners --json>"
                   if r.get("stop") else r.get("stop_note") or "no targeted stop")
            L.append(f"  {suggestion_text(r)}")
            L.append(f"      {how}")
    return "\n".join(L)


def owners_cli(argv: list) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="memmon owners")
    ap.add_argument("--json", action="store_true", help="schema 2 payload")
    ap.add_argument("--cpu-window", type=float, default=1.0,
                    help="seconds between the two CPU samples; 0 reuses the "
                         "sampler's baseline (default 1.0)")
    ap.add_argument("--expand", action="append", metavar="OWNER_ID",
                    help="also look up listening sockets for this owner's jobs")
    args = ap.parse_args(argv)
    payload = owners_json(max(0.0, args.cpu_window), args.expand)
    print(json.dumps(payload) if args.json else owners_text(payload))
    return 0


def act_cli(argv: list, engine=None) -> int:
    """memmon act ACTION --target TOKEN. The outcome JSON always goes to stdout;
    the exit code is 0 done, 3 partial, 4 refused, 1 error."""
    import argparse
    import memmon_act

    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError(message)
    ap = Parser(prog="memmon act")
    ap.add_argument("action", nargs="?", choices=(
        *memmon_act.PROCESS_ACTIONS, *memmon_act.TOKEN_ACTION))
    ap.add_argument("--target", help="token from `memmon owners --json`")
    ap.add_argument("--force", metavar="FORCE_TOKEN",
                    help="SIGKILL the survivors a partial result named")
    ap.add_argument("--lock-fd", type=int, help="inherited actions.lock descriptor")
    try:
        args = ap.parse_args(argv)
        action, token = (("force", args.force) if args.force
                         else (args.action, args.target))
        if not action or not token:
            raise ValueError("give an action and --target TOKEN, or --force FORCE_TOKEN")
    except (ValueError, SystemExit) as exc:
        if isinstance(exc, SystemExit) and not exc.code:
            raise                                   # --help
        out = memmon_act.outcome("refused", "bad_args")
        out["detail"] = str(exc)
        print(json.dumps(out))
        return memmon_act.exit_code(out)
    try:
        out = (engine or _engine()).run(action, token, lock_fd=args.lock_fd)
    except Exception as exc:
        out = memmon_act.outcome("error", f"{type(exc).__name__}: {exc}")
    print(json.dumps(out))
    return memmon_act.exit_code(out)


def reap_cli(argv: list, engine=None) -> int:
    import argparse
    import memmon_act
    ap = argparse.ArgumentParser(prog="memmon reap")
    ap.add_argument("--apply", action="store_true",
                    help="SIGTERM the listed processes; stops at partial")
    ap.add_argument("--force", metavar="FORCE_TOKEN",
                    help="SIGKILL exactly the survivors a partial reap named")
    ap.add_argument("--spares", action="store_true",
                    help="idle claude prewarms older than 4h instead of orphans")
    args = ap.parse_args(argv)
    if args.force:
        import memmon_owners
        try:
            selector = memmon_owners.decode_token(args.force).get("selector")
        except ValueError:
            selector = None
        out = (engine or _engine()).force(args.force, origin="reap",
                                          still_selected=SELECTORS.get(selector))
        print(_stop_report(out, "force"))
        return memmon_act.exit_code(out)
    snap = collect()
    text, out = (reap_spares_report if args.spares else reap_report)(snap, args.apply, engine)
    print(text)
    return _apply_exit(out, args.apply)


# --------------------------------------------------------------------- usage

USAGE_CACHE = os.path.join(STATE_DIR, "runner", "coord", "usage-cache.json")
ADMISSION_LOG = os.path.join(STATE_DIR, "runner", "coord", "admission-log.jsonl")
USAGE_SECTIONS = ("claude", "codex", "browser", "dev", "app", "service", "other")
# Names in a history row's `apps` that are not apps: a VM is a service, the
# window server is the system.
USAGE_VM_NAMES = {"Docker VM", "colima", "lima", "qemu", "Virtualization"}
USAGE_SYSTEM_NAMES = {"WindowServer", "kernel_task", "launchd"}
# What a malformed history, gate or admission row can raise; it is skipped.
BAD_ROW = (TypeError, ValueError, AttributeError, OverflowError, OSError)
_TS_RE = re.compile(r'"ts":\s*([0-9.]+)')


def _day_rows(path: str, start: float):
    """(ts, row) for every JSON line at or after `start`; a line older than
    the window is skipped on its ts alone, without being parsed."""
    try:
        fh = open(path)
    except OSError:
        return
    with fh:
        for line in fh:
            try:
                m = _TS_RE.search(line, 0, 40)
                if m and float(m.group(1)) < start:
                    continue
                row = json.loads(line)
            except ValueError:
                continue
            ts = row.get("ts") if isinstance(row, dict) else None
            if isinstance(ts, (int, float)) and ts >= start:
                yield ts, row


def usage_section(name: str) -> str:
    """The section of one name in a history row's `apps`, decided by the
    same bundle-id sets as the owner list (memmon_owners.category_of)."""
    import memmon_common
    import memmon_owners
    if name in USAGE_VM_NAMES:
        return "service"
    if name in USAGE_SYSTEM_NAMES:
        return "other"
    bid = memmon_common.NAME_BUNDLE.get(name, "")
    if bid in memmon_owners.GUI_VM_APPS:
        return "service"
    # Every other entry is an app: dev, browser or plain app, never "other".
    return memmon_owners.category_of("app", "app:" + bid)


def _row_sections(row: dict) -> dict | None:
    """One history row's memory by section. sessions and the Claude runtime
    pool (overhead) are claude; `apps` names go by usage_section(); worktree
    builds are other. Codex has no field in a history row, so the caller
    reports it as not recorded (null), never zero. Orphans are left out:
    they are mostly the same processes as the worktree builds."""
    if "sessions" not in row:
        return None                                  # a partial row
    if not isinstance(row["sessions"], dict):
        raise TypeError("sessions is not an object")
    out = dict.fromkeys(USAGE_SECTIONS, 0)
    out["claude"] = sum(v for v in row["sessions"].values() if isinstance(v, (int, float)))
    out["claude"] += row.get("overhead") or 0
    for name, mem in (row.get("apps") or {}).items():          # a list raises
        if isinstance(mem, (int, float)):
            out[usage_section(name)] += mem
    out["other"] += sum(v for v in (row.get("worktrees") or {}).values()
                        if isinstance(v, (int, float)))
    return out


def _usage_inputs() -> list:
    sig = []
    for path in (HISTORY, GATE_LOG, ADMISSION_LOG):
        try:
            st = os.stat(path)
            sig.append([path, st.st_size, st.st_mtime_ns])
        except OSError:
            sig.append([path, None, None])
    return sig


def usage(days: int = 7, now: float | None = None) -> dict:
    """`memmon usage --json`: one entry per local day, oldest first, from
    history.jsonl, gate.jsonl and the runner's admission log only. A day
    without samples is empty (samples 0, nulls), never interpolated."""
    now = time.time() if now is None else now
    today = time.localtime(now)
    start_day = time.mktime((today.tm_year, today.tm_mon, today.tm_mday - (days - 1),
                             0, 0, 0, 0, 0, -1))
    dates = [time.strftime("%Y-%m-%d", time.localtime(
        time.mktime((today.tm_year, today.tm_mon, today.tm_mday - (days - 1) + i,
                     12, 0, 0, 0, 0, -1)))) for i in range(days)]
    # v changes whenever the mapping does, so an old cache is never served.
    key = {"v": 3, "days": days, "dates": dates, "inputs": _usage_inputs()}
    ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    cached = _read_row(USAGE_CACHE)
    if cached and cached.get("key") == key:
        return cached["value"]

    def day_of(ts):
        return time.strftime("%Y-%m-%d", time.localtime(ts))
    acc = {d: {"samples": 0, "mem_n": 0, "mem_sum": 0, "mem_peak": None,
               "mem_estimated": False, "sec_n": 0,
               "sec": dict.fromkeys(USAGE_SECTIONS, 0), "warned": 0, "stopped": 0,
               "held": set()} for d in dates}
    first = last = None
    for ts, row in _day_rows(HISTORY, start_day):
        # A malformed row is skipped whole, before anything of it is counted.
        try:
            a = acc.get(day_of(ts))
            sec = _row_sections(row)
        except BAD_ROW:
            continue
        if a is None:
            continue
        first = ts if first is None else min(first, ts)
        last = ts if last is None else max(last, ts)
        a["samples"] += 1
        # The strict "used" where the sampler recorded it (S2 rows). A v1 row
        # has only top's ram_used, which counts file cache and sits near RAM,
        # so it is never used: its kernel free_pct gives an estimate instead.
        mem = row.get("used_bytes")
        if not (isinstance(mem, (int, float)) and mem > 0):
            free = row.get("free_pct")
            mem = (int(ram * (1 - free / 100.0))
                   if isinstance(free, (int, float)) and 0 <= free <= 100 else None)
            if mem is not None:
                a["mem_estimated"] = True
        if isinstance(mem, (int, float)) and mem > 0:
            a["mem_n"] += 1
            a["mem_sum"] += mem
            a["mem_peak"] = mem if a["mem_peak"] is None else max(a["mem_peak"], mem)
        if sec is not None:
            a["sec_n"] += 1
            for k, v in sec.items():
                a["sec"][k] += v
    for ts, row in _day_rows(GATE_LOG, start_day):
        try:
            a = acc.get(day_of(ts))
        except BAD_ROW:
            continue
        if a is not None and row.get("action") == "warn":
            a["warned"] += 1
        elif a is not None and row.get("action") == "block":
            a["stopped"] += 1
    # Holds are recorded per run in the admission log, which is trimmed: a
    # day before its first row is not recorded. Policy cancels are not logged.
    log_first = None
    for ts, row in _day_rows(ADMISSION_LOG, start_day):
        try:
            a = acc.get(day_of(ts))
            run = row.get("run_id")
            if a is not None and row.get("decision") == "hold" and run:
                if not isinstance(run, str):
                    raise TypeError("run_id is not a string")
                a["held"].add(run)
        except BAD_ROW:
            continue
        log_first = ts if log_first is None else min(log_first, ts)
    log_from = day_of(log_first) if log_first is not None else None
    series = []
    for d in dates:
        a = acc[d]
        series.append({
            "date": d, "samples": a["samples"],
            "mem_peak_bytes": a["mem_peak"],
            "mem_avg_bytes": a["mem_sum"] // a["mem_n"] if a["mem_n"] else None,
            "mem_basis": (None if not a["mem_n"] else
                          "estimated" if a["mem_estimated"] else "measured"),
            "by_section": ({k: (None if k == "codex" else v // a["sec_n"])
                            for k, v in a["sec"].items()} if a["sec_n"] else None),
            "gate": {"warned": a["warned"], "stopped": a["stopped"]},
            "runner": {"held": len(a["held"]) if log_from is not None and d >= log_from
                       else None, "cancelled": None}})
    value = {"schema_version": 1, "days": days,
             "ram_bytes": ram,
             "series": series,
             "coverage": {"from_ts": first, "to_ts": last,
                          "complete": first is not None and first < start_day + 3600
                          and all(e["samples"] for e in series)}}
    try:
        import memmon_owners
        memmon_owners.write_json_atomic(USAGE_CACHE, {"key": key, "value": value})
    except Exception:
        pass
    return value


def usage_cli(argv: list) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="memmon usage")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    out = usage(max(1, min(args.days, 31)))
    if args.json:
        print(json.dumps(out))
        return 0
    for e in out["series"]:
        peak = human(e["mem_peak_bytes"]) if e["mem_peak_bytes"] is not None else "—"
        print(f"{e['date']}  peak {peak:>7}  {e['samples']:>5} samples  "
              f"warned {e['gate']['warned']}  stopped {e['gate']['stopped']}")
    return 0


# ------------------------------------------------------------------ settings

BOOL_SETTINGS = ("auto_cancel_interruptible", "pressure_suggestions", "notifications")
SETTING_KEYS = ("gate_mode", "runner_mode", *BOOL_SETTINGS)
ENV_WARNING = "MEMMON_GATE in the hook environment overrides this setting"


def _runner_settings() -> dict:
    import memmon_runner
    return memmon_runner.get_settings(STATE_DIR)


CLAUDE_SETTINGS = os.path.join(HOME, ".claude", "settings.json")


def _hook_env_gate() -> str | None:
    """MEMMON_GATE from Claude Code's settings.json env block: the hook's
    environment, which a terminal or MemmonBar does not share. Read only."""
    try:
        with open(CLAUDE_SETTINGS) as fh:
            env = (json.load(fh) or {}).get("env") or {}
        value = env.get("MEMMON_GATE")
        return value if isinstance(value, str) else None
    except Exception:
        return None


def _last_gate_row() -> dict | None:
    """The newest gate.jsonl row that records its mode."""
    try:
        with open(GATE_LOG, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 16384))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("mode"):
            return row
    return None


def effective_gate_mode() -> tuple:
    """The gate mode as the hook sees it. This process's own environment is
    not the hook's, so settings also reads the hook env from settings.json and
    the mode the gate last recorded: a gate row whose mode came from its
    environment means MEMMON_GATE is set there. A row from before the gate
    recorded its source counts only when no config value explains it."""
    mode, source = gate_mode()
    if source == "env":
        return mode, source
    hook = _hook_env_gate()
    if hook is not None:
        return hook, "env"
    row = _last_gate_row()
    if row is not None:
        recorded = row.get("mode_source")
        if recorded == "env":
            return row["mode"], "env"
        if recorded is None and source == "default" and row["mode"] != mode:
            return row["mode"], "env"
    return mode, source


def settings_payload() -> dict:
    mode, source = effective_gate_mode()
    paused = pause_until()
    out = {"schema_version": 1,
           "gate_mode": {"value": mode, "source": source, "choices": list(GATE_MODES)},
           "paused_until": None if not paused else "forever" if paused == float("inf")
           else paused,
           **_runner_settings(),
           "pressure_suggestions": suggestions_enabled(),
           "notifications": CONFIG.get("notifications", True) is not False,
           "state_dir": os.path.abspath(STATE_DIR)}
    if source == "env":
        out["warning"] = ENV_WARNING
    return out


def _update_config(key: str, value) -> None:
    """Change one key of config.json atomically and keep every other key.
    Raises ValueError for a config.json that does not parse: rewriting it
    would discard what the user wrote."""
    import fcntl
    import memmon_owners
    path = os.path.join(STATE_DIR, "config.json")
    lock = os.path.join(STATE_DIR, "runner", "coord", "config.lock")
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        # Two sets of different keys must not lose one another's change.
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with open(path) as fh:
                cfg = json.load(fh)
        except FileNotFoundError:
            cfg = {}
        except ValueError:
            raise ValueError("config.json is not valid JSON; fix or remove it first")
        if not isinstance(cfg, dict):
            raise ValueError("config.json is not a JSON object; fix or remove it first")
        cfg[key] = value
        memmon_owners.write_json_atomic(path, cfg)
    finally:
        os.close(fd)
    CONFIG[key] = value


def settings_set(key: str, raw: str) -> dict:
    """Apply one allowlisted setting. Raises KeyError for an unknown key and
    ValueError for a bad value. Never touches ~/.claude/settings.json."""
    import memmon_runner
    if key not in SETTING_KEYS:
        raise KeyError(key)
    if key == "gate_mode":
        if raw not in GATE_MODES:
            raise ValueError(f"gate_mode must be one of {', '.join(GATE_MODES)}")
        _update_config("gate_mode", raw)
    elif key == "runner_mode":
        if raw not in memmon_runner.MODES:
            raise ValueError(f"runner_mode must be one of {', '.join(memmon_runner.MODES)}")
        memmon_runner.write_mode(STATE_DIR, raw)
    else:
        if raw not in ("true", "false"):
            raise ValueError(f"{key} must be true or false")
        value = raw == "true"
        if key == "auto_cancel_interruptible":
            memmon_runner.set_auto_cancel(STATE_DIR, value)
        else:
            _update_config(key, value)
    return settings_payload()


def settings_cli(argv: list) -> int:
    """memmon settings [--json] | memmon settings set KEY VALUE.
    Exit 0 with the settings JSON, or 2 with {"error", "key"}."""
    import memmon_runner
    if argv[:1] == ["set"]:
        if len(argv) != 3:
            print(json.dumps({"error": "usage: memmon settings set KEY VALUE",
                              "key": argv[1] if len(argv) > 1 else None}))
            return 2
        try:
            out = settings_set(argv[1], argv[2])
        except KeyError:
            print(json.dumps({"error": f"unknown setting; one of {', '.join(SETTING_KEYS)}",
                              "key": argv[1]}))
            return 2
        except (ValueError, TypeError) as exc:      # the runner's own refusals too
            print(json.dumps({"error": str(exc), "key": argv[1]}))
            return 2
        except OSError as exc:
            print(json.dumps({"error": f"could not write the setting: {exc}",
                              "key": argv[1]}))
            return 2
        except memmon_runner.LedgerTimeout:
            # runner.json is written under ledger.lock, which admission holds.
            print(json.dumps({"error": "runner busy, try again", "key": argv[1]}))
            return 2
        print(json.dumps(out))
        return 0
    if argv and argv != ["--json"]:
        print(json.dumps({"error": "usage: memmon settings [--json] | "
                                   "memmon settings set KEY VALUE", "key": None}))
        return 2
    out = settings_payload()
    if argv == ["--json"]:
        print(json.dumps(out))
        return 0
    g = out["gate_mode"]
    print(f"gate_mode                  {g['value']} ({g['source']})"
          + (f"  — {out['warning']}" if out.get("warning") else ""))
    p = out["paused_until"]
    print(f"paused_until               {'not paused' if p is None else p}")
    for k in ("runner_mode", *BOOL_SETTINGS):
        v = out[k]
        print(f"{k:<27}{str(v).lower() if isinstance(v, bool) else v}")
    print(f"state_dir                  {out['state_dir']}")
    return 0


# ---------------------------------------------------------------------- main

def main() -> int:
    # Dispatch before scanning flags: a wrapped command may itself use --gate.
    if len(sys.argv) > 1 and sys.argv[1] in ("run", "jobs", "run-mode"):
        from memmon_runner import cli
        return cli(sys.argv[1:], STATE_DIR, lambda: pressure(read_vm(fast=True)))
    if len(sys.argv) > 1 and sys.argv[1] in ("route", "route-classify"):
        import memmon_route
        return memmon_route.cli(sys.argv[1:], STATE_DIR, classify=classify_command,
                                split=shell_commands)
    if len(sys.argv) > 1 and sys.argv[1] == "update":
        import memmon_update
        return memmon_update.cli(sys.argv[2:], STATE_DIR)
    if len(sys.argv) > 1 and sys.argv[1] == "explain":
        import memmon_explain
        return memmon_explain.cli(sys.argv[2:], lambda: owners_json(cpu_window=1.0),
                                  lambda: (lambda vm: (vm, pressure(vm)))(read_vm(fast=True)))
    if len(sys.argv) > 1 and sys.argv[1] in ("owners", "act", "reap", "settings", "usage"):
        return {"owners": owners_cli, "act": act_cli, "reap": reap_cli,
                "settings": settings_cli, "usage": usage_cli}[sys.argv[1]](sys.argv[2:])
    # Short-circuit before the parser exists: gate() runs on every Bash tool call
    # and has no use for 24 argument definitions.
    if "--gate" in sys.argv:
        return gate()
    import argparse
    ap = argparse.ArgumentParser(prog="memmon", add_help=True,
                                 epilog="Also: memmon run --help | memmon jobs --help")
    ap.add_argument("--once", action="store_true", help="one snapshot then exit")
    ap.add_argument("--json", action="store_true", help="machine-readable snapshot")
    ap.add_argument("--statusline", action="store_true", help="one compact line")
    ap.add_argument("--log", action="store_true", help="append a history sample")
    ap.add_argument("--report", action="store_true", help="per-owner averages")
    ap.add_argument("--days", type=int, default=7, help="report window (default 7)")
    ap.add_argument("--reap", action="store_true", help="list reclaimable orphans")
    ap.add_argument("--reap-spares", action="store_true",
                    help="list idle claude prewarm procs older than 4h")
    ap.add_argument("--apply", action="store_true",
                    help="with --reap, SIGTERM them (stops at partial; see `memmon reap`)")
    ap.add_argument("--gate", action="store_true",
                    help="PreToolUse hook: gate heavy commands on memory pressure")
    ap.add_argument("--blocked", action="store_true",
                    help="commands the gate refused that nobody has re-run")
    ap.add_argument("--dismiss-blocked", metavar="ID",
                    help="stop listing one blocked command (its id from --blocked)")
    ap.add_argument("--off", nargs="?", const="forever", metavar="DURATION",
                    help="pause the gate entirely, e.g. --off 8h (default: until --on)")
    ap.add_argument("--on", action="store_true", help="resume the gate")
    ap.add_argument("--clear-gate-log", action="store_true",
                    help="reset the gate counters")
    ap.add_argument("--profile", action="store_true",
                    help="what this machine has learned costs memory")
    ap.add_argument("--gate-log", action="store_true",
                    help="recent gate decisions across all sessions")
    ap.add_argument("--clear-blocked", action="store_true",
                    help="empty the outstanding-blocked list")
    ap.add_argument("--end-session", type=int, metavar="PID",
                    help="terminate a Claude session by its root pid")
    ap.add_argument("--pressure", action="store_true",
                    help="crash-risk verdict only (fast, no top)")
    ap.add_argument("--wait-safe", action="store_true",
                    help="block until memory pressure clears")
    ap.add_argument("--timeout", type=int, default=600,
                    help="with --wait-safe, seconds to wait (default 600)")
    ap.add_argument("--interval", type=float, default=4.0, help="live refresh secs")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    if args.gate:
        return gate()
    if args.end_session:
        text, out = end_session_report(args.end_session, args.apply)
        print(text)
        return _apply_exit(out, args.apply)
    if args.off is not None:
        until = "forever"
        if args.off != "forever":
            m = re.match(r"^(\d+(?:\.\d+)?)([mhd])$", args.off)
            if not m:
                print("duration looks like 30m, 8h or 1d")
                return 2
            mult = {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
            until = time.time() + float(m.group(1)) * mult
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(PAUSE, "w") as fh:
            json.dump({"until": until}, fh)
        _write_learned_glob()
        if until == "forever":
            print("Gate paused indefinitely. Nothing will be checked, warned or "
                  "stopped.\n`memmon --on` to resume.")
        else:
            print(f"Gate paused until "
                  f"{time.strftime('%a %H:%M', time.localtime(until))}. "
                  f"It resumes on its own — no need to remember.")
        return 0
    if args.on:
        try:
            os.remove(PAUSE)
        except FileNotFoundError:
            pass
        _write_learned_glob()
        print("Gate resumed.")
        return 0
    if args.clear_gate_log:
        try:
            os.remove(GATE_LOG)
        except FileNotFoundError:
            pass
        print("Gate counters reset.")
        return 0
    if args.clear_blocked:
        save_pending([])
        print("outstanding-blocked list cleared")
        return 0
    if args.dismiss_blocked:
        if dismiss_pending(args.dismiss_blocked):
            print("Dismissed.")
            return 0
        print("No outstanding blocked command has that id.", file=sys.stderr)
        return 1
    if args.blocked:
        pend = load_pending()
        if not pend:
            print("Nothing outstanding in the last "
                  f"{PENDING_TTL_S // 3600} hours — no command is waiting to be re-run.")
            return 0
        cur = pressure(read_vm(fast=True))
        lvl = cur["level"]
        print(f"{len(pend)} command(s) blocked and not yet re-run:\n")
        for b in pend:
            print(f"  {time.strftime('%H:%M', time.localtime(b['ts']))}  "
                  f"{b.get('session', '?')}  ·  {short_command(b.get('cmd', ''))}")
            print(f"        {b.get('cmd', '')[:100]}")
            print(f"        dismiss: memmon --dismiss-blocked {pending_id(b)}")
            if b.get("cwd"):
                print(f"        in {b['cwd']}")
        print()
        print(f"current pressure: {lvl}  — "
              + ("safe to re-run these now" if lvl in ("HEALTHY", "WATCH") else
                 f"not known yet ({cur.get('level_reason')}); check again in a "
                 "minute" if lvl == "UNKNOWN" else "still under pressure, wait"))
        return 0
    if args.profile:
        prof = load_profile()
        if not prof:
            print("Nothing learned yet — a command's cost is recorded once it "
                  "has been running for a minute.\n"
                  "Until then the built-in list is used.")
            return 0
        rows = sorted(prof.items(), key=lambda kv: -kv[1].get("peak", 0))
        heavy = [k for k, v in rows if v.get("n", 0) >= LEARN_MIN_SAMPLES
                 and v.get("peak", 0) >= LEARN_HEAVY_AT]
        print(f"{len(rows)} command shape(s) observed; {len(heavy)} learned heavy "
              f"(peak >= {human(LEARN_HEAVY_AT)}, seen >= {LEARN_MIN_SAMPLES}x)")
        print()
        print(f"  {'COMMAND SHAPE':<34}{'PEAK':>8}{'SEEN':>6}  VERDICT")
        for k, v in rows[:25]:
            is_h = (v.get("n", 0) >= LEARN_MIN_SAMPLES
                    and v.get("peak", 0) >= LEARN_HEAVY_AT)
            note = "heavy" if is_h else (
                "light" if v.get("n", 0) >= LEARN_MIN_SAMPLES else "need more data")
            if is_h and not HEAVY_CMD.search(k):
                note += "  <- learned, not in the built-in list"
            print(f"  {clip(k, 32):<34}{human(v.get('peak', 0)):>8}"
                  f"{v.get('n', 0):>6}  {note}")
        return 0
    if args.gate_log:
        # Formats what gate_stats() already computed. These used to be two
        # independent implementations of the same tally and percentiles, and had
        # already drifted: one excluded error rows from the latency sample and
        # the other did not, so the menu bar and the CLI reported different p50s
        # for the same file — diverging exactly when the gate was failing.
        g = gate_stats()
        if not g["total"]:
            print("No gate activity recorded yet — nothing has been checked.")
            return 0
        paused = pause_until()
        if paused:
            when = ("indefinitely" if paused == float("inf") else
                    "until " + time.strftime('%a %H:%M', time.localtime(paused)))
            print(f"GATE PAUSED {when} — nothing is being checked. "
                  f"`memmon --on` to resume.\n")
        print(f"{g['total']} heavy command(s) checked over {dur(g['span_s'])} — "
              f"{g['allow']} ran silently, {g['warn']} warned but still ran, "
              f"{g['block']} STOPPED"
              + (f", {g['error']} ERRORED" if g["error"] else ""))
        print("  gate healthy — every invocation completed and was recorded"
              if g["healthy"] else
              f"  ⚠ the gate failed {g['error']} time(s) and fell open")
        print(f"gate latency on those: median {g['p50_ms']}ms, p95 {g['p95_ms']}ms")
        print("light commands never reach python (shell fast-path, ~4ms) "
              "and are not logged")
        print()
        rows = _read_gate_rows()
        blocks = [r for r in rows if r.get("action") == "block"]
        if blocks:
            print(f"STOPPED ({len(blocks)}) — these did not run:")
            for r in blocks:
                print(f"  {time.strftime('%b %d %H:%M', time.localtime(r['ts']))}  "
                      f"{clip(r.get('session_name') or r.get('session', '?'), 24)}")
                print(f"      {r.get('cmd', '')[:90]}")
        else:
            print("No command was stopped in the retained gate history.")
        print()
        print("recent decisions:")
        print(f"  {'when':<9}{'session':<24}{'level':<9}{'action':<7}{'ms':>4}  cmd")
        for r in rows[-12:]:
            print(f"  {time.strftime('%H:%M:%S', time.localtime(r['ts'])):<9}"
                  f"{clip(r.get('session_name') or r.get('session', '?'), 22):<24}"
                  f"{r.get('level', '?'):<9}{r.get('action', '?'):<7}"
                  f"{r.get('ms', 0):>4}  {r.get('cmd', '')[:38]}")
        return 0
    if args.wait_safe:
        return wait_safe(args.timeout)
    if args.pressure:
        p = pressure(read_vm(fast=True))
        room = p.get("headroom_min")
        print(f"{p['level']}  score={p['score']}  "
              f"{' · '.join(p['reasons']) or 'no pressure signals'}"
              + (f"  ({p['level_reason']})" if p.get("level_reason") else "")
              + (f"  ~{room:.0f} min headroom" if room is not None and room < 120
                 else ""))
        # UNKNOWN exits 0, as the gate allows on it: fail-open.
        return 0 if p["level"] in ("HEALTHY", "WATCH", "UNKNOWN") else 1
    if args.report:
        print(report(args.days))
        return 0

    color = sys.stdout.isatty() and not args.no_color

    if args.statusline:
        # Prefer the cached sample: the statusline must never block on `top`.
        try:
            with open(SNAPSHOT) as fh:
                r = json.load(fh)
            print(cached_statusline(r, time.time()))
            return 0
        except Exception:
            pass
        print(statusline(collect()))
        return 0

    if args.log:
        return sampler_run()

    snap = collect()

    if args.json:
        print(json.dumps({**snap, "schema_version": 2}, indent=1))
        return 0
    if args.reap or args.reap_spares:
        text, out = (reap_report if args.reap else reap_spares_report)(snap, args.apply)
        print(text)
        return _apply_exit(out, args.apply)
    if args.once:
        print(render(snap, color))
        return 0

    # live
    alt = sys.stdout.isatty()
    last_log = 0.0
    try:
        if alt:
            # Alternate screen, as top/htop use: the dashboard never enters
            # scrollback, and your shell history is intact on exit.
            sys.stdout.write("\033[?1049h")
        sys.stdout.write("\033[?25l")  # hide cursor
        while True:
            snap = collect()
            try:
                # The launchd sampler owns the 1/min cadence; the dashboard must
                # not also write every 4s or history grows 15x faster than the
                # retention maths assumes.
                if time.time() - last_log >= 60:
                    log_sample(snap)
                    last_log = time.time()
            except Exception:
                pass
            # A terminal that reports 0 rows (a pty with no winsize set) would
            # otherwise collapse the frame to a single line.
            import shutil
            reported = shutil.get_terminal_size((110, 40)).lines
            rows = reported if reported >= 10 else 40
            frame = render(snap, color, max_lines=rows - 2).split("\n")
            frame.append(col(f"  ctrl-c to quit · refresh {args.interval:.0f}s",
                             "grey", color))
            # Repaint in place: home the cursor, erase each line as it is
            # rewritten, then clear whatever is left below. Nothing scrolls, so
            # the display updates rather than a new page being appended.
            buf = ["\033[H"]
            for line in frame[: max(1, rows - 1)]:
                buf.append(line + "\033[K\n")
            buf.append("\033[J")
            sys.stdout.write("".join(buf))
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        sys.stdout.write("\033[?25h")  # restore cursor
        if alt:
            sys.stdout.write("\033[?1049l")
        sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
