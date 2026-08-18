#!/usr/bin/env python3
"""Dependency-free regression tests for memmon's command gate.

Run with:  python3 test_memmon.py
"""

import os
import contextlib
import io
import json
import tempfile
import unittest
from unittest import mock

import memmon


class CommandClassifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.originals = {name: getattr(memmon, name) for name in (
            "STATE_DIR", "PROFILE", "SHELL_STATE", "PAUSE", "GATE_LOG", "PENDING",
        )}
        root = self.tmp.name
        memmon.STATE_DIR = root
        memmon.PROFILE = os.path.join(root, "profile.json")
        memmon.SHELL_STATE = os.path.join(root, "learned.zsh")
        memmon.PAUSE = os.path.join(root, "paused.json")
        memmon.GATE_LOG = os.path.join(root, "gate.jsonl")
        memmon.PENDING = os.path.join(root, "blocked.json")

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(memmon, name, value)
        self.tmp.cleanup()

    def classification(self, command, profile=None):
        return memmon.classify_command(command, {} if profile is None else profile)

    def test_allow_side_truth_table(self):
        # False positives are the dangerous regression for a guard: every one of
        # these must bypass memory evaluation entirely.
        commands = [
            "cat vitest.config.ts",
            'echo "=== who is running vitest ==="',
            "sed 's/x/y/' build.log",
            'grep -E "error|test" build.log',
            'sed "s/x/y/" build.log | grep -E "error|test"',
            "git status",
            "linear.py issue-get ABC-4573 --json",
            "printf '{\"jsonrpc\":\"2.0\",\"method\":\"initialize\"}'",
            "cat /tmp/projects/vitest/results.txt",
        ]
        for command in commands:
            with self.subTest(command=command):
                result = self.classification(command)
                self.assertFalse(result["matched"])
                self.assertEqual(result["source"], "none")
                self.assertFalse(result["block_eligible"])

    def test_builtin_match_truth_table(self):
        expected = {
            "pnpm --filter dashboard typecheck": "pnpm … typecheck",
            "npx vitest run": "vitest",
            "docker compose build": "docker compose build",
            "cargo test": "cargo test",
        }
        for command, rule in expected.items():
            with self.subTest(command=command):
                result = self.classification(command)
                self.assertTrue(result["matched"])
                self.assertEqual(result["source"], "builtin")
                self.assertEqual(result["rule"], rule)
                self.assertTrue(result["block_eligible"])

    def test_quoted_operator_is_not_a_segment(self):
        commands = memmon.shell_commands(
            'grep -E "error TS|Found [0-9]+ errors" output.log | sed "s/x/y/"'
        )
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][0], "grep")
        self.assertIn("error TS|Found [0-9]+ errors", commands[0])
        self.assertEqual(commands[1][0], "sed")
        self.assertEqual(memmon.normalise_cmd(
            'pnpm test 2>&1 | grep -E "FAIL|AssertionError|Tests"'
        ), ["pnpm test"])

    def test_quoted_heredoc_contents_are_not_commands(self):
        command = """cat <<'PROMPT'
Tell the agent to run pnpm test and npx vitest run.
PROMPT
printf 'saved prompt'
"""
        self.assertFalse(self.classification(command)["matched"])
        self.assertEqual(memmon.normalise_cmd(command), [])

    def test_learned_rule_is_warning_only(self):
        profile = {"codex.sh run": {
            "n": 13, "peak": 3 * 1024**3, "last": 1,
        }}
        result = self.classification("/opt/tools/codex.sh run ABC-4573", profile)
        self.assertTrue(result["matched"])
        self.assertEqual(result["source"], "learned")
        self.assertEqual(result["rule"], "codex.sh run")
        self.assertEqual(result["samples"], 13)
        self.assertFalse(result["block_eligible"])

        critical = {"level": "CRITICAL", "reasons": ["heavy thrashing"]}
        for mode in ("block", "block-critical"):
            with self.subTest(mode=mode):
                action, _ = memmon.gate_decision(
                    "Bash", "codex.sh run", critical, {}, mode, result
                )
                self.assertEqual(action, "warn")

    def test_builtin_can_block_but_only_at_policy_threshold(self):
        match = self.classification("cargo test")
        watch = {"level": "WATCH", "reasons": ["load 24"]}
        critical = {"level": "CRITICAL", "reasons": ["heavy thrashing"]}
        self.assertEqual(memmon.gate_decision(
            "Bash", "cargo test", watch, {}, "block-critical", match
        )[0], "warn")
        self.assertEqual(memmon.gate_decision(
            "Bash", "cargo test", critical, {}, "block-critical", match
        )[0], "block")

    def test_malformed_shell_input_fails_open(self):
        result = self.classification("echo 'unterminated | vitest run")
        self.assertFalse(result["matched"])
        self.assertEqual(memmon.gate_decision(
            "Bash", "echo 'unterminated | vitest run",
            {"level": "CRITICAL"}, {}, "block", result
        )[0], "allow")

    def test_learned_shell_prefilter_uses_full_shape(self):
        memmon._write_learned_glob({
            "codex.sh run": {"n": 2, "peak": 2 * 1024**3, "last": 1},
        })
        with open(memmon.SHELL_STATE) as fh:
            state = fh.read()
        self.assertIn("*codex.sh*run*", state)
        self.assertNotIn("MEMMON_LEARNED='*codex.sh*'", state)

    def test_v1_profile_is_quarantined_and_restarted_clean(self):
        with open(memmon.PROFILE, "w") as fh:
            json.dump({"git status": {"n": 8, "peak": 3 * 1024**3, "last": 1}}, fh)
        self.assertEqual(memmon.load_profile(), {})
        quarantined = [name for name in os.listdir(self.tmp.name)
                       if ".quarantined-v1-" in name]
        self.assertEqual(len(quarantined), 1)
        with open(memmon.PROFILE) as fh:
            replacement = json.load(fh)
        self.assertEqual(replacement, {"version": 2, "commands": {}})
        with open(memmon.SHELL_STATE) as fh:
            shell_state = fh.read()
        self.assertIn("MEMMON_LEARNED='__never_matches__'", shell_state)

    def test_gate_records_decision_time_classification_and_full_command(self):
        command = "cargo test " + "very-long-argument-" * 20
        payload = json.dumps({
            "tool_name": "Bash", "session_id": "abcdef12-0000-0000-0000-000000000000",
            "cwd": "/repo", "tool_input": {"command": command},
        })
        fake_pressure = {"level": "DANGER", "score": 5,
                         "reasons": ["paging 80 MB/s"]}
        with mock.patch.object(memmon, "read_vm", return_value={}), \
             mock.patch.object(memmon, "pressure", return_value=fake_pressure), \
             mock.patch.object(memmon, "lookup_session_name", return_value="cargo check"), \
             mock.patch.object(memmon.sys, "stdin", io.StringIO(payload)), \
             mock.patch.dict(os.environ, {"MEMMON_GATE": "warn"}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(memmon.gate(), 0)
        with open(memmon.GATE_LOG) as fh:
            row = json.loads(fh.readline())
        self.assertEqual(row["cmd"], command)
        self.assertGreater(len(row["cmd"]), 200)
        self.assertEqual(row["session_name"], "cargo check")
        self.assertEqual(row["classification"]["source"], "builtin")
        self.assertEqual(row["classification"]["rule"], "cargo test")
        self.assertTrue(row["classification"]["block_eligible"])
        self.assertEqual(row["score"], 5)

    def test_legacy_history_is_not_reclassified(self):
        with open(memmon.GATE_LOG, "w") as fh:
            fh.write(json.dumps({
                "ts": 100.0, "cmd": "codex.sh run", "mode": "block-critical",
                "level": "CRITICAL", "action": "block", "session": "deadbeef",
                "reasons": ["heavy thrashing"], "ms": 70,
            }) + "\n")
        stats = memmon.gate_stats()
        event = stats["history"]["events"][0]
        self.assertTrue(event["legacy"])
        self.assertIsNone(event["classification"])
        self.assertIsNone(event["session"]["name"])


class PendingPruneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = memmon.PENDING
        memmon.PENDING = os.path.join(self.tmp.name, "blocked.json")
        memmon.save_pending([
            {"ts": 1, "session_id": "aaaaaaaa-1111-2222-3333-444444444444",
             "session": "live one", "cmd": "pnpm build", "cwd": "", "level": "CRITICAL"},
            {"ts": 2, "session_id": "bbbbbbbb-5555-6666-7777-888888888888",
             "session": "gone", "cmd": "pnpm test", "cwd": "", "level": "CRITICAL"},
        ])

    def tearDown(self):
        memmon.PENDING = self.original
        self.tmp.cleanup()

    def test_entries_whose_session_exited_are_dropped(self):
        memmon._prune_pending([{"session_id": "aaaaaaaa-1111-2222-3333-444444444444",
                                "short": "aaaaaaaa"}])
        kept = memmon.load_pending()
        self.assertEqual([i["cmd"] for i in kept], ["pnpm build"])

    def test_short_id_alone_still_matches(self):
        memmon._prune_pending([{"short": "bbbbbbbb"}])
        self.assertEqual([i["cmd"] for i in memmon.load_pending()], ["pnpm test"])

    def test_no_visible_sessions_never_empties_the_queue(self):
        # A failed collection must not be read as "every session exited".
        memmon._prune_pending([])
        self.assertEqual(len(memmon.load_pending()), 2)
        memmon._prune_pending([{"name": "no id recorded"}])
        self.assertEqual(len(memmon.load_pending()), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
