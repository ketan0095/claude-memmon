"""MemmonBar native tests: outcome decoding, quit-app safety, renders, accessibility.

Nothing here touches a real process or app. `memmon` is replaced by stub
scripts in a temp dir, NSRunningApplication by the binary's fake AppControl,
and actions.lock by a temp file. Set MEMMON_BAR_BIN to reuse a built binary;
otherwise MemmonBar.swift is compiled into a temp dir.
"""

import base64
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures" / "ui"
BIN = None
_BUILD_DIR = None


def setUpModule():
    global BIN, _BUILD_DIR
    if os.environ.get("MEMMON_BAR_BIN"):
        BIN = os.environ["MEMMON_BAR_BIN"]
        return
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        raise unittest.SkipTest("swiftc not found; MemmonBar tests need Xcode CLT")
    _BUILD_DIR = tempfile.TemporaryDirectory()
    BIN = os.path.join(_BUILD_DIR.name, "MemmonBar")
    build = subprocess.run(["swiftc", "-o", BIN, str(ROOT / "MemmonBar.swift"), "-framework", "Cocoa"],
                           capture_output=True, text=True, timeout=600)
    if build.returncode != 0:
        raise RuntimeError("MemmonBar.swift does not compile:\n" + build.stderr[-4000:])


def tearDownModule():
    if _BUILD_DIR is not None:
        _BUILD_DIR.cleanup()


def run_bin(*args, timeout=60):
    proc = subprocess.run([BIN, *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise AssertionError(f"MemmonBar {' '.join(args)} exited {proc.returncode}:\n{proc.stderr[-2000:]}")
    return proc.stdout


def run_json(*args, timeout=60):
    return json.loads(run_bin(*args, timeout=timeout).strip().splitlines()[-1])


def a11y(fixture, *flags):
    rows = []
    for line in run_bin("--a11y-dump", "--fixture", str(FIXTURES / fixture), *flags).splitlines():
        parts = line.split("\t")
        if len(parts) >= 4 and parts[0].isdigit():
            parts += [""] * (5 - len(parts))
            rows.append({"depth": int(parts[0]), "role": parts[1], "label": parts[2],
                         "value": parts[3], "described": parts[4]})
    return rows


def labels(rows):
    return [r["label"] for r in rows]


class StubCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def stub(self, body):
        """A fake memmon.py: records its argv, then runs `body`."""
        path = self.dir / "memmon_stub.py"
        path.write_text("import json, os, sys, time\n"
                        f"open({str(self.dir / 'argv.json')!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                        + body)
        return str(path)

    def argv(self):
        p = self.dir / "argv.json"
        return json.loads(p.read_text()) if p.exists() else None


class ActOutcomeDecodingTests(StubCase):
    """A21: stdout JSON is decoded first and must agree with the exit code."""

    def probe(self, body, timeout="5"):
        return run_json("--act-probe", "--script", self.stub(body), "--timeout", timeout,
                        "--", "act", "stop-job", "--target", "tok-1")

    def emit(self, code, payload):
        return f"print(json.dumps({payload!r}))\nsys.exit({code})\n"

    def test_exit_3_with_partial_json_offers_force(self):
        r = self.probe(self.emit(3, {"result": "partial", "exited": 3, "remaining": [{"pid": 1}],
                                     "force_token": "force-abc"}))
        self.assertEqual(r["view"], "partial")
        self.assertEqual(r["force_token"], "force-abc")
        self.assertEqual(self.argv(), ["act", "stop-job", "--target", "tok-1"])

    def test_exit_4_with_refused_json_is_refused_with_reason(self):
        r = self.probe(self.emit(4, {"result": "refused", "reason": "target_changed"}))
        self.assertEqual((r["view"], r["reason"]), ("refused", "target_changed"))

    def test_exit_0_with_stopped_json_is_success(self):
        r = self.probe(self.emit(0, {"result": "stopped", "exited": 5}))
        self.assertEqual((r["view"], r["result"]), ("success", "stopped"))

    def test_exit_0_already_exited_is_success(self):
        r = self.probe(self.emit(0, {"result": "already_exited", "exited": 0}))
        self.assertEqual(r["view"], "success")

    def test_exit_3_respawned_is_its_own_outcome_without_force(self):
        r = self.probe(self.emit(3, {"result": "respawned", "exited": 9, "captured": 9, "remaining": []}))
        self.assertEqual((r["view"], r["result"]), ("success", "respawned"))
        self.assertIsNone(r["force_token"])

    def test_exit_1_is_error(self):
        r = self.probe("sys.stdout.write('Traceback: boom')\nsys.exit(1)\n")
        self.assertEqual(r["view"], "error")

    def test_exit_1_is_error_even_when_json_claims_success(self):
        r = self.probe(self.emit(1, {"result": "stopped", "exited": 5}))
        self.assertEqual(r["view"], "error")

    def test_malformed_output_with_exit_0_is_error(self):
        r = self.probe("print('stopped (probably)')\nsys.exit(0)\n")
        self.assertEqual(r["view"], "error")

    def test_malformed_output_with_exit_3_is_error_not_partial(self):
        r = self.probe("print('{not json')\nsys.exit(3)\n")
        self.assertEqual(r["view"], "error")

    def test_json_that_disagrees_with_exit_code_is_error(self):
        for code, payload in [(0, {"result": "partial", "force_token": "x"}),
                              (3, {"result": "stopped"}),
                              (4, {"result": "stopped"}),
                              (0, {"result": "refused", "reason": "busy"})]:
            with self.subTest(code=code, result=payload["result"]):
                self.assertEqual(self.probe(self.emit(code, payload))["view"], "error")

    def test_hang_beyond_timeout_is_error(self):
        start = time.monotonic()
        r = self.probe("time.sleep(4)\nprint(json.dumps({'result': 'stopped'}))\n", timeout="0.5")
        self.assertLess(time.monotonic() - start, 3.0)
        self.assertEqual(r["view"], "error")
        self.assertTrue(r["timed_out"])


class QuitAppEngineTests(unittest.TestCase):
    """A15: per-instance outcomes on a fake AppControl; force only for survivors."""

    def scenario(self, name):
        return run_json("--selftest-quit-app", name)

    def test_two_instances_one_ignoring_terminate_needs_force(self):
        r = self.scenario("two-one-stubborn")
        self.assertEqual([i["state"] for i in r["after_quit"]], ["exited", "running"])
        self.assertFalse(r["complete_after_quit"])
        self.assertEqual(r["quit_calls"], ["terminate 101", "terminate 102"])
        self.assertGreaterEqual(r["virtual_seconds"], 10.0)
        self.assertEqual(r["force_calls"], ["force 102"])
        self.assertEqual([i["state"] for i in r["after_force"]], ["exited", "force_stopped"])
        self.assertTrue(r["complete_after_force"])

    def test_pid_reused_by_another_bundle_is_never_touched(self):
        r = self.scenario("pid-reused")
        self.assertEqual([i["state"] for i in r["after_quit"]], ["exited", "changed"])
        self.assertEqual(r["quit_calls"], ["terminate 101"])
        self.assertNotIn("force_calls", r)
        self.assertFalse(r["complete_after_quit"])

    def test_relaunched_instance_with_new_launch_date_is_never_touched(self):
        r = self.scenario("relaunched")
        self.assertEqual([i["state"] for i in r["after_quit"]], ["exited", "changed"])
        self.assertEqual(r["quit_calls"], ["terminate 101"])

    def test_force_skips_instances_that_already_quit(self):
        # AppKit can still hand back the handle of an app that just quit; only
        # the instance reported as still running may be force-quit.
        r = self.scenario("lingering-handle")
        self.assertEqual([i["state"] for i in r["after_quit"]], ["exited", "running"])
        self.assertEqual(r["force_calls"], ["force 102"])

    def test_instance_already_gone_is_reported_not_signalled(self):
        r = self.scenario("gone")
        self.assertEqual(r["after_quit"][0]["state"], "already_exited")
        self.assertNotIn("terminate 101", r["quit_calls"])


def app_token(instances, bundle="com.example.containers"):
    raw = {"v": 1, "action": "quit-app", "owner_id": "service:vm:fixture", "bundle_id": bundle,
           "instances": [{"pid": p, "start": [int(l), 0], "launch_date": l} for p, l in instances],
           "snapshot_ts": 1791449998.0}
    return base64.b64encode(json.dumps(raw).encode()).decode()


class QuitAppLockTests(StubCase):
    """S1.5: Swift holds actions.lock and hands it to verify-app by descriptor."""

    VERIFY = """
args = sys.argv[1:]
fd = int(args[args.index('--lock-fd') + 1])
import fcntl
report = {'fd_open': True, 'same_description_relock': False, 'held_against_others': False}
try:
    os.fstat(fd)
except OSError:
    report['fd_open'] = False
if report['fd_open']:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report['same_description_relock'] = True
    except OSError:
        pass
    other = os.open(LOCK, os.O_RDWR)
    try:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        report['held_against_others'] = True
    os.close(other)
open(REPORT, 'w').write(json.dumps(report))
print(json.dumps({'result': RESULT, 'reason': REASON}))
sys.exit(CODE)
"""

    def verify_stub(self, result="verified", reason=None, code=0):
        body = (f"LOCK = {str(self.lock)!r}\nREPORT = {str(self.dir / 'verify.json')!r}\n"
                f"RESULT = {result!r}\nREASON = {reason!r}\nCODE = {code}\n" + self.VERIFY)
        return self.stub(body)

    def setUp(self):
        super().setUp()
        self.lock = self.dir / "coord" / "actions.lock"

    def quit_probe(self, script, timeout=30):
        return run_json("--quit-probe", "--script", script, "--lock-path", str(self.lock),
                        "--token", app_token([(5101, 1791442800.0), (5188, 1791446400.0)]),
                        timeout=timeout)

    def test_verify_app_gets_the_held_lock_descriptor_then_instances_quit(self):
        r = self.quit_probe(self.verify_stub())
        argv = self.argv()
        self.assertEqual(argv[:4], ["act", "verify-app", "--target", argv[3]])
        self.assertIn("--lock-fd", argv)
        report = json.loads((self.dir / "verify.json").read_text())
        self.assertEqual(report, {"fd_open": True, "same_description_relock": True,
                                  "held_against_others": True})
        self.assertEqual(r["view"], "done")
        self.assertEqual(r["states"], ["exited", "exited"])
        self.assertEqual(r["calls"], ["terminate 5101", "terminate 5188"])

    def test_refused_verification_terminates_nothing(self):
        r = self.quit_probe(self.verify_stub("refused", "target_changed", 4))
        self.assertEqual((r["view"], r["reason"]), ("refused", "target_changed"))
        self.assertEqual(r["calls"], [])

    def test_verification_finding_every_instance_gone_terminates_nothing(self):
        r = self.quit_probe(self.verify_stub("already_exited"))
        self.assertEqual(r["states"], ["already_exited", "already_exited"])
        self.assertEqual(r["calls"], [])

    def test_unreadable_verification_terminates_nothing(self):
        r = self.quit_probe(self.stub("print('ok')\nsys.exit(0)\n"))
        self.assertEqual(r["view"], "error")
        self.assertEqual(r["calls"], [])

    def test_lock_held_elsewhere_is_busy_and_verify_never_runs(self):
        self.lock.parent.mkdir(parents=True)
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            start = time.monotonic()
            r = self.quit_probe(self.verify_stub(), timeout=20)
            self.assertGreaterEqual(time.monotonic() - start, 4.5)
        finally:
            os.close(fd)
        self.assertEqual((r["view"], r["reason"]), ("refused", "busy"))
        self.assertIsNone(self.argv())
        self.assertEqual(r["calls"], [])


def luminance(hex_colour):
    r, g, b = (int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


class RenderTests(unittest.TestCase):
    """A20: every fixture renders in both themes from the dynamic palette."""

    def test_every_fixture_renders_in_both_themes(self):
        fixtures = sorted(FIXTURES.glob("*.json"))
        self.assertGreaterEqual(len(fixtures), 12)
        with tempfile.TemporaryDirectory() as out:
            for f in fixtures:
                for theme in ("light", "dark"):
                    with self.subTest(fixture=f.name, theme=theme):
                        png = Path(out) / f"{f.stem}-{theme}.png"
                        line = run_bin("--render", str(png), "--fixture", str(f), f"--{theme}")
                        self.assertTrue(png.exists() and png.stat().st_size > 10_000)
                        bg = line.split("bg=")[1].strip()
                        if theme == "light":
                            self.assertGreater(luminance(bg), 0.7, bg)
                        else:
                            self.assertLess(luminance(bg), 0.2, bg)

    def test_unknown_fixture_view_state_is_rejected(self):
        proc = subprocess.run([BIN, "--render", os.devnull, "--fixture", str(FIXTURES / "overview.json"),
                               "--confirm", "stop-job"], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)


class AccessibilityTests(unittest.TestCase):
    """R5: walks the accessibility tree SwiftUI builds for each fixture."""

    def test_overview_labels_rows_chips_meter_and_status_lines(self):
        rows = a11y("overview.json")
        found = labels(rows)
        self.assertIn("Sampled 2s ago by the live reader", found)
        self.assertIn("Protection partial · 2 heavy processes not started through memmon run", found)
        meter = next(r for r in rows if r["label"] == "Memory in use")
        self.assertEqual(meter["role"], "AXProgressIndicator")
        self.assertEqual(meter["described"], "39.2 of 48 GB")
        owner = next(r for r in rows if r["label"].startswith("Checkout refactor,"))
        self.assertEqual(owner["role"], "AXButton")
        # Line 2 is clipped at two lines on screen; the label carries all of it.
        self.assertIn("Claude · Building · typecheck + 4 workers", owner["label"])
        self.assertIn("ownership confidence: exact", owner["label"])
        self.assertIn("8.9 GB, 3.1 cores", owner["label"])
        self.assertEqual(owner["value"], "collapsed")
        self.assertIn("ownership confidence: inferred",
                      next(l for l in found if l.startswith("Billing API tests,")))
        for sort in ("Sort by Memory", "Sort by CPU", "Sort by Growth"):
            self.assertIn(sort, found)
        self.assertIn("Pause command protection", found)

    def test_unavailable_values_are_spoken_as_unavailable_never_zero(self):
        found = labels(a11y("unavailable.json"))
        row = next(l for l in found if l.startswith("Checkout refactor,"))
        self.assertIn("CPU not available, warming up", row)
        self.assertIn("memory not available, not readable", row)
        self.assertNotIn("0.0 GB", row)
        self.assertIn("Sample time unknown", found)
        meter = next(r for r in a11y("unavailable.json") if r["label"] == "Memory in use")
        self.assertEqual(meter["described"], "not available")

    def test_session_detail_labels_child_jobs_and_kept_marker(self):
        found = labels(a11y("session-detail.json"))
        self.assertIn("Stop build: Typecheck · build in Checkout refactor", found)
        self.assertIn("Stop server: Dev server · port 3000 in Checkout refactor", found)
        self.assertIn("Typecheck · build, 6.8 GB · 5 processes", found)
        self.assertIn("kept running", found)
        self.assertIn("ownership confidence: exact", found)
        self.assertIn("End session Checkout refactor (asks to confirm)", found)

    def test_confirm_overlay_is_modal_and_names_both_choices(self):
        rows = a11y("confirm-stop.json")
        found = labels(rows)
        self.assertIn("Stop typecheck?", found)
        self.assertIn("Cancel, keep the job running", found)
        self.assertIn("Stop build: send stop signal to 5 processes", found)
        # The list behind the scrim is hidden from assistive tech while modal.
        self.assertFalse(any(l.startswith("Checkout refactor,") for l in found))

    def test_partial_outcome_puts_leave_running_before_force(self):
        found = labels(a11y("outcome-partial.json"))
        leave = found.index("Leave the 2 remaining processes running")
        force = found.index("Force stop the 2 remaining processes")
        self.assertLess(leave, force)

    def test_outcome_banners_and_degraded_banner_are_announced(self):
        stopped = a11y("outcome-stopped.json")
        text = " ".join(r["value"] for r in stopped)
        self.assertIn("used memory 2.1 GB lower at the next sample", text)
        self.assertIn("(measured; other apps also change)", text)
        self.assertIn("Outcome", labels(stopped))
        self.assertNotIn("Could not refresh", text)
        self.assertIn("Refresh the process list", labels(a11y("outcome-refused.json")))
        degraded = a11y("degraded.json")
        spoken = [r["label"] or r["value"] for r in degraded]
        self.assertIn("Limited process details: libproc is unavailable (libproc self-check failed), "
                      "so memory comes from top and stop actions are off.", spoken)

    def test_respawned_session_says_so_and_offers_no_force(self):
        rows = a11y("outcome-respawned.json")
        spoken = " ".join(r["label"] + " " + r["value"] for r in rows)
        self.assertIn("Session restarted by Claude — it is running again.", spoken)
        self.assertIn("Refresh the process list", labels(rows))
        self.assertFalse(any("Force" in l for l in labels(rows)))
        self.assertNotIn("still running after 10 s", spoken)

    def test_counts_use_the_captured_total(self):
        spoken = " ".join(r["value"] for r in a11y("outcome-stopped.json"))
        self.assertIn("5 of 5 processes exited", spoken)

    def test_degraded_inventory_offers_no_stop_buttons(self):
        found = labels(a11y("degraded.json"))
        self.assertFalse(any(l.startswith(("Stop build", "Stop server", "End session")) for l in found))

    def test_quit_app_partial_reports_each_instance(self):
        rows = a11y("quit-service-partial.json")
        text = " ".join(r["value"] for r in rows)
        self.assertIn("Instance 1 · quit", text)
        self.assertIn("Instance 2 · still running", text)
        self.assertIn("Force quit the 1 remaining instance", labels(rows))


class HostedPopoverTests(unittest.TestCase):
    """The real popover root, hosted offscreen: size, keyboard and ticker."""

    def host(self, check, fixture):
        return run_json("--selftest-host", check, "--fixture", str(FIXTURES / fixture))

    def test_height_adapts_and_the_popover_follows_it(self):
        small = self.host("size", "small.json")
        large = self.host("size", "overview.json")
        for r in (small, large):
            self.assertEqual(r["preferred"][0], 380)
            self.assertLessEqual(r["preferred"][1], 620)
            self.assertTrue(r["popover_shown"])
            self.assertEqual(r["popover"], r["preferred"])
            self.assertFalse(r["popover_on_a_display"])
        self.assertEqual(large["preferred"][1], 620)
        self.assertLess(small["preferred"][1], large["preferred"][1])

    def test_confirm_starts_on_cancel_ignores_return_and_esc_dismisses(self):
        r = self.host("keys", "confirm-stop.json")
        self.assertEqual((r["phase_before"], r["focus"]), ("ask", "safe"))
        self.assertEqual(r["after_return_phase"], "ask")
        self.assertEqual(r["after_return_actions"], [])
        self.assertEqual(r["after_esc_phase"], "closed")
        self.assertEqual(r["actions"], [])

    def test_partial_starts_on_leave_running_and_return_never_forces(self):
        r = self.host("keys", "outcome-partial.json")
        self.assertEqual((r["phase_before"], r["focus"]), ("partial", "safe"))
        self.assertNotIn("force", r["after_return_actions"])
        self.assertEqual(r["after_return_phase"], "partial")
        self.assertEqual(r["after_esc_phase"], "closed")
        self.assertEqual(r["actions"], [])

    def test_inside_the_transient_popover_esc_closes_only_the_overlay(self):
        for fixture, phase in (("confirm-stop.json", "ask"), ("outcome-partial.json", "partial")):
            with self.subTest(fixture=fixture):
                r = self.host("popover-keys", fixture)
                self.assertFalse(r["popover_on_a_display"])
                self.assertEqual(r["focus"], "safe")
                self.assertEqual(r["after_return_phase"], phase)
                self.assertEqual(r["after_esc_phase"], "closed")
                self.assertTrue(r["popover_open_after_esc"])
                self.assertEqual(r["actions"], [])

    def test_freshness_ticker_marks_a_live_sample_stale_after_95_s(self):
        r = self.host("ticker", "overview.json")
        self.assertEqual(r["before"], "Sampled 2s ago by the live reader")
        self.assertTrue(r["after"].endswith(", stale"), r["after"])
        self.assertGreaterEqual(r["ticks"], 1)


if __name__ == "__main__":
    unittest.main()
