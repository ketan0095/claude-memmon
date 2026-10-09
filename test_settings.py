#!/usr/bin/env python3
"""memmon settings (D43): the allowlisted keys, their sources and exit codes,
the MEMMON_GATE override, and the gate honouring config.json's gate_mode.
Everything runs in a temp state dir; ~/.claude/settings.json is never read
or written."""

from __future__ import annotations

import contextlib
import io
import json
import os
import time
import unittest
from unittest import mock

import memmon
import memmon_runner
from testkit import TempState


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.close)
        p = mock.patch.dict(memmon.CONFIG, {"gate_mode": None, "notifications": True,
                                            "pressure_suggestions": True})
        p.start()
        self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MEMMON_GATE", None)
        self.config = os.path.join(self.state.root, "config.json")
        self.claude_settings = os.path.join(self.state.root, "claude-settings.json")
        q = mock.patch.object(memmon, "CLAUDE_SETTINGS", self.claude_settings)
        q.start()
        self.addCleanup(q.stop)

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = memmon.settings_cli(list(argv))
        return rc, json.loads(out.getvalue())

    def test_defaults(self):
        rc, out = self.cli("--json")
        self.assertEqual(rc, 0)
        self.assertEqual(out["schema_version"], 1)
        self.assertEqual(out["gate_mode"], {"value": "block-critical", "source": "default",
                                            "choices": ["block-critical", "block", "warn", "off"]})
        self.assertIsNone(out["paused_until"])
        self.assertEqual(out["runner_mode"], memmon_runner.DEFAULT_MODE)
        self.assertIs(out["auto_cancel_interruptible"], False)
        self.assertIs(out["pressure_suggestions"], True)
        self.assertIs(out["notifications"], True)
        self.assertEqual(out["state_dir"], os.path.abspath(self.state.root))
        self.assertNotIn("warning", out)

    def test_each_key_sets_and_reads_back(self):
        cases = [("gate_mode", "warn", lambda o: o["gate_mode"]["value"], "warn"),
                 ("runner_mode", "observe", lambda o: o["runner_mode"], "observe"),
                 ("auto_cancel_interruptible", "true",
                  lambda o: o["auto_cancel_interruptible"], True),
                 ("pressure_suggestions", "false", lambda o: o["pressure_suggestions"], False),
                 ("notifications", "false", lambda o: o["notifications"], False)]
        for key, raw, get, want in cases:
            with self.subTest(key=key):
                rc, out = self.cli("set", key, raw)
                self.assertEqual(rc, 0)
                self.assertEqual(get(out), want)
                self.assertEqual(get(self.cli("--json")[1]), want)
        self.assertEqual(memmon.gate_mode(), ("warn", "config"))
        self.assertEqual(memmon_runner.read_mode(self.state.root)[0], "observe")
        self.assertIs(memmon_runner.read_mode(self.state.root)[2], True)
        with open(self.config) as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg, {"gate_mode": "warn", "pressure_suggestions": False,
                               "notifications": False})

    def test_bad_key_or_value_exits_2(self):
        for argv in (("set", "headroom_frac", "0.5"), ("set", "gate_mode", "loud"),
                     ("set", "project_roots", "true"), ("set", "gate", "warn"),
                     ("set", "runner_mode", "fast"), ("set", "notifications", "yes"),
                     ("set", "auto_cancel_interruptible", "1"), ("set", "gate_mode")):
            with self.subTest(argv=argv):
                rc, out = self.cli(*argv)
                self.assertEqual(rc, 2)
                self.assertIn("error", out)
                self.assertIn("key", out)
        self.assertFalse(os.path.exists(self.config))
        self.assertFalse(os.path.exists(os.path.join(self.state.root, "runner", "coord",
                                                     "runner.json")))

    def test_other_config_keys_are_preserved(self):
        with open(self.config, "w") as fh:
            json.dump({"project_roots": ["~/code"], "headroom_frac": 0.25}, fh)
        self.assertEqual(self.cli("set", "gate_mode", "block")[0], 0)
        self.assertEqual(self.cli("set", "notifications", "false")[0], 0)
        with open(self.config) as fh:
            self.assertEqual(json.load(fh), {"project_roots": ["~/code"], "headroom_frac": 0.25,
                                             "gate_mode": "block", "notifications": False})
        self.assertEqual([f for f in os.listdir(self.state.root) if f.endswith(".tmp")], [])

    def test_unparseable_config_is_not_overwritten(self):
        with open(self.config, "w") as fh:
            fh.write("{not json")
        rc, out = self.cli("set", "gate_mode", "warn")
        self.assertEqual(rc, 2)
        with open(self.config) as fh:
            self.assertEqual(fh.read(), "{not json")

    def test_env_overrides_config_with_a_warning(self):
        self.cli("set", "gate_mode", "warn")
        os.environ["MEMMON_GATE"] = "off"
        rc, out = self.cli("--json")
        self.assertEqual(out["gate_mode"]["value"], "off")
        self.assertEqual(out["gate_mode"]["source"], "env")
        self.assertEqual(out["warning"], memmon.ENV_WARNING)
        rc, out = self.cli("set", "gate_mode", "block")
        self.assertEqual((rc, out["gate_mode"]["source"]), (0, "env"))
        self.assertEqual(out["warning"], memmon.ENV_WARNING)

    def gate_row(self, **row):
        with open(memmon.GATE_LOG, "a") as fh:
            fh.write(json.dumps({"ts": time.time(), "action": "allow", **row}) + "\n")

    def test_hook_env_seen_only_in_the_gate_log(self):
        # MEMMON_GATE is in Claude's hook environment, not this process's.
        self.cli("set", "gate_mode", "block")
        self.gate_row(mode="warn", mode_source="env")
        out = self.cli("--json")[1]
        self.assertEqual(out["gate_mode"]["value"], "warn")
        self.assertEqual(out["gate_mode"]["source"], "env")
        self.assertEqual(out["warning"], memmon.ENV_WARNING)

    def test_hook_env_from_claude_settings_is_read_only(self):
        with open(self.claude_settings, "w") as fh:
            json.dump({"env": {"MEMMON_GATE": "off"}, "hooks": {}}, fh)
        before = os.stat(self.claude_settings).st_mtime_ns
        out = self.cli("--json")[1]
        self.assertEqual((out["gate_mode"]["value"], out["gate_mode"]["source"]), ("off", "env"))
        self.assertEqual(out["warning"], memmon.ENV_WARNING)
        self.cli("set", "gate_mode", "warn")
        self.assertEqual(os.stat(self.claude_settings).st_mtime_ns, before)

    def test_no_override_anywhere(self):
        self.cli("set", "gate_mode", "warn")
        # A gate row from before the change (config-sourced) is just stale.
        self.gate_row(mode="block-critical", mode_source="default")
        out = self.cli("--json")[1]
        self.assertEqual((out["gate_mode"]["value"], out["gate_mode"]["source"]),
                         ("warn", "config"))
        self.assertNotIn("warning", out)
        self.gate_row(mode="warn", mode_source="config")
        self.assertNotIn("warning", self.cli("--json")[1])

    def test_legacy_gate_row_without_source(self):
        # Before rows recorded their source: a mode that no config explains.
        self.gate_row(mode="off")
        out = self.cli("--json")[1]
        self.assertEqual((out["gate_mode"]["value"], out["gate_mode"]["source"]), ("off", "env"))
        self.cli("set", "gate_mode", "warn")
        self.assertEqual(self.cli("--json")[1]["gate_mode"]["source"], "config")

    def test_concurrent_sets_keep_every_key(self):
        import threading
        real = json.load

        def slow(fh, *a, **kw):
            value = real(fh, *a, **kw)
            time.sleep(0.05)                      # widen the read-modify-write
            return value
        with open(self.config, "w") as fh:
            json.dump({"project_roots": []}, fh)
        errors = []

        def run(key, raw):
            try:
                memmon.settings_set(key, raw)
            except Exception as exc:              # pragma: no cover
                errors.append(exc)
        with mock.patch.object(json, "load", slow):
            threads = [threading.Thread(target=run, args=kv) for kv in
                       (("gate_mode", "warn"), ("notifications", "false"),
                        ("pressure_suggestions", "false"))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        with open(self.config) as fh:
            self.assertEqual(json.load(fh), {"project_roots": [], "gate_mode": "warn",
                                             "notifications": False,
                                             "pressure_suggestions": False})

    def test_failed_write_leaves_config_byte_identical(self):
        original = b'{"project_roots": ["~/code"],  "gate_mode": "block"}\n'
        with open(self.config, "wb") as fh:
            fh.write(original)
        with mock.patch.object(os, "replace", side_effect=OSError(28, "No space left")):
            rc, out = self.cli("set", "gate_mode", "warn")
        self.assertEqual((rc, out["key"]), (2, "gate_mode"))
        self.assertIn("could not write", out["error"])
        with open(self.config, "rb") as fh:
            self.assertEqual(fh.read(), original)
        self.assertEqual([f for f in os.listdir(self.state.root) if f.endswith(".tmp")], [])

    def test_runner_busy_exits_2_with_json(self):
        import fcntl
        import testkit
        paths = memmon_runner.Paths(self.state.root)
        paths.make()
        fd = os.open(paths.ledger, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)                     # admission holds the ledger
        clock = testkit.FakeTelemetryClock()               # its sleep moves time on
        with mock.patch.object(memmon_runner.telemetry, "SYSTEM_CLOCK", clock):
            for key, raw in (("runner_mode", "observe"), ("auto_cancel_interruptible", "true")):
                with self.subTest(key=key):
                    rc, out = self.cli("set", key, raw)
                    self.assertEqual(rc, 2)
                    self.assertEqual(out, {"error": "runner busy, try again", "key": key})

    def test_unwritable_state_dir_exits_2_with_json(self):
        os.chmod(self.state.root, 0o500)
        self.addCleanup(os.chmod, self.state.root, 0o700)
        rc, out = self.cli("set", "gate_mode", "warn")
        self.assertEqual(rc, 2)
        self.assertEqual(out["key"], "gate_mode")
        self.assertIn("could not write", out["error"])

    def test_paused_until(self):
        until = time.time() + 3600
        with open(memmon.PAUSE, "w") as fh:
            json.dump({"until": until}, fh)
        self.assertAlmostEqual(self.cli("--json")[1]["paused_until"], until, places=3)
        with open(memmon.PAUSE, "w") as fh:
            json.dump({"until": "forever"}, fh)
        self.assertEqual(self.cli("--json")[1]["paused_until"], "forever")

    def test_never_touches_claude_settings(self):
        claude = os.path.join(memmon.HOME, ".claude", "settings.json")
        before = os.stat(claude).st_mtime_ns if os.path.exists(claude) else None
        for key, raw in (("gate_mode", "warn"), ("notifications", "false"),
                         ("runner_mode", "paused")):
            self.cli("set", key, raw)
        after = os.stat(claude).st_mtime_ns if os.path.exists(claude) else None
        self.assertEqual(before, after)

    def test_main_dispatches_before_the_flag_parser(self):
        out = io.StringIO()
        with mock.patch("sys.argv", ["memmon", "settings", "set", "gate_mode", "warn"]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(memmon.main(), 0)
        self.assertEqual(json.loads(out.getvalue())["gate_mode"]["value"], "warn")


class GateHonoursConfigTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.close)
        p = mock.patch.dict(memmon.CONFIG, {"gate_mode": None})
        p.start()
        self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MEMMON_GATE", None)
        q = mock.patch.object(memmon, "CLAUDE_SETTINGS",
                              os.path.join(self.state.root, "claude-settings.json"))
        q.start()
        self.addCleanup(q.stop)
        crit = {"level": "CRITICAL", "color": "red", "score": 9, "reasons": ["paging"],
                "rates": "ok", "level_reason": None, "headroom_min": None}
        for name, value in (("read_vm", lambda *a, **k: {}),
                            ("pressure", lambda vm: dict(crit))):
            q = mock.patch.object(memmon, name, value)
            q.start()
            self.addCleanup(q.stop)

    def gate(self, cmd="pnpm typecheck"):
        payload = json.dumps({"tool_name": "Bash", "session_id": "s",
                              "tool_input": {"command": cmd}})
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(payload)), \
                mock.patch("sys.stderr", err), contextlib.redirect_stdout(out):
            return memmon.gate(), out.getvalue()

    def test_gate_reads_config_gate_mode(self):
        self.assertEqual(self.gate()[0], 2)                 # default block-critical
        memmon.settings_set("gate_mode", "warn")
        rc, out = self.gate()
        self.assertEqual(rc, 0)
        self.assertIn("additionalContext", out)
        memmon.settings_set("gate_mode", "off")
        self.assertEqual(self.gate(), (0, ""))

    def test_env_still_wins_over_config(self):
        memmon.settings_set("gate_mode", "off")
        os.environ["MEMMON_GATE"] = "block-critical"
        self.assertEqual(self.gate()[0], 2)

    def test_gate_logs_where_its_mode_came_from(self):
        memmon.settings_set("gate_mode", "warn")
        self.gate()
        os.environ["MEMMON_GATE"] = "block"
        self.gate()
        with open(memmon.GATE_LOG) as fh:
            rows = [json.loads(line) for line in fh]
        self.assertEqual([(r["mode"], r["mode_source"]) for r in rows],
                         [("warn", "config"), ("block", "env")])

    def test_light_command_never_reads_the_mode(self):
        with mock.patch.object(memmon, "gate_mode",
                               side_effect=AssertionError("mode read on a light command")):
            self.assertEqual(self.gate("git status"), (0, ""))


class NotificationsSettingTests(unittest.TestCase):
    def test_notifications_off_posts_nothing(self):
        calls = []
        with mock.patch.dict(memmon.CONFIG, {"notifications": False}):
            memmon.notify("text", run=lambda argv, **kw: calls.append(argv))
        self.assertEqual(calls, [])
        with mock.patch.dict(memmon.CONFIG, {"notifications": True}):
            memmon.notify("text", run=lambda argv, **kw: calls.append(argv))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
