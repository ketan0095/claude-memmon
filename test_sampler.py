#!/usr/bin/env python3
"""Sampler liveness and gap honesty (spec S2.10): in-run rates, the free-delta
floor, gap causes, the 40 s run budget, UNKNOWN for every reader and the
sampler plist. Clocks, the boot session, the strict reads and notifications
are all injected: nothing here sleeps the machine, induces real pressure or
posts a real notification."""

from __future__ import annotations

import contextlib
import io
import json
import linecache
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import memmon
import memmon_telemetry
from testkit import TempState

HERE = os.path.dirname(os.path.abspath(__file__))
GB = 1 << 30
BOOT = "11111111-2222-3333-4444-555555555555"
PAGE = 16384


class SamplerState:
    """TempState, a clean in-process baseline and a recording notifier."""

    def __init__(self, tc):
        self.ts = TempState()
        tc.addCleanup(self.ts.close)
        r = self.ts.root
        for name in ("_prev_vm", "_free_base", "_last_rates"):
            p = mock.patch.object(memmon, name, {})
            p.start()
            tc.addCleanup(p.stop)
        self.notes = []
        p = mock.patch.object(memmon, "notify",
                              lambda text, title="memmon", subtitle="", **kw:
                              self.notes.append((text, title, subtitle)))
        p.start()
        tc.addCleanup(p.stop)
        self.root = r

    def write(self, path, row):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(row, fh)

    def read(self, path):
        with open(path) as fh:
            return json.load(fh)


class FakeTelemetry(memmon_telemetry.TelemetrySource):
    """Scripted kernel inputs and clocks for memmon_telemetry: both the
    source and the clock. sleep() advances every clock."""

    def __init__(self, mono=10_000.0, uptime=8_000.0, boot=BOOT, free=(40, 40),
                 swapins=(0, 0), swapouts=(0, 0), swap_used=1 * GB, ram=48 * GB,
                 load=2.0, ncpu=18, kernel=1, vm_stat_fails=False, sysctl_fails=False):
        self.m, self.u, self.boot_id = mono, uptime, boot
        self.free, self.swapins, self.swapouts = list(free), list(swapins), list(swapouts)
        self.swap_used, self.ram, self.load, self.n = swap_used, ram, load, ncpu
        self.kernel, self.vm_stat_fails, self.sysctl_fails = kernel, vm_stat_fails, sysctl_fails
        self.free_reads = self.vm_reads = 0
        self.vm_stat_timeouts = []

    # clock
    def mono(self):
        return self.m

    def awake(self):
        return self.u

    def raw(self):
        return self.m

    def uptime(self):
        return self.u

    def wall(self):
        return time.time()

    def boot(self):
        return self.boot_id

    def sleep(self, s):
        self.m += s
        self.u += s

    # source
    def pressure_level(self):
        if self.sysctl_fails:
            raise memmon_telemetry.TelemetryError("sysctl kern.memorystatus_level failed")
        return self.kernel

    def free_pct(self):
        i = min(self.free_reads, len(self.free) - 1)
        self.free_reads += 1
        return self.free[i]

    def memsize(self):
        return self.ram

    def swapusage(self):
        return 2 * self.swap_used, self.swap_used

    def loadavg(self):
        return self.load

    def ncpu(self):
        return self.n

    def vm_stat(self, timeout):
        self.vm_stat_timeouts.append(timeout)
        if self.vm_stat_fails:
            raise memmon_telemetry.TelemetryError(f"vm_stat timed out after {timeout:g}s")
        i = min(self.vm_reads, len(self.swapins) - 1)
        self.vm_reads += 1
        return (f"Mach Virtual Memory Statistics: (page size of {PAGE} bytes)\n"
                "Anonymous pages: 1000.\nPages purgeable: 0.\nPages wired down: 10.\n"
                "Pages occupied by compressor: 10.\nPageins: 0.\nPageouts: 0.\n"
                f"Swapins: {self.swapins[i]}.\nSwapouts: {self.swapouts[i]}.\n")


def prev_file(mono, free=40, streak=0, boot=BOOT, uptime=None, ts=None, **extra):
    return {"ts": ts or time.time() - 60, "mono": mono,
            "uptime": uptime if uptime is not None else mono - 2000, "boot": boot,
            "free_pct": free, "swapins": 0, "swapouts": 0, "swap_used": GB,
            "lh_streak": streak, "level": "WATCH", **extra}


class InRunRateTests(unittest.TestCase):
    def setUp(self):
        self.st = SamplerState(self)

    def test_in_run_rates_after_a_gap_report_the_paging(self):
        # B32 (a), I-13: the previous row is 21 min old, so v1 would have
        # dropped every rate and said HEALTHY. The run's own pair sees it.
        T = FakeTelemetry(swapins=(0, 20_000), swapouts=(0, 20_000))
        self.st.write(memmon.PRESSURE_FILE, prev_file(T.m - 21 * 60, ts=time.time() - 1260))
        out = memmon.sampler_reading(T, T)
        p = out["pressure"]
        self.assertEqual(p["rates_source"], "in_run")
        self.assertIn(p["level"], ("DANGER", "CRITICAL"))
        self.assertTrue(any("thrashing" in r for r in p["reasons"]), p["reasons"])
        self.assertGreater(p["swapin_mbs"], 100)
        self.assertEqual(T.vm_stat_timeouts, [10, 10])     # the sampler's own bound
        self.assertGreaterEqual(T.vm_reads, 2)

    def test_gap_adjacent_reading_never_healthy(self):
        # I-13: with no paging the run is HEALTHY only because its rates are
        # valid; take the second vm_stat away and it cannot be.
        T = FakeTelemetry()
        healthy = memmon.sampler_reading(T, T)["pressure"]
        self.assertEqual((healthy["level"], healthy["rates_source"]), ("HEALTHY", "in_run"))
        T2 = FakeTelemetry(vm_stat_fails=True)
        p = memmon.sampler_reading(T2, T2)["pressure"]
        self.assertEqual(p["level"], "UNKNOWN")
        self.assertEqual(p["rates"], "unavailable")
        self.assertIn("vm_stat timed out", p["level_reason"])

    def test_one_point_free_change_inside_the_run_has_no_runway(self):
        # B32 (b): over 2 s one free_pct tick would read as 30 %/min. The
        # previous file is 20 s old, so free_delta_min is unavailable and the
        # carried streak passes through without escalating WATCH.
        T = FakeTelemetry(free=(20, 19))                   # kernel headroom: WATCH
        self.st.write(memmon.PRESSURE_FILE, prev_file(T.m - 20, free=21, streak=1))
        p = memmon.sampler_reading(T, T)["pressure"]
        self.assertIsNone(p["free_delta_min"])
        self.assertIsNone(p["headroom_min"])
        self.assertEqual(p["lh_streak"], 1)
        self.assertEqual(p["level"], "WATCH")

    def test_free_delta_uses_the_previous_pressure_file_30_to_300_s_old(self):
        T = FakeTelemetry(free=(30, 30))
        self.st.write(memmon.PRESSURE_FILE, prev_file(T.m - 60, free=33, streak=1))
        p = memmon.sampler_reading(T, T)["pressure"]
        self.assertAlmostEqual(p["free_delta_min"], -3.0 * 60 / 62, places=3)
        for age in (12, 400):
            with self.subTest(age=age):
                T = FakeTelemetry(free=(30, 30))
                self.st.write(memmon.PRESSURE_FILE, prev_file(T.m - age, free=33))
                self.assertIsNone(memmon.sampler_reading(T, T)
                                  ["pressure"]["free_delta_min"])
        other = FakeTelemetry(free=(30, 30))
        self.st.write(memmon.PRESSURE_FILE, prev_file(other.m - 60, free=33, boot="other"))
        self.assertIsNone(memmon.sampler_reading(other, other)
                          ["pressure"]["free_delta_min"])

    def test_streak_advances_only_on_a_valid_free_delta(self):
        # Falling 1 %/min toward the 20 % floor from 24 %: headroom 4 min.
        T = FakeTelemetry(free=(24, 24), load=54.0)        # load 3x cores: WATCH
        self.st.write(memmon.PRESSURE_FILE, prev_file(T.m - 58, free=25, streak=1))
        p = memmon.sampler_reading(T, T)["pressure"]
        self.assertEqual(p["lh_streak"], 2)
        self.assertEqual(p["level"], "DANGER")
        self.assertTrue(any("headroom falling" in r for r in p["reasons"]))

    def test_one_shot_reader_three_seconds_after_a_sampler_write(self):
        # B32 (c): the baseline is valid for rates (2-300 s) but not for
        # free_delta_min (30 s), so a 1-point drop gives no runway and the
        # carried streak passes through unchanged.
        now = 5_000.0
        self.st.write(memmon.PRESSURE_FILE, prev_file(now - 3, free=21, streak=1))
        vm = {"free_pct": 20, "swap_used": GB, "ram_total": 48 * GB, "load": 1.0,
              "ncpu": 18, "swapins": 0, "swapouts": 0, "boot": BOOT,
              "kernel_level": "normal"}
        with mock.patch.object(memmon, "mono_now", lambda: now):
            p = memmon.pressure(vm)
        self.assertEqual(p["rates_source"], "baseline")
        self.assertIsNone(p["free_delta_min"])
        self.assertIsNone(p["headroom_min"])
        self.assertEqual(p["lh_streak"], 1)
        self.assertEqual(p["level"], "WATCH")

    def test_live_reader_keeps_a_separate_30_s_free_baseline(self):
        # AD-S2-2: the 2 s rate baseline advances on every call; the free_pct
        # baseline only once it is 30 s old.
        clock = [1000.0]
        vm = {"free_pct": 40, "swap_used": GB, "ram_total": 48 * GB, "load": 1.0,
              "ncpu": 18, "swapins": 0, "swapouts": 0, "boot": BOOT}
        seen = []
        with mock.patch.object(memmon, "mono_now", lambda: clock[0]):
            for i in range(10):
                vm = {**vm, "free_pct": 40 - i}
                seen.append(memmon.pressure(vm)["free_delta_min"])
                clock[0] += 4
        self.assertIsNone(seen[0])
        self.assertTrue(all(v is None for v in seen[1:8]), seen)
        self.assertAlmostEqual(seen[8], -8 * 60 / 32)

    def test_strict_read_failure_is_unknown_and_written_first(self):
        T = FakeTelemetry(sysctl_fails=True)
        out = memmon.sampler_reading(T, T)
        self.assertEqual(out["pressure"]["level"], "UNKNOWN")
        self.assertIn("strict read failed", out["pressure"]["level_reason"])
        rec = self.st.read(memmon.PRESSURE_FILE)
        self.assertEqual(rec["level"], "UNKNOWN")
        self.assertEqual(rec["rates"], "unavailable")

    def test_vm_stat_failure_keeps_an_instantaneous_lower_bound(self):
        # The sysctl signals alone say WATCH (kernel headroom 18 %): that
        # stands, with rates unavailable and the kernel level recorded.
        T = FakeTelemetry(free=(18, 18), vm_stat_fails=True, kernel=2)
        rec = memmon.sampler_reading(T, T)["record"]
        self.assertEqual(rec["level"], "WATCH")
        self.assertEqual(rec["rates"], "unavailable")
        self.assertTrue(rec["level_reason"].startswith("lower bound"))
        self.assertEqual(rec["kernel_level"], "warning")
        self.assertTrue(rec["under_pressure"])

    def test_score_parity_with_the_legacy_scorer(self):
        # B26's half that lives here: one pure scorer for every reader.
        vm = {"free_pct": 15, "swap_used": 30 * GB, "ram_total": 48 * GB,
              "load": 40, "ncpu": 18}
        rates = {"swapin_mbs": 60.0, "swapout_mbs": 10.0, "swap_growth_mbmin": 200.0,
                 "free_delta_min": None}
        p = memmon.score_pressure(vm, rates, 0, "in_run")
        self.assertEqual(p["score"], 2 + 2 + 2 + 1 + 1)
        self.assertEqual(p["level"], "CRITICAL")


class GapTests(unittest.TestCase):
    def setUp(self):
        self.st = SamplerState(self)

    def gap(self, d_mono, d_uptime, boot=BOOT):
        prev = {"ts": 1000.0, "mono": 50_000.0, "uptime": 40_000.0, "boot": BOOT}
        cur = {"ts": 1000.0 + d_mono, "mono": 50_000.0 + d_mono,
               "uptime": 40_000.0 + d_uptime, "boot": boot}
        return memmon.gap_record(prev, cur)

    def test_sampler_gap_cause(self):
        # B33
        a = self.gap(21 * 60, 21 * 60)
        self.assertEqual((a["cause"], a["asleep_s"], a["awake_s"]), ("starved", 0, 1260))
        b = self.gap(21 * 60, 60)
        self.assertEqual((b["cause"], b["awake_s"]), ("sleep", 60))
        self.assertEqual(b["asleep_s"], 1200)
        c = self.gap(21 * 60, 9 * 60)            # two sleeps totalling 12 min
        self.assertEqual((c["cause"], c["asleep_s"], c["awake_s"]), ("starved", 720, 540))
        d = self.gap(30, 30, boot="another-boot")
        self.assertEqual(d["cause"], "reboot")
        self.assertIsNone(self.gap(150, 150))
        self.assertEqual(self.gap(151, 151)["cause"], "starved")
        self.assertEqual(self.gap(151, 150)["cause"], "sleep")

    def test_gap_lands_in_pressure_file_and_row_never_backfilled(self):
        T = FakeTelemetry()
        self.st.write(memmon.PRESSURE_FILE, prev_file(T.m - 21 * 60, uptime=T.u - 21 * 60,
                                                      ts=time.time() - 1260))
        reading = memmon.sampler_reading(T, T)
        rec = self.st.read(memmon.PRESSURE_FILE)
        # The previous file is 21 min older than the run's second read (2 s on).
        self.assertEqual(rec["gap"]["cause"], "starved")
        self.assertEqual(rec["last_gap"], rec["gap"])
        snap = {"ts": time.time(), "vm": {}, "pressure": reading["pressure"],
                "orphan_total": 0, "sessions": [], "apps": {}, "worktrees": []}
        with mock.patch.object(memmon, "learn"):
            memmon.log_sample(snap, reading)
        with open(memmon.HISTORY) as fh:
            rows = [json.loads(line) for line in fh]
        self.assertEqual(len(rows), 1)                      # never back-filled
        self.assertEqual(rows[0]["gap"]["cause"], "starved")
        self.assertEqual(rows[0]["boot"], BOOT)
        self.assertEqual(rows[0]["mono"], rec["mono"])
        self.assertEqual(self.st.notes, [])      # posted by sampler_run, not log_sample

    def test_last_gap_carries_forward_until_the_next(self):
        T = FakeTelemetry()
        g = {"cause": "starved", "awake_s": 900, "asleep_s": 0, "gap_s": 900,
             "from_ts": 1.0, "to_ts": 901.0}
        self.st.write(memmon.PRESSURE_FILE, prev_file(T.m - 60, uptime=T.u - 60,
                                                      last_gap=g))
        rec = memmon.sampler_reading(T, T)["record"]
        self.assertIsNone(rec["gap"])
        self.assertEqual(rec["last_gap"], g)
        blk = memmon.sampler_block(now=rec["ts"] + 30)
        self.assertEqual(blk["last_gap"], g)
        self.assertAlmostEqual(blk["age_s"], 30, places=0)
        self.assertFalse(blk["stale"])

    def sampler_run(self, T, collect=None):
        snap = {"ts": time.time(), "vm": {}, "pressure": {}, "orphan_total": 0,
                "sessions": [], "apps": {}, "worktrees": []}
        with mock.patch.object(memmon, "collect", collect or (lambda pres: dict(
                snap, pressure=pres))), \
                mock.patch.object(memmon, "sampler_owners"), \
                mock.patch.object(memmon, "learn"), \
                mock.patch("sys.stderr", io.StringIO()):
            return memmon.sampler_run(budget_s=5.0, source=T, clock=T)

    def test_starved_gap_notice_survives_a_budget_killed_run(self):
        # SAMPLER-2: the run that discovers the gap dies in collect. The
        # notice is already out, once; the next run does not repeat it.
        T = FakeTelemetry()
        self.st.write(memmon.PRESSURE_FILE, prev_file(T.m - 21 * 60, uptime=T.u - 21 * 60,
                                                      ts=time.time() - 1260))

        def starved(pres):
            raise memmon.SamplerBudget()
        self.assertEqual(self.sampler_run(T, starved), 0)
        self.assertTrue(self.st.read(memmon.SNAPSHOT)["partial"])
        self.assertEqual(len(self.st.notes), 1)
        self.assertIn("couldn't sample for 21 min while the Mac was awake",
                      self.st.notes[0][0])
        T.sleep(60)
        self.sampler_run(T)
        self.assertEqual(len(self.st.notes), 1)

    def test_sleep_gap_and_short_starved_gap_are_not_notified(self):
        for awake in (60, 240):
            with self.subTest(awake=awake):
                self.st.notes.clear()
                T = FakeTelemetry()
                self.st.write(memmon.PRESSURE_FILE,
                              prev_file(T.m - 1200, uptime=T.u - awake))
                self.sampler_run(T)
                self.assertEqual(self.st.notes, [])

    def test_starved_gap_outlives_a_later_sleep_gap(self):
        # SAMPLER-6: the 24 h notice reads last_starved_gap, which a later
        # sleep (or reboot) gap does not replace.
        T = FakeTelemetry()
        self.st.write(memmon.PRESSURE_FILE, prev_file(T.m - 1260, uptime=T.u - 1260))
        starved = memmon.sampler_reading(T, T)["record"]["gap"]
        self.assertEqual(starved["cause"], "starved")
        T.m += 3600                                        # an hour asleep
        rec = memmon.sampler_reading(T, T)["record"]
        self.assertEqual(rec["gap"]["cause"], "sleep")
        self.assertEqual(rec["last_gap"]["cause"], "sleep")
        self.assertEqual(rec["last_starved_gap"], starved)
        self.assertEqual(memmon.sampler_block()["last_starved_gap"], starved)

    def test_report_lists_gaps_skips_partial_and_counts_unknown_unscored(self):
        now = time.time()
        rows = [{"ts": now - 300, "swap_used": GB, "pressure": "HEALTHY"},
                {"ts": now - 200, "swap_used": 3 * GB, "pressure": "DANGER",
                 "gap": {"cause": "starved", "awake_s": 1260, "asleep_s": 0,
                         "from_ts": now - 1460, "to_ts": now - 200}},
                {"ts": now - 100, "partial": True, "pressure": "UNKNOWN",
                 "partial_reason": "sampler budget exceeded"}]
        with open(memmon.HISTORY, "w") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in rows)
        text = memmon.report(1)
        self.assertIn("avg 2.0G", text)
        self.assertIn("50% of 2 scored samples (1 unscored)", text)
        self.assertIn("1 partial sample", text)
        self.assertIn("starved: 21m awake unsampled", text)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.st = SamplerState(self)

    def test_run_budget(self):
        # B35: top and lsof stall up to their own timeouts. SamplerBudget, a
        # BaseException, passes straight through _sh's `except Exception`.
        T = FakeTelemetry()
        real_run = subprocess.run
        stalled = []

        def stall(cmd, *a, **kw):
            if cmd and cmd[0] in ("top", "lsof", "ps"):
                stalled.append(cmd[0])
                time.sleep(kw.get("timeout") or 15)
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return real_run(cmd, *a, **kw)
        t0 = time.monotonic()
        with mock.patch.object(memmon.subprocess, "run", stall):
            rc = memmon.sampler_run(budget_s=1.0, source=T, clock=T)
        elapsed = time.monotonic() - t0
        self.assertEqual(rc, 0)
        self.assertLess(elapsed, 5, "the budget did not fire")
        self.assertTrue(stalled)
        rec = self.st.read(memmon.PRESSURE_FILE)          # written before collection
        self.assertEqual(rec["rates_source"], "in_run")
        row = self.st.read(memmon.SNAPSHOT)
        self.assertTrue(row["partial"])
        self.assertEqual(row["partial_reason"], "sampler budget exceeded")
        self.assertEqual(row["pressure"], rec["level"])
        self.assertEqual(row["mono"], rec["mono"])
        self.assertEqual([f for f in os.listdir(self.st.root) if f.endswith(".tmp")], [])
        with open(memmon.HISTORY) as fh:
            self.assertTrue(json.loads(fh.readlines()[-1])["partial"])
        # The next run proceeds normally.
        T.sleep(60)
        with mock.patch.object(memmon, "collect", return_value={
                "ts": time.time(), "vm": {}, "pressure": {}, "orphan_total": 0,
                "sessions": [], "apps": {}, "worktrees": []}) as col, \
                mock.patch.object(memmon, "sampler_owners"), \
                mock.patch.object(memmon, "learn"):
            self.assertEqual(memmon.sampler_run(budget_s=5.0, source=T, clock=T), 0)
        self.assertEqual(col.call_args.kwargs["pres"]["rates_source"], "in_run")
        self.assertNotIn("partial", self.st.read(memmon.SNAPSHOT))

    def test_budget_during_history_trim_keeps_history(self):
        # SAMPLER-1: the budget fires mid-trim, at the write of the kept rows.
        # The old truncate-then-write lost the file; now the old file stays,
        # the full row is in it once, and no partial row follows.
        T = FakeTelemetry()
        with open(memmon.HISTORY, "w") as fh:
            fh.writelines(json.dumps({"ts": i, "swap_used": GB}) + "\n" for i in range(50))
        here = os.path.abspath(memmon.__file__)

        def tracer(frame, event, arg):
            if frame.f_code.co_filename != here:
                return None
            if event == "line" and ".writelines(" in linecache.getline(here, frame.f_lineno) \
                    and frame.f_code.co_name in ("_trim_history", "_replace_lines"):
                sys.settrace(None)
                raise memmon.SamplerBudget()
            return tracer
        snap = {"ts": time.time(), "vm": {}, "pressure": {}, "orphan_total": 0,
                "sessions": [], "apps": {}, "worktrees": []}
        with mock.patch.object(memmon, "HISTORY_TRIM_AT", 100), \
                mock.patch.object(memmon, "HISTORY_KEEP_ROWS", 40), \
                mock.patch.object(memmon, "collect", lambda pres: dict(snap, pressure=pres)), \
                mock.patch.object(memmon, "sampler_owners"), \
                mock.patch.object(memmon, "learn"), mock.patch("sys.stderr", io.StringIO()):
            sys.settrace(tracer)
            try:
                rc = memmon.sampler_run(budget_s=5.0, source=T, clock=T)
            finally:
                sys.settrace(None)
        self.assertEqual(rc, 0)
        with open(memmon.HISTORY) as fh:
            rows = [json.loads(line) for line in fh]
        self.assertGreaterEqual(len(rows), 41)
        self.assertEqual([r for r in rows if r.get("partial")], [])
        self.assertEqual(rows[-1]["rates_source"], "in_run")
        self.assertNotIn("partial", self.st.read(memmon.SNAPSHOT))
        self.assertEqual([f for f in os.listdir(self.st.root) if f.endswith(".tmp")], [])

    def test_budget_between_append_and_row_flag(self):
        # A real SIGALRM lands right after the history append, before the
        # row flag: it must not add a partial row after the full one.
        T = FakeTelemetry()
        here = os.path.abspath(memmon.__file__)
        fired = []

        def tracer(frame, event, arg):
            if frame.f_code.co_filename != here:
                return None
            if (event == "line" and frame.f_code.co_name == "_append_row" and not fired
                    and 'reading["row"] = row' in linecache.getline(here, frame.f_lineno)):
                fired.append(1)
                os.kill(os.getpid(), signal.SIGALRM)
            return tracer
        snap = {"ts": time.time(), "vm": {}, "pressure": {}, "orphan_total": 0,
                "sessions": [], "apps": {}, "worktrees": []}
        with mock.patch.object(memmon, "collect", lambda pres: dict(snap, pressure=pres)), \
                mock.patch.object(memmon, "sampler_owners"), \
                mock.patch.object(memmon, "learn"), mock.patch("sys.stderr", io.StringIO()):
            sys.settrace(tracer)
            try:
                rc = memmon.sampler_run(budget_s=5.0, source=T, clock=T)
            finally:
                sys.settrace(None)
        self.assertEqual((rc, fired), (0, [1]))
        with open(memmon.HISTORY) as fh:
            rows = [json.loads(line) for line in fh]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("partial", rows[0])
        self.assertNotIn("partial", self.st.read(memmon.SNAPSHOT))

    def test_trim_still_trims(self):
        with open(memmon.HISTORY, "w") as fh:
            fh.writelines(json.dumps({"ts": i}) + "\n" for i in range(50))
        with mock.patch.object(memmon, "HISTORY_TRIM_AT", 100), \
                mock.patch.object(memmon, "HISTORY_KEEP_ROWS", 40):
            memmon._trim_history()
        with open(memmon.HISTORY) as fh:
            rows = [json.loads(line)["ts"] for line in fh]
        self.assertEqual(rows, list(range(10, 50)))

    def test_budget_is_not_an_exception(self):
        self.assertTrue(issubclass(memmon.SamplerBudget, BaseException))
        self.assertFalse(issubclass(memmon.SamplerBudget, Exception))


class UnknownReaderTests(unittest.TestCase):
    """B34: no valid baseline for any reader. Nothing says HEALTHY."""

    VM = {"free_pct": 40, "swap_used": GB, "swap_total": 2 * GB, "ram_total": 48 * GB,
          "load": 1.0, "ncpu": 18, "swapins": 0, "swapouts": 0, "boot": BOOT,
          "kernel_level": "normal"}

    def setUp(self):
        self.st = SamplerState(self)
        self.vm = dict(self.VM)
        p = mock.patch.object(memmon, "read_vm", lambda *a, **kw: dict(self.vm))
        p.start()
        self.addCleanup(p.stop)

    def run_main(self, *argv, stdin=None):
        out = io.StringIO()
        with mock.patch("sys.argv", ["memmon", *argv]), contextlib.redirect_stdout(out):
            if stdin is not None:
                with mock.patch("sys.stdin", io.StringIO(stdin)):
                    rc = memmon.main()
            else:
                rc = memmon.main()
        return rc, out.getvalue()

    def stale_baselines(self):
        """Missing, over 300 s old, and from another boot."""
        now = memmon.mono_now()
        yield "missing", None
        yield "old", prev_file(now - 400)
        yield "other boot", prev_file(now - 60, boot="other")

    def reset(self, row):
        memmon._prev_vm.clear(), memmon._free_base.clear(), memmon._last_rates.clear()
        for path in (memmon.PRESSURE_FILE, memmon.SNAPSHOT):
            if os.path.exists(path):
                os.remove(path)
        if row is not None:
            self.st.write(memmon.PRESSURE_FILE, row)

    def test_one_shot_without_baseline_is_unknown(self):
        for name, row in self.stale_baselines():
            with self.subTest(baseline=name):
                self.reset(row)
                p = memmon.pressure(dict(self.vm))
                self.assertEqual(p["level"], "UNKNOWN")
                self.assertEqual(p["rates"], "unavailable")
                self.assertTrue(p["level_reason"])

    def test_in_process_baseline_over_300_s_is_unknown(self):
        # A live reader whose last refresh was 400 s ago (a sleep, a stall)
        # re-seeds instead of scoring a rate over the gap.
        self.reset(None)
        clock = [5_000.0]
        with mock.patch.object(memmon, "mono_now", lambda: clock[0]):
            memmon.pressure(dict(self.vm))
            clock[0] += 3
            self.assertEqual(memmon.pressure(dict(self.vm))["level"], "HEALTHY")
            clock[0] += 400
            p = memmon.pressure(dict(self.vm))
        self.assertEqual((p["level"], p["level_reason"]), ("UNKNOWN", "baseline over 300 s old"))

    def test_no_baseline_from_defaulted_counters(self):
        # SAMPLER-4: both sampler vm_stat reads failed, so neither file holds
        # real counters. A reader with real since-boot counters must not
        # score them against zeros as thrash.
        self.reset(None)
        now = memmon.mono_now()
        self.st.write(memmon.PRESSURE_FILE, {k: v for k, v in prev_file(now - 30).items()
                                             if k not in ("swapins", "swapouts")})
        snap = {"ts": time.time(), "vm": {"free_pct": 40, "swap_used": GB},
                "pressure": {"level": "UNKNOWN", "rates": "unavailable"},
                "orphan_total": 0, "sessions": [], "apps": {}, "worktrees": []}
        reading = {"record": {"mono": now - 30, "uptime": now - 2030, "boot": BOOT}}
        row = memmon._log_row(snap, reading)
        self.assertIsNone(row["swapins"])
        self.st.write(memmon.SNAPSHOT, row)
        memmon._prev_vm.clear()
        p = memmon.pressure({**self.vm, "swapins": 90_000_000, "swapouts": 90_000_000})
        self.assertEqual(p["level"], "UNKNOWN")
        # Real counters, but the row's own rates were unavailable: refused too.
        self.reset(None)
        self.st.write(memmon.SNAPSHOT, {**prev_file(now - 30), "rates": "unavailable"})
        self.assertEqual(memmon.pressure(dict(self.vm))["level"], "UNKNOWN")

    def test_once_shows_the_unknown_reason(self):
        # SAMPLER-5
        self.reset(None)
        with mock.patch.object(memmon, "read_top", return_value=({}, "")), \
                mock.patch.object(memmon, "read_ps", return_value={}):
            text = memmon.render(memmon.collect(), on=False)
        self.assertIn("UNKNOWN", text)
        self.assertIn("no valid rate baseline", text)
        self.assertNotIn("no pressure signals", text)
        self.assertNotIn("→ WATCH", text)
        self.assertNotIn("Scope new work", text)

    def test_pressure_flag_prints_unknown_and_exits_0(self):
        self.reset(None)
        rc, out = self.run_main("--pressure")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("UNKNOWN"), out)
        self.assertIn("no valid rate baseline", out)

    def test_watch_lower_bound_stands(self):
        self.reset(None)
        self.vm["free_pct"] = 18
        p = memmon.pressure(dict(self.vm))
        self.assertEqual(p["level"], "WATCH")
        self.assertEqual(p["rates"], "unavailable")
        rc, out = self.run_main("--pressure")
        self.assertEqual(rc, 0)
        self.assertIn("lower bound", out)

    def test_gate_allows_logs_unknown_injects_nothing(self):
        self.reset(None)
        payload = {"tool_name": "Bash", "tool_input": {"command": "pnpm typecheck"},
                   "session_id": "s1", "cwd": "/tmp"}
        with mock.patch.dict(os.environ, {"MEMMON_GATE": "block"}):
            rc, out = self.run_main("--gate", stdin=json.dumps(payload))
        self.assertEqual((rc, out), (0, ""))
        with open(memmon.GATE_LOG) as fh:
            row = json.loads(fh.readlines()[-1])
        self.assertEqual((row["level"], row["action"]), ("UNKNOWN", "allow"))
        self.assertEqual(row["level_reason"], "no valid rate baseline")

    def test_blocked_shows_unknown(self):
        self.reset(None)
        memmon.save_pending([{"ts": time.time(), "session_id": "s", "session": "Checkout",
                              "cmd": "pnpm test", "cwd": "", "level": "CRITICAL"}])
        rc, out = self.run_main("--blocked")
        self.assertEqual(rc, 0)
        self.assertIn("current pressure: UNKNOWN", out)
        self.assertNotIn("safe to re-run", out)

    def test_once_and_json_show_unknown(self):
        self.reset(None)
        with mock.patch.object(memmon, "read_top", return_value=({}, "")), \
                mock.patch.object(memmon, "read_ps", return_value={}):
            snap = memmon.collect()
        self.assertEqual(snap["pressure"]["level"], "UNKNOWN")
        self.assertIn("UNKNOWN", memmon.render(snap, on=False))
        self.assertNotIn("HEALTHY", memmon.render(snap, on=False))

    def test_owners_system_block_is_unknown(self):
        self.reset(None)
        blk = memmon.system_block(reader=lambda: {"ram_bytes": 48 * GB,
                                                  "used_bytes": 20 * GB,
                                                  "pressure_level": "normal"})
        self.assertEqual(blk["score_level"], "UNKNOWN")
        self.assertEqual(blk["rates"], "unavailable")
        self.assertEqual(blk["level_reason"], "no valid rate baseline")

    def test_status_line(self):
        now = time.time()
        row = {"ts": now - 30, "pressure": "UNKNOWN", "swap_used": GB, "orphan": 0}
        self.assertEqual(memmon.cached_statusline(row, now), "memmon: pressure unknown")
        row = {"ts": now - 600, "pressure": "HEALTHY", "swap_used": GB, "orphan": 0}
        self.assertEqual(memmon.cached_statusline(row, now), "memmon: no sample for 10 min")
        self.reset(None)
        self.st.write(memmon.SNAPSHOT, {"ts": now - 20, "pressure": "UNKNOWN"})
        rc, out = self.run_main("--statusline")
        self.assertEqual(out.strip(), "memmon: pressure unknown")

    def test_wait_safe_keeps_waiting(self):
        self.reset(None)
        clock = [1000.0]
        with mock.patch.object(memmon.time, "time", lambda: clock[0]), \
                mock.patch.object(memmon.time, "sleep",
                                  lambda s: clock.__setitem__(0, clock[0] + s)), \
                mock.patch.object(memmon, "pressure", lambda vm: memmon.score_pressure(
                    vm, None, 0, None, "no valid rate baseline")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = memmon.wait_safe(30)
        self.assertEqual(rc, 1)
        self.assertNotIn("clear:", out.getvalue())
        self.assertIn("UNKNOWN — no valid rate baseline", out.getvalue())


class NotifyTests(unittest.TestCase):
    def test_osascript_gets_text_only_as_argv(self):
        calls = []
        text = 'vitest in acme-web" & (do shell script "id") & "'
        memmon.notify(text, "memmon", "sub", run=lambda argv, **kw: calls.append(argv))
        argv = calls[0]
        self.assertEqual(argv[0], "osascript")
        scripts = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        self.assertTrue(all(text not in s and "acme" not in s for s in scripts))
        self.assertEqual(argv[-3:], [text, "memmon", "sub"])
        self.assertIn("on run argv", scripts)

    def test_cleared_notification_also_goes_through_argv(self):
        st = SamplerState(self)
        st.write(memmon.SNAPSHOT, {"ts": time.time() - 60, "pressure": "DANGER"})
        memmon.save_pending([{"ts": time.time(), "session_id": "s", "session": "x",
                              "cmd": "pnpm test", "cwd": "", "level": "DANGER"}])
        snap = {"ts": time.time(), "vm": {}, "pressure": {"level": "HEALTHY"},
                "orphan_total": 0, "sessions": [], "apps": {}, "worktrees": []}
        with mock.patch.object(memmon, "learn"):
            memmon.log_sample(snap)
        self.assertEqual(st.notes, [("1 blocked command(s) can be retried", "memmon",
                                     "Memory pressure cleared")])


class DashboardRowTests(unittest.TestCase):
    def test_dashboard_row_keeps_the_samplers_suggestions(self):
        # PRES-2: the live dashboard writes latest.json between sampler runs;
        # the gate must still name the sampler's top suggestion.
        st = SamplerState(self)
        now = time.time()
        top = {"label": "vitest in acme-web", "footprint": 9 * GB, "job_id": "1.2.3"}
        st.write(memmon.SNAPSHOT, {"ts": now - 30, "pressure": "DANGER", "under_pressure": True,
                                   "pressure_suggestions": [top], "suggestions_ts": now - 30})
        snap = {"ts": now, "vm": {}, "pressure": {"level": "DANGER", "rates": "ok"},
                "orphan_total": 0, "sessions": [], "apps": {}, "worktrees": []}
        with mock.patch.object(memmon, "learn"):
            memmon.log_sample(snap)
        row = st.read(memmon.SNAPSHOT)
        self.assertEqual(row["pressure_suggestions"], [top])
        self.assertTrue(row["under_pressure"])
        self.assertEqual(memmon.top_suggestion(row, now + 1), top)
        # Carried suggestions still age out on the sampler's clock.
        self.assertIsNone(memmon.top_suggestion(row, now + 200))


class AtomicWriteTests(unittest.TestCase):
    def test_latest_json_is_replaced_atomically(self):
        st = SamplerState(self)
        snap = {"ts": time.time(), "vm": {}, "pressure": {"level": "HEALTHY"},
                "orphan_total": 0, "sessions": [], "apps": {}, "worktrees": []}
        replaced = []
        real = os.replace
        with mock.patch.object(os, "replace",
                               lambda a, b: (replaced.append(b), real(a, b))), \
                mock.patch.object(memmon, "learn"):
            memmon.log_sample(snap)
        self.assertIn(memmon.SNAPSHOT, replaced)
        self.assertEqual(st.read(memmon.SNAPSHOT)["pressure"], "HEALTHY")


class PlistTests(unittest.TestCase):
    """B36: install.sh --sampler into a temp HOME. launchctl, pkill and pgrep
    are stubs on PATH, so the real LaunchAgent is never loaded or unloaded."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="memmon-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.stubs = os.path.join(self.home, "stubs")
        os.makedirs(self.stubs)
        self.calls = os.path.join(self.home, "calls.log")
        for name in ("launchctl", "pkill", "pgrep"):
            path = os.path.join(self.stubs, name)
            with open(path, "w") as fh:
                fh.write(f'#!/bin/sh\necho "{name} $*" >> "{self.calls}"\n'
                         + ("exit 1\n" if name == "pgrep" else "exit 0\n"))
            os.chmod(path, 0o755)
        self.env = {"HOME": self.home, "PATH": f"{self.stubs}:/usr/bin:/bin",
                    "TMPDIR": self.home}
        found = subprocess.run(["/bin/bash", "-c", "command -v launchctl"], env=self.env,
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(found, os.path.join(self.stubs, "launchctl"))

    def install(self, *flags):
        return subprocess.run(["/bin/bash", os.path.join(HERE, "install.sh"), *flags],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def test_sampler_plist_is_standard_priority(self):
        out = self.install("--sampler")
        self.assertEqual(out.returncode, 0, out.stderr)
        plist = os.path.join(self.home, "Library/LaunchAgents/dev.memmon.sampler.plist")
        with open(plist, "rb") as fh:
            cfg = plistlib.load(fh)
        self.assertEqual(cfg["ProcessType"], "Standard")
        self.assertNotIn("Nice", cfg)
        self.assertNotIn("LowPriorityIO", cfg)
        self.assertEqual(cfg["ProgramArguments"][-1], "--log")
        with open(self.calls) as fh:
            self.assertIn("launchctl bootstrap", fh.read())
        dest = os.path.join(self.home, ".claude/memmon")
        route = os.path.join(dest, "memmon_route.sh")
        with open(route) as fh, open(os.path.join(HERE, "memmon_route.sh")) as src:
            self.assertEqual(fh.read(), src.read())
        self.assertTrue(os.access(route, os.X_OK))
        self.assertFalse(os.path.exists(os.path.join(dest, "runner/coord/route.json")))
        out = self.install("--uninstall")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(os.path.exists(plist))
        for mod in ("memmon_pressure", "memmon_telemetry", "memmon_route", "memmon"):
            self.assertFalse(os.path.exists(os.path.join(dest, f"{mod}.py")), mod)
        # Route off ran first, and the launcher stays as a pass-through stub.
        self.assertTrue(os.path.exists(os.path.join(dest, "runner/coord/route.off")))
        import memmon_route
        with open(route) as fh:
            self.assertEqual(fh.read(), memmon_route.STUB)
        self.assertTrue(os.access(route, os.X_OK))


if __name__ == "__main__":
    unittest.main()
