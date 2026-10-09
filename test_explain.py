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
if mode == "sleep":
    time.sleep(5)
if mode == "fail":
    sys.stderr.write("x" * 500 + "auth failed")
    sys.exit(3)
print("1. Stop the vitest run in Checkout refactor; it holds 8.8 GB.")
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
         "jobs": [{"kind": "test", "label": "vitest", "footprint_bytes": int(8.8 * GB),
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
    def test_summary_content(self):
        s = ex.build_summary(payload())
        self.assertEqual(s, ex.build_summary(payload()), "deterministic")
        self.assertLessEqual(len(s), ex.SUMMARY_MAX)
        for want in ("DANGER", "kernel warning", "swap 1.1x RAM size", "paging 80 MB per s",
                     "44.2 GB used of 48.0 GB",
                     "swap 5.5 GB", "Checkout refactor", "9.5 GB", "test 8.8 GB", "VM · colima",
                     "idle", "2 queued", "holding for recovery", "vitest (test) in",
                     "idle 31 min"):
            self.assertIn(want, s)
        owners = [l for l in s.splitlines() if re.match(r"\d+\. ", l)]
        self.assertEqual(len(owners), ex.TOP_OWNERS)
        for line in owners:
            title = re.search(r'"([^"]*)"', line).group(1)
            self.assertLessEqual(len(title), ex.TITLE_MAX)

    def test_summary_hygiene(self):
        s = ex.build_summary(payload())
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
            o["jobs"] = [{"kind": "test", "footprint_bytes": GB}] * 5
        self.assertLessEqual(len(ex.build_summary(p)), ex.SUMMARY_MAX)


class CallTests(unittest.TestCase):
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

    def test_exact_argv_tools_disabled_and_prompt_on_stdin(self):
        code, out = self.cli("--json")
        self.assertEqual(code, 0)
        res = json.loads(out)
        self.assertIn("Checkout refactor", res["text"])
        self.assertEqual(res["model"], "claude-haiku-5-5")
        call = self.call()
        self.assertEqual(call["argv"], ["-p", "--model", "claude-haiku-5-5", "--output-format",
                                        "text", "--tools", "", "--no-session-persistence"])
        self.assertTrue(call["stdin"].startswith(ex.PROMPT))
        self.assertIn("no action needed", call["stdin"])
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
        self.assertEqual(out, ex.PROMPT + ex.build_summary(payload()) + "\n")
        self.assertFalse((self.bin / "call.json").exists())

    def test_reply_is_printed_never_acted_on(self):
        with mock.patch.object(subprocess, "Popen", wraps=subprocess.Popen) as popen:
            code, out = self.cli()
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "1. Stop the vitest run in Checkout refactor; it holds 8.8 GB.")
        self.assertEqual(popen.call_count, 1, "one process: claude itself")


if __name__ == "__main__":
    unittest.main()
