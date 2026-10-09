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
SUMMARY_MAX = 1500
TITLE_MAX = 40
TOP_OWNERS = 8
EXTRA_PATH = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
GB = 1 << 30
PROMPT = (
    "You are helping someone on a Mac that runs several AI coding agents and dev tools. "
    "Below is memmon's snapshot of memory use. Give at most 4 short, concrete "
    "recommendations to free memory or reduce memory pressure, one per line, each "
    "naming the owner it concerns. If pressure is HEALTHY and nothing stands out, reply "
    "exactly: no action needed. Plain text only. Do not include shell commands or code.\n\n")
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
             and not re.fullmatch(r"[&|;<>()]+", w)]
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


def build_summary(payload: dict) -> str:
    """At most ~1,500 characters, the same for the same payload."""
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

    titles = {}
    rows = []
    owners = sorted(payload.get("owners") or [], key=lambda o: -(o.get("footprint_bytes") or 0))
    for o in owners:
        titles[o.get("owner_id")] = clean(o.get("title")) or KIND.get(o.get("kind"), "owner")
    for i, o in enumerate(owners[:TOP_OWNERS], 1):
        jobs = [j for j in o.get("jobs") or [] if j.get("kind") != "conversation"
                and (j.get("footprint_bytes") or 0) >= GB // 20]
        job_txt = ", ".join(f"{clean(j.get('kind'), 12)} {_gb(j.get('footprint_bytes'))}"
                            for j in jobs[:3])
        rows.append(f"{i}. {KIND.get(o.get('kind'), clean(o.get('kind'), 12))} "
                    f"\"{titles[o.get('owner_id')]}\" {_gb(o.get('footprint_bytes'))}, "
                    f"{_activity(o)}" + (f"; jobs: {job_txt}" if job_txt else ""))

    jobs = payload.get("runner_jobs") or []
    queue = (runner.get("queue") or {}).get("length") or 0
    adm = runner.get("admission") or {}
    hold = clean(adm.get("reason"), 60) if not adm.get("open") and queue else ""
    managed = [f"{clean(j.get('label'))} {clean(j.get('state'), 24)}"
               for j in jobs if j.get("state")][:4]
    tail = [f"Managed jobs (memmon run, mode {clean(runner.get('mode'), 10) or 'unknown'}): "
            + (", ".join(managed) if managed else "none")
            + (f"; {queue} queued" if queue else "") + (f"; holding: {hold}" if hold else "")]
    sugg = []
    for s in (payload.get("pressure_suggestions") or [])[:3]:
        idle = s.get("idle_s")
        bits = [f"{clean(s.get('label'))} ({clean(s.get('kind'), 12)})",
                f"in {titles.get(s.get('owner_id'), 'unattributed')}", _gb(s.get("footprint"))]
        if idle:
            bits.append(f"idle {int(idle // 60)} min")
        if s.get("growth_mb_min") is not None:
            bits.append(f"growing {s['growth_mb_min']:.0f} MB per min")
        sugg.append(" ".join(bits))
    tail.append("Under pressure, suggested stops: " + ("; ".join(sugg) if sugg else "none"))

    def render(n):
        return "\n".join(head + ["Top owners by memory:"] + rows[:n] + tail)
    n = len(rows)
    while n > 1 and len(render(n)) > SUMMARY_MAX:
        n -= 1
    return render(n)[:SUMMARY_MAX]


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


def cli(argv, payload_fn, pressure_fn=None, timeout: float = TIMEOUT_S) -> int:
    """payload_fn() -> owners --json payload; pressure_fn() -> (vm, pressure)
    from the legacy reader, for the reasons and swap figure."""
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
    prompt = PROMPT + build_summary(payload)
    if args.preview:
        print(json.dumps({"preview": prompt, "chars": len(prompt)}) if args.json else prompt)
        return 0
    try:
        result = run_claude(prompt, timeout)
    except ExplainError as exc:
        print(json.dumps({"error": str(exc)}) if args.json else f"memmon: {exc}",
              file=sys.stdout if args.json else sys.stderr)
        return 2
    print(json.dumps(result) if args.json else result["text"])
    return 0
