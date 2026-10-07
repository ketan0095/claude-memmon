"""Action engine tests (I-1 to I-4, I-6). Rows marked real spawn bounded
synthetic sleepers and signal nothing else: every engine here is either fed
an injected process table, or is a GuardedEngine whose kill function refuses
any PID the test did not spawn."""

import fcntl
import json
import os
import plistlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import memmon
import memmon_act as ma
import memmon_owners as mo
import memmon_procs as mp
import memmon_runner
from testkit import (MB, NOTE, PY, SLEEP, FakeSource, P, Registry, TempState,
                     fake_engine, session_file)

T0 = 1_791_400_000
HERE = os.path.dirname(os.path.abspath(__file__))


def tok(action, root, target, ts=None, **extra):
    return mo.mint_token({"v": 1, "action": action, "owner_id": "claude:t",
                          "owner_root": {"pid": root.pid, "start": list(root.start)},
                          "target": {"pid": target.pid, "start": list(target.start)},
                          "snapshot_ts": T0 if ts is None else ts, **extra})


class FakeActTests(unittest.TestCase):
    """Races and refusals on an injected table: nothing real is signalled."""

    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.close)
        self.sessions = memmon.CLAUDE_SESSIONS_DIR
        self.ctx = mo.Context(sessions_dir=self.sessions)
        me = os.getpid()
        self.root = P(10, start=(T0 + 10, 0), comm="2.1.293")
        session_file(self.sessions, 10, T0 + 10, job_id="0000aaaa")
        self.procs = [
            P(me, ppid=0), self.root,
            P(11, ppid=10, pgid=10, comm="npm"),           # conversation helper
            P(20, ppid=10), P(21, ppid=20, pgid=20), P(22, ppid=21, pgid=20),
            P(40, comm="other"),
        ]
        self.src = FakeSource(self.procs, argv={20: ["/bin/zsh", "-c", "eval 'pnpm test'"]})
        self.used = iter([10_000 * MB, 7_900 * MB, 0, 0])
        self.eng = fake_engine(self.src, self.ctx, memmon.ACTIONS_LOCK,
                               clock=lambda: T0 + 5,
                               system_reader=lambda: {"used_bytes": next(self.used)})

    def run_act(self, action, token):
        return self.eng.run(action, token)

    def job_token(self, **kw):
        return tok("stop-job", self.src.table[10], self.src.table[20], **kw)

    def test_stop_job_signals_each_member_once_leaves_first(self):
        out = self.run_act("stop-job", self.job_token())
        self.assertEqual(out["result"], "stopped")
        self.assertEqual(ma.exit_code(out), 0)
        self.assertEqual(self.src.signals, [(22, signal.SIGTERM), (21, signal.SIGTERM),
                                            (20, signal.SIGTERM)])
        self.assertIn(10, self.src.table)
        self.assertIn(11, self.src.table)

    def test_used_bytes_measured_before_and_after(self):
        # A19
        out = self.run_act("stop-job", self.job_token())
        self.assertEqual((out["used_bytes_before"], out["used_bytes_after"]),
                         (10_000 * MB, 7_900 * MB))
        self.assertEqual(out["measured_at"], T0 + 5)

    def test_already_exited_sends_nothing(self):
        # A5
        token = self.job_token()
        for pid in (20, 21, 22):
            del self.src.table[pid]
        out = self.run_act("stop-job", token)
        self.assertEqual((out["result"], ma.exit_code(out)), ("already_exited", 0))
        self.assertEqual(self.src.signals, [])

    def test_act_identity_target_changed(self):
        # A6: same PID, new start.
        token = self.job_token()
        self.src.table[20] = P(20, ppid=10, start=(T0 + 999, 1))
        out = self.run_act("stop-job", token)
        self.assertEqual((out["result"], out["reason"]), ("refused", "target_changed"))
        self.assertEqual(ma.exit_code(out), 4)
        self.assertEqual(self.src.signals, [])

    def test_act_identity_ownership_changed(self):
        # A7
        token = self.job_token()
        self.src.table[20].ppid = 1
        out = self.run_act("stop-job", token)
        self.assertEqual(out["reason"], "ownership_changed")
        self.assertEqual(self.src.signals, [])

    def test_act_identity_reread_before_every_signal(self):
        # I-1: a member replaced between capture and its signal is not signalled.
        def swap(captured):
            self.src.table[21] = P(21, ppid=20, pgid=20, start=(T0 + 777, 7))
        self.eng.after_capture = swap
        out = self.run_act("stop-job", self.job_token())
        self.assertNotIn((21, signal.SIGTERM), self.src.signals)
        self.assertIn((22, signal.SIGTERM), self.src.signals)
        self.assertEqual(out["result"], "partial")
        self.assertIn(21, [r["pid"] for r in out["remaining"]])

    def test_stale_token_and_degraded_inventory(self):
        # A10
        fresh = self.job_token()
        out = self.run_act("stop-job", self.job_token(ts=T0 + 5 - 121))
        self.assertEqual(out["reason"], "stale_token")
        self.src.name = "degraded"
        out = self.run_act("stop-job", fresh)
        self.assertEqual((out["result"], out["reason"]), ("refused", "degraded_identity"))
        self.src.name = "libproc"
        out = self.run_act("stop-job", self.job_token(ts=T0 + 5 - 119))
        self.assertEqual(out["result"], "stopped")

    def test_act_protected_owner_root(self):
        # A11
        out = self.run_act("stop-job", tok("stop-job", self.root, self.root))
        self.assertEqual(out["reason"], "protected")
        self.assertEqual(self.src.signals, [])

    def test_act_protected_conversation_pgid(self):
        out = self.run_act("stop-job", tok("stop-job", self.root, self.src.table[11]))
        self.assertEqual(out["reason"], "protected")

    def test_act_protected_other_uid_and_self(self):
        self.src.table[23] = P(23, ppid=20, uid=0)
        out = self.run_act("stop-job", tok("stop-job", self.root, self.src.table[23]))
        self.assertEqual(out["reason"], "protected")
        me = self.src.table[os.getpid()]
        me.ppid = 20
        out = self.run_act("stop-job", tok("stop-job", self.root, me))
        self.assertEqual(out["reason"], "protected")
        self.assertEqual(self.src.signals, [])

    def test_act_protected_other_owners_root(self):
        self.src.table[30] = P(30, ppid=20, start=(T0 + 30, 0))
        session_file(self.sessions, 30, T0 + 30, job_id="0000bbbb")
        out = self.run_act("stop-job", tok("stop-job", self.root, self.src.table[30]))
        self.assertEqual(out["reason"], "protected")

    def test_end_session_refuses_shared_and_unknown(self):
        out = self.run_act("end-session", tok("end-session", self.src.table[40],
                                              self.src.table[40]))
        self.assertEqual(out["reason"], "not_stoppable")
        self.assertEqual(self.src.signals, [])

    def test_nested_owner_kept(self):
        # A13 control: end-session on the outer session keeps the inner root.
        self.src.table[30] = P(30, ppid=21, pgid=30, start=(T0 + 30, 0))
        self.src.table[31] = P(31, ppid=30, pgid=30)
        session_file(self.sessions, 30, T0 + 30, job_id="0000bbbb")
        out = self.run_act("end-session", tok("end-session", self.root, self.root))
        self.assertEqual(out["result"], "stopped")
        self.assertEqual(out["kept"], ["claude:0000bbbb"])
        signalled = {p for p, _ in self.src.signals}
        self.assertEqual(signalled, {10, 11, 20, 21, 22})
        self.assertIn(30, self.src.table)
        self.assertIn(31, self.src.table)

    def test_end_session_reports_respawn(self):
        # R1: the daemon restarts the worker under the same job id.
        def respawn(pid, sig):
            if pid == 10:
                self.src.table[50] = P(50, start=(T0 + 50, 0))
                os.remove(os.path.join(self.sessions, "10.json"))
                session_file(self.sessions, 50, T0 + 50, job_id="0000aaaa")
        self.src.on_kill = respawn
        out = self.run_act("end-session", tok("end-session", self.root, self.root))
        self.assertEqual(out["result"], "respawned")
        self.assertEqual(out["respawned_as"], {"pid": 50})
        self.assertEqual(ma.exit_code(out), 0)

    def test_act_force_only_after_partial(self):
        # I-4
        self.src.ignore_term = {21}
        job = self.job_token()
        out = self.run_act("stop-job", job)
        self.assertEqual((out["result"], ma.exit_code(out)), ("partial", 3))
        self.assertNotIn(signal.SIGKILL, [s for _, s in self.src.signals])
        body = mo.decode_token(out["force_token"])
        self.assertEqual([s["pid"] for s in body["survivors"]], [21])
        # A process token can never be used to force.
        refused = self.run_act("force", job)
        self.assertEqual(refused["reason"], "wrong_action")
        forced = self.run_act("force", out["force_token"])
        self.assertEqual((forced["result"], ma.exit_code(forced)), ("force_stopped", 0))
        self.assertEqual([s for s in self.src.signals if s[1] == signal.SIGKILL],
                         [(21, signal.SIGKILL)])

    def test_act_force_skips_changed_identity_and_stale_token(self):
        self.src.ignore_term = {21}
        out = self.run_act("stop-job", self.job_token())
        self.src.table[21] = P(21, ppid=1, start=(T0 + 555, 5))   # PID reused
        forced = self.run_act("force", out["force_token"])
        self.assertNotIn((21, signal.SIGKILL), self.src.signals)
        self.assertEqual(forced["result"], "force_stopped")
        late = fake_engine(self.src, self.ctx, memmon.ACTIONS_LOCK, clock=lambda: T0 + 500)
        self.assertEqual(late.run("force", out["force_token"])["reason"], "stale_token")

    def test_legacy_end_session_routes_through_engine(self):
        dry = memmon.end_session(10, False, engine=self.eng)
        self.assertIn("would end claude:0000aaaa", dry)
        self.assertEqual(self.src.signals, [])
        self.assertTrue(memmon.end_session(20, True, engine=self.eng).startswith("refused"))
        self.assertEqual(self.src.signals, [])
        self.assertIn("end claude:0000aaaa: stopped",
                      memmon.end_session(10, True, engine=self.eng))
        self.assertEqual({s for _, s in self.src.signals}, {signal.SIGTERM})

    def test_spare_selector_needs_an_unclaimed_stale_spare(self):
        claim = os.path.join(self.state.root, "x.claim.sock")
        open(claim, "w").close()
        self.src.table[60] = P(60, start=(T0 - 5 * 3600, 0))
        self.src._argv[60] = ["claude", "bg-spare", "--bg-spare", claim]
        inv = mp.snapshot(self.src, clock=lambda: T0)
        self.assertTrue(memmon._spare_still_selected(inv, 60))
        os.remove(claim)                                  # claimed since the listing
        self.assertFalse(memmon._spare_still_selected(inv, 60))

    def test_wrong_action_and_bad_token(self):
        self.assertEqual(self.run_act("stop-server", self.job_token())["reason"],
                         "wrong_action")
        self.assertEqual(self.run_act("stop-job", "garbage")["reason"], "bad_token")

    def test_busy_lock_refuses_within_limit(self):
        # Serialisation: a held actions.lock means refused: busy, never interleaved.
        os.makedirs(os.path.dirname(memmon.ACTIONS_LOCK), exist_ok=True)
        holder = open(memmon.ACTIONS_LOCK, "a+")
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            clock = iter(range(0, 1000))
            eng = fake_engine(self.src, self.ctx, memmon.ACTIONS_LOCK,
                              clock=lambda: T0 + 5, mono=lambda: next(clock))
            out = eng.run("stop-job", self.job_token())
        finally:
            holder.close()
        self.assertEqual((out["result"], out["reason"]), ("refused", "busy"))
        self.assertEqual(self.src.signals, [])

    def test_lock_fd_shares_the_callers_lock_and_keeps_it(self):
        os.makedirs(os.path.dirname(memmon.ACTIONS_LOCK), exist_ok=True)
        held = open(memmon.ACTIONS_LOCK, "a+")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX)
        job = self.job_token()
        out = self.eng.run("stop-job", job, lock_fd=held.fileno())
        self.assertEqual(out["result"], "stopped")
        other = open(memmon.ACTIONS_LOCK, "a+")
        self.addCleanup(other.close)
        with self.assertRaises(BlockingIOError):          # still held by the caller
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stray = tempfile.TemporaryFile()
        self.addCleanup(stray.close)
        out = self.eng.run("stop-job", job, lock_fd=stray.fileno())
        self.assertEqual(out["reason"], "bad_lock_fd")


class VerifyAppTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.close)
        bundle = os.path.join(self.state.root, "Applications", "Brave Browser.app")
        os.makedirs(os.path.join(bundle, "Contents"))
        with open(os.path.join(bundle, "Contents", "Info.plist"), "wb") as fh:
            plistlib.dump({"CFBundleIdentifier": "com.example.brave"}, fh)
        exe = f"{bundle}/Contents/MacOS/Brave Browser"
        self.src = FakeSource([P(os.getpid(), ppid=0), P(60), P(61, ppid=60), P(70)],
                              paths={60: exe, 61: exe, 70: exe})
        ctx = mo.Context(sessions_dir=memmon.CLAUDE_SESSIONS_DIR)
        self.eng = fake_engine(self.src, ctx, memmon.ACTIONS_LOCK, clock=lambda: T0 + 1)

    def token(self, **over):
        insts = [{"pid": p, "start": list(self.src.table[p].start),
                  "launch_date": self.src.table[p].start[0] + self.src.table[p].start[1] / 1e6}
                 for p in (60, 70)]
        body = {"v": 1, "action": "quit-app", "owner_id": "app:com.example.brave",
                "bundle_id": "com.example.brave", "instances": insts, "snapshot_ts": T0}
        body.update(over)
        return mo.mint_token(body)

    def test_verify_app_checks_every_instance(self):
        out = self.eng.run("verify-app", self.token())
        self.assertEqual((out["result"], ma.exit_code(out)), ("verified", 0))
        self.assertEqual([i["status"] for i in out["instances"]], ["running", "running"])
        self.assertEqual(self.src.signals, [])

    def test_verify_app_refuses_changed_instance_or_bundle(self):
        token = self.token()
        self.src.table[70] = P(70, start=(T0 + 70, 70))
        self.assertEqual(self.eng.run("verify-app", token)["reason"], "instance_changed")
        self.assertEqual(self.eng.run("verify-app", self.token(bundle_id="com.other"))
                         ["reason"], "instance_changed")

    def test_verify_app_all_gone(self):
        token = self.token()
        del self.src.table[60], self.src.table[70]
        self.assertEqual(self.eng.run("verify-app", token)["result"], "already_exited")


class NoKillpgTests(unittest.TestCase):
    def test_act_no_killpg(self):
        # I-1 / D28: the engine and everything it calls signal per PID only.
        for name in ("memmon_act.py", "memmon_owners.py", "memmon_procs.py"):
            with open(os.path.join(HERE, name)) as fh:
                self.assertNotRegex(fh.read(), r"killpg\s*\(", name)


# ------------------------------------------------------- real processes

A_CODE = NOTE + """
import subprocess, sys, time
note("A")
mode = sys.argv[2]
kw = {"start_new_session": True} if mode != "pgrp" else {"preexec_fn": __import__("os").setpgrp}
subprocess.Popen([sys.executable, "-c", sys.argv[3], sys.argv[1]] + sys.argv[4:], **kw)
time.sleep(120)
"""

BUILD_3 = NOTE + """
import os, subprocess, sys, time
note("build")
for i in range(3):
    subprocess.Popen([sys.executable, "-c",
        "import os,sys,time; open(sys.argv[1],'a').write(f'sleeper {os.getpid()}\\\\n'); time.sleep(120)",
        sys.argv[1]], preexec_fn=os.setsid if i == 2 else None)
time.sleep(120)
"""

BUILD_IGNORES_TERM = NOTE + """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
note("build")
time.sleep(120)
"""

BUILD_FORKS_ON_TERM = NOTE + """
import os, signal, subprocess, sys, time
def on_term(*_):
    subprocess.Popen([sys.executable, "-c",
        "import os,sys,time; open(sys.argv[1],'a').write(f'gc {os.getpid()}\\\\n'); time.sleep(120)",
        sys.argv[1]])
    time.sleep(0.8)
    os._exit(0)
signal.signal(signal.SIGTERM, on_term)
note("build")
time.sleep(120)
"""

BUILD_DOUBLE_FORK = NOTE + """
import subprocess, sys, time
note("build")
subprocess.Popen([sys.executable, "-c",
    "import subprocess,sys; subprocess.Popen([sys.executable, '-c', "
    "\\"import os,sys,time; open(sys.argv[1],'a').write(f'gc {os.getpid()}\\\\\\\\n'); time.sleep(120)\\", "
    "sys.argv[1]])", sys.argv[1]]).wait()
time.sleep(120)
"""

JOINER = NOTE + """
import os, sys, time
note("joiner")
flag = sys.argv[2]
while not os.path.exists(flag):
    time.sleep(0.01)
os.setpgid(0, int(open(flag).read()))
time.sleep(120)
"""

ORPHAN = NOTE + """
import os, signal, subprocess, sys
# vitest  (marks this process reapable to the orphan selector)
code = ("import os,signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "open(sys.argv[1],'a').write(f'orphan {os.getpid()}\\\\n'); time.sleep(120) # vitest")
subprocess.Popen([sys.executable, "-c", code, sys.argv[1]], start_new_session=True)
"""


class RealProcessTests(unittest.TestCase):
    """Bounded synthetic process trees; nothing outside them is signalled."""

    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.close)
        self.reg = Registry(self)
        self.addCleanup(self.reg.cleanup)
        self.sessions = memmon.CLAUDE_SESSIONS_DIR
        self.ctx = mo.Context(sessions_dir=self.sessions,
                              socks_dir=os.path.join(self.state.root, "socks"),
                              codex_home=os.path.join(self.state.root, "codex"))
        self.eng = self.reg.engine(self.ctx, memmon.ACTIONS_LOCK)

    def stand_in(self, build_code, mode="session", extra=()):
        """A Claude stand-in (registered as a session root) and its build."""
        a = self.reg.spawn(A_CODE, mode, build_code, *extra)
        session_file(self.sessions, a.pid, self.reg.ids[a.pid][0], job_id=f"{a.pid:08x}")
        return a.pid

    def test_real_stop_job_with_setsid_member(self):
        # A3
        a = self.stand_in(BUILD_3)
        b = self.reg.spawn(NOTE + "note('B')\n" + SLEEP).pid
        session_file(self.sessions, b, self.reg.ids[b][0], job_id=f"{b:08x}")
        notes = self.reg.wait_notes(6)
        sleepers = [pid for tag, pid in self.reg.read_notes() if tag == "sleeper"]
        build = notes["build"]
        before = {p: self.reg.identity(p) for p in (a, b)}
        out = self.eng.run("stop-job", self.reg.token("stop-job", a, build))
        self.assertEqual(out["result"], "stopped", out)
        self.assertEqual(out["exited"], 4)
        self.assertEqual(sorted(p for p, _ in self.eng.sent), sorted([build] + sleepers))
        self.assertEqual({s for _, s in self.eng.sent}, {signal.SIGTERM})
        for p in [build] + sleepers:
            self.assertFalse(self.reg.alive(p), p)
        self.assertEqual({p: self.reg.identity(p) for p in (a, b)}, before)

    def test_real_outsider_joining_group_is_remaining(self):
        # A4
        a = self.stand_in(BUILD_IGNORES_TERM.replace(
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)", "pass"), mode="pgrp")
        flag = os.path.join(self.reg.dir, "flag")
        joiner = self.reg.spawn(JOINER, flag).pid
        notes = self.reg.wait_notes(3)
        build = notes["build"]
        pgid = self.reg.src.read(build).pgid

        def join(captured):
            self.assertNotIn(joiner, captured)
            with open(flag + ".tmp", "w") as fh:
                fh.write(str(pgid))
            os.replace(flag + ".tmp", flag)
            deadline = time.monotonic() + 5
            while self.reg.src.read(joiner).pgid != pgid:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
        self.eng.after_capture = join
        out = self.eng.run("stop-job", self.reg.token("stop-job", a, build))
        self.assertEqual(out["result"], "partial", out)
        self.assertIn(joiner, [r["pid"] for r in out["remaining"]])
        self.assertTrue(self.reg.alive(joiner))
        self.assertNotIn(joiner, [p for p, _ in self.eng.sent])
        self.assertNotIn("force_token", out)       # never signalled, so never forced

    def test_real_term_ignoring_child_needs_explicit_force(self):
        # A8
        a = self.stand_in(BUILD_IGNORES_TERM)
        build = self.reg.wait_notes(2)["build"]
        out = self.eng.run("stop-job", self.reg.token("stop-job", a, build))
        self.assertEqual((out["result"], ma.exit_code(out)), ("partial", 3))
        self.assertTrue(self.reg.alive(build))
        self.assertEqual(self.eng.sent, [(build, signal.SIGTERM)])
        forced = self.eng.run("force", out["force_token"])
        self.assertEqual(forced["result"], "force_stopped", forced)
        self.assertFalse(self.reg.alive(build))
        self.assertTrue(self.reg.alive(a))

    def test_real_grandchild_forked_during_grace_is_captured(self):
        # A9, first case
        a = self.stand_in(BUILD_FORKS_ON_TERM)
        build = self.reg.wait_notes(2)["build"]
        out = self.eng.run("stop-job", self.reg.token("stop-job", a, build))
        gc = self.reg.wait_notes(3)["gc"]
        self.assertEqual(out["result"], "stopped", out)
        self.assertIn((gc, signal.SIGTERM), self.eng.sent)
        self.assertIsNone(self.reg.identity(gc))

    def test_real_reparented_grandchild_reported_not_signalled(self):
        # A9, second case: it left the lineage before any scan saw it.
        a = self.stand_in(BUILD_DOUBLE_FORK)
        notes = self.reg.wait_notes(3)
        build, gc = notes["build"], notes["gc"]
        deadline = time.monotonic() + 5
        while self.reg.src.read(gc).ppid != 1:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        out = self.eng.run("stop-job", self.reg.token("stop-job", a, build))
        self.assertEqual(out["result"], "partial", out)
        self.assertIn(gc, [r["pid"] for r in out["remaining"]])
        self.assertNotIn(gc, [p for p, _ in self.eng.sent])
        self.assertTrue(self.reg.alive(gc))

    def test_real_engine_never_calls_killpg(self):
        a = self.stand_in(BUILD_3)
        build = self.reg.wait_notes(5)["build"]
        with mock.patch.object(os, "killpg", side_effect=AssertionError("killpg")):
            out = self.eng.run("stop-job", self.reg.token("stop-job", a, build))
        self.assertEqual(out["result"], "stopped", out)

    def test_guard_rejects_unregistered_tokens(self):
        a = self.stand_in(BUILD_3)
        self.reg.wait_notes(5)
        stranger = {"pid": os.getppid(), "start": [1, 1]}
        body = {"v": 1, "action": "stop-job", "owner_id": "x", "owner_root": stranger,
                "target": {"pid": a, "start": list(self.reg.ids[a])},
                "snapshot_ts": time.time()}
        with self.assertRaises(AssertionError):
            self.eng.run("stop-job", mo.mint_token(body))
        with self.assertRaises(AssertionError):
            self.reg.guarded_kill(os.getppid(), 0)

    def test_real_stop_managed_job(self):
        # A14
        runner_dir = os.path.join(self.state.root, "runstate")
        payload = dict(command=[PY, "-c", SLEEP], state_dir=runner_dir, poll_interval=0.02)
        wrapper = subprocess.Popen([PY, os.path.join(HERE, "test_runner.py"), "--worker",
                                    json.dumps(payload)], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        self.reg.popens.append(wrapper)
        self.reg.register(wrapper.pid)
        deadline = time.monotonic() + 10
        rows = []
        while not rows or not rows[0].get("child_start"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
            rows = memmon_runner.jobs(runner_dir)
        row = rows[0]
        child = self.reg.register(row["child_pid"])
        self.ctx.leases = rows
        self.eng.leases_fn = lambda: memmon_runner.jobs(runner_dir)
        wrong = self.eng.run("stop-managed-job", self.reg.token(
            "stop-managed-job", child, child, run_id="not-this-run"))
        self.assertEqual(wrong["reason"], "lease_mismatch")
        self.assertTrue(self.reg.alive(child))
        out = self.eng.run("stop-managed-job", self.reg.token(
            "stop-managed-job", child, child, owner_id=f"job:{row['id']}", run_id=row["id"]))
        self.assertEqual(out["result"], "stopped", out)
        self.assertEqual(wrapper.wait(timeout=10), 128 + signal.SIGTERM)
        self.assertNotIn(wrapper.pid, [p for p, _ in self.eng.sent])

    def test_reap_stops_at_partial(self):
        # A22 / A22b
        self.reg.spawn(ORPHAN).wait(timeout=10)
        orphan = self.reg.wait_notes(1)["orphan"]
        snap = {"orphans": [{"pid": orphan, "mem": 200 * MB, "age": 10, "orphaned": True,
                             "tag": "vitest", "worktree": ""}],
                "orphan_total": 200 * MB}
        text = memmon.reap(snap, True, engine=self.eng)
        self.assertIn("memmon reap --force ", text)
        self.assertTrue(self.reg.alive(orphan))
        self.assertEqual(self.eng.sent, [(orphan, signal.SIGTERM)])
        force = text.split("memmon reap --force ")[1].split()[0]

        self.reg.spawn(ORPHAN).wait(timeout=10)                 # appears in between
        self.reg.wait_notes(2)
        late = [p for t, p in self.reg.read_notes() if t == "orphan" and p != orphan][0]
        out = self.eng.force(force, origin="reap")
        self.assertEqual(out["result"], "force_stopped", out)
        self.assertFalse(self.reg.alive(orphan))
        self.assertTrue(self.reg.alive(late))
        self.assertNotIn(late, [p for p, _ in self.eng.sent])
        self.assertEqual(self.eng.run("force", force, origin="stop-job")["reason"],
                         "wrong_action")

    def test_reap_refuses_attributed_or_changed_target(self):
        a = self.stand_in(BUILD_3)
        self.reg.wait_notes(5)
        out = self.eng.reap([a], memmon._orphan_still_selected)
        self.assertEqual(out["refused"], [{"pid": a, "reason": "attributed"}])
        self.assertEqual(self.eng.sent, [])


class CliTests(unittest.TestCase):
    """`memmon act` end to end in a subprocess with an isolated HOME."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.reg = Registry(self)
        self.addCleanup(self.reg.cleanup)
        self.sessions = os.path.join(self.home.name, ".claude", "sessions")
        os.makedirs(self.sessions)

    def act(self, *args):
        env = dict(os.environ, HOME=self.home.name)
        proc = subprocess.run([PY, os.path.join(HERE, "memmon.py"), "act", *args],
                              capture_output=True, text=True, timeout=60, env=env)
        return proc.returncode, json.loads(proc.stdout)

    def test_cli_exit_codes_and_json(self):
        a = self.reg.spawn(A_CODE, "session", BUILD_3).pid
        session_file(self.sessions, a, self.reg.ids[a][0], job_id=f"{a:08x}")
        build = self.reg.wait_notes(5)["build"]
        stale = mo.mint_token({**mo.decode_token(self.reg.token("stop-job", a, build)),
                               "snapshot_ts": time.time() - 500})
        code, out = self.act("stop-job", "--target", stale)
        self.assertEqual((code, out["result"], out["reason"]), (4, "refused", "stale_token"))
        code, out = self.act("stop-job", "--target", self.reg.token("stop-job", a, build))
        self.assertEqual((code, out["result"]), (0, "stopped"), out)
        self.assertFalse(self.reg.alive(build))
        self.assertTrue(self.reg.alive(a))
        self.assertTrue(os.path.exists(os.path.join(
            self.home.name, ".claude", "memmon", "runner", "coord", "actions.lock")))


if __name__ == "__main__":
    unittest.main()
