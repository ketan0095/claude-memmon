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


# Sections start collapsed; tests that read rows open the agent sections.
AGENTS = ("--sections", "claude,codex")


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
                        head = line.split("head=")[1].split()[0]
                        dark_px = int(line.split("dark_tokens=")[1].split()[0])
                        if theme == "light":
                            self.assertEqual(dark_px, 0, "a dark-only surface colour in the light render")
                        else:
                            self.assertGreater(dark_px, 1000)
                        if theme == "light":
                            self.assertGreater(luminance(bg), 0.7, bg)
                            self.assertGreater(luminance(head), 0.7, head)    # soft lavender
                        else:
                            self.assertLess(luminance(bg), 0.2, bg)
                            self.assertLess(luminance(head), 0.25, head)      # deep purple

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
        rows = a11y("overview.json", *AGENTS)
        found = labels(rows)
        self.assertIn("Sampled 2s ago by the live reader", found)
        self.assertIn("Protection partial · 2 heavy processes not started through memmon run", found)
        ring = next(l for l in found if l.startswith("Memory "))
        self.assertEqual(ring, "Memory 39.2 of 48 GB in use, pressure normal; Claude sessions 9.5 GB, Codex 2.1 GB, "
                               "Mac apps 5.2 GB, Shared services 9.9 GB, Background 1.8 GB, "
                               "System & other 10.7 GB, free 8.8 GB")
        owner = next(r for r in rows if r["label"].startswith("Checkout refactor,"))
        self.assertEqual(owner["role"], "AXButton")
        # Line 2 is clipped at two lines on screen; the label carries all of it.
        self.assertIn("Claude · Building · typecheck", owner["label"])
        self.assertIn("ownership confidence: exact", owner["label"])
        self.assertIn("8.9 GB, 3.1 cores", owner["label"])
        self.assertEqual(owner["value"], "collapsed")
        self.assertIn("ownership confidence: inferred",
                      next(l for l in found if l.startswith("Billing API tests,")))
        for sort in ("Sort by Memory", "Sort by CPU"):
            self.assertIn(sort, found)
        # Growth needs ten unbroken minutes of history; it is not a sort.
        self.assertNotIn("Sort by Growth", found)
        self.assertIn("Pause command protection", found)

    def test_unavailable_values_are_spoken_as_unavailable_never_zero(self):
        found = labels(a11y("unavailable.json", *AGENTS))
        row = next(l for l in found if l.startswith("Checkout refactor,"))
        # Nothing measured is one state, not a memory reason plus a CPU one.
        self.assertIn("memory and CPU not available, not measured", row)
        self.assertNotIn("warming up", row)
        self.assertNotIn("0.0 GB", row)
        self.assertIn("Sample time unknown", found)
        ring = next(l for l in found if l.startswith("Memory in use"))
        self.assertEqual(ring, "Memory in use not available, pressure unknown")
        self.assertIn("Memory in use not available", " ".join(r["value"] for r in a11y("unavailable.json", *AGENTS)))

    def test_session_detail_labels_child_jobs_and_kept_marker(self):
        found = labels(a11y("session-detail.json"))
        self.assertIn("Stop build: Typecheck · build in Checkout refactor", found)
        self.assertIn("Stop server: Dev server · port 3000 in Checkout refactor", found)
        self.assertIn("Typecheck · build, 6.8 GB · 5 processes", found)
        self.assertIn("kept running", found)
        self.assertIn("ownership confidence: exact", found)
        self.assertIn("End session Checkout refactor: stops the conversation and all its processes "
                      "(asks to confirm)", found)

    def test_destructive_footer_actions_carry_their_scope_not_a_caption(self):
        session = [r["label"] or r["value"] for r in a11y("session-detail.json")]
        self.assertNotIn("Conversation + all its processes", session)
        service = a11y("quit-service-forced.json")
        spoken = [r["label"] or r["value"] for r in service]
        self.assertNotIn("The VM and all its containers", spoken)
        self.assertIn("Quit Container VM: quits the VM and stops all its containers (asks to confirm)",
                      labels(service))
        app = [r["label"] or r["value"] for r in a11y("helpers-only.json")]
        self.assertNotIn("1 instance", app)
        # A footer with no destructive action keeps its caption.
        hosts = [r["label"] or r["value"] for r in a11y("hosts-detail.json")]
        self.assertIn("Hosts 2 sessions — quit it from the app itself", hosts)

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
        # A schema 1 row (no reservation, estimate or queue fields) keeps
        # what S1 said: its reason and age.
        self.assertIn("Managed job acme-web typecheck, running, command running · 42s", found)
        self.assertIn("Managed job acme-api tests, waiting, resource held by acme-web typecheck · 12s", found)

    def test_cpu_total_only_with_full_coverage(self):
        self.assertIn("CPU still measuring some processes", self.spoken("small.json"))
        self.assertIn("CPU still measuring some processes", self.spoken("overview.json"))
        self.assertIn("CPU 3.1 of 18 cores busy", self.spoken("helpers-only.json"))
        for jargon in ("Score HEALTHY", "kernel normal"):
            self.assertNotIn(jargon, self.spoken("overview.json"))
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
        rows = a11y("long-activity.json", *AGENTS)
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
        detail = labels(a11y("session-detail.json", *AGENTS))
        self.assertIn("Conversation, 1.2 GB · stays open when you stop a build", detail)
        self.assertFalse(any(l.startswith("Stop job: Conversation") for l in detail))
        shared = labels(a11y("shared-detail.json", *AGENTS))
        # The generator's own title for a Codex frontend.
        title = "Codex thread · runs in Codex daemon"
        self.assertIn(f'owner.title = "{title}"', (ROOT / "memmon_owners.py").read_text())
        pointer = next(l for l in shared if l.startswith(title + ","))
        self.assertIn("Codex thread · 1 process", pointer)
        self.assertIn("ownership confidence: shared", pointer)
        # A saved "growth" sort from an older build falls back to memory.
        growth = labels(a11y("growth-sort.json", *AGENTS))
        unattributed = next(l for l in growth if l.startswith("Unattributed,"))
        self.assertTrue(unattributed.endswith("1.8 GB, CPU not available, not measured"), unattributed)
        self.assertFalse(any("growth not available" in l for l in growth))

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

    def test_every_section_starts_collapsed(self):
        sec = self.by_section(self.probe())
        self.assertEqual({k for k, v in sec.items() if v["open"]}, set())

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

    def test_header_totals_follow_the_sort(self):
        payload = effective(FIXTURES / "sections.json")
        for sort, unit in (("cpu", "cores"), ("memory", "GB")):
            path = Path(self.tmp.name) / f"{sort}.json"
            path.write_text(json.dumps(dict(payload, _view={"sort": sort})))
            header = next(l for l in labels(a11y_path(path)) if l.startswith("Claude sessions,"))
            self.assertIn(unit, header, sort)
            if sort == "cpu":
                self.assertNotIn(" GB", header)

    def test_a_tiny_footprint_is_spoken_as_under_a_tenth(self):
        found = labels(a11y("sections-background.json"))
        row = next(l for l in found if l.startswith("Example Updater,"))
        self.assertIn("less than 0.1 GB", row)
        self.assertNotIn("0.0 GB", row)

    def test_headers_and_rows_say_everything_the_old_rows_did(self):
        found = labels(a11y("sections-open.json", *AGENTS))
        self.assertIn("Mac apps, 8 owners, 6.4 GB, expanded", found)
        self.assertIn("Browsers, 2 owners, 5.9 GB, collapsed", found)
        self.assertIn("Background, 9 small owners and 6 unattributed processes, 2.1 GB, expanded", found)
        row = next(l for l in found if l.startswith("Checkout refactor,"))
        self.assertEqual(row, "Checkout refactor, Claude · Building · typecheck, acme-web · checkout worktree, "
                              "ownership confidence: exact, 8.9 GB, 3.1 cores")
        helper = next(l for l in found if l.startswith("Example Widgets,"))
        self.assertIn("ownership confidence: exact", helper)


class HeaderAndRingTests(unittest.TestCase):
    """The gradient header's status pill, the protection pill, the memory
    ring and the one-row command protection."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def probe(self, fixture, *flags, payload=None):
        if payload is not None:
            path = Path(self.tmp.name) / "payload.json"
            path.write_text(json.dumps(payload))
            fixture = path
        else:
            fixture = FIXTURES / fixture
        return run_json("--sections-probe", "--fixture", str(fixture), *flags)

    def test_status_pill_says_live_syncing_sampling_stale_or_unknown(self):
        live = self.probe("overview.json")["status"]
        self.assertEqual((live["kind"], live["text"]), ("live", "Live · 2s"))
        self.assertEqual(live["spoken"], "Sampled 2s ago by the live reader")
        sync = self.probe("overview.json", "--refreshing")["status"]
        self.assertEqual((sync["kind"], sync["text"]), ("syncing", "Syncing…"))
        sampling = self.probe("overview.json", "--sampling")["status"]
        self.assertEqual((sampling["kind"], sampling["text"]), ("sampling", "Still sampling…"))
        stale = self.probe("stale-paused.json")["status"]
        self.assertEqual((stale["kind"], stale["text"]), ("stale", "Stale · 3 min"))
        self.assertTrue(stale["spoken"].endswith(", stale"))
        unknown = self.probe("unavailable.json")["status"]
        self.assertEqual((unknown["kind"], unknown["text"], unknown["spoken"]),
                         ("stale", "Time unknown", "Sample time unknown"))

    def test_status_pill_is_spoken_in_the_header(self):
        self.assertIn("Sampled 3 min ago by the background sampler, stale", labels(a11y("stale-paused.json")))

    def test_protection_pill_says_the_whole_sentence(self):
        cases = {"overview.json": "Protection partial · 2 heavy processes not started through memmon run",
                 "small.json": "Protection on · no heavy processes outside memmon run",
                 "stale-paused.json": "Protection paused · commands run without a memory check",
                 "unavailable.json": "Protection off · the command gate is not installed or is disabled"}
        for fixture, sentence in cases.items():
            with self.subTest(fixture=fixture):
                self.assertIn(sentence, labels(a11y(fixture)))

    def test_command_protection_is_one_row_that_opens_its_history(self):
        rows = a11y("overview.json")
        row = next(r for r in rows if r["label"].startswith("Command protection,"))
        self.assertEqual(row["label"], "Command protection, Active, 1 warned · 1 stopped, policy and history")
        self.assertEqual(row["value"], "collapsed")
        self.assertIn("Pause command protection", labels(rows))
        # The actionable blocked card stays visible with the row closed.
        self.assertIn("1 blocked command waiting to retry", [r["label"] or r["value"] for r in rows])
        opened = next(r for r in a11y("gate-open.json") if r["label"].startswith("Command protection,"))
        self.assertEqual(opened["value"], "expanded")
        off = next(l for l in labels(a11y("unavailable.json")) if l.startswith("Command protection,"))
        self.assertEqual(off, "Command protection, Not installed, what command protection does")

    def ring(self, r):
        return {s["id"]: s for s in r["ring"]}

    def test_system_and_other_is_used_minus_sections_never_negative(self):
        over = self.probe("sections-open.json")       # sections add up to more than used
        ring = self.ring(over)
        self.assertEqual(ring["system"]["bytes"], 0)
        self.assertLessEqual(sum(s["arc"] for s in over["ring"]), over["used"] + 1)
        under = self.probe("overview.json")
        sections = sum(s["bytes"] for s in under["ring"] if s["id"] != "system")
        self.assertAlmostEqual(self.ring(under)["system"]["bytes"], under["used"] - sections, delta=1)

    def test_ring_arcs_never_exceed_used_and_count_each_owner_once(self):
        for fixture in ("overview.json", "sections.json", "sections-open.json", "small.json", "degraded.json"):
            with self.subTest(fixture=fixture):
                r = self.probe(fixture)
                self.assertLessEqual(sum(s["arc"] for s in r["ring"]), r["used"] + 1)
                # Each section's arc source is that section's own total.
                totals = {s["section"]: s["total"] for s in r["sections"]}
                for seg in r["ring"]:
                    if seg["id"] != "system":
                        self.assertAlmostEqual(seg["bytes"], totals[seg["id"]], delta=1)
        payload = effective(FIXTURES / "overview.json")
        fp = sum(o.get("footprint_bytes") or 0 for o in payload["owners"])
        r = self.probe("overview.json")
        self.assertAlmostEqual(sum(s["bytes"] for s in r["ring"] if s["id"] != "system"), fp, delta=1)

    def test_unavailable_memory_draws_no_arcs(self):
        r = self.probe("unavailable.json")
        self.assertIsNone(r["used"])
        self.assertNotIn("system", self.ring(r))

    def test_reduce_motion_stops_the_sweep_and_the_pulse(self):
        moving = self.probe("overview.json")["motion"]
        self.assertEqual(moving, {"sweep": True, "sweep_render": False, "pulse": True})
        still = self.probe("overview.json", "--reduce-motion")["motion"]
        self.assertEqual(still, {"sweep": False, "sweep_render": False, "pulse": False})

    def test_stale_stays_visible_while_a_refresh_runs(self):
        for flag in ("--refreshing", "--sampling"):
            state = self.probe("stale-paused.json", flag)["status"]
            self.assertEqual((state["kind"], state["text"]), ("stale", "Stale · 3 min"), flag)

    def test_ring_marks_a_partly_measured_section_as_a_lower_bound(self):
        payload = effective(FIXTURES / "overview.json")
        row = next(o for o in payload["owners"] if o["title"] == "Checkout refactor")
        row["footprint_bytes"], row["footprint_reason"] = None, "not measured"
        path = Path(self.tmp.name) / "unmeasured.json"
        path.write_text(json.dumps(payload))
        found = labels(a11y_path(path))
        ring = next(l for l in found if l.startswith("Memory "))
        self.assertIn("Claude sessions at least 0.6 GB", ring)
        self.assertIn("System & other at most ", ring)
        system = next(l for l in found if l.startswith("System & other,"))
        self.assertTrue(system.startswith("System & other, at most "), system)
        exact = labels(a11y("overview.json"))
        self.assertIn("Show Claude sessions in the list, 9.5 GB", exact)

    def test_clicking_a_legend_row_opens_and_scrolls_to_its_section(self):
        before = {s["section"]: s["open"] for s in self.probe("overview.json")["sections"]}
        self.assertFalse(before["service"])
        after = self.probe("overview.json", "--legend-click", "service")
        self.assertTrue({s["section"]: s["open"] for s in after["sections"]}["service"])
        self.assertEqual(after["scroll_target"], "service")

    def test_dismiss_calls_memmon_with_the_entry_id(self):
        out = self.probe("stale-paused.json", "--dismiss", "1791449760000.fixture-a")
        self.assertEqual(out["actions"], ["memmon --dismiss-blocked 1791449760000.fixture-a"])

    def test_an_open_confirm_alone_keeps_its_section_open(self):
        payload = effective(FIXTURES / "overview.json")
        payload["_view"] = {"select": "claude:fixture-a", "confirm": "stop-job",
                            "sections": ["-claude"]}
        state = {s["section"]: s for s in self.probe(None, "--deselect", payload=payload)["sections"]}
        self.assertTrue(state["claude"]["open"])
        self.assertIn("claude:fixture-a", state["claude"]["shown"])

    def test_gate_detail_is_one_policy_line_and_one_recent_list(self):
        rows = a11y("gate-open.json")
        spoken = [r["label"] or r["value"] for r in rows]
        self.assertIn("Current policy: WATCH or DANGER warns; CRITICAL stops before running. "
                      "Only commands that match a memory-intensive rule are checked.", spoken)
        self.assertIn("What commands match?", labels(rows))
        self.assertIn("Recent", spoken)
        for gone in ("Stopped before running", "Warned — command ran",
                     "Matched command + memory then → result"):
            self.assertNotIn(gone, spoken)
        events = [l for l in labels(rows) if l.startswith(("Stopped, ", "Warned, "))]
        self.assertEqual(len(events), 2)
        self.assertTrue(events[0].startswith("Stopped, Checkout refactor"))
        # Plain words: who, what kind of command, how memory was, what happened.
        self.assertIn("Held back a type check before it started; memory was critical.", events[0])
        self.assertIn("A test run started while memory was getting tight; it still ran.", events[1])
        # The card names what happened; it does not narrate itself.
        self.assertFalse(any("memmon stopped" in e or "memmon warned" in e for e in events))
        self.assertIn("Command: pnpm typecheck", events[0])
        self.assertTrue(any(s.startswith("Retained activity since") for s in spoken))

    def test_header_face_follows_pressure_and_staleness(self):
        found = labels(a11y("overview.json"))
        self.assertIn("memmon mood: calm, memory pressure is normal", found)
        self.assertIn("memmon mood: asleep, the sample is stale", labels(a11y("stale-paused.json")))
        self.assertIn("memmon mood: unsure, memory pressure is unknown", labels(a11y("unavailable.json")))
        self.assertIn("memmon mood: strained, memory pressure is high", labels(a11y("gate-open.json")))
        base = json.loads((FIXTURES / "overview.json").read_text())
        for level, want in (("WATCH", "watchful, memory pressure is rising"),
                            ("CRITICAL", "overheating, memory pressure is critical")):
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "f.json"
                p.write_text(json.dumps(dict(base, system=dict(base["system"], score_level=level))))
                self.assertIn("memmon mood: " + want, labels(a11y_path(p)))

    def test_footer_says_nothing_it_cannot_explain(self):
        spoken = " ".join(r["label"] + " " + r["value"] for r in a11y("overview.json"))
        self.assertNotIn("child processes included once", spoken)
        self.assertNotIn("Memory figures are estimates", spoken)

    def test_jewel_palette_and_separable_neutrals(self):
        t = run_json("--palette-probe")
        want = {"claude": ("7c3aed", "a78bfa"), "codex": ("0d9488", "2dd4bf"), "job": ("ea580c", "fb923c"),
                "browser": ("2563eb", "60a5fa"), "dev": ("16a34a", "4ade80"), "app": ("db2777", "f472b6"),
                "service": ("ca8a04", "facc15"), "background": ("94a3b8", "8391a7")}
        for sec, (light, dark) in want.items():
            self.assertEqual(t["tokens"]["section." + sec], {"light": light, "dark": dark}, sec)
        def lum(h):
            r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
            return 0.2126 * r + 0.7152 * g + 0.0722 * b
        for theme in ("light", "dark"):
            levels = sorted(lum(t["tokens"][k][theme]) for k in ("section.background", "system", "track"))
            # Background, System & other and the free track must not blend.
            self.assertGreater(min(b - a for a, b in zip(levels, levels[1:])), 0.08, theme)

    def test_an_open_gate_event_reads_as_short_labelled_rows(self):
        events = [l for l in labels(a11y("gate-events-open.json")) if l.startswith(("Stopped, ", "Warned, "))]
        self.assertEqual(len(events), 2)
        stop = events[0]
        self.assertIn("Held back a type check before it started; memory was critical.", stop)
        self.assertIn("Why: Type checkers like tsc", stop)
        self.assertIn("Memory: Swap growing", stop)
        self.assertIn("Next: Waiting in the blocked list. Retry once memory is normal.", stop)
        self.assertIn("Next: Nothing needed. If memory keeps climbing, stop an idle session.", events[1])
        self.assertFalse(any("memmon " in e for e in events), events)

    def test_free_row_toggles_its_split(self):
        self.assertEqual(run_json("--palette-probe")["free_toggle"], [True, False])
        src = (ROOT / "MemmonBar.swift").read_text()
        self.assertIn("{ model.toggleFree() } } label: {", src)

    def test_recent_shows_three_and_offers_the_rest(self):
        base = json.loads((FIXTURES / "overview.json").read_text())
        ev = base["gate"]["history"]["events"][-1]
        events = [dict(ev, ts=ev["ts"] - 60 * i, session=dict(ev["session"], id=f"fixture-r{i}"))
                  for i in range(6)]
        gate = dict(base["gate"], history=dict(base["gate"]["history"], events=events), pending_retry=[])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "f.json"
            p.write_text(json.dumps(dict(base, gate=gate, _view={"open_gate": True})))
            found = labels(a11y_path(p))
        self.assertEqual(sum(l.startswith("Warned, ") for l in found), 3, found)
        self.assertIn("Show all 6", found)

    def test_app_icons_are_local_and_only_in_the_live_app(self):
        src = (ROOT / "MemmonBar.swift").read_text()
        # The live app, and only it, turns icons on before the controller starts.
        self.assertIn("enableLiveOnlyFeatures()\nlet controller = Controller()", src)
        self.assertEqual(src.count("AppIcons.enabled = true"), 1)
        icons = run_json("--palette-probe")["icons"]
        # Renders keep glyphs; the app finds an installed app's icon; a missing
        # bundle falls back to the glyph.
        self.assertEqual(icons, {"off": False, "on": True, "missing": False})

    def test_mood_face_is_still_under_reduce_motion(self):
        t = run_json("--palette-probe")
        self.assertEqual((t["mood_animates"], t["mood_animates_reduced"]), (True, False))

    def test_free_explains_what_it_holds(self):
        payload = effective(FIXTURES / "overview.json")
        payload["system"]["idle_bytes"], payload["system"]["cache_bytes"] = 3 * 2**30, 5.8 * 2**30
        path = Path(self.tmp.name) / "free.json"
        path.write_text(json.dumps(payload))
        free = next(l for l in labels(a11y_path(path)) if l.startswith("Free "))
        self.assertIn("3.0 GB empty right now and 5.8 GB of file cache macOS reclaims", free)
        plain = next(l for l in labels(a11y("overview.json")) if l.startswith("Free "))
        self.assertIn("Memory no app is using right now.", plain)

    def test_legend_rows_open_their_section(self):
        found = labels(a11y("overview.json"))
        self.assertIn("Show Claude sessions in the list, 9.5 GB", found)
        system = next(l for l in found if l.startswith("System & other,"))
        self.assertIn("System and other users: not itemised (214 processes)", system)

    def test_system_line_moves_into_background(self):
        closed = [r["label"] or r["value"] for r in a11y("overview.json")]
        self.assertNotIn("System and other users: not itemised (214 processes)", closed)
        opened = [r["label"] or r["value"] for r in a11y("sections-background.json")]
        self.assertIn("System and other users: not itemised (214 processes)", opened)
        nobackground = [r["label"] or r["value"] for r in a11y("small.json")]
        self.assertIn("System and other users: not itemised (214 processes)", nobackground)


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
        rows = a11y_path(path, *AGENTS)
        found = labels(rows)
        row = next(l for l in found if l.startswith(session["title"] + ","))
        self.assertIn("Building · typecheck", row)
        self.assertIn("ownership confidence: exact", row)
        self.assertTrue(any(l.startswith("Unattributed,") for l in found))
        self.assertTrue(any(l.startswith("Memory ") and "pressure normal" in l for l in found), found)
        # No project for a scripted session reads as unknown, not as a claim.
        self.assertIn("No project detected", row)
        # owners_json's own assembly: partial CPU coverage gives no total, and
        # the waiting runner lease reaches the Managed jobs card verbatim.
        self.assertLess(payload["system"]["cpu_coverage"], 1)
        self.assertIsNone(payload["system"]["cpu_cores"])
        self.assertEqual(payload["runner_jobs"], [self.LEASE])
        spoken = " ".join(r["label"] + " " + r["value"] for r in rows)
        self.assertIn("CPU still measuring some processes", spoken)
        self.assertIn("Managed job acme-api tests, waiting, memory pressure: WATCH · 12s",
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


# ---------------------------------------------------------------- Stage 2

def s2(fixture=None, payload=None, *flags):
    """The S2 part of --sections-probe, for a fixture or an inline payload."""
    with tempfile.TemporaryDirectory() as d:
        if payload is not None:
            path = Path(d) / "payload.json"
            path.write_text(json.dumps(payload))
        else:
            path = FIXTURES / fixture
        return run_json("--sections-probe", "--fixture", str(path), *flags)


def payload(fixture, **change):
    """A fixture flattened through its _base chain, with keys replaced."""
    return dict(effective(FIXTURES / fixture), **change)


def said(fixture):
    """Every label, or the text itself for a plain text element."""
    return [r["label"] or r["value"] for r in a11y(fixture)]


def host(check, fixture, *extra):
    return run_json("--selftest-host", check, "--fixture", str(fixture), *extra)


class ManagedJobsCardTests(StubCase):
    """M6a / M6b: the managed-jobs card built from jobs --json schema 2."""

    def test_m6a_budget_line_rows_and_strict_queue(self):
        r = s2("managed-jobs-v2.json")["s2"]
        self.assertEqual(r["budget"], ["Committed 34.4 of 38.4 GB limit · 4.0 GB free to admit",
                                       "30.5 GB in use + 3.9 GB reserved but not yet used"])
        self.assertIsNone(r["hold"])
        self.assertEqual([(m["state"], m["detail"]) for m in r["managed"]], [
            ("Running", "3.2 GB now / 4.0 GB reserved · 2 min"),
            ("Running", "19.4 GB now / 22.5 GB reserved · 6 min"),
            ("Waiting", "needs 6.0 GB (learned from 5 runs) · waiting for budget · gives up in 8 min"),
            ("Waiting", "needs 4.0 GB (unknown — default) · queued behind #1")])
        found = labels(a11y("managed-jobs-v2.json"))
        self.assertIn("Managed job #1 Billing API tests, waiting, needs 6.0 GB (learned from 5 runs) · "
                      "waiting for budget · gives up in 8 min", found)
        self.assertIn("Managed job #2 Checkout typecheck, waiting, needs 4.0 GB (unknown — default) · "
                      "queued behind #1", found)
        # A distinct mode control, not a view filter.
        self.assertIn("Protection mode, protect", found)
        self.assertIn("Protect, current protection mode", found)
        self.assertIn("Set protection mode to Paused", found)

    def test_m6b_over_limit_holds_and_puts_the_intervention_first(self):
        r = s2("managed-jobs-intervention.json")["s2"]
        head, sub = r["budget"]
        self.assertEqual(head, "Committed 49.2 GB · over the 38.4 GB limit by 10.8 GB")
        self.assertIn("20 % of memory (9.6 GB)", sub)
        self.assertIn("headroom target is not currently met", sub)
        self.assertEqual(r["hold"], "Holding new heavy work — memory must stay at Watch or better for 30 s")
        first = r["managed"][0]
        self.assertEqual(first["state"], "Intervention needed")
        self.assertEqual(first["detail"], "grew to 7.9 GB, above its 4.0 GB reservation; memory DANGER · "
                                          "new heavy work is on hold")
        self.assertTrue(first["stop"])
        self.assertEqual(r["managed"][2]["detail"],
                         "needs 6.0 GB (learned from 5 runs) · on hold · gives up in 6 min")
        found = labels(a11y("managed-jobs-intervention.json"))
        self.assertIn("Holding new heavy work — memory must stay at Watch or better for 30 s", found)
        self.assertIn("Stop job Search index rebuild: stops the job and its memmon run wrapper (asks to confirm)",
                      found)

    def test_intervention_stop_needs_an_s1_token(self):
        p = payload("managed-jobs-intervention.json")
        p["owners"] = [o for o in p["owners"] if not o["owner_id"].startswith("job:")]
        self.assertFalse(s2(payload=p)["s2"]["managed"][0]["stop"])
        p = payload("managed-jobs-intervention.json", inventory="degraded")
        self.assertFalse(s2(payload=p)["s2"]["managed"][0]["stop"])

    def test_intervention_stop_finds_a_managed_child_job(self):
        p = payload("managed-jobs-intervention.json")
        p["owners"] = [o for o in p["owners"] if not o["owner_id"].startswith("job:")]
        p["owners"][0]["jobs"].append({"job_id": "48501.1791449958.120044", "kind": "build",
                                       "label": "index", "action": "stop-managed-job",
                                       "managed": True, "token": "tok-child-managed"})
        self.assertTrue(s2(payload=p)["s2"]["managed"][0]["stop"])

    def test_only_protect_mode_holds(self):
        for mode in ("observe", "paused"):
            p = payload("managed-jobs-intervention.json")
            p["runner"] = dict(p["runner"], mode=mode)
            r = s2(payload=p)["s2"]
            self.assertIsNone(r["hold"], mode)
            self.assertEqual(r["mode"], mode)

    def test_telemetry_hold_says_memmon_cannot_read_pressure(self):
        p = payload("managed-jobs-intervention.json")
        p["runner"] = dict(p["runner"], admission={"open": False, "reason": "telemetry unavailable",
                                                   "hysteresis_s": 30})
        self.assertEqual(s2(payload=p)["s2"]["hold"],
                         "Holding new heavy work — memmon can’t read memory pressure right now")

    def test_unavailable_budget_says_why(self):
        p = payload("managed-jobs-v2.json")
        p["runner"] = dict(p["runner"], committed={"used": None, "slack": None, "limit": None,
                                                   "free": None, "reason": "system memory unavailable"})
        self.assertEqual(s2(payload=p)["s2"]["budget"],
                         ["Committed memory not available · system memory unavailable", None])

    def test_schema_1_payload_keeps_the_s1_card(self):
        r = s2("managed-jobs.json")["s2"]
        self.assertIsNone(r["mode"])
        self.assertIsNone(r["budget"])
        self.assertEqual(r["managed"][0]["detail"], "command running · 42s")

    def idle_hold(self, **runner):
        p = payload("managed-jobs-intervention.json", runner_jobs=[])
        p["runner"] = dict(p["runner"], queue={"length": 0, "max": 32}, **runner)
        return p

    def test_a_hold_on_an_idle_machine_shows_no_banner_and_no_card(self):
        r = s2(payload=self.idle_hold())["s2"]
        self.assertEqual((r["hold"], r["card"]), (None, False))
        self.assertFalse(any(l.startswith("Holding new heavy work") for l in labels(a11y_path_payload(self.idle_hold()))))
        # A runner that is not protecting still shows its card, never a hold.
        r = s2(payload=self.idle_hold(mode="paused"))["s2"]
        self.assertEqual((r["hold"], r["card"]), (None, True))

    def test_a_hold_shows_once_work_is_waiting(self):
        p = self.idle_hold()
        p["runner"]["queue"]["length"] = 1
        r = s2(payload=p)["s2"]
        self.assertEqual((bool(r["hold"]), r["card"]), (True, True))
        # The queue count can lag the rows: a waiting row is enough.
        p = self.idle_hold()
        p["runner_jobs"] = [effective(FIXTURES / "managed-jobs-intervention.json")["runner_jobs"][2]]
        self.assertTrue(s2(payload=p)["s2"]["hold"])

    def test_mode_control_runs_run_mode_and_nothing_else(self):
        fixture = FIXTURES / "managed-jobs-v2.json"
        self.assertEqual(host("run-mode", fixture, "--mode", "paused")["actions"], ["run-mode paused"])
        self.assertEqual(host("run-mode", fixture, "--mode", "off")["actions"], [])
        calls = self.dir / "calls.jsonl"
        script = self.dir / "memmon_calls.py"
        script.write_text("import json, sys\n"
                          f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                          "print('{}')\n")
        host("run-mode", fixture, "--mode", "observe", "--script", str(script))
        made = [json.loads(l) for l in calls.read_text().splitlines()]
        self.assertEqual(made[0], ["run-mode", "observe"])
        self.assertTrue(all(c[0] in ("run-mode", "owners") for c in made), made)


class UnknownPressureTests(StubCase):
    """M7c: UNKNOWN is never Normal and never green."""

    def test_unknown_is_muted_with_a_question_mark(self):
        r = s2("pressure-unknown.json")
        self.assertEqual(r["s2"]["pressure"], {"word": "Unknown", "headline": "Unknown", "tone": "muted",
                                               "glyph": True})
        self.assertEqual((r["status"]["kind"], r["status"]["text"]), ("plain", "Sampled 6 min ago"))
        self.assertNotIn("stale", r["status"]["text"])
        # Busy states never hide staleness, and the spoken label keeps it.
        busy = s2("pressure-unknown.json", None, "--refreshing")["status"]
        self.assertEqual((busy["kind"], busy["text"]), ("plain", "Sampled 6 min ago"))
        self.assertIn(", stale", busy["spoken"])
        found = labels(a11y("pressure-unknown.json"))
        ring = next(l for l in found if l.startswith("Memory "))
        self.assertIn("pressure unknown", ring)
        self.assertNotIn("normal", ring.lower())
        self.assertIn("Sampled 6 min ago by the background sampler, stale, pressure unknown", found)

    def test_a_fresh_unknown_is_not_live(self):
        p = payload("pressure-unknown.json", ts=effective(FIXTURES / "pressure-unknown.json")["_now"] - 2)
        st = s2(payload=p)["status"]
        self.assertEqual((st["kind"], st["text"]), ("plain", "Sampled 2s ago"))

    def test_only_healthy_is_normal_and_green(self):
        for level, word, tone in [("HEALTHY", "Normal", "green"), ("WATCH", "Watch", "amber"),
                                  ("DANGER", "Danger", "red"), ("CRITICAL", "Critical", "red"),
                                  ("UNKNOWN", "Unknown", "muted"), (None, "Unknown", "muted")]:
            p = payload("overview.json")
            p["system"] = dict(p["system"], score_level=level)
            self.assertEqual(s2(payload=p)["s2"]["pressure"]["word"], word, level)
            self.assertEqual(s2(payload=p)["s2"]["pressure"]["tone"], tone, level)

    def test_status_item_dot_is_neutral_for_unknown(self):
        path = self.dir / "latest.json"
        path.write_text(json.dumps({"ts": 1000, "swap_used": 2 * 1024 ** 3, "pressure": "UNKNOWN",
                                    "level_reason": "no rate baseline", "rates": "unavailable"}))
        self.assertEqual(run_bin("--title-probe", "--latest", str(path), "--now", "1010").strip(), "⚪ 2.0G")

    def test_a_lower_bound_level_is_shown_as_at_least(self):
        p = payload("overview.json")
        p["system"] = dict(p["system"], score_level="WATCH", level_reason="lower bound: vm_stat failed",
                           rates="unavailable")
        self.assertEqual(s2(payload=p)["s2"]["pressure"]["headline"], "≥ Watch")
        ring = next(l for l in labels(a11y_path_payload(p)) if l.startswith("Memory "))
        self.assertIn("pressure at least watch", ring)
        p["system"]["level_reason"] = None
        self.assertEqual(s2(payload=p)["s2"]["pressure"]["headline"], "Watch")

    def test_kernel_level_stands_in_when_the_strict_read_failed(self):
        p = payload("under-pressure.json")
        p["system"] = dict(p["system"], score_level="UNKNOWN", pressure_level=None, kernel_level="critical",
                           level_reason="vm_stat failed", rates="unavailable")
        found = said_payload(p)
        self.assertIn("Under pressure, kernel critical", found)
        self.assertTrue(any("macOS reports critical pressure" in l and l.startswith("CPU") for l in found), found)

    def test_retry_copy_under_unknown_says_the_gate_lets_it_run(self):
        found = said("pressure-unknown.json")
        self.assertTrue(any("a retry runs without a memory check" in l for l in found), found)


class CoverageTests(unittest.TestCase):
    """S2.7: the route sentence is coverage[0]; protection.route is the state."""

    def pill(self, p):
        rows = a11y_path_payload(p)
        return next(r for r in rows if r["label"].startswith("Protection "))

    def test_coverage_lines_follow_the_protection_pill(self):
        lines = ["Route on: heavy Bash from Claude sessions started after it was turned on",
                 "2 heavy processes not started through memmon run",
                 "Codex, other apps and terminals are covered only when they call memmon run"]
        row = self.pill(payload("overview.json", coverage=lines))
        self.assertEqual(row["label"], "Protection partial · 2 heavy processes not started through memmon run")
        self.assertEqual(row["described"] or row["value"], ". ".join(lines))

    def test_without_coverage_the_route_state_is_named(self):
        row = self.pill(payload("overview.json"))
        self.assertEqual(row["described"] or row["value"], "Route off")


def said_payload(p):
    return [r["label"] or r["value"] for r in a11y_path_payload(p)]


def a11y_path_payload(p):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "payload.json"
        path.write_text(json.dumps(p))
        return a11y_path(path)


class GapNoticeTests(unittest.TestCase):
    """M7b: the sampling-gap notice (S2.10, AD-S2-13)."""

    def expected(self, g):
        hm = lambda t: time.strftime("%H:%M", time.localtime(t))
        return (f"memmon couldn’t sample for 21 min while the Mac was awake "
                f"({hm(g['from_ts'])}–{hm(g['to_ts'])}). Readings around the gap may be incomplete.")

    def gap(self, **change):
        p = payload("sampler-gap.json")
        p["sampler"] = dict(p["sampler"], last_gap=dict(p["sampler"]["last_gap"], **change))
        return p

    def test_a_starved_gap_shows_with_its_times(self):
        g = effective(FIXTURES / "sampler-gap.json")["sampler"]["last_gap"]
        self.assertEqual(s2("sampler-gap.json")["s2"]["gap_notice"], self.expected(g))
        found = labels(a11y("sampler-gap.json"))
        self.assertIn(self.expected(g), found)
        self.assertIn("Dismiss the sampling gap notice", found)

    def test_only_a_starved_gap_with_five_awake_minutes_within_a_day(self):
        now = effective(FIXTURES / "sampler-gap.json")["_now"]
        self.assertIsNone(s2(payload=self.gap(cause="sleep"))["s2"]["gap_notice"])
        self.assertIsNone(s2(payload=self.gap(cause="reboot", awake_s=None))["s2"]["gap_notice"])
        self.assertIsNone(s2(payload=self.gap(awake_s=299))["s2"]["gap_notice"])
        self.assertIsNotNone(s2(payload=self.gap(awake_s=300))["s2"]["gap_notice"])
        self.assertIsNone(s2(payload=self.gap(to_ts=now - 86_401, from_ts=now - 90_000))["s2"]["gap_notice"])
        self.assertIsNone(s2(payload=payload("sampler-gap.json", sampler=None))["s2"]["gap_notice"])

    def test_a_later_sleep_gap_does_not_hide_a_recent_starved_one(self):
        p = payload("sampler-gap.json")
        starved = p["sampler"]["last_gap"]
        sleep = {"cause": "sleep", "gap_s": 540.0, "asleep_s": 490.0, "awake_s": 50.0,
                 "from_ts": starved["to_ts"] + 30, "to_ts": starved["to_ts"] + 570}
        p["sampler"] = dict(p["sampler"], last_gap=sleep, last_starved_gap=starved)
        self.assertEqual(s2(payload=p)["s2"]["gap_notice"], self.expected(starved))
        # Without last_starved_gap (an older payload), last_gap still drives it.
        self.assertEqual(s2("sampler-gap.json")["s2"]["gap_notice"], self.expected(starved))
        # A payload that says there is no starved gap shows nothing, whatever last_gap is.
        p["sampler"] = dict(p["sampler"], last_gap=starved, last_starved_gap=None)
        self.assertIsNone(s2(payload=p)["s2"]["gap_notice"])

    def test_dismissal_keys_the_starved_gap(self):
        r = host("gap-dismiss", FIXTURES / "sampler-gap-after-sleep.json")
        starved = effective(FIXTURES / "sampler-gap-after-sleep.json")["sampler"]["last_starved_gap"]
        self.assertIsNotNone(r["before"])
        self.assertIsNone(r["after"])
        self.assertEqual(r["stored"], starved["to_ts"])

    def test_dismissal_hides_that_gap_and_not_a_later_one(self):
        g = effective(FIXTURES / "sampler-gap.json")["sampler"]["last_gap"]
        r = host("gap-dismiss", FIXTURES / "sampler-gap.json",
                 "--next", str(FIXTURES / "next" / "sampler-gap-later.json"))
        self.assertEqual(r["before"], self.expected(g).split(" Readings")[0])
        self.assertIsNone(r["after"])
        self.assertEqual(r["stored"], g["to_ts"])
        self.assertIsNotNone(r["next"])
        self.assertIsNone(s2("sampler-gap.json", None, "--dismissed-gap", str(g["to_ts"]))["s2"]["gap_notice"])


class UnderPressureCardTests(StubCase):
    """M7a: unmanaged heavy work under pressure (S2.11, I-14)."""

    def test_rows_follow_m7a(self):
        r = s2("under-pressure.json")["s2"]
        self.assertTrue(r["under_pressure_card"])
        self.assertEqual([(x["title"], x["evidence"], x["button"], x["note"]) for x in r["suggestions"]], [
            ("vitest · acme-web", "8.8 GB · growing 120 MB/min · idle for 31 min", "Stop tests…", None),
            ("vite dev server · acme-web", "3.3 GB · running 5 h", "Stop server…", None),
            ("vite dev server · billing-api", "1.2 GB · not enough history", None,
             "orphaned · stop it where it was started")])
        self.assertEqual(r["suggestions"][0]["owner_line"], "under Claude session “Checkout refactor”")
        found = said("under-pressure.json")
        self.assertIn("Under pressure, DANGER", found)
        self.assertIn("Stop tests: vitest · acme-web (asks to confirm)", found)
        self.assertIn("memmon never stops these on its own.", found)
        self.assertFalse(any(l.startswith("Stop server: vite dev server · billing-api") for l in found))

    def test_at_most_three_rows(self):
        p = payload("under-pressure.json")
        extra = dict(p["pressure_suggestions"][1], job_id="9240.1791400000.0", footprint=1024 ** 3)
        p["pressure_suggestions"] = p["pressure_suggestions"] + [extra]
        self.assertEqual(len(s2(payload=p)["s2"]["suggestions"]), 3)

    def test_card_only_while_under_pressure(self):
        self.assertFalse(s2(payload=payload("under-pressure.json", under_pressure=False))["s2"]["under_pressure_card"])
        # An older payload without the predicate falls back to DANGER/CRITICAL.
        p = payload("under-pressure.json")
        del p["under_pressure"]
        self.assertTrue(s2(payload=p)["s2"]["under_pressure_card"])
        p["system"] = dict(p["system"], score_level="WATCH")
        self.assertFalse(s2(payload=p)["s2"]["under_pressure_card"])
        # UNKNOWN with a kernel warning is under pressure when memmon says so.
        p = payload("under-pressure.json")
        p["system"] = dict(p["system"], score_level="UNKNOWN")
        self.assertTrue(s2(payload=p)["s2"]["under_pressure_card"])

    def test_no_button_without_a_token_or_identity(self):
        p = payload("under-pressure.json")
        p["pressure_suggestions"][0] = dict(p["pressure_suggestions"][0], token=None)
        self.assertFalse(s2(payload=p)["s2"]["suggestions"][0]["can_stop"])
        rows = s2(payload=payload("under-pressure.json", inventory="degraded"))["s2"]["suggestions"]
        self.assertFalse(any(x["can_stop"] for x in rows))

    def confirm(self, *extra):
        return host("suggest", FIXTURES / "under-pressure-confirm.json", *extra)

    def test_confirmed_stop_uses_the_s1_stop_and_token(self):
        r = self.confirm()
        self.assertEqual(r["phase_before"], "ask")
        self.assertEqual(r["actions"], ["act stop-job --target tok-fixture-s1-vitest"])

    def test_token_more_than_120_s_old_is_never_sent(self):
        self.assertEqual(self.confirm("--age", "119")["actions"],
                         ["act stop-job --target tok-fixture-s1-vitest"])
        r = self.confirm("--age", "121")
        self.assertEqual(r["actions"], [])
        self.assertEqual(r["phase"], "closed")
        self.assertTrue(r["banner"].startswith("Nothing done"))

    def test_the_token_comes_from_the_latest_refresh(self):
        r = self.confirm("--next", str(FIXTURES / "next" / "rebind-suggestion-fresh.json"))
        self.assertEqual(r["actions"], ["act stop-job --target tok-fixture-s1-vitest-fresh"])

    def test_perform_rereads_the_token_even_without_a_rebind(self):
        r = self.confirm("--next", str(FIXTURES / "next" / "rebind-suggestion-fresh.json"), "--no-rebind")
        self.assertEqual(r["actions"], ["act stop-job --target tok-fixture-s1-vitest-fresh"])
        r = self.confirm("--next", str(FIXTURES / "next" / "rebind-suggestion-gone.json"), "--no-rebind")
        self.assertEqual(r["actions"], [])
        self.assertTrue(r["banner"].startswith("Nothing done"))

    def test_a_suggestion_gone_or_changed_closes_the_confirm(self):
        for name, says in [("rebind-suggestion-gone.json", "no longer listed under pressure"),
                           ("rebind-suggestion-changed.json", "changed while this was open")]:
            r = self.confirm("--next", str(FIXTURES / "next" / name))
            self.assertEqual((r["phase_before"], r["actions"]), ("closed", []), name)
            self.assertIn(says, r["banner"])

    def test_confirm_says_what_it_stops_and_what_keeps_running(self):
        found = said("under-pressure-confirm.json")
        self.assertIn("Stop vitest?", found)
        self.assertTrue(any("vitest in acme-web · under Claude session “Checkout refactor”" in l for l in found))
        self.assertIn("Stop tests: send stop signal to Vitest", found)
        self.assertIn("Cancel, keep the job running", found)


class PressureSourceTests(unittest.TestCase):
    """S2.9: the kernel's pressure events refresh only while the popover is open."""

    def test_source_runs_only_while_open(self):
        r = host("pressure-watch", FIXTURES / "overview.json")
        self.assertEqual((r["active_before"], r["active_open"], r["active_closed"]), (False, True, False))
        self.assertEqual(r["events_open"], 2)
        self.assertEqual(r["events_closed"], 2)
        self.assertEqual(r["scans"], 0)      # a fixture model never spawns memmon


def job(id_="a" * 32, state="intervention_needed", changed=100.0, **extra):
    return dict({"id": id_, "label": "Search index rebuild", "state": state, "state_changed_ts": changed,
                 "intervention": {"cause": "growth", "since_ts": changed}, "footprint_bytes": 8 * 1024 ** 3,
                 "reservation_bytes": 4 * 1024 ** 3}, **extra)


class InterventionNotificationTests(unittest.TestCase):
    """S2.5 / AD-S2-8: one notification per job per state change, never repeated."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def probe(self, rounds, store=True, *extra):
        seq = self.dir / "seq.json"
        seq.write_text(json.dumps(rounds))
        args = ["--notify-probe", "--sequence", str(seq), *extra]
        if store:
            args += ["--store", str(self.dir / "store.json")]
        return run_json(*args)

    def test_one_notification_per_state_change(self):
        running = job(state="running", intervention=None)
        r = self.probe([[running], [job()], [job()], [job()], [running], [job(changed=200.0)]])
        self.assertEqual([len(x) for x in r["rounds"]], [0, 1, 0, 0, 0, 1])
        self.assertEqual(r["posted"][0]["title"], "Search index rebuild needs attention")
        self.assertIn("It grew to 8.0 GB, above its 4.0 GB reservation", r["posted"][0]["body"])

    def test_a_relaunch_does_not_repeat(self):
        self.assertEqual(len(self.probe([[job()]])["posted"]), 1)
        self.assertEqual(len(self.probe([[job()]])["posted"]), 0)

    def test_only_states_that_need_a_person(self):
        states = ["waiting", "starting", "running", "cancelling", "detached", "done", "gave_up",
                  "stopped_by_user"]
        r = self.probe([[job(id_=f"{k:032x}", state=s) for k, s in enumerate(states)]])
        self.assertEqual(r["posted"], [])
        r = self.probe([[job(state="cancelled_by_policy", changed=300.0)]])
        self.assertEqual(r["posted"][0]["title"], "memmon cancelled Search index rebuild")

    def test_notifications_off_in_config_posts_nothing_and_never_replays(self):
        cfg = self.dir / "config.json"
        cfg.write_text(json.dumps({"notifications": False, "headroom_frac": 0.2}))
        r = self.probe([[job()]], True, "--config", str(cfg))
        self.assertEqual((r["posted"], len(r["remembered"])), ([], 1))
        self.assertEqual(self.probe([[job()]])["posted"], [])        # turned back on: no replay
        cfg.write_text(json.dumps({"headroom_frac": 0.2}))
        self.assertEqual(len(self.probe([[job(changed=900.0)]], True, "--config", str(cfg))["posted"]), 1)
        self.assertEqual(len(self.probe([[job(changed=901.0)]], True, "--config", str(self.dir / "absent"))["posted"]), 1)

    def test_a_failed_hand_over_is_retried_and_not_remembered(self):
        r = self.probe([[job()], [job()], [job()]], True, "--fail-adds", "1")
        self.assertEqual([len(x) for x in r["rounds"]], [1, 1, 0])
        self.assertEqual(len(r["remembered"]), 1)
        r = self.probe([[job(changed=500.0)]], True, "--fail-adds", "5")
        self.assertEqual(len(r["remembered"]), 1)          # the failed one is not added
        self.assertEqual(len(self.probe([[job(changed=500.0)]])["posted"]), 1)

    def runner_dir(self):
        runner = self.dir / "runner"
        (runner / "coord").mkdir(parents=True, exist_ok=True)
        return runner

    def record(self, runner, rid, body=None, lease="held"):
        (runner / f"{rid}.json").write_text(body if body is not None else json.dumps(job(id_=rid)))
        if lease is None:
            return
        fh = open(runner / f"{rid}.lease", "w")
        self.addCleanup(fh.close)
        if lease == "held":
            fcntl.flock(fh, fcntl.LOCK_EX)

    def test_runner_records_are_read_from_disk_by_run_id_only(self):
        runner = self.runner_dir()
        self.record(runner, "b" * 32)
        (runner / "coord" / "admission-state.json").write_text(json.dumps(job(id_="c" * 32)))
        (runner / "not-a-run.json").write_text(json.dumps(job(id_="d" * 32)))
        r = self.probe([], True, "--runner-dir", str(runner))
        self.assertEqual([p["id"].split("|")[0] for p in r["posted"]], ["b" * 32])

    def test_a_record_without_a_live_lease_never_notifies(self):
        runner = self.runner_dir()
        self.record(runner, "1" * 32, lease="free")       # the runner died: lease unlocked
        self.record(runner, "2" * 32, lease=None)         # no lease at all
        self.record(runner, "3" * 32)
        r = self.probe([], True, "--runner-dir", str(runner))
        self.assertEqual([p["id"].split("|")[0] for p in r["posted"]], ["3" * 32])

    def test_oversized_garbage_and_excess_records_are_skipped(self):
        runner = self.runner_dir()
        big = json.dumps(dict(job(id_="4" * 32), pad="x" * (300 * 1024)))
        self.record(runner, "4" * 32, body=big)
        self.record(runner, "5" * 32, body="{not json")
        self.record(runner, "6" * 32, body="[1, 2]")
        self.assertEqual(self.probe([], True, "--runner-dir", str(runner))["posted"], [])
        for k in range(70):
            self.record(runner, f"{k + 0x100:032x}")
        r = self.probe([], True, "--runner-dir", str(runner))
        self.assertLessEqual(len(r["posted"]), 64)
        self.assertGreater(len(r["posted"]), 0)


SETTINGS = {"schema_version": 1,
            "gate_mode": {"value": "block-critical", "source": "config",
                          "choices": ["block-critical", "block", "warn", "off"]},
            "paused_until": None, "runner_mode": "protect", "auto_cancel_interruptible": False,
            "pressure_suggestions": True, "notifications": True, "state_dir": "/opt/example/memmon"}


class SettingsPanelTests(StubCase):
    """D43: the Settings panel. Every change goes through memmon and shows
    only memmon's answer."""

    def settings(self, fixture, steps, script=None):
        extra = ["--do", ",".join(steps)] + (["--script", script] if script else [])
        return host("settings", FIXTURES / fixture, *extra)

    def memmon_stub(self, set_reply, code=0):
        """Records every argv; `settings set` answers `set_reply`, the rest the fixture state."""
        calls = self.dir / "calls.jsonl"
        script = self.dir / "memmon_settings.py"
        script.write_text(
            "import json, sys\n"
            f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "a = sys.argv[1:]\n"
            "if a[:2] == ['settings', 'set']:\n"
            f"    print(json.dumps({set_reply!r})); sys.exit({code})\n"
            f"print(json.dumps({SETTINGS!r}))\n")
        return str(script), calls

    def test_each_control_logs_its_exact_argv(self):
        r = self.settings("settings-panel.json", [
            "gate:block", "runner:observe", "auto:on", "suggest:off", "notify:off",
            "pause:1h", "pause:8h", "pause:forever", "resume", "folder", "open"])
        self.assertEqual(r["actions"], [
            "memmon settings set gate_mode block",
            "memmon settings set runner_mode observe",
            "memmon settings set auto_cancel_interruptible true",
            "memmon settings set pressure_suggestions false",
            "memmon settings set notifications false",
            "memmon --off 1h", "memmon --off 8h", "memmon --off", "memmon --on",
            "open /opt/example/memmon",
            "memmon settings --json"])

    def test_env_locked_gate_mode_is_never_written(self):
        r = self.settings("settings-env-locked.json", ["gate:block", "runner:paused"])
        self.assertTrue(r["locked"])
        self.assertEqual(r["actions"], ["memmon settings set runner_mode paused"])
        found = said("settings-env-locked.json")
        self.assertIn("Gate mode, Warn only, locked: MEMMON_GATE in the hook environment overrides this setting",
                      found)
        self.assertIn("MEMMON_GATE in the hook environment overrides this setting", " ".join(found))

    def test_a_change_shows_memmon_answer(self):
        reply = dict(SETTINGS, gate_mode=dict(SETTINGS["gate_mode"], value="block"), notifications=False)
        script, calls = self.memmon_stub(reply)
        r = self.settings("settings-panel.json", ["gate:block", "intervene"], script)
        self.assertEqual(json.loads(calls.read_text().splitlines()[0]), ["settings", "set", "gate_mode", "block"])
        self.assertEqual((r["gate_mode"], r["error"]), ("block", None))
        # memmon said notifications are off: MemmonBar's own notices follow.
        self.assertEqual((r["notifications"], r["alerts_enabled"], r["posted"]), (False, False, 0))

    def test_a_refused_change_is_shown_and_never_applied(self):
        script, _ = self.memmon_stub({"error": "gate_mode must be one of block-critical, block, warn, off",
                                      "key": "gate_mode"}, code=2)
        r = self.settings("settings-panel.json", ["gate:block"], script)
        self.assertEqual(r["gate_mode"], "block-critical")
        self.assertEqual(r["error"], "Could not change command protection: "
                                     "gate_mode must be one of block-critical, block, warn, off.")
        script, _ = self.memmon_stub("not json")
        r = self.settings("settings-panel.json", ["notify:off"], script)
        self.assertEqual(r["notifications"], True)
        self.assertEqual(r["error"], "Could not change notifications: memmon's answer could not be read.")

    def test_notifications_on_still_posts(self):
        self.assertEqual(self.settings("settings-panel.json", ["intervene"])["posted"], 1)

    def test_only_the_header_closes_settings(self):
        found = said("settings-panel.json")
        self.assertNotIn("Done", found)
        self.assertEqual(found.count("Close settings"), 1)        # the header's ×
        r = self.settings("settings-panel.json", ["close"])
        self.assertFalse(r["open"])

    def test_labels(self):
        found = said("settings-panel.json")
        for label in ["Close settings", "Gate mode, Stop at Critical", "Choose Stop at Danger",
                      "Pause command protection for 1 h", "Pause command protection until resumed",
                      "Auto-cancel interruptible jobs under CRITICAL. memmon stops jobs started with "
                      "--interruptible after 10 s at CRITICAL",
                      "Suggest stops under pressure. Lists heavy jobs memmon can’t hold. memmon never stops "
                      "them on its own.", "Open data folder", "Set protection mode to Observe"]:
            self.assertIn(label, found)
        self.assertNotIn("Resume command protection", found)          # not paused
        self.assertIn("Resume command protection", said("settings-env-locked.json"))
        self.assertIn("Settings", said("overview.json"))                # the header's gear
        self.assertIn("Settings error: Could not change protection mode: runner_mode must be one of "
                      "protect, observe, paused.", said("settings-error.json"))


INDEX_RUN = "3f0c2a9e5b7d4c1a8e6f2b0d9c4a7e15"


class NotificationClickTests(unittest.TestCase):
    """D44: a click on an intervention notification opens the normal confirm,
    with the token from the refresh the click starts, and never stops."""

    def click(self, *extra):
        return host("notice-click", FIXTURES / "managed-jobs-intervention.json", "--run-id", INDEX_RUN,
                    "--label", "Search index rebuild", *extra)

    def test_click_selects_the_job_and_opens_its_stop_confirm(self):
        r = self.click("--next", str(FIXTURES / "next" / "notice-fresh.json"))
        self.assertEqual(r["selected"], "job:" + INDEX_RUN)
        self.assertEqual((r["phase"], r["confirm_action"]), ("ask", "stop-managed-job"))
        self.assertEqual(r["actions"], [])            # nothing is stopped by the click

    def test_the_confirm_carries_the_fresh_token(self):
        r = self.click("--next", str(FIXTURES / "next" / "notice-fresh.json"))
        self.assertEqual(r["confirm_token"], "tok-fixture-managed-index-fresh")

    def test_the_confirm_is_built_from_the_fresh_payload_not_the_old_one(self):
        # The job's owner restarted under a new identity: a confirm opened on
        # the old payload would be closed as "changed"; the fresh one opens.
        r = self.click("--next", str(FIXTURES / "next" / "notice-restarted.json"))
        self.assertEqual((r["phase"], r["confirm_token"]), ("ask", "tok-fixture-managed-index-restarted"))
        self.assertIsNone(r["banner"])

    def test_only_the_confirm_click_stops(self):
        r = self.click("--next", str(FIXTURES / "next" / "notice-fresh.json"), "--confirm-click")
        self.assertEqual(r["actions"], ["perform"])

    def test_a_job_that_is_gone_says_so(self):
        r = self.click("--next", str(FIXTURES / "next" / "notice-gone.json"))
        self.assertEqual((r["phase"], r["actions"]), ("closed", []))
        self.assertEqual(r["banner"], "Nothing done — Search index rebuild is no longer running.")

    def test_a_job_that_recovered_is_selected_without_a_confirm(self):
        r = self.click("--next", str(FIXTURES / "next" / "notice-recovered.json"))
        self.assertEqual((r["phase"], r["selected"]), ("closed", "job:" + INDEX_RUN))
        self.assertIn("no longer needs attention", r["banner"])

    def test_a_policy_cancel_shows_its_outcome_and_no_confirm(self):
        r = self.click("--state", "cancelled_by_policy", "--next", str(FIXTURES / "next" / "notice-gone.json"))
        self.assertEqual((r["phase"], r["actions"]), ("closed", []))
        self.assertTrue(r["banner"].startswith("Search index rebuild was cancelled by policy"))

    def test_a_busy_confirm_is_never_replaced_by_a_click(self):
        for busy in ("working", "partial"):
            r = self.click("--next", str(FIXTURES / "next" / "notice-fresh.json"), "--busy", busy)
            self.assertEqual((r["kept_phase"], r["still_pending"]), (busy, True), busy)
            self.assertNotEqual(r["kept_owner"], "job:" + INDEX_RUN)
            # Once that confirm has closed, the next refresh opens the notice's confirm.
            self.assertEqual((r["phase"], r["confirm_token"]), ("ask", "tok-fixture-managed-index-fresh"), busy)
            self.assertEqual(r["actions"], [])

    def test_a_click_is_ignored_while_notifications_are_off(self):
        r = self.click("--next", str(FIXTURES / "next" / "notice-fresh.json"), "--notifications-off")
        self.assertEqual((r["pending"], r["selected"], r["phase"], r["banner"], r["actions"]),
                         (False, None, "closed", None, []))

    def test_notifications_carry_the_job_but_no_token(self):
        with tempfile.TemporaryDirectory() as d:
            seq = Path(d) / "seq.json"
            seq.write_text(json.dumps([[job(wrapper_pid=48500, child_pid=48501)]]))
            r = run_json("--notify-probe", "--sequence", str(seq))
        info = r["posted"][0]["user_info"]
        self.assertEqual(info, {"run_id": "a" * 32, "state": "intervention_needed",
                                "label": "Search index rebuild", "wrapper_pid": 48500, "child_pid": 48501})


class UsageCardTests(StubCase):
    """D45: the "Last 7 days" card. It is collapsed by default, reads
    history only while open (cached 5 min), and never invents a day."""

    def probe(self, fixture):
        return s2(fixture)["s2"]["usage"]

    def test_three_views_decode_and_summarise(self):
        u = self.probe("usage-protection.json")["views"]
        self.assertEqual([b["label"] for b in u["memory"]["bars"]], ["Fri", "Sat", "Sun", "Mon", "Tue", "Wed", "Thu"])
        self.assertEqual(u["memory"]["summary"], "Today’s peak 41.2 GB of 48.0 GB; highest this week 44.1 GB.")
        self.assertEqual(u["memory"]["bars"][4]["spoken"], "Tue, peak 44.1 GB, average 33.8 GB")
        self.assertEqual(u["consumers"]["summary"],
                         "Top consumers on average: Claude sessions 11.2 GB, Shared services 7.1 GB, Mac apps 4.6 GB.")
        self.assertEqual(u["protection"]["summary"],
                         "16 warned and 7 stopped this week; managed jobs held 7, cancelled 1.")
        self.assertIn("managed-job holds and cancels not recorded",
                      self.probe("usage-memory.json")["views"]["protection"]["summary"])

    def test_empty_days_stay_empty(self):
        u = self.probe("usage-empty-days.json")["views"]
        for view in ("memory", "consumers"):
            sat, sun = u[view]["bars"][1], u[view]["bars"][2]
            self.assertEqual((sat["fraction"], sat["empty"], sat["spoken"]), (None, True, "Sat, no samples"), view)
            self.assertEqual(sun["fraction"], None, view)
        tue = u["memory"]["bars"][4]
        self.assertTrue(tue["few"])
        self.assertTrue(tue["spoken"].endswith("only 42 samples"))
        found = said("usage-empty-days.json")
        self.assertIn("Sat, no samples", found)
        self.assertTrue(any("No samples: Sat, Sun" in l for l in found), found)
        self.assertTrue(any("Few samples: Tue (42)" in l for l in found), found)

    def test_dev_and_missing_sections_decode_as_optional(self):
        p = payload("usage-consumers.json")
        for k, d in enumerate(p["_usage"]["series"]):
            d["by_section"] = {"claude": 4 * 1024 ** 3, "dev": (k + 1) * 1024 ** 3, "codex": None}
            d["runner"] = {"held": None, "cancelled": None}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "u.json"
            path.write_text(json.dumps(dict(p, _view={"usage": "consumers"})))
            u = run_json("--sections-probe", "--fixture", str(path))["s2"]["usage"]
        self.assertEqual(u["top"], ["claude", "dev"])
        self.assertIn("Terminals & editors 7.0 GB", u["views"]["consumers"]["bars"][6]["spoken"])
        self.assertNotIn("Codex", u["views"]["consumers"]["bars"][6]["spoken"])
        self.assertFalse(u["runner_recorded"])
        self.assertIn("not recorded", u["views"]["protection"]["summary"])

    def test_dev_as_the_largest_section_leads_the_legend(self):
        u = self.probe("usage-consumers-dev.json")
        self.assertEqual(u["top"], ["dev", "claude", "service"])
        self.assertTrue(u["views"]["consumers"]["summary"].startswith(
            "Top consumers on average: Terminals & editors 14.7 GB"))
        self.assertIn("Terminals & editors 15.9 GB", u["views"]["consumers"]["bars"][6]["spoken"])
        self.assertIn("Terminals & editors", " ".join(said("usage-consumers-dev.json")))

    def test_headline_value_and_caption_per_view(self):
        u = self.probe("usage-protection.json")["views"]
        self.assertEqual((u["memory"]["value"], u["memory"]["caption"]), ("41.2 GB", "today’s peak · week high 44.1 GB"))
        self.assertEqual((u["consumers"]["value"], u["consumers"]["caption"]), ("11.6 GB", "today’s top · Claude sessions"))
        self.assertEqual((u["protection"]["value"], u["protection"]["caption"]), ("23", "7 stopped · 16 warned this week"))
        self.assertEqual(u["memory"]["axis"], ["0", "24 GB", "48 GB"])
        self.assertEqual(u["consumers"]["axis"], ["0", "18 GB", "36 GB"])
        self.assertEqual(u["protection"]["axis"], ["0", "4", "8"])
        dev = self.probe("usage-consumers-dev.json")["views"]["consumers"]
        self.assertEqual((dev["value"], dev["caption"]), ("15.9 GB", "today’s top · Terminals & editors"))
        # The sentence stays as the spoken summary of the headline.
        self.assertIn(u["protection"]["summary"], said("usage-protection.json"))

    def test_estimated_memory_is_marked(self):
        p = payload("usage-memory.json")
        u = self.probe("usage-memory.json")["views"]["memory"]
        self.assertEqual(u["value"], "41.2 GB")                      # today is measured
        self.assertTrue(u["bars"][0]["spoken"].endswith(", estimated"))
        self.assertIn("≈ estimated from free memory on older days", " ".join(said("usage-memory.json")))
        for d in p["_usage"]["series"]:
            d["mem_basis"] = "estimated"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "u.json"
            path.write_text(json.dumps(dict(p, _view={"usage": "memory"})))
            est = run_json("--sections-probe", "--fixture", str(path))["s2"]["usage"]["views"]["memory"]
        self.assertEqual((est["value"], est["caption"]), ("≈ 41.2 GB", "today’s peak · week high ≈ 44.1 GB"))
        # Without the field nothing is marked.
        plain = self.probe("usage-protection.json")["views"]["memory"]
        self.assertFalse(any("estimated" in b["spoken"] for b in plain["bars"]))
        self.assertNotIn("≈", plain["value"] + plain["caption"])

    def test_today_is_found_by_date_not_position(self):
        # The fixtures' _now is 09:00 UTC on their last date, the same local
        # date from UTC-9 to UTC+14.
        p = payload("usage-memory.json")
        import datetime
        for d in p["_usage"]["series"]:
            d["date"] = (datetime.date.fromisoformat(d["date"]) - datetime.timedelta(days=1)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "u.json"
            path.write_text(json.dumps(dict(p, _view={"usage": "memory"})))
            u = run_json("--sections-probe", "--fixture", str(path))["s2"]["usage"]
        self.assertTrue(u["views"]["memory"]["summary"].startswith("No samples today"), u["views"]["memory"]["summary"])
        self.assertTrue(self.probe("usage-memory.json")["views"]["memory"]["summary"].startswith("Today’s peak 41.2 GB"))

    def test_an_open_card_rereads_expired_history_when_the_popover_opens(self):
        script, calls = self.stub()
        host("usage", FIXTURES / "overview.json", "--script", script,
             "--do", "expand,age:100,popover,age:201,popover,popover")
        self.assertEqual(self.calls(calls).count("usage"), 2)

    def test_a_collapsed_card_never_reads_when_the_popover_opens(self):
        script, calls = self.stub()
        host("usage", FIXTURES / "overview.json", "--script", script,
             "--do", "expand,collapse,age:400,popover,popover")
        self.assertEqual(self.calls(calls).count("usage"), 1)

    def test_a_day_with_no_samples_drops_any_values_it_carries(self):
        p = payload("usage-empty-days.json")
        p["_usage"]["series"][1].update(mem_peak_bytes=40 * 1024 ** 3, by_section={"claude": 1})
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "u.json"
            path.write_text(json.dumps(dict(p, _view={"usage": "memory"})))
            u = run_json("--sections-probe", "--fixture", str(path))["s2"]["usage"]["views"]
        self.assertIsNone(u["memory"]["bars"][1]["fraction"])
        self.assertIsNone(u["consumers"]["bars"][1]["fraction"])

    def stub(self):
        calls = self.dir / "calls.jsonl"
        owners = self.dir / "owners.json"
        owners.write_text(json.dumps(effective(FIXTURES / "overview.json")))
        usage = effective(FIXTURES / "usage-memory.json")["_usage"]
        script = self.dir / "memmon_usage.py"
        script.write_text(
            "import json, sys\n"
            f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:2] == ['usage']:\n"
            f"    print(json.dumps({usage!r}))\n"
            "else:\n"
            f"    print(open({str(owners)!r}).read())\n")
        return str(script), calls

    def calls(self, path):
        return [json.loads(l)[0] for l in path.read_text().splitlines()] if path.exists() else []

    def test_no_fetch_while_collapsed_or_on_refresh(self):
        script, calls = self.stub()
        r = host("usage", FIXTURES / "overview.json", "--script", script, "--do", "refresh,refresh")
        self.assertFalse(r["open"])
        self.assertEqual(self.calls(calls), ["owners", "owners"])
        r = host("usage", FIXTURES / "overview.json", "--script", script, "--do", "expand,refresh,view:consumers")
        self.assertTrue(r["loaded"])
        self.assertEqual(self.calls(calls).count("usage"), 1)

    def test_history_is_cached_for_five_minutes(self):
        script, calls = self.stub()
        host("usage", FIXTURES / "overview.json", "--script", script,
             "--do", "expand,collapse,age:299,expand,collapse,age:2,expand")
        self.assertEqual(self.calls(calls).count("usage"), 2)

    def test_collapsed_by_default_and_spoken_as_one_line(self):
        self.assertIn("Last 7 days", said("overview.json"))
        cached = said("usage-collapsed.json")
        self.assertIn("Last 7 days, Today’s peak 41.2 GB of 48.0 GB; highest this week 44.1 GB.", cached)
        expanded = said("usage-memory.json")
        self.assertIn("Today’s peak 41.2 GB of 48.0 GB; highest this week 44.1 GB.", expanded)
        self.assertIn("Fri, peak about 34.2 GB, average 26.1 GB, estimated", expanded)
        self.assertIn("Choose Top consumers", expanded)


class ThemeTests(unittest.TestCase):
    """D48: MemmonBar's own theme, stored in UserDefaults (a file here) and
    applied to the popover. Renders stay on --light/--dark."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = str(Path(self.tmp.name) / "prefs.json")

    def probe(self, *extra):
        return run_json("--theme-probe", "--store", self.store, *extra)

    def test_default_is_system_with_no_forced_appearance(self):
        r = self.probe()
        self.assertEqual((r["theme"], r["stored"], r["appearance"]), ("system", None, None))

    def test_a_choice_is_stored_applied_and_kept_across_launches(self):
        r = self.probe("--set", "dark")
        self.assertEqual((r["stored"], r["appearance"]), ("dark", "NSAppearanceNameDarkAqua"))
        again = self.probe()                       # a relaunch reads it back
        self.assertEqual((again["before"], again["before_appearance"]), ("dark", "NSAppearanceNameDarkAqua"))
        r = self.probe("--set", "light")
        self.assertEqual((r["stored"], r["appearance"]), ("light", "NSAppearanceNameAqua"))
        r = self.probe("--set", "system")
        self.assertEqual((r["stored"], r["appearance"]), ("system", None))

    def test_an_unknown_stored_value_is_system(self):
        Path(self.store).write_text(json.dumps({"memmon.theme": "sepia"}))
        self.assertEqual((self.probe()["theme"], self.probe()["appearance"]), ("system", None))

    def test_settings_offers_the_theme(self):
        found = said("settings-panel.json")
        self.assertIn("Theme, System", found)
        self.assertIn("System, current choice", found)
        self.assertIn("Choose Dark", found)


class ExplainTests(StubCase):
    """D47: a click shows exactly what would be sent; only Send asks Claude,
    bounded to 60 s and cancellable. The reply is plain text memmon never
    acts on."""

    PREVIEW = {"preview": "pressure HEALTHY; used 39.2 of 48.0 GB\nClaude session \"Checkout refactor\" 8.9 GB",
               "chars": 74}
    REPLY = {"text": "**Checkout refactor** holds 8.9 GB.\nRun `memmon act stop-job --target x` to free it.",
             "model": "claude-haiku-5-5", "chars_sent": 812, "elapsed_s": 3.1}

    def stub(self, reply=None, code=0, sleep=0.0):
        calls = self.dir / "calls.jsonl"
        owners = self.dir / "owners.json"
        owners.write_text(json.dumps(effective(FIXTURES / "overview.json")))
        script = self.dir / "memmon_explain_stub.py"
        script.write_text(
            "import json, sys, time\n"
            f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "a = sys.argv[1:]\n"
            "if a[:2] == ['explain', '--preview']:\n"
            f"    print(json.dumps({self.PREVIEW!r}))\n"
            "elif a[:1] == ['explain']:\n"
            f"    time.sleep({sleep}); print(json.dumps({reply if reply is not None else self.REPLY!r})); sys.exit({code})\n"
            "else:\n"
            f"    print(open({str(owners)!r}).read())\n")
        return str(script), calls

    def run_steps(self, steps, **stub):
        script, calls = self.stub(**stub)
        r = host("explain", FIXTURES / "overview.json", "--script", script, "--do", steps)
        made = [json.loads(l) for l in calls.read_text().splitlines()] if calls.exists() else []
        return r, [c for c in made if c[0] == "explain"]

    def test_nothing_is_asked_without_a_click(self):
        r, made = self.run_steps("refresh,popover,refresh")
        self.assertEqual((r["open"], made), (False, []))

    def test_a_click_shows_the_preview_and_sends_nothing(self):
        r, made = self.run_steps("click")
        self.assertEqual(made, [["explain", "--preview", "--json"]])
        self.assertEqual(r["preview"], self.PREVIEW["preview"])
        self.assertIsNone(r["text"])

    def test_send_asks_once_and_shows_the_reply_as_is(self):
        r, made = self.run_steps("click,send-twice")
        self.assertEqual(made, [["explain", "--preview", "--json"], ["explain", "--json"]])
        self.assertEqual(r["text"], self.REPLY["text"])
        # A command in the reply is only text: memmon runs nothing after it.
        self.assertFalse(any(c[1:2] == ["act"] for c in made))

    def test_send_needs_the_preview_first(self):
        r, made = self.run_steps("send")
        self.assertEqual(made, [])

    def test_cancel_drops_a_late_answer(self):
        r, made = self.run_steps("click,send-nowait,cancel,wait", sleep=1.0)
        self.assertEqual(made[-1], ["explain", "--json"])
        self.assertIsNone(r["text"])
        self.assertEqual(r["error"], "Cancelled. Nothing was changed.")

    def test_the_wait_is_bounded_to_60_s(self):
        self.assertEqual(run_json("--constants")["explain_timeout"], 60)

    def test_errors_are_readable(self):
        cases = [({"error": "claude not found on PATH, ~/.local/bin, /opt/homebrew/bin or /usr/local/bin"},
                  "Claude Code isn’t installed, or memmon can’t find it"),
                 ({"error": "claude did not answer within 60 s"}, "Claude did not answer within 60 s."),
                 ({"error": "claude exited 1: rate limited"}, "Claude stopped with an error: exit 1: rate limited."),
                 ("not json", "Could not ask Claude: memmon's answer could not be read.")]
        for reply, says in cases:
            r, _ = self.run_steps("click,send", reply=reply, code=0 if reply == "not json" else 2)
            self.assertTrue(r["error"].startswith(says), (reply, r["error"]))
            self.assertIsNone(r["text"])

    def test_fixture_mode_logs_the_exact_argv(self):
        self.assertEqual(host("explain", FIXTURES / "overview.json", "--do", "click")["actions"],
                         ["memmon explain --preview --json"])
        self.assertEqual(host("explain", FIXTURES / "explain-preview.json", "--do", "send")["actions"],
                         ["memmon explain --json"])

    def test_cards_say_what_they_show(self):
        found = said("explain-reply.json")
        self.assertTrue(any(l.startswith("Checkout refactor (Claude session) holds 8.9 GB") for l in found), found)
        self.assertTrue(any("**Billing API tests** (Codex)" in l for l in found), found)
        self.assertTrue(any(l.startswith("From Claude. memmon never acts on it. Sent 812 characters") for l in found))
        self.assertIn("Ask Claude what to do about memory", said("overview.json"))
        preview = said("explain-preview.json")
        self.assertIn("Send this summary to Claude", preview)
        self.assertTrue(any(l.startswith("This is all that is sent (") for l in preview), preview)
        self.assertIn("Cancel asking Claude", said("explain-busy.json"))
        self.assertTrue(any(l.startswith("Claude Code isn’t installed") for l in said("explain-error.json")))
