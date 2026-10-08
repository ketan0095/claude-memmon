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

# Cheap prefilter on executable names; most commands stop here.
case "$1" in
  *tsc*|*vitest*|*jest*|*playwright*|*pytest*|*gradle*|*bazel*|*xcodebuild*|\
  *webpack*|*make*|*pnpm*|*npm*|*npx*|*yarn*|*bun*|*turbo*|*docker*|*cargo*|\
  *colima*|*next*|*expo*) ;;
  *) run_plain "$1" ;;
esac
[ -f "$M/memmon.py" ] || run_plain "$1"

# Classify with a 1 s watchdog; a timeout or any failure passes through.
verdict=$(
  /usr/bin/python3 "$M/memmon.py" route-classify "$1" 2>/dev/null &
  c=$!
  ( sleep 1; kill -9 "$c" 2>/dev/null ) >/dev/null 2>&1 &
  w=$!
  wait "$c"
  kill "$w" 2>/dev/null
)
tab=$(printf '\t')
case "$verdict" in
  "wrap$tab"*)
    label=${verdict#wrap"$tab"}
    exec /usr/bin/python3 "$M/memmon.py" run --via route --label "$label" -- /bin/bash -c "$1" ;;
esac
run_plain "$1"
