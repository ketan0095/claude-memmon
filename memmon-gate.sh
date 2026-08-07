#!/bin/zsh
# PreToolUse fast path.
#
# This runs on EVERY tool call, so paying ~45ms of Python startup to decide that
# `git status` is harmless is not acceptable. The case globs below are a
# deliberately BROAD superset of memmon's HEAVY_CMD regex: anything that could
# possibly be heavy falls through to Python, which then does the precise match.
# Over-matching here costs one Python start; under-matching would silently
# disable the gate, so when in doubt add the pattern.
#
# zsh rather than bash for EPOCHREALTIME: stamping the start here is the only way
# to measure what a session actually waits for, since timing from inside Python
# hides interpreter startup, which is most of the cost.
#
# Fails open on everything — a monitoring tool must never block real work.
IFS= read -rd '' input   # builtin, no fork — this runs on every tool call

# Match the COMMAND, not the whole payload. The payload also carries cwd and
# transcript_path, so globbing it made any path containing "build"/"test"/
# "install"/"tsc" spawn Python for every command — measured 55ms vs 7ms for a
# `git status` under a worktree named …-Rebuild-Cache, and invisible because
# Python then rejects it before anything is logged.
case "$input" in
  *'"command":'*) cmd=${input#*\"command\":} ;;
  # No command key: nothing to match on, so do not fall back to globbing the
  # whole payload — that is the bug this block exists to prevent.
  *) exit 0 ;;
esac

# Commands this machine has learned are expensive — a generated pattern list, so
# a local wrapper script the built-in globs know nothing about still reaches
# Python. Sourcing a tiny file costs no fork; without it the profile could never
# take effect, because we would exit before Python ever saw the command.
MEMMON_LEARNED='__never_matches__'
MEMMON_PAUSED=''
[[ -r "$HOME/.claude/memmon/learned.zsh" ]] && source "$HOME/.claude/memmon/learned.zsh"

# Paused by `memmon --off`: cost nothing at all rather than merely declining to
# act, so an overnight run with the cap lifted pays no per-command price.
[[ -n $MEMMON_PAUSED ]] && exit 0

case "$cmd" in
  ${~MEMMON_LEARNED}) ;;
  *typecheck*|*"turbo run"*|*"turbo watch"*|*tsc*|*vitest*|*jest*|*playwright*|\
  *docker*|*colima*|*webpack*|*"cargo build"*|*expo*|*gradle*|*bazel*|*xcodebuild*|\
  *build*|*test*|*install*|*dev*) ;;
  *) exit 0 ;;
esac

# Only heavy commands pay for the timestamp — the light path above never gets
# here, and this is the whole point of the split.
zmodload zsh/datetime 2>/dev/null
export MEMMON_T0=${EPOCHREALTIME:-0}

exec /usr/bin/python3 "$HOME/.claude/memmon/memmon.py" --gate <<EOF
$input
EOF
