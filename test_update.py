"""memmon update: a temp bare origin, a temp clone and a temp HOME only.

The clone's install.sh is a stub that records its argv, session and HOME
and exits. Nothing here runs the real installer, touches the real ~/.claude
or LaunchAgents, or contacts a real remote."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import memmon_update as up

HERE = os.path.dirname(os.path.abspath(__file__))
STUB_INSTALL = """#!/bin/bash
/usr/bin/python3 - "$@" <<'PY'
import json, os, sys
mark = os.environ["STUB_MARK"]
json.dump({"argv": sys.argv[1:], "sid": os.getsid(0), "home": os.environ.get("HOME")},
          open(mark + ".tmp", "w"))
os.replace(mark + ".tmp", mark)
PY
echo "stub installer ran"
exit ${STUB_EXIT:-0}
"""


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.home = t / "home"
        self.state = self.home / ".claude" / "memmon"
        self.state.mkdir(parents=True)
        self.mark = t / "installer-ran.json"
        env = {"HOME": str(self.home), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "acme", "GIT_AUTHOR_EMAIL": "acme",
               "GIT_COMMITTER_NAME": "acme", "GIT_COMMITTER_EMAIL": "acme",
               "STUB_MARK": str(self.mark)}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.origin = t / "origin.git"
        self.clone = t / "acme-memmon"
        self.git(t, "init", "--quiet", "--bare", "-b", "main", str(self.origin))
        self.git(t, "clone", "--quiet", str(self.origin), str(self.clone))
        self.git(self.clone, "checkout", "--quiet", "-b", "main")
        (self.clone / "install.sh").write_text(STUB_INSTALL)
        (self.clone / "install.sh").chmod(0o755)
        self.commit("first: install script")
        self.git(self.clone, "push", "--quiet", "-u", "origin", "main")
        self.record()
        # Another clone pushes new work to origin.
        self.other = t / "other"
        self.git(t, "clone", "--quiet", str(self.origin), str(self.other))

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, subject, repo=None, name="notes.txt"):
        repo = repo or self.clone
        p = Path(repo) / name
        p.write_text((p.read_text() if p.exists() else "") + subject + "\n")
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "--quiet", "-m", subject)

    def record(self, flags=("--sampler", "--menubar")):
        rec = {"version": 1, "source": str(self.clone), "flags": list(flags),
               "commit": self.git(self.clone, "rev-parse", "HEAD"), "branch": "main",
               "installed_at": 1}
        (self.state / "install.json").write_text(json.dumps(rec))

    def upstream(self, *subjects):
        for s in subjects:
            self.commit(s, self.other)
        self.git(self.other, "push", "--quiet", "origin", "HEAD:main")

    # ------------------------------------------------------------- check

    def test_up_to_date(self):
        r = up.check(str(self.state))
        self.assertEqual((r["state"], r["behind"], r["commits"], r["reason"]),
                         ("up_to_date", 0, [], None))
        self.assertRegex(r["installed"], r"^[0-9a-f]{7}$")

    def test_available_lists_subjects(self):
        self.upstream("Checkout refactor: faster scans", "fix(menubar): tidy the card")
        r = up.check(str(self.state))
        self.assertEqual((r["state"], r["behind"]), ("available", 2))
        self.assertEqual([c["subject"] for c in r["commits"]],
                         ["fix(menubar): tidy the card", "Checkout refactor: faster scans"])
        self.assertNotEqual(r["installed"], r["latest"])

    def test_no_install_json(self):
        (self.state / "install.json").unlink()
        r = up.check(str(self.state))
        self.assertEqual((r["state"], r["reason"]), ("unavailable", up.NO_INSTALL))

    def test_source_missing_not_git_detached_or_other_branch(self):
        self.git(self.clone, "checkout", "--quiet", "-b", "side")
        self.assertIn("on side, not main", up.check(str(self.state))["reason"])
        self.git(self.clone, "checkout", "--quiet", "--detach")
        self.assertIn("detached", up.check(str(self.state))["reason"])
        rec = json.loads((self.state / "install.json").read_text())
        rec["source"] = str(Path(self.tmp.name) / "gone")
        (self.state / "install.json").write_text(json.dumps(rec))
        self.assertIn("gone", up.check(str(self.state))["reason"])

    def test_fetch_timeout_is_unavailable(self):
        hang = Path(self.tmp.name) / "hang-upload-pack"
        hang.write_text("#!/bin/sh\nsleep 10\n")
        hang.chmod(0o755)
        self.git(self.clone, "config", "remote.origin.uploadpack", str(hang))
        t0 = time.monotonic()
        r = up.check(str(self.state), timeout=1.0)
        self.assertEqual(r["state"], "unavailable")
        self.assertIn("offline", r["reason"])
        self.assertLess(time.monotonic() - t0, 5)

    # ------------------------------------------------------------- apply

    def wait_mark(self, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.mark.exists():
                return json.loads(self.mark.read_text())
            time.sleep(0.05)
        self.fail("the installer stub never ran")

    def test_apply_fast_forwards_and_starts_installer_detached_with_flags(self):
        self.upstream("feat: new thing")
        before = self.git(self.clone, "rev-parse", "HEAD")
        r = up.apply(str(self.state))
        self.assertEqual(r["state"], "started")
        self.assertEqual(r["from"], before[:7])
        latest = self.git(self.origin, "rev-parse", "main")
        self.assertEqual(self.git(self.clone, "rev-parse", "HEAD"), latest)
        self.assertEqual(r["to"], latest[:7])
        ran = self.wait_mark()
        self.assertEqual(ran["argv"], ["--sampler", "--menubar"])
        self.assertNotEqual(ran["sid"], os.getsid(0), "the installer runs in its own session")
        self.assertEqual(ran["home"], str(self.home))
        deadline = time.monotonic() + 10
        while up.status(str(self.state))["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        st = up.status(str(self.state))
        self.assertEqual((st["state"], st["to"]), ("finished", latest[:7]))
        self.assertIn("stub installer ran", Path(r["log"]).read_text())

    def test_recorded_flags_only_and_in_order(self):
        self.record(flags=("--gate", "--sampler", "; rm -rf ~", "--uninstall"))
        self.upstream("feat: new thing")
        up.apply(str(self.state))
        self.assertEqual(self.wait_mark()["argv"], ["--sampler", "--gate"])
        self.wait_done()

    def wait_done(self, timeout=10):
        deadline = time.monotonic() + timeout
        while up.status(str(self.state))["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        return up.status(str(self.state))

    def test_failed_install_reports_tail(self):
        os.environ["STUB_EXIT"] = "3"
        self.upstream("feat: new thing")
        up.apply(str(self.state))
        self.wait_mark()
        deadline = time.monotonic() + 10
        while up.status(str(self.state))["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        st = up.status(str(self.state))
        self.assertEqual((st["state"], st["exit"]), ("failed", "3"))
        self.assertIn("stub installer ran", st["tail"])

    def test_dirty_clone_refused(self):
        self.upstream("feat: new thing")
        (self.clone / "notes.txt").write_text("local edit\n")
        with self.assertRaises(up.Refused) as cm:
            up.apply(str(self.state))
        self.assertIn("uncommitted", str(cm.exception))
        self.assertFalse(self.mark.exists())

    def test_non_fast_forward_refused(self):
        self.upstream("feat: upstream")
        self.commit("local: diverging commit")
        head = self.git(self.clone, "rev-parse", "HEAD")
        with self.assertRaises(up.Refused) as cm:
            up.apply(str(self.state))
        # memmon's own check refuses before git is asked to merge anything.
        self.assertIn("commits that are not on origin/main", str(cm.exception))
        self.assertEqual(self.git(self.clone, "rev-parse", "HEAD"), head)
        self.assertFalse(self.mark.exists())

    def test_cli_json_and_exit_codes(self):
        import contextlib, io
        self.upstream("feat: new thing")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(up.cli(["--check", "--json"], str(self.state)), 0)
        self.assertEqual(json.loads(buf.getvalue())["state"], "available")
        (self.clone / "notes.txt").write_text("local edit\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(up.cli(["--apply", "--json"], str(self.state)), 2)
        self.assertEqual(json.loads(buf.getvalue())["state"], "refused")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(up.cli(["--status", "--json"], str(self.state)), 0)
        self.assertEqual(json.loads(buf.getvalue())["state"], "none")


class SettingsInstallTests(unittest.TestCase):
    def setUp(self):
        import testkit
        self.state = testkit.TempState()
        self.addCleanup(self.state.close)

    def settings(self):
        import contextlib, io, memmon
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(memmon.settings_cli(["--json"]), 0)
        return json.loads(buf.getvalue())

    def test_settings_json_carries_install_without_the_path(self):
        self.assertEqual(self.settings()["install"],
                         {"commit": None, "branch": None, "installed_at": None,
                          "source_known": False})
        rec = {"version": 1, "source": "/Volumes/acme/acme-memmon", "flags": ["--sampler"],
               "commit": "9782572aa1b2c3d4e5f60718293a4b5c6d7e8f90", "branch": "main",
               "installed_at": 1791500000}
        with open(os.path.join(self.state.root, "install.json"), "w") as fh:
            json.dump(rec, fh)
        out = self.settings()
        self.assertEqual(out["install"], {"commit": "9782572", "branch": "main",
                                          "installed_at": 1791500000, "source_known": True})
        self.assertNotIn("acme-memmon", json.dumps(out))

    def test_odd_install_json_values_become_null(self):
        with open(os.path.join(self.state.root, "install.json"), "w") as fh:
            json.dump({"source": "/Volumes/acme/x", "commit": 7, "branch": ["main"],
                       "installed_at": "yesterday"}, fh)
        self.assertEqual(up.install_summary(self.state.root),
                         {"commit": None, "branch": None, "installed_at": None,
                          "source_known": True})


class InstallScriptTests(unittest.TestCase):
    def test_install_json_written_atomically_and_removed_on_uninstall(self):
        with open(os.path.join(HERE, "install.sh")) as fh:
            script = fh.read()
        block = script[script.index('"$DEST_DIR/install.json"'):]
        self.assertIn('tmp = f"{path}.{os.getpid()}.tmp"', block)
        self.assertIn("os.replace(tmp, path)", block)
        for key in ('"source"', '"flags"', '"commit"', '"branch"', '"installed_at"'):
            self.assertIn(key, block)
        rm = script[script.index("rm -rf \"$PLIST\""):script.index("memmon removed.")]
        self.assertIn('"$DEST_DIR/install.json"', rm)
        self.assertIn('"$DEST_DIR/memmon_update.py"', rm)
        self.assertRegex(script, r"for mod in [^;]*\bmemmon_update\b")

    def test_flags_recorded_as_requested(self):
        """The REQ_FLAGS block, run alone under bash: what install.json gets."""
        with open(os.path.join(HERE, "install.sh")) as fh:
            script = fh.read()
        parse = script[script.index("WANT_SAMPLER=0"):script.index("# Boot out any agent")]
        for argv, want in (([], ""), (["--gate", "--sampler"], "--sampler --gate"),
                           (["--menubar"], "--menubar")):
            out = subprocess.run(["/bin/bash", "-c", "set -euo pipefail\n" + parse
                                  + '\necho ${REQ_FLAGS[@]+"${REQ_FLAGS[@]}"}', "x", *argv],
                                 capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), want, out.stderr)


if __name__ == "__main__":
    unittest.main()
