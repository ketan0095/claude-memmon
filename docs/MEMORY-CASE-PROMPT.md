# Prompt: build a hardware case from your own memmon data

Paste the block below into Claude Code (or any agent with shell access) on the machine
you want to assess. It has memmon's data layout and, more importantly, the traps that
data contains — every one of them produced a wrong or overstated number on the first
pass here before it was caught.

Run it after memmon's sampler has been collecting for at least a few days. A week or
more of real working days makes the percentiles meaningful.

---

## The prompt

> I have `memmon` installed and its sampler has been recording. I want an honest,
> defensible assessment of whether this machine has enough memory for how I actually
> work, and if not, what size would fix it. Treat me as someone who will have to defend
> every number to a skeptic — an overstated figure is worse than no figure. Where a
> number is soft, say so next to it rather than in a footnote.
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
> plainly if that is the case. On Intel Macs and PCs, check the actual slots and maximum.
>
> ### Data hygiene — all of these, they are not optional
>
> 1. **Collapse to one sample per wall-clock minute** (`ts // 60`). Duplicate launchd
>    agents can double-sample, which silently inflates any "minutes" count.
> 2. **Discard partial samples.** A sample is unusable if it has no `sessions` and no
>    `apps`, or `ram_used` below ~3 GB, or a null `pressure`. These are reads taken
>    while the machine was too busy to answer. Report what percentage you discarded,
>    and check whether including them would strengthen or weaken the case — if they
>    flatter it, quote the conservative numbers and say why.
> 3. **Do not trust `swapins`/`swapouts` for cumulative volume.** They reset on reboot
>    and can record physically impossible jumps (hundreds of GB in a minute). Use them
>    only as a per-minute rate, discard negative deltas and anything above ~3 GB/s, and
>    never publish a lifetime total from them.
> 4. **Deduplicate retry storms in `gate.jsonl`.** The same command blocked repeatedly
>    within minutes is one event. Collapse repeats of the same command inside a
>    10-minute window.
> 5. **Exclude self-inflicted contamination.** Benchmarking or testing the gate itself
>    shows up as a huge block count. Ask me about any suspicious cluster before counting it.
> 6. **Respect `gate.jsonl`'s retention.** It is capped at ~500 rows, often a day or two.
>    Never present a stop count as covering the same window as `history.jsonl`.
>
> ### Honesty rules — these exist because each one was got wrong first time
>
> 7. **Report the median day as well as the mean day.** Degraded time is bursty. Here
>    the mean was 50 min/day while the median day was 32, and the two worst days out of
>    nine held 56% of all degraded time — one day had none at all. Quoting only the mean
>    with the words "every day" implies a consistency the data does not show. Give me
>    mean, median, best day, worst day, and what share the top two days hold.
> 8. **Split WATCH from DANGER and CRITICAL.** WATCH is the mildest tier and usually
>    dominates the total — it was 63% here. A single "degraded" figure leans on the
>    weakest evidence. Report the combined figure and the DANGER+CRITICAL-only figure
>    side by side.
> 9. **Do not annualise from a short window.** Refused-command counts especially: a
>    handful of events over one day of gate log does not support a per-year figure.
>    State the observed count and the exact window it came from. Annualise only from
>    `history.jsonl`-scale evidence, and label it an extrapolation when you do.
> 10. **Per-process attribution double-counts shared memory.** Summed `sessions` +
>     `apps` can exceed total demand — it did in 1% of minutes here, worst case 50.5 GB
>     attributed against 30.3 GB actually in use. Use attribution for proportions and
>     direction, never as a precise counterfactual. If you compute "what if X were not
>     running", label it approximate.
> 11. **State both directions on the demand estimate.** Demand = `ram_used + swap_used`
>     is not exactly "RAM required". Compression means the figure understates the need;
>     swap holding pages that were allocated and abandoned means it overstates it. Say
>     both. Do not quote only the direction that helps.
>
> ### Compute, using only usable samples
>
> - Demand per minute = `ram_used + swap_used`. Median, p95, p99, peak, and the
>   percentage of minutes where demand exceeded installed RAM. Confirm whether the peak
>   came from a usable sample or a discarded one.
> - Degraded time: minutes at WATCH+DANGER+CRITICAL and at DANGER+CRITICAL, per day and
>   per 5-day week, with the per-day distribution required by rule 7.
> - Episodes: contiguous runs at DANGER-or-worse and at CRITICAL, with start times, split
>   by hour of day. Derive the work/overnight boundary from when the machine is actually
>   busy (load, session count), not an assumed 9-to-5. Note that a quiet overnight may
>   only mean nothing heavy is scheduled then, not that overnight is safe.
> - From `gate.jsonl`: allow/warn/block counts, the warn-to-block ratio, which kinds of
>   command trip it most, and whether any command was blocked more than once.
> - Attribution: for the median degraded minute versus the median healthy minute, memory
>   held by coding sessions versus other applications — subject to rule 10.
> - Sizing: percentage of working minutes that would fit in 16 / 24 / 32 / 48 / 64 GB.
>
> **Find me one worked example.** The longest continuous stretch where every usable
> minute was degraded. Date, times, what was running, what memory and load did across it.
> An abstract weekly average persuades nobody; half an hour I can point at does.
>
> **Also check these, because they are counter-intuitive:**
>
> - What fraction of minutes graded HEALTHY were already over installed RAM. HEALTHY
>   means "not actively thrashing", not "fits in memory".
> - Compression: `vm_stat`, comparing "Pages stored in compressor" against "Pages
>   occupied by compressor". Report it as a single point-in-time reading, not an average,
>   and note that a very high ratio often means cheap zero-filled pages rather than
>   hidden working set.
>
> **Limits to state plainly in the output:**
>
> - Degraded time is drag, not stoppage — time working on a machine that is paging, not
>   time sitting idle. Never present it as lost hours.
> - The measurement covers the machine, not me. It cannot tell whether I was at the
>   keyboard in a given minute. The unambiguous number is the count of refused commands.
> - How many days of data this is, and whether that period was typical or unusually busy.
>
> **Deliverable:** a single page for someone who controls the budget — the numbers, the
> worked example, the sizing table, and a section listing the data's limitations before
> anyone else finds them. No persuasion scripting. Numbers and validation only.

---

## What it produced here, for comparison

A 16 GB M1 iMac, nine recorded working days, 5,947 usable minutes after discarding
4.5% as partial reads.

| Measure | Value | Confidence |
|---|---|---|
| Minutes over installed RAM | 87% | solid — direct measurement |
| Demand: median / p95 / p99 / peak | 18.5 / 21.7 / 24.5 / 31.6 GB | solid |
| HEALTHY minutes already over RAM | 86% | solid |
| Fits in 16 / 24 / 32 GB | 13% / 98.8% / 100% | solid for the window measured |
| Degraded time, all tiers | mean 50 min/day, median 32 | mean skewed by 2 of 9 days |
| Degraded time, DANGER+CRITICAL only | 19 min/day · 1.6 h/week | solid, and the stronger claim |
| Commands refused | 5 events in 26 hours of gate log | too small a window to annualise |

Your numbers will differ, and that is the point — the method transfers, the result does
not. The single most valuable output here was the first check: the machine could not be
upgraded at all, which changed the request from "more RAM" to "a different machine".
