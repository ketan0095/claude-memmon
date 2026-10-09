# memmon reference

The full reference for memmon. The [README](../README.md) covers install and everyday use.

## Contents

- [Installation](#installation)
- [Updating](#updating)
- [Permissions and privacy](#permissions-and-privacy)
- [Turning it off and uninstalling](#turning-it-off-and-uninstalling)
- [Commands](#commands)
- [Reading the popover](#reading-the-popover)
- [Owners and targeted stops](#owners-and-targeted-stops)
- [Under pressure: heavy work that memmon run did not start](#under-pressure-heavy-work-that-memmon-run-did-not-start)
- [The gate](#the-gate)
- [Shared heavy-job runner (Claude, Codex and terminal)](#shared-heavy-job-runner-claude-codex-and-terminal)
- [Last 7 days](#last-7-days)
- [Settings](#settings)
- [Ask Claude what to do](#ask-claude-what-to-do)
- [Crash prediction](#crash-prediction)
- [Two ways to measure memory](#two-ways-to-measure-memory)
- [Why not just use Activity Monitor](#why-not-just-use-activity-monitor)
- [How session attribution works](#how-session-attribution-works)
- [What it changes on its own](#what-it-changes-on-its-own)
- [Giving this to someone else](#giving-this-to-someone-else)
- [Working out whether you need a bigger machine](#working-out-whether-you-need-a-bigger-machine)
- [Configuration](#configuration)
- [Storage](#storage)
- [Troubleshooting](#troubleshooting)

## Installation

### Prerequisites

| Need | Check | If missing |
|---|---|---|
| macOS 13 or later | `sw_vers -productVersion` | — |
| `/usr/bin/python3` (Xcode Command Line Tools) | `ls /usr/bin/python3` | `xcode-select --install` |
| `swiftc`, only for `--menubar` | `which swiftc` | same as above |
| Claude Code, only for session names and the gate | `ls ~/.claude` | the memory monitor works without it |

Nothing is pip-installed. The CLI is Python standard library only. `install.sh`
checks each of these and stops with a sentence that says what to do.

### Run the installer

```bash
git clone https://github.com/ketan0095/claude-memmon.git memmon
cd memmon
./install.sh                                # CLI only
./install.sh --sampler --menubar --gate     # everything
```

| Flag | Adds |
|---|---|
| *(none)* | the `memmon` CLI at `~/.local/bin/memmon`; the code is copied to `~/.claude/memmon/` |
| `--sampler` | a launchd job that takes one sample a minute, for `--report`, Last 7 days and the menu-bar dot |
| `--menubar` | `MemmonBar.app`, compiled locally with `swiftc` and started at login |
| `--gate` | a `PreToolUse` hook so Claude sessions back off under memory pressure |
| `--uninstall` | removes all of the above; collected history is kept |

The code is copied, not symlinked, so deleting the clone does not break the
install. If `~/.local/bin` is not on your `PATH`, the installer says so.

### What changes outside the repo

The CLI alone writes only to `~/.local/bin/memmon` and `~/.claude/memmon/`. Two
flags change more, and you should know what before you run them:

| Flag | Changes | Undo |
|---|---|---|
| `--gate` | adds one `PreToolUse` hook (matcher `Bash`) to `~/.claude/settings.json`. It backs the file up first as `settings.json.bak.<timestamp>`, keeps your existing hooks, and never adds a second copy. It affects **every** Claude session on the machine. If `settings.json` does not exist, the gate is skipped. | `memmon --off` pauses it; `./install.sh --uninstall` removes it |
| `--menubar` | writes `~/Library/LaunchAgents/dev.memmon.bar.plist`, which starts the app at login | `./install.sh --uninstall` |
| `--sampler` | writes `~/Library/LaunchAgents/dev.memmon.sampler.plist`, which runs every 60 s | `./install.sh --uninstall` |

If you only want to look at memory, install the CLI alone.

**Or let Claude do it.** After cloning, tell Claude Code *"Read CLAUDE.md and
install memmon."* [`CLAUDE.md`](../CLAUDE.md) tells the agent to confirm the
`settings.json` and LaunchAgent changes with you before making them.

### Check that it works

Do not take the printed "installed" line as proof:

```bash
memmon --once              # the dashboard renders and shows a verdict
memmon --pressure          # e.g. "HEALTHY  score=0  no pressure signals"
memmon owners              # every process under exactly one owner
memmon --gate-log          # "gate healthy" once any heavy command has run (with --gate)
pgrep -f MemmonBar         # exactly one pid (with --menubar)
launchctl list | grep memmon
```

If `pgrep` shows two pids, wait two seconds and check again. The installer
waits for the old app to exit, but a slow machine can lag.

Expect a quiet first day. The gate does nothing visible until memory is tight,
`--report` and Last 7 days need the sampler to build history, and
`--profile` learns nothing until a command has run for a full minute.

## Updating

```bash
cd memmon
git pull --ff-only
./install.sh --sampler --menubar --gate    # the same flags you installed with
memmon --once
```

Re-running the installer is safe. It:

- copies the new code into `~/.claude/memmon/` one file at a time, through a
  temp file and a rename, so a running sampler never reads a half-written file;
- removes any older memmon LaunchAgent that points at `~/.claude/memmon/`,
  whatever its label, so an upgrade never leaves two samplers running;
- with `--sampler`, rewrites the sampler's plist and reloads it;
- with `--menubar`, rebuilds the app from source, stops the running one, waits
  for it to exit and starts the new one;
- with `--gate`, backs up `settings.json` again and leaves the hook alone if it
  is already there ("gate hook already present"). The hook points at
  `~/.claude/memmon/memmon-gate.sh`, which every run replaces, so the gate
  updates even without `--gate`;
- keeps your history, gate log, learned profile and `config.json`.

Pass `--menubar` again if you use the menu bar. Without it, the old app keeps
running its old build against the new CLI. An old menu bar still calls
`--end-session` and `--reap`, which now take the graceful path and can keep it
busy for about 25 seconds per click.

### Upgrade notes

- **A new popover.** The RAM and Swap tiles, the Reclaimable card, its Reap
  button and the separate session, worktree and app lists are gone. Every
  process now appears under exactly one owner, in sections, and orphaned builds
  show up under Background.
- **`reap --apply` stops at partial.** It used to send SIGTERM, wait two seconds
  and SIGKILL whatever was left. It now sends SIGTERM only, reports what
  survived, and prints `memmon reap --force <token>` for the survivors. See
  [Behaviour change: reap](#behaviour-change-reap---apply-stops-at-partial).
- **The runway badge is a trend, not a countdown.** `~N min left` is when free
  memory would reach the 20 % floor at the current rate. It is not a time until
  the machine freezes. It needs two readings at least 30 s apart, so it is
  sometimes missing.
- **`memmon run` admits by memory budget.** The default runner mode is now
  `protect`: a job starts when its estimate fits 80 % of RAM and memory has been
  at Watch or better for 30 s. `memmon run-mode paused` restores the old rule
  (start at HEALTHY or WATCH, wait at DANGER or CRITICAL).
- **The gate mode lives in `config.json`.** Set it with
  `memmon settings set gate_mode …` or the Settings panel. `MEMMON_GATE` in
  Claude Code's environment still overrides it.
- **Blocked commands expire.** A refused command is listed for 2 hours at most,
  and you can dismiss one.
- **UNKNOWN is a level.** A reading with no rate baseline says `UNKNOWN` instead
  of HEALTHY, and some rate fields in `--json` can be `null`. See
  [UNKNOWN](#unknown-no-reading-says-healthy-without-its-rates).
- **`memmon jobs --json` is schema 2**, a strict superset of schema 1.

## Permissions and privacy

**No permissions are required.** No `sudo`, no Screen Recording, no
Accessibility, no Full Disk Access. Every reading comes from libproc, `top`,
`ps`, `sysctl`, `vm_stat` and `lsof`, which run unprivileged for your own
processes. Other users' and root's processes are listed as "not itemised",
never as zero. The menu-bar app is compiled on your machine and ad-hoc signed,
so Gatekeeper does not prompt.

macOS may ask once whether memmon can post notifications. Declining costs only
the notifications.

**Everything is local.** memmon makes no network calls and sends no telemetry.
The one exception is one you start: *Ask Claude what to do* (`memmon explain`)
runs your own `claude` CLI, which sends Claude a fixed instruction and a
short summary. Nothing is sent until you click, and `memmon explain --preview`
prints the exact text in the terminal.

**What it records**, all in `~/.claude/memmon/`: session names, worktree names,
process names, per-app memory, and the first 200 characters of commands the gate
judged heavy. Treat that directory as you would your shell history. Install
without `--gate` if you do not want command lines on disk.

## Turning it off and uninstalling

### Turning the gate off

```bash
memmon --off        # pause until you turn it back on
memmon --off 8h     # pause, then resume on its own
memmon --on         # resume now
```

`--off` takes effect at once in every session, including ones already running.
Changing `MEMMON_GATE` in another terminal does not affect a Claude process that
is already running. To remove the gate entirely, run `./install.sh --uninstall`.

### Uninstall

```bash
./install.sh --uninstall
```

This turns route mode off, stops and removes the sampler and the menu bar,
removes the hook from `settings.json` (backing the file up first), and deletes
the CLI and the copied code. `memmon_route.sh` stays as a stub that just runs
its command, because sessions already running may still call it. History and
settings in `~/.claude/memmon/` are kept on purpose. Delete that directory for a
clean slate.

## Commands

```
memmon                 live dashboard (repaints in place, alternate screen; ctrl-c quits)
memmon --once          one snapshot, good for piping
memmon --json          machine-readable snapshot
memmon --statusline    one compact line for a shell or Claude statusline; never blocks
memmon --pressure      crash-risk verdict only (fast, no top); UNKNOWN exits 0
memmon --wait-safe [--timeout S]   block until memory pressure clears (default 600 s)
memmon --report [--days N]   per-app / per-worktree averages (needs --sampler)
memmon --profile       what this machine has learned costs memory

memmon owners [--json] [--cpu-window S] [--expand OWNER_ID]
                       every process under exactly one owner; --json is schema 2
memmon act ACTION --target TOKEN   identity-checked stop (see below)
memmon act force --target TOKEN    SIGKILL the survivors a partial result named
memmon reap [--apply] [--spares]   orphaned builds (or idle prewarms); --apply stops at partial
memmon reap --force TOKEN          SIGKILL exactly the survivors a partial reap named
memmon --reap / --reap-spares [--apply]   the older spellings of `reap`
memmon --end-session PID [--apply]        end a session by its root pid

memmon run [--label L] [--resource R] [--reserve GB] [--interruptible] [--timeout S] -- CMD
memmon jobs [--json]   who is running and who is waiting
memmon run-mode [protect|observe|paused] [--auto-cancel-interruptible on|off] [--json]

memmon usage [--json] [--days N]   the last 7 days by day (the menu bar's Last 7 days card)
memmon settings [--json]           the switches people change
memmon settings set KEY VALUE      see Settings below
memmon explain [--json] [--preview]   ask Claude what to do; --preview sends nothing
memmon route status|off            route mode (`route on` is refused; see below)

memmon --gate-log      what the gate has evaluated, advised and blocked
memmon --blocked       commands the gate refused in the last 2 hours that nobody has re-run
memmon --dismiss-blocked ID   stop listing one of them (the menu bar's Dismiss)
memmon --clear-blocked        empty the list
memmon --clear-gate-log       reset the gate counters
memmon --off [8h] / --on      pause or resume the gate
```

The terminal dashboard looks like this:

```
 MEMMON  16.0G · 8 cores ───────────────────────────────────────── 13:26:28
 RAM   █████████████████████████████████░░░ 15.0G/16.0G  compressed 7.4G
 SWAP  ███████████████████████░░░░░░░░░░░░  6.5G/8.0G   1.21x RAM size
 ▌ DANGER   swap 1.2x RAM size · heavy thrashing 180 MB/s · load 24  1 pt → CRITICAL
            web-checkout is running tsc typecheck (21.7G across 12 processes).
            Let it finish before starting another build.

 CLAUDE SESSIONS           TOTAL    RAM  SWAP~ PROC   AGE  STATE    DOING
  ● api error handling      6.8G   2.9G   3.9G   20   34m  working  infra + flake audit
      ├ tsc typecheck web-checkout                    3.5G    23m  pid 99036
      ├ subagents  9 active (Explore, schema-auditor, …)
      └ started    Docker (2h ago) · typecheck (56m ago)
```

## Reading the popover

From top to bottom:

- **Header.** A face that follows the pressure level, how fresh the reading is
  (`Live · 2s`), and the gear for Settings.
- **Protection pill.** Whether heavy commands are protected: `on`, `partial · N
  outside memmon run` (the gate is on, but some heavy work was not started
  through `memmon run`), `paused` or `off`. It never claims to prevent a freeze.
- **Memory ring.** Memory in use against RAM (the kernel's own accounting, not
  `top`'s), split by section, with the pressure level under the total. Free is
  split into *Empty now* and *Cache macOS can reclaim*. A section memmon could
  only partly measure shows its total as a bound. *Ask Claude what to do* sits
  under the legend.
- **Outcome banner.** After a stop, what exited and what memory in use did at
  the next sample, labelled as a measurement.
- **Under pressure.** Only at DANGER or CRITICAL; see below.
- **Last 7 days.** Collapsed by default. Memory, Top consumers and Protection
  views.
- **Sessions & apps.** One row per owner, grouped into sections. Sort by memory
  or CPU. A row shows its title, what it is doing, and where it runs
  (project · worktree · confidence). A value memmon could not measure shows `—`
  with the reason, and sorts last. Expand a session for its builds, tests and
  servers, each with its own Stop; the conversation is listed too and marked
  kept. *Technical details* shows PIDs, start time and confidence.
- **Managed jobs.** Jobs started with `memmon run`, running or waiting, and why.
- **Command protection.** Active or paused, with the pause control and counts of
  warnings and stops. Commands still waiting to be re-run are listed in full,
  each with Dismiss. Expanded, it shows the policy (`Healthy runs · Watch warns ·
  Danger warns · Critical stops`, matching your mode) and recent events as plain
  sentences you can open for the session, the time, why the command counts as
  heavy and what memory was doing. Only the three most recent stops show, behind
  a "Show all" toggle.

Every stop asks first, in an overlay inside the popover. Reaping stays on the
command line (`memmon reap --apply`, then `--force` if needed).

## Owners and targeted stops

`memmon owners` answers "whose is this?" for every process you can see. Each
process belongs to exactly one owner, so nothing is counted twice. When one
owner runs inside another (a session started from another session's shell),
the nearest one wins.

| Owner | Found by | Confidence |
|---|---|---|
| Claude session | `~/.claude/sessions/<pid>.json` or `/tmp/cc-socks/<pid>.sock`, each checked against the process start time (an idle prewarm is not a session) | exact |
| Codex `exec` | a `codex exec` process; its thread from the rollout file it holds open | inferred |
| Codex daemon / app-server | the shared server that hosts interactive threads | shared |
| Codex terminal frontend | an interactive `codex` connected to the Codex daemon (or started with `--remote`) that holds no thread itself and has no children — a pointer to the daemon | shared |
| Managed job | the child of a live `memmon run` lease | exact |
| App | an executable in `Contents/MacOS` of an `Applications/<Name>.app` (the outermost bundle, helpers included, or the code-signing clone an app re-executes from); one row per bundle id. Only processes running the main executable are instances; an app with none (widgets only) has no quit action, and neither has one that hosts a Claude or Codex session | exact |
| VM / container service | Docker Desktop or OrbStack (quit as an app), or the Virtualization VM process, Lima/Colima, qemu (a copyable stop command). A VM joins Docker Desktop or OrbStack only when macOS names that app responsible for it | shared |
| Unattributed | any other top-level process tree | unknown |

A language runtime that lives inside an app bundle (Python inside Xcode's
`Python3.framework`) does not make its programs part of that app, and memmon's
own process is never counted inside the app it runs in.

Memory is each process's footprint (what Activity Monitor and `top` call MEM),
summed over the owner. memmon reads it with libproc, in about 4 ms for every
process on the machine, instead of `top`'s ~0.6 s. A number memmon could not
read is shown as `—` with the reason, never as 0.

CPU needs two samples. The popover takes them a second apart; the sampler keeps
a baseline so the next tick can measure against it. Growth per 10 minutes is
shown only where it is known: in an owner's detail and in the Under pressure
list. It needs at least 5 samples over 10 minutes with no gap longer than 3
minutes, so a freshly started owner, or one seen across a sleep, says "not
enough history" there.

### Stopping something

`memmon act` is the only part of memmon that signals a process memmon did not
start (`memmon run` signals its own child's process group, escalating to
SIGKILL after 5 s), and the menu bar goes through it too. It is the confirmed
step: a script or agent shows the row to a person first and acts only on their
say-so. Nothing in memmon stops anything on its own. Each action takes a token
from `memmon owners --json` that names the exact processes it was shown, and
expires after 120 seconds:

| Action | Stops | Keeps running |
|---|---|---|
| `stop-job` / `stop-server` | one build, test or dev server inside a session | the session's conversation and every other owner |
| `stop-managed-job` | the child of a `memmon run`; its wrapper then exits 143 | the wrapper and everything else |
| `end-session` | a Claude session or a `codex exec`, down to any nested session | nested sessions (reported as kept) |
| `verify-app` | nothing — checks every instance of an app before the menu bar quits it, and answers with a fresh token for the check after the quit (where a reused PID reads `exited` and an unreadable one `unverified`) | — |

Every signal goes to one process at a time, immediately after re-reading that
process's identity (PID plus start time to the microsecond) and finding it
unchanged. There is no process-group kill. A PID that was reused, or a process
that moved to another owner, is refused rather than signalled. One residual
race remains: a process that exits and has its PID reused in the instant
between that re-read and the signal would be signalled.

It is graceful first. Everything gets SIGTERM; memmon then watches for up to 10
seconds, catching any child forked in the meantime, and reports exactly what is
still alive. Nothing is force-killed automatically. If something survives, the
result is `partial` with a force token naming only the survivors it signalled,
and only `memmon act force --target <token>` sends SIGKILL to them — after
re-reading each one again, and skipping any that has since become another
owner's root. Processes the stop only observed (a reparented child found in the
group, say) are listed as `forceable: false`; force never touches them and its
result stays `partial` while they run. A token lists at most 64 of them; the
rest are re-counted at force time from the process groups they were seen in
(`observed_unlisted`). A force result splits the named survivors that are gone
three ways: `killed` (sent SIGKILL, now gone), `exited_unsignalled` (left alone
as protected, or the signal could not be delivered, and gone anyway) and the
rest, which had already exited before force ran; `exited` is the total, kept
under that name for compatibility. Each remaining row has a `role` (`survivor`,
`observed` or `runner`), whether it was actually `signalled`, and for a
survivor force left alone a `skip_reason` (`protected` or `signal_failed`).

A stop never signals a `memmon run` wrapper while its child may still run,
because the wrapper would pass the signal to the child's whole process group.
That holds when the lease row has no child start time (older runners), when the
start is only known to the second, while the wrapper is still starting its
child, and for a wrapper started during the grace period. The child is stopped
by PID and the wrapper then exits on its own; one still alive at the end is
listed with role `runner`, and force waits for it once it has killed its child.
A dry run lists such wrappers as stopping once their job ends.

A reap's force token is accepted only by `memmon reap --force`, which re-checks
each survivor against the orphan or prewarm rule; `memmon act` refuses it. If a
job's process exited before the stop but what it started is still running in
its group, the result is `partial` (`root_exited`) with those processes listed
and nothing signalled.

The outcome is JSON on stdout with exit code 0 (stopped), 3 (partial, or
respawned), 4 (refused) or 1 (error). It reports memory in use before and after
as a measurement: other apps change it too, so it is never a promise of what was
freed.

Two cases end on purpose with exit 3:

- **Respawned.** Claude's daemon restarts a worker that dies mid-turn, under the
  same job, about ten seconds later. After ending a Claude session, memmon keeps
  watching (reading only) for up to 20 seconds after the first signal and
  reports `respawned` if that happens. It never signals the new worker; stop the
  job from Claude itself (`claude stop <job>`) if you want it gone.
- **Codex in a terminal.** A `codex` terminal session connected to the Codex
  daemon is only a window onto a thread the daemon runs, so it has no stop
  action. One running its thread itself is an owner you can end, but it usually
  ignores SIGTERM, so expect `partial` and a force step. A force-killed Codex
  terminal may need `reset`.

`MEMMON_INVENTORY=top` forces the old `ps`/`top` inventory. memmon also falls
back to it on its own if libproc ever fails its self-check; in that mode every
action is refused, because one-second start times are not an identity.

### Behaviour change: `reap --apply` stops at partial

`memmon reap --apply` used to send SIGTERM, wait two seconds and SIGKILL whatever
was left — including, if a PID had been reused in those two seconds, a process
that was not the orphan at all. It now uses the same engine as `memmon act`:
each orphan is re-checked against the orphan rule and must still be
unattributed, gets SIGTERM, and anything that survives is listed with a command:

```
memmon reap --force <token>
```

That token names only the processes that survived, by identity, and expires
after 120 seconds. Orphans that appeared after the first run are never touched
by it. `--reap-spares --apply` goes through the same engine. Without `--apply`
the same engine decides without signalling: the dry run lists every process
`--apply` would signal, descendants included, and every one it would refuse.
With `--apply`, `reap`, `--reap`, `--reap-spares` and `--end-session` exit with
the outcome's code (0 done, 3 partial, 4 refused).

## Under pressure: heavy work that `memmon run` did not start

The gate checks a command once, when it launches. A test run that starts at
HEALTHY and grows to 9 GB is invisible to it afterwards, and the runner watches
only what it started. So at DANGER or CRITICAL (or when the rates are
unavailable and the kernel's own level is warning or critical), memmon lists
the largest unmanaged test, build and server jobs: subtrees, never whole
sessions or apps, of at least 1 GiB (or 2 % of RAM), at most three, with their
growth and how long they have been idle.

Each row uses the same confirmed stop as the owner list (*Stop tests…*,
*Stop build…*, *Stop server…*). Two kinds have no button and say why: a job
that runs in its session's own process group, and an orphan, whose only stop
is the untargeted `reap`. **Nothing stops on its own.** "Idle" and "growing"
are labels, not permission. The sampler posts at most one notification per
job per episode, and never more than one every 5 minutes. The gate's advisory
names the top job and tells the agent to ask the user first. `memmon owners`
prints the same list.

To keep such work out of this list, start it through `memmon run`, which puts
it under admission and monitoring. `memmon settings set pressure_suggestions
false` turns all of this off.

## The gate

With `--gate`, a session about to run something heavy is told what the rest of
the machine is doing:

> System memory pressure is CRITICAL (swap 1.3x RAM size · heavy thrashing 210 MB/s).
> web-checkout is holding 23.0G of build processes. At the current rate, about 4 min
> until free memory reaches the 20 % floor (trend estimate). Do NOT start this command
> now… scope it down (`pnpm --filter <pkg>`).

### What a session sees

Every Bash tool call enters the hook:

```
Bash tool call
      │
      ▼
 shell fast-path            not build/test/install/docker/dev?
 (~6 ms, no Python) ─────────────────────────────────────────────►  exit 0, done
      │                                    (99%+ of calls — never logged)
      │ could be heavy
      ▼
 python gate (~70 ms)   reads sysctl + vm_stat only, never `top`
      │
      ├─ HEALTHY ─────────────►  exit 0, silent.        Command RUNS.
      │
      ├─ WATCH / DANGER ──────►  exit 0 + stdout JSON.  Command RUNS.
      │                          additionalContext is injected into the
      │                          session's context alongside the result.
      │
      └─ CRITICAL ────────────►  exit 2 + stderr.       Command DOES NOT RUN.
                                 Recorded in `memmon --blocked` to re-run later.
```

That is the default mode. The gate has four: `block-critical` (default),
`block` (also refuses at DANGER), `warn` (never refuses) and `off`.

A blocked command stays listed until the same session runs the same command
again, until it is dismissed, or for 2 hours, whichever comes first. The menu
bar shows the operation itself (`pnpm test:affected`), not the whole shell line,
which stays in the tooltip and the VoiceOver label.

Exit code 2 is the only code that stops a tool. Every other path returns 0,
including every error path: malformed input, missing files and any exception
all exit 0, and the failure is recorded so a broken gate shows in
`memmon --gate-log` rather than looking like a quiet machine.

**A warning does not prevent anything.** The command has already run by the
time the advisory lands; it shapes the session's *next* decision. Only a block
refuses.

On a block, the session receives this on stderr, as the tool result:

> System memory pressure is CRITICAL (swap 1.3x RAM size · heavy thrashing
> 210 MB/s). web-checkout is holding 23.0G of build processes. At the
> current rate, about 4 min until free memory reaches the 20 % floor (trend
> estimate). Do NOT start this command now — it would likely freeze the machine
> and lose work in every session. Either wait and retry, or scope it down (for
> example `pnpm --filter <package> typecheck` instead of a full-repo run). Check
> with `memmon --once`. This command has been recorded as outstanding
> (`memmon --blocked`).

On a warning, this arrives as context and the command proceeds:

```json
{"hookSpecificOutput": {
   "hookEventName": "PreToolUse",
   "additionalContext": "System memory pressure is WATCH (swap 44% of RAM size).
     web-checkout is holding 23.0G of build processes. Prefer a scoped
     command (`pnpm --filter <package> …`) or wait for the other build."}}
```

Both messages name the worktree actually holding the memory, so a session can
tell the pressure is not its own doing and decide which of *its* workloads is
expendable.

### Gate mode and pausing

Set the mode with `memmon settings set gate_mode warn` or the Settings panel. It
lives in `~/.claude/memmon/config.json` and takes effect on the next heavy
command in every session, running or not. `MEMMON_GATE` in the hook's
environment still overrides it. That environment is Claude Code's, not your
terminal's or the menu bar's, so `memmon settings` finds an override in two
places: the `env` block of `~/.claude/settings.json` (read, never written), and
the mode the gate last logged. When either shows one, `settings --json` reports
`source: "env"` with the gate's actual mode and a warning, and the menu bar
locks the control.

**Do not use the mode as a kill switch**: it is read inside Python, after a
heavy command has already started the gate. To switch the gate off, use
`memmon --off`. Paused, a command costs ~4 ms: the shell path exits before
Python starts. `--off 8h` resumes on its own, so an overnight run with the cap
deliberately lifted needs no cleanup in the morning.

### What it looks like in practice

Two things that happened once the gate was live. Both are the same mechanism:
the session is told what the rest of the machine is doing, and decides for
itself.

**A session shed its own load.** One session received a DANGER advisory while
memory was thrashing. It was running both a dev server and a test suite. Rather
than abandoning its work it killed the dev server, kept the tests, and said so:

> Memory pressure is in DANGER (paging at 111 MB/s). Killing the dev server to
> protect the test run — the api suite covers this directly, so it's the better
> signal anyway.

Over the following minutes swap fell 2.1 GB and load dropped from 20 to 3 on an
8-core machine. A blanket block could not have produced that outcome: the gate
has no idea a dev server matters less than a test run. Only the session knew.

**A session changed the question it asked.** Another was about to start a full
end-to-end run. Seeing two other worktrees holding dev servers for ~23 hours, it
put the trade-off to the human instead of just launching:

```
Memory is at its limit (swap 49%) with two other worktrees running dev
servers for ~23h. How do you want the end-to-end run done?

  1. Light E2E now, CI covers the suites
  2. Full E2E, I'll free memory first
  3. Wait and do it later
```

In both cases the command was allowed. What changed is that the session knew
the pressure was not its own doing and could name whose it was.

## Shared heavy-job runner (Claude, Codex and terminal)

The gate decides whether a command can start under memory pressure. It does not
coordinate two agents that both decide to start at the same time. For
foreground builds, tests and typechecks, opt in to the shared runner:

```bash
memmon run --label "api typecheck" -- pnpm --filter api typecheck
memmon run --label "targeted tests" --timeout 900 -- pnpm test --project api
memmon jobs
memmon jobs --json
```

`memmon run` waits for a ticket, then admits when the job's estimate fits the
committed-memory budget (80 % of RAM by default; see `headroom_frac`) and memory
has stayed at Watch or better for 30 s. A job's first run reserves 4 GB until
memmon learns its peak; `--reserve GB` overrides that. Tickets are first come,
first served (at most 32 waiting); a ticket whose resource is busy is skipped,
never one that merely does not fit.

`memmon run-mode protect|observe|paused` switches modes. `protect` is the
default. `paused` is the v1 behaviour (HEALTHY/WATCH start, DANGER/CRITICAL
wait), and `observe` admits as v1 does while logging what protect would have
held.

All callers on the same Mac and user share the default `heavy` slot, and only
one wrapped command holds it at a time. Each wait is bounded (600 seconds by
default), with progress on stderr explaining the hold. A running job is watched
every 2 s; growth past its reservation while the budget is over, or DANGER or
CRITICAL for two ticks, flags it for intervention, which the menu bar shows with
a confirmed Stop and a notification. Without MemmonBar, the stderr `memmon:`
line and `memmon jobs` are the only intervention signals. Only a job started
with `--interruptible`, with auto-cancel on (`memmon run-mode
--auto-cancel-interruptible on`), is ever cancelled by policy: after 10 s at
CRITICAL. `jobs --json` returns schema 2, a strict superset of v1: mode, the
committed budget, admission, queue and per-job state, reason, reservation,
estimate and footprint. It never includes command arguments.

**Use it from both agents:** replace the command the agent would run with
`memmon run --label "<task>" -- <that command>`. Codex can call this directly
through its shell tool; no Claude hook is involved. Commands not started through
it are not intercepted or queued after the fact. Do not wrap a whole agent
session, a dev server, an interactive command or another resource governor, and
do not replace the runner with a `pgrep` waiting loop. Nested runners fail
immediately rather than deadlocking. A `--resource call-rig` slot can serialize
a separate foreground resource, but different resource names do not serialize
each other, and no slot locks files.

The wait timeout limits **acquisition**, not command runtime. Commands inherit
stdin, stdout and stderr, receive literal arguments (no implicit shell), and keep
their exit status. An admitted job's stderr is byte-identical; memmon writes
`memmon:` lines only when it holds, refuses or intervenes.

| Exit | Meaning |
|---|---|
| child's code | admitted; passed through unchanged |
| 2 | nested runner, or a usage error |
| 75 | cancelled by policy; trust the stderr line `memmon: cancelled by policy …`, since a child can exit 75 too |
| 124 | gave up: the wait expired, or `memmon: admission queue full (32)`. The command never started |
| 125 | telemetry unavailable at the deadline, or the runner itself unavailable |
| 126 / 127 | could not start / command not found |
| 128+sig | the wrapper was cancelled |

A child can return any of these codes too, so use the stderr message to tell
runner outcomes apart.

Kernel locks release on process exit and need no stale-PID cleanup or lease
expiry. The command inherits its resource lock: even SIGKILL of the wrapper does
not admit another command while that child is alive. Catchable cancellation is
forwarded to the command's own process group; after five seconds a
still-running child is killed. Detached or background jobs that close inherited
descriptors are outside this contract.

Runner metadata is local under `~/.claude/memmon/runner`. It records labels and
working directories, **not command arguments**; keep secrets out of labels.
Stale records are ignored using kernel locks and pruned on the next run. The
empty `*.lock` files that stay behind are intentional: deleting an in-use lock
file can break exclusivity. `memmon --off` pauses the Bash gate; it does not
turn off the runner or its pressure check.

### Route mode is refused

`memmon route on` would point Claude Code's `CLAUDE_CODE_SHELL_PREFIX` at a
launcher that wraps heavy Bash commands in `memmon run`. It is refused until
gate G1 is verified: the shell prefix also runs hooks, MCP servers and the
status line, and memmon has not verified that it can tell every one of those
apart from a Bash tool call. Wrapping a hook could turn its deny into an allow.
Until then the Claude integration is advisory only (the gate's
`additionalContext`); use `memmon run` from CLAUDE.md or AGENTS.md instead.
`memmon route status` explains the refusal, and `memmon route off` always works.

## Last 7 days

`memmon usage --json [--days 7]` summarises each local day from the existing
`history.jsonl`, `gate.jsonl` and the runner's admission log. There is no rollup
file, only a cache in `runner/coord/usage-cache.json` keyed by those files' size
and mtime. Each day has its sample count, peak and average memory, memory by
section, gate warnings and stops, and runner holds. A day with no samples is
empty, never interpolated.

Memory is the strict "used" figure where the sampler recorded it. Rows written
before that field existed are estimated from free memory and marked as
estimates (`≈` in the menu bar); `top`'s own "used" figure is never charted.

Sections come only from what a history row records. Claude sessions and
Claude's runtime pool are `claude`. Each app the row names goes to `browser`,
`dev` (terminals and editors) or `service` (Docker Desktop, OrbStack, a VM) by
the same rules as the owner list, and to `app` otherwise. Worktree builds and
the window server are `other`. Codex and policy cancels are not in history, so
they are `null` ("not recorded"), as are runner holds for days older than the
admission log.

## Settings

`memmon settings` shows the few switches people change, and the menu bar's gear
button shows the same panel. Nothing here touches `~/.claude/settings.json`.

```bash
memmon settings --json                         # every setting, with its source
memmon settings set gate_mode warn             # block-critical | block | warn | off
memmon settings set runner_mode observe        # protect | observe | paused (same as run-mode)
memmon settings set auto_cancel_interruptible true
memmon settings set pressure_suggestions false # the Under pressure list and its notices
memmon settings set notifications false        # memmon's macOS notifications
```

Boolean values are `true` or `false`. A set prints the settings after the change
and exits 0. An unknown key or a bad value exits 2 with `{"error": …, "key": …}`
and changes nothing. Writes go through a temp file and keep every other key in
`config.json` (the runner's two settings live in `runner/coord/runner.json`);
concurrent sets are serialised by `runner/coord/config.lock`, and a write that
fails exits 2 the same way.

`gate_mode` reports where its value came from: `env` when `MEMMON_GATE` is set
in the hook's environment (and then it wins), `config`, or `default`. When the
hook's environment changes, the report catches up at the gate's next heavy
command. Pausing stays `memmon --off [DURATION]` and `memmon --on`.

The theme (System, Light or Dark) is a menu-bar preference only; it has no CLI
key.

## Ask Claude what to do

`memmon explain` asks Claude, once, what to do about memory. It never runs on
its own: only the menu bar's *Ask Claude what to do* button or the command
starts it.

- **Three kinds of answer**, picked by memmon from the current state:
  - *Free memory now*, under pressure: at most three owners to stop, ranked by
    memory freed and safety, using what memmon knows (idle time, growth,
    whether memmon can stop it).
  - *Patterns this week*, when memory is healthy but the last 7 days show
    something worth changing: high peaks, repeated gate warnings, duplicate
    servers, or an owner that sits idle at 2 GB or more most days.
  - *Nothing to do*, when none of that applies. memmon says so itself and
    sends nothing.
- **Only specific advice is kept.** A reply line that does not name one of the
  owners in the summary is dropped, and at most three lines are shown.
- **What is sent.** A fixed instruction and a summary of the pressure reading,
  the owners (names, kinds, sizes, idle time) and, for patterns, the week's
  daily peaks and gate counts. Paths, PIDs, tokens, working directories,
  usernames and emails are removed. `memmon explain --preview` prints the exact
  text and sends nothing.
- **How it is sent.** memmon runs your `claude` CLI (`claude -p`, found on
  `PATH`, `~/.local/bin`, `/opt/homebrew/bin` or `/usr/local/bin`) with every
  tool disabled, so Claude can only reply. The call is bounded to 60 seconds and
  can be cancelled.
- **What happens to the reply.** It is shown as plain text, labelled as coming
  from Claude. memmon never acts on it and never reads commands out of it.

If `claude` is not installed, the button reports that and nothing else
changes.

## Crash prediction

`HEALTHY → WATCH → DANGER → CRITICAL`, scored from:

| Signal | Points |
|---|---|
| swap ÷ **RAM size** ≥1.0 / ≥0.5 / ≥0.25 | +4 / +2 / +1 |
| swapin+swapout ≥150 / ≥50 / ≥10 MB/s | +4 / +2 / +1 |
| kernel headroom ≤12% / ≤20% | +3 / +2 |
| swap growth ≥500 / ≥150 MB/min | +2 / +1 |
| load ÷ cores ≥3 / ≥1.75 | +2 / +1 |

≥7 CRITICAL, ≥4 DANGER, ≥2 WATCH.

The score is deliberately **not** based on swap as a share of swap size: macOS
grows the swapfile to match demand, so that ratio sits near 100 % on an idle
machine.

**Validated by replay.** Scored against 929 logged samples spanning a real
near-freeze:

| Window | Verdicts |
|---|---|
| Crisis (swap 17–25 G, load 12–44) | **DANGER 78% · CRITICAL 6% · WATCH 15% · HEALTHY 0%** |
| Calm (swap 2–4 G) | **HEALTHY 99%** |

The first scoring attempt reported HEALTHY *during* the crisis. The replay is
what caught it, and why swap-vs-RAM was promoted over free memory.

**All memory counts, not just Claude's.** Consumers are ranked across builds,
resident processes and applications alike, and the advice names whichever is
actually largest. If that is a browser it says so, because closing tabs frees
more than scoping any build.

### What "headroom" means

Not "unused RAM": macOS keeps nearly all memory busy with cache and the
compressor, so unused RAM sits near zero on a healthy machine and tells you
nothing. Headroom is `kern.memorystatus_level`, the kernel's own percentage, the
one it consults when deciding whether to start killing processes. It is the
weakest signal in the table: it measured 28 % both mid-crisis and idle.

The `~N min left` badge projects when that figure reaches **20 %**, at the
current rate of decline. It is a trend estimate of when free memory reaches
that floor, not a time until the machine freezes, which nothing on macOS can
predict. It needs two readings of the percentage at least 30 seconds apart: the
figure is a whole percentage, so over 2 seconds a single tick would read as 30
points a minute. With no such pair the estimate is left out.

```
headroom_min = (current % − 20) ÷ (percentage points lost per minute)
```

So 46 % falling at 2 points a minute reads as ~13 minutes.

It targets 20 % rather than 0 % because 0 % never happens: on the machine this
was built for, the minimum ever recorded is 18 %, and only 2 of 2,536 samples
went below 20 %. Treat it as "the trend is bad, roughly this bad". It is a
straight line through two points, and memory use is bursty enough that the
slope often inverts within the minute. For that reason it may only promote
WATCH to DANGER after **two consecutive** low readings, and when it does, it is
added to the stated reasons.

### UNKNOWN: no reading says HEALTHY without its rates

Paging and swap growth are rates, so a verdict needs a baseline: an earlier
reading from the same boot, 2 to 300 seconds old. The sampler takes its own,
two reads 2 seconds apart inside each run. A one-shot reader (`--pressure`,
`--once`, the gate) seeds from the sampler's `pressure.json`, then from
`latest.json`. With no valid baseline the level is the string `UNKNOWN` with a
reason, never HEALTHY. If the instantaneous signals alone already reach WATCH
or worse, that level stands as a lower bound and says rates are unavailable.

Each reader treats UNKNOWN as fail-open: the gate allows silently and injects
nothing, `--pressure` prints UNKNOWN and exits 0, `--wait-safe` keeps waiting
(its next poll has a baseline), and the status line prints
`memmon: pressure unknown`. A `memmon run` in `paused` mode treats it as WATCH
and admits; `protect` reads the strict path only.

**Changed values for v1 parsers.** `level` stays a string, but some numbers can
now be `null`. In `--json` and the pressure fields, `thrash_mbs`, `swapin_mbs`,
`swapout_mbs` and `swap_growth_mbmin` are `null` when the rates are unavailable,
and `free_delta_min` is `null` whenever there is no free-percentage baseline at
least 30 s old. v1 always wrote numbers there, with 0.0 meaning "no baseline". A
`latest.json` row also has `swapins`/`swapouts` as `null` when vm_stat could not
be read, rather than 0.

### Sampling gaps

On a starved machine the sampler may not get scheduled at all. Each row records
`mono` (CLOCK_MONOTONIC_RAW, which keeps counting while the Mac sleeps),
`uptime` (CLOCK_UPTIME_RAW, which does not) and `boot`
(`kern.bootsessionuuid`). More than 150 s between rows is a gap: the difference
of the two clocks is the time asleep, and the rest is awake time nobody
sampled. A gap with at most 150 s awake is `sleep`; anything more is `starved`;
a changed boot is `reboot`. Rows are never back-filled, and `--report` lists
the gaps in its window. After a starved gap with at least 5 min awake, the
sampler posts one notification and the menu bar shows a notice for a day. The
notice never says why sampling stopped: an unloaded sampler looks the same.

Each run has a 40 s budget. If `top` or `lsof` stalls past it, the run writes
what it has as a `partial` row and exits, so the next minute's run is not
hidden behind it. The sampler runs at launchd's Standard priority rather than
Background, which would yield to exactly the contention it is there to observe.
Standard still has light resource limits, so this lessens throttling; it does
not remove it.

## Two ways to measure memory

Every process has two memory numbers, and they can differ by 50x.

| | What it counts | Where you see it |
|---|---|---|
| **RSS** | Only the pages sitting in physical RAM right now, uncompressed | `ps`, `htop`, most scripts |
| **Footprint** | Everything the process owns, including pages macOS has compressed | `top`'s `MEM`, Activity Monitor, memmon |

macOS compresses memory aggressively. The moment it compresses a page, that page
leaves RSS, but the process still owns it and still needs it back. So RSS falls
while the real footprint does not move. Measured on the machine this was built
for:

```
pid     command      ps RSS   top MEM    CMPRS   ratio
33847   node            47M     2394M    2289M    50.5x
48888   node            62M     2208M    2125M    35.5x
11351   node            36M     1640M    1544M    45.5x
17525   claude         236M      517M     310M     2.2x
```

`ps` says 47 MB. That process is holding 2.4 GB, nearly all of it compressed.
Actively running processes sit near 1x, because they keep touching their pages.
It is the **idle-but-huge** processes that vanish from RSS: a finished build
still holding gigabytes, a session that stopped working an hour ago. So any tool
built on RSS reports a healthy machine during the event you are investigating.

## Why not just use Activity Monitor

**Activity Monitor is not wrong.** It shows footprint, the same figure memmon
uses. The reason for this tool is not measurement. It is that Activity Monitor
shows you fifteen processes called `node`, and cannot tell you which of your ten
Claude sessions started one, whether that session finished an hour ago, or
whether killing it destroys work in progress.

- **A session is one row, not twenty.** A 6.8 GB session shows as a session with
  a name and a current task, instead of twenty anonymous rows to sum in your
  head.
- **Subagents have no pid.** They run inside the parent process, so no process
  monitor can show them. memmon reads their transcripts instead.
- **An orphan looks identical to live work.** A build whose parent shell died is
  reparented to launchd and will never be cleaned up. Only walking the process
  tree separates "3.5 GB nobody will ever reclaim" from "3.5 GB doing your work".
- **An idle prewarm looks identical to a working session.** They differ by
  whether a socket file still exists. Getting that backwards would kill live
  sessions (see the safety rule below).

And **Activity Monitor is passive.** It cannot tell the session that is about to
launch a second 20 GB typecheck that the first one is still running, which is
the moment that decides whether the machine survives.

Activity Monitor tells you *what* is using memory. memmon tells you *who*, and
can reach them. For a plain always-on RAM/swap readout in the menu bar,
[Stats](https://github.com/exelban/stats) is free, mature and better at that
job. The two are not in competition.

## How session attribution works

| Source | Gives us |
|---|---|
| `~/.claude/jobs/<id>/state.json` | session name, live detail, state, cwd |
| `lsof -U` → `/tmp/cc-daemon-*/rv/<job>.sock` | **pid → job id**, the primary link |
| `--session-id` in the command line | fallback, for terminal sessions |
| `~/.claude/projects/<key>/<sid>.jsonl` | cwd + last prompt for terminal sessions |
| `.../<sid>/subagents/*.meta.json` | agent type, task, model per subagent |
| every `Bash` tool_use in a transcript | which session started Docker, a dev server, … |
| walking the pid tree | every child, including codex → pnpm → turbo → tsc |
| `ppid == 1` on a build process | an orphan nothing will ever reap |

**Why `lsof` and not the command line.** A session claimed from Claude's prewarm
pool keeps the pool's `claude bg-spare …` command line forever and never gains a
`--session-id`. Six of seven live sessions were invisible to command-line
parsing. The daemon's rendezvous socket is the only reliable link.

**The safety rule.** An idle prewarm advertises itself on a `.claim.sock`. When a
session claims it, the socket is deleted but *the command line does not change*.
Socket present → genuinely idle and reclaimable. Socket gone → a live session.
Before this check the pool looked like "14 idle prewarms holding 2.7 GB" when it
was really 2 idle prewarms holding 186 MB plus six working sessions, one of them
22 hours old. `--reap-spares` excludes claimed sessions unconditionally.

## What it changes on its own

Most of memmon is read-only. Four things are not: it updates them without being
asked.

| What it changes | When | Effect on you | How to see it | How to stop it |
|---|---|---|---|---|
| **The list of heavy commands** | Sampler, 1/min | A command it has seen cost >1.5 GB twice starts being checked, so it may begin warning about something it used to ignore | `memmon --profile` | delete `~/.claude/memmon/profile.json` |
| **A generated shell pattern file** | Whenever the list changes | Learned commands reach the memory check (~70 ms) instead of exiting on the fast path (~6 ms) | `cat ~/.claude/memmon/learned.zsh` | same as above |
| **Forgetting** | Sampler, 1/min | A shape unseen for 30 days is dropped, so a retired script stops being checked | `memmon --profile` | — |
| **Log trimming** | Sampler, 1/min | History keeps 7 days once it passes 12 MB; the gate log keeps 500 entries | `ls -la ~/.claude/memmon/` | — |

Nothing else is autonomous. memmon never stops a process on its own, never
refuses a command outside the gate's blocking levels, and never changes a
setting unless you ask. The one opt-in exception is auto-cancel: a job you
started with `memmon run --interruptible`, with auto-cancel turned on, is
cancelled after 10 s at CRITICAL.

**What learning does to your day.** The gate starts out knowing only a built-in
list of JS build tools. Over the first few days it adds whatever *your* machine
shows to be expensive: a wrapper script, a task runner, a language toolchain the
built-in list never heard of. So a command that ran silently last week may start
producing an advisory this week. That is the feature working. It only ever
*adds*: a command the built-in list already catches can never be demoted by
learning, because under-matching disables the gate with no symptom while
over-matching costs one process start. Learned rules only ever warn.

## Giving this to someone else

The repository is public, so anyone can clone it. Share the URL and the revision
you reviewed, and tell them what they are agreeing to: with all flags, memmon
adds a hook that sees every Bash command in every Claude session, two
LaunchAgents, a CLI on `PATH`, and its own state in `~/.claude/memmon/` (see
[What changes outside the repo](#what-changes-outside-the-repo)). Each person
installs on their own Mac; there is no hosted service and no cross-machine
queue.

A gentle start is `./install.sh --sampler --menubar`: the monitor with no
interception at all. Add `--gate` once they trust it.

If their machine is not like yours:

- **Not a JS monorepo.** The built-in list of heavy commands is pnpm/turbo/tsc
  shaped. Their machine learns its own (see `memmon --profile`), but the first
  day leans on the built-in list.
- **Different project layout.** Worktree attribution assumes directories named
  `monorepo-*`. Set `project_roots` in `~/.claude/memmon/config.json` (see
  [Configuration](#configuration)) and no naming convention is needed.
- **Much more RAM.** The pressure thresholds were tuned on 16 GB. On 64 GB they
  fire late rather than early. This is a real limitation, not a setting.
- **No Claude Code.** The memory monitor works; session attribution and the
  gate do nothing.

## Working out whether you need a bigger machine

Once the sampler has a week of real working days, the history can answer "is
16 GB enough for how I work" with measurements rather than impressions.

[`docs/MEMORY-CASE-PROMPT.md`](MEMORY-CASE-PROMPT.md) is a prompt to hand an
agent that does exactly that against your own data. It matters mainly for what
it tells the agent *not* to trust: the sampler can double-record minutes, some
samples are partial reads taken while the machine was too busy to answer, the
kernel's swap counters reset on reboot and occasionally report impossible
values, and the gate log only retains a day or two. It also starts by checking
whether the machine can be upgraded at all, which on Apple Silicon it cannot.

## Configuration

Optional, at `~/.claude/memmon/config.json`:

```json
{
  "project_roots": ["~/Desktop/Work", "~/code"],
  "worktree_pattern": "monorepo(?:-([A-Za-z0-9._-]+))?",
  "ticket_pattern": "[A-Z]{2,6}-\\d+",
  "headroom_frac": 0.20,
  "pressure_suggestions": true
}
```

`project_roots` is the portable option: the child directory of a root becomes
the worktree name, with no naming convention needed. The regexes are the
fallback. `headroom_frac` sets `memmon run`'s admission limit, RAM × (1 −
headroom_frac). `pressure_suggestions: false` turns off the *Under pressure*
list, its notifications and the gate's naming of the top job. `memmon settings
set` writes `gate_mode`, `pressure_suggestions` and `notifications` here for
you.

## Storage

Everything lives in `~/.claude/memmon/`. Nothing is written to `/tmp`.

| File | Bound |
|---|---|
| `history.jsonl` | trims to the last 7 days once past 12 MB (~1 MB/day) |
| `gate.jsonl` | last 500 entries past 256 KB |
| `latest.json`, `blocked.json` | fixed / last 50, entries listed for 2 hours |
| `sampler.err` | last 200 lines past 1 MB |
| `owners-history.json` | 60 samples × 200 owners, under ~600 KB |
| `cpu-baseline.json` | one sample of up to 4,096 processes |
| `pressure.json` | the sampler's last reading, replaced atomically each run |
| `job-history.json` | 60 points × 64 heavy job roots |
| `config.json` | your settings |
| `runner/coord/actions.lock` | empty; the lock that serialises every stop |
| `runner/coord/pressure-episode.json` | the notification dedupe state |
| `runner/coord/usage-cache.json` | the Last 7 days cache, rebuilt when its inputs change |

Worst case ~13 MB, self-limiting. Trimming is by row count, not age: an age
cutoff further out than the size gate removes nothing, so the file gets
rewritten every minute and never shrinks. That bug shipped once.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Sessions show as unnamed or missing | `~/.claude/jobs` is absent, or Claude Code is not running |
| `swiftc not found` | Xcode Command Line Tools are missing: `xcode-select --install` |
| Two menu-bar icons | the old instance is still exiting; it resolves, re-run `./install.sh --menubar` if not |
| The menu bar looks like an old version | it was not rebuilt: re-run the installer with `--menubar` |
| `--report` or Last 7 days says no history | `--sampler` is not installed, or has not run long enough |
| The gate seems inert | check `memmon --gate-log`; `memmon --off`, `gate_mode off` or `MEMMON_GATE=off` disables it |
| A command was refused | `memmon --blocked` lists it; retry once memory is lower, or `memmon --off` |
| *Ask Claude* says Claude was not found | install the Claude Code CLI, or put `claude` on `PATH` |
| Every stop is refused | memmon fell back to the `ps`/`top` inventory; check `MEMMON_INVENTORY` |
