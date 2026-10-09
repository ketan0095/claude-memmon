"""`memmon explain`: ask Claude, on demand, what to do about memory.

Nothing here runs unless a person asks (the CLI, or MemmonBar's Explain
button). It sends a short, deterministic summary of the owners payload, with
no PIDs, paths, tokens, working directories, usernames or emails, to
`claude -p` with every tool disabled, and prints the reply as text. It never
acts on the reply and never parses commands out of it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time

MODEL = "claude-haiku-5-5"
TIMEOUT_S = 60
SUMMARY_MAX = 2000
TITLE_MAX = 40
STOP_CANDIDATES = 6
REPLY_LINES = 3
EXTRA_PATH = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
GB = 1 << 30
WEEK_DAYS = 7
# Any of these makes a HEALTHY machine worth a "patterns" answer; with none,
# memmon says "Nothing to do" itself and Claude is not asked.
NOW_LEVELS = {"WATCH", "DANGER", "CRITICAL", "UNKNOWN"}
PEAK_TRIGGER_FRAC = 0.75             # the week's peak reached 75 % of RAM
GATE_TRIGGER = 1                     # any gate warn or stop this week
DUPLICATE_TRIGGER = 2                # the same server or service label twice, now
IDLE_OWNER_BYTES = 2 * GB            # an owner idle now at 2 GB or more ...
IDLE_OWNER_DAYS = 4                  # ... that held 2 GB or more on 4 of the 7 days
MIN_CANDIDATE = GB // 4
TITLES = {"now": "Free memory now", "patterns": "Patterns this week", "quiet": "Nothing to do"}
QUIET_TEXT = "Nothing to do. Memory is healthy."
PREAMBLE = ("You are helping someone on a Mac that runs several AI coding agents and dev "
            "tools. Below is memmon's view of memory: what each owner is, how idle it is, "
            "and whether memmon can stop it, which Activity Monitor cannot tell them.\n")
PROMPT_NOW = PREAMBLE + (
    "Memory is under pressure now. Using only the candidates listed, write at most 3 "
    "lines, ranked by memory freed and then by safety. Each line names the candidate's "
    "owner exactly as it is written in quotes, says why stopping it is safe, and roughly "
    "how many GB it frees. Plain text only, no shell commands or code.\n\n")
PROMPT_PATTERNS = PREAMBLE + (
    "Memory is healthy right now. From the week's patterns below, write at most 3 lines, "
    "each one structural change that would prevent the next memory squeeze. Each line "
    "names the owner it concerns exactly as it is written in quotes. Plain text only, no "
    "shell commands or code.\n\n")
KIND = {"claude": "Claude session", "codex": "Codex", "codex-app": "Codex app",
        "codex-ui": "Codex", "app": "app", "service": "service", "job": "managed job",
        "unknown": "unattributed"}
# Anything that could identify a person, a place on disk or a process.
_UNSAFE_WORD = re.compile(r"[/\\~@]|^[A-Za-z0-9_+=-]{24,}$")
_PID_LIKE = re.compile(r"\d{3,}")
_PID = re.compile(r"\bpids?\b", re.I)


def clean(text, limit: int = TITLE_MAX, numbers: bool = False) -> str:
    """Words that carry a path, an email, a long token or (unless `numbers`,
    for memmon's own reason strings) a PID-like number are dropped, then the
    rest is cut to `limit` characters."""
    text = re.sub(r"(MB|GB)/(s|min)\b", r"\1 per \2", str(text or ""))
    words = [w for w in text.split()
             if not _UNSAFE_WORD.search(w) and (numbers or not _PID_LIKE.search(w))
             and not re.fullmatch(r"[&|;<>()]+|\(?pids?[:#]?\)?", w, re.I)]
    out = _PID.sub("", " ".join(words))
    out = re.sub(r"\(\s*\)|\s{2,}", " ", out).strip(" ·,;:")
    return out[:limit].rstrip()


def _gb(n) -> str:
    return "unknown" if n is None else f"{n / GB:.1f} GB"


def _activity(owner: dict) -> str:
    if owner.get("activity"):
        return clean(owner["activity"]) or "active"
    cores = owner.get("cpu_cores")
    if cores is None:
        return "activity unknown"
    return "idle" if cores < 0.05 else f"using {cores:.1f} cores"


def _idle(owner: dict) -> bool:
    act = (owner.get("activity") or "").lower()
    if act:
        return act.startswith("idle")
    cores = owner.get("cpu_cores")
    return cores is not None and cores < 0.05


def _head(payload: dict) -> list:
    sysb = payload.get("system") or {}
    level = sysb.get("score_level") or "UNKNOWN"
    reasons = [clean(r, 60, numbers=True) for r in (sysb.get("reasons") or [])]
    reasons = [r for r in reasons if r]
    why = "; ".join(reasons) or clean(sysb.get("level_reason") or sysb.get("reason"), 80) or "none"
    head = [f"Pressure: {level} (kernel {sysb.get('pressure_level') or 'unknown'}); reasons: {why}",
            f"Memory: {_gb(sysb.get('used_bytes'))} used of {_gb(sysb.get('ram_bytes'))} RAM; "
            f"swap {_gb(sysb.get('swap_used_bytes'))}"]
    runner = payload.get("runner") or {}
    c = runner.get("committed") or {}
    if c.get("limit"):
        head.append(f"Committed {_gb((c.get('used') or 0) + (c.get('slack') or 0))} of "
                    f"{_gb(c['limit'])} limit, {_gb(c.get('free'))} free to admit")
    return head


def _titles(payload: dict) -> dict:
    return {o.get("owner_id"): clean(o.get("title")) or KIND.get(o.get("kind"), "owner")
            for o in payload.get("owners") or []}


def _fit(head: list, rows: list, tail: list) -> str:
    def render(n):
        return "\n".join(head + rows[:n] + tail)
    n = len(rows)
    while n > 1 and len(render(n)) > SUMMARY_MAX:
        n -= 1
    return render(n)[:SUMMARY_MAX]


def stop_candidates(payload: dict) -> list:
    """What a person could stop now, biggest first, with the evidence memmon
    has: idle time, growth, orphaned, and whether memmon can stop it."""
    titles = _titles(payload)
    out, seen = [], set()
    for sg in payload.get("pressure_suggestions") or []:
        seen.add(sg.get("job_id"))
        ev = []
        if sg.get("idle_s"):
            ev.append(f"idle {int(sg['idle_s'] // 60)} min")
        if sg.get("growth_mb_min") is not None:
            ev.append(f"growing {sg['growth_mb_min']:.0f} MB per min")
        if not sg.get("stop"):
            ev.append("orphaned or shares its session's group")
        out.append({"label": clean(sg.get("label")), "kind": f"{clean(sg.get('kind'), 12)} job",
                    "owner": titles.get(sg.get("owner_id"), "unattributed"),
                    "bytes": sg.get("footprint") or 0, "evidence": ev,
                    "stoppable": bool(sg.get("stop"))})
    for o in payload.get("owners") or []:
        title = titles.get(o.get("owner_id"))
        for j in o.get("jobs") or []:
            if j.get("kind") == "conversation" or j.get("job_id") in seen:
                continue
            if (j.get("footprint_bytes") or 0) < MIN_CANDIDATE:
                continue
            out.append({"label": clean(j.get("label")) or clean(j.get("kind"), 12),
                        "kind": f"{clean(j.get('kind'), 12)} job", "owner": title,
                        "bytes": j["footprint_bytes"], "evidence": [],
                        "stoppable": bool(j.get("token") and j.get("action"))})
        if _idle(o) and (o.get("footprint_bytes") or 0) >= MIN_CANDIDATE * 2:
            what = ("finished or idle Claude session" if o.get("kind") == "claude"
                    else f"idle {KIND.get(o.get('kind'), 'owner')}")
            out.append({"label": title, "kind": KIND.get(o.get("kind"), "owner"), "owner": title,
                        "bytes": o["footprint_bytes"], "evidence": [what],
                        "stoppable": bool(o.get("token") and o.get("actions"))})
    out.sort(key=lambda c: -c["bytes"])
    return out[:STOP_CANDIDATES]


def build_now_summary(payload: dict) -> str:
    rows = ["Stop candidates, biggest first:"]
    for i, c in enumerate(stop_candidates(payload), 1):
        where = f" in \"{c['owner']}\"" if c["owner"] and c["owner"] != c["label"] else ""
        rows.append(f"{i}. \"{c['label']}\" ({c['kind']}){where}: {_gb(c['bytes'])}"
                    + "".join(f", {e}" for e in c["evidence"])
                    + ("; memmon can stop it" if c["stoppable"] else "; memmon cannot stop it"))
    if len(rows) == 1:
        rows.append("none found")
    runner = payload.get("runner") or {}
    queue = (runner.get("queue") or {}).get("length") or 0
    adm = runner.get("admission") or {}
    hold = clean(adm.get("reason"), 60) if not adm.get("open") and queue else ""
    tail = [f"Managed jobs queued: {queue}" + (f"; holding: {hold}" if hold else "")] if queue else []
    return _fit(_head(payload), rows, tail)


# --------------------------------------------------------------- the week

def load_week(days: int = WEEK_DAYS) -> dict:
    """The week from memmon's own records, read only: `memmon usage` for the
    daily peaks and gate counts, plus, from the same history and gate logs,
    how many days each named owner held 2 GB or more and which gate rule
    warned most."""
    import memmon
    usage = memmon.usage(days)
    start = time.time() - days * 86400
    per_day = {}
    for ts, row in memmon._day_rows(memmon.HISTORY, start):
        try:
            day = time.strftime("%Y-%m-%d", time.localtime(ts))
            for group in ("sessions", "apps"):
                for name, mem in (row.get(group) or {}).items():
                    if isinstance(mem, (int, float)) and mem >= IDLE_OWNER_BYTES:
                        per_day.setdefault(str(name), set()).add(day)
        except (AttributeError, TypeError, ValueError):
            continue
    rules = {}
    for ts, row in memmon._day_rows(memmon.GATE_LOG, start):
        try:
            if row.get("action") in ("warn", "block"):
                rule = (row.get("classification") or {}).get("rule") or "unclassified"
                rules[rule] = rules.get(rule, 0) + 1
        except (AttributeError, TypeError):
            continue
    return {"usage": usage, "owner_days": {k: len(v) for k, v in per_day.items()},
            "rules": rules}


def duplicates(payload: dict) -> list:
    """Server jobs, or services, sharing one label across owners right now."""
    titles = _titles(payload)
    groups = {}
    for o in payload.get("owners") or []:
        if o.get("kind") == "service":
            groups.setdefault(("service", clean(o.get("title"))), []).append(titles[o.get("owner_id")])
        for j in o.get("jobs") or []:
            if j.get("kind") == "server":
                groups.setdefault(("server", clean(j.get("label"))), []).append(
                    titles.get(o.get("owner_id")))
    return [{"kind": k[0], "label": k[1], "owners": sorted(set(v)), "count": len(v)}
            for k, v in sorted(groups.items()) if k[1] and len(v) >= DUPLICATE_TRIGGER]


def heavy_idle_owners(payload: dict, week: dict) -> list:
    days = week.get("owner_days") or {}
    out = []
    for o in payload.get("owners") or []:
        if (o.get("footprint_bytes") or 0) < IDLE_OWNER_BYTES or not _idle(o):
            continue
        title = o.get("title") or ""
        n = max((v for k, v in days.items() if k and (k == title or title.startswith(k)
                                                    or k.startswith(title))), default=0)
        if n >= IDLE_OWNER_DAYS:
            out.append({"owner": clean(title), "bytes": o["footprint_bytes"], "days": n})
    return out


def pattern_triggers(payload: dict, week: dict) -> list:
    usage = week.get("usage") or {}
    ram = usage.get("ram_bytes") or (payload.get("system") or {}).get("ram_bytes") or 0
    series = usage.get("series") or []
    fired = []
    peak = max((e.get("mem_peak_bytes") or 0 for e in series), default=0)
    if ram and peak >= PEAK_TRIGGER_FRAC * ram:
        fired.append("week peak at or above 75 % of RAM")
    gate = sum((e.get("gate") or {}).get("warned", 0) + (e.get("gate") or {}).get("stopped", 0)
               for e in series)
    if gate >= GATE_TRIGGER:
        fired.append("the gate warned or stopped a command")
    if duplicates(payload):
        fired.append("duplicate servers or services")
    if heavy_idle_owners(payload, week):
        fired.append("an owner idle at 2 GB or more on most days")
    return fired


def select_mode(payload: dict, week: dict | None) -> str:
    level = (payload.get("system") or {}).get("score_level") or "UNKNOWN"
    if level != "HEALTHY":          # WATCH, DANGER, CRITICAL and UNKNOWN (NOW_LEVELS)
        return "now"
    return "patterns" if week is not None and pattern_triggers(payload, week) else "quiet"


SECTION_NAMES = {"claude": "Claude sessions", "codex": "Codex", "browser": "browsers",
                 "dev": "dev tools", "app": "apps", "service": "services", "other": "other"}


def build_patterns_summary(payload: dict, week: dict) -> str:
    usage = week.get("usage") or {}
    rows = ["Last 7 days (peak used, basis, biggest section):"]
    warned = stopped = 0
    for e in usage.get("series") or []:
        g = e.get("gate") or {}
        warned += g.get("warned", 0)
        stopped += g.get("stopped", 0)
        try:
            day = time.strftime("%a %d %b", time.strptime(e["date"], "%Y-%m-%d"))
        except (KeyError, ValueError):
            continue
        if not e.get("mem_peak_bytes"):
            rows.append(f"{day}: no samples")
            continue
        sec = e.get("by_section") or {}
        top = max((k for k in sec if sec[k]), key=lambda k: sec[k], default=None)
        rows.append(f"{day}: peak {_gb(e['mem_peak_bytes'])} ({e.get('mem_basis') or 'unknown'})"
                    + (f", top {SECTION_NAMES.get(top, top)} {_gb(sec[top])}" if top else ""))
    rules = week.get("rules") or {}
    tail = [f"Gate this week: warned {warned}, stopped {stopped}"
            + (f"; most-warned rule {clean(max(rules, key=rules.get), 30)} "
               f"({max(rules.values())})" if rules else "")]
    often = sorted(((clean(k), v) for k, v in (week.get("owner_days") or {}).items()
                    if v >= IDLE_OWNER_DAYS and clean(k)), key=lambda kv: (-kv[1], kv[0]))[:5]
    if often:
        tail.append("Held 2 GB or more on most days: "
                    + "; ".join(f"\"{k}\" {v} of 7 days" for k, v in often))
    for o in heavy_idle_owners(payload, week):
        tail.append(f"Idle now and heavy most days: \"{o['owner']}\" {_gb(o['bytes'])}")
    for d in duplicates(payload):
        tail.append(f"Duplicate {d['kind']} now: \"{d['label']}\" x{d['count']} in "
                    + ", ".join(f"\"{t}\"" for t in d["owners"]))
    titles = _titles(payload)
    biggest = sorted(payload.get("owners") or [], key=lambda o: -(o.get("footprint_bytes") or 0))
    tail.append("Biggest owners now: " + "; ".join(
        f"\"{titles[o.get('owner_id')]}\" ({KIND.get(o.get('kind'), 'owner')}) "
        f"{_gb(o.get('footprint_bytes'))}, {_activity(o)}" for o in biggest[:5]))
    tail.append("Triggered by: " + ("; ".join(pattern_triggers(payload, week)) or "nothing"))
    return _fit(_head(payload), rows, tail)


# -------------------------------------------------------- reply discipline

def owner_labels(summary: str) -> list:
    """Every quoted name in the summary: the only things a reply line may be about."""
    names = {m for m in re.findall(r'"([^"]{3,})"', summary)}
    return sorted(names - set(KIND.values()), key=len, reverse=True)


def filter_reply(text: str, labels: list) -> list:
    """Keep at most 3 plain lines that each name an owner from the summary.
    Generic advice ("close some tabs") names none and is dropped, and so is
    anything that looks like a command."""
    keep = []
    for line in (text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        if not line or "`" in line or line.startswith(("$", "sudo ", "kill ")):
            continue
        low = line.lower()
        if any(l.lower() in low for l in labels):
            keep.append(line)
        if len(keep) == REPLY_LINES:
            break
    return keep


# ------------------------------------------------------------------- call

def find_claude(path=None, extra=EXTRA_PATH) -> str | None:
    dirs = (path if path is not None else os.environ.get("PATH", "")).split(os.pathsep)
    dirs += [os.path.expanduser(d) for d in extra]
    return shutil.which("claude", path=os.pathsep.join(d for d in dirs if d))


def claude_argv(binary: str) -> list:
    # --tools "" disables every built-in tool (claude --help); nothing the
    # model says can run anything.
    return [binary, "-p", "--model", MODEL, "--output-format", "text",
            "--tools", "", "--no-session-persistence"]


class ExplainError(Exception):
    pass


def run_claude(prompt: str, timeout: float = TIMEOUT_S, binary: str | None = None) -> dict:
    binary = binary or find_claude()
    if not binary:
        raise ExplainError("claude not found on PATH, ~/.local/bin, /opt/homebrew/bin or /usr/local/bin")
    # The user's own environment, so their proxy or base URL applies, minus
    # anything of memmon's.
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMMON_")}
    t0 = time.monotonic()
    try:
        out = subprocess.run(claude_argv(binary), input=prompt, capture_output=True,
                             text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise ExplainError(f"claude did not answer within {timeout:g} s")
    except OSError as exc:
        raise ExplainError(f"could not start claude: {exc}")
    if out.returncode != 0:
        raise ExplainError(f"claude exited {out.returncode}: {(out.stderr or '')[-300:]}")
    return {"text": out.stdout.strip(), "model": MODEL, "chars_sent": len(prompt),
            "elapsed_s": round(time.monotonic() - t0, 2)}


def quiet_result() -> dict:
    return {"mode": "quiet", "title": TITLES["quiet"], "text": QUIET_TEXT}


def plan(payload: dict, week: dict | None) -> dict:
    """{mode, title, prompt or None, labels}. Quiet needs no prompt: memmon
    decides it, not Claude."""
    mode = select_mode(payload, week)
    if mode == "quiet":
        return {"mode": mode, "title": TITLES[mode], "prompt": None, "labels": []}
    summary = build_now_summary(payload) if mode == "now" else build_patterns_summary(payload, week)
    prompt = (PROMPT_NOW if mode == "now" else PROMPT_PATTERNS) + summary
    return {"mode": mode, "title": TITLES[mode], "prompt": prompt, "labels": owner_labels(summary)}


def cli(argv, payload_fn, pressure_fn=None, timeout: float = TIMEOUT_S, week_fn=None) -> int:
    """payload_fn() -> owners --json payload; pressure_fn() -> (vm, pressure)
    from the legacy reader, for the reasons and swap figure; week_fn() -> the
    week (load_week) when the machine is healthy."""
    import argparse
    ap = argparse.ArgumentParser(prog="memmon explain",
                                 description="Ask Claude, once, what to do about memory. "
                                             "Sends a short summary with no paths or PIDs.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--preview", action="store_true",
                    help="print exactly what would be sent, without calling Claude")
    args = ap.parse_args(argv)
    payload = payload_fn()
    if pressure_fn is not None:
        try:
            vm, pres = pressure_fn()
            payload = dict(payload, system=dict(payload.get("system") or {},
                                                reasons=pres.get("reasons"),
                                                swap_used_bytes=vm.get("swap_used")))
        except Exception:
            pass
    week = None
    if (payload.get("system") or {}).get("score_level") == "HEALTHY":
        try:
            week = (week_fn or load_week)()
        except Exception:
            week = {}
    p = plan(payload, week)
    if args.preview:
        if p["prompt"] is None:
            text = QUIET_TEXT + " Nothing would be sent to Claude."
            print(json.dumps({"preview": text, "chars": 0, "would_send": False, "mode": p["mode"],
                              "title": p["title"]}) if args.json else text)
        else:
            print(json.dumps({"preview": p["prompt"], "chars": len(p["prompt"]), "would_send": True,
                              "mode": p["mode"], "title": p["title"]})
                  if args.json else p["prompt"])
        return 0
    if p["prompt"] is None:
        result = quiet_result()
    else:
        try:
            result = run_claude(p["prompt"], timeout)
        except ExplainError as exc:
            print(json.dumps({"error": str(exc), "mode": p["mode"]}) if args.json
                  else f"memmon: {exc}", file=sys.stdout if args.json else sys.stderr)
            return 2
        lines = filter_reply(result["text"], p["labels"])
        if lines:
            result.update(mode=p["mode"], title=p["title"], text="\n".join(lines))
        else:
            result.update(quiet_result(), asked=p["mode"])
    print(json.dumps(result) if args.json else result["text"])
    return 0
