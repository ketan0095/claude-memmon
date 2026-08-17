#!/bin/zsh
# PreToolUse fast path.
#
# This runs on EVERY tool call, so paying ~45ms of Python startup to decide that
# `git status` is harmless is not acceptable. Three stages, cheapest first:
#
#   1. a BROAD glob superset — one builtin dispatch, and the overwhelming
#      majority of commands exit here having forked nothing;
#   2. a quote-aware position pass over what survives, so a tool name sitting in
#      an argument, a path, or a grep pattern is not mistaken for an executable
#      (`cat vitest.config.ts` used to cost a full Python start, and a warning);
#   3. Python, which repeats the precise match and decides.
#
# Stage 2 may only ever REJECT what stage 1 admitted. Wrapper syntax it cannot
# resolve (sudo, timeout, bash -c) is passed through rather than judged: an
# ambiguous command costs one Python start, whereas guessing wrong would
# silently disable protection for the command most likely to be expensive.
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

# Peel the JSON string enough for zsh's own lexer. We deliberately do not invoke
# jq/python here: this is the every-command path. ${(z)} respects shell quotes,
# so a tool name in `echo "vitest"`, a grep pattern, or a filename remains an
# argument instead of looking like an executable position.
cmd=${cmd# }
cmd=${cmd#\"}
cmd=${cmd%%\"\},\"*}
cmd=${cmd//\\\\\"/\"}

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

# Keep the overwhelmingly common light path as cheap as the original wrapper:
# one builtin glob dispatch, then exit. Only text that could name a built-in or
# learned operation pays for quote-aware tokenisation below.
learned_candidate=''
case "$cmd" in
  ${~MEMMON_LEARNED}) learned_candidate=1 ;;
  *pnpm*|*npm*|*yarn*|*bun*|*npx*|*typecheck*|*lint*|*"turbo run"*|*"turbo watch"*|\
  *tsc*|*vitest*|*jest*|*playwright*|*pytest*|*docker*|*colima*|*webpack*|\
  *cargo*|*expo*|*gradle*|*bazel*|*xcodebuild*|*make*|*build*|*test*|*install*|*dev*) ;;
  *) exit 0 ;;
esac

# Precise built-in fast path for ordinary (unwrapped) shell segments. Wrapper
# syntax such as sudo/timeout falls through to the conservative glob superset
# below, so ambiguity can cost a Python start but can never disable protection.
words=(${(z)cmd})
at_command=1
position_match=''
ambiguous=''
for word in $words; do
  clean=${(Q)word}
  case "$clean" in
    ';'|'&&'|'||'|'|'|'&'|'('|')') at_command=1; continue ;;
  esac
  [[ -n $at_command ]] || continue
  case "$clean" in
    *=*) continue ;;
    if|then|else|elif|do|while|until|'!'|'{'|'}') continue ;;
    sudo|nohup|time|timeout|caffeinate|env|exec|command|bash|sh|zsh)
      ambiguous=1; break ;;
  esac
  exe=${clean:t}
  case "$exe" in
    pnpm|npm|yarn|bun|turbo|npx|tsc|vitest|jest|playwright|pytest|gradle|gradlew|\
    bazel|xcodebuild|webpack|make|docker|colima|cargo|next|expo)
      position_match=1; break ;;
  esac
  at_command=''
done

if [[ -z $position_match && -z $ambiguous ]]; then
  [[ -n $learned_candidate ]] || exit 0
fi

# Only heavy commands pay for the timestamp — the light path above never gets
# here, and this is the whole point of the split.
zmodload zsh/datetime 2>/dev/null
export MEMMON_T0=${EPOCHREALTIME:-0}

gate_py="$HOME/.claude/memmon/memmon.py"
[[ -x /usr/bin/python3 && -r "$gate_py" ]] || exit 0
exec /usr/bin/python3 "$gate_py" --gate <<EOF
$input
EOF
