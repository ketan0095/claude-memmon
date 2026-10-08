"""Action engine tests: identity, protection, graceful-first and force. Rows marked real spawn bounded
synthetic sleepers and signal nothing else: every engine here is either fed
an injected process table, or is a GuardedEngine whose kill function refuses
any PID the test did not spawn."""

import contextlib
import fcntl
import io
import json
import re
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
from testkit import (MB, NOTE, PY, SLEEP, FakeClock, FakeSource, P, Registry,
                     TempState, fake_engine, session_file)
from test_owners import fake_lsof

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
        # A member replaced between capture and its signal is not signalled.
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

    def test_act_force_only_after_partial(self):
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

    def test_force_counts_only_what_it_signalled(self):
        # An observed group member is reported, never forced, and keeps
        # force from claiming success.
        self.src.ignore_term = {21}
        self.src.table[30] = P(30, ppid=1, pgid=20)          # outside the lineage
        out = self.run_act("stop-job", self.job_token())
        rows = {r["pid"]: r["forceable"] for r in out["remaining"]}
        self.assertEqual(rows, {21: True, 30: False})
        self.assertEqual((out["forceable"], out["observed"]), (1, 1))
        forced = self.run_act("force", out["force_token"])
        self.assertEqual((forced["result"], forced["reason"]), ("partial", "outside_force"))
        self.assertEqual((forced["named"], forced["exited"], forced["killed"]), (1, 1, 1))
        self.assertEqual(forced["observed"], 1)
        self.assertEqual([(r["pid"], r["role"], r["signalled"]) for r in forced["remaining"]],
                         [(30, "observed", False)])
        self.assertNotIn((30, signal.SIGKILL), self.src.signals)
        self.assertNotIn("force_token", forced)
        text = memmon._stop_report(forced, "force")
        self.assertIn("1 of 1 named survivor(s) gone: 1 killed by SIGKILL, 0 had already "
                      "exited", text)
        self.assertIn("30  proc  (observed, never signalled)", text)
        self.assertNotIn("of 0", text)

    def test_force_tells_killed_from_already_gone_and_kept_alive(self):
        # Of three survivors: one is killed, one exits before force, one has
        # since become a session (left alone). Only the left-alone one is
        # remaining, as an unsignalled survivor.
        self.src.ignore_term = {21, 22, 23}
        self.src.table[23] = P(23, ppid=20, pgid=20)
        out = self.run_act("stop-job", self.job_token())
        self.assertEqual(out["forceable"], 3)
        self.src.table.pop(22)
        start = self.src.table[23].start
        self.src.table[23] = P(23, ppid=1, pgid=23, start=start)
        session_file(self.sessions, 23, start[0], job_id="0000dddd")
        forced = self.run_act("force", out["force_token"])
        self.assertEqual((forced["named"], forced["exited"], forced["killed"],
                          forced["observed"]), (3, 2, 1, 0))
        self.assertEqual([(r["pid"], r["role"], r["signalled"]) for r in forced["remaining"]],
                         [(23, "survivor", False)])
        text = memmon._stop_report(forced, "force")
        self.assertIn("1 killed by SIGKILL, 1 had already exited", text)
        self.assertNotIn("never signalled", text)

    def test_protected_survivor_that_ends_on_its_own_is_not_already_exited(self):
        # One survivor is killed; another became a session (protected) and
        # ends on its own while force waits. It was neither killed nor gone
        # before force ran.
        self.src.ignore_term = {21, 22}
        out = self.run_act("stop-job", self.job_token())
        start = self.src.table[22].start
        self.src.table[22] = P(22, ppid=1, pgid=22, start=start)
        session_file(self.sessions, 22, start[0], job_id="0000dddd")
        real_kill = self.src.kill

        def kill(pid, sig):
            real_kill(pid, sig)
            if pid == 21 and sig == signal.SIGKILL:
                self.src.table.pop(22, None)
        self.eng.kill = kill
        forced = self.run_act("force", out["force_token"])
        self.assertEqual((forced["result"], forced["exited"], forced["killed"],
                          forced["exited_unsignalled"]), ("force_stopped", 2, 1, 1))
        self.assertNotIn((22, signal.SIGKILL), self.src.signals)
        text = memmon._stop_report(forced, "force")
        self.assertIn("1 killed by SIGKILL, 0 had already exited", text)
        self.assertIn("1 ended on their own while protected", text)

    def test_force_row_says_why_a_survivor_was_not_killed(self):
        self.src.ignore_term = {21, 22}
        out = self.run_act("stop-job", self.job_token())
        real_kill = self.src.kill

        def kill(pid, sig):
            if pid == 22 and sig == signal.SIGKILL:
                raise PermissionError
            real_kill(pid, sig)
        self.eng.kill = kill
        forced = self.run_act("force", out["force_token"])
        rows = {r["pid"]: r for r in forced["remaining"]}
        self.assertEqual((rows[22]["skip_reason"], rows[22]["signalled"]), ("signal_failed", False))
        self.assertIn("22  proc  (SIGKILL could not be delivered)",
                      memmon._stop_report(forced, "force"))

    def test_graceful_signalled_is_what_was_delivered(self):
        real_kill = self.src.kill

        def kill(pid, sig):
            if pid == 22:
                raise PermissionError
            real_kill(pid, sig)
        self.eng.kill = kill
        out = self.run_act("stop-job", self.job_token())
        row = next(r for r in out["remaining"] if r["pid"] == 22)
        self.assertEqual((row["forceable"], row["signalled"]), (True, False))

    def test_observed_beyond_the_token_cap_keep_force_partial(self):
        # 70 observed processes; the token lists 64. Even with all 64 gone,
        # force cannot vouch for the other 6.
        self.src.ignore_term = {21}
        for pid in range(100, 170):
            self.src.table[pid] = P(pid, ppid=1, pgid=20)
        out = self.run_act("stop-job", self.job_token())
        self.assertEqual(out["observed"], 70)
        body = mo.decode_token(out["force_token"])
        self.assertEqual((len(body["observed"]), body["observed_total"]), (ma.OBSERVED_CAP, 70))
        for o in body["observed"]:
            self.src.table.pop(o["pid"])
        forced = self.run_act("force", out["force_token"])
        self.assertEqual((forced["result"], forced["reason"]), ("partial", "outside_force"))
        self.assertEqual((forced["observed"], forced["observed_unlisted"]), (0, 6))
        self.assertIn("6 more still running that memmon could not list",
                      memmon._stop_report(forced, "force"))
        for pid in range(100, 170):
            self.src.table.pop(pid, None)
        again = self.run_act("force", out["force_token"])
        self.assertEqual((again["result"], again["observed_unlisted"]), ("force_stopped", 0))

    def test_reused_parent_is_not_walked(self):
        # A captured parent whose PID now belongs to another process is not
        # a parent any more; its children are not ours.
        original = self.eng._alive
        self.eng._alive = lambda pid, start: pid == 21 or original(pid, start)

        def swap(captured):
            self.src.table[21] = P(21, ppid=1, pgid=21, start=(T0 + 777, 7))
            self.src.table[77] = P(77, ppid=21, pgid=21)
        self.eng.after_capture = swap
        self.run_act("stop-job", self.job_token())
        self.assertNotIn(77, [p for p, _ in self.src.signals])

    def test_owner_appearing_during_grace_is_kept(self):
        # A session root started under the job mid-stop is another owner.
        self.src.ignore_term = {21}

        def spawn(pid, sig):
            if pid == 22:
                self.src.table[25] = P(25, ppid=21, pgid=25, start=(T0 + 25, 0))
                session_file(self.sessions, 25, T0 + 25, job_id="0000cccc")
        self.src.on_kill = spawn
        out = self.run_act("stop-job", self.job_token())
        self.assertNotIn(25, [p for p, _ in self.src.signals])
        self.assertIn("claude:0000cccc", out["kept"])
        self.assertNotIn(25, [r["pid"] for r in out["remaining"]])

    def test_force_skips_survivor_that_became_an_owner(self):
        # Still alive and same identity, but now another owner's root.
        self.src.ignore_term = {21}
        out = self.run_act("stop-job", self.job_token())
        session_file(self.sessions, 21, self.src.table[21].start[0], job_id="0000dddd")
        forced = self.run_act("force", out["force_token"])
        self.assertNotIn((21, signal.SIGKILL), self.src.signals)
        self.assertEqual((forced["result"], forced["reason"], forced["exited"]),
                         ("partial", "survivors", 0))
        self.assertEqual([r["pid"] for r in forced["remaining"]], [21])

    def test_force_never_touches_memmon_itself(self):
        me = self.src.table[os.getpid()]
        token = mo.mint_token({"v": 1, "action": "force", "origin": "stop-job",
                               "survivors": [{"pid": me.pid, "start": list(me.start)}],
                               "snapshot_ts": T0 + 5})
        forced = self.run_act("force", token)
        self.assertEqual(self.src.signals, [])
        self.assertEqual((forced["result"], forced["exited"]), ("partial", 0))

    def test_force_token_from_the_future_is_stale(self):
        self.assertEqual(self.run_act("stop-job", self.job_token(ts=T0 + 5 + 6))["reason"],
                         "stale_token")
        self.assertEqual(self.run_act("stop-job", self.job_token(ts=T0 + 5 + 4))["result"],
                         "stopped")

    def lease(self, wrapper=30, child=31):
        row = {"id": "run1", "wrapper_pid": wrapper, "child_pid": child,
               "child_start": list(self.src.table[child].start)}
        self.eng.leases_fn = lambda: [row]
        return row

    def runner_fixture(self, **row):
        """Wrapper 30 under the session's tool shell, its TERM-ignoring child
        31 leading its own group, and a disowned session 45 in that group."""
        self.src.table[30] = P(30, ppid=20, pgid=20, comm="python3")
        self.src.table[31] = P(31, ppid=30, pgid=31, start=(T0 + 31, 31))
        self.src.table[45] = P(45, ppid=1, pgid=31, start=(T0 + 45, 0))
        session_file(self.sessions, 45, T0 + 45, job_id="0000cccc")
        self.src.ignore_term = {31}
        lease = {"id": "run1", "state": "running", "wrapper_pid": 30, "child_pid": 31,
                 "child_start": [T0 + 31, 31], **row}
        if lease.get("child_start") is None:
            lease.pop("child_start")
        self.eng.leases_fn = lambda: [lease]
        return lease

    def end_session(self):
        out = self.run_act("end-session", tok("end-session", self.root, self.root))
        return out, {p for p, _ in self.src.signals}

    def test_wrapper_held_for_a_lease_row_without_child_start(self):
        # Runners before this version never wrote child_start.
        self.runner_fixture(child_start=None)
        out, signalled = self.end_session()
        self.assertFalse(signalled & {30, 45}, signalled)
        self.assertIn(31, signalled)
        runner = next(r for r in out["remaining"] if r["pid"] == 30)
        self.assertEqual((runner["role"], runner["signalled"]), ("runner", False))

    def test_wrapper_stays_held_when_its_lease_row_disappears(self):
        # A re-read that misses the row must not release the wrapper while
        # its child still runs.
        lease = self.runner_fixture()
        rows = [lease]
        self.eng.leases_fn = lambda: list(rows)

        def drop(pid, sig):
            if pid == 31:
                rows.clear()
        self.src.on_kill = drop
        _, signalled = self.end_session()
        self.assertIn(31, signalled)
        self.assertFalse(signalled & {30, 45}, signalled)

    def test_wrapper_held_for_a_one_second_child_start(self):
        # A runner whose memmon read ps starts records (sec, 0).
        self.runner_fixture(child_start=[T0 + 31, 0])
        _, signalled = self.end_session()
        self.assertFalse(signalled & {30, 45}, signalled)

    def test_wrapper_held_in_its_starting_window(self):
        # The lease row does not name the child yet; the wrapper is held
        # while it has a live descendant.
        self.runner_fixture(state="starting", child_pid=None, child_start=None)
        _, signalled = self.end_session()
        self.assertFalse(signalled & {30, 45}, signalled)
        self.assertIn(31, signalled)

    def test_starting_window_hold_releases_once_the_wrapper_has_no_child(self):
        self.runner_fixture(state="starting", child_pid=None, child_start=None)
        self.src.ignore_term = set()
        self.end_session()
        order = [p for p, _ in self.src.signals]
        self.assertLess(order.index(31), order.index(30))
        self.assertNotIn(45, order)

    def test_wrapper_born_during_grace_is_held(self):
        # The job starts a `memmon run` after the first SIGTERM.
        self.src.ignore_term = {21}
        lease = {"id": "run1", "state": "running", "wrapper_pid": 30, "child_pid": 31,
                 "child_start": [T0 + 31, 31]}
        rows = []
        self.eng.leases_fn = lambda: list(rows)

        def spawn(pid, sig):
            if pid == 22 and 30 not in self.src.table:
                self.src.table[30] = P(30, ppid=21, pgid=20, comm="python3")
                self.src.table[31] = P(31, ppid=30, pgid=31, start=(T0 + 31, 31))
                self.src.table[45] = P(45, ppid=1, pgid=31, start=(T0 + 45, 0))
                session_file(self.sessions, 45, T0 + 45, job_id="0000cccc")
                self.src.ignore_term = {21, 31}
                rows.append(lease)
        self.src.on_kill = spawn
        out = self.run_act("stop-job", self.job_token())
        signalled = {p for p, _ in self.src.signals}
        self.assertFalse(signalled & {30, 45}, signalled)
        self.assertIn(31, signalled)
        self.assertIn(30, [r["pid"] for r in out["remaining"] if r["role"] == "runner"])

    def test_stop_managed_job_lease_check_stays_exact(self):
        # The lenient hold rule never loosens the lease identity check.
        self.src.table[30] = P(30, ppid=20, pgid=20, comm="python3")
        self.src.table[31] = P(31, ppid=30, pgid=31, start=(T0 + 31, 31))
        self.eng.leases_fn = lambda: [{"id": "run1", "wrapper_pid": 30, "child_pid": 31,
                                       "child_start": [T0 + 31, 0]}]
        out = self.run_act("stop-managed-job", tok("stop-managed-job", self.root,
                                                   self.src.table[31], run_id="run1"))
        self.assertEqual((out["result"], out["reason"]), ("refused", "lease_mismatch"))
        self.assertEqual(self.src.signals, [])

    def test_force_waits_for_the_runner_of_a_killed_child(self):
        # SIGKILL of the TERM-ignoring child lets its wrapper exit, and then
        # the result is force_stopped, not a runner left outside force.
        self.runner_fixture()
        self.src.table.pop(45)
        out, _ = self.end_session()
        self.assertEqual(out["result"], "partial", out)
        body = mo.decode_token(out["force_token"])
        self.assertEqual(body["runners"], [{"pid": 30, "start": list(self.src.table[30].start),
                                            "child": {"pid": 31, "start": [T0 + 31, 31]}}])
        self.assertEqual(body["observed_total"], 0)
        # The runner notices its child died a poll later, then exits.
        self.eng.sleep = lambda s: (31 not in self.src.table) and self.src.table.pop(30, None)
        forced = self.run_act("force", out["force_token"])
        self.assertEqual(forced["result"], "force_stopped", forced)
        self.assertNotIn((30, signal.SIGKILL), self.src.signals)

    def test_a_runner_that_outlives_force_stays_outside_force(self):
        self.runner_fixture()
        self.src.table.pop(45)
        out, _ = self.end_session()
        forced = self.run_act("force", out["force_token"])
        self.assertEqual((forced["result"], forced["reason"]), ("partial", "outside_force"))
        self.assertEqual([(r["pid"], r["role"]) for r in forced["remaining"]], [(30, "runner")])
        self.assertNotIn(30, [p for p, _ in self.src.signals])

    def test_runner_wrapper_guarding_a_kept_owner_is_not_signalled(self):
        # A `memmon run` wrapper killpg()s its child's group on SIGTERM;
        # that group holds a nested session, so the wrapper is signalled only
        # once its child is gone (it then exits without touching the group).
        self.src.table[30] = P(30, ppid=20, pgid=20, comm="python3")
        self.src.table[31] = P(31, ppid=30, pgid=31)
        self.src.table[32] = P(32, ppid=31, pgid=31, start=(T0 + 32, 0))
        session_file(self.sessions, 32, T0 + 32, job_id="0000bbbb")
        self.lease()
        out = self.run_act("end-session", tok("end-session", self.root, self.root))
        order = [p for p, _ in self.src.signals]
        self.assertNotIn(32, order)
        self.assertLess(order.index(31), order.index(30))
        self.assertEqual(out["kept"], ["claude:0000bbbb"])

    def test_wrapper_is_never_signalled_while_its_child_runs(self):
        # The child ignores SIGTERM, and a disowned session shares its group:
        # the wrapper is never signalled, the session is never touched, and
        # both the child and the wrapper are reported.
        self.src.table[30] = P(30, ppid=20, pgid=20, comm="python3")
        self.src.table[31] = P(31, ppid=30, pgid=31)
        self.src.table[40] = P(40, ppid=1, pgid=31, start=(T0 + 40, 0))
        session_file(self.sessions, 40, T0 + 40, job_id="0000cccc")
        self.src.ignore_term = {31}
        self.lease()
        out = self.run_act("end-session", tok("end-session", self.root, self.root))
        signalled = {p for p, _ in self.src.signals}
        self.assertNotIn(30, signalled)
        self.assertNotIn(40, signalled)
        rows = {r["pid"]: r for r in out["remaining"]}
        self.assertTrue(rows[31]["forceable"])
        self.assertFalse(rows[30]["forceable"])
        self.assertNotIn(40, rows)

    def test_kept_lease_child_wrapper_is_never_signalled(self):
        # `memmon run -- claude` inside a session: the child is a nested
        # session, kept, so its wrapper is never signalled at all.
        self.src.table[30] = P(30, ppid=20, pgid=20, comm="python3")
        self.src.table[31] = P(31, ppid=30, pgid=31, start=(T0 + 31, 0))
        session_file(self.sessions, 31, T0 + 31, job_id="0000bbbb")
        self.src.table[32] = P(32, ppid=31, pgid=31)      # the kept session's own work
        self.lease()

        def born(pid, sig):                               # and more of it, mid-stop
            self.src.table.setdefault(33, P(33, ppid=31, pgid=31, start=(T0 + 33, 3)))
        self.src.on_kill = born
        out = self.run_act("end-session", tok("end-session", self.root, self.root))
        signalled = {p for p, _ in self.src.signals}
        self.assertFalse(signalled & {30, 31, 32, 33})
        self.assertEqual(out["kept"], ["claude:0000bbbb"])
        # Its group is not this stop's, so nothing in it is a survivor.
        self.assertEqual((out["result"], out["remaining"]), ("stopped", []))

    def test_unreadable_lease_child_holds_its_wrapper(self):
        self.src.table[30] = P(30, ppid=20, pgid=20, comm="python3")
        self.src.table[31] = P(31, ppid=30, pgid=31)
        self.lease()
        # The child became unreadable (its start can no longer be read).
        self.src.table[31] = P(31, ppid=30, pgid=31, visible=False, uid=0)
        self.src.table[31].start = None
        out = self.run_act("end-session", tok("end-session", self.root, self.root))
        self.assertNotEqual(out["result"], "error", out)
        self.assertNotIn(30, {p for p, _ in self.src.signals})
        # The wrapper is still alive and this stop's own: it is reported.
        self.assertEqual(out["result"], "partial", out)
        self.assertEqual([(r["pid"], r["role"]) for r in out["remaining"]], [(30, "runner")])

    def test_kept_owner_that_exited_is_not_reported_kept(self):
        self.src.table[30] = P(30, ppid=21, pgid=30, start=(T0 + 30, 0))
        session_file(self.sessions, 30, T0 + 30, job_id="0000bbbb")
        self.eng.after_capture = lambda captured: self.src.table.pop(30)
        out = self.run_act("end-session", tok("end-session", self.root, self.root))
        self.assertEqual(out["kept"], [])

    def test_job_root_gone_but_its_group_still_runs(self):
        token = self.job_token(target_pgid=20)
        del self.src.table[20]
        self.src.table[21].ppid = 1
        out = self.run_act("stop-job", token)
        self.assertEqual((out["result"], out["reason"], ma.exit_code(out)),
                         ("partial", "root_exited", 3))
        self.assertEqual(sorted(r["pid"] for r in out["remaining"]), [21, 22])
        self.assertNotIn("force_token", out)
        self.assertEqual(self.src.signals, [])
        del self.src.table[21], self.src.table[22]
        self.assertEqual(self.run_act("stop-job", token)["result"], "already_exited")

    def test_managed_job_root_gone_but_its_group_still_runs(self):
        # The same for a `memmon run` child that exited before the stop.
        token = tok("stop-managed-job", self.src.table[10], self.src.table[20],
                    target_pgid=20, run_id="run1")
        del self.src.table[20]
        self.src.table[21].ppid = 1
        out = self.run_act("stop-managed-job", token)
        self.assertEqual((out["result"], out["reason"]), ("partial", "root_exited"))
        self.assertEqual(self.src.signals, [])

    def test_unreadable_start_is_target_changed(self):
        # A reused PID now owned by another user reads no start time.
        token = self.job_token()
        self.src.table[20] = P(20, ppid=10, uid=0, visible=False)
        self.src.table[20].start = None
        self.assertEqual(self.run_act("stop-job", token)["reason"], "target_changed")

    def test_permission_error_keeps_the_survivor(self):
        def deny(pid, sig):
            if pid == 22:
                raise PermissionError(pid)
        self.src.on_kill = deny
        out = self.run_act("stop-job", self.job_token())
        self.assertEqual(out["result"], "partial")
        self.assertIn(22, [r["pid"] for r in out["remaining"]])

    def test_stop_managed_job_refuses_a_lease_with_another_child_start(self):
        child = self.src.table[21]
        self.eng.leases_fn = lambda: [{"id": "run1", "child_pid": 21, "child_start": [9, 9]}]
        out = self.run_act("stop-managed-job",
                           tok("stop-managed-job", self.root, child, run_id="run1"))
        self.assertEqual(out["reason"], "lease_mismatch")
        self.eng.leases_fn = lambda: [{"id": "run1", "child_pid": 21,
                                       "child_start": list(child.start)}]
        self.assertNotEqual(self.run_act("stop-managed-job", tok(
            "stop-managed-job", self.root, child, run_id="run1"))["reason"], "lease_mismatch")

    def test_reap_force_refuses_an_act_token(self):
        # `memmon reap --force` only takes tokens a reap minted.
        self.src.ignore_term = {21}
        out = self.run_act("stop-job", self.job_token())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = memmon.reap_cli(["--force", out["force_token"]], engine=self.eng)
        self.assertEqual(code, 4)
        self.assertIn("wrong_action", buf.getvalue())
        self.assertNotIn(signal.SIGKILL, [s for _, s in self.src.signals])

    def test_reap_spares_goes_through_the_engine(self):
        claim = os.path.join(self.state.root, "x.claim.sock")
        open(claim, "w").close()
        self.src.table[60] = P(60, start=(T0 - 5 * 3600, 0))
        self.src._argv[60] = ["claude", "bg-spare", "--bg-spare", claim]
        snap = {"overhead": {"items": [{"pid": 60, "mem": MB, "age": 5 * 3600,
                                        "stale": True}]}}
        dry = memmon.reap_spares(snap, False, engine=self.eng)
        self.assertIn("would send SIGTERM to 1", dry)
        self.assertEqual(self.src.signals, [])
        text, out = memmon.reap_spares_report(snap, True, engine=self.eng)
        self.assertEqual(out["result"], "stopped", text)
        self.assertEqual(self.src.signals, [(60, signal.SIGTERM)])

    def test_legacy_flags_exit_with_the_outcome(self):
        # main() wires --reap/--reap-spares/--end-session with --apply to the
        # outcome's exit code, and a dry run to 0.
        partial, refused = ma.outcome("partial", "survivors"), ma.outcome("refused", "x")
        cases = [(["--reap", "--apply"], "reap_report", partial, 3),
                 (["--reap"], "reap_report", partial, 0),
                 (["--reap-spares", "--apply"], "reap_spares_report", refused, 4),
                 (["--end-session", "10", "--apply"], "end_session_report", refused, 4),
                 (["--end-session", "10"], "end_session_report", refused, 0)]
        for argv, fn, out, code in cases:
            with mock.patch.object(sys, "argv", ["memmon", *argv]), \
                    mock.patch.object(memmon, "collect", return_value={}), \
                    mock.patch.object(memmon, fn, return_value=("text", out)), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(memmon.main(), code, argv)

    def test_claimed_spare_survives_every_force_path(self):
        # A stale spare survives the reap; before Force it is claimed (its
        # claim socket is gone) but not yet attributed (its sessions record
        # still says spare). Only reap's own force, with its selector, may
        # act on a reap token, and it must leave the spare alone.
        claim = os.path.join(self.state.root, "x.claim.sock")
        open(claim, "w").close()
        self.src.table[60] = P(60, start=(T0 - 5 * 3600, 0))
        self.src._argv[60] = ["claude", "bg-spare", "--bg-spare", claim]
        self.src.ignore_term = {60}
        with open(os.path.join(self.sessions, "60.json"), "w") as fh:
            json.dump({"pid": 60, "spare": True}, fh)
        snap = {"overhead": {"items": [{"pid": 60, "mem": MB, "age": 5 * 3600,
                                        "stale": True}]}}
        _, out = memmon.reap_spares_report(snap, True, engine=self.eng)
        self.assertEqual(out["result"], "partial", out)
        token = out["force_token"]
        os.remove(claim)                                   # claimed since
        for argv in (["force", "--target", token], ["--force", token]):
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                self.assertEqual(memmon.act_cli(argv, engine=self.eng), 4)
            self.assertEqual(json.loads(buf.getvalue())["reason"], "wrong_action")
        self.assertEqual(self.eng.force(token, origin="reap")["result"], "partial")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(memmon.reap_cli(["--force", token], engine=self.eng), 3)
        self.assertNotIn(signal.SIGKILL, [s for _, s in self.src.signals])

    def test_reap_dry_run_matches_apply(self):
        # The dry run lists what --apply would signal (descendants too)
        # and what it would refuse, and signals nothing.
        self.src.table[80] = P(80, start=(T0 - 7200, 0), comm="node")
        self.src.table[81] = P(81, ppid=80)
        self.src._argv[80] = ["node", "/r/node_modules/.bin/vitest"]
        snap = {"orphans": [{"pid": 80, "mem": MB, "age": 7200, "orphaned": True,
                             "tag": "vitest", "worktree": ""},
                            {"pid": 20, "mem": MB, "age": 7200, "orphaned": False,
                             "tag": "vitest", "worktree": ""}], "orphan_total": 2 * MB}
        self.src.table[82] = P(82, ppid=80, start=(T0 + 82, 0))     # a nested session
        session_file(self.sessions, 82, T0 + 82, job_id="0000ffff")
        # A dry run only reads: it works while another action holds the lock.
        with ma.action_lock(memmon.ACTIONS_LOCK):
            text, out = memmon.reap_report(snap, False, engine=self.eng)
        self.assertEqual(out["result"], "would_stop")
        self.assertEqual([r["pid"] for r in out["would_signal"]], [80, 81])
        self.assertEqual(out["kept"], ["claude:0000ffff"])
        self.assertEqual(out["refused"], [{"pid": 20, "reason": "attributed"}])
        self.assertIn("would keep running: claude:0000ffff", text)
        self.assertIn("left alone: pid 20 (attributed)", text)
        self.assertIn("dry run — re-run with --apply", text)
        self.assertEqual(self.src.signals, [])
        # Nothing it would stop: the refusals are listed, with no footer.
        text, out = memmon.reap_report({**snap, "orphans": snap["orphans"][1:]}, False,
                                       engine=self.eng)
        self.assertEqual(out["result"], "refused")
        self.assertIn("left alone: pid 20 (attributed)", text)
        self.assertNotIn("re-run with --apply", text)

    def test_reap_report_binds_the_identity_it_read(self):
        # The identity read when the report starts is the one the engine
        # checks: a PID reused after that read is refused, not signalled.
        self.src.table[80] = P(80, start=(T0 - 7200, 0), comm="node")
        self.src._argv[80] = ["node", "/r/node_modules/.bin/vitest"]
        real, reads = self.src.read, []

        def read(pid):
            reads.append(pid)
            p = real(pid)
            if pid == 80 and reads.count(80) == 1:      # right after the report's read
                self.src.table[80] = P(80, start=(T0 - 3700, 9), comm="node")
            return p
        self.src.read = read
        snap = {"orphans": [{"pid": 80, "mem": MB, "age": 7200, "orphaned": True,
                             "tag": "vitest", "worktree": ""}], "orphan_total": MB}
        text, out = memmon.reap_report(snap, True, engine=self.eng)
        self.assertEqual(out["refused"], [{"pid": 80, "reason": "target_changed"}], text)
        self.assertEqual(self.src.signals, [])

    def test_reap_dry_run_lists_held_wrappers(self):
        self.src.table[80] = P(80, start=(T0 - 7200, 0), comm="node")
        self.src._argv[80] = ["node", "/r/node_modules/.bin/vitest"]
        self.src.table[83] = P(83, ppid=80, comm="python3")
        self.src.table[84] = P(84, ppid=83, pgid=84)
        self.eng.leases_fn = lambda: [{"id": "r", "state": "running", "wrapper_pid": 83,
                                       "child_pid": 84,
                                       "child_start": list(self.src.table[84].start)}]
        snap = {"orphans": [{"pid": 80, "mem": MB, "age": 7200, "orphaned": True,
                             "tag": "vitest", "worktree": ""}], "orphan_total": MB}
        text, out = memmon.reap_report(snap, False, engine=self.eng)
        self.assertEqual([r["pid"] for r in out["would_signal"]], [80, 84])
        self.assertEqual([r["pid"] for r in out["would_hold"]], [83])
        self.assertIn("(memmon run wrapper: would stop once its job ends)", text)

    def test_reap_refuses_a_listed_pid_reused_since(self):
        self.src.table[80] = P(80, start=(T0 - 7200, 0), comm="node")
        self.src._argv[80] = ["node", "/r/node_modules/.bin/vitest"]
        listed = list(self.src.table[80].start)
        self.src.table[80] = P(80, start=(T0 - 3700, 5), comm="node")
        self.src._argv[80] = ["node", "/r/node_modules/.bin/vitest"]
        out = self.eng.reap([(80, listed)], memmon._orphan_still_selected)
        self.assertEqual(out["refused"], [{"pid": 80, "reason": "target_changed"}])
        self.assertEqual(self.src.signals, [])

    def test_reap_and_end_session_exit_with_the_outcome(self):
        # Applying exits 0 done / 3 partial / 4 refused; a dry run exits 0.
        self.src.table[80] = P(80, start=(T0 - 7200, 0), comm="node")
        self.src._argv[80] = ["node", "/r/node_modules/.bin/vitest"]
        self.src.ignore_term = {80}
        orphan = {"pid": 80, "mem": MB, "age": 7200, "orphaned": True,
                  "tag": "vitest", "worktree": ""}
        attributed = {**orphan, "pid": 20, "orphaned": False}

        def reap(args, *rows):
            snap = {"orphans": list(rows), "orphan_total": MB}
            with mock.patch.object(memmon, "collect", return_value=snap), \
                    contextlib.redirect_stdout(io.StringIO()):
                return memmon.reap_cli(args, engine=self.eng)
        self.assertEqual(reap([], orphan), 0)
        self.assertEqual(self.src.signals, [])
        self.assertEqual(reap(["--apply"], attributed), 4)
        self.assertEqual(reap(["--apply"], orphan), 3)
        self.assertEqual(memmon._apply_exit(memmon.end_session_report(20, True, self.eng)[1],
                                            True), 4)
        self.assertEqual(memmon._apply_exit(memmon.end_session_report(10, False, self.eng)[1],
                                            False), 0)

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


class RespawnTests(unittest.TestCase):
    """The daemon revives a worker ended mid-turn. Fixture from the probe;
    no real Claude process is involved."""

    JOB = "a1b2c3d4"

    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.close)
        self.sessions = memmon.CLAUDE_SESSIONS_DIR
        self.jobs = memmon.JOBS_DIR
        self.roster = memmon.CLAUDE_ROSTER
        os.makedirs(os.path.join(self.jobs, self.JOB))
        os.makedirs(os.path.dirname(self.roster))
        self.ctx = mo.Context(sessions_dir=self.sessions, jobs_dir=self.jobs,
                              roster_path=self.roster)
        self.clock = FakeClock()

    def set_state(self, state):
        with open(os.path.join(self.jobs, self.JOB, "state.json"), "w") as fh:
            json.dump({"state": state}, fh)

    def set_roster(self, entry):
        with open(self.roster, "w") as fh:
            json.dump({"workers": {self.JOB: entry} if entry else {}}, fh)

    def fixture(self, busy=True):
        procs = [P(os.getpid(), ppid=0, start=(T0 + 4, 0)),       # act began 1 s ago
                 P(40000, start=(1000, 0)), P(41000, ppid=40000, start=(2000, 0)),
                 P(41001, ppid=41000, start=(2000, 100000)),
                 P(41002, ppid=41001, pgid=41001, start=(2001, 0))]
        if busy:
            procs += [P(41010, ppid=41001, start=(2100, 0)),
                      P(41011, ppid=41010, pgid=41010, start=(2100, 200000))]
        session_file(self.sessions, 41001, 2000, job_id=self.JOB,
                     status="busy" if busy else "idle")
        self.set_roster({"pid": 41000, "attempt": 1})
        self.set_state("working" if busy else "done")
        self.src = FakeSource(procs)
        self.eng = fake_engine(self.src, self.ctx, memmon.ACTIONS_LOCK, clock=lambda: T0 + 5,
                               mono=self.clock.mono, sleep=self.clock.sleep, poll_s=0.5,
                               respawn=mo.RespawnWatch(self.ctx))
        self.token = tok("end-session", self.src.table[41001], self.src.table[41001])

    def crash_on_term(self):
        def on_kill(pid, sig):
            if pid == 41001:
                os.remove(os.path.join(self.sessions, "41001.json"))
                self.set_state("crashed")
                self.t_term = self.clock.t
        self.src.on_kill = on_kill

    def test_busy_worker_respawn_is_reported(self):
        self.fixture(busy=True)
        self.crash_on_term()

        def revive():
            self.src.table[42000] = P(42000, ppid=40000, start=(2212, 0))
            self.src.table[42001] = P(42001, ppid=42000, start=(2212, 400000))
            session_file(self.sessions, 42001, 2212, job_id=self.JOB, status="busy")
            self.set_roster({"pid": 42000, "attempt": 2})
            self.set_state("running")
        self.clock.at(self.clock.t + 12.5, revive)
        out = self.eng.run("end-session", self.token)
        self.assertEqual((out["result"], ma.exit_code(out)), ("respawned", 3))
        self.assertEqual(out["reason"], "daemon_restarted_worker")
        self.assertEqual(out["exited"], 4)
        self.assertEqual(out["respawned_as"], {"pid": 42001, "start": [2212, 400000]})
        self.assertNotIn("force_token", out)
        self.assertEqual({p for p, _ in self.src.signals}, {41001, 41002, 41010, 41011})
        self.assertLessEqual(self.clock.t - 1000, 22)
        # The old token now names a process that is gone: nothing is signalled.
        again = self.eng.run("end-session", self.token)
        self.assertEqual(again["result"], "already_exited")
        self.assertEqual({p for p, _ in self.src.signals}, {41001, 41002, 41010, 41011})

    def test_respawn_watch_runs_without_the_actions_lock(self):
        # Another action during the 20 s watch is served, not busy.
        self.fixture(busy=True)
        self.crash_on_term()
        other_clock = FakeClock()
        other = fake_engine(self.src, self.ctx, memmon.ACTIONS_LOCK, clock=lambda: T0 + 5,
                            mono=other_clock.mono, sleep=other_clock.sleep)
        seen = []
        daemon = self.src.table[40000]
        self.clock.at(self.clock.t + 5, lambda: seen.append(
            other.run("end-session", tok("end-session", daemon, daemon))))
        out = self.eng.run("end-session", self.token)
        self.assertEqual(out["result"], "stopped")
        self.assertEqual(len(seen), 1)
        self.assertEqual((seen[0]["result"], seen[0]["reason"]), ("refused", "not_stoppable"))

    def test_respawn_watch_respects_the_act_budget(self):
        # The whole act stays inside 22 s from its own process start.
        self.fixture(busy=True)
        self.src.table[os.getpid()].start = (T0 + 5 - 10, 0)    # started 10 s ago
        self.crash_on_term()
        self.eng.run("end-session", self.token)
        self.assertLessEqual(self.clock.t - 1000, 12.6)

    def test_respawn_watch_error_keeps_the_result(self):
        self.fixture(busy=True)

        class Broken:
            def baseline(self, job):
                return {}

            def status(self, *a):
                raise OSError("roster unreadable")
        self.eng.respawn = Broken()
        out = self.eng.run("end-session", self.token)
        self.assertEqual(out["result"], "stopped")
        self.assertIn("roster unreadable", out["watch_error"])
        self.assertIn("the respawn watch stopped early (OSError: roster unreadable)",
                      memmon._stop_report(out, "end"))

    def test_respawn_idle_worker_settles_early(self):
        self.fixture(busy=False)
        out = self.eng.run("end-session", self.token)
        self.assertEqual((out["result"], ma.exit_code(out)), ("stopped", 0))
        self.assertLess(self.clock.t - 1000, 5)        # did not sit out the window

    def test_respawn_roster_removal_settles(self):
        self.fixture(busy=True)
        self.crash_on_term()
        self.clock.at(self.clock.t + 12, lambda: self.set_roster(None))
        out = self.eng.run("end-session", self.token)
        self.assertEqual(out["result"], "stopped")
        self.assertLess(self.clock.t - 1000, 14)

    def test_respawn_spare_for_another_job_is_not_a_respawn(self):
        self.fixture(busy=True)
        self.crash_on_term()

        def spare():
            self.src.table[43001] = P(43001, ppid=40000, start=(2205, 0))
            session_file(self.sessions, 43001, 2205, job_id="ffff0000", spare=True)
        self.clock.at(self.clock.t + 5, spare)
        out = self.eng.run("end-session", self.token)
        self.assertEqual(out["result"], "stopped")
        self.assertGreaterEqual(self.clock.t - 1000, 20)   # watched the whole window
        self.assertLessEqual(self.clock.t - 1000, 22)
        self.assertNotIn(43001, {p for p, _ in self.src.signals})


class CodexEndSessionTests(unittest.TestCase):
    """An in-process TUI is ended (usually ignoring TERM); a daemon
    frontend is refused."""

    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.close)
        u2 = "22222222-2222-4222-8222-222222222222"
        self.ctx = mo.Context(sessions_dir=memmon.CLAUDE_SESSIONS_DIR, lsof=fake_lsof({
            50001: [("unix", "0xd0d0", "/private/tmp/codex-daemon-501/aaaa")],
            51000: [("unix", "0xc1c1", "->0xd0d0")],
            52000: [("REG", "0x1", f"/h/.codex/thread-writer-locks/{u2}.lock")]}))
        procs = [P(os.getpid(), ppid=0), P(50000, comm="codex"),
                 P(50001, ppid=50000, comm="codex"),
                 P(51000, comm="codex"), P(52000, comm="codex", start=(3000, 0)),
                 P(52001, ppid=52000), P(52002, ppid=52000)]
        argv = {50000: ["codex", "app-server", "daemon", "pid-update-loop"],
                50001: ["codex", "app-server", "--listen", "unix://"],
                51000: ["codex", "--model", "gpt-fixture"],
                52000: ["codex", "--disable", "daemon_auto_start"]}
        self.src = FakeSource(procs, argv=argv, ignore_term={52000})

        def children_die_with_parent(pid, sig):
            if pid == 52000 and sig == signal.SIGKILL:
                for c in (52001, 52002):
                    self.src.table.pop(c, None)
        self.src.on_kill = children_die_with_parent
        self.eng = fake_engine(self.src, self.ctx, memmon.ACTIONS_LOCK, clock=lambda: T0 + 5)

    def test_r3_in_process_tui_ends_partial_then_force(self):
        t = self.src.table[52000]
        out = self.eng.run("end-session", tok("end-session", t, t))
        self.assertEqual((out["result"], ma.exit_code(out)), ("partial", 3))
        self.assertEqual([r["pid"] for r in out["remaining"]], [52000])
        body = mo.decode_token(out["force_token"])
        self.assertEqual([(v["pid"], v["start"]) for v in body["survivors"]], [(52000, [3000, 0])])
        forced = self.eng.run("force", out["force_token"])
        self.assertEqual(forced["result"], "force_stopped")
        self.assertEqual([s for s in self.src.signals if s[1] == signal.SIGKILL],
                         [(52000, signal.SIGKILL)])

    def test_r3_daemon_frontend_is_refused(self):
        t = self.src.table[51000]
        out = self.eng.run("end-session", tok("end-session", t, t))
        self.assertEqual((out["result"], out["reason"]), ("refused", "not_stoppable"))
        self.assertEqual(self.src.signals, [])


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

    def test_verify_app_hands_out_a_fresh_token_for_the_recheck(self):
        # Each answer carries a token minted now; on that re-check a reused
        # PID means the named instance exited, not that the call is refused.
        owners_token = self.token()
        first = self.eng.run("verify-app", owners_token)
        body = mo.decode_token(first["token"])
        self.assertEqual((body["action"], body["checked"], body["snapshot_ts"]),
                         ("quit-app", True, T0 + 1))
        self.assertEqual(body["instances"], mo.decode_token(owners_token)["instances"])
        self.src.table[70] = P(70, start=(T0 + 70, 70))           # quit, PID reused
        again = self.eng.run("verify-app", first["token"])
        self.assertEqual(again["result"], "verified", again)
        self.assertEqual([i["status"] for i in again["instances"]], ["running", "exited"])
        self.assertIn("token", again)
        self.src.table.pop(60)
        last = self.eng.run("verify-app", again["token"])
        self.assertEqual((last["result"], [i["status"] for i in last["instances"]]),
                         ("already_exited", ["exited", "exited"]))
        # The first check still refuses a changed instance outright.
        self.src.table[60] = P(60, start=(T0 + 60, 60))
        self.assertEqual(self.eng.run("verify-app", owners_token)["reason"], "instance_changed")

    def test_verify_app_unreadable_start_is_instance_changed(self):
        token = self.token()
        self.src.table[70] = P(70, visible=False, uid=0)
        self.src.table[70].start = None
        self.assertEqual(self.eng.run("verify-app", token)["reason"], "instance_changed")

    def test_recheck_of_an_unreadable_instance_is_unverified(self):
        # After the quit the PID is held by something memmon cannot read:
        # that is not proof the named instance exited.
        first = self.eng.run("verify-app", self.token())
        self.src.table.pop(60)
        self.src.table[70] = P(70, visible=False, uid=0)
        self.src.table[70].start = None
        again = self.eng.run("verify-app", first["token"])
        self.assertEqual([i["status"] for i in again["instances"]], ["exited", "unverified"])
        self.assertEqual(again["result"], "verified")

    def test_verify_app_refuses_a_helper_as_an_instance(self):
        helper = os.path.join(self.state.root, "Applications", "Brave Browser.app",
                              "Contents", "Frameworks", "Brave Helper.app", "Contents",
                              "MacOS", "Brave Helper")
        self.src._paths[70] = helper
        self.assertEqual(self.eng.run("verify-app", self.token())["reason"], "instance_changed")

    def test_verify_app_on_a_code_sign_clone_main(self):
        clone = os.path.join(self.state.root, "X", "com.example.brave.code_sign_clone",
                             "code_sign_clone.r", "Brave Browser.app.bundle")
        os.makedirs(os.path.join(clone, "Contents"))
        with open(os.path.join(clone, "Contents", "Info.plist"), "wb") as fh:
            plistlib.dump({"CFBundleIdentifier": "com.example.brave",
                           "CFBundleExecutable": "Brave Browser"}, fh)
        self.src._paths[70] = f"{clone}/Contents/MacOS/Brave Browser"
        out = self.eng.run("verify-app", self.token())
        self.assertEqual(out["result"], "verified", out)

    def test_verify_app_refuses_an_app_hosting_a_session(self):
        self.src.table[71] = P(71, ppid=70, comm="zsh")
        self.src.table[72] = P(72, ppid=71, start=(T0 + 72, 0))
        session_file(memmon.CLAUDE_SESSIONS_DIR, 72, T0 + 72, job_id="0000ee02")
        self.assertEqual(self.eng.run("verify-app", self.token())["reason"], "hosts_sessions")

    def test_verify_app_all_gone(self):
        token = self.token()
        del self.src.table[60], self.src.table[70]
        self.assertEqual(self.eng.run("verify-app", token)["result"], "already_exited")


class PolicyTests(unittest.TestCase):
    def test_policy_never_autostops_unmanaged(self):
        # Viewing, sampling and gating never stop anything, however idle,
        # heavy or orphaned a process looks.
        state = TempState()
        self.addCleanup(state.close)
        ctx = mo.Context(sessions_dir=memmon.CLAUDE_SESSIONS_DIR)
        src = FakeSource([P(os.getpid(), ppid=0), P(10, comm="node"), P(11, ppid=10)],
                         argv={10: ["node", "/r/node_modules/.bin/vitest"]})
        boom = mock.Mock(side_effect=AssertionError("signal sent"))
        with mock.patch.object(os, "kill", boom), mock.patch.object(os, "killpg", boom), \
                mock.patch.object(ma.Engine, "_signal", boom), \
                mock.patch.object(memmon, "gate_stats", return_value={}), \
                mock.patch.object(memmon, "system_block", return_value={}):
            memmon.owners_json(0, source=src, ctx=ctx)
            memmon.owners_sampler_tick(src, ctx)
            memmon.gate_decision("Bash", "pnpm typecheck",
                                 {"level": "CRITICAL", "reasons": ["x"]}, {}, "block")
        boom.assert_not_called()


SIGNAL_SITE = re.compile(
    r"\bos\.kill\s*\(|\bkillpg\s*\(|\bself\.kill\s*\(|\.kill\s*\(|\.terminate\s*\("
    r"|\bsend_signal\s*\(|\bpthread_kill\s*\(|(?<![\"'])\b(?:pkill|killall)\b(?![\"'])"
    r"|\[\s*[\"'](?:kill|pkill|killall)[\"']"           # argv lists
    r"|[\"'](?:kill|pkill|killall)\s+-?\w"               # shell strings: os.system, shell=True
    r"|(^|[;&|\s])kill\s+-"                              # shell scripts
    r"|\bfrom\s+os\s+import\b.*\b(?:kill|killpg)\b"
    r"|\bgetattr\s*\(\s*os\s*,\s*[\"']kill")
BAR = 'pkill {}-f "MemmonBar.app/Contents/MacOS/MemmonBar" 2>/dev/null || true'


class SignalSiteTests(unittest.TestCase):
    def test_act_no_killpg(self):
        # The engine and everything it calls signal per PID only.
        for name in ("memmon_act.py", "memmon_owners.py", "memmon_procs.py"):
            with open(os.path.join(HERE, name)) as fh:
                self.assertNotRegex(fh.read(), r"killpg\s*\(", name)

    def test_only_known_code_sends_signals(self):
        # Every shipped .py/.sh signal site, exactly: the engine's _signal
        # (through its injected kill), the runner's _stop (its own child's
        # group) and install.sh stopping the menu bar app it installs. Tests
        # signal only what they spawned and are excluded here.
        from collections import Counter
        from test_owners import tracked_files
        hits = Counter()                    # (file, function, line) -> sites
        for path in tracked_files(self):
            name = os.path.basename(path)
            if not name.endswith((".py", ".sh")) or name.startswith("test") or \
                    name == "testkit.py":
                continue
            with open(path) as fh:
                lines = fh.read().splitlines()
            func = None
            for line in lines:
                m = re.match(r"\s*def (\w+)", line)
                if m:
                    func = m.group(1)
                if SIGNAL_SITE.search(line.split("#")[0]):
                    hits[(name, func if name.endswith(".py") else None, line.strip())] += 1
        self.assertEqual(hits, Counter({
            ("memmon_act.py", "_signal", "self.kill(pid, sig)"): 1,
            ("memmon_runner.py", "_stop", "os.killpg(child.pid, signum)"): 1,
            ("memmon_runner.py", "_stop", "os.killpg(child.pid, signal.SIGKILL)"): 1,
            ("install.sh", None, BAR.format("")): 2,          # uninstall and reinstall
            ("install.sh", None, BAR.format("-9 ")): 1}))

    def test_timing_constants_match_the_menu_bar(self):
        # The menu bar waits actTimeout for an act call that the engine keeps
        # under ACT_BUDGET_S, and expires a Force offer when the token does.
        with open(os.path.join(HERE, "MemmonBar.swift")) as fh:
            swift = fh.read()
        num = lambda name: float(re.search(rf"static (?:var|let) {name} = ([0-9.]+)",
                                           swift).group(1))
        self.assertLess(ma.ACT_BUDGET_S, num("actTimeout"))
        self.assertEqual(ma.TOKEN_TTL_S, num("forceTTL"))

    # Refusals memmon emits that the menu bar has no copy for yet.
    PENDING_SWIFT_COPY = set()

    def test_every_refusal_reason_has_menu_bar_copy(self):
        # Every emitter, not only the engine: memmon.py refuses too.
        reasons = set()
        for name in ("memmon_act.py", "memmon.py"):
            with open(os.path.join(HERE, name)) as fh:
                src = fh.read()
            reasons |= set(re.findall(r'Refused\("([a-z_]+)"\)', src))
            reasons |= set(re.findall(r'outcome\("refused",\s*"([a-z_]+)"', src))
            reasons |= set(re.findall(r'"reason": "([a-z_]+)"', src))
        self.assertTrue({"bad_args", "hosts_sessions", "attributed"} <= reasons, reasons)
        with open(os.path.join(HERE, "MemmonBar.swift")) as fh:
            swift = fh.read()
        body = swift[swift.index("static func refusal("):]
        body = body[:body.index("default:")]
        cases = set(re.findall(r'"([a-z_]+)"', body))
        self.assertLessEqual(reasons - cases, self.PENDING_SWIFT_COPY)

    def test_signal_site_pattern_catches_every_spelling(self):
        for line in ("os.kill(p, 9)", "os.killpg(g, 15)", "self.kill(pid, sig)",
                     "proc.kill()", "proc.terminate()", "p.send_signal(15)",
                     "signal.pthread_kill(t, 15)", "pkill -f x", "killall Dock",
                     'subprocess.run(["kill", "-9", pid])', "kill -TERM $pid",
                     'subprocess.run(["pkill", "-f", x])', 'os.system("kill -9 %d" % pid)',
                     'subprocess.run("killall Dock", shell=True)', "from os import kill",
                     "from os import getpid, killpg", 'getattr(os, "kill")(pid, 9)'):
            self.assertRegex(line, SIGNAL_SITE)
        for line in ('words = {"kill", "pgrep", "pkill"}', '"skill level"', "kill_switch = 1"):
            self.assertNotRegex(line, SIGNAL_SITE)


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
        "import os; exec(os.environ['MEMMON_NOTE']); note('sleeper'); import time; time.sleep(120)",
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
    # The parent records the child: the stop may TERM it before it could.
    gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    note_child("gc", gc.pid)
    time.sleep(0.8)
    os._exit(0)
signal.signal(signal.SIGTERM, on_term)
note("build")
time.sleep(120)
"""

BUILD_DOUBLE_FORK = NOTE + """
import subprocess, sys, time
note("build")
GC = "import os; exec(os.environ['MEMMON_NOTE']); note('gc'); import time; time.sleep(120)"
subprocess.Popen([sys.executable, "-c",
    "import subprocess, sys; subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]])",
    sys.argv[1], GC]).wait()
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
code = ("import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "exec(os.environ['MEMMON_NOTE']); note('orphan'); time.sleep(120) # vitest")
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
        sleepers = [pid for tag, pid, _ in self.reg.read_notes() if tag == "sleeper"]
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

    def test_registry_binds_pid_and_start(self):
        # The harness records an identity once and covers a PID only while
        # that same identity holds it, so a reused PID is never signalled.
        pid = self.reg.spawn(SLEEP).pid
        real = self.reg.ids[pid]
        self.reg.register(pid, (1, 1))
        self.assertEqual(self.reg.ids[pid], real)
        self.assertTrue(self.reg._covered(pid))
        self.reg.ids[pid] = (1, 1)                 # as if the PID now held another process
        try:
            self.assertFalse(self.reg._covered(pid))
            with self.assertRaises(AssertionError):
                self.reg.guarded_kill(pid, 0)
        finally:
            self.reg.ids[pid] = real

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
        child = self.reg.register(row["child_pid"], row["child_start"])
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
        late = [p for t, p, _ in self.reg.read_notes() if t == "orphan" and p != orphan][0]
        out = self.eng.force(force, origin="reap", still_selected=memmon._orphan_still_selected)
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

    def test_cli_bad_args_and_reap_force_dispatch(self):
        env = dict(os.environ, HOME=self.home.name)
        run = lambda *a: subprocess.run([PY, os.path.join(HERE, "memmon.py"), *a],
                                        capture_output=True, text=True, timeout=60, env=env)
        proc = run("act", "stop-job")
        self.assertEqual(proc.returncode, 4)
        self.assertEqual(json.loads(proc.stdout)["reason"], "bad_args")
        proc = run("act", "teleport", "--target", "x")
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["reason"]), (4, "bad_args"))
        proc = run("reap", "--force", "not-a-token")
        self.assertEqual(proc.returncode, 4)
        self.assertIn("refused: bad_token", proc.stdout)

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
