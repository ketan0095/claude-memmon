"""S2 admission: strict telemetry, the ledger, the queue, hysteresis,
estimates and run monitoring (protect mode).

Real-process tests run `memmon run` wrappers as workers of this file, with
injected kernel inputs (testkit.FakeTelemetry steered through a JSON file)
and only synthetic children: sleepers, a TERM-ignoring sleeper and an
allocator capped at 384 MiB. Nothing touches real memory pressure, and the
policy engine in a worker may signal only its own wrapper's child tree."""

import fcntl
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

import memmon_runner as runner
import memmon_telemetry as tm
import testkit
from testkit import GiB, FakeTelemetry, FakeTelemetryClock

MiB = 1 << 20
HERE = os.path.dirname(os.path.abspath(__file__))
SLEEP = "import sys,time; time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 30)"
IGNORE_TERM = ("import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
               "time.sleep(float(sys.argv[1]))")
# Grows 64 MiB at a time to the cap, touching every page so the footprint is
# real, holds, then exits 0. Never more than 384 MiB.
ALLOCATOR = ("import sys,time\n"
             "cap=min(int(sys.argv[1]),384)<<20; hold=float(sys.argv[2]); bufs=[]\n"
             "while sum(map(len,bufs))<cap:\n"
             "    b=bytearray(64<<20)\n"
             "    for i in range(0,len(b),16384): b[i]=1\n"
             "    bufs.append(b); time.sleep(0.05)\n"
             "time.sleep(hold)\n")


def est(n_bytes):
    return {"bytes": n_bytes, "confidence": "reserved", "samples": None}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tele = str(self.root / "telemetry.json")
        self.fake = FakeTelemetry(path=self.tele)
        with open(self.tele, "w") as fh:
            json.dump({"level": "HEALTHY", "memsize": 48 * GiB, "used": 8 * GiB}, fh)
        self.procs = []
        self.reg = testkit.Registry(self)
        self.saved_mode = runner.DEFAULT_MODE
        runner.DEFAULT_MODE = "protect"

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
        self.reg.cleanup()
        runner.DEFAULT_MODE = self.saved_mode
        self.tmp.cleanup()

    # ------------------------------------------------------ real workers

    def seed(self):
        testkit.seed_admission(str(self.root), self.fake)

    def launch(self, code=SLEEP, args=(), guard=False, env=None, root=None, tele=None,
               real=False, cpu_log=None, **kw):
        kw.setdefault("poll_interval", 0.05)
        kw.setdefault("tick_s", 0.2)
        kw.setdefault("hysteresis_s", 0.0)
        kw.setdefault("timeout", 30)
        payload = dict(command=[sys.executable, "-c", code, *map(str, args)],
                       state_dir=str(root or self.root), telemetry=tele or self.tele, guard=guard,
                       sent_log=str(self.root / "sent.jsonl"), kw=kw, real=real,
                       cpu_log=cpu_log)
        proc = subprocess.Popen([sys.executable, __file__, "--worker", json.dumps(payload)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env=dict(os.environ, **(env or {})))
        self.procs.append(proc)
        self.reg.register(proc.pid)
        return proc

    def wait_for(self, predicate, timeout=10, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail(f"{what} not reached within {timeout}s")

    def finish(self, proc, expected=0, timeout=30):
        out, err = proc.communicate(timeout=timeout)
        self.assertEqual(proc.returncode, expected, err)
        return out, err

    def running(self, n=1):
        rows = [j for j in runner.jobs(self.root) if j["state"] in ("running", "intervention_needed")]
        for j in rows:
            if j.get("child"):
                self.reg.register(j["child"]["pid"], j["child"]["start"])
        return rows if len(rows) >= n else None

    def log(self):
        path = self.root / "runner/coord/admission-log.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    # ------------------------------------------------- in-process reader

    def reader(self, clock, fake, **kw):
        kw.setdefault("source", testkit.FakeSource([]))
        r = runner.Runner(["true"], str(self.root), None, clock=clock,
                          telemetry_read=lambda: tm.read_pressure_strict(source=fake, clock=clock),
                          err=io.StringIO(), **kw)
        r.run_id = uuid.uuid4().hex
        r.paths.make()
        return r


# ===================================================== telemetry (S2.1)

class StrictTelemetryTests(Base):
    def test_strict_reader_raises_on_every_missing_input(self):
        for fail in ("sysctl", "vm_stat"):
            with self.subTest(fail=fail), self.assertRaises(tm.TelemetryError):
                tm.read_pressure_strict(source=FakeTelemetry(fail=fail))
        text = FakeTelemetry().vm_stat(1).replace("Swapins: 1000.\n", "")
        with self.assertRaises(tm.TelemetryError):
            tm.parse_vm_stat(text)
        with self.assertRaises(tm.TelemetryError):
            tm.parse_vm_stat("no header")
        bad = FakeTelemetry(free_pct=140)
        with self.assertRaises(tm.TelemetryError):
            tm.read_pressure_strict(source=bad)
        bad = FakeTelemetry(kernel=3)
        with self.assertRaises(tm.TelemetryError):
            tm.read_pressure_strict(source=bad)

    def test_mach_counters_match_vm_stat(self):
        """The runner's host_statistics64 reader names vm_stat's counters."""
        src = tm.MachTelemetrySource()
        mach = src._mach()
        ref = tm.parse_vm_stat(src.vm_stat(2.0))
        self.assertTrue(tm.agree(mach, ref), (mach, ref))
        r = tm.read_pressure_strict(source=src)
        self.assertTrue(src.verified)
        for key in ("swapins", "swapouts", "used_bytes", "page_size"):
            self.assertIsInstance(r[key], int)

    def test_mach_reader_falls_back_to_vm_stat_on_disagreement_or_failure(self):
        ref = tm.parse_vm_stat(FakeTelemetry().vm_stat(1))

        class Odd(tm.MachTelemetrySource):
            calls = 0

            def _mach(self):
                return dict(ref, used_bytes=ref["used_bytes"] * 3)

            def vm_stat(self, timeout):
                Odd.calls += 1
                return FakeTelemetry().vm_stat(timeout)
        src = Odd()
        self.assertEqual(src.vm_counters(1.0)["used_bytes"], ref["used_bytes"])
        self.assertIs(src.verified, False)
        self.assertEqual(src.vm_counters(1.0)["used_bytes"], ref["used_bytes"])
        self.assertEqual(Odd.calls, 2, "every later read uses vm_stat")

        class Broken(Odd):
            def _mach(self):
                raise tm.TelemetryError("host_statistics64 failed (5)")

            def vm_stat(self, timeout):
                raise tm.TelemetryError("vm_stat exited 1")
        with self.assertRaises(tm.TelemetryError):
            Broken().vm_counters(1.0)

    def test_hung_reader_is_bounded(self):
        t0 = time.monotonic()
        with self.assertRaises(tm.TelemetryError):
            tm.read_pressure_strict(source=FakeTelemetry(hang=5), budget_s=0.3)
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_used_is_anonymous_minus_purgeable_plus_wired_and_compressor(self):
        text = ("Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
                "Anonymous pages: 100.\nPages purgeable: 10.\nPages wired down: 20.\n"
                "Pages occupied by compressor: 5.\nPages stored in compressor: 999.\n"
                "Pageins: 1.\nPageouts: 2.\nSwapins: 3.\nSwapouts: 4.\n")
        self.assertEqual(tm.parse_vm_stat(text)["used_bytes"], (100 - 10 + 20 + 5) * 16384)

    def test_real_strict_read_has_every_field(self):
        r = tm.read_pressure_strict()
        for key in ("pressure_level", "free_pct", "ram_total", "swap_used", "swapins",
                    "swapouts", "used_bytes", "mono", "boot", "load", "ncpu"):
            self.assertIsNotNone(r.get(key), key)

    def test_score_parity_with_legacy_pressure(self):
        """B26: the strict score equals pressure() for the same inputs."""
        import memmon
        memmon._page = testkit.PAGE
        ram = 48 * GiB
        base = dict(ram_total=ram, ncpu=8, load=2.0, swap_used=0, free_pct=60,
                    swapins=1000, swapouts=1000, page_size=testkit.PAGE, boot="b")
        cases = [
            ({}, {}),
            ({}, {"swapins": 1000 + 60 * 20000, "swapouts": 1000 + 60 * 5000}),
            ({"swap_used": ram}, {"swap_used": int(ram * 1.1), "swapins": 1000 + 60 * 3000}),
            ({"free_pct": 40}, {"free_pct": 21, "swap_used": int(ram * 0.6)}),
            ({"free_pct": 30}, {"free_pct": 18, "load": 40.0}),
        ]
        for streak in (0, 1):
            for prev_over, cur_over in cases:
                prev, cur = dict(base, **prev_over), dict(base, **cur_over)
                prev["mono"], cur["mono"] = 0.0, 60.0
                with self.subTest(prev=prev_over, cur=cur_over, streak=streak):
                    if hasattr(memmon, "mono_now"):
                        # pressure() on the sampler lane: mono baselines.
                        memmon._prev_vm = dict(prev, _mono=1000.0, _boot="b", _lh_streak=streak)
                        memmon._free_base = {"free_pct": prev["free_pct"], "mono": 1000.0,
                                             "boot": "b"}
                        memmon._last_rates = {}
                        with mock.patch("memmon.mono_now", return_value=1060.0):
                            legacy = memmon.pressure(dict(cur))
                    else:
                        memmon._prev_vm = dict(prev, _ts=1_000_000.0, _lh_streak=streak)
                        with mock.patch("memmon.time.time", return_value=1_000_060.0):
                            legacy = memmon.pressure(dict(cur))
                    strict = tm.score(cur, tm.rates_between(prev, cur),
                                      tm.free_delta_between(prev, cur), streak)
                    for key in ("level", "score", "reasons", "lh_streak", "next_level",
                                "to_next"):
                        self.assertEqual(strict[key], legacy[key], key)
                    for key in ("headroom_min", "swapin_mbs", "swapout_mbs",
                                "swap_growth_mbmin", "free_delta_min"):
                        if legacy[key] is None:
                            self.assertIsNone(strict[key], key)
                        else:
                            self.assertAlmostEqual(strict[key], legacy[key], places=6, msg=key)
        memmon._prev_vm = {}
        for name in ("_free_base", "_last_rates"):
            if hasattr(memmon, name):
                setattr(memmon, name, {})

    def test_no_rates_is_unknown_never_healthy(self):
        v = tm.score({"free_pct": 80, "ram_total": GiB}, None, None, 3)
        self.assertEqual((v["level"], v["rates"], v["lh_streak"]), ("UNKNOWN", "unavailable", 3))
        self.assertTrue(v["level_reason"])
        watch = tm.score({"free_pct": 18, "ram_total": GiB}, None, None, 0)
        self.assertEqual((watch["level"], watch["rates"]), ("WATCH", "unavailable"))

    def test_free_delta_needs_a_30s_baseline(self):
        a = {"mono": 0.0, "free_pct": 30, "boot": "b"}
        self.assertIsNone(tm.free_delta_between(a, {"mono": 2.0, "free_pct": 29, "boot": "b"}))
        self.assertIsNone(tm.free_delta_between(a, {"mono": 301.0, "free_pct": 29, "boot": "b"}))
        self.assertIsNone(tm.free_delta_between(a, {"mono": 60.0, "free_pct": 29, "boot": "c"}))
        self.assertAlmostEqual(tm.free_delta_between(a, {"mono": 60.0, "free_pct": 29, "boot": "b"}), -1.0)
        # One whole-percent tick over 2 s must not escalate WATCH (B32 rule).
        v = tm.score({"free_pct": 18, "ram_total": GiB}, {"swapin_mbs": 0}, None, 1)
        self.assertEqual((v["level"], v["lh_streak"], v["headroom_min"]), ("WATCH", 1, None))

    def test_gap_record_causes(self):
        """B33 vectors (the sampler lane uses this helper)."""
        a = {"mono": 0.0, "uptime": 0.0, "boot": "b"}
        self.assertIsNone(tm.gap_record(a, {"mono": 120.0, "uptime": 120.0, "boot": "b"}))
        g = tm.gap_record(a, {"mono": 1260.0, "uptime": 1260.0, "boot": "b"})
        self.assertEqual(g["cause"], "starved")
        g = tm.gap_record(a, {"mono": 1260.0, "uptime": 60.0, "boot": "b"})
        self.assertEqual(g["cause"], "sleep")
        g = tm.gap_record(a, {"mono": 1260.0, "uptime": 540.0, "boot": "b"})
        self.assertEqual((g["cause"], g["asleep_s"]), ("starved", 720.0))
        self.assertEqual(tm.gap_record(a, {"mono": 5.0, "uptime": 5.0, "boot": "c"})["cause"], "reboot")


# ============================================= shared rates + hysteresis

class HysteresisTests(Base):
    def series(self, levels, hysteresis_s=30.0, step=1.0, est_bytes=GiB):
        """Poll once per `step` through `levels` (a list, or a function of t),
        returning the admit decision at each t."""
        clock = FakeTelemetryClock(t=5000.0)
        fake = FakeTelemetry(memsize=48 * GiB, used=8 * GiB)
        r = self.reader(clock, fake, hysteresis_s=hysteresis_s)
        out = {}
        t0 = clock.t
        n = len(levels) if isinstance(levels, list) else levels
        for i in range(n):
            clock.t = t0 + i * step
            lvl = levels[i] if isinstance(levels, list) else None
            if lvl is None:
                continue
            if lvl == "FAIL":
                fake.values.update(fail="vm_stat")
            else:
                fake.values.update(fail=None, level=lvl)
            out[i] = r.decide(est(est_bytes))[0]
        return out

    def first_admit(self, out):
        return min((t for t, ok in out.items() if ok), default=None)

    def test_single_bad_sample_reopens_exactly_30s_after_next_good(self):
        """B28: polling 1 s, one bad sample at t=31, then good."""
        levels = ["HEALTHY"] * 31 + ["CRITICAL"] + ["HEALTHY"] * 40
        out = self.series(levels)
        self.assertFalse(any(out[t] for t in range(31, 62)))
        self.assertTrue(out[62])
        self.assertTrue(all(out[t] for t in range(62, 72)))

    def test_unavailable_then_good_restarts_window(self):
        """B25 (a): good, 10 s unavailable, good."""
        levels = ["HEALTHY"] * 41 + ["FAIL"] * 10 + ["HEALTHY"] * 40
        out = self.series(levels)
        self.assertTrue(out[40])
        self.assertFalse(any(out[t] for t in range(41, 81)))
        self.assertTrue(out[81])

    def test_reader_gap_after_bad_restarts_from_first_good(self):
        """B25 (b): a bad reading, 60 s with no readers, then good."""
        levels = ["HEALTHY"] * 3 + ["DANGER"] + [None] * 60 + ["HEALTHY"] * 40
        out = self.series(levels)
        self.assertFalse(any(out[t] for t in range(64, 94)))
        self.assertTrue(out[94])

    def test_silence_without_a_bad_reading_also_needs_30s(self):
        levels = ["HEALTHY"] * 40 + [None] * 20 + ["HEALTHY"] * 40
        out = self.series(levels)
        self.assertTrue(out[39])
        self.assertFalse(out[60])
        self.assertTrue(out[90])

    def test_candidate_change_carries_window(self):
        """B10: CRITICAL then HEALTHY; the candidate dies mid-window and a
        new arrival continues it, admitting only 30 s after the last bad."""
        clock = FakeTelemetryClock(t=7000.0)
        fake = FakeTelemetry(memsize=48 * GiB, used=8 * GiB)
        a, b = self.reader(clock, fake), self.reader(clock, fake)
        t0 = clock.t
        for i in range(0, 5):
            clock.t = t0 + i
            fake.values["level"] = "CRITICAL"
            self.assertFalse(a.decide(est(GiB))[0])
        last_bad = clock.t
        for i in range(5, 20):              # a recovers for 15 s, then dies
            clock.t = t0 + i
            fake.values["level"] = "HEALTHY"
            self.assertFalse(a.decide(est(GiB))[0])
        admitted = None
        for i in range(20, 60):             # b arrives and polls
            clock.t = t0 + i
            if b.decide(est(GiB))[0]:
                admitted = clock.t
                break
        self.assertEqual(admitted, last_bad + 1 + 30)

    def test_wrapper_tick_danger_holds_new_job(self):
        """B11 (state level): a wrapper tick's DANGER is shared."""
        clock = FakeTelemetryClock(t=9000.0)
        fake = FakeTelemetry(memsize=48 * GiB, used=8 * GiB)
        cand = self.reader(clock, fake, hysteresis_s=30)
        for i in range(40):
            clock.t += 1
            cand.decide(est(GiB))
        self.assertTrue(cand.decide(est(GiB))[0])
        # Another wrapper's tick reads DANGER and writes the shared state.
        p = runner.Paths(self.root)
        st = runner.load_state(p, clock.boot())
        st = tm.apply_hysteresis(st, False, clock.t, clock.t)
        runner._write(p.admission, st)
        clock.t += 1
        ok, reason, _ = cand.decide(est(GiB))
        self.assertFalse(ok)
        self.assertIn("holding for recovery", reason)

    def test_interleaved_readers_all_see_cached_rates(self):
        """B29: sustained paging, candidate every 1 s and a wrapper every
        2 s; every reader after the seed sees the same non-zero rates, so
        2-tick intervention and 5-tick auto-cancel arrive on time."""
        clock = FakeTelemetryClock(t=11000.0)
        fake = FakeTelemetry(clock=clock, memsize=48 * GiB, used=8 * GiB, level="DANGER",
                             swapin_rate=20000)          # ~330 MB/s of paging
        p = runner.Paths(self.root)
        p.make()
        state = tm.fresh_state(clock.boot())
        m = {}
        causes, verdicts = [], []
        t0 = clock.t
        for i in range(0, 25):
            for who in (("cand",) if i % 2 else ("cand", "wrap")):
                clock.t = t0 + i + (0.01 if who == "wrap" else 0)
                tm.note_clocks(state, clock.mono(), clock.awake())
                reading = tm.read_pressure_strict(source=fake, clock=clock)
                state, v = tm.strict_verdict(state, reading, clock.mono())
                state = tm.apply_hysteresis(state, tm.is_good(v), v["mono"] or clock.t, clock.t)
                if i >= 2:
                    verdicts.append(v)
                if who == "wrap":
                    causes.append((i, runner.intervention_cause(m, v, None, None, GiB),
                                   m.get("critical")))
        self.assertTrue(all(v["ok"] and v["swapin_mbs"] > 100 for v in verdicts), verdicts[:3])
        self.assertTrue(all(v["level"] == "CRITICAL" for v in verdicts))
        ticks = [(i, c, crit) for i, c, crit in causes if i >= 2]
        self.assertEqual(ticks[1][1], "pressure")          # second valid tick
        self.assertEqual(ticks[4][2], runner.AUTO_CANCEL_TICKS)  # fifth: 10 s CRITICAL

    def test_wake_makes_telemetry_stale_and_restarts_window(self):
        """B13 (state level)."""
        clock = FakeTelemetryClock(t=13000.0)
        fake = FakeTelemetry(memsize=48 * GiB, used=8 * GiB)
        r = self.reader(clock, fake, hysteresis_s=30)
        for _ in range(40):
            clock.t += 1
            r.decide(est(GiB))
        self.assertTrue(r.decide(est(GiB))[0])
        clock.machine_sleep(600)
        ok, reason, _ = r.decide(est(GiB))
        self.assertFalse(ok)
        self.assertIn("telemetry unavailable", reason)
        opened = None
        for i in range(1, 50):
            clock.t += 1
            if r.decide(est(GiB))[0]:
                opened = i
                break
        self.assertGreaterEqual(opened, 30)

    def test_deadline_counts_sleep(self):
        """B13 (runner level): a 100 s timeout ends 150 s of sleep later,
        long before 100 s of awake time has passed."""
        clock = FakeTelemetryClock(t=14000.0)
        fake = FakeTelemetry(memsize=48 * GiB, used=8 * GiB, level="DANGER")
        t0 = clock.t
        clock.at(t0 + 10, lambda: clock.machine_sleep(150))
        err = io.StringIO()
        code = runner.Runner(["/bin/echo", "must-not-run"], str(self.root), None, timeout=100,
                             clock=clock, source=testkit.FakeSource([]), err=err,
                             telemetry_read=lambda: tm.read_pressure_strict(source=fake, clock=clock)
                             ).run()
        self.assertEqual(code, 124)
        self.assertLess(clock.awake() - (t0 - 0), 100 - 50)
        self.assertGreaterEqual(clock.mono() - t0, 100)
        self.assertIn("command not started", err.getvalue())

    def test_window_needs_last_bad_before_recovery_start(self):
        """The admission condition itself, independent of how the state was
        written: a bad reading at or after recovery_start keeps it shut."""
        st = dict(tm.fresh_state("b"), recovery_start_mono=10.0, last_bad_mono=20.0)
        self.assertFalse(tm.recovery(st, 100.0)["open"])
        st["last_bad_mono"] = 9.0
        self.assertTrue(tm.recovery(st, 100.0)["open"])
        self.assertFalse(tm.recovery(st, 39.0)["open"])

    def test_rates_from_before_a_gap_are_never_reused(self):
        """After a re-seed the cache still holds pre-gap rates; a reader
        inside the next 2 s must fail, not score with them."""
        clock = FakeTelemetryClock(t=19000.0)
        fake = FakeTelemetry(memsize=48 * GiB, used=8 * GiB)
        r = self.reader(clock, fake, hysteresis_s=0)
        for _ in range(5):
            clock.t += 1
            r.decide(est(GiB))
        self.assertTrue(r.decide(est(GiB))[0])
        clock.t += 400                           # baseline too old: re-seed only
        self.assertFalse(r.decide(est(GiB))[0])
        clock.t += 1
        ok, reason, _ = r.decide(est(GiB))
        self.assertFalse(ok)
        self.assertIn("rate cache stale", reason)

    def test_short_sleep_reseeds_instead_of_spanning_it(self):
        """A wake shorter than the 300 s baseline limit still re-seeds: rates
        must not be averaged across the time asleep."""
        clock = FakeTelemetryClock(t=21000.0)
        fake = FakeTelemetry(memsize=48 * GiB, used=8 * GiB)
        r = self.reader(clock, fake, hysteresis_s=0)
        for _ in range(5):
            clock.t += 1
            r.decide(est(GiB))
        clock.machine_sleep(10)
        ok, reason, _ = r.decide(est(GiB))
        self.assertFalse(ok)
        self.assertIn("telemetry unavailable", reason)

    def test_boot_change_and_corrupt_state_need_a_fresh_window(self):
        clock = FakeTelemetryClock(t=15000.0)
        fake = FakeTelemetry(memsize=48 * GiB, used=8 * GiB)
        r = self.reader(clock, fake, hysteresis_s=30)
        for _ in range(40):
            clock.t += 1
            r.decide(est(GiB))
        self.assertTrue(r.decide(est(GiB))[0])
        for breaker in ("boot", "corrupt"):
            with self.subTest(breaker):
                if breaker == "boot":
                    clock.boot_id = "boot-b"
                else:
                    r.paths.admission.write_text("{not json")
                clock.t += 1
                self.assertFalse(r.decide(est(GiB))[0])
                for _ in range(40):
                    clock.t += 1
                    r.decide(est(GiB))
                self.assertTrue(r.decide(est(GiB))[0])

    def test_unknown_or_failed_pressure_never_starts_in_protect(self):
        """The protect half of AD-S2-1: no rates (UNKNOWN) or a failed read holds."""
        clock = FakeTelemetryClock(t=17000.0)
        fake = FakeTelemetry(memsize=48 * GiB, used=8 * GiB)
        r = self.reader(clock, fake, hysteresis_s=0)
        ok, reason, _ = r.decide(est(GiB))           # first read only seeds the baseline
        self.assertFalse(ok)
        self.assertIn("telemetry unavailable", reason)
        fake.values["fail"] = "vm_stat"
        clock.t += 3
        self.assertFalse(r.decide(est(GiB))[0])


# ====================================================== committed (S2.2)

def P(pid, fp, ppid=1, start=None):
    return testkit.P(pid, ppid=ppid, fp=fp, start=start)


def row(rid, res, pid=None, state="running", start=None):
    child = {"pid": pid, "start": list(start or (1_700_000_000 + pid, pid))} if pid else None
    return {"id": rid, "reservation_bytes": res, "child": child, "state": state}


class CommittedTests(unittest.TestCase):
    G = GiB

    def test_m6a_and_m6b_worked_examples(self):
        g = lambda x: int(round(x * GiB))
        rows = [row("index", g(4.0), 100), row("e2e", g(22.5), 200)]
        procs = {100: P(100, g(3.2)), 200: P(200, g(19.4))}
        c = runner.committed(rows, g(30.5), 48 * GiB, 0.2, procs, procs)
        self.assertAlmostEqual(runner.committed_total(c) / GiB, 34.4, places=6)
        self.assertAlmostEqual(c["free"] / GiB, 4.0, places=6)
        self.assertAlmostEqual(c["limit"] / GiB, 38.4, places=6)
        procs = {100: P(100, g(7.9)), 200: P(200, g(19.4))}
        c = runner.committed(rows, g(46.1), 48 * GiB, 0.2, procs, procs)
        self.assertAlmostEqual(runner.committed_total(c) / GiB, 49.2, places=6)
        self.assertAlmostEqual(c["free"] / GiB, -10.8, places=6)
        self.assertTrue(c["over"])

    def test_shrink_during_read_uses_lower_sample(self):
        """B2: footprint 10 before vm_stat, 4 after; the lower sample counts."""
        rows = [row("a", 12 * GiB, 100)]
        before, after = {100: P(100, 10 * GiB)}, {100: P(100, 4 * GiB)}
        c = runner.committed(rows, 20 * GiB, 48 * GiB, 0.2, before, after)
        self.assertEqual(c["slack"], 8 * GiB)
        # 38.4 - 28 = 10.4 free: a 10 GiB job that the upper sample (slack 2,
        # free 16.4) would have admitted with room to spare still fits, but
        # an 11 GiB one is held.
        self.assertLess(c["free"], 11 * GiB)

    def test_degraded_inventory_counts_full_reservations(self):
        """B3: used 20, sum r 16, true footprints 2, estimate 10 -> committed 36, held."""
        rows = [row("a", 8 * GiB, 100), row("b", 8 * GiB, 200)]
        procs = {100: P(100, GiB), 200: P(200, GiB)}
        c = runner.committed(rows, 20 * GiB, 48 * GiB, 0.2, procs, procs, degraded=True)
        self.assertEqual(runner.committed_total(c), 36 * GiB)
        self.assertLess(c["free"], 10 * GiB)

    def test_mostly_compressed_job_follows_documented_heuristic(self):
        """B27: footprint 10 (resident 2) against reservation 10 has no slack:
        footprint, not resident, is the measure; re-inflation is caught by
        the pressure gate and intervention, not by this formula."""
        rows = [row("a", 10 * GiB, 100)]
        procs = {100: P(100, 10 * GiB)}
        procs[100].resident = 2 * GiB
        c = runner.committed(rows, 20 * GiB, 48 * GiB, 0.2, procs, procs)
        self.assertEqual(c["slack"], 0)
        m = {}
        crit = {"ok": True, "level": "CRITICAL"}
        self.assertIsNone(runner.intervention_cause(m, crit, 10 * GiB, c, 10 * GiB))
        self.assertEqual(runner.intervention_cause(m, crit, 10 * GiB, c, 10 * GiB), "pressure")

    def test_tree_gone_or_reused_counts_full_reservation(self):
        rows = [row("a", 4 * GiB, 100, start=(5, 5)), row("w", 4 * GiB, None, state="waiting")]
        procs = {100: P(100, GiB, start=(6, 6))}
        c = runner.committed(rows, 0, 48 * GiB, 0.2, procs, procs)
        self.assertEqual(c["slack"], 4 * GiB)

    def test_descendants_count_toward_the_job(self):
        rows = [row("a", 4 * GiB, 100)]
        procs = {100: P(100, GiB), 101: P(101, GiB, ppid=100), 102: P(102, GiB, ppid=101)}
        c = runner.committed(rows, 0, 48 * GiB, 0.2, procs, procs)
        self.assertEqual(c["slack"], GiB)

    def test_pre_s2_lease_counts_default_or_footprint(self):
        self.assertEqual(runner.reservation_of({"footprint_bytes": None}), runner.DEFAULT_ESTIMATE)
        self.assertEqual(runner.reservation_of({"footprint_bytes": 9 * GiB}), 9 * GiB)

    def test_intervention_causes(self):
        m = {}
        ok = {"ok": True, "level": "HEALTHY"}
        over = {"over": True}
        self.assertEqual(runner.intervention_cause(m, ok, 200 * MiB, over, 128 * MiB), "growth")
        self.assertIsNone(runner.intervention_cause(m, ok, 200 * MiB, {"over": False}, 128 * MiB))
        for i in range(2):
            self.assertIsNone(runner.intervention_cause(m, {"ok": False}, None, None, GiB))
        self.assertEqual(runner.intervention_cause(m, {"ok": False}, None, None, GiB), "telemetry")


# ======================================================= estimates (S2.4)

class EstimateTests(Base):
    def test_estimate_tiers(self):
        peaks = {"keys": {"k": {"peaks": [GiB]}}}
        self.assertEqual(runner.estimate_for({"keys": {}}, "k")["confidence"], "unknown")
        self.assertEqual(runner.estimate_for({"keys": {}}, "k")["bytes"], 4 * GiB)
        low = runner.estimate_for(peaks, "k")
        self.assertEqual((low["confidence"], low["bytes"]), ("low", 4 * GiB))
        peaks["keys"]["k"]["peaks"] = [6 * GiB, 2 * GiB]
        self.assertEqual(runner.estimate_for(peaks, "k")["bytes"], 9 * GiB)
        peaks["keys"]["k"]["peaks"] = [GiB, 2 * GiB, 2 * GiB]
        learned = runner.estimate_for(peaks, "k")
        self.assertEqual((learned["confidence"], learned["bytes"]), ("learned", int(2 * GiB * 1.15)))
        self.assertEqual(runner.estimate_for(peaks, "k", 3 * GiB)["confidence"], "reserved")

    def test_job_key_has_no_argv_and_worktrees_share_it(self):
        repo = self.root / "acme-web"
        (repo / ".git" / "worktrees" / "wt1").mkdir(parents=True)
        wt = self.root / "acme-web-wt1"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {repo}/.git/worktrees/wt1\n")
        self.assertEqual(runner.project_of(repo), "acme-web")
        self.assertEqual(runner.project_of(wt / "sub" if False else wt), "acme-web")
        shape = runner.command_shape(["pnpm", "--filter", "x", "test", "secret-token-123"])
        self.assertNotIn("secret", shape)
        k = runner.job_key("heavy", shape, "acme-web")
        self.assertRegex(k, r"^[0-9a-f]{16}$")

    def test_concurrent_peak_updates_lose_nothing_and_lru_keeps_200(self):
        """B20."""
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import memmon_runner as r\n"
                "import memmon_telemetry as tm\n"
                "p = r.Paths(sys.argv[2])\n"
                "with r.ledger(p, tm.SYSTEM_CLOCK, tm.SYSTEM_CLOCK.mono() + 20):\n"
                "    r.record_peak(p, 'shared', int(sys.argv[3]), float(sys.argv[3]))\n")
        procs = [subprocess.Popen([sys.executable, "-c", code, HERE, str(self.root), str(n)])
                 for n in range(1, 9)]
        for p in procs:
            self.reg.register(p.pid)
        for p in procs:
            self.assertEqual(p.wait(timeout=30), 0)
        peaks = runner.load_peaks(runner.Paths(self.root))
        self.assertEqual(sorted(peaks["keys"]["shared"]["peaks"]), list(range(1, 9)))
        paths = runner.Paths(self.root)
        for i in range(201):
            runner.record_peak(paths, f"k{i}", GiB, 1000.0 + i)
        keys = runner.load_peaks(paths)["keys"]
        self.assertEqual(len(keys), 200)
        self.assertNotIn("k0", keys)
        self.assertNotIn("shared", keys)
        self.assertIn("k200", keys)

    def test_corrupt_peaks_and_mode_fall_back(self):
        p = runner.Paths(self.root)
        p.make()
        p.peaks.write_text("{oops")
        self.assertEqual(runner.load_peaks(p), {"version": 1, "keys": {}})
        p.mode.write_text("{oops")
        mode, warning, auto = runner.read_mode(self.root)
        self.assertEqual((mode, auto), ("protect", False))
        self.assertIn("unreadable", warning)
        runner.write_mode(self.root, "observe", True)
        self.assertEqual(runner.read_mode(self.root), ("observe", None, True))


# ======================================================== real processes

class RealAdmissionTests(Base):
    def test_concurrent_admissions_never_overcommit(self):
        """B1: 16 runners, fake limit 10 GiB, reservations of 1-4 GiB."""
        self.fake.set(memsize=int(12.5 * GiB), used=GiB)
        self.seed()
        procs = [self.launch(SLEEP, [0.3], resource=f"r{i}", reserve=1 + i % 4, timeout=60)
                 for i in range(16)]
        for p in procs:
            self.finish(p, timeout=90)
        log = self.log()
        admits = [r for r in log if r["decision"] == "admit"]
        self.assertEqual(len(admits), 16)
        for r in admits:
            self.assertLessEqual(r["committed"] + r["estimate"], r["limit"], r)
        self.assertTrue(any(r["decision"] == "hold" and "budget" in r["reason"] for r in log),
                        "the fake limit must actually have forced holds")

    def test_wrapper_sigkill_keeps_reservation_until_child_exits(self):
        """B4."""
        self.fake.set(memsize=int(12.5 * GiB), used=GiB)
        self.seed()
        holder = self.launch(SLEEP, [3], reserve=8, resource="a")
        self.wait_for(self.running, what="holder running")
        holder.kill()
        holder.wait(timeout=5)
        self.assertTrue(runner.jobs(self.root), "the child keeps its lease")
        waiter = self.launch(SLEEP, [0.1], reserve=4, resource="b")
        self.wait_for(lambda: [j for j in runner.jobs(self.root)
                               if j["state"] == "waiting" and "budget" in (j["reason"] or "")],
                      what="waiter held for budget")
        holder.communicate(timeout=10)           # the orphaned child inherits the pipes
        self.finish(waiter, timeout=20)
        admits = [r for r in self.log() if r["decision"] == "admit"]
        self.assertEqual(len(admits), 2)

    def test_sigkilled_waiter_ticket_is_pruned(self):
        """B5."""
        self.seed()
        holder = self.launch(SLEEP, [1.5])
        self.wait_for(self.running, what="holder")
        w1 = self.launch(SLEEP, [0.1])
        self.wait_for(lambda: len(runner.tickets(self.root / "runner/queue")) == 1, what="w1 ticket")
        w2 = self.launch("print('w2 ran')")
        self.wait_for(lambda: len(runner.tickets(self.root / "runner/queue")) == 2, what="w2 ticket")
        w1.kill()
        w1.wait(timeout=5)
        out, _ = self.finish(w2, timeout=20)
        self.assertEqual(out, "w2 ran\n")
        self.finish(holder)

    def test_growth_triggers_one_intervention_and_never_stops(self):
        """B6."""
        self.fake.set(memsize=2 * GiB, used=int(1.3 * GiB))
        self.seed()
        proc = self.launch(ALLOCATOR, [384, 2.0], reserve=0.125)
        self.wait_for(self.running, what="allocator running")
        self.fake.set(used=int(1.9 * GiB))          # committed above the 1.6 GiB limit
        rows = self.wait_for(lambda: [j for j in runner.jobs(self.root)
                                      if j["state"] == "intervention_needed"],
                             timeout=10, what="intervention")
        self.assertEqual(rows[0]["intervention"]["cause"], "growth")
        _, err = self.finish(proc, 0, timeout=30)
        self.assertEqual(err.count("needs attention"), 1, err)
        self.assertNotIn("cancelled by policy", err)

    def test_policy_cancel_with_opt_in(self):
        """B7: --interruptible + opt-in + CRITICAL for 5 ticks -> per-PID
        cancel through the S1 engine (TERM, grace, KILL of the survivor),
        exit 75, and nothing outside the child's tree is signalled."""
        runner.write_mode(self.root, "protect", True)
        self.seed()
        proc = self.launch(IGNORE_TERM, [60], guard=True, interruptible=True,
                           policy_grace_s=0.5)
        rows = self.wait_for(self.running, what="running")
        child = rows[0]["child"]
        self.fake.set(level="CRITICAL")
        _, err = self.finish(proc, 75, timeout=30)
        self.assertIn(f"memmon: cancelled by policy (ended_by=policy, run_id={rows[0]['id']})", err)
        sent = [json.loads(x) for x in (self.root / "sent.jsonl").read_text().splitlines()]
        self.assertEqual({s["pid"] for s in sent}, {child["pid"]})
        self.assertEqual([s["sig"] for s in sent], [signal.SIGTERM, signal.SIGKILL])
        self.assertFalse(any(s.get("refused") for s in sent))

    def test_no_opt_in_means_hold_and_notify_only(self):
        """B8."""
        self.seed()
        proc = self.launch(SLEEP, [2.5], guard=True, interruptible=True)
        self.wait_for(self.running, what="running")
        self.fake.set(level="CRITICAL")
        _, err = self.finish(proc, 0, timeout=30)
        self.assertEqual(err.count("needs attention"), 1, err)
        self.assertFalse((self.root / "sent.jsonl").exists())

    def test_stale_or_blocked_telemetry_bounds_wait(self):
        """B9: a raising reader -> 125, a hung reader -> 125, ledger.lock
        held by a stuck process -> 124; each at ~6 s, child never started."""
        marker = self.root / "must-not-exist"
        touch = "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()"
        cases = []
        for name, values in (("raises", {"fail": "sysctl"}), ("hangs", {"hang": 10})):
            root = self.root / name
            tele = str(self.root / f"tele-{name}.json")
            with open(tele, "w") as fh:
                json.dump(dict(level="HEALTHY", **values), fh)
            cases.append((name, self.launch(touch, [marker], timeout=6, root=root, tele=tele), 125))
        root = self.root / "ledger-held"
        p = runner.Paths(root)
        p.make()
        lock = open(p.ledger, "a+")
        fcntl.flock(lock, fcntl.LOCK_EX)
        cases.append(("ledger", self.launch(touch, [marker], timeout=6, root=root), 124))
        t0 = time.monotonic()
        try:
            for name, proc, code in cases:
                with self.subTest(name):
                    _, err = self.finish(proc, code, timeout=20)
                    self.assertIn("command not started", err)
        finally:
            lock.close()
        self.assertLess(time.monotonic() - t0, 10)
        self.assertGreater(time.monotonic() - t0, 5)
        self.assertFalse(marker.exists())

    def test_wrapper_tick_danger_holds_new_arrival(self):
        """B11 with real wrappers."""
        self.seed()
        holder = self.launch(SLEEP, [4], resource="a")
        self.wait_for(self.running, what="holder")
        self.fake.set(level="DANGER")
        time.sleep(0.5)                       # two of the holder's ticks see it
        self.fake.set(level="HEALTHY")
        waiter = self.launch("print('ran')", resource="b", hysteresis_s=1.5)
        self.wait_for(lambda: [j for j in runner.jobs(self.root)
                               if j["state"] == "waiting"
                               and "holding for recovery" in (j["reason"] or "")],
                      what="held for recovery")
        out, _ = self.finish(waiter, timeout=20)
        self.assertEqual(out, "ran\n")
        self.finish(holder, timeout=20)

    def test_queue_bound_and_fairness(self):
        """B12: the 33rd waiter is refused; a newer ticket for a free
        resource overtakes an older one whose resource is busy; same-resource
        order holds."""
        self.seed()
        holder = self.launch(SLEEP, [60])
        self.wait_for(self.running, what="holder")
        waiters = [self.launch(SLEEP, [0.1], timeout=60) for _ in range(32)]
        q = self.root / "runner/queue"
        self.wait_for(lambda: len(runner.tickets(q)) == 32, timeout=30, what="32 tickets")
        extra = self.launch(SLEEP, [0.1])
        _, err = self.finish(extra, 124)
        self.assertIn("memmon: admission queue full (32)", err)
        for w in waiters[1:]:
            w.terminate()
        for w in waiters[1:]:
            w.communicate(timeout=10)
        self.wait_for(lambda: len(runner.tickets(q)) == 1, what="one older ticket left")
        other = self.launch("print('other ran')", resource="other")
        out, _ = self.finish(other, timeout=20)
        self.assertEqual(out, "other ran\n")
        self.assertEqual(waiters[0].poll(), None, "the older ticket still waits for its resource")
        waiters[0].terminate()
        waiters[0].communicate(timeout=10)
        holder.terminate()
        holder.communicate(timeout=10)

    def test_same_resource_runs_in_arrival_order(self):
        self.seed()
        events = self.root / "events"
        code = ("import sys,time; open(sys.argv[1],'a').write(sys.argv[2]+'\\n'); "
                "time.sleep(float(sys.argv[3]))")
        holder = self.launch(code, [events, "holder", 1.0])
        self.wait_for(self.running, what="holder")
        order = []
        for name in ("first", "second", "third"):
            order.append(self.launch(code, [events, name, 0.05]))
            self.wait_for(lambda n=len(order): len(runner.tickets(self.root / "runner/queue")) == n,
                          what=f"{name} ticket")
        for p in [holder, *order]:
            self.finish(p, timeout=30)
        self.assertEqual(events.read_text().split(), ["holder", "first", "second", "third"])

    def test_no_budget_jumping(self):
        """A newer ticket that would fit never overtakes an older one that
        does not, even on a different resource (ticket-first FIFO)."""
        self.fake.set(memsize=int(12.5 * GiB), used=GiB)
        self.seed()
        holder = self.launch(SLEEP, [1.5], reserve=6, resource="h")
        self.wait_for(self.running, what="holder")
        big = self.launch("print('big')", reserve=6, resource="a")
        self.wait_for(lambda: len(runner.tickets(self.root / "runner/queue")) == 1, what="big ticket")
        small = self.launch("print('small')", reserve=1, resource="b")
        for p in (holder, big, small):
            self.finish(p, timeout=30)
        admits = [r["resource"] for r in self.log() if r["decision"] == "admit"]
        self.assertEqual(admits, ["h", "a", "b"])

    def test_resource_named_ledger_is_just_a_resource(self):
        """B23."""
        self.seed()
        a = self.launch(SLEEP, [1.0], resource="ledger")
        b = self.launch("print('b ran')", resource="heavy")
        self.finish(a, timeout=20)
        out, _ = self.finish(b, timeout=20)
        self.assertEqual(out, "b ran\n")
        self.assertTrue((self.root / "runner/ledger.lock").exists())
        self.assertTrue((self.root / "runner/coord/ledger.lock").exists())

    def test_grandchild_keeps_reservation_detached(self):
        """B24: sh -c 'sleep N & exit 0'."""
        self.seed()
        # The spec's shape: sh keeps the inherited descriptors for its background child.
        code = ("import os; os.execv('/bin/sh', ['sh', '-c', "
                "'/bin/sleep 2.5 >/dev/null 2>&1 & exit 0'])")
        proc = self.launch(code, reserve=2)
        self.finish(proc, timeout=20)
        rows = runner.jobs(self.root)
        self.assertEqual([r["state"] for r in rows], ["detached"])
        snap = runner.snapshot(self.root, system={"used_bytes": GiB, "ram_bytes": 48 * GiB})
        self.assertEqual(snap["committed"]["slack"], 2 * GiB)
        rid = rows[0]["id"]
        self.wait_for(lambda: not runner.jobs(self.root), timeout=10, what="grandchild exit")
        self.assertTrue((self.root / f"runner/{rid}.json").exists(), "only pruning unlinks")
        self.finish(self.launch("pass"), timeout=20)
        self.assertFalse((self.root / f"runner/{rid}.json").exists())
        self.assertFalse((self.root / f"runner/{rid}.lease").exists())

    def test_observe_mode_admits_like_v1_and_logs_would_hold(self):
        """B15."""
        runner.write_mode(self.root, "observe")
        self.fake.set(memsize=int(12.5 * GiB), used=GiB)
        self.seed()
        out, err = self.finish(self.launch("print('ran')", reserve=20))
        self.assertEqual((out, err), ("ran\n", ""))
        log = self.log()
        self.assertEqual(log[-1]["decision"], "would_hold")
        self.assertIn("waiting for budget", log[-1]["reason"])

    def test_admitted_job_stderr_is_byte_identical(self):
        self.seed()
        out, err = self.finish(self.launch(
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"), 3)
        self.assertEqual((out, err), ("out\n", "err\n"))

    def test_jobs_json_schema_2(self):
        self.seed()
        proc = self.launch(SLEEP, [1.0], reserve=1, label="Checkout refactor")
        self.wait_for(lambda: [j for j in runner.jobs(self.root) if j.get("footprint_bytes")],
                      what="first tick")
        snap = runner.snapshot(self.root)
        self.assertEqual(snap["schema_version"], 2)
        self.assertEqual(snap["mode"], "protect")
        for key in ("used", "slack", "limit", "free"):
            self.assertIsInstance(snap["committed"][key], int)
        j = snap["jobs"][0]
        for key in ("state", "reason", "reservation_bytes", "footprint_bytes", "estimate",
                    "queue_position", "deadline_ts", "mode", "ended_by", "child", "job_key"):
            self.assertIn(key, j)
        self.assertEqual(j["reservation_bytes"], GiB)
        self.assertNotIn(SLEEP, json.dumps(snap))
        self.finish(proc)


class OmegaR1LedgerTests(Base):
    def test_awake_clock_is_shared_across_processes(self):
        """LEDGER-1: a (mono, awake) pair stored by one runner must read as
        no time asleep in another process started much later."""
        here = tm.SYSTEM_CLOCK.mono() - tm.SYSTEM_CLOCK.awake()
        time.sleep(1.5)                       # the other process starts later
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import memmon_telemetry as t; "
                "print(t.SYSTEM_CLOCK.mono() - t.SYSTEM_CLOCK.awake())")
        out = subprocess.run([sys.executable, "-c", code, HERE], capture_output=True,
                             text=True, check=True).stdout
        self.assertLess(abs(float(out) - here), 0.5)

    def test_runners_started_apart_record_no_wake(self):
        """LEDGER-1, real clocks: B starts 6 s after A; A's ticks must not
        read as a wake to B, and B is admitted."""
        self.seed()
        a = self.launch(SLEEP, [12], resource="a")
        self.wait_for(self.running, what="A running")
        time.sleep(6.0)
        b = self.launch("print('b ran')", resource="b", timeout=4)
        out, _ = self.finish(b, timeout=20)
        self.assertEqual(out, "b ran\n")
        st = json.loads((self.root / "runner/coord/admission-state.json").read_text())
        self.assertIsNone(st.get("last_wake_mono"))
        rows = [j for j in runner.jobs(self.root) if j["resource"] == "a"]
        self.assertEqual(rows[0]["state"], "running", "A must not be flagged")
        a.terminate()
        a.communicate(timeout=10)

    def own_child(self, argv):
        """A Runner whose child is a synthetic process this test spawned."""
        import memmon_procs
        r = runner.Runner(["true"], str(self.root), None, err=io.StringIO(),
                          policy_grace_s=0.5,
                          engine_factory=_guarded_factory(str(self.root / "sent.jsonl")))
        r.run_id = uuid.uuid4().hex
        r.paths.make()
        r.record = r.paths.root / (r.run_id + ".json")
        r.child = subprocess.Popen(argv, start_new_session=True)
        self.reg.register(r.child.pid)
        start = list(memmon_procs.default_source().read(r.child.pid).start)
        r.row = {"id": r.run_id, "label": "acme-web tests", "state": "running",
                 "reason": "command running", "wrapper_pid": os.getpid(),
                 "child_pid": r.child.pid, "child_start": start,
                 "child": {"pid": r.child.pid, "start": start}, "ended_by": None}
        r.monitor = {"bad": 0, "failed": 0, "critical": 5, "clear_since": None}
        return r

    def test_policy_cancel_follows_child_despite_group_survivor(self):
        """LEDGER-2 (a): an orphan left in the child's group makes the engine
        report partial; the child we TERMed is gone, so it is a policy cancel."""
        import memmon_procs
        r = self.own_child(["/bin/sh", "-c", "(/bin/sleep 3 &) ; exec /bin/sleep 30"])
        src = memmon_procs.default_source()
        time.sleep(0.3)
        for p in src.scan().values():
            if p.pgid == r.child.pid and p.pid != r.child.pid and p.start:
                self.reg.register(p.pid, p.start)
        self.assertEqual(r.policy_cancel(), "cancelled")
        self.assertEqual((r.row["ended_by"], r.row["state"]), ("policy", "cancelled_by_policy"))
        self.assertEqual(r.child.returncode, -signal.SIGTERM)
        self.assertNotIn("keeps running", r.err.getvalue())

    def test_child_exiting_on_its_own_keeps_its_code(self):
        """LEDGER-2 (b): the child exits while the engine waits; no signal
        was ours, so its own exit code passes through."""
        base = _guarded_factory(str(self.root / "sent.jsonl"))

        def slow_factory(run, grace):
            eng, token = base(run, grace)
            time.sleep(0.8)                  # an actions.lock wait, say
            return eng, token
        r = self.own_child(["/bin/sleep", "0.3"])
        r.engine_factory = slow_factory
        self.assertIsNone(r.policy_cancel())
        self.assertIsNone(r.row["ended_by"])
        self.assertEqual(r.child.returncode, 0)
        r2 = self.own_child(["/bin/sleep", "0.01"])
        r2.child.wait()
        calls = []
        r2.engine_factory = lambda run, grace: calls.append(run) or (None, None)
        self.assertIsNone(r2.policy_cancel())
        self.assertEqual(calls, [], "no engine for a child that already exited")
        self.assertNotIn("keeps running", r2.err.getvalue())

    def test_timeout_zero_still_attempts_once(self):
        """LEDGER-3."""
        clock = FakeTelemetryClock(t=23000.0)
        fake = FakeTelemetry(memsize=48 * GiB, used=8 * GiB)
        testkit.seed_admission(str(self.root), fake, clock=clock)
        code = runner.Runner(["/usr/bin/true"], str(self.root), None, timeout=0, clock=clock,
                             hysteresis_s=0, source=testkit.FakeSource([]), err=io.StringIO(),
                             telemetry_read=lambda: tm.read_pressure_strict(source=fake, clock=clock)
                             ).run()
        self.assertEqual(code, 0)

    def test_peak_waits_out_a_busy_ledger_and_logs_a_drop(self):
        """LEDGER-4."""
        import threading
        r = runner.Runner(["true"], str(self.root), None, err=io.StringIO())
        r.run_id, r.key, r.peak = uuid.uuid4().hex, "acme-web-key", 3 * GiB
        r.paths.make()
        fh = open(r.paths.ledger, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        threading.Timer(2.5, fh.close).start()      # longer than the old 2 s wait
        self.assertTrue(r.save_peak())
        self.assertEqual(runner.load_peaks(r.paths)["keys"]["acme-web-key"]["peaks"], [3 * GiB])
        fh = open(r.paths.ledger, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            self.assertFalse(r.save_peak(wait_s=0.3))
        finally:
            fh.close()
        self.assertEqual(self.log()[-1]["decision"], "peak_dropped")
        # A SIGINT/SIGTERM during the wait returns at once (the handler sets this flag).
        fh = open(r.paths.ledger, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        threading.Timer(0.3, lambda: r.cancelled.__setitem__(0, signal.SIGTERM)).start()
        t0 = time.monotonic()
        try:
            self.assertFalse(r.save_peak())
        finally:
            fh.close()
        self.assertLess(time.monotonic() - t0, 2.0)


def _healthy_now():
    """Benches run only while the kernel level is normal and the strict
    score, with real rates, is HEALTHY or WATCH."""
    try:
        a = tm.read_pressure_strict(source=tm.MACH_SOURCE)
        time.sleep(2.1)
        b = tm.read_pressure_strict(source=tm.MACH_SOURCE)
        v = tm.score(b, tm.rates_between(a, b), None, 0)
        return b["pressure_level"] == "normal" and v["level"] in ("HEALTHY", "WATCH")
    except tm.TelemetryError:
        return False


class OverheadTests(Base):
    def test_runner_average_cpu_under_one_percent(self):
        """B22c: a ~20 s job under the real strict reader and libproc costs
        the runner at most 1 % of wall time (measured from run() entry, so
        interpreter start-up is excluded), and its RSS stays under 40 MB."""
        if not _healthy_now():
            self.skipTest("benches run only at HEALTHY/WATCH with normal kernel pressure")
        r = tm.read_pressure_strict(source=tm.MACH_SOURCE)
        st = tm.fresh_state(r["boot"])
        base = tm._counters(r)
        base["mono"] -= 3
        st.update(rate_baseline=base, free_baseline=base, recovery_start_mono=r["mono"] - 60,
                  last_good_mono=r["mono"], clock_pair=[tm.SYSTEM_CLOCK.mono(), tm.SYSTEM_CLOCK.awake()])
        coord = self.root / "runner/coord"
        coord.mkdir(parents=True, exist_ok=True)
        (coord / "admission-state.json").write_text(json.dumps(st))
        cpu_log = str(self.root / "cpu.json")
        proc = self.launch("import time; time.sleep(20)", real=True, cpu_log=cpu_log,
                           tick_s=2.0, poll_interval=1.0, hysteresis_s=30.0)
        self.finish(proc, timeout=60)
        m = json.loads(Path(cpu_log).read_text())
        self.assertGreater(m["wall"], 19.5)
        self.assertLessEqual(m["cpu"] / m["wall"], 0.01, m)
        self.assertLess(m["maxrss"], 40e6, m)

    def test_tick_cost(self):
        """B22: one monitoring tick with real telemetry and a real scan."""
        import resource
        r = runner.Runner(["true"], str(self.root), None, err=io.StringIO())
        r.run_id = uuid.uuid4().hex
        r.paths.make()
        r.record = r.paths.root / (r.run_id + ".json")
        me = r.source().read(os.getpid())
        r.row = {"id": r.run_id, "state": "running", "child": {"pid": os.getpid(),
                 "start": list(me.start)}, "reservation_bytes": GiB, "label": "bench"}
        r.monitor = {"bad": 0, "failed": 0, "critical": 0, "clear_since": None}
        costs = []
        for _ in range(12):
            s0 = resource.getrusage(resource.RUSAGE_SELF)
            c0 = resource.getrusage(resource.RUSAGE_CHILDREN)
            r.tick()
            s1 = resource.getrusage(resource.RUSAGE_SELF)
            c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
            costs.append((s1.ru_utime + s1.ru_stime - s0.ru_utime - s0.ru_stime)
                         + (c1.ru_utime + c1.ru_stime - c0.ru_utime - c0.ru_stime))
        costs.sort()
        p95 = costs[int(len(costs) * 0.95) - 1]
        self.assertLess(p95, 0.020, costs)


# ------------------------------------------------------------------ worker

def _guarded_factory(sent_log):
    def factory(run, grace_s):
        import memmon_procs
        eng, token = runner._default_engine(run, grace_s)
        child = run.child.pid
        src = memmon_procs.default_source()

        def covered(pid):
            cur, seen = src.read(pid), set()
            while cur is not None and cur.pid not in seen and cur.pid > 1:
                if cur.pid == child:
                    return True
                seen.add(cur.pid)
                cur = src.read(cur.ppid)
            return False

        def kill(pid, sig):
            ok = covered(pid)
            with open(sent_log, "a") as fh:
                fh.write(json.dumps({"pid": pid, "sig": int(sig), "refused": not ok}) + "\n")
            if not ok:
                raise PermissionError(f"test guard: {pid} is outside the job tree")
            os.kill(pid, sig)
        if eng is not None:
            eng.kill = kill
        return eng, token
    return factory


if __name__ == "__main__":
    if sys.argv[1:2] == ["--worker"]:
        payload = json.loads(sys.argv[2])
        fake = FakeTelemetry(path=payload.pop("telemetry"))
        kw = payload.pop("kw")
        sent_log = payload.pop("sent_log")
        if payload.pop("guard"):
            kw["engine_factory"] = _guarded_factory(sent_log)
        if not payload.pop("real"):
            kw["telemetry_read"] = lambda: tm.read_pressure_strict(source=fake)
        cpu_log = payload.pop("cpu_log")
        if cpu_log:
            import resource

            def usage():
                s = resource.getrusage(resource.RUSAGE_SELF)
                c = resource.getrusage(resource.RUSAGE_CHILDREN)
                return s, c.ru_utime + c.ru_stime
            (s0, c0), t0 = usage(), time.monotonic()
        code = runner.run(payload["command"], payload["state_dir"], lambda: {"level": "HEALTHY"},
                          **kw)
        if cpu_log:
            (s1, c1), wall = usage(), time.monotonic() - t0
            # The child's own CPU (it only sleeps) is part of RUSAGE_CHILDREN;
            # it is negligible and counted against the runner, never for it.
            cpu = (s1.ru_utime + s1.ru_stime - s0.ru_utime - s0.ru_stime) + (c1 - c0)
            with open(cpu_log, "w") as fh:
                json.dump({"cpu": cpu, "wall": wall, "maxrss": s1.ru_maxrss}, fh)
        raise SystemExit(code)
    unittest.main()
