"""Opt-in routing of heavy Claude Bash commands through `memmon run`.

`memmon route on` points Claude Code's CLAUDE_CODE_SHELL_PREFIX at
memmon_route.sh. Claude Code calls the prefix with the whole assembled shell
invocation as one argument, after it has made its permission decision, so
routing never changes allow/deny/ask or the Bash timeout (I-11).

The prefix also runs hooks and MCP server startup. A hook that waited in the
admission queue could exit 124 without its decision JSON, and a denying hook
would silently stop blocking, so only Bash tool calls are ever routed. The
discriminator is the one the G1 probe observed: a Bash tool call has
CLAUDE_PID set and CLAUDE_PROJECT_DIR unset; hooks and MCP startup carry
CLAUDE_PROJECT_DIR. The status-line origin is not yet verified, so
`route on` refuses (G1_VERIFIED) and the integration stays advisory.

Fail-open happens only before launch: once the runner is exec'd nothing is
replayed, so every command runs exactly once.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import sys
import time
from pathlib import Path

SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
KEY = "CLAUDE_CODE_SHELL_PREFIX"
SCRIPT = "memmon_route.sh"
STUB = '#!/bin/sh\nexec /bin/bash -c "$1"\n'
# Both parts of gate G1 must hold before routing can be enabled. Part 1 (the
# single-$1 invocation) and the Bash/hook/MCP discriminator were confirmed by
# probe; the status-line origin was not.
G1_VERIFIED = False
G1_REFUSAL = ("route on is unavailable: Claude Code's shell prefix also runs hooks, MCP "
              "servers and the status line, and memmon has not verified that it can tell "
              "every one of those apart from a Bash tool call (gate G1). Wrapping a hook "
              "could turn its deny into an allow. Use `memmon run --label <task> -- <cmd>` "
              "from CLAUDE.md or AGENTS.md instead (see README).")
LINE_ON = "Route on: heavy Bash from Claude sessions started after it was turned on"
LINE_OFF = "Route off"

SERVER_WORDS = {"dev", "start", "serve", "watch"}
DAEMON_EXES = {("colima", "start"), ("expo", "start")}
DOCKER_LONG = {"--detach", "--interactive", "--tty"}
BACKGROUND_EXES = {"nohup", "setsid", "disown", "coproc"}
# Tools where a bare -w means watch (pnpm's -w is --workspace-root, jest's
# is the worker count).
WATCH_W_TOOLS = {"tsc", "vite", "webpack", "nodemon", "rollup", "esbuild", "babel",
                 "sass", "tailwindcss", "vitest", "tsup", "parcel"}
# Build tools whose `run` target starts the program rather than building it.
RUN_TOOLS = {"make", "gradle", "gradlew", "bazel", "bazelisk"}
INTERACTIVE_FLAGS = {"--ui", "--headed", "--debug", "--looponfail", "--interactive", "--tty"}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
SHORT_BUNDLE = re.compile(r"-[a-z]+")


def _paths(state_dir):
    coord = Path(state_dir) / "runner" / "coord"
    return {"coord": coord, "json": coord / "route.json", "off": coord / "route.off",
            "script": Path(state_dir) / SCRIPT}


# ------------------------------------------------------------- classify

def inner_command(invocation: str) -> str:
    """The user's command inside Claude Code's assembled invocation:
    `source <snapshot> … && eval '<command>' < /dev/null && pwd -P >| <tmp>`.
    Without that wrapper the whole text is the command."""
    try:
        toks = shlex.split(invocation, posix=True)
    except ValueError:
        return invocation
    for i, tok in enumerate(toks[:-1]):
        if tok == "eval":
            return toks[i + 1]
    return invocation


def _lex(cmd: str) -> list:
    # commenters="" as in memmon.shell_commands: a '#' inside a word must not
    # hide the rest of the command from this check.
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _backgrounds(tok: str) -> bool:
    """A punctuation run that holds a lone '&' ('&', '&)', '&;'), as opposed
    to '&&', '|&' or a redirection such as '>&', '&>' or '2>&1'."""
    if not tok or any(c not in "();<>|&" for c in tok):
        return False
    for joined in ("&&", ">&", "&>", "|&"):
        tok = tok.replace(joined, "")
    return "&" in tok


def _server_word(arg: str) -> bool:
    """dev, start, serve or watch as a word or a script part (dev:web,
    test:watch), and the long watch flags (--watch, --watchAll)."""
    if arg.startswith("--watch"):
        return True
    return any(part in SERVER_WORDS for part in arg.split(":"))


def _long_running(exe: str, args: list) -> str | None:
    """Why a command would keep running or wait on a person, or None.
    Anything named here passes through unwrapped, which is always safe."""
    tools = {exe} | {os.path.basename(a) for a in args}
    if any(_server_word(a) for a in args):
        return "server or watcher"
    if "-w" in args and tools & WATCH_W_TOOLS:
        return "server or watcher"
    if exe in RUN_TOOLS and any(a.rsplit(":", 1)[-1].endswith("run") for a in args):
        return "server or watcher"              # make run, gradle bootRun, bazel run
    if any(a.split("=", 1)[0] in INTERACTIVE_FLAGS for a in args):
        return "interactive"
    if "pytest" in tools and "-f" in args:
        return "interactive"                    # pytest-xdist looponfail
    return None


def _docker_daemon(args: list) -> bool:
    """docker … up, or run/exec detached or interactive in any spelling:
    -d, -dit, -itd, --detach, --detach=true, --tty=true."""
    if "up" in args:
        return True
    if not any(a in ("run", "exec", "create", "start") for a in args):
        return False
    for a in args:
        if a.split("=", 1)[0] in DOCKER_LONG:
            return True
        if SHORT_BUNDLE.fullmatch(a) and set(a[1:]) & {"d", "i", "t"}:
            return True
    return False


def route_classify(invocation: str, env=None, classify_fn=None, split_fn=None) -> tuple:
    """("wrap", label) or ("pass", reason). Anything uncertain passes."""
    env = os.environ if env is None else env
    if env.get("MEMMON_RUN_ID"):
        return "pass", "already inside memmon run"
    if not env.get("CLAUDE_PID") or "CLAUDE_PROJECT_DIR" in env:
        return "pass", "not a Bash tool call"
    if classify_fn is None or split_fn is None:
        import memmon
        classify_fn = classify_fn or memmon.classify_command
        split_fn = split_fn or memmon.shell_commands
    cmd = inner_command(invocation)
    try:
        toks = _lex(cmd)
    except ValueError:
        return "pass", "unparseable"
    if any(_backgrounds(t) for t in toks):
        return "pass", "sent to the background"
    for words in split_fn(cmd):
        while words and ASSIGNMENT.match(words[0]):
            words = words[1:]
        if not words:
            continue
        exe = os.path.basename(words[0]).lower()
        args = [w.lower() for w in words[1:]]
        if exe in BACKGROUND_EXES:
            return "pass", "sent to the background"
        if exe in ("memmon", "memmon.py") or (exe.startswith("python") and any(
                os.path.basename(a) == "memmon.py" for a in args[:2])):
            return "pass", "already wrapped"
        why = _long_running(exe, args)
        if why:
            return "pass", why
        if exe == "docker" and _docker_daemon(args):
            return "pass", "server or daemon"
        if (exe, args[0] if args else "") in DAEMON_EXES:
            return "pass", "server or daemon"
    hit = classify_fn(cmd)
    if not hit.get("matched"):
        return "pass", "not heavy"
    if any(_server_word(w) for w in (hit.get("shape") or "").lower().split()[1:]):
        return "pass", "server or watcher"
    label = (hit.get("shape") or hit.get("rule") or "heavy command")[:60]
    return "wrap", label


# ------------------------------------------------------------- settings

def _read_settings(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _write_atomic(path, text, mode=None):
    path = str(path)
    tmp = f"{path}.memmon.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_settings(path, data):
    """Replace the file a symlink points at, never the link, keeping its mode:
    settings.json often holds tokens and may be a managed dotfile."""
    real = os.path.realpath(path)
    try:
        mode = stat.S_IMODE(os.stat(real).st_mode)
    except FileNotFoundError:
        mode = 0o600
    _write_atomic(real, json.dumps(data, indent=2) + "\n", mode)


def status(state_dir, settings_path=None) -> dict:
    p = _paths(state_dir)
    try:
        current = (_read_settings(settings_path or SETTINGS).get("env") or {}).get(KEY)
    except (OSError, ValueError):
        current = None
    try:
        recorded = json.loads(p["json"].read_text()).get("state")
    except (OSError, ValueError, AttributeError):
        recorded = None
    on = current == str(p["script"]) and not p["off"].exists() and recorded == "on"
    return {"state": "on" if on else "off", "line": LINE_ON if on else LINE_OFF,
            "g1": "verified" if G1_VERIFIED else "unverified", "prefix": current}


def route_on(state_dir, settings_path=None, verified=None) -> tuple:
    """(exit code, message). Refuses without G1, without the installed
    script, or when a different prefix is already set."""
    verified = G1_VERIFIED if verified is None else verified
    if not verified:
        return 1, G1_REFUSAL
    p = _paths(state_dir)
    settings_path = settings_path or SETTINGS
    if not p["script"].is_file():
        return 1, f"route on needs {p['script']}; run ./install.sh first"
    try:
        cfg = _read_settings(settings_path)
    except (OSError, ValueError) as exc:
        return 1, f"cannot read {settings_path}: {exc}"
    env = cfg.get("env") or {}
    if not isinstance(env, dict):
        return 1, f"{settings_path}: env is not an object; not touching it"
    current = env.get(KEY)
    ours = str(p["script"])
    if current and current != ours:
        return 1, (f"refusing: {KEY} is already set to another prefix ({current}). "
                   "Remove it yourself if you want memmon to route.")
    p["coord"].mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        previous = json.loads(p["json"].read_text()).get("previous") if current == ours else current
    except (OSError, ValueError, AttributeError):
        previous = None
    # The launcher routes only while this record says on.
    _write_atomic(p["json"], json.dumps({"state": "on", "prefix": ours,
                                         "previous": previous, "ts": round(time.time(), 3)}))
    if current != ours:
        if os.path.exists(settings_path):
            stamp = time.strftime("%Y%m%d%H%M%S")
            shutil.copy2(os.path.realpath(settings_path), f"{settings_path}.bak.{stamp}")
        env[KEY] = ours
        cfg["env"] = env
        _write_settings(settings_path, cfg)
    p["off"].unlink(missing_ok=True)
    return 0, LINE_ON + ". Sessions already running are unaffected; `memmon route off` stops it at once."


def route_off(state_dir, settings_path=None) -> tuple:
    """route.off first (immediate, even for running sessions), then remove
    the key only while it is still memmon's, restoring any recorded value."""
    p = _paths(state_dir)
    settings_path = settings_path or SETTINGS
    p["coord"].mkdir(parents=True, exist_ok=True, mode=0o700)
    p["off"].touch()
    rec = {}
    try:
        rec = json.loads(p["json"].read_text())
    except (OSError, ValueError):
        pass
    note = ""
    try:
        cfg = _read_settings(settings_path)
        env = cfg.get("env")
        if isinstance(env, dict) and env.get(KEY) == str(p["script"]):
            if rec.get("previous"):
                env[KEY] = rec["previous"]
            else:
                del env[KEY]
            _write_settings(settings_path, cfg)
        elif isinstance(env, dict) and env.get(KEY):
            note = f" {KEY} is set to another prefix; left unchanged."
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        note = f" Could not edit {settings_path} ({exc}); route.off still disables routing."
    _write_atomic(p["json"], json.dumps({"state": "off", "prefix": str(p["script"]),
                                         "previous": rec.get("previous"),
                                         "ts": round(time.time(), 3)}))
    return 0, LINE_OFF + "." + note


# ------------------------------------------------------------------ CLI

def classify_main(invocation: str) -> None:
    """memmon_route.sh's entry point. Prints one verdict line; any failure
    prints "pass", and so does a timeout, by printing nothing."""
    try:
        verdict, detail = route_classify(invocation)
    except Exception as exc:
        verdict, detail = "pass", f"classifier error: {type(exc).__name__}"
    print(f"{verdict}\t{detail}")


def cli(argv, state_dir, classify=None, settings_path=None, split=None) -> int:
    if argv[0] == "route-classify":
        try:
            verdict, detail = route_classify(argv[1] if len(argv) > 1 else "",
                                             classify_fn=classify, split_fn=split)
        except Exception as exc:              # fail open: before launch only
            verdict, detail = "pass", f"classifier error: {type(exc).__name__}"
        print(f"{verdict}\t{detail}")
        return 0
    import argparse
    ap = argparse.ArgumentParser(prog="memmon route",
                                 description="Route heavy Claude Bash commands through memmon run.")
    ap.add_argument("action", choices=("on", "off", "status"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv[1:])
    if args.action == "status":
        st = status(state_dir, settings_path)
        print(json.dumps(st) if args.json else
              st["line"] + ("" if G1_VERIFIED else " (route on unavailable until gate G1 is verified)"))
        return 0
    fn = route_on if args.action == "on" else route_off
    code, msg = fn(state_dir, settings_path)
    print(("" if code == 0 else "memmon: ") + msg, file=sys.stdout if code == 0 else sys.stderr)
    return code
