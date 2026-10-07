"""Process-level regression tests; isolated state, no real builds or settings."""

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import memmon_runner as runner


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.procs = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
            proc.communicate(timeout=8)
        self.tmp.cleanup()

    def launch(self, code="pass", args=(), **kwargs):
        payload = dict(command=[sys.executable, "-c", code, *args],
                       state_dir=str(self.root), poll_interval=0.02, **kwargs)
        proc = subprocess.Popen([sys.executable, __file__, "--worker", json.dumps(payload)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(proc)
        return proc

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail("condition not reached within deadline")

    def finish(self, proc, expected=0):
        out, err = proc.communicate(timeout=8)
        self.assertEqual(proc.returncode, expected, err)
        return out, err

    def test_same_resource_never_overlaps(self):
        events = self.root / "events"
        code = ("import os,sys,time,json; "
                "f=open(sys.argv[1],'a',buffering=1); "
                "f.write(json.dumps(['start',os.getpid(),time.monotonic()])+'\\n'); "
                "time.sleep(.2); "
                "f.write(json.dumps(['end',os.getpid(),time.monotonic()])+'\\n')")
        procs = [self.launch(code, [str(events)]) for _ in range(4)]
        for proc in procs:
            self.finish(proc)
        rows = [json.loads(line) for line in events.read_text().splitlines()]
        self.assertEqual([r[0] for r in rows], ["start", "end"] * 4)
        for start, end in zip(rows[::2], rows[1::2]):
            self.assertEqual(start[1], end[1])
        self.assertEqual(runner.jobs(self.root), [])

    def test_different_resources_can_overlap(self):
        procs = [self.launch("import time; time.sleep(.5)", resource=r) for r in ("heavy", "call-rig")]
        self.wait_for(lambda: len([j for j in runner.jobs(self.root) if j["state"] == "running"]) == 2)
        for proc in procs:
            self.finish(proc)

    def test_timeout_does_not_start_command(self):
        holder = self.launch("import time; time.sleep(.5)", label="owner")
        self.wait_for(lambda: runner.jobs(self.root))
        marker = self.root / "must-not-exist"
        waiter = self.launch("import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", [str(marker)], timeout=.1)
        _, err = self.finish(waiter, 124)
        self.assertIn("owner", err)
        self.assertFalse(marker.exists())
        self.finish(holder)

    def test_waiting_state_explains_owner(self):
        holder = self.launch("import time; time.sleep(.6)", label="typecheck")
        self.wait_for(lambda: any(j["state"] == "running" for j in runner.jobs(self.root)))
        waiter = self.launch(label="tests")
        rows = self.wait_for(lambda: [j for j in runner.jobs(self.root) if j["state"] == "waiting"])
        self.assertIn("typecheck", rows[0]["reason"])
        waiter.terminate()
        self.finish(waiter, 143)
        self.finish(holder)

    def test_sigkill_wrapper_keeps_child_resource_locked(self):
        holder = self.launch("import time; time.sleep(.8)", label="surviving child")
        self.wait_for(lambda: any(j["state"] == "running" for j in runner.jobs(self.root)))
        holder.kill()
        holder.wait(timeout=2)
        self.assertTrue(runner.jobs(self.root), "child must retain the live-job lease")
        contender = self.launch(timeout=.1)
        self.finish(contender, 124)
        holder.communicate(timeout=3)  # Child inherits these pipes until it exits.
        self.wait_for(lambda: not runner.jobs(self.root))
        self.finish(self.launch(timeout=.1))

    def test_running_cancellation_releases_resource(self):
        holder = self.launch("import time; time.sleep(30)")
        self.wait_for(lambda: any(j["state"] == "running" for j in runner.jobs(self.root)))
        holder.send_signal(signal.SIGINT)
        self.finish(holder, 130)
        self.assertEqual(runner.jobs(self.root), [])
        self.finish(self.launch(timeout=0))

    def test_pressure_blocks_then_can_clear(self):
        blocked = self.launch("print('must-not-run')", levels=["CRITICAL"], timeout=.1)
        out, err = self.finish(blocked, 124)
        self.assertEqual(out, "")
        self.assertIn("memory pressure: CRITICAL", err)
        out, _ = self.finish(self.launch("print('cleared')", levels=["DANGER", "WATCH"]))
        self.assertEqual(out, "cleared\n")

    def test_unknown_or_failed_pressure_never_starts(self):
        self.finish(self.launch(levels=["UNKNOWN"], timeout=0), 124)
        out, _ = self.finish(self.launch("print('must-not-run')", levels=[None]), 125)
        self.assertEqual(out, "")

    def test_exit_status_output_and_literal_arguments(self):
        literal = "$(touch nope); `pwd` spaces --gate"
        out, err = self.finish(self.launch(
            "import sys; print(sys.argv[1]); print('stderr',file=sys.stderr); sys.exit(7)", [literal]), 7)
        self.assertEqual(out, literal + "\n")
        self.assertEqual(err, "stderr\n")

    def test_wait_timeout_does_not_limit_command_runtime(self):
        self.finish(self.launch("import time; time.sleep(.2)", timeout=0))

    def test_nested_runner_fails_without_deadlock(self):
        code = "import memmon_runner,sys; sys.exit(memmon_runner.run(['true'],sys.argv[1],lambda: {'level':'HEALTHY'},resource='other'))"
        _, err = self.finish(self.launch(code, [str(self.root)]), 2)
        self.assertIn("nested runners", err)

    def test_stale_record_does_not_claim_ownership(self):
        folder = self.root / "runner"
        folder.mkdir()
        (folder / "stale.lease").touch()
        (folder / "stale.json").write_text(json.dumps(dict(wrapper_pid=os.getpid(), state="running")))
        self.assertEqual(runner.jobs(self.root), [])
        self.finish(self.launch())
        self.assertFalse((folder / "stale.json").exists())

    def test_lock_inode_stays_stable(self):
        self.finish(self.launch())
        path = self.root / "runner/heavy.lock"
        inode = path.stat().st_ino
        self.finish(self.launch())
        self.assertEqual(path.stat().st_ino, inode)

    def test_job_records_omit_command_arguments(self):
        proc = self.launch("import time; time.sleep(.3)", ["private-token-for-test"])
        rows = self.wait_for(lambda: runner.jobs(self.root))
        self.assertNotIn("private-token-for-test", json.dumps(rows))
        self.finish(proc)

    def test_invalid_resource_and_timeout(self):
        for resource, timeout in (("../bad", 1), ("x", -1), ("x", float("nan")), ("x", float("inf"))):
            with self.subTest(resource=resource, timeout=timeout), self.assertRaises(ValueError):
                runner.run(["true"], self.root, lambda: {}, resource, timeout)

    def test_missing_executable_releases_lock(self):
        self.assertEqual(runner.run(["/no/such/memmon-test-command"], self.root,
                                    lambda: {"level": "HEALTHY"}), 127)
        self.finish(self.launch(timeout=0))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--worker"]:
        payload = json.loads(sys.argv[2])
        levels = payload.pop("levels", ["HEALTHY"])

        def pressure():
            level = levels.pop(0) if len(levels) > 1 else levels[0]
            if level is None:
                raise OSError("test pressure failure")
            return {"level": level}

        raise SystemExit(runner.run(pressure_reader=pressure, **payload))
    unittest.main()
