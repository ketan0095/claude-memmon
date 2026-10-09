"""memmon explain: the summary it sends, and the one Claude call it makes.
Every test uses a stub `claude` on PATH; nothing reaches a real model."""

import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import memmon_explain as ex

GB = 1 << 30
TOKEN = "eyJ2IjoxLCJhY3Rpb24iOiJzdG9wLWpvYiJ9ZmFrZXRva2VuMTIzNDU2"
STUB = """#!{py}
import json, os, sys, time
d = os.path.dirname(os.path.abspath(__file__))
json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read(),
           "memmon_env": sorted(k for k in os.environ if k.startswith("MEMMON_"))}},
          open(os.path.join(d, "call.json"), "w"))
mode = os.environ.get("STUB_MODE", "")
reply = os.environ.get("STUB_REPLY")
if mode == "sleep":
    time.sleep(5)
if mode == "fail":
    sys.stderr.write("x" * 500 + "auth failed")
    sys.exit(3)
print(reply.replace("|", "\\n") if reply else
      "1. Stop the vitest run in Checkout refactor; it holds 8.8 GB.")
"""


def payload():
    """Synthetic, and hostile to hygiene: paths, PIDs, tokens and an email
    sit in the fields the summary must not leak."""
    owners = [
        {"owner_id": "claude:a", "kind": "claude",
         "title": "Checkout refactor for acme-web at /Volumes/someone/acme-web please",
         "footprint_bytes": int(9.5 * GB), "activity": "Running · cd ~/code/acme-web && pnpm test",
         "cpu_cores": 1.2, "root": {"pid": 4242, "start": [1, 2]}, "token": TOKEN,
         "cwd": "/Volumes/someone/acme-web",
         "jobs": [{"job_id": "4300.1.2", "kind": "test", "label": "vitest",
                   "footprint_bytes": int(8.8 * GB), "action": "stop-job",
                   "root": {"pid": 4300}, "token": TOKEN},
                  {"kind": "conversation", "label": "Conversation", "footprint_bytes": GB}]},
        {"owner_id": "service:vm:colima", "kind": "service", "title": "VM · colima",
         "footprint_bytes": 6 * GB, "activity": None, "cpu_cores": 0.01, "jobs": []},
        {"owner_id": "app:browser", "kind": "app", "title": "Browser (pid 999) ops@acme-web",
         "footprint_bytes": 3 * GB, "activity": None, "cpu_cores": None, "jobs": []},
    ] + [{"owner_id": f"unknown:{i}", "kind": "unknown", "title": f"Shell {i}",
          "footprint_bytes": (i + 1) * 10 << 20, "activity": None, "cpu_cores": 0.0, "jobs": []}
         for i in range(9)]
    return {
        "system": {"score_level": "DANGER", "pressure_level": "warning", "ram_bytes": 48 * GB,
                   "used_bytes": int(44.2 * GB), "reasons": ["swap 1.1x RAM size",
                                                              "paging 80 MB/s",
                                                              "swap growing 600 MB/min"],
                   "swap_used_bytes": int(5.5 * GB)},
        "owners": owners,
        "runner": {"mode": "protect", "committed": {"used": int(44.2 * GB), "slack": GB,
                                                    "limit": int(38.4 * GB), "free": -7 * GB},
                   "admission": {"open": False, "reason": "holding for recovery"},
                   "queue": {"length": 2, "max": 32}},
        "runner_jobs": [{"label": "Checkout refactor", "state": "waiting",
                         "reason": "resource busy: held by tsc (PID 4400)", "cwd": "/tmp/x"}],
        "pressure_suggestions": [{"job_id": "4300.1.2", "owner_id": "claude:a", "kind": "test",
                                  "label": "vitest", "footprint": int(8.8 * GB),
                                  "growth_mb_min": 12.0, "idle_s": 1860, "stop": "stop-job",
                                  "token": TOKEN}],
    }


class SummaryTests(unittest.TestCase):
    def summary(self, p=None):
        return ex.build_now_summary(p or payload())

    def test_summary_content(self):
        s = self.summary()
        self.assertEqual(s, self.summary(), "deterministic")
        self.assertLessEqual(len(s), ex.SUMMARY_MAX)
        for want in ("DANGER", "kernel warning", "swap 1.1x RAM size", "paging 80 MB per s",
                     "44.2 GB used of 48.0 GB", "swap 5.5 GB", "Checkout refactor", "8.8 GB",
                     "VM · colima", "idle service", "queued: 2", "holding for recovery",
                     "idle 31 min"):
            self.assertIn(want, s)
        for title in re.findall(r'"([^"]*)"', s):
            self.assertLessEqual(len(title), ex.TITLE_MAX)

    def test_summary_hygiene(self):
        s = self.summary()
        self.assertNotIn("/", s)
        self.assertNotRegex(s.lower(), r"\bpid\b")
        self.assertNotIn("@", s)
        self.assertNotIn(TOKEN, s)
        self.assertNotRegex(s, r"[A-Za-z0-9_+=-]{24,}")
        self.assertNotIn("4242", s)
        self.assertNotIn("4400", s)
        self.assertNotIn("999", s)
        self.assertNotIn("someone", s)

    def test_long_payload_is_cut_to_the_bound(self):
        p = payload()
        for o in p["owners"]:
            o["title"] = "Checkout refactor " * 10
            o["jobs"] = [{"kind": "test", "label": "vitest " * 9, "footprint_bytes": GB}] * 5
        self.assertLessEqual(len(self.summary(p)), ex.SUMMARY_MAX)


class StubBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)
        stub = self.bin / "claude"
        stub.write_text(STUB.format(py=sys.executable))
        stub.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {"PATH": str(self.bin), "MEMMON_RUN_ID": "a" * 32})
        self.env.start()
        self.extra = mock.patch.object(ex, "EXTRA_PATH", ())
        self.extra.start()

    def tearDown(self):
        self.extra.stop()
        self.env.stop()
        self.tmp.cleanup()

    def cli(self, *argv, timeout=ex.TIMEOUT_S):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = ex.cli(list(argv), payload, timeout=timeout)
        return code, buf.getvalue()

    def call(self):
        return json.loads((self.bin / "call.json").read_text())


class CallTests(StubBase):
    def test_exact_argv_tools_disabled_and_prompt_on_stdin(self):
        code, out = self.cli("--json")
        self.assertEqual(code, 0)
        res = json.loads(out)
        self.assertIn("Checkout refactor", res["text"])
        self.assertEqual(res["model"], "claude-haiku-5-5")
        call = self.call()
        self.assertEqual(call["argv"], ["-p", "--model", "claude-haiku-5-5", "--output-format",
                                        "text", "--tools", "", "--no-session-persistence"])
        self.assertTrue(call["stdin"].startswith(ex.PROMPT_NOW))
        self.assertEqual((res["mode"], res["title"]), ("now", "Free memory now"))
        self.assertEqual(res["chars_sent"], len(call["stdin"]))
        self.assertEqual(call["memmon_env"], [], "memmon's own variables never reach claude")
        self.assertNotIn(TOKEN, call["stdin"])

    def test_timeout_is_exit_2(self):
        os.environ["STUB_MODE"] = "sleep"
        code, out = self.cli("--json", timeout=0.5)
        self.assertEqual(code, 2)
        self.assertIn("did not answer", json.loads(out)["error"])

    def test_nonzero_exit_is_exit_2_with_short_stderr(self):
        os.environ["STUB_MODE"] = "fail"
        code, out = self.cli("--json")
        err = json.loads(out)["error"]
        self.assertEqual(code, 2)
        self.assertIn("auth failed", err)
        self.assertLess(len(err), 340)

    def test_not_found_is_exit_2(self):
        (self.bin / "claude").unlink()
        code, out = self.cli("--json")
        self.assertEqual(code, 2)
        self.assertIn("not found", json.loads(out)["error"])

    def test_preview_makes_no_call(self):
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("called")):
            code, out = self.cli("--preview")
        self.assertEqual(code, 0)
        self.assertEqual(out, ex.plan(payload(), None)["prompt"] + "\n")
        self.assertFalse((self.bin / "call.json").exists())

    def test_reply_is_printed_never_acted_on(self):
        with mock.patch.object(subprocess, "Popen", wraps=subprocess.Popen) as popen:
            code, out = self.cli()
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "Stop the vitest run in Checkout refactor; it holds 8.8 GB.")
        self.assertEqual(popen.call_count, 1, "one process: claude itself")


def healthy(p=None):
    p = p or payload()
    p["system"] = dict(p["system"], score_level="HEALTHY", pressure_level="normal",
                       used_bytes=int(20 * GB), reasons=[])
    p["pressure_suggestions"] = []
    return p


def quiet_payload():
    p = healthy()
    for o in p["owners"]:
        o["activity"], o["cpu_cores"], o["jobs"] = "Working", 1.0, []
    return p


def week(peak_frac=0.5, warned=0, owner_days=None, rules=None):
    ram = 48 * GB
    series = [{"date": f"2026-10-0{i + 1}", "samples": 1400, "mem_peak_bytes": int(peak_frac * ram),
               "mem_basis": "measured",
               "by_section": {"claude": 9 * GB, "browser": 4 * GB, "service": 6 * GB},
               "gate": {"warned": warned if i == 6 else 0, "stopped": 0}} for i in range(7)]
    return {"usage": {"ram_bytes": ram, "series": series}, "owner_days": owner_days or {},
            "rules": rules or {}}


class ModeTests(unittest.TestCase):
    def test_pressure_selects_now(self):
        for level in ("WATCH", "DANGER", "CRITICAL", "UNKNOWN", None):
            p = payload()               # kernel at warning
            p["system"]["score_level"] = level
            with self.subTest(level=level):
                self.assertEqual(ex.select_mode(p, week()), "now")

    def test_healthy_triggers_select_patterns(self):
        dup = quiet_payload()
        dup["owners"][0]["jobs"] = [{"kind": "server", "label": "vite", "footprint_bytes": GB}]
        dup["owners"][2]["jobs"] = [{"kind": "server", "label": "vite", "footprint_bytes": GB}]
        idle = quiet_payload()
        idle["owners"][1].update(activity="Idle", footprint_bytes=6 * GB)
        cases = {"peak": (quiet_payload(), week(peak_frac=0.8)),
                 "gate": (quiet_payload(), week(warned=1)),
                 "duplicate": (dup, week()),
                 "idle owner": (idle, week(owner_days={"VM · colima": 5}))}
        for name, (p, w) in cases.items():
            with self.subTest(name):
                self.assertEqual(ex.select_mode(p, w), "patterns")
                self.assertTrue(ex.pattern_triggers(p, w))

    def test_unknown_follows_the_kernel_level(self):
        """Just after start-up the score is UNKNOWN: with the kernel at normal
        the machine is healthy (patterns or quiet); otherwise it is now."""
        for kernel, want in (("normal", "quiet"), ("warning", "now"), ("critical", "now"),
                             (None, "now")):
            p = quiet_payload()
            p["system"].update(score_level="UNKNOWN", pressure_level=kernel)
            with self.subTest(kernel=kernel):
                self.assertEqual(ex.select_mode(p, week()), want)
        p = quiet_payload()
        p["system"].update(score_level="UNKNOWN", pressure_level="normal")
        self.assertEqual(ex.select_mode(p, week(warned=1)), "patterns")

    def test_healthy_without_triggers_is_quiet(self):
        idle = quiet_payload()
        idle["owners"][1].update(activity="Idle", footprint_bytes=6 * GB)
        self.assertEqual(ex.select_mode(quiet_payload(), week()), "quiet")
        self.assertEqual(ex.select_mode(idle, week(owner_days={"VM · colima": 3})), "quiet")
        self.assertEqual(ex.select_mode(quiet_payload(), week(peak_frac=0.74)), "quiet")

    def test_clean_drops_only_unsafe_words_from_titles(self):
        """The fixture's "…at please" is its own path being removed, not a
        mangled title: ordinary titles pass through whole."""
        for title in ("Checkout refactor", "acme-web: fix login (v2)", "VM · colima"):
            self.assertEqual(ex.clean(title), title)
        self.assertEqual(ex.clean("Fix build at /Volumes/x/acme-web now"), "Fix build at now")
        self.assertEqual(len(ex.clean("Checkout refactor " * 5)), ex.TITLE_MAX)

    def test_now_summary_lists_evidence(self):
        s = ex.plan(payload(), None)["prompt"]
        self.assertIn('"vitest" (test job) in "Checkout refactor', s)
        for want in ("idle 31 min", "growing 12 MB per min", "memmon can stop it", "8.8 GB"):
            self.assertIn(want, s)
        cands = [l for l in s.splitlines() if re.match(r"\d+\. ", l)]
        self.assertLessEqual(len(cands), ex.STOP_CANDIDATES)
        self.assertEqual(sum('"vitest"' in l for l in cands), 1, "a suggested job is listed once")

    def test_patterns_summary_content_and_hygiene(self):
        p = quiet_payload()
        p["owners"][1].update(activity="Idle", footprint_bytes=6 * GB)
        w = week(peak_frac=0.8, warned=3, owner_days={"VM · colima": 6, "/Users/x": 7},
                 rules={"tsc": 3, "pnpm … test": 1})
        s = ex.build_patterns_summary(p, w)
        for want in ("peak 38.4 GB (measured)", "top Claude sessions", "warned 3, stopped 0",
                     "most-warned rule tsc (3)", '"VM · colima" 6 of 7 days',
                     "Idle now and heavy most days", "Triggered by:"):
            self.assertIn(want, s)
        self.assertLessEqual(len(s), ex.SUMMARY_MAX)
        self.assertNotIn("/", s)
        self.assertNotRegex(s.lower(), r"\bpid\b")
        self.assertNotIn(TOKEN, s)
        self.assertNotIn("@", s)
        self.assertNotIn("someone", s)


class ReplyTests(StubBase):
    def run_cli(self, p, w=None, reply=None, *argv):
        if reply is not None:
            os.environ["STUB_REPLY"] = reply
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = ex.cli(["--json", *argv], lambda: p, week_fn=lambda: w)
        os.environ.pop("STUB_REPLY", None)
        return code, json.loads(buf.getvalue())

    def test_quiet_never_calls_claude(self):
        code, out = self.run_cli(quiet_payload(), week())
        self.assertEqual(code, 0)
        self.assertEqual(out, {"mode": "quiet", "title": "Nothing to do",
                               "text": "Nothing to do. Memory is healthy."})
        self.assertFalse((self.bin / "call.json").exists())
        code, out = self.run_cli(quiet_payload(), week(), None, "--preview")
        self.assertEqual((out["would_send"], out["mode"]), (False, "quiet"))
        self.assertIn("Nothing would be sent", out["preview"])
        self.assertFalse((self.bin / "call.json").exists())

    def test_generic_lines_are_dropped(self):
        reply = ("1. Close some browser tabs to free memory.|"
                 "2. Stop vitest in Checkout refactor; it is idle and frees about 8.8 GB.|"
                 "3. Restart your Mac.|`kill 4300` to stop vitest")
        code, out = self.run_cli(payload(), None, reply)
        self.assertEqual(code, 0)
        self.assertEqual(out["text"],
                         "Stop vitest in Checkout refactor; it is idle and frees about 8.8 GB.")

    def test_markdown_is_stripped_and_backticked_names_kept(self):
        reply = ("1. **Stop `vitest`** in __Checkout refactor__; frees about 8.8 GB.|"
                 "- `kill 4300` to stop vitest")
        _, out = self.run_cli(payload(), None, reply)
        self.assertEqual(out["text"], "Stop vitest in Checkout refactor; frees about 8.8 GB.")

    def test_unknown_with_normal_kernel_asks_nothing(self):
        p = quiet_payload()
        p["system"].update(score_level="UNKNOWN", pressure_level="normal")
        _, out = self.run_cli(p, week())
        self.assertEqual(out["mode"], "quiet")
        self.assertFalse((self.bin / "call.json").exists())

    def test_reply_capped_at_three_lines(self):
        reply = "|".join(f"{i}. Stop vitest, step {i}." for i in range(1, 6))
        _, out = self.run_cli(payload(), None, reply)
        self.assertEqual(len(out["text"].splitlines()), ex.REPLY_LINES)

    def test_nothing_specific_left_is_quiet(self):
        _, out = self.run_cli(payload(), None, "Close tabs.|Quit unused apps.")
        self.assertEqual((out["mode"], out["text"], out["asked"]),
                         ("quiet", "Nothing to do. Memory is healthy.", "now"))

    def test_patterns_mode_end_to_end(self):
        reply = "Run one \"VM · colima\" instead of leaving it idle all week."
        _, out = self.run_cli(quiet_payload(), week(warned=2), reply)
        self.assertEqual((out["mode"], out["title"]), ("patterns", "Patterns this week"))
        self.assertTrue(self.call()["stdin"].startswith(ex.PROMPT_PATTERNS))


if __name__ == "__main__":
    unittest.main()
