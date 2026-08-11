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
  memmon --reap          list reclaimable orphans (add --apply to kill)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
# argparse, shutil and signal are imported lazily where used. None is reachable
# from gate(), which runs on every Bash tool call; importing them unconditionally
# measured ~6ms of its ~85ms budget, and shutil alone pulls in bz2 and lzma.
from collections import defaultdict

HOME = os.path.expanduser("~")
JOBS_DIR = os.path.join(HOME, ".claude", "jobs")
STATE_DIR = os.path.join(HOME, ".claude", "memmon")
HISTORY = os.path.join(STATE_DIR, "history.jsonl")
SNAPSHOT = os.path.join(STATE_DIR, "latest.json")
# The one state file written from three functions was the only one
# without a constant.
GATE_LOG = os.path.join(STATE_DIR, "gate.jsonl")

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


def read_vm(fast: bool = False, header: str = "") -> dict:
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
    swap = _sh(["sysctl", "-n", "vm.swapusage", "kern.memorystatus_level"])
    m = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M\s+free = ([\d.]+)M", swap)
    if m:
        vm["swap_total"] = int(float(m.group(1)) * MB)
        vm["swap_used"] = int(float(m.group(2)) * MB)
    lvl = re.search(r"^\s*(\d+)\s*$", swap, re.M)
    vm["free_pct"] = int(lvl.group(1)) if lvl else 0
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
        m = re.search(r"Load Avg:\s+([\d.]+)", hdr)
        vm["load"] = float(m.group(1)) if m else 0.0
        m = re.search(r"Processes:\s+(\d+)", hdr)
        vm["nprocs"] = int(m.group(1)) if m else 0

    # Cumulative counters. Their *rate* is what predicts a freeze — a high
    # swapin rate means the working set no longer fits in RAM and the machine is
    # reading pages back as fast as it evicts them.
    for line in _sh(["vm_stat"]).splitlines():
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


def _page_size() -> int:
    m = re.search(r"page size of (\d+) bytes", _sh(["vm_stat"]))
    return int(m.group(1)) if m else os.sysconf("SC_PAGE_SIZE")


# The free-% level the runway estimate projects toward. See pressure().
HEADROOM_FLOOR = 20

PAGE = _page_size()
_prev_vm: dict = {}


def pressure(vm: dict) -> dict:
    """How close is this machine to the freeze, and how fast is it getting there.

    Levels are NOT read off swap usage. macOS grows swap on demand, so a full
    swapfile means nothing; the freeze comes from jetsam deciding pageout cannot
    keep up with allocation. The signals that actually precede it are the swapin
    rate (working set no longer fits, pages being read back as fast as they are
    evicted) and a collapsing free percentage."""
    global _prev_vm
    now = time.time()
    if not _prev_vm:
        # A one-shot run has no in-process history, so fall back to the sample
        # the launchd sampler wrote. Live mode overwrites this each refresh.
        try:
            with open(SNAPSHOT) as fh:
                cached = json.load(fh)
            if 5 <= now - cached.get("ts", 0) <= 300 and "swapins" in cached:
                _prev_vm = {**cached, "_ts": cached["ts"],
                            "_lh_streak": cached.get("_lh_streak", 0)}
        except Exception:
            pass
    prev = _prev_vm

    rates = {"swapin_mbs": 0.0, "swapout_mbs": 0.0,
             "free_delta_min": 0.0, "swap_growth_mbmin": 0.0}
    dt = now - prev.get("_ts", 0) if prev else 0
    if prev and 2 <= dt <= 300:
        rates["swapin_mbs"] = max(0, vm.get("swapins", 0)
                                  - prev.get("swapins", 0)) * PAGE / dt / 1e6
        rates["swapout_mbs"] = max(0, vm.get("swapouts", 0)
                                   - prev.get("swapouts", 0)) * PAGE / dt / 1e6
        rates["free_delta_min"] = (vm.get("free_pct", 0)
                                   - prev.get("free_pct", 0)) * 60.0 / dt
        rates["swap_growth_mbmin"] = (vm.get("swap_used", 0)
                                      - prev.get("swap_used", 0)) / MB * 60.0 / dt
    _prev_vm = {**vm, "_ts": now}

    free = vm.get("free_pct", 100)
    thrash = rates["swapin_mbs"] + rates["swapout_mbs"]
    ram_total = max(vm.get("ram_total", 1), 1)
    swap_ratio = vm.get("swap_used", 0) / ram_total
    reasons, score = [], 0

    # Swap held against RAM SIZE is the strongest discriminator available.
    # Replaying this morning's near-freeze: swap sat at 1.1-1.4x RAM throughout,
    # versus 0.15x when idle. Swap as a share of swap_total is useless here
    # because macOS grows the swapfile to match demand.
    if swap_ratio >= 1.0:
        score += 4
        reasons.append(f"swap {swap_ratio:.1f}x RAM size")
    elif swap_ratio >= 0.5:
        score += 2
        reasons.append(f"swap {swap_ratio:.1f}x RAM size")
    elif swap_ratio >= 0.25:
        score += 1
        reasons.append(f"swap {int(swap_ratio * 100)}% of RAM size")

    # Sustained two-way paging is the thrash signature that precedes a stall.
    if thrash >= 150:
        score += 4; reasons.append(f"heavy thrashing {thrash:.0f} MB/s")
    elif thrash >= 50:
        score += 2; reasons.append(f"paging {thrash:.0f} MB/s")
    elif thrash >= 10:
        score += 1; reasons.append(f"paging {thrash:.0f} MB/s")

    # free_pct is a weak signal on this machine — it sits near 28% both when
    # healthy and mid-crisis — so only genuinely extreme values count.
    if free <= 12:
        score += 3; reasons.append(f"kernel headroom down to {free}%")
    elif free <= 20:
        score += 2; reasons.append(f"kernel headroom {free}%")

    if rates["swap_growth_mbmin"] >= 500:
        score += 2; reasons.append(
            f"swap growing {rates['swap_growth_mbmin']:.0f} MB/min")
    elif rates["swap_growth_mbmin"] >= 150:
        score += 1; reasons.append(
            f"swap growing {rates['swap_growth_mbmin']:.0f} MB/min")

    load_ratio = vm.get("load", 0) / max(vm.get("ncpu", 8), 1)
    if load_ratio >= 3:
        score += 2; reasons.append(f"load {vm.get('load', 0):.0f} on "
                                   f"{vm.get('ncpu', 8)} cores")
    elif load_ratio >= 1.75:
        score += 1; reasons.append(f"load {vm.get('load', 0):.0f}")

    # Rough runway: free% is falling this fast, so this long until it reaches
    # the level where the machine is genuinely in trouble.
    #
    # The target was 10%, which this machine has never reached — the minimum
    # across 2,536 samples is 18%, and only 2 samples went below 20%. Projecting
    # toward a level that never occurs makes the estimate optimistic: it always
    # reported more runway than the machine actually had. 20% is the region
    # stress actually reaches here.
    headroom = None
    drop = -rates["free_delta_min"]
    if drop > 0.5 and free > HEADROOM_FLOOR:
        headroom = (free - HEADROOM_FLOOR) / drop

    if score >= 7:
        level, color = "CRITICAL", "red"
    elif score >= 4:
        level, color = "DANGER", "red"
    elif score >= 2:
        level, color = "WATCH", "yellow"
    else:
        level, color = "HEALTHY", "green"
    # Escalating on headroom needs care: it is a straight-line projection from
    # the rate of change of free_pct — the weakest signal here, demoted in the
    # scoring above because it reads ~28% both mid-crisis and idle. One noisy
    # derivative should not be able to move the verdict a whole tier on its own,
    # and when it does, the card has to say so: a DANGER whose listed reasons
    # only add up to WATCH is worse than no warning.
    low = headroom is not None and headroom < 5
    streak = (prev.get("_lh_streak", 0) + 1) if low else 0
    _prev_vm["_lh_streak"] = streak
    if low and streak >= 2 and level == "WATCH":
        level, color = "DANGER", "red"
        reasons.append(f"headroom falling, ~{headroom:.0f} min to "
                       f"{HEADROOM_FLOOR}%")

    # Distance to the next tier. A bare score is meaningless to read; "2 more
    # points and this becomes DANGER" is not.
    tiers = [("WATCH", 2), ("DANGER", 4), ("CRITICAL", 7)]
    nxt, to_next = None, None
    for name, need in tiers:
        if score < need:
            nxt, to_next = name, need - score
            break

    if level == "HEALTHY":
        headroom = None          # see README: "appears only when something is wrong"
    return {"level": level, "color": color, "score": score,
            "reasons": reasons, "headroom_min": headroom,
            "next_level": nxt, "to_next": to_next, "lh_streak": streak,
            "thrash_mbs": thrash, **rates}


# The VM is Docker; folding it here rather than in one of two front ends means
# the terminal and the popover agree without either knowing the special case.
SERVICE_ALIAS = {"Docker VM": "Docker"}


PROFILE = os.path.join(STATE_DIR, "profile.json")
SHELL_STATE = os.path.join(STATE_DIR, "learned.zsh")
PAUSE = os.path.join(STATE_DIR, "paused.json")
# A command has to cost this much before it is worth gating.
LEARN_HEAVY_AT = 1500 * MB
# Peaks below this are noise from whatever else the session was doing.
LEARN_MIN_SAMPLES = 2

_CMD_NOISE = re.compile(r"""^(sudo|nohup|time|env|exec|bash|sh|zsh|/bin/\w+)$""")
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
            "grep", "rg", "sed", "awk", "find", "which", "date", "sleep",
            "kill", "pgrep", "pkill", "open", "diff", "basename", "dirname"}
_VERBS = {"build", "test", "typecheck", "install", "dev", "lint", "check",
          "compile", "start", "bundle", "package", "e2e"}
# Transparent: `turbo run typecheck` is a typecheck, not a "run". Used only when
# nothing more specific follows, so `codex.sh run` still keeps its subcommand.
_PASSTHROUGH = {"run", "watch", "exec"}


def normalise_cmd(cmd: str) -> list[str]:
    """Reduce a command line to comparable shapes.

    Raw command text is unique every time — paths, flags, quoted prompts — so it
    can never accumulate evidence. `bash ~/.claude/skills/codex/scripts/codex.sh
    run "implement ABC-1234…"` has to become `codex.sh run` before a second
    occurrence counts as the same thing.

    Returns one shape per chained segment, because `cd x && pnpm build` is two
    commands and only the second one matters."""
    shapes = []
    for segment in re.split(r"&&|\|\||;|\|", cmd or ""):
        toks = segment.strip().split()
        # Drop wrappers and leading VAR=value assignments.
        while toks and (_CMD_NOISE.match(os.path.basename(toks[0]))
                        or re.match(r"^[A-Za-z_][\w]*=", toks[0])):
            toks.pop(0)
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
        if sub and len(sub) < 32 and "/" not in sub:
            shapes.append(f"{exe} {sub}")
        else:
            shapes.append(exe)
    return shapes[:4]


def load_profile() -> dict:
    try:
        with open(PROFILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


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
        doing = s.get("doing") or ""
        m = re.match(r"\[shell\]\s*(.+)", doing)
        if not m:
            continue
        for shape in normalise_cmd(m.group(1)):
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
        tmp = PROFILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(prof, fh)
        os.replace(tmp, PROFILE)
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
    # Only tokens that can safely become a glob, deduped. A token like "." or
    # "until" would publish `*.*` and send every command to Python.
    toks = []
    for k in heavy:
        t = k.split()[0]
        if re.fullmatch(r"[\w.@+-]{3,}", t) and not t.startswith(".") and t not in toks:
            toks.append(t)
    pats = "|".join(f"*{t}*" for t in sorted(toks))
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
        doing = d.get("detail") or d.get("displayIntent") or d.get("intent") or ""
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
CLAIM_SOCK_RE = re.compile(r"(\S+\.claim\.sock)")


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


CONDUCTOR_CLAUDE_RE = re.compile(r"com\.conductor\.app/agent-binaries/claude/")


def pid_cwds(pids: list[int]) -> dict[int, str]:
    """pid -> working directory, via lsof. macOS ps truncates another
    process's argv, so a Conductor session's --session-id is never visible;
    its cwd is the only thing that names the worktree."""
    if not pids:
        return {}
    out = _sh(["lsof", "-a", "-p", ",".join(map(str, pids)), "-d", "cwd", "-Fpn"],
              timeout=20)
    cwds: dict[int, str] = {}
    pid = None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("n") and pid:
            cwds[pid] = line[1:]
    return cwds


def spare_is_idle(cmd: str) -> bool:
    """A prewarm process advertises itself on a .claim.sock. Once a session
    claims it the socket is removed, so a missing socket means this process is
    doing real work and must never be treated as reclaimable."""
    m = CLAIM_SOCK_RE.search(cmd)
    if not m:
        return False
    return os.path.exists(m.group(1))


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
    if "com.conductor.app" in cmd or "Conductor.app" in cmd:
        if "agent-binaries/claude" in cmd:
            return "claude"
        if "conductor-runtime" in cmd:
            return "conductor sidecar"
        return "Conductor"
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
    ):
        if needle in cmd:
            return name
    return ""


def collect() -> dict:
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

    # Conductor-hosted sessions never write a job state file and their argv is
    # truncated by ps, so neither path above finds them. The binary path prefix
    # is visible, and lsof gives the cwd, which names the worktree. Transcripts
    # are matched through the projects dir that mirrors that cwd.
    seen_roots = set(sid_to_pid.values()) | set(short_to_pid.values())
    conductor_roots = [pid for pid, info in ps.items()
                       if CONDUCTOR_CLAUDE_RE.search(info["cmd"])
                       and pid not in seen_roots]
    cond_cwds = pid_cwds(conductor_roots)
    by_proj: dict[str, list[str]] = defaultdict(list)
    for tp_path in transcripts.values():
        by_proj[os.path.basename(os.path.dirname(tp_path))].append(tp_path)
    for paths in by_proj.values():
        paths.sort(key=os.path.getmtime, reverse=True)
    taken: set[str] = set()
    for root in sorted(conductor_roots, key=lambda p: ps[p]["age"]):
        cwd = cond_cwds.get(root, "")
        proj = cwd.replace("/", "-").replace(".", "-")
        tp = next((p for p in by_proj.get(proj, []) if p not in taken), None)
        sid = os.path.basename(tp)[:-6] if tp else ""
        if tp:
            taken.add(tp)
        if sid and sid in by_sid:
            by_sid[sid].setdefault("root_pid", root)
            continue
        meta = read_transcript(tp) if tp else {}
        fallback = f"pid{root}"
        by_sid[sid or fallback] = {
            "short": sid[:8] if sid else fallback,
            "state": "conductor", "origin": "conductor",
            "name": os.path.basename(cwd.rstrip("/")) or fallback,
            "doing": meta.get("last_prompt", ""), "cwd": cwd,
            "session_id": sid, "updated": meta.get("mtime", 0),
            "intent": meta.get("last_prompt", ""),
            "root_pid": root,
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
        root = (s.get("root_pid") or short_to_pid.get(s.get("short"))
                or sid_to_pid.get(s["session_id"]))
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
    STALE_SPARE = 4 * 3600
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
        "pressure": pressure(vm),
        "blocked": load_pending(),
        "gate": gate_stats(),
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
        head = f" ▌ {p['level']:<9}"
        L.append(col(head, p["color"], on)
                 + col(detail, "grey" if p["level"] == "HEALTHY" else p["color"], on)
                 + col(nxt, "grey", on))
        room = p.get("headroom_min")
        room_s = (f" · ~{room:.0f} min before memory runs out"
                  if room is not None and room < 120 else "")
        L.append(col(f" {'':<10}{p.get('advice', '')}{room_s}", "grey", on))
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
        L.append(col("  → memmon --reap           preview the kill list", "grey", on))
        L.append(col("  → memmon --reap --apply   free it now", "grey", on))
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


def statusline(snap: dict) -> str:
    vm = snap["vm"]
    level = (snap.get("pressure") or {}).get("level", "HEALTHY")
    s = f"{LEVEL_ICON.get(level, '')} {human(vm.get('swap_used', 0))} swap"
    if level != "HEALTHY":
        s += f" · {level}"
    if snap["orphan_total"] > GB:
        s += f" · {human(snap['orphan_total'])} reclaimable"
    return s


# ------------------------------------------------------------- history/report

def log_sample(snap: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    vm = snap["vm"]
    row = {
        "ts": int(snap["ts"]),
        "ram_used": vm.get("ram_used", 0), "swap_used": vm.get("swap_used", 0),
        "swap_total": vm.get("swap_total", 0), "free_pct": vm.get("free_pct", 0),
        "load": vm.get("load", 0), "orphan": snap["orphan_total"],
        # Counters, so the next run can compute paging rates against this point.
        "swapins": vm.get("swapins", 0), "swapouts": vm.get("swapouts", 0),
        "pressure": (snap.get("pressure") or {}).get("level", "?"),
        # Carries the low-headroom streak across process boundaries: every CLI
        # invocation is a fresh process, so without this the streak could never
        # reach 2 and the escalation would never fire outside the live loop.
        "_lh_streak": (snap.get("pressure") or {}).get("lh_streak", 0),
        "sessions": {s["name"]: s["mem"] for s in snap["sessions"]},
        "apps": {k: v["mem"] for k, v in snap["apps"].items()},
        "worktrees": {f"build:{r['name']}": r["mem"] for r in snap.get("worktrees", [])},
        "worktree_tags": {r["name"]: r.get("tag", "") for r in snap.get("worktrees", [])},
        "overhead": (snap.get("overhead") or {}).get("mem", 0),
    }
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
        subprocess.run([
            "osascript", "-e",
            f'display notification "{len(pend)} blocked command(s) can be '
            f'retried" with title "memmon" subtitle "Memory pressure cleared"',
        ], capture_output=True)

    try:
        learn(snap)
    except Exception:
        pass

    with open(HISTORY, "a") as fh:
        fh.write(json.dumps(row) + "\n")
    with open(SNAPSHOT, "w") as fh:
        json.dump(row, fh)
    _trim_history()


# Retention. Rows are ~690 bytes, so 1 sample/min is ~1 MB/day.
# Trimming by COUNT rather than by age is deliberate: an age cutoff that is
# further out than the size gate removes nothing once the gate is reached, so
# the sampler rewrites the whole file every single minute and it never shrinks.
# Keeping the last N rows always reduces the file, so a trim happens rarely.
HISTORY_TRIM_AT = 12 * MB          # ~12 days
HISTORY_KEEP_ROWS = 10_080         # 7 days at 1/min
ERRLOG_TRIM_AT = 1 * MB


def _trim_history() -> None:
    try:
        if os.path.getsize(HISTORY) < HISTORY_TRIM_AT:
            return
        with open(HISTORY) as fh:
            rows = fh.readlines()
        with open(HISTORY, "w") as fh:
            fh.writelines(rows[-HISTORY_KEEP_ROWS:])
    except Exception:
        pass
    # launchd appends the sampler's stderr forever; nothing else bounds it.
    try:
        err = os.path.join(STATE_DIR, "sampler.err")
        if os.path.getsize(err) > ERRLOG_TRIM_AT:
            with open(err) as fh:
                tail = fh.readlines()[-200:]
            with open(err, "w") as fh:
                fh.writelines(tail)
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
    swaps = [r["swap_used"] for r in rows]
    peak = max(rows, key=lambda r: r["swap_used"])
    L.append(f"  swap   avg {human(sum(swaps) // len(swaps))}   "
             f"peak {human(peak['swap_used'])} at "
             f"{time.strftime('%b %d %H:%M', time.localtime(peak['ts']))}")
    # Report the recorded verdict, not a free_pct threshold that has never been
    # crossed on this machine (min observed 18 across 2,487 rows).
    bad = sum(1 for r in rows if r.get("pressure") in ("DANGER", "CRITICAL"))
    known = sum(1 for r in rows if r.get("pressure"))
    if known:
        L.append(f"  time at DANGER or worse: {bad * 100 // known}% "
                 f"of {known} scored samples")
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

def reap_spares(snap: dict, apply: bool) -> str:
    """Idle prewarm processes older than 4h. The daemon keeps a warm pool and is
    meant to recycle it; when it doesn't, these just hold memory. Killing one is
    safe — the pool respawns on demand — so only the stale ones are targeted."""
    import signal
    ov = snap.get("overhead") or {}
    items = ov.get("items", [])
    stale = [i for i in items if i["stale"]]
    if not stale:
        oldest = max((i["age"] for i in items), default=0)
        return (f"No stale prewarms. Idle pool: {ov.get('spares', 0)} spares, "
                f"{human(ov.get('spare_mem', 0))}, oldest {dur(oldest)}.\n"
                f"{ov.get('claimed', 0)} claimed session(s) holding "
                f"{human(ov.get('claimed_mem', 0))} are working and excluded.")
    L = [f"{'PID':>7}  {'MEM':>7} {'IDLE':>7}"]
    for i in sorted(stale, key=lambda x: -x["mem"]):
        L.append(f"{i['pid']:>7}  {human(i['mem']):>7} {dur(i['age']):>7}")
    total = sum(i["mem"] for i in stale)
    L.append("")
    L.append(f"{len(stale)} idle prewarm procs · {human(total)} reclaimable")
    if not apply:
        L.append("dry run — re-run with --apply to kill these.")
        return "\n".join(L)
    killed = 0
    for i in stale:
        try:
            os.kill(i["pid"], signal.SIGTERM)
            killed += 1
        except Exception:
            pass
    L.append(f"killed {killed} prewarm process(es) — ~{human(total)} freed")
    return "\n".join(L)


def reap(snap: dict, apply: bool) -> str:
    import signal  # reap() has no docstring, which is how my patch missed it
    targets = snap["orphans"]
    if not targets:
        return "Nothing to reap — no orphaned or stale build processes."
    L = [f"{'PID':>7}  {'MEM':>7} {'AGE':>7}  WHAT"]
    for o in targets:
        L.append(f"{o['pid']:>7}  {human(o['mem']):>7} {dur(o['age']):>7}  "
                 f"{o['tag']} {o['worktree']}"
                 + ("  [orphan]" if o["orphaned"] else "  [stale]"))
    L.append("")
    L.append(f"total reclaimable: {human(snap['orphan_total'])}")
    if not apply:
        L.append("dry run — re-run with --apply to kill these.")
        return "\n".join(L)

    killed, failed = 0, 0
    for o in targets:
        try:
            os.kill(o["pid"], signal.SIGTERM)
            killed += 1
        except ProcessLookupError:
            pass
        except Exception:
            failed += 1
    time.sleep(2)
    for o in targets:
        try:
            os.kill(o["pid"], 0)
            os.kill(o["pid"], signal.SIGKILL)
        except Exception:
            pass
    L.append(f"killed {killed} process(es)"
             + (f", {failed} failed" if failed else "")
             + f" — ~{human(snap['orphan_total'])} freed")
    return "\n".join(L)


# -------------------------------------------------------------------- the gate

# Commands worth gating: each can add gigabytes. Everything else runs freely —
# blocking a `git status` because a build is running would be useless friction.
# Flags may sit between the launcher and the verb — `pnpm --filter web build`,
# `pnpm -r build`, `turbo watch dev`, `pnpm --dir=packages/call build:all`. The
# earlier form bound the verb adjacent to the launcher and so missed every one of
# those, including `pnpm --filter <pkg> typecheck`, which is the exact command
# the gate's own block message tells you to run instead.
# Up to four intervening tokens, non-greedy, so a flag AND its separate value
# are skipped: `pnpm --filter dashboard typecheck` has two tokens before the verb.
_LAUNCHER = r"(?:pnpm|npm|yarn|bun|turbo)(?:\s+[-\w:=@/.]+){0,4}?"
_VERB = r"(?:typecheck|build|test|install|dev|lint)[\w:-]*"
HEAVY_CMD = re.compile(
    rf"{_LAUNCHER}\s+(?:run\s+|watch\s+)?{_VERB}\b"
    r"|\btsc\b|\bvitest\b|\bjest\b|\bplaywright\b|\bgradlew?\b|\bbazel\b|\bxcodebuild\b"
    r"|docker\s+(?:compose\s+)?(?:up|build|run)\b|\bcolima\s+start\b"
    r"|next\s+build\b|expo\s+(?:start|run)\b|cargo\s+(?:build|test)\b|\bwebpack\b"
    r"|\bmake\b|\bpytest\b"
)


def is_heavy(cmd: str) -> bool:
    """What this machine has observed beats what I guessed.

    The regex only ever encoded JavaScript-monorepo tooling; it cannot know that
    `codex.sh run` on this machine spawns an 8 GB build, and it would be wrong
    about a Rust or Python workflow entirely."""
    return profile_says_heavy(cmd) or bool(HEAVY_CMD.search(cmd or ""))


def gate_decision(tool: str, cmd: str, pres: dict, cached: dict,
                  mode: str) -> tuple[str, str]:
    """Pure decision, so it can be tested without provoking real memory pressure.

    Returns (action, message) where action is allow | warn | block."""
    if mode == "off" or tool != "Bash" or not is_heavy(cmd):
        return "allow", ""

    level = pres.get("level", "HEALTHY")
    if level == "HEALTHY":
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
    room = pres.get("headroom_min")
    if room is not None and room < 30:
        bits.append(f"At the current rate memory runs out in ~{room:.0f} min.")

    # "warn" never blocks, whatever the level — that is the point of the mode.
    blocking = (mode in ("block", "block-critical") and level == "CRITICAL") \
        or (mode == "block" and level == "DANGER")
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


def session_name_for(session_id: str) -> str:
    """Job short id is the first segment of the session uuid."""
    short = (session_id or "")[:8]
    try:
        with open(os.path.join(JOBS_DIR, short, "state.json")) as fh:
            return json.load(fh).get("name") or short
    except Exception:
        return short


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


def gate_stats(limit: int | None = None) -> dict:
    """Summary of what the gate has actually done, for display in the UI.

    Reads the whole log rather than a fixed window: the file is already bounded
    by _trim_lines, and a fixed window made the headline count freeze at exactly
    the window size, which reads as a stalled tool rather than a capped view."""
    rows = _read_gate_rows(limit)
    if not rows:
        return {"total": 0}

    acts = defaultdict(int)
    per = defaultdict(lambda: {"n": 0, "warn": 0, "block": 0})
    for r in rows:
        a = r.get("action", "?")
        acts[a] += 1
        sid = r.get("session") or ""
        if not sid:
            continue
        per[sid]["n"] += 1
        if a in ("warn", "block"):
            per[sid][a] += 1
    lat = sorted(r.get("ms", 0) for r in rows if r.get("action") != "error")
    sessions = sorted(
        ({"name": session_name_for(k), **v} for k, v in per.items()),
        key=lambda s: -s["n"])[:4]
    paused = pause_until()
    return {
        "paused": bool(paused),
        "paused_until": (None if paused in (0, float("inf")) else paused),
        "total": len(rows), "allow": acts["allow"], "warn": acts["warn"],
        "block": acts["block"], "error": acts["error"],
        "healthy": acts["error"] == 0,
        "span_s": int(rows[-1].get("ts", 0) - rows[0].get("ts", 0)),
        "p50_ms": lat[len(lat) // 2] if lat else 0,
        "p95_ms": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0,
        "sessions": sessions,
        "last_block": next((r for r in reversed(rows)
                            if r.get("action") == "block"), None),
    }


def load_pending() -> list[dict]:
    try:
        with open(PENDING) as fh:
            return json.load(fh)
    except Exception:
        return []


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
        mode = os.environ.get("MEMMON_GATE", "block-critical")

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
        if mode == "off" or tool != "Bash" or not is_heavy(cmd):
            return 0

        vm = read_vm(fast=True)
        pres = pressure(vm)
        try:
            with open(SNAPSHOT) as fh:
                cached = json.load(fh)
        except Exception:
            cached = {}

        action, msg = gate_decision(tool, cmd, pres, cached, mode)

        # Rolling record of what every session asked to run and what we decided.
        # This is the only cross-session audit trail of the gate, and it is
        # written only for commands heavy enough to be evaluated.
        try:
            path = GATE_LOG
            with open(path, "a") as fh:
                fh.write(json.dumps({
                    "ts": time.time(), "cmd": cmd[:200], "mode": mode,
                    "level": pres.get("level"), "action": action,
                    "session": payload.get("session_id", "")[:8],
                    "cwd": payload.get("cwd", ""),
                    "reasons": pres.get("reasons", []),
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


def end_session(pid: int, apply: bool) -> str:
    """Terminate a Claude session by hand, tree and all.

    Refuses any pid that is not currently a session ROOT in the live snapshot —
    the caller passes a number, and a stale or mistyped one must never be able to
    kill an arbitrary process. SIGTERM, never SIGKILL, so the session gets to
    flush its transcript."""
    import signal
    snap = collect()
    sess = next((s for s in snap["sessions"] if s.get("root") == pid), None)
    if sess is None:
        live = ", ".join(f"{s['name']}={s.get('root')}" for s in snap["sessions"])
        return (f"refused: pid {pid} is not a live Claude session root.\n"
                f"live sessions: {live or 'none'}")

    ps = read_ps()
    tree = [pid] + descendants(pid, build_tree(ps))
    if not apply:
        return (f"would end \"{sess['name']}\" — {human(sess['mem'])} across "
                f"{len(tree)} process(es), root pid {pid}\n"
                f"re-run with --apply to do it.")

    # Children first, so the root does not respawn or re-adopt them mid-teardown.
    for p in sorted(tree, reverse=True):
        try:
            os.kill(p, signal.SIGTERM)
        except Exception:
            pass
    time.sleep(2)
    still = [p for p in tree if _alive(p)]
    return (f"ended \"{sess['name']}\" — SIGTERM to {len(tree)} process(es); "
            f"{len(still)} still winding down")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


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
        print(f"{pres['level']} — {' · '.join(pres['reasons'])} "
              f"(waiting, {left}s left)", flush=True)
        time.sleep(15)
    print("timed out still under pressure")
    return 1


# ---------------------------------------------------------------------- main

def main() -> int:
    # Short-circuit before the parser exists: gate() runs on every Bash tool call
    # and has no use for 24 argument definitions.
    if "--gate" in sys.argv:
        return gate()
    import argparse
    ap = argparse.ArgumentParser(prog="memmon", add_help=True)
    ap.add_argument("--once", action="store_true", help="one snapshot then exit")
    ap.add_argument("--json", action="store_true", help="machine-readable snapshot")
    ap.add_argument("--statusline", action="store_true", help="one compact line")
    ap.add_argument("--log", action="store_true", help="append a history sample")
    ap.add_argument("--report", action="store_true", help="per-owner averages")
    ap.add_argument("--days", type=int, default=7, help="report window (default 7)")
    ap.add_argument("--reap", action="store_true", help="list reclaimable orphans")
    ap.add_argument("--reap-spares", action="store_true",
                    help="list idle claude prewarm procs older than 4h")
    ap.add_argument("--apply", action="store_true", help="with --reap, actually kill")
    ap.add_argument("--gate", action="store_true",
                    help="PreToolUse hook: gate heavy commands on memory pressure")
    ap.add_argument("--blocked", action="store_true",
                    help="commands the gate refused that nobody has re-run")
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
        print(end_session(args.end_session, args.apply))
        return 0
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
    if args.blocked:
        pend = load_pending()
        if not pend:
            print("Nothing outstanding — no command has been blocked.")
            return 0
        lvl = pressure(read_vm(fast=True))["level"]
        print(f"{len(pend)} command(s) blocked and not yet re-run:\n")
        for b in pend:
            print(f"  {time.strftime('%H:%M', time.localtime(b['ts']))}  "
                  f"{b.get('session', '?')}")
            print(f"        {b.get('cmd', '')[:100]}")
            if b.get("cwd"):
                print(f"        in {b['cwd']}")
        print()
        print(f"current pressure: {lvl}  — "
              + ("safe to re-run these now" if lvl in ("HEALTHY", "WATCH")
                 else "still under pressure, wait"))
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
                      f"{clip(session_name_for(r.get('session', '')), 24)}")
                print(f"      {r.get('cmd', '')[:90]}")
        else:
            print("No command has ever been stopped — "
                  "the gate has not cost anyone a command.")
        print()
        print("recent decisions:")
        print(f"  {'when':<9}{'session':<24}{'level':<9}{'action':<7}{'ms':>4}  cmd")
        for r in rows[-12:]:
            print(f"  {time.strftime('%H:%M:%S', time.localtime(r['ts'])):<9}"
                  f"{clip(session_name_for(r.get('session', '')), 22):<24}"
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
              + (f"  ~{room:.0f} min headroom" if room is not None and room < 120
                 else ""))
        return 0 if p["level"] in ("HEALTHY", "WATCH") else 1
    if args.report:
        print(report(args.days))
        return 0

    color = sys.stdout.isatty() and not args.no_color

    if args.statusline:
        # Prefer the cached sample: the statusline must never block on `top`.
        try:
            with open(SNAPSHOT) as fh:
                r = json.load(fh)
            if time.time() - r["ts"] < 120:
                level = r.get("pressure", "HEALTHY")
                out = f"{LEVEL_ICON.get(level, '')} {human(r['swap_used'])} swap"
                if level != "HEALTHY":
                    out += f" · {level}"
                if r.get("orphan", 0) > GB:
                    out += f" · {human(r['orphan'])} reclaimable"
                print(out)
                return 0
        except Exception:
            pass
        print(statusline(collect()))
        return 0

    snap = collect()

    if args.json:
        print(json.dumps(snap, indent=1))
        return 0
    if args.log:
        log_sample(snap)
        return 0
    if args.reap:
        print(reap(snap, args.apply))
        return 0
    if args.reap_spares:
        print(reap_spares(snap, args.apply))
        return 0
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
