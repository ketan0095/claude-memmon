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
| Sampler | launchd job, 1 sample/min at Standard priority, history for `--report` | `--sampler` |
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
  from libproc (`proc_pidinfo`, footprint, start time), `top`, `ps`, `sysctl`,
  `vm_stat` and `lsof`, all of which run unprivileged for the current user's own
  processes.
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
- Pressure UNKNOWN (no valid rate baseline yet) → allowed silently, logged as
  UNKNOWN, nothing injected. The gate fails open; it never guesses HEALTHY.
- Under pressure, the advisory also names the largest heavy job `memmon run`
  did not start, addressed to the human: *"Ask the user before stopping it; do
  not stop it yourself."* Follow that. The gate's decision is unchanged.

`MEMMON_GATE` controls it: `block-critical` (default — refuses only at CRITICAL),
`block` (also at DANGER), `warn` (never refuses), `off`.

It **fails open on everything**. Malformed input, missing files, any exception →
exit 0, and the failure is recorded so a silently-broken gate is visible in
`memmon --gate-log` rather than looking like a quiet machine.

Tell the user how to turn it off instantly: `memmon --off` (or `--off 8h`).
An export in another terminal does not change an already-running Claude process.

## Coordinating heavy jobs across agents

Use `memmon run --label "<task>" -- <command> <args>` for foreground builds,
typechecks and tests that should share one machine-wide slot. Claude, Codex and
terminal commands use the same runner. `memmon jobs` explains who is running or
waiting; the menu-bar dashboard shows those jobs too.

Wrap repo-wide test, build and typecheck runs this way. That is what puts them
under admission and monitoring; anything started directly is only ever listed
under pressure, never stopped by memmon.

The default mode is `protect`: a job waits for a ticket, then starts when its
estimate (4 GB until memmon has seen its peak; `--reserve GB` overrides) fits
the committed-memory budget of 80 % of RAM and memory has stayed at Watch or
better for 30 s. `memmon run-mode paused` restores the old behaviour (start at
HEALTHY or WATCH), `observe` admits the old way and logs what protect would have
held. Without MemmonBar, the stderr `memmon:` line and `memmon jobs` are the
only signals that a running job needs attention. Nothing is cancelled by policy
unless the job was started with `--interruptible` and auto-cancel is on.

Waiting is bounded (600 seconds by default, `--timeout` overrides it). Exit
codes: the child's own; 2 nested runner; 75 cancelled by policy (trust the
stderr line, a child can exit 75 too); 124 gave up waiting or the queue is full
(32); 125 telemetry unavailable at the deadline or the runner unavailable;
126/127 launch failure; 128+signal cancelled. Exit 124 or 125 means the command
never started. Do not replace this with a `pgrep` waiting loop, wrap the same
command twice, or wrap an entire multi-hour agent session. Only wrapped jobs
coordinate; existing jobs and other resource governors remain independent. A
runner slot is not a worktree lock or permission to edit files.

Use it for foreground, non-interactive commands, not dev servers, daemon
launchers or interactive shells.

`memmon route on` (wrapping Claude's heavy Bash commands automatically) refuses
for now: the launcher cannot yet be shown to tell a Bash tool call from a
status-line command. Do not work around it; use `memmon run` explicitly.
`memmon route off` always works.

## Showing it in the terminal

```bash
memmon                 # live dashboard, alternate screen, ctrl-c to quit
memmon --once          # single snapshot, good for piping
memmon --gate-log      # what the gate has done, and whether it ever blocked
memmon --blocked       # commands refused that nobody has re-run
memmon --report        # per-app / per-worktree averages (needs --sampler)
memmon owners          # who owns each process; --json carries action tokens
```

`memmon act <action> --target <token>` stops one owner's job, server or session,
identity-checked per PID. `act` is the confirmed step: an agent must show the
row to a human first and act only on their say-so. Never loop it, and never
pass a `force` token without that confirmation.

For an always-visible readout, add `memmon --statusline` to a Claude Code
statusline command or shell prompt. It reads the cached sample and never blocks.
It prints `memmon: pressure unknown` when the last reading had no valid rates,
and `memmon: no sample for N min` when the sampler has not run for over 3 min.

Under DANGER or CRITICAL, `memmon owners` adds an *Under pressure* section: the
largest heavy jobs that `memmon run` did not start. "Idle" and "growing" there
are labels, not permission. Stopping one is the same confirmed `act` step as
above: show the row to the human and act only on their say-so.

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
| `--pressure` says UNKNOWN | no rate baseline: the sampler is not installed or has not run in the last 5 min. A fresh process has nothing to compare against; `memmon` (live) has one after its first refresh |
| Status line says "no sample for N min" | the sampler did not run; `launchctl list \| grep memmon` |
| Gate seems inert | Check `memmon --gate-log`; `MEMMON_GATE=off` disables it |

Do not report the install as done until `memmon --once` renders and, if you
installed it, `pgrep -f MemmonBar` returns exactly one pid.
