"""S2.7 route: memmon_route.sh and route-classify, grouped by axis.

Every case runs the real POSIX launcher on an assembled invocation shaped
like the one the G1 probe recorded from Claude Code. The installed memmon.py
is a shim in a temp HOME: route-classify goes to the real classifier, and
`run` only records that it was called, so no heavy command ever runs. The
package tools on PATH are fakes that count their invocations."""

import json
import os
import shutil
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

import memmon_route as route

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "memmon_route.sh")
# The installed memmon.py: run as a script it only records a `run`; imported
# (by the classifier) it is the real memmon.
SHIM = """import os, sys
if __name__ != "__main__":
    import importlib.util
    sys.path.insert(1, {repo!r})
    spec = importlib.util.spec_from_file_location("memmon_real", {repo!r} + "/memmon.py")
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    globals().update({{k: getattr(real, k) for k in dir(real) if not k.startswith("__")}})
else:
    state = os.path.join(os.environ["HOME"], ".claude", "memmon")
    if sys.argv[1] == "run":
        with open(os.path.join(state, "wrapped.jsonl"), "a") as fh:
            fh.write(__import__("json").dumps(sys.argv[2:]) + "\\n")
        sys.exit(0)
    sys.exit(99)
"""
HANG = "import time\ntime.sleep(5)\n"
FORCE_WRAP = "def classify_main(invocation):\n    print('wrap\\tforced')\n"
FAKE_TOOL = """#!/bin/sh
echo "$(basename "$0") $*" >> "$HOME/calls"
exit "${FAKE_EXIT:-0}"
"""
TOOLS = ("pnpm", "tsc", "npx", "docker", "colima", "expo", "vitest")


def assembled(cmd):
    """Claude Code's Bash invocation, as observed by the G1 probe."""
    quoted = shlex.quote(cmd)
    return ("source /tmp/snapshot-zsh-0000.sh 2>/dev/null || true && "
            "{ shopt -u extglob || setopt NO_EXTENDED_GLOB NO_BARE_GLOB_QUAL; } >/dev/null 2>&1 || true && "
            f"eval {quoted} < /dev/null && pwd -P >| /tmp/claude-0000-cwd")


BASH_TOOL = {"CLAUDE_PID": "4242"}
HOOK = {"CLAUDE_PID": "4242", "CLAUDE_PROJECT_DIR": "/tmp/acme-web"}


class RouteScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.state = self.home / ".claude" / "memmon"
        (self.state / "runner" / "coord").mkdir(parents=True)
        (self.state / "memmon.py").write_text(SHIM.format(repo=HERE))
        shutil.copy(os.path.join(HERE, "memmon_route.py"), self.state / "memmon_route.py")
        self.bin = self.home / "bin"
        self.bin.mkdir()
        for tool in TOOLS:
            p = self.bin / tool
            p.write_text(FAKE_TOOL)
            p.chmod(0o755)
        self.work = self.home / "acme-web"
        (self.work / "x").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def route(self, cmd, origin=BASH_TOOL, path=None, extra=None, raw=False):
        env = {"HOME": str(self.home), "PATH": path or f"{self.bin}:/usr/bin:/bin",
               **origin, **(extra or {})}
        return subprocess.run(["/bin/sh", SCRIPT, cmd if raw else assembled(cmd)],
                              cwd=self.work, env=env, capture_output=True, text=True,
                              timeout=20)

    def wrapped(self):
        p = self.state / "wrapped.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def calls(self):
        p = self.home / "calls"
        return p.read_text().splitlines() if p.exists() else []

    def assert_passes(self, cmd, expect_calls=None, **kw):
        out = self.route(cmd, **kw)
        self.assertEqual(self.wrapped(), [], f"{cmd!r} must not be wrapped")
        if expect_calls is not None:
            self.assertEqual(len(self.calls()), expect_calls, (cmd, self.calls()))
        return out

    # --------------------------------------------------- allow side (B16)

    def test_lexical_and_quoted(self):
        (self.work / "f").write_text("pnpm test\n")
        out = self.assert_passes('grep "pnpm test" f', expect_calls=0)
        self.assertEqual(out.stdout, "pnpm test\n")
        out = self.assert_passes("echo tsc", expect_calls=0)
        self.assertEqual(out.stdout, "tsc\n")

    def test_server_or_daemon(self):
        for cmd in ("pnpm dev", "docker compose up -d", "colima start", "expo start",
                    "pnpm vitest --watch"):
            with self.subTest(cmd):
                (self.home / "calls").unlink(missing_ok=True)
                self.assert_passes(cmd, expect_calls=1)

    def test_background(self):
        out = self.assert_passes("pnpm test & wait", expect_calls=1)
        self.assertEqual(out.returncode, 0)
        self.assert_passes("nohup pnpm test", expect_calls=2)

    def test_already_wrapped(self):
        self.assert_passes("memmon run -- pnpm test", expect_calls=0)
        self.assert_passes("pnpm test", expect_calls=1, extra={"MEMMON_RUN_ID": "a" * 32})

    def test_hook_and_mcp_origins_pass_through(self):
        # A denying PreToolUse hook whose text is eligible: runs once, and its
        # exit 2 still blocks (B16, B30).
        out = self.route("pnpm test || exit 2", origin=HOOK, raw=True,
                         extra={"FAKE_EXIT": "1"})
        self.assertEqual(out.returncode, 2)
        self.assertEqual(self.calls(), ["pnpm test"])
        self.assertEqual(self.wrapped(), [])
        # MCP startup arrives as a plain command line with the project dir set.
        out = self.route("docker run -i --rm acme/mcp-server", origin=HOOK, raw=True)
        self.assertEqual(self.wrapped(), [])
        self.assertEqual(self.calls()[-1], "docker run -i --rm acme/mcp-server")
        # No Claude process at all (a terminal using the prefix by hand).
        self.route("pnpm test", origin={})
        self.assertEqual(self.wrapped(), [])

    def test_launcher_checks_origin_before_the_classifier(self):
        """The launcher's own origin check, with a classifier that would wrap
        anything: hooks, MCP startup and non-Claude callers still pass."""
        (self.state / "memmon_route.py").write_text(FORCE_WRAP)
        for origin in (HOOK, {}, {"CLAUDE_PROJECT_DIR": "/tmp/acme-web"}):
            with self.subTest(origin=origin):
                (self.state / "wrapped.jsonl").unlink(missing_ok=True)
                self.route("pnpm test", origin=origin)
                self.assertEqual(self.wrapped(), [])
        self.route("pnpm test")
        self.assertEqual(len(self.wrapped()), 1)

    def test_denying_hook_with_route_on_queue_full_and_telemetry_broken(self):
        """B30: the runner would refuse (124/125) if it were reached; the
        hook never reaches it."""
        (self.state / "memmon.py").write_text("import sys; sys.exit(124)\n")
        (self.state / "memmon_route.py").write_text(FORCE_WRAP)
        out = self.route("pnpm test || exit 2", origin=HOOK, raw=True, extra={"FAKE_EXIT": "1"})
        self.assertEqual((out.returncode, self.calls()), (2, ["pnpm test"]))

    def test_off_flag(self):
        (self.state / "runner/coord/route.off").touch()
        self.assert_passes("pnpm test", expect_calls=1)

    def test_classifier_failure_or_timeout(self):
        (self.state / "memmon_route.py").write_text(HANG)
        t0 = time.monotonic()
        out = self.assert_passes("pnpm test", expect_calls=1)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(out.stderr, "")
        (self.state / "memmon_route.py").write_text("raise SystemExit('broken install')\n")
        self.assert_passes("pnpm test", expect_calls=2)
        (self.state / "memmon_route.py").unlink()
        self.assert_passes("pnpm test", expect_calls=3)

    def test_benign_commands_run_immediately_with_telemetry_broken(self):
        """B21: benign commands never reach the classifier or the runner."""
        (self.state / "memmon_route.py").write_text("import time; time.sleep(10)\n")
        t0 = time.monotonic()
        out = self.route("echo hello && ls x")
        self.assertEqual(out.stdout, "hello\n")
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_exit_status_output_and_quoting_preserved(self):
        literal = "$(touch nope) `pwd` spaces \"dq\" 'sq'"
        out = self.route(f"printf '%s\\n' {shlex.quote(literal)}; exit 7")
        self.assertEqual((out.returncode, out.stdout), (7, literal + "\n"))
        self.assertFalse((self.work / "nope").exists())

    # --------------------------------------------------- block side (B17)

    def test_heavy_commands_are_wrapped(self):
        for cmd, label in (("pnpm typecheck", "pnpm typecheck"), ("cd x && tsc -b", "tsc"),
                           ("npx vitest run", "npx vitest"),
                           ("echo 'memmon run'; pnpm test", "pnpm test")):
            with self.subTest(cmd):
                (self.state / "wrapped.jsonl").unlink(missing_ok=True)
                out = self.route(cmd)
                self.assertEqual(out.returncode, 0, out.stderr)
                rows = self.wrapped()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][:4], ["--via", "route", "--label", label])
                self.assertEqual(rows[0][4:6], ["--", "/bin/bash"])
                self.assertEqual(rows[0][6:], ["-c", assembled(cmd)])
        self.assertEqual(self.calls(), [], "a wrapped command runs only inside the runner")

    def test_minimal_path_still_wraps(self):
        """B18."""
        out = self.route("pnpm typecheck", path="/usr/bin:/bin")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(self.wrapped()), 1)

    def test_pass_through_is_cheap(self):
        """Bench (e) proxy: the pass-through path never starts Python."""
        times = []
        for _ in range(40):
            t0 = time.perf_counter()
            self.route("echo hi")
            times.append(time.perf_counter() - t0)
        times.sort()
        self.assertLess(times[int(len(times) * 0.95) - 1], 0.05, times)


class RouteClassifyTests(unittest.TestCase):
    def check(self, cmd, env=BASH_TOOL):
        return route.route_classify(assembled(cmd), env=env)

    def test_inner_command_unwraps_eval(self):
        self.assertEqual(route.inner_command(assembled("pnpm test && echo 'x y'")),
                         "pnpm test && echo 'x y'")
        self.assertEqual(route.inner_command("pnpm test"), "pnpm test")

    def test_verdicts(self):
        self.assertEqual(self.check("pnpm typecheck"), ("wrap", "pnpm typecheck"))
        self.assertEqual(self.check("pnpm test")[0], "wrap")
        self.assertEqual(self.check("pnpm dev")[1], "server or watcher")
        self.assertEqual(self.check("pnpm test &")[1], "sent to the background")
        self.assertEqual(self.check("echo a >&2 && pnpm test")[0], "wrap")
        self.assertEqual(self.check("docker compose up")[1], "server or daemon")
        self.assertEqual(self.check("docker run -it img")[1], "server or daemon")
        self.assertEqual(self.check("docker build .")[0], "wrap")
        self.assertEqual(self.check("pnpm test", env=HOOK)[1], "not a Bash tool call")
        self.assertEqual(self.check("ls")[1], "not heavy")

    def test_cli_fails_open(self):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = route.cli(["route-classify", assembled("pnpm test")], "/nonexistent",
                             classify=lambda c: 1 / 0, split=lambda c: [["pnpm", "test"]])
        self.assertEqual(code, 0)
        self.assertTrue(buf.getvalue().startswith("pass\t"))


class RouteLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "memmon"
        self.state.mkdir()
        (self.state / route.SCRIPT).write_text("#!/bin/sh\n")
        self.settings = Path(self.tmp.name) / "settings.json"
        self.settings.write_text(json.dumps({"hooks": {"PreToolUse": []}, "env": {"KEEP": "1"}}))
        self.ours = str(self.state / route.SCRIPT)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self):
        return json.loads(self.settings.read_text())

    def test_route_on_refuses_until_g1_is_verified(self):
        before = self.settings.read_text()
        code, msg = route.route_on(self.state, self.settings)
        self.assertEqual(code, 1)
        self.assertIn("G1", msg)
        self.assertIn("memmon run", msg)
        self.assertEqual(self.settings.read_text(), before)
        self.assertEqual(route.status(self.state, self.settings)["line"], route.LINE_OFF)

    def test_foreign_prefix_refused(self):
        """B19 (a)."""
        cfg = self.cfg()
        cfg["env"][route.KEY] = "/opt/other-prefix.sh"
        self.settings.write_text(json.dumps(cfg))
        code, msg = route.route_on(self.state, self.settings, verified=True)
        self.assertEqual(code, 1)
        self.assertIn("another prefix", msg)
        self.assertEqual(self.cfg()["env"][route.KEY], "/opt/other-prefix.sh")
        route.route_off(self.state, self.settings)
        self.assertEqual(self.cfg()["env"][route.KEY], "/opt/other-prefix.sh")

    def test_on_then_off_after_unrelated_edit(self):
        """B19 (b): route off deletes only its own key."""
        code, _ = route.route_on(self.state, self.settings, verified=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.cfg()["env"][route.KEY], self.ours)
        self.assertEqual(route.status(self.state, self.settings)["line"], route.LINE_ON)
        self.assertTrue(list(Path(self.tmp.name).glob("settings.json.bak.*")))
        cfg = self.cfg()
        cfg["model"] = "edited-by-user"
        cfg["env"]["NEW"] = "2"
        self.settings.write_text(json.dumps(cfg))
        route.route_off(self.state, self.settings)
        after = self.cfg()
        self.assertNotIn(route.KEY, after["env"])
        self.assertEqual((after["model"], after["env"]["NEW"], after["env"]["KEEP"]),
                         ("edited-by-user", "2", "1"))
        self.assertTrue((self.state / "runner/coord/route.off").exists())
        self.assertEqual(route.status(self.state, self.settings)["state"], "off")
        # On again clears the flag.
        route.route_on(self.state, self.settings, verified=True)
        self.assertFalse((self.state / "runner/coord/route.off").exists())

    def test_off_flag_alone_turns_routing_off(self):
        route.route_on(self.state, self.settings, verified=True)
        (self.state / "runner/coord/route.off").touch()
        self.assertEqual(route.status(self.state, self.settings)["state"], "off")

    def test_stub_passes_through(self):
        """B19 (d): the stub uninstall leaves behind."""
        stub = Path(self.tmp.name) / "stub.sh"
        stub.write_text(route.STUB)
        out = subprocess.run(["/bin/sh", str(stub), "echo stub-ok; exit 3"],
                             capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout), (3, "stub-ok\n"))


if __name__ == "__main__":
    unittest.main()
