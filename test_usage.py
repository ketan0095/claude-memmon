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
    row = {"ts": int(ts), "used_bytes": ram, "ram_used": 47 * GB, "swap_used": GB,
           "pressure": "HEALTHY",
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
                                      "mem_basis", "by_section", "gate", "runner"})
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

    def test_history_records_browsers_and_editors(self):
        cases = [
            ("/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework"
             ".framework/Versions/1/Helpers/Google Chrome Helper (Renderer).app/Contents/"
             "MacOS/Google Chrome Helper (Renderer) --type=renderer", "Google Chrome", "browser"),
            ("/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
             "Google Chrome Canary", "browser"),
            ("/Applications/Ghostty.app/Contents/MacOS/ghostty", "Ghostty", "dev"),
            ("/Applications/iTerm.app/Contents/MacOS/iTerm2", "iTerm2", "dev"),
            ("/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal",
             "Terminal", "dev"),
            ("/Applications/Xcode.app/Contents/MacOS/Xcode", "Xcode", "dev"),
            ("/Applications/Firefox.app/Contents/MacOS/plugin-container.app/Contents/MacOS/"
             "plugin-container", "Firefox", "browser"),
            ("/Applications/OrbStack.app/Contents/MacOS/OrbStack", "OrbStack", "service"),
            ("/Applications/Slack.app/Contents/MacOS/Slack", "Slack", "app"),
            ("/System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer -daemon",
             "WindowServer", "other"),
        ]
        for cmd, name, section in cases:
            with self.subTest(name=name):
                self.assertEqual(memmon.app_group(cmd), name)
                self.assertEqual(memmon.usage_section(name), section)
        # Developer tools that merely live inside Xcode.app are not Xcode.
        self.assertEqual(memmon.app_group("/Applications/Xcode.app/Contents/Developer/usr/"
                                          "bin/git status"), "")
        self.assertEqual(memmon.usage_section("SomeNewApp"), "app")

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

    def test_strict_used_bytes_is_measured(self):
        self.write(memmon.HISTORY, [full_row(at(0), used_bytes=20 * GB)])
        day = memmon.usage(7, now=NOW)["series"][-1]
        self.assertEqual((day["mem_peak_bytes"], day["mem_basis"]), (20 * GB, "measured"))

    def test_legacy_rows_estimate_from_free_pct_never_ram_used(self):
        # A v1 sampler row: top's ram_used sits near RAM; the kernel's free %
        # is the honest basis.
        ram = 48 * GB
        legacy = {"ts": int(at(0)), "ram_used": int(47.9 * GB), "free_pct": 40,
                  "sessions": {}, "apps": {}, "worktrees": {}}
        self.write(memmon.HISTORY, [legacy, full_row(at(0, 13), used_bytes=20 * GB)])
        with mock.patch.object(memmon.os, "sysconf",
                               lambda name: {"SC_PHYS_PAGES": ram // 16384,
                                             "SC_PAGE_SIZE": 16384}[name]):
            day = memmon.usage(7, now=NOW)["series"][-1]
        self.assertEqual(day["mem_peak_bytes"], int(ram * 0.6))      # 28.8 GB
        self.assertEqual(day["mem_basis"], "estimated")
        self.assertEqual(day["mem_avg_bytes"], (int(ram * 0.6) + 20 * GB) // 2)

    def test_no_memory_figure_is_null_not_zero(self):
        self.write(memmon.HISTORY, [{"ts": int(at(0)), "ram_used": 47 * GB,
                                     "sessions": {}, "apps": {}, "worktrees": {}}])
        day = memmon.usage(7, now=NOW)["series"][-1]
        self.assertEqual(day["samples"], 1)
        self.assertEqual((day["mem_peak_bytes"], day["mem_avg_bytes"], day["mem_basis"]),
                         (None, None, None))

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

    def test_malformed_rows_are_skipped(self):
        good = full_row(at(0))
        with open(memmon.HISTORY, "w") as fh:
            fh.write('{"ts": 1..2, "ram_used": 1}\n')
            fh.write(json.dumps(full_row(at(0, 9), apps=["Brave"])) + "\n")
            fh.write(json.dumps(full_row(at(0, 10), worktrees=["build:x"])) + "\n")
            fh.write(json.dumps(full_row(at(0, 11), sessions=["x"])) + "\n")
            fh.write(json.dumps({"ts": float("nan")}).replace("NaN", "1e400") + "\n")
            fh.write("not json\n")
            fh.write(json.dumps(good) + "\n")
        self.write(memmon.GATE_LOG, [{"ts": at(0), "action": ["warn"]},
                                     {"ts": at(0), "action": "warn"}])
        self.write(memmon.ADMISSION_LOG, [{"ts": at(0), "decision": "hold", "run_id": ["a"]},
                                          {"ts": at(0), "decision": "hold", "run_id": {"x": 1}},
                                          {"ts": at(0), "decision": "hold", "run_id": "b"}])
        day = memmon.usage(7, now=NOW)["series"][-1]
        self.assertEqual(day["samples"], 1)
        self.assertEqual(day["by_section"]["browser"], 2 * GB)
        self.assertEqual(day["gate"], {"warned": 1, "stopped": 0})
        self.assertEqual(day["runner"]["held"], 1)
        import contextlib
        import io
        out = io.StringIO()
        with mock.patch("sys.argv", ["memmon", "usage", "--json"]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(memmon.main(), 0)

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
