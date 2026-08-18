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
>    flatter it, quote the conservative numbers and say why. Do the same for part-days
>    at the start and end of the window: state which direction excluding them moves
>    the result.
> 3. **Check whether `ram_used` is quantised.** Until 2026-08-18 memmon read the "used"
>    figure straight from `top`, which prints whole gigabytes and truncates — a machine
>    sitting at 15.90 GiB printed `15G`, understating by 0.9 GiB on every sample, always
>    downward. The tool now derives it from `ram_total - unused`. If your history
>    predates the fix, check whether `ram_used` is ever a non-whole-GB value; if it never
>    is, measure today's offset (`top -l 1 -n 0 | grep PhysMem`, comparing the truncated
>    "used" against `ram_total` minus the megabyte-precise "unused") and present a
>    floor-corrected column beside the raw one. Argue from the corrected column, and say
>    the correction is a lower bound on the error rather than an exact restatement.
> 4. **Do not trust `swapins`/`swapouts` for cumulative volume.** They reset on reboot
>    and can record physically impossible jumps (hundreds of GB in a minute). Use them
>    only as a per-minute rate, discard negative deltas and anything above ~3 GB/s, and
>    never publish a lifetime total from them.
> 5. **Deduplicate retry storms in `gate.jsonl`.** The same command blocked repeatedly
>    within minutes is one event. Collapse repeats of the same command inside a 10-minute
>    window, and report raw rows and distinct events side by side.
> 6. **Identify automated retry loops by signature, not by asking.** A cluster with
>    sub-second-to-2-second intervals, an identical command, and a missing or identical
>    session id is a loop, not a person being stopped that many times. Say so explicitly
>    — on this machine 3 commands accounted for 100 of 105 raw blocks.
> 7. **Respect `gate.jsonl`'s retention.** It is capped at ~500 rows, often a day or two.
>    Never present a stop count as covering the same window as `history.jsonl`.
>
> ### Honesty rules — these exist because each one was got wrong first time
>
> 8. **Report the median day as well as the mean day.** Degraded time is bursty. Here the
>    mean was 54 min/day while the median day was 39, and the two worst days out of nine
>    held 54% of all degraded time — one day had a single degraded minute. Quoting only
>    the mean with the words "every day" implies a consistency the data does not show.
>    Give mean, median, best day, worst day, and the share held by the top two days.
> 9. **Split WATCH from DANGER and CRITICAL.** WATCH is the mildest tier and usually
>    dominates — it was 64% here. A single "degraded" figure leans on the weakest
>    evidence. Report the combined figure and the DANGER+CRITICAL-only figure side by side.
> 10. **Do not annualise from a short window.** Refused-command counts especially: a
>     handful of events over one day of gate log does not support a per-year figure.
>     State the observed count and the exact window it came from.
> 11. **Per-process attribution double-counts shared memory.** Summed `sessions` + `apps`
>     can exceed total demand — it did in ~1% of minutes here, worst case 50.5 GiB
>     attributed against 30.3 GiB actually in use. Build worktrees also overlap the
>     sessions that launched them. Use attribution for proportions and direction, never
>     as a precise counterfactual, and label any "what if X were not running" approximate.
> 12. **State both directions on the demand estimate, and test one of them.** Demand =
>     `ram_used + swap_used` is not exactly "RAM required": compression means it
>     understates the need, while swap holding allocated-then-abandoned pages means it
>     overstates it. There is an empirical check for the second — compare overnight
>     demand against working-hours demand. If off-hours demand is about the same as
>     working hours while degraded time is near zero, a large share of that swap is
>     abandoned rather than live, because a machine genuinely needing that working set
>     at 3am would be thrashing at 3am. Report both directions and say neither has been
>     netted out.
>
> ### Compute, using only usable samples
>
> - Demand per minute = `ram_used + swap_used`. Median, p95, p99, peak, and the share of
>   minutes over installed RAM — **split into working hours, off-hours, and all minutes**,
>   because a machine left running 24/7 has its headline percentage inflated by idle
>   overnight swap. Lead with the working-hours row. Confirm whether the peak came from a
>   usable sample or a discarded one.
> - Degraded time: minutes at WATCH+DANGER+CRITICAL and at DANGER+CRITICAL, per day and
>   per 5-day week, with the per-day distribution required by rule 8.
> - Episodes: contiguous runs at DANGER-or-worse and at CRITICAL — count, median length,
>   longest, and start times by hour. Derive the work/off-hours boundary from where
>   episodes actually fall rather than assuming 9-to-5, and say so. A quiet overnight only
>   means nothing heavy is scheduled then, not that overnight is safe.
> - **Concurrent session count**, median and peak, healthy minutes versus degraded
>   minutes. Memory is the symptom; how many sessions are open at once is the lever, and
>   it is the one number the reader can act on today.
> - From `gate.jsonl`: allow/warn/block as raw rows and distinct events, the warn-to-block
>   ratio, which kinds of command trip it most, and whether any command was blocked more
>   than once.
> - Attribution: median degraded minute versus median healthy minute, memory held by
>   coding sessions versus other applications — subject to rule 11.
> - Sizing: share of working-hour minutes that would fit in 16 / 24 / 32 / 48 / 64 GB, as
>   measured and floor-corrected. Recommend against the corrected p99, not the median, and
>   note that on soldered memory the size chosen at purchase is permanent.
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

A 16 GB M1 iMac (iMac21,1), nine recorded working days, ~6,000 usable minutes after
discarding partial reads. Two independent runs of this prompt against the same data
agreed on every solid figure and differed only where the filter was drawn.

| Measure | Value | Confidence |
|---|---|---|
| Upgrade path | none — memory is on the M1 package, 16 GB was the factory maximum | the finding that mattered most |
| Minutes over installed RAM | 87% all minutes · 82% working hours · 95% off-hours | solid |
| Demand: median / p95 / p99 / peak | 18.5 / 21.8 / 24.5 / 31.6 GiB | conservative — RAM term was truncated |
| HEALTHY minutes already over RAM | 86% | solid |
| Degraded, all tiers | mean 54 min/day, median day 39 | mean skewed; 2 of 9 days hold 54% |
| Degraded, DANGER+CRITICAL only | 19 min/day · 1.6 h/week | solid, and the stronger claim |
| Commands refused | 8 distinct events from 105 raw rows | 26 h of log — do not annualise |
| Concurrent sessions | median 6, peak 11 | the actionable lever |
| Fits in 16 / 24 / 32 GB | 15% / 96.5% / ~100% (floor-corrected) | recommend against the corrected p99 |

Your numbers will differ, and that is the point — the method transfers, the result does
not. Two findings from these runs were worth more than any single percentage: the machine
could not be upgraded at all, which changed the request from "more RAM" to "a different
machine"; and `top` was under-reporting memory by 0.9 GiB on every sample, which is now
fixed in the tool but still present in any history recorded before 2026-08-18.
