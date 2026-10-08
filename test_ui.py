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

# py's real owners generator: the contract tests run against it, never skip.
import re
from unittest import mock

import memmon
import memmon_act
import memmon_owners
import memmon_procs
import testkit
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
    return a11y_path(FIXTURES / fixture, *flags)


def a11y_path(path, *flags):
    rows = []
    for line in run_bin("--a11y-dump", "--fixture", str(path), *flags).splitlines():
        parts = line.split("\t")
        if len(parts) >= 4 and parts[0].isdigit():
            parts += [""] * (6 - len(parts))
            rows.append({"depth": int(parts[0]), "role": parts[1], "label": parts[2],
                         "value": parts[3], "described": parts[4], "height": parts[5] or "0"})
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
    """stdout JSON is decoded first and must agree with the exit code."""

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
    """Per-instance outcomes on a fake AppControl; force only for survivors."""

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

    def test_no_running_application_is_not_an_app_and_never_success(self):
        r = self.scenario("not-an-app")
        self.assertEqual([i["state"] for i in r["after_quit"]], ["exited", "not_an_app"])
        self.assertFalse(r["complete_after_quit"])
        self.assertNotIn("terminate 102", r["quit_calls"])

    def test_appkit_dropping_the_handle_is_not_an_exit(self):
        r = self.scenario("appkit-says-gone")
        self.assertEqual(r["after_quit"][1]["state"], "running")
        self.assertFalse(r["complete_after_quit"])
        self.assertFalse(r["complete_after_force"])

    def test_unreadable_instance_is_unverified_never_quit_or_forced(self):
        r = self.scenario("unreadable")
        self.assertEqual([i["state"] for i in r["after_quit"]], ["exited", "unverified"])
        self.assertFalse(r["complete_after_quit"])
        self.assertNotIn("force_calls", r)

    def test_instance_exiting_at_quit_time_is_already_quit_not_an_app(self):
        r = self.scenario("raced-exit")
        self.assertEqual([i["state"] for i in r["after_quit"]], ["exited", "already_exited"])
        self.assertTrue(r["complete_after_quit"])
        self.assertNotIn("terminate 102", r["quit_calls"])

    def test_instance_that_quit_before_force_is_decided_by_memmon(self):
        r = self.scenario("quit-before-force")
        self.assertEqual(r["force_calls"], [])
        self.assertEqual([i["state"] for i in r["after_force"]], ["exited", "exited"])
        self.assertTrue(r["complete_after_force"])

    def test_failed_final_check_is_unverified_never_success(self):
        r = self.scenario("verify-fails")
        self.assertEqual({i["state"] for i in r["after_quit"]}, {"unverified"})
        self.assertFalse(r["complete_after_quit"])


def app_token(instances, bundle="com.example.containers"):
    raw = {"v": 1, "action": "quit-app", "owner_id": "service:vm:docker-desktop", "bundle_id": bundle,
           "instances": [{"pid": p, "start": [int(l), 0], "launch_date": l} for p, l in instances],
           "snapshot_ts": 1791449998.0}
    return base64.b64encode(json.dumps(raw).encode()).decode()


class QuitAppLockTests(StubCase):
    """Swift holds actions.lock and hands it to verify-app by descriptor."""

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
calls = REPORT + '.calls'
n = int(open(calls).read()) + 1 if os.path.exists(calls) else 1
open(calls, 'w').write(str(n))
open(REPORT if n == 1 else f'{REPORT}.{n}', 'w').write(json.dumps(report))
open(f'{REPORT}.argv.{n}', 'w').write(json.dumps(args))
if SLEEP:
    # A slow memmon that would not die on SIGTERM either: only never
    # signalling it lets the caller return at once.
    import signal
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(SLEEP)
r = RESPONSES[min(n, len(RESPONSES)) - 1]
out = {'result': r['result'], 'reason': r['reason'],
       'instances': [{'pid': p, 'status': st} for p, st in r['status'].items()]}
if r['result'] in ('verified', 'already_exited'):
    out['token'] = f'fresh-{n}'
print(json.dumps(out))
sys.exit(r['code'])
"""

    def verify_stub(self, result="verified", reason=None, code=0, later=None, sleep=0, responses=None):
        """memmon verify-app stand-in. Call n answers responses[n-1] (the last
        repeats) and, when it verifies, mints the token `fresh-n`."""
        if responses is None:
            responses = [
                {"result": result, "reason": reason, "code": code,
                 "status": {5101: "running", 5188: "running"}},
                {"result": "verified", "reason": None, "code": 0,
                 "status": later or {5101: "exited", 5188: "exited"}}]
        body = (f"LOCK = {str(self.lock)!r}\nREPORT = {str(self.dir / 'verify.json')!r}\n"
                f"SLEEP = {sleep}\nRESPONSES = {responses!r}\n" + self.VERIFY)
        return self.stub(body)

    def verify_target(self, n):
        args = json.loads((self.dir / f"verify.json.argv.{n}").read_text())
        return args[args.index("--target") + 1]

    def verify_calls(self):
        p = self.dir / "verify.json.calls"
        return int(p.read_text()) if p.exists() else 0

    def setUp(self):
        super().setUp()
        self.lock = self.dir / "coord" / "actions.lock"

    def quit_probe(self, script, timeout=30, act_timeout=None, extra=()):
        extra = (["--timeout", str(act_timeout)] if act_timeout else []) + list(extra)
        return run_json("--quit-probe", "--script", script, "--lock-path", str(self.lock),
                        "--token", app_token([(5101, 1791442800.0), (5188, 1791446400.0)]),
                        *extra, timeout=timeout)

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
        # The lock is still held at each terminate(), and memmon is asked a
        # second time, after the watch, before anything is called quit.
        self.assertEqual(r["calls"], ["terminate 5101 locked", "terminate 5188 locked"])
        self.assertEqual(self.verify_calls(), 2)
        # The second check, made after the 10 s watch, still finds the lock
        # held against every other process.
        after_watch = json.loads((self.dir / "verify.json.2").read_text())
        self.assertEqual(after_watch, {"fd_open": True, "same_description_relock": True,
                                       "held_against_others": True})
        self.assertTrue(r["lock_free_after"])

    def test_post_watch_check_uses_the_token_the_pre_quit_check_minted(self):
        tok = app_token([(5101, 1791442800.0), (5188, 1791446400.0)])
        r = self.quit_probe(self.verify_stub())
        self.assertEqual(r["states"], ["exited", "exited"])
        self.assertEqual(self.verify_target(1), tok)
        self.assertEqual(self.verify_target(2), "fresh-1")
        self.assertEqual(r["token"], "fresh-2")

    FORCE_FLOW = [
        {"result": "verified", "reason": None, "code": 0, "status": {5101: "running", 5188: "running"}},
        {"result": "verified", "reason": None, "code": 0, "status": {5101: "exited", 5188: "running"}},
        None,   # the check right before Force
        {"result": "already_exited", "reason": None, "code": 0, "status": {5101: "exited", 5188: "exited"}},
    ]

    def force_probe(self, before_force):
        flow = list(self.FORCE_FLOW)
        flow[2] = before_force
        return self.quit_probe(self.verify_stub(responses=flow),
                               extra=["--stubborn", "5188", "--force-after"])

    def test_force_rechecks_with_the_fresh_token_under_the_lock(self):
        r = self.force_probe({"result": "verified", "reason": None, "code": 0,
                              "status": {5101: "exited", 5188: "running"}})
        self.assertEqual(r["states"], ["exited", "running"])
        self.assertEqual(self.verify_target(3), "fresh-2")
        self.assertEqual(self.verify_target(4), "fresh-3")
        self.assertEqual(r["force_calls"], ["force 5188 locked"])
        self.assertEqual(r["force_states"], ["exited", "force_stopped"])
        held = json.loads((self.dir / "verify.json.3").read_text())
        self.assertTrue(held["held_against_others"])

    def test_force_refused_by_the_last_check_signals_nothing(self):
        for reason in ("hosts_sessions", "instance_changed", "stale_token", "protected"):
            with self.subTest(reason=reason):
                for f in self.dir.glob("verify.json*"):
                    f.unlink()
                r = self.force_probe({"result": "refused", "reason": reason, "code": 4, "status": {}})
                self.assertEqual(r["force_view"], "refused")
                self.assertEqual(r["force_reason"], reason)
                self.assertEqual(r["force_calls"], [])

    def test_instance_gone_before_force_is_not_signalled(self):
        r = self.force_probe({"result": "already_exited", "reason": None, "code": 0,
                              "status": {5101: "exited", 5188: "exited"}})
        self.assertEqual(r["force_calls"], [])
        self.assertEqual(r["force_states"], ["exited", "exited"])

    def test_unverified_recheck_is_never_success(self):
        r = self.quit_probe(self.verify_stub(later={5101: "exited", 5188: "unverified"}))
        self.assertEqual(r["states"], ["exited", "unverified"])

    def test_memmon_not_appkit_decides_an_instance_exited(self):
        r = self.quit_probe(self.verify_stub(later={5101: "exited", 5188: "running"}))
        self.assertEqual(r["states"], ["exited", "running"])

    def test_verify_app_timeout_returns_at_once_and_frees_the_lock(self):
        r = self.quit_probe(self.verify_stub(sleep=4), act_timeout=0.5)
        self.assertEqual(r["view"], "error")
        self.assertLess(r["seconds"], 2.0)
        self.assertTrue(r["lock_free_after"])
        self.assertEqual(r["calls"], [])

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
    """Every fixture renders in both themes from the dynamic palette."""

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
                        bg = line.split("bg=")[1].split()[0]
                        dark_px = int(line.split("dark_tokens=")[1].split()[0])
                        if theme == "light":
                            self.assertEqual(dark_px, 0, "a dark-only surface colour in the light render")
                        else:
                            self.assertGreater(dark_px, 1000)
                        if theme == "light":
                            self.assertGreater(luminance(bg), 0.7, bg)
                        else:
                            self.assertLess(luminance(bg), 0.2, bg)

    def test_render_without_a_fixture_fails(self):
        proc = subprocess.run([BIN, "--render", os.devnull], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--fixture", proc.stderr)

    def test_unknown_fixture_view_state_is_rejected(self):
        proc = subprocess.run([BIN, "--render", os.devnull, "--fixture", str(FIXTURES / "overview.json"),
                               "--confirm", "stop-job"], capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)


class AccessibilityTests(unittest.TestCase):
    """Walks the accessibility tree SwiftUI builds for each fixture."""

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
        self.assertIn("Claude · Building · typecheck", owner["label"])
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
        # Nothing measured is one state, not a memory reason plus a CPU one.
        self.assertIn("memory and CPU not available, not measured", row)
        self.assertNotIn("warming up", row)
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
        force = found.index("Force stop 2 of the 2 remaining processes")
        self.assertLess(leave, force)

    def spoken(self, fixture):
        return " ".join(r["label"] + " " + r["value"] for r in a11y(fixture))

    def test_force_counts_only_what_its_token_names(self):
        rows = a11y("outcome-partial-outside.json")
        text = " ".join(r["label"] + " " + r["value"] for r in rows)
        self.assertIn("1 of 2 can be force-stopped; the other is outside what was stopped and won't be signalled.", text)
        self.assertIn("Force stop 1 of the 2 remaining processes", labels(rows))

    def test_survivors_are_never_reported_as_success(self):
        text = self.spoken("outcome-outside-force.json")
        self.assertIn("Typecheck partly stopped · 1 force-stopped · 1 still running outside what was stopped", text)
        listed = self.spoken("outcome-force-with-survivors.json")
        self.assertIn("partly stopped", listed)
        self.assertNotIn("force-stopped ·", listed.split("partly stopped")[0][-40:])
        self.assertNotIn("Typecheck force-stopped", listed)
        for fixture in ("outcome-outside-force.json", "outcome-force-with-survivors.json"):
            self.assertFalse(any("Force" in l for l in labels(a11y(fixture))), fixture)

    def test_app_force_that_forced_nothing_is_not_called_force_quit(self):
        text = self.spoken("quit-service-forced-none.json")
        self.assertIn("Container VM quit · nothing needed forcing · 2 of 2 instances exited", text)
        self.assertNotIn("force-quit", text)

    def test_runner_wrappers_are_never_outside_what_was_stopped(self):
        rows = a11y("outcome-partial-runner.json")
        text = " ".join(r["label"] + " " + r["value"] for r in rows)
        self.assertIn("1 of 2 can be force-stopped.", text)
        self.assertIn("1 is a memmon run wrapper; it exits once its job ends.", text)
        self.assertNotIn("outside what was stopped", text)
        self.assertIn("Force stop 1 of the 2 remaining processes", labels(rows))
        forced = self.spoken("outcome-force-runner.json")
        self.assertIn("Typecheck partly stopped · 1 force-stopped · 1 memmon run wrapper — exits once its job ends",
                      forced)
        self.assertNotIn("outside what was stopped", forced)

    def test_protected_survivors_that_ended_are_not_already_exited(self):
        text = self.spoken("outcome-force-protected-gone.json")
        self.assertIn("1 ended by force · 1 had already exited · 1 ended on its own while protected", text)

    def test_self_exited_survivors_are_not_counted_as_force_stopped(self):
        text = self.spoken("outcome-force-self-exited.json")
        self.assertIn("Typecheck force-stopped · 1 ended by force · 1 had already exited", text)
        self.assertNotIn("2 force-stopped", text)

    def test_unlisted_survivors_are_counted_never_dropped(self):
        text = self.spoken("outcome-outside-unlisted.json")
        # One of the two named survivors ended by itself: it is not force-stopped.
        self.assertIn("Typecheck partly stopped · 1 force-stopped · 1 had already exited · "
                      "3 more still running that memmon could not list", text)
        self.assertNotIn("0 still running", text)

    def test_root_exited_says_nothing_was_signalled(self):
        text = self.spoken("outcome-root-exited.json")
        self.assertIn("Typecheck had exited — but 2 of its processes are still running · nothing was signalled", text)
        self.assertIn("Refresh the process list", labels(a11y("outcome-root-exited.json")))

    def test_stale_force_token_says_the_result_is_old(self):
        text = self.spoken("outcome-force-stale.json")
        self.assertIn("Not force-stopped — this result is more than 2 minutes old.", text)
        self.assertNotIn("since the list was sampled", text)

    def test_quit_app_with_a_helper_instance_is_not_reported_quit(self):
        text = self.spoken("quit-service-not-an-app.json")
        self.assertIn("Not fully quit", text)
        self.assertIn("instance 2 is not an app memmon can quit, so it was left alone.", text)
        self.assertNotIn("Refresh and try again", text)
        self.assertNotIn("Container VM quit", text)

    def test_hosts_sessions_refusal_is_a_refusal(self):
        text = self.spoken("quit-refused-hosts.json")
        self.assertIn("Not quit — this app hosts agent sessions; quit it from the app itself.", text)
        self.assertNotIn("quit ·", text)

    def test_watch_error_is_a_note_on_a_stop_that_stands(self):
        text = self.spoken("outcome-watch-error.json")
        self.assertIn("Checkout refactor stopped · 9 of 9 processes exited", text)
        self.assertIn("(measured; other apps also change; memmon could not keep watching for a restart)", text)
        self.assertNotIn("Result unknown", text)

    def test_helpers_only_app_row_offers_nothing_to_quit(self):
        found = labels(a11y("helpers-only.json"))
        row = next(l for l in found if l.startswith("Example Widgets,"))
        self.assertIn("Helpers only — nothing to quit", row)
        self.assertFalse(any(l.startswith("Quit Example Widgets") for l in found))

    def test_wired_outcome_fixtures_render_their_copy(self):
        self.assertIn("Result unknown — memmon did not answer within 25 s.", self.spoken("outcome-timeout.json"))
        self.assertIn("is protected", self.spoken("outcome-refused-protected.json"))
        forced = self.spoken("quit-service-forced.json")
        self.assertIn("Container VM force-quit · 2 of 2 instances exited", forced)
        self.assertIn("force-quit (used memory updates at the next sample)", forced)

    def test_hosting_app_offers_no_quit_and_terminal_quit_warns(self):
        hosts = labels(a11y("hosts-detail.json"))
        row = next(l for l in hosts if l.startswith("Example Terminal,"))
        self.assertIn("Hosts 2 sessions", row)
        self.assertFalse(any(l.startswith("Quit Example Terminal") for l in hosts))
        self.assertIn("Hosts 2 sessions — quit it from the app itself", self.spoken("hosts-detail.json"))
        self.assertIn("Quitting a terminal ends every shell and agent session in it.",
                      self.spoken("quit-terminal.json"))

    def test_managed_jobs_card_lists_running_and_waiting_jobs(self):
        found = [r["label"] or r["value"] for r in a11y("managed-jobs.json")]
        self.assertIn("Managed jobs", found)
        self.assertIn("Managed job acme-web typecheck · running · 42s, heavy, command running", found)
        self.assertIn("Managed job acme-api tests · waiting · 12s, heavy, resource held by acme-web typecheck",
                      found)

    def test_cpu_total_only_with_full_coverage(self):
        self.assertIn("CPU partly measured", self.spoken("small.json"))
        self.assertIn("CPU partly measured", self.spoken("overview.json"))
        self.assertIn("CPU 3.1 / 18 cores", self.spoken("helpers-only.json"))
        # Nothing measured: coverage is null and memmon says why.
        self.assertIn("CPU warming up", self.spoken("warming-up.json"))
        self.assertIn("CPU not measured", self.spoken("degraded.json"))

    def test_unattributed_cpu_needs_every_tree_fully_measured(self):
        found = labels(a11y("unattributed-partial.json"))
        row = next(l for l in found if l.startswith("Unattributed,"))
        self.assertIn("CPU not available, partly measured", row)

    def test_kept_owners_are_named_by_kind(self):
        self.assertIn("2 nested sessions and 1 app or service kept running",
                      self.spoken("outcome-kept.json"))

    def test_force_with_named_survivors_stays_partial(self):
        text = self.spoken("outcome-force-survivors.json")
        self.assertIn("Typecheck partly stopped · 1 force-stopped · 1 process still running", text)
        self.assertFalse(any("Force" in l for l in labels(a11y("outcome-force-survivors.json"))))

    def test_partial_landing_while_closed_is_a_banner_not_a_force(self):
        for fixture, title in (("outcome-partial-closed.json", "Typecheck partly stopped"),
                               ("quit-service-partial-closed.json", "Container VM still running")):
            with self.subTest(fixture=fixture):
                self.assertIn(title, self.spoken(fixture))
                self.assertFalse(any(l.startswith("Force") for l in labels(a11y(fixture))))

    def test_refusal_vocabulary_and_constants_match_memmon(self):
        src = (ROOT / "memmon_act.py").read_text()
        reasons = sorted(set(re.findall(r'Refused\("([a-z_]+)"\)', src))
                         | set(re.findall(r'"reason": "([a-z_]+)"', src)))
        self.assertIn("hosts_sessions", reasons)
        r = run_json("--constants", "--reasons", ",".join(reasons))
        for reason in reasons:
            self.assertFalse(r["refusals"][reason].startswith("memmon declined"), reason)
        # The menu bar outlives memmon's own act budget, and never offers an
        # app Force longer than memmon accepts a token.
        self.assertGreater(r["act_timeout"], memmon_act.ACT_BUDGET_S)
        self.assertLessEqual(r["force_ttl"], memmon_owners.TOKEN_TTL_S)

    def test_rows_stay_one_line_and_the_label_keeps_everything(self):
        rows = a11y("long-activity.json")
        short = next(r for r in rows if r["label"].startswith("Checkout refactor,"))
        long = next(r for r in rows if r["label"].startswith("Long activity,"))
        self.assertIn("very long label " * 6, long["label"])
        self.assertLessEqual(abs(float(long["height"]) - float(short["height"])), 1)

    def test_pending_retries_listed_in_the_gate_section(self):
        rows = a11y("overview.json")
        spoken = [r["label"] or r["value"] for r in rows]
        self.assertIn("1 blocked command waiting to retry", spoken)
        self.assertTrue(any(l.startswith("pnpm typecheck · Checkout refactor · blocked at CRITICAL")
                            for l in spoken))
        self.assertIn("Pause command protection", labels(rows))

    def test_a_long_blocked_command_shows_short_and_can_be_dismissed(self):
        rows = a11y("stale-paused.json")
        spoken = [r["label"] or r["value"] for r in rows]
        # VoiceOver keeps the whole line; the card shows only the operation.
        self.assertTrue(any(l.startswith("timeout 1500 pnpm test:affected > /tmp/") for l in spoken))
        self.assertIn("Dismiss blocked pnpm test:affected", labels(rows))

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
        self.assertIn("Limited process details: libproc self-check failed, "
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

    def test_contract_shapes_conversation_job_codex_frontend_and_unattributed(self):
        detail = labels(a11y("session-detail.json"))
        self.assertIn("Conversation, 1.2 GB · stays open when you stop a build", detail)
        self.assertFalse(any(l.startswith("Stop job: Conversation") for l in detail))
        shared = labels(a11y("shared-detail.json"))
        # The generator's own title for a Codex frontend.
        title = "Codex thread · runs in Codex daemon"
        self.assertIn(f'owner.title = "{title}"', (ROOT / "memmon_owners.py").read_text())
        pointer = next(l for l in shared if l.startswith(title + ","))
        self.assertIn("Codex thread · 1 process", pointer)
        self.assertIn("ownership confidence: shared", pointer)
        growth = labels(a11y("growth-sort.json"))
        unattributed = next(l for l in growth if l.startswith("Unattributed,"))
        self.assertIn("growth not available, not enough history, 1.8 GB", unattributed)

    def test_degraded_inventory_offers_no_stop_buttons(self):
        found = labels(a11y("degraded.json"))
        self.assertFalse(any(l.startswith(("Stop build", "Stop server", "End session")) for l in found))

    def test_quit_app_partial_reports_each_instance(self):
        rows = a11y("quit-service-partial.json")
        text = " ".join(r["value"] for r in rows)
        self.assertIn("Instance 1 · quit", text)
        self.assertIn("Instance 2 · still running", text)
        self.assertIn("Force quit the 1 remaining instance", labels(rows))


def effective(fixture):
    """A fixture with its _base chain applied, as the binary loads it."""
    d = json.loads(Path(fixture).read_text())
    if "_base" in d:
        base = effective(Path(fixture).parent / d["_base"])
        base.update({k: v for k, v in d.items() if k not in ("_base", "_view")})
        return base
    return {k: v for k, v in d.items() if k != "_view"}


class SectionTests(unittest.TestCase):
    """Owners grouped by memmon's category into collapsible sections."""

    ORDER = ["claude", "codex", "job", "browser", "dev", "app", "service", "background"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def probe(self, fixture="sections.json", payload=None, view=None):
        if payload is not None:
            path = Path(self.tmp.name) / "payload.json"
            path.write_text(json.dumps(dict(payload, _view=view or {})))
            fixture = path
        else:
            fixture = FIXTURES / fixture
        return run_json("--sections-probe", "--fixture", str(fixture))

    def by_section(self, r):
        return {s["section"]: s for s in r["sections"]}

    def test_sections_follow_category_in_a_fixed_order(self):
        r = self.probe()
        self.assertEqual([s["section"] for s in r["sections"]], self.ORDER)
        sec = self.by_section(r)
        self.assertEqual([s["title"] for s in r["sections"]],
                         ["Claude sessions", "Codex", "Managed jobs", "Browsers", "Terminals & editors",
                          "Mac apps", "Shared services", "Background"])
        self.assertIn("app:com.example.browser", sec["browser"]["owners"])
        self.assertIn("app:com.example.editor", sec["dev"]["owners"])
        self.assertIn("job:run-fixture-1", sec["job"]["owners"])
        self.assertIn("codex-app:7001.1791440000", sec["service"]["owners"])
        self.assertIn("unknown:*", sec["background"]["owners"])
        # A terminal hosting a session is never background, however small.
        self.assertIn("app:com.example.terminal", sec["dev"]["owners"])

    def test_kind_decides_the_section_when_category_is_missing(self):
        payload = effective(FIXTURES / "sections.json")
        for o in payload["owners"]:
            o.pop("category", None)
        sec = self.by_section(self.probe(payload=payload))
        self.assertIn("claude:fixture-a", sec["claude"]["owners"])
        self.assertIn("codex:fixture-b", sec["codex"]["owners"])
        self.assertIn("job:run-fixture-1", sec["job"]["owners"])
        self.assertIn("app:com.example.browser", sec["app"]["owners"])     # no category: an app
        self.assertIn("codex-app:7001.1791440000", sec["service"]["owners"])
        self.assertNotIn("browser", sec)
        self.assertNotIn("dev", sec)

    def small(self, **change):
        payload = effective(FIXTURES / "small.json")
        helper = {"owner_id": "app:com.example.probe", "kind": "app", "agent": "app", "title": "Probe Helper",
                  "category": "app", "footprint_bytes": 50 * 1024 ** 2, "member_count": 1, "actions": [],
                  "token": None, "hosts": [], "jobs": [], "confidence": "exact"}
        helper.update(change)
        payload["owners"] = payload["owners"] + [helper]
        return self.probe(payload=payload)["small"]["app:com.example.probe"]

    def test_small_needs_every_condition(self):
        self.assertTrue(self.small())
        self.assertTrue(self.small(category="service"))
        self.assertTrue(self.small(actions=["quit-app"]))                     # offered, but no token
        self.assertFalse(self.small(category="claude"))
        self.assertFalse(self.small(actions=["quit-app"], token="tok"))      # an action is available
        self.assertFalse(self.small(hosts=["claude:fixture-c"]))
        self.assertFalse(self.small(footprint_bytes=100 * 1024 ** 2))         # not under 100 MiB
        self.assertFalse(self.small(footprint_bytes=None))                    # unknown is never small

    def test_only_agent_sections_start_open(self):
        sec = self.by_section(self.probe())
        self.assertEqual({k for k, v in sec.items() if v["open"]}, {"claude", "codex"})

    def test_an_open_section_shows_six_rows_then_show_more(self):
        sec = self.by_section(self.probe("sections-open.json"))
        self.assertEqual((len(sec["app"]["shown"]), sec["app"]["hidden"]), (6, 2))
        self.assertEqual(sec["app"]["shown"], sec["app"]["owners"][:6])
        found = labels(a11y("sections-open.json"))
        self.assertIn("Show 2 more in Mac apps", found)
        everything = self.by_section(self.probe("sections-background.json"))["background"]
        self.assertEqual((everything["hidden"], everything["shown"]), (0, everything["owners"]))

    def test_a_held_owner_opens_its_section_and_is_never_hidden(self):
        payload = effective(FIXTURES / "sections.json")
        widget = self.by_section(self.probe(payload=payload, view={"select": "app:com.example.widgets"}))
        self.assertTrue(widget["background"]["open"])
        self.assertIn("app:com.example.widgets", widget["background"]["shown"])
        last = self.by_section(self.probe(payload=payload))["app"]["owners"][-1]
        app = self.by_section(self.probe(payload=payload, view={"select": last}))["app"]
        self.assertTrue(app["open"])
        self.assertEqual(app["hidden"], 0)

    def test_header_total_is_the_sum_of_known_footprints(self):
        payload = effective(FIXTURES / "sections.json")
        fp = {o["owner_id"]: o.get("footprint_bytes") for o in payload["owners"]}
        unknown = sum(v or 0 for k, v in fp.items() if k.startswith("unknown:"))
        for s in self.probe()["sections"]:
            want = sum(unknown if oid == "unknown:*" else (fp[oid] or 0) for oid in s["owners"])
            self.assertAlmostEqual(s["total"], want, delta=1, msg=s["section"])

    def test_header_total_with_an_unmeasured_row_is_a_lower_bound(self):
        payload = effective(FIXTURES / "sections.json")
        app = next(o for o in payload["owners"]
                   if o.get("category") == "app" and o.get("actions"))
        app["footprint_bytes"], app["footprint_reason"] = None, "not measured"
        path = Path(self.tmp.name) / "partial.json"
        path.write_text(json.dumps(payload))
        header = next(l for l in labels(a11y_path(path)) if l.startswith("Mac apps,"))
        self.assertIn("at least ", header)
        claude = next(l for l in labels(a11y_path(path)) if l.startswith("Claude sessions,"))
        self.assertNotIn("at least", claude)

    def test_a_tiny_footprint_is_spoken_as_under_a_tenth(self):
        found = labels(a11y("sections-background.json"))
        row = next(l for l in found if l.startswith("Example Updater,"))
        self.assertIn("less than 0.1 GB", row)
        self.assertNotIn("0.0 GB", row)

    def test_headers_and_rows_say_everything_the_old_rows_did(self):
        found = labels(a11y("sections-open.json"))
        self.assertIn("Mac apps, 8 owners, 6.4 GB, expanded", found)
        self.assertIn("Browsers, 2 owners, 5.9 GB, collapsed", found)
        self.assertIn("Background, 9 small owners and 6 unattributed processes, 2.1 GB, expanded", found)
        row = next(l for l in found if l.startswith("Checkout refactor,"))
        self.assertEqual(row, "Checkout refactor, Claude · Building · typecheck, acme-web · checkout worktree, "
                              "ownership confidence: exact, 8.9 GB, 3.1 cores")
        helper = next(l for l in found if l.startswith("Example Widgets,"))
        self.assertIn("ownership confidence: exact", helper)


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
        # Tab and shift-Tab never leave the overlay's two buttons.
        self.assertEqual(r["tab_trail"], ["act", "safe", "act", "safe", "act"])
        self.assertEqual(r["after_return_phase"], "ask")
        self.assertEqual(r["after_return_actions"], [])
        self.assertEqual(r["after_esc_phase"], "closed")
        self.assertEqual(r["actions"], [])

    def test_partial_starts_on_leave_running_and_return_never_forces(self):
        r = self.host("keys", "outcome-partial.json")
        self.assertEqual((r["phase_before"], r["focus"]), ("partial", "safe"))
        self.assertTrue(all(f in ("safe", "act") for f in r["tab_trail"]), r["tab_trail"])
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

    def test_leaving_a_partial_keeps_its_outcome(self):
        r = self.host("keys", "outcome-partial.json")
        self.assertIn("partly stopped", r["after_esc_banner"])
        closed = self.host("close", "outcome-partial.json")
        self.assertEqual(closed["phase"], "closed")
        self.assertIn("partly stopped", closed["banner"])

    def test_app_force_is_refused_after_120_s(self):
        fresh = run_json("--selftest-host", "force-ttl", "--fixture", str(FIXTURES / "quit-service-partial.json"),
                         "--age", "30")
        self.assertEqual(fresh["actions"], ["force"])
        old = run_json("--selftest-host", "force-ttl", "--fixture", str(FIXTURES / "quit-service-partial.json"),
                       "--age", "121")
        self.assertEqual(old["actions"], [])
        self.assertEqual(old["phase"], "closed")
        self.assertIn("more than 2 minutes old", old["banner"])

    def test_app_force_clock_defaults_to_one_that_counts_sleep(self):
        src = (ROOT / "MemmonBar.swift").read_text()
        self.assertIn("var clock: () -> Double = { Double(clock_gettime_nsec_np(CLOCK_MONOTONIC)) / 1e9 }", src)
        before = time.clock_gettime(time.CLOCK_MONOTONIC)
        probed = float(run_bin("--clock-probe").strip())
        after = time.clock_gettime(time.CLOCK_MONOTONIC)
        self.assertTrue(before - 1 <= probed <= after + 1, (before, probed, after))
        # On a Mac that has slept since boot, the uptime clock lags behind.
        uptime = time.clock_gettime(time.CLOCK_UPTIME_RAW)
        if after - uptime > 10:
            self.assertGreater(abs(probed - uptime), 5)

    def test_app_force_window_counts_time_asleep(self):
        fixture = str(FIXTURES / "quit-service-partial.json")
        awake = run_json("--selftest-host", "force-ttl", "--fixture", fixture, "--sleep", "30")
        self.assertEqual(awake["actions"], ["force"])
        slept = run_json("--selftest-host", "force-ttl", "--fixture", fixture, "--sleep", "3600")
        self.assertEqual(slept["actions"], [])
        self.assertIn("more than 2 minutes old", slept["banner"])

    def rebind(self, nxt, fixture="confirm-stop.json"):
        return run_json("--selftest-host", "rebind", "--fixture", str(FIXTURES / fixture),
                        "--next", str(FIXTURES / "next" / nxt))

    def test_top_level_managed_job_confirm_names_the_job_once(self):
        found = labels(a11y("confirm-managed-job.json"))
        self.assertIn("Stop acme-web typecheck?", found)
        text = " ".join(found)
        self.assertNotIn("in acme-web typecheck", text)
        self.assertNotIn("keeps running", text)

    def test_top_level_managed_job_confirm_follows_its_owner(self):
        same = self.rebind("rebind-managed-same.json", "confirm-managed-job.json")
        self.assertEqual(same["phase"], "ask")
        self.assertEqual(same["job_token"], "fresh-managed-token")
        gone = self.rebind("rebind-managed-gone.json", "confirm-managed-job.json")
        self.assertEqual(gone["phase"], "closed")
        self.assertIn("can no longer be stopped from here", gone["banner"])

    def test_confirm_closes_when_its_action_changed(self):
        kind = self.rebind("rebind-job-kind-changed.json")
        self.assertEqual(kind["phase"], "closed")
        self.assertIn("changed while this was open", kind["banner"])
        hosts = self.rebind("rebind-quit-hosts.json", "quit-service.json")
        self.assertEqual(hosts["phase"], "closed")
        self.assertIn("Not quit — Container VM now hosts agent sessions", hosts["banner"])
        # Per kind only: a stop-command confirm has no token and stays open.
        command = self.rebind("rebind-stop-command.json", "stop-command.json")
        self.assertEqual(command["phase"], "ask")

    def test_a_real_refresh_rebinds_the_open_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "payload.json"
            stub = Path(tmp) / "memmon_stub.py"
            stub.write_text(f"print(open({str(payload)!r}).read())\n")
            def go(nxt):
                return run_json("--selftest-host", "refresh-rebind", "--fixture", str(FIXTURES / "confirm-stop.json"),
                                "--next", str(FIXTURES / "next" / nxt), "--script", str(stub),
                                "--payload", str(payload), timeout=60)
            same = go("rebind-same.json")
            self.assertEqual((same["phase"], same["job_token"]), ("ask", "fresh-build-token"))
            changed = go("rebind-changed.json")
            self.assertEqual(changed["phase"], "closed")
            self.assertIn("Nothing done", changed["banner"])

    def test_partial_after_the_popover_closed_is_a_banner(self):
        r = run_json("--selftest-host", "closed-then-partial", "--fixture", str(FIXTURES / "confirm-stop.json"),
                     "--outcome", str(FIXTURES / "outcomes" / "partial.json"))
        self.assertEqual(r["phase"], "closed")
        self.assertIn("partly stopped", r["banner"])

    def test_open_confirm_follows_a_refresh_or_closes(self):
        same = self.rebind("rebind-same.json")
        self.assertEqual(same["phase"], "ask")
        self.assertEqual(same["job_token"], "fresh-build-token")
        self.assertEqual(same["owner_token"], "fresh-session-token")
        changed = self.rebind("rebind-changed.json")
        self.assertEqual(changed["phase"], "closed")
        self.assertIn("Nothing done", changed["banner"])
        gone = self.rebind("rebind-job-gone.json")
        self.assertEqual(gone["phase"], "closed")
        self.assertIn("no longer running", gone["banner"])

    def test_freshness_ticker_marks_a_live_sample_stale_after_95_s(self):
        r = self.host("ticker", "overview.json")
        self.assertEqual(r["before"], "Sampled 2s ago by the live reader")
        self.assertEqual(r["after_5s"], "Sampled 7s ago by the live reader")
        self.assertTrue(r["after"].endswith(", stale"), r["after"])
        self.assertGreaterEqual(r["ticks"], 1)


class RefreshAndTitleTests(StubCase):
    """A timed-out scan is never signalled or duplicated, the request made
    meanwhile still runs, and the status title never says green without a
    fresh, known level."""

    def test_timed_out_scan_is_single_flight_and_the_queued_refresh_runs(self):
        count = self.dir / "count"
        script = self.stub(
            f"c = {str(count)!r}\n"
            "n = int(open(c).read()) + 1 if os.path.exists(c) else 1\n"
            "open(c, 'w').write(str(n))\n"
            "if n == 1:\n    time.sleep(2.0)\n    open(c + '.finished', 'w').write('1')\n    print('late')\n"
            "else:\n    print(json.dumps({'schema_version': 2, 'ts': time.time(), 'source': 'live', 'owners': []}))\n")
        r = run_json("--selftest-refresh", "--script", script, "--timeout", "0.3", timeout=30)
        self.assertTrue(r["after_timeout"]["still_sampling"])
        self.assertIn("still sampling", r["after_timeout"]["error"])
        self.assertEqual(r["while_busy_scans"], 1)
        self.assertEqual(r["end"], {"scans": 2, "loaded": True, "still_sampling": False, "error": None})
        # The slow first scan ran to completion: it was never signalled.
        self.assertEqual(count.read_text(), "2")
        self.assertTrue((self.dir / "count.finished").exists())

    def test_memmon_that_cannot_start_never_wedges_refresh(self):
        r = run_json("--selftest-refresh", "--script", str(self.dir / "absent.py"),
                     "--python", str(self.dir / "no-such-python"), timeout=30)
        self.assertIn("could not start memmon", r["first"]["error"])
        self.assertEqual(r["second"]["scans"], 2)
        self.assertFalse(r["second"]["refreshing"])

    def test_still_sampling_error_clears_when_the_scan_ends(self):
        script = self.stub("time.sleep(1.5)\nprint('late')\n")
        r = run_json("--selftest-refresh", "--script", script, "--timeout", "0.3", "--single", timeout=30)
        self.assertIn("still sampling", r["during"]["error"])
        self.assertEqual(r["after"], {"error": None, "still_sampling": False})

    def title(self, payload, now):
        path = self.dir / "latest.json"
        path.write_text(json.dumps(payload))
        return run_bin("--title-probe", "--latest", str(path), "--now", str(now)).strip()

    def test_status_title_is_neutral_when_unknown_or_stale(self):
        base = {"ts": 1000, "swap_used": 2 * 1024 ** 3}
        self.assertEqual(self.title(dict(base, pressure="HEALTHY"), 1100), "🟢 2.0G")
        self.assertEqual(self.title(dict(base, pressure="DANGER"), 1100), "🔴 2.0G")
        self.assertEqual(self.title(base, 1100), "⚪ 2.0G")
        self.assertEqual(self.title(dict(base, pressure="?"), 1100), "⚪ 2.0G")
        self.assertEqual(self.title(dict(base, pressure="HEALTHY"), 1181), "⚪ 2.0G")


class GeneratorContractTests(unittest.TestCase):
    """Fixtures can drift from the contract, so one payload comes straight from
    memmon.owners_json over a scripted process table, with only the sampler,
    the gate log and the memory reader faked."""

    GATE = {"installed": True, "paused": False, "policy": {"mode": "block-critical"},
            "counts": {"stopped": 0, "warned": 0}, "history": {"events": []}, "pending_retry": []}
    LEASE = {"id": "run-gen-1", "resource": "heavy", "label": "acme-api tests", "cwd": "/work/acme-api",
             "wrapper_pid": 900, "child_pid": None, "created_at": 1_791_404_988.0, "state": "waiting",
             "reason": "memory pressure: WATCH", "elapsed_seconds": 12}

    T0 = 1_791_400_000

    def setUp(self):
        self.state = testkit.TempState()
        self.addCleanup(self.state.close)
        self.out = tempfile.TemporaryDirectory()
        self.addCleanup(self.out.cleanup)

    def generated(self, view):
        T0, MB, P = self.T0, testkit.MB, testkit.P
        sessions = memmon.CLAUDE_SESSIONS_DIR
        ctx = memmon_owners.Context(sessions_dir=sessions,
                                    socks_dir=os.path.join(self.state.root, "socks"),
                                    codex_home=os.path.join(self.state.root, "codex"),
                                    classify=lambda c: memmon.classify_command(c, {}),
                                    leases=[self.LEASE])
        testkit.session_file(sessions, 10, T0 + 10, job_id="0000a001")

        def shell(cmd):
            return ["/bin/zsh", "-c", f"eval '{cmd}' < /dev/null"]

        procs = [P(10, start=(T0 + 10, 0), comm="2.1.293"), P(11, ppid=10, pgid=10, comm="npm"),
                 P(20, ppid=10, fp=10 * MB), P(21, ppid=20, pgid=20, fp=4000 * MB),
                 P(30, ppid=10, fp=50 * MB), P(400, comm="node", fp=300 * MB)]
        src = testkit.FakeSource(procs, argv={20: shell("pnpm --filter web typecheck"),
                                              30: shell("pnpm dev")})
        inv = memmon_procs.snapshot(src, clock=lambda: T0 + 5000, mono=lambda: 10 ** 12)

        def sample(window, source=None, ctx=None, sleep=None):
            part = memmon_owners.partition(inv, ctx)
            return memmon_owners.Sample(inv, part, {10: 0.1}, "warming up"), 1.0
        with mock.patch.object(memmon, "owners_sample", sample), \
                mock.patch.object(memmon, "gate_stats", return_value=self.GATE), \
                mock.patch.object(memmon, "gate_installed", return_value=True), \
                mock.patch.object(memmon, "pressure", return_value={"level": "HEALTHY"}), \
                mock.patch.object(memmon, "read_vm", return_value={}):
            payload = memmon.owners_json(1.0, ctx=ctx, system_reader=lambda: {
                "ram_bytes": 48 << 30, "used_bytes": 30 << 30, "pressure_level": "normal"})
        payload["_now"] = payload["ts"] + 2
        payload["_view"] = view
        path = Path(self.out.name) / "generated.json"
        path.write_text(json.dumps(payload))
        session = next(r for r in payload["owners"] if r["kind"] == "claude")
        return path, payload, session

    def test_generated_payload_decodes_renders_and_reads(self):
        path, payload, session = self.generated({"sections": ["background"]})
        self.assertEqual(session["jobs"][0]["kind"], "conversation")
        png = Path(self.out.name) / "generated.png"
        line = run_bin("--render", str(png), "--fixture", str(path), "--dark")
        self.assertIn("rendered", line)
        rows = a11y_path(path)
        found = labels(rows)
        row = next(l for l in found if l.startswith(session["title"] + ","))
        self.assertIn("Building · typecheck", row)
        self.assertIn("ownership confidence: exact", row)
        self.assertTrue(any(l.startswith("Unattributed,") for l in found))
        self.assertIn("Memory pressure normal", " ".join(r["value"] for r in rows))
        # No project for a scripted session reads as unknown, not as a claim.
        self.assertIn("No project detected", row)
        # owners_json's own assembly: partial CPU coverage gives no total, and
        # the waiting runner lease reaches the Managed jobs card verbatim.
        self.assertLess(payload["system"]["cpu_coverage"], 1)
        self.assertIsNone(payload["system"]["cpu_cores"])
        self.assertEqual(payload["runner_jobs"], [self.LEASE])
        spoken = " ".join(r["label"] + " " + r["value"] for r in rows)
        self.assertIn("CPU partly measured", spoken)
        self.assertIn("Managed job acme-api tests · waiting · 12s, heavy, memory pressure: WATCH",
                      [r["label"] or r["value"] for r in rows])

    def test_generated_session_detail_and_stop_confirm(self):
        _, _, session = self.generated({})
        path, _, _ = self.generated({"select": session["owner_id"]})
        found = labels(a11y_path(path))
        self.assertIn("kept running", found)
        self.assertTrue(any(l.startswith("Conversation, ") for l in found))
        self.assertTrue(any(l.startswith("Stop build: Typecheck") for l in found), found)
        self.assertTrue(any(l.startswith("Stop server:") for l in found), found)
        path, _, _ = self.generated({"select": session["owner_id"], "confirm": "stop-job"})
        self.assertIn("Stop typecheck?", labels(a11y_path(path)))


if __name__ == "__main__":
    unittest.main()
