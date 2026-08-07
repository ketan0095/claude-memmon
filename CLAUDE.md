# CLAUDE.md — installing memmon

Instructions for Claude Code (or any agent) asked to install this repo.
A human can follow them too; the steps are the same.

**Say this to Claude after cloning:** *"Read CLAUDE.md and install memmon."*

---

## What it is for, in one paragraph

Activity Monitor already reports memory correctly, so this is not a measurement
tool. It exists because Activity Monitor shows fifteen processes called `node`
and cannot say which Claude session started one, whether that session finished an
hour ago, or whether killing it destroys work. memmon answers *who*, and can
reach them through a hook. If a user asks why they need it, that is the answer —
not "ps is wrong" (though it is: `ps` reports RSS, which excludes compressed
pages, and understates an idle 2.4 GB process as 47 MB).

## What you are installing

Four independent pieces. All are optional except the CLI.

| Piece | What it does | Flag |
|---|---|---|
| CLI | `memmon` — live dashboard + one-shot queries | *(always)* |
| Sampler | launchd job, 1 sample/min, history for `--report` | `--sampler` |
| Menu bar | `MemmonBar.app` — status dot + popover, at login | `--menubar` |
| Gate | `PreToolUse` hook so Claude sessions back off under memory pressure | `--gate` |

## Before you start — ask the user

The gate and the menu bar change things outside this repo. **Confirm before
installing them**, and say plainly what changes:

1. **`--gate` edits `~/.claude/settings.json`** to add one `PreToolUse` hook. It
   backs the file up first (`settings.json.bak.<timestamp>`), is idempotent, and
   preserves existing hooks. It affects **every** Claude session on the machine,
   not just this one.
2. **`--menubar` and `--sampler` register LaunchAgents** that start at login
   (`~/Library/LaunchAgents/dev.memmon.sampler*.plist`).

If the user only wants to look at memory, install the CLI alone — no flags.

## Prerequisites

```bash
sw_vers -productVersion          # need macOS 13+
ls /usr/bin/python3              # need Xcode CLT: xcode-select --install
which swiftc                     # only needed for --menubar
ls ~/.claude                     # only needed for session attribution + gate
```

`install.sh` preflights all of these and fails with a sentence saying what to do.
Nothing is pip-installed; the CLI is Python stdlib only.

## Install

```bash
./install.sh                          # CLI only
./install.sh --sampler --menubar --gate   # everything
```

Then verify — do not report success without this:

```bash
memmon --once          # dashboard renders, shows a verdict
memmon --pressure      # e.g. "HEALTHY  score=0  no pressure signals"
memmon --gate-log      # "gate healthy" once any heavy command has run
pgrep -f MemmonBar     # exactly one pid, if --menubar
launchctl list | grep memmon
```

If `pgrep` returns two pids, wait two seconds and re-check — the installer waits
for the old instance to exit, but a slow machine can lag.

## Permissions

**None are required.** This is worth stating to the user, because a memory
monitor sounds like it should need them.

- No `sudo`, at any point.
- No Screen Recording, Accessibility, or Full Disk Access. Every reading comes
  from `top`, `ps`, `sysctl`, `vm_stat` and `lsof`, all of which run unprivileged
  for the current user's own processes.
- The menu-bar app is compiled locally by `swiftc` and is ad-hoc signed with no
  quarantine attribute, so Gatekeeper does not prompt.
- **Notifications**: the sampler posts one via `osascript` when pressure clears
  and blocked commands are waiting. macOS may ask to allow notifications the
  first time. Declining costs only that notification.

Everything is local — no network calls, no telemetry.

## What the gate does to Claude sessions

Once `--gate` is installed, before any Bash command in any session:

- Not a heavy command (`git status`, `ls`, …) → exits in ~6 ms, nothing logged.
- Heavy (typecheck / build / test / install / docker / dev server) → reads memory
  pressure in ~70 ms and either stays silent, injects an advisory into the
  session's context, or refuses the command.

`MEMMON_GATE` controls it: `block-critical` (default — refuses only at CRITICAL),
`block` (also at DANGER), `warn` (never refuses), `off`.

It **fails open on everything**. Malformed input, missing files, any exception →
exit 0, and the failure is recorded so a silently-broken gate is visible in
`memmon --gate-log` rather than looking like a quiet machine.

Tell the user how to turn it off instantly: `export MEMMON_GATE=off`.

## Showing it in the terminal

```bash
memmon                 # live dashboard, alternate screen, ctrl-c to quit
memmon --once          # single snapshot, good for piping
memmon --gate-log      # what the gate has done, and whether it ever blocked
memmon --blocked       # commands refused that nobody has re-run
memmon --report        # per-app / per-worktree averages (needs --sampler)
```

For an always-visible readout, add `memmon --statusline` to a Claude Code
statusline command or shell prompt. It reads the cached sample and never blocks.

## Configuration (usually unnecessary)

Worktree attribution assumes directories named `monorepo-*` and tickets like
`ABC-123`. On a different layout, write `~/.claude/memmon/config.json`:

```json
{ "project_roots": ["~/code", "~/Desktop/Work"] }
```

The child directory of a root becomes the worktree name — no naming convention
needed. `worktree_pattern` and `ticket_pattern` are the regex fallbacks.

## Uninstall

```bash
./install.sh --uninstall
```

Removes the CLI, app, LaunchAgents and the settings.json hook (backing the file
up first). Collected history in `~/.claude/memmon/` is kept deliberately — delete
that directory too for a clean slate.

## If something looks wrong

| Symptom | Cause |
|---|---|
| Sessions show as unnamed or missing | `~/.claude/jobs` absent, or Claude Code not running |
| `swiftc not found` | Xcode CLT missing — `xcode-select --install` |
| Two menu-bar icons | Old instance still exiting; it resolves, re-run install if not |
| `--report` says no history | `--sampler` not installed |
| Gate seems inert | Check `memmon --gate-log`; `MEMMON_GATE=off` disables it |

Do not report the install as done until `memmon --once` renders and, if you
installed it, `pgrep -f MemmonBar` returns exactly one pid.
