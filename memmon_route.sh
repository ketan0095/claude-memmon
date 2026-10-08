#!/bin/sh
# memmon route: Claude Code's CLAUDE_CODE_SHELL_PREFIX. Claude calls it with
# the whole assembled shell invocation as "$1", after its permission
# decision. Heavy Bash tool commands are wrapped in `memmon run`; everything
# else, and anything uncertain, runs unchanged, exactly once.
M="$HOME/.claude/memmon"
run_plain() { exec /bin/bash -c "$1"; }

# Off switch (takes effect in running sessions), or already inside a runner.
[ -e "$M/runner/coord/route.off" ] && run_plain "$1"
[ -n "${MEMMON_RUN_ID:-}" ] && run_plain "$1"
# Only Bash tool calls: hooks and MCP startup carry CLAUDE_PROJECT_DIR. A hook
# routed into the queue could lose its deny (I-11).
[ -n "${CLAUDE_PID:-}" ] || run_plain "$1"
[ -z "${CLAUDE_PROJECT_DIR+set}" ] || run_plain "$1"
# Only while `memmon route on` recorded it; a prefix set by hand, or left over
# from a rollback, passes everything through.
state=
[ -f "$M/runner/coord/route.json" ] && { IFS= read -r state < "$M/runner/coord/route.json" || :; }
case "$state" in
  *'"state": "on"'*) ;;
  *) run_plain "$1" ;;
esac

# Cheap prefilter on executable names; most commands stop here.
case "$1" in
  *tsc*|*vitest*|*jest*|*playwright*|*pytest*|*gradle*|*bazel*|*xcodebuild*|\
  *webpack*|*make*|*pnpm*|*npm*|*npx*|*yarn*|*bun*|*turbo*|*docker*|*cargo*|\
  *colima*|*next*|*expo*) ;;
  *) run_plain "$1" ;;
esac
[ -f "$M/memmon.py" ] && [ -f "$M/memmon_route.py" ] || run_plain "$1"

# Classify within 1 s: alarm(1) is the interpreter's first act and SIGALRM
# ends it, so a hang or any failure yields no verdict and passes through.
# -I keeps the session's working directory off sys.path.
verdict=$(/usr/bin/python3 -I -c 'import signal, sys
signal.alarm(1)
sys.path.insert(0, sys.argv[1])
import memmon_route
memmon_route.classify_main(sys.argv[2])' "$M" "$1" 2>/dev/null)
tab=$(printf '\t')
case "$verdict" in
  "wrap$tab"*)
    label=${verdict#wrap"$tab"}
    # -E -s: the session's PYTHON* variables and user site cannot stop the
    # runner from starting; the wrapped command still gets the full env.
    exec /usr/bin/python3 -E -s "$M/memmon.py" run --via route --label "$label" -- /bin/bash -c "$1" ;;
esac
run_plain "$1"
