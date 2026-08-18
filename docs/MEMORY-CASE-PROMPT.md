# Prompt: build a hardware case from your own memmon data

Paste the block below into Claude Code (or any agent with shell access) on the machine
you want to assess. It has memmon's data layout and, more importantly, the traps that
data contains — every one of them produced a wrong number on the first pass here before
it was caught.

Run it after memmon's sampler has been collecting for at least a few days. A week or
more of real working days makes the percentiles meaningful.

---

## The prompt

> I have `memmon` installed and its sampler has been recording. I want an honest,
> defensible assessment of whether this machine has enough memory for how I actually
> work, and if not, what size would fix it. Treat me as someone who will have to defend
> every number to a skeptic — an overstated figure is worse than no figure.
>
> **Data lives in `~/.claude/memmon/`:**
> - `history.jsonl` — one sample per minute. Fields: `ts`, `ram_used`, `swap_used`,
>   `swap_total`, `free_pct`, `load`, `swapins`, `swapouts`, `pressure`
>   (HEALTHY/WATCH/DANGER/CRITICAL), plus `sessions`, `apps`, `worktrees` as
>   name → bytes maps.
> - `gate.jsonl` — one row per checked command: `ts`, `cmd`, `action`
>   (allow/warn/block), `level`, `session`, `reasons`.
>
> **Start here, before any analysis.** Establish whether this machine's memory can be
> upgraded at all: `system_profiler SPHardwareDataType`. On Apple Silicon, memory is on
> the processor package and cannot be changed — the answer is a different machine, not
> an upgrade, and the model's maximum factory configuration is the real ceiling. Say so
> plainly if that is the case. On Intel Macs and PCs, check the actual slot/maximum.
>
> **Data hygiene — apply all of these, they are not optional:**
>
> 1. **Collapse to one sample per wall-clock minute** (`ts // 60`). Duplicate launchd
>    agents can double-sample, which silently inflates any "minutes" count.
> 2. **Discard partial samples.** A sample is unusable if it has no `sessions` and no
>    `apps`, or `ram_used` below ~3 GB, or a null `pressure`. These are reads taken
>    while the machine was too busy to report. Tell me what percentage you discarded,
>    and confirm whether including them would make the case stronger or weaker — if
>    they flatter the case, quote the conservative numbers.
> 3. **Do not trust `swapins`/`swapouts` for cumulative volume.** They reset on reboot
>    and can record physically impossible jumps (hundreds of GB in one minute). Only
>    use them as a per-minute rate, discard negative deltas and anything faster than
>    the SSD can actually go (~3 GB/s), and never publish a lifetime total from them.
> 4. **Deduplicate retry storms in `gate.jsonl`.** The same command blocked repeatedly
>    within minutes is one event, not many. Collapse repeats of the same command within
>    a 10-minute window.
> 5. **Exclude self-inflicted contamination.** If I have been benchmarking or testing
>    the gate itself, those runs appear as huge block counts. Ask me about any suspicious
>    cluster before counting it.
> 6. **Respect `gate.jsonl`'s retention.** It is capped at ~500 rows, often only a day
>    or two. Never present a stop count as if it covered the whole `history.jsonl`
>    window. State the gate window separately.
>
> **Compute, using only usable samples:**
>
> - Demand per minute = `ram_used + swap_used`. Report median, p95, p99 and peak, and
>   the percentage of minutes where demand exceeded installed RAM.
> - Degraded time: minutes at WATCH + DANGER + CRITICAL, as minutes/day and hours per
>   5-day week. Then convert to human scale — working days and working weeks per year.
> - Episodes: contiguous runs at DANGER-or-worse and at CRITICAL, with their start
>   times. Split by hour of day so I can see whether this is a working-hours problem
>   or a round-the-clock one. Derive the work/overnight boundary from when the machine
>   is actually busy (load and session count), not from an assumed 9-to-5.
> - From `gate.jsonl`: allow/warn/block counts, the warn-to-block ratio, which kinds of
>   command trip it most, and whether any command was blocked more than once.
> - Attribution: for the median degraded minute vs the median healthy minute, how much
>   memory was held by coding sessions versus other applications. Then the
>   counterfactual — what percentage of minutes would fit in installed RAM with each
>   side removed. This is the question a skeptic asks first.
> - Sizing: percentage of working minutes that would fit in 16 / 24 / 32 / 48 / 64 GB.
>
> **Find me one worked example.** The longest continuous stretch where every usable
> minute was degraded. Give me the date, the times, what was running, and what memory
> and load did across it. An abstract weekly average persuades nobody; half an hour I
> can point at does.
>
> **Things to check and tell me about, because they are counter-intuitive:**
>
> - What fraction of minutes graded HEALTHY were *already* over installed RAM. HEALTHY
>   means "not actively thrashing", not "fits in memory", and the gap is usually large.
> - Whether the measured peak comes from a usable sample or a discarded one.
> - Compression: run `vm_stat` and compare "Pages stored in compressor" against "Pages
>   occupied by compressor". That ratio shows how much data is only fitting because
>   macOS is compressing it, which means measured demand is a floor, not a ceiling.
>
> **Be honest about these limits in whatever you produce:**
>
> - Degraded time is drag, not stoppage. It is time spent working on a machine that is
>   paging, not time sitting idle. Do not present it as lost hours.
> - The measurement covers the machine, not me. It cannot tell whether I was at the
>   keyboard for a given minute. The hard number is the count of refused commands.
> - Say how many days of data this is. A few days establishes a pattern, not a quarter.
>
> **Deliverable:** a single page I can send to someone who controls the budget —
> the numbers, the worked example, the sizing table, and a section listing the
> limitations of the data before anyone else finds them. No persuasion scripting, no
> advice on what to say. Numbers and validation only.

---

## What it produced here, for comparison

On a 16 GB M1 iMac across nine recorded working days (5,947 usable minutes after
discarding 4.5% as partial):

| Measure | Value |
|---|---|
| Minutes over installed RAM | 87% |
| Demand: median / p95 / p99 / peak | 18.5 / 21.7 / 24.5 / 31.6 GB |
| Degraded time | 51 min/day · 4.2 h/week · ~5 working weeks a year |
| Commands refused | 3–5 per day, all during working hours |
| HEALTHY minutes already over RAM | 86% |
| Fits in 16 / 24 / 32 GB | 13% / 98.8% / 100% |

Your numbers will differ. The point is the method, not the result — and the most
valuable output was the first check: the machine could not be upgraded at all, which
changed the request from "more RAM" to "a different machine".
