"""`memmon update`: check the clone memmon was installed from, and update it.

install.sh records where it ran from (install.json: source clone, branch,
commit and flags). --check fetches that branch from the clone's origin and
reports what is new. --apply fast-forwards the clone and re-runs the clone's
own install.sh with the recorded flags, detached, so the menu bar that asked
for it can be restarted by the installer without killing it.

Network only on --check and --apply, and only through git in the recorded
clone. No sudo. Nothing else on disk is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

FETCH_TIMEOUT_S = 30
GIT_TIMEOUT_S = 60
COMMITS_SHOWN = 10
ALLOWED_FLAGS = ("--sampler", "--menubar", "--gate")
NO_INSTALL = "Run ./install.sh once from your clone to enable updates"
DONE_MARK = "memmon update: installer exited "
START_MARK = "memmon update: started "


def _paths(state_dir):
    return {"install": os.path.join(state_dir, "install.json"),
            "log": os.path.join(state_dir, "update.log"),
            "state": os.path.join(state_dir, "update-state.json")}


def read_install(state_dir) -> dict | None:
    try:
        with open(_paths(state_dir)["install"]) as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("source") else None


def recorded_flags(rec: dict) -> list:
    """Only the installer's own flags, in their usual order: install.json is
    a file on disk, and nothing else from it reaches a command line."""
    flags = rec.get("flags") or []
    return [f for f in ALLOWED_FLAGS if isinstance(flags, list) and f in flags]


def _git(src, *args, timeout=GIT_TIMEOUT_S):
    return subprocess.run(["git", "-C", src, *args], capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)


def _out(src, *args, timeout=GIT_TIMEOUT_S):
    try:
        r = _git(src, *args, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _short(sha):
    return sha[:7] if sha else None


def _unavailable(reason, rec=None):
    return {"state": "unavailable", "installed": _short((rec or {}).get("commit")),
            "latest": None, "behind": None, "commits": [], "reason": reason}


def _locate(state_dir):
    """(record, source, branch, error result or None)."""
    rec = read_install(state_dir)
    if rec is None:
        return None, None, None, _unavailable(NO_INSTALL)
    src = rec["source"]
    if not os.path.isdir(src):
        return rec, src, None, _unavailable(f"the clone memmon was installed from is gone ({os.path.basename(src)})", rec)
    if _out(src, "rev-parse", "--is-inside-work-tree") != "true" or not rec.get("commit"):
        return rec, src, None, _unavailable("the install source is not a git clone", rec)
    branch = rec.get("branch")
    current = _out(src, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch or current is None:
        return rec, src, None, _unavailable("the clone is on a detached HEAD", rec)
    if current != branch:
        return rec, src, None, _unavailable(f"the clone is on {current}, not {branch}", rec)
    return rec, src, branch, None


def check(state_dir, timeout=FETCH_TIMEOUT_S) -> dict:
    rec, src, branch, err = _locate(state_dir)
    if err:
        return err
    try:
        r = _git(src, "fetch", "--quiet", "origin", branch, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _unavailable(f"offline: fetch did not finish within {timeout:g} s", rec)
    except OSError as exc:
        return _unavailable(f"fetch failed: {exc}", rec)
    if r.returncode != 0:
        return _unavailable("offline or fetch failed: " + ((r.stderr or "").strip()[-200:] or
                                                          f"git exited {r.returncode}"), rec)
    latest = _out(src, "rev-parse", f"origin/{branch}")
    installed = rec["commit"]
    if not latest or _out(src, "cat-file", "-e", f"{installed}^{{commit}}") is None:
        return _unavailable("the installed commit is not in the clone", rec)
    behind = int(_out(src, "rev-list", "--count", f"{installed}..{latest}") or 0)
    log = _out(src, "log", f"-n{COMMITS_SHOWN}", "--format=%h%x09%s", f"{installed}..{latest}") or ""
    commits = [{"sha": line.split("\t", 1)[0][:7], "subject": line.split("\t", 1)[1]}
               for line in log.splitlines() if "\t" in line]
    return {"state": "available" if behind else "up_to_date", "installed": _short(installed),
            "latest": _short(latest), "behind": behind, "commits": commits, "reason": None}


class Refused(Exception):
    pass


def apply(state_dir, timeout=FETCH_TIMEOUT_S) -> dict:
    """Fast-forward the recorded clone and start its installer, detached."""
    c = check(state_dir, timeout)
    if c["state"] == "unavailable":
        raise Refused(c["reason"])
    rec, src, branch, _ = _locate(state_dir)
    if _out(src, "status", "--porcelain", "--untracked-files=no") != "":
        raise Refused("the clone has uncommitted changes; commit or stash them first")
    head = _out(src, "rev-parse", "HEAD")
    latest = _out(src, "rev-parse", f"origin/{branch}")
    try:
        ff = _git(src, "merge-base", "--is-ancestor", "HEAD", f"origin/{branch}").returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ff = False
    if not ff:
        raise Refused(f"the clone has commits that are not on origin/{branch}; "
                      "it cannot be fast-forwarded")
    try:
        pull = _git(src, "merge", "--ff-only", "--quiet", f"origin/{branch}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused(f"git could not fast-forward the clone: {exc}")
    if pull.returncode != 0:
        raise Refused("git could not fast-forward the clone: " + (pull.stderr or "").strip()[-200:])
    to = _out(src, "rev-parse", "HEAD") or latest
    return start_installer(state_dir, rec, src, _short(rec["commit"] or head), _short(to))


def start_installer(state_dir, rec, src, frm, to) -> dict:
    p = _paths(state_dir)
    flags = recorded_flags(rec)
    os.makedirs(state_dir, exist_ok=True)
    with open(p["log"], "a") as log:
        log.write(f"{START_MARK}{time.strftime('%Y-%m-%d %H:%M:%S')} {frm} -> {to} "
                  f"flags {' '.join(flags) or '(none)'}\n")
    # setsid: the installer restarts the menu bar that asked for this and
    # must not die with it. posix_spawn leaves nothing for this process to
    # reap. The trailing line is how --status learns the result.
    script = 'cd "$0" && bash ./install.sh "$@"; echo "' + DONE_MARK + '$?"'
    pid = os.posix_spawn("/bin/bash", ["/bin/bash", "-c", script, src, *flags], dict(os.environ),
                         file_actions=[(os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                                       (os.POSIX_SPAWN_OPEN, 1, p["log"],
                                        os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600),
                                       (os.POSIX_SPAWN_DUP2, 1, 2)],
                         setsid=True)
    state = {"started_at": round(time.time(), 3), "from": frm, "to": to, "flags": flags,
             "pid": pid}
    tmp = f"{p['state']}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, p["state"])
    return {"state": "started", "from": frm, "to": to, "log": p["log"]}


def status(state_dir) -> dict:
    """The last --apply: running, finished or failed (with the log's tail)."""
    p = _paths(state_dir)
    try:
        with open(p["state"]) as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return {"state": "none", "reason": "no update has been applied"}
    try:
        with open(p["log"]) as fh:
            lines = fh.read().splitlines()
    except OSError:
        lines = []
    start = max((i for i, l in enumerate(lines) if l.startswith(START_MARK)), default=0)
    run = lines[start:]
    done = [l for l in run if l.startswith(DONE_MARK)]
    out = {"from": st.get("from"), "to": st.get("to"), "started_at": st.get("started_at"),
           "log": p["log"]}
    if not done:
        return dict(out, state="running")
    code = done[-1][len(DONE_MARK):].strip()
    if code == "0":
        return dict(out, state="finished")
    tail = "\n".join(l for l in run if not l.startswith((START_MARK, DONE_MARK)))[-600:]
    return dict(out, state="failed", exit=code, tail=tail)


def _human(res: dict) -> str:
    s = res.get("state")
    if s == "available":
        lines = [f"{res['behind']} update{'s' if res['behind'] != 1 else ''} available "
                 f"({res['installed']} -> {res['latest']}):"]
        lines += [f"  {c['sha']} {c['subject']}" for c in res["commits"]]
        return "\n".join(lines + ["Run `memmon update --apply` to install them."])
    if s == "up_to_date":
        return f"Up to date ({res['installed']})."
    if s == "started":
        return f"Updating {res['from']} -> {res['to']}; the installer is running. Log: {res['log']}"
    if s in ("finished", "running"):
        return f"Last update {res.get('from')} -> {res.get('to')}: {s}."
    if s == "failed":
        return f"Last update failed (exit {res.get('exit')}):\n{res.get('tail', '')}"
    return res.get("reason") or res.get("error") or s


def cli(argv, state_dir) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="memmon update",
                                 description="Check for and install memmon updates from the "
                                             "clone it was installed from. Contacts the clone's "
                                             "git origin only with --check or --apply.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="fetch and list new commits")
    g.add_argument("--apply", action="store_true",
                   help="fast-forward the clone and re-run its installer with your flags")
    g.add_argument("--status", action="store_true", help="the last update's result")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        res = check(state_dir) if args.check else status(state_dir) if args.status else apply(state_dir)
    except Refused as exc:
        res = {"state": "refused", "error": str(exc)}
        print(json.dumps(res) if args.json else f"memmon: {exc}",
              file=sys.stdout if args.json else sys.stderr)
        return 2
    print(json.dumps(res) if args.json else _human(res))
    return 0
