#!/usr/bin/env python3
"""memmon usage --json (D45): seven local days from history.jsonl, gate.jsonl
and the runner's admission log, with no rollup file. Empty days stay empty;
the section mapping never guesses."""

from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

import memmon
from testkit import TempState

GB = 1 << 30
NOW = time.mktime((2026, 10, 9, 15, 0, 0, 0, 0, -1))      # a local afternoon


def at(day_offset, hour=12):
    t = time.localtime(NOW)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + day_offset, hour, 0, 0, 0, 0, -1))


def full_row(ts, ram=30 * GB, **kw):
    row = {"ts": int(ts), "ram_used": ram, "swap_used": GB, "pressure": "HEALTHY",
           "sessions": {"Checkout refactor": 1 * GB}, "overhead": GB // 2,
           "apps": {"Brave": 2 * GB, "Docker VM": 3 * GB, "Slack": GB,
                    "WindowServer": GB // 4, "SomeNewApp": GB // 4},
           "worktrees": {"build:acme-web": 4 * GB}, "orphan": 4 * GB}
    row.update(kw)
    return row


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.close)
        r = self.state.root
        for name, value in (("USAGE_CACHE", f"{r}/runner/coord/usage-cache.json"),
                            ("ADMISSION_LOG", f"{r}/runner/coord/admission-log.jsonl")):
            p = mock.patch.object(memmon, name, value)
            p.start()
            self.addCleanup(p.stop)
        os.makedirs(f"{r}/runner/coord", exist_ok=True)

    def write(self, path, rows):
        with open(path, "w") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in rows)

    def test_shape_and_empty_days(self):
        self.write(memmon.HISTORY, [full_row(at(-6)), full_row(at(-6, 13), ram=34 * GB),
                                    full_row(at(-2)), full_row(at(0))])
        out = memmon.usage(7, now=NOW)
        self.assertEqual((out["schema_version"], out["days"]), (1, 7))
        self.assertGreater(out["ram_bytes"], 0)
        dates = [e["date"] for e in out["series"]]
        self.assertEqual(len(dates), 7)
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(dates[-1], time.strftime("%Y-%m-%d", time.localtime(NOW)))
        first = out["series"][0]
        self.assertEqual((first["samples"], first["mem_peak_bytes"], first["mem_avg_bytes"]),
                         (2, 34 * GB, 32 * GB))
        for e in out["series"][1:5]:
            if e["date"] == dates[4]:
                continue
            self.assertEqual(e["samples"], 0)                  # never interpolated
            self.assertIsNone(e["mem_peak_bytes"])
            self.assertIsNone(e["mem_avg_bytes"])
            self.assertIsNone(e["by_section"])
        self.assertEqual(set(first), {"date", "samples", "mem_peak_bytes", "mem_avg_bytes",
                                      "by_section", "gate", "runner"})
        cov = out["coverage"]
        self.assertEqual((cov["from_ts"], cov["to_ts"]), (int(at(-6)), int(at(0))))
        self.assertFalse(cov["complete"])

    def test_section_mapping(self):
        self.write(memmon.HISTORY, [full_row(at(0)), full_row(at(0, 13), sessions={})])
        sec = memmon.usage(7, now=NOW)["series"][-1]["by_section"]
        # Row 2 has no sessions: its claude share is the runtime pool only.
        self.assertEqual(sec, {"claude": (GB + GB // 2 + GB // 2) // 2,
                               "codex": None, "browser": 2 * GB, "dev": 0,
                               "app": GB + GB // 4, "service": 3 * GB,
                               "other": 4 * GB + GB // 4})

    def test_app_names_follow_the_owner_list_categories(self):
        cases = {"Google Chrome": "browser", "Chrome": "browser", "Safari": "browser",
                 "Arc": "browser", "Brave": "browser", "Cursor": "dev", "VS Code": "dev",
                 "Ghostty": "dev", "Xcode": "dev", "PyCharm": "dev", "Docker": "service",
                 "OrbStack": "service", "Docker VM": "service", "colima": "service",
                 "Slack": "app", "SomeNewApp": "app", "WindowServer": "other"}
        for name, want in cases.items():
            with self.subTest(name=name):
                self.assertEqual(memmon.usage_section(name), want)

    def test_name_table_covers_every_categorised_bundle(self):
        # One table: every bundle id the owner list categorises has a name.
        import memmon_common
        import memmon_owners as mo
        named = set(memmon_common.APP_NAMES)
        for group in (mo.BROWSERS, mo.SHELL_HOSTS, mo.DEV_APPS, set(mo.GUI_VM_APPS)):
            self.assertEqual(set(group) - named, set())

    def test_partial_rows_count_as_samples_only(self):
        self.write(memmon.HISTORY, [full_row(at(0)),
                                    {"ts": int(at(0, 13)), "partial": True,
                                     "pressure": "UNKNOWN"}])
        day = memmon.usage(7, now=NOW)["series"][-1]
        self.assertEqual(day["samples"], 2)
        self.assertEqual(day["mem_avg_bytes"], 30 * GB)
        self.assertEqual(day["by_section"]["browser"], 2 * GB)

    def test_strict_used_bytes_preferred(self):
        self.write(memmon.HISTORY, [full_row(at(0), used_bytes=20 * GB)])
        self.assertEqual(memmon.usage(7, now=NOW)["series"][-1]["mem_peak_bytes"], 20 * GB)

    def test_gate_counts_per_day(self):
        self.write(memmon.HISTORY, [full_row(at(0))])
        self.write(memmon.GATE_LOG, [
            {"ts": at(-1), "action": "warn"}, {"ts": at(-1, 14), "action": "warn"},
            {"ts": at(-1, 15), "action": "block"}, {"ts": at(0), "action": "allow"},
            {"ts": at(0), "action": "error"}, {"ts": at(-30), "action": "block"}])
        s = memmon.usage(7, now=NOW)["series"]
        self.assertEqual(s[-2]["gate"], {"warned": 2, "stopped": 1})
        self.assertEqual(s[-1]["gate"], {"warned": 0, "stopped": 0})
        self.assertEqual(sum(e["gate"]["stopped"] for e in s), 1)

    def test_runner_holds_from_the_admission_log(self):
        self.write(memmon.HISTORY, [full_row(at(0))])
        s = memmon.usage(7, now=NOW)["series"]
        self.assertTrue(all(e["runner"] == {"held": None, "cancelled": None} for e in s))
        self.write(memmon.ADMISSION_LOG, [
            {"ts": at(-1), "run_id": "a", "decision": "hold"},
            {"ts": at(-1, 13), "run_id": "a", "decision": "hold"},
            {"ts": at(-1, 14), "run_id": "b", "decision": "hold"},
            {"ts": at(0), "run_id": "c", "decision": "admit"}])
        s = memmon.usage(7, now=NOW)["series"]
        self.assertEqual(s[-2]["runner"], {"held": 2, "cancelled": None})
        self.assertEqual(s[-1]["runner"], {"held": 0, "cancelled": None})
        self.assertIsNone(s[-3]["runner"]["held"])         # before the log's first row

    def test_cache_is_keyed_by_the_inputs(self):
        self.write(memmon.HISTORY, [full_row(at(0))])
        first = memmon.usage(7, now=NOW)
        with mock.patch.object(memmon, "_day_rows", side_effect=AssertionError("re-read")):
            self.assertEqual(memmon.usage(7, now=NOW), first)
        with open(memmon.HISTORY, "a") as fh:
            fh.write(json.dumps(full_row(at(0, 14), ram=40 * GB)) + "\n")
        self.assertEqual(memmon.usage(7, now=NOW)["series"][-1]["mem_peak_bytes"], 40 * GB)

    def test_cli(self):
        import contextlib
        import io
        self.write(memmon.HISTORY, [full_row(time.time())])
        out = io.StringIO()
        with mock.patch("sys.argv", ["memmon", "usage", "--json", "--days", "3"]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(memmon.main(), 0)
        self.assertEqual(len(json.loads(out.getvalue())["series"]), 3)


class UsageBenchTests(unittest.TestCase):
    """p95 at most 300 ms, cold, on a full 12 MB history. Runs only while the
    machine reads HEALTHY."""

    def test_full_history_p95(self):
        state = TempState()
        self.addCleanup(state.close)
        level = memmon.sampler_reading()["pressure"]["level"]
        if level != "HEALTHY":
            self.skipTest(f"pressure is {level}; benchmarks run only at HEALTHY")
        cache = os.path.join(state.root, "runner", "coord", "usage-cache.json")
        p = mock.patch.object(memmon, "USAGE_CACHE", cache)
        p.start()
        self.addCleanup(p.stop)
        now = time.time()
        row = full_row(now)
        row["sessions"] = {f"Session {i}": GB for i in range(8)}
        size, rows = 0, []
        i = 0
        while size < memmon.HISTORY_TRIM_AT:
            r = dict(row, ts=int(now - 12 * 86400 + i * 60))
            line = json.dumps(r) + "\n"
            rows.append(line)
            size += len(line)
            i += 1
        with open(memmon.HISTORY, "w") as fh:
            fh.writelines(rows)
        times = []
        for _ in range(20):
            try:
                os.remove(cache)
            except OSError:
                pass
            t = time.perf_counter()
            memmon.usage(7, now=now)
            times.append((time.perf_counter() - t) * 1000)
        times.sort()
        p95 = times[18]
        print(f"\nusage bench: {len(rows)} rows, {size / 1e6:.1f} MB, "
              f"median {times[10]:.0f} ms, p95 {p95:.0f} ms")
        self.assertLessEqual(p95, 300)


if __name__ == "__main__":
    unittest.main()
