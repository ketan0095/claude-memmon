#!/usr/bin/env python3
"""Unmanaged heavy work under pressure (spec S2.11): the trigger, the
candidate filter, job history, ranking, dedupe, the osascript argv and
never-signal. Inventories are scripted (FakeSource) except B40, whose only
real processes are registered synthetic sleepers."""

from __future__ import annotations

import io
import json
import os
import signal
import time
import unittest
from unittest import mock

import memmon
import memmon_act as ma
import memmon_owners as mo
import memmon_pressure as mpx
import memmon_procs as mp
from test_sampler import BOOT, SamplerState
from testkit import FakeSource, P, Registry, fake_engine, session_file

GB = 1 << 30
MB = 1 << 20
T0 = 1_791_400_000
RAM = 48 * GB


def classify(cmd):
    return memmon.classify_command(cmd, {})


def kind_of():
    return mpx.Classifier(classify, memmon.job_tokens)


def node(script, *args):
    return ["/opt/homebrew/bin/node", "--max-old-space-size=8192",
            f"/r/acme-web/node_modules/{script}", *args]


class Scene:
    """The B38 inventory: one of each candidate the spec names."""

    def __init__(self, tc, sessions, without=(), extra=()):
        me = os.getpid()
        procs = [P(me, ppid=0, comm="Python"),
                 # A Claude session (5 GB itself) with a 9 GB vitest tool job.
                 P(10, start=(T0 + 10, 0), comm="2.1.293", fp=5 * GB),
                 P(11, ppid=10, pgid=11, comm="zsh", fp=10 * MB),
                 P(12, ppid=11, pgid=11, comm="node", fp=9 * GB),
                 # An unattributed terminal shell running a 3 GB vite server.
                 P(20, comm="zsh", fp=5 * MB),
                 P(21, ppid=20, pgid=21, comm="node", fp=3 * GB),
                 # Reparented roots: a 2 GB vitest and a 1.2 GB vite.
                 P(30, comm="node", fp=2 * GB),
                 P(40, comm="node", fp=int(1.2 * GB)),
                 # A tmux server owner with a 1.5 GB vitest grandchild.
                 P(50, comm="tmux", fp=20 * MB),
                 P(51, ppid=50, pgid=51, comm="zsh", fp=5 * MB),
                 P(52, ppid=51, pgid=52, comm="node", fp=int(1.5 * GB)),
                 # A 0.5 GB tsc, a 6 GB managed lease, another uid's 10 GB.
                 P(60, comm="node", fp=GB // 2),
                 P(70, comm="node", fp=6 * GB),
                 P(80, comm="node", fp=10 * GB, uid=0)]
        argv = {me: ["/usr/bin/python3", "memmon.py", "owners", "--json"],
                10: ["claude"], 11: ["/bin/zsh", "-c", "pnpm vitest run"],
                12: node("vitest/vitest.mjs", "run"), 20: ["-zsh"],
                21: node("vite/bin/vite.js", "dev"), 30: node("vitest/vitest.mjs", "run"),
                40: node("vite/bin/vite.js", "dev"), 50: ["tmux"], 51: ["-zsh"],
                52: node("vitest/vitest.mjs", "run"), 60: node("typescript/bin/tsc", "-b"),
                70: node("vitest/vitest.mjs", "run"), 80: node("vitest/vitest.mjs", "run")}
        procs = [p for p in procs if p.pid not in without] + list(extra)
        for p in extra:
            argv.setdefault(p.pid, getattr(p, "_argv", [p.comm]))
        session_file(sessions, 10, T0 + 10, job_id="0000a001")
        cwds = {pid: "/r/acme-web" for pid in (11, 12, 21, 52, 91)}
        self.src = FakeSource(procs, argv=argv, cwds=cwds)
        self.inv = mp.snapshot(self.src, clock=lambda: T0 + 5000, mono=lambda: 10**12)
        lease_start = list(self.src.table[70].start) if 70 in self.src.table else None
        self.leases = [{"id": "r1", "child_pid": 70, "child_start": lease_start,
                        "resource": "heavy"}] if lease_start else []
        self.ctx = mo.Context(sessions_dir=sessions, classify=classify,
                              commands=memmon.job_tokens, leases=self.leases)
        self.part = mo.partition(self.inv, self.ctx)

    def s1_jobs(self):
        payload = mo.owners_payload(mo.Sample(self.inv, self.part, {}, "warming up"),
                                    self.ctx, now=self.inv.ts)
        return {j["job_id"]: j for o in payload["owners"] for j in o["jobs"]
                if j["kind"] != "conversation"}

    def suggest(self, **kw):
        kw.setdefault("leases", self.leases)
        kw.setdefault("ram_bytes", RAM)
        return mpx.suggestions(self.inv, self.part, kind_of(), **kw)


class TriggerTests(unittest.TestCase):
    def test_under_pressure_predicate(self):
        # B37's truth table, one predicate for suggestions, episode and gate.
        rates = {"swapin_mbs": 0.0}
        cases = [("HEALTHY", rates, "critical", False), ("WATCH", rates, "critical", False),
                 ("DANGER", rates, "normal", True), ("CRITICAL", rates, "normal", True),
                 ("UNKNOWN", "unavailable", "normal", False),
                 ("UNKNOWN", "unavailable", "warning", True),
                 ("UNKNOWN", "unavailable", "critical", True),
                 ("WATCH", "unavailable", "critical", True),       # failed vm_stat
                 ("WATCH", "unavailable", "normal", False),
                 ("UNKNOWN", "unavailable", None, False)]
        for level, r, kern, want in cases:
            with self.subTest(level=level, rates=r, kernel=kern):
                self.assertIs(mpx.under_pressure(level, r, kern), want)


class StripTests(unittest.TestCase):
    def cmd(self, argv):
        src = FakeSource([P(5, comm=os.path.basename(argv[0]))], argv={5: argv})
        return mpx.heavy_command(mp.snapshot(src), 5)

    def test_runtime_and_script_extension_are_stripped(self):
        self.assertEqual(self.cmd(node("vitest/vitest.mjs", "run")), "vitest run")
        self.assertEqual(self.cmd(node("typescript/bin/tsc", "-b")), "tsc -b")
        self.assertEqual(self.cmd(["node", "/r/x/forks.js"]), "forks")
        self.assertEqual(self.cmd(["bun", "--smol", "/r/x/build.cjs"]), "build")
        self.assertEqual(self.cmd(["/bin/zsh", "-c", "pnpm vitest run"]), "pnpm vitest run")
        self.assertEqual(self.cmd(["node", "-e", "1"]), "node -e 1")

    def test_classifies_through_s1_classify_job(self):
        k = kind_of()
        src = FakeSource([P(5, comm="node"), P(6, comm="node")],
                         argv={5: node("vitest/vitest.mjs", "run"),
                               6: node("typescript/bin/tsc", "-b")})
        inv = mp.snapshot(src)
        self.assertEqual(k(inv, 5), ("test", "vitest"))
        self.assertEqual(k(inv, 6), ("build", "tsc"))
        # S1's own process_command is unchanged: it keeps the extension.
        self.assertEqual(mo.process_command(inv, 5), "vitest.mjs run")


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.st = SamplerState(self)
        self.sessions = memmon.CLAUDE_SESSIONS_DIR

    def test_candidates_ranked_capped_and_stoppable_as_s1_allows(self):
        # B38, first run
        sc = Scene(self, self.sessions)
        s1 = sc.s1_jobs()
        rows = sc.suggest(s1_jobs=s1, mint=True)
        self.assertEqual([r["root"]["pid"] for r in rows], [11, 21, 30])
        vitest, vite, orphan = rows
        self.assertEqual((vitest["kind"], vitest["stop"]), ("test", "stop-job"))
        self.assertEqual(vitest["token"], s1[vitest["job_id"]]["token"])   # S1's token
        self.assertEqual(vitest["owner_id"], "claude:0000a001")
        self.assertEqual((vite["kind"], vite["stop"]), ("server", "stop-server"))
        body = mo.decode_token(vite["token"])
        self.assertEqual(body["action"], "stop-server")
        self.assertEqual(body["owner_root"]["pid"], 20)
        self.assertEqual(body["target"], {"pid": 21, "start": list(sc.src.table[21].start)})
        self.assertEqual(body["target_pgid"], 21)
        self.assertEqual((orphan["stop"], orphan["stop_note"], orphan.get("token")),
                         (None, mpx.ORPHAN_NOTE, None))
        for r in rows:
            self.assertEqual((r["growth_mb_min"], r["idle_s"], r["history_reason"]),
                             (None, None, mpx.NO_HISTORY))
        pids = {p for r in rows for p in [r["root"]["pid"]]}
        self.assertFalse(pids & {10, 60, 70, 80, os.getpid()})

    def test_tmux_grandchild_gets_an_s2_token_once_room_frees(self):
        # B38, second run: without the 9 GB vitest, the cap admits the tmux job.
        sc = Scene(self, self.sessions, without=(11, 12))
        rows = sc.suggest(s1_jobs=sc.s1_jobs(), mint=True)
        self.assertEqual([r["root"]["pid"] for r in rows], [21, 30, 52])
        tmux = rows[2]
        body = mo.decode_token(tmux["token"])
        self.assertEqual((body["action"], body["owner_root"]["pid"], body["target"]["pid"],
                          body["target_pgid"]), ("stop-job", 50, 52, 52))

    def test_job_sharing_its_owner_roots_group_has_no_stop(self):
        # B38, third run: a nohup'd vitest in its shell's process group.
        extra = P(91, ppid=90, pgid=90, comm="node", fp=4 * GB)
        extra._argv = node("vitest/vitest.mjs", "run")
        shell = P(90, comm="sh", fp=5 * MB)
        shell._argv = ["sh", "./run-tests.sh"]
        sc = Scene(self, self.sessions, extra=(shell, extra))
        row = next(r for r in sc.suggest(mint=True) if r["root"]["pid"] == 91)
        self.assertEqual((row["stop"], row["stop_note"], row["token"]),
                         (None, mpx.GROUP_NOTE, None))

    def test_floor_is_max_of_1_gib_and_2_percent_of_ram(self):
        sc = Scene(self, self.sessions)
        big_ram = sc.suggest(ram_bytes=128 * GB)             # floor 2.56 GiB
        self.assertEqual([r["root"]["pid"] for r in big_ram], [11, 21])

    def test_no_tokens_unless_minted(self):
        sc = Scene(self, self.sessions)
        for r in sc.suggest():
            self.assertNotIn("token", r)
        self.assertNotIn("token", json.dumps(mpx.without_tokens(sc.suggest(mint=True))))

    def test_degraded_inventory_has_no_stop(self):
        sc = Scene(self, self.sessions)
        sc.src.name = "degraded"
        rows = sc.suggest(mint=True)
        self.assertTrue(rows)
        self.assertTrue(all(r["stop"] is None and r["token"] is None for r in rows))


K30 = P(30).key()


class HistoryTests(unittest.TestCase):
    def tree(self, fp_root, fp_workers, ticks):
        procs = [P(30, comm="node", fp=fp_root, ticks=ticks),
                 P(31, ppid=30, comm="node", fp=fp_workers, ticks=ticks),
                 P(32, ppid=30, comm="node", fp=fp_workers, ticks=ticks),
                 P(40, comm="node", fp=100 * MB)]
        argv = {30: node("vitest/vitest.mjs", "run"), 31: ["node", "/r/forks.js"],
                32: ["node", "/r/forks.js"], 40: node("vitest/vitest.mjs", "run")}
        return FakeSource(procs, argv=argv)

    def tick(self, hist, src, awake, ts):
        inv = mp.snapshot(src, clock=lambda: ts, mono=lambda: int(awake * 1e9))
        k = kind_of()
        return mpx.update_job_history(hist, inv, mpx.heavy_pids(inv, k), awake, BOOT)

    def test_job_history_sums_descendants(self):
        # B38's last case: non-heavy forks.js workers count toward the root.
        hist = {}
        for i in range(6):
            src = self.tree(200 * MB, (300 + 100 * i) * MB, ticks=0)
            hist = self.tick(hist, src, 1000 + 60 * i, T0 + 60 * i)
        key = K30
        self.assertEqual(set(hist["roots"]), {key})          # 40 is under 256 MiB
        pts = hist["roots"][key]["points"]
        self.assertEqual(len(pts), 6)
        self.assertEqual(pts[-1][2], (200 + 2 * 800) * MB)
        stats = mpx.series_stats(pts)
        self.assertAlmostEqual(stats["growth_mb_min"], 200.0)
        self.assertEqual(stats["idle_s"], 0)                # only 5 min of history

    def test_idle_after_ten_quiet_minutes_and_growth_window(self):
        hist = {}
        for i in range(13):
            src = self.tree(GB, GB, ticks=1000)              # CPU never moves
            hist = self.tick(hist, src, 1000 + 60 * i, T0 + 60 * i)
        stats = mpx.series_stats(hist["roots"][K30]["points"])
        self.assertEqual(stats["idle_s"], 720)
        self.assertEqual(stats["growth_mb_min"], 0.0)

    def test_busy_root_is_not_idle(self):
        pts = [[T0 + 60 * i, 60.0 * i, GB, int(i * 60 * 0.5e9)] for i in range(15)]
        self.assertEqual(mpx.series_stats(pts)["idle_s"], 0)

    def test_fewer_than_two_points_is_not_enough_history(self):
        self.assertEqual(mpx.series_stats([[T0, 1.0, GB, 0]]),
                         {"growth_mb_min": None, "idle_s": None,
                          "history_reason": "not enough history"})

    def test_bounds_exit_and_boot(self):
        hist = {"v": 1, "boot": BOOT,
                "roots": {f"{9000 + i}.1.0": {"kind": "test", "label": "x",
                                              "points": [[0, 0, GB, 0]] * 70}
                          for i in range(3)}}
        src = self.tree(GB, GB, 0)
        out = self.tick(hist, src, 1000, T0)
        self.assertEqual(list(out["roots"]), [K30])   # exited roots dropped
        procs = [P(100 + i, comm="node", fp=(300 + i) * MB) for i in range(70)]
        src = FakeSource(procs, argv={p.pid: node("vitest/vitest.mjs", "run") for p in procs})
        out = self.tick({}, src, 1000, T0)
        self.assertEqual(len(out["roots"]), mpx.HISTORY_MAX_ROOTS)
        self.assertNotIn(P(100).key(), out["roots"])        # smallest dropped
        many = {"v": 1, "boot": BOOT, "roots": {K30: {
            "points": [[0, float(i), GB, 0] for i in range(60)]}}}
        out = self.tick(many, self.tree(GB, GB, 0), 1000, T0)
        self.assertEqual(len(out["roots"][K30]["points"]), 60)
        other = self.tick({**many, "boot": "other"}, self.tree(GB, GB, 0), 1000, T0)
        self.assertEqual(len(other["roots"][K30]["points"]), 1)


class SamplerPathTests(unittest.TestCase):
    """B37: the scan, the episode and the gate naming follow under_pressure;
    job history gains points at every level."""

    def setUp(self):
        self.st = SamplerState(self)
        self.sessions = memmon.CLAUDE_SESSIONS_DIR

    @staticmethod
    def reading(level, rates, kernel="normal", ts=None, mono=50_000.0):
        rec = {"ts": ts or time.time(), "mono": mono, "uptime": mono - 1000, "boot": BOOT,
               "level": level, "rates": rates, "kernel_level": kernel,
               "ram_total": RAM, "lh_streak": 0, "gap": None,
               "under_pressure": mpx.under_pressure(level, rates, kernel)}
        pres = {"level": level, "rates": rates, "kernel_level": kernel, "reasons": [],
                "lh_streak": 0, "level_reason": None, "rates_source": None}
        return {"record": rec, "pressure": pres}

    def run_sampler(self, sc, reading):
        snap = {"ts": reading["record"]["ts"], "vm": {}, "pressure": reading["pressure"],
                "orphan_total": 0, "sessions": [], "apps": {}, "worktrees": []}
        with mock.patch("memmon_runner.jobs", return_value=sc.leases), \
                mock.patch.object(memmon, "_owners_ctx", return_value=sc.ctx):
            memmon.sampler_owners(snap, reading, source=sc.src, ctx=sc.ctx)
        with mock.patch.object(memmon, "learn"):
            memmon.log_sample(snap, reading)
        return snap

    def test_scan_follows_under_pressure_and_history_always_grows(self):
        ok = {"swapin_mbs": 0.0}
        cases = [("HEALTHY", ok, "normal", False), ("WATCH", ok, "normal", False),
                 ("UNKNOWN", "unavailable", "normal", False),
                 ("DANGER", ok, "normal", True), ("CRITICAL", ok, "normal", True),
                 ("UNKNOWN", "unavailable", "warning", True),
                 ("WATCH", "unavailable", "critical", True)]
        sc = Scene(self, self.sessions)
        real = mpx.suggestions
        for i, (level, rates, kern, fires) in enumerate(cases):
            with self.subTest(level=level, rates=rates, kernel=kern):
                calls = []
                with mock.patch.object(mpx, "suggestions",
                                       lambda *a, **kw: calls.append(1) or real(*a, **kw)):
                    snap = self.run_sampler(sc, self.reading(level, rates, kern,
                                                             mono=50_000.0 + 60 * i))
                self.assertEqual(bool(calls), fires)
                self.assertEqual(bool(snap["pressure_suggestions"]), fires)
                row = self.st.read(memmon.SNAPSHOT)
                self.assertIs(row["under_pressure"], fires)
                self.assertEqual(bool(row["pressure_suggestions"]), fires)
                hist = self.st.read(memmon.JOB_HISTORY)
                key = sc.inv.procs[12].key()
                self.assertEqual(len(hist["roots"][sc.inv.procs[11].key()]["points"]), i + 1)
                self.assertNotIn(key, hist["roots"])        # under its heavy shell

    def test_latest_json_holds_no_token(self):
        # B38, I-14
        sc = Scene(self, self.sessions)
        self.run_sampler(sc, self.reading("DANGER", {"swapin_mbs": 0.0}))
        for path in (memmon.SNAPSHOT, memmon.HISTORY):
            with open(path) as fh:
                text = fh.read()
            self.assertIn("pressure_suggestions", text)
            self.assertNotIn('"token"', text)
        self.assertTrue(self.st.read(memmon.SNAPSHOT)["pressure_suggestions"])

    def test_off_switch_disables_scan_notification_and_naming(self):
        sc = Scene(self, self.sessions)
        with mock.patch.dict(memmon.CONFIG, {"pressure_suggestions": False}):
            snap = self.run_sampler(sc, self.reading("CRITICAL", {"swapin_mbs": 0.0}))
            self.assertNotIn("pressure_suggestions", {k for k, v in snap.items() if v})
            self.assertEqual(self.st.notes, [])
            cached = {"ts": time.time(), "under_pressure": True, "pressure_suggestions": [
                {"label": "vitest in acme-web", "footprint": 9 * GB}]}
            _, msg = memmon.gate_decision("Bash", "pnpm typecheck",
                                          {"level": "DANGER", "reasons": ["x"]}, cached,
                                          "warn")
        self.assertNotIn("acme-web", msg)

    def test_gate_names_the_top_suggestion_to_the_human(self):
        cached = {"ts": 1000.0, "under_pressure": True, "pressure_suggestions": [
            {"label": "vitest in acme-web", "footprint": int(8.8 * GB), "idle_s": 31 * 60}]}
        pres = {"level": "DANGER", "reasons": ["paging 80 MB/s"]}
        action, msg = memmon.gate_decision("Bash", "pnpm typecheck", pres, cached, "warn",
                                           now=1100.0)
        self.assertEqual(action, "warn")
        self.assertIn("vitest in acme-web holds 8.8G and has been idle 31 min. Ask the "
                      "user before stopping it; do not stop it yourself.", msg)
        same, _ = memmon.gate_decision("Bash", "pnpm typecheck", pres, {}, "warn", now=1100.0)
        self.assertEqual(same, action)                      # the decision is unchanged
        for stale in ({**cached, "ts": 800.0}, {**cached, "under_pressure": False}):
            _, quiet = memmon.gate_decision("Bash", "pnpm typecheck", pres, stale, "warn",
                                            now=1100.0)
            self.assertNotIn("acme-web", quiet)
        allow, empty = memmon.gate_decision("Bash", "pnpm typecheck",
                                            {"level": "UNKNOWN"}, cached, "warn", now=1100.0)
        self.assertEqual((allow, empty), ("allow", ""))


class OwnersTextTests(unittest.TestCase):
    def test_owners_prints_an_under_pressure_section_without_tokens(self):
        payload = {"owners": [], "inventory": "libproc", "hidden_process_count": 3,
                   "pressure_suggestions": [
                       {"label": "vitest in acme-web", "footprint": 9 * GB, "idle_s": 0,
                        "growth_mb_min": 120.0, "stop": "stop-job", "token": "SECRET"},
                       {"label": "vitest", "footprint": 2 * GB, "stop": None,
                        "stop_note": mpx.ORPHAN_NOTE, "token": None}]}
        text = memmon.owners_text(payload)
        self.assertIn("Under pressure", text)
        self.assertIn("vitest in acme-web holds 9.0G and is growing 120 MB/min.", text)
        self.assertIn("memmon act stop-job --target <token from owners --json>", text)
        self.assertIn(mpx.ORPHAN_NOTE, text)
        self.assertNotIn("SECRET", text)
        quiet = memmon.owners_text({**payload, "pressure_suggestions": []})
        self.assertNotIn("Under pressure", quiet)


class EpisodeTests(unittest.TestCase):
    def step(self, st, minute, pressured=False, unknown=False, gap=False, rows=()):
        return mpx.episode_step(st, now_ts=T0 + 60 * minute, mono=1000 + 60 * minute,
                                boot=BOOT, pressured=pressured, unknown=unknown, gap=gap,
                                rows=list(rows))

    def test_notification_dedupe(self):
        # B39
        j1, j2 = {"job_id": "1.1.0"}, {"job_id": "2.2.0"}
        sent, st = [], None
        timeline = []
        for m in range(0, 12):
            timeline.append((m, dict(pressured=True, rows=[j1] + ([j2] if m >= 2 else []))))
        timeline.append((12, dict(unknown=True)))
        timeline += [(m, dict(gap=(m == 15))) for m in range(15, 26)]   # 3 min gap, WATCH
        timeline += [(m, dict(pressured=True, rows=[j1])) for m in range(26, 30)]
        episodes = []
        for m, kw in timeline:
            st, out = self.step(st, m, **kw)
            sent += [(m, r["job_id"]) for r in out]
            episodes.append((m, st["episode"], st["active"]))
        self.assertEqual(sent, [(0, "1.1.0"), (5, "2.2.0"), (26, "1.1.0")])
        active = {m: a for m, _, a in episodes}
        self.assertTrue(active[12] and active[15] and active[24])   # neither ended early
        self.assertFalse(active[25])                                # 10 min of WATCH
        self.assertEqual(episodes[-1][1], 2)

    def test_unknown_and_gaps_neither_extend_nor_end(self):
        st, _ = self.step(None, 0, pressured=True, rows=[{"job_id": "a"}])
        for m in range(1, 6):
            st, _ = self.step(st, m)
        for m in range(6, 30, 3):
            st, _ = self.step(st, m, unknown=True)
        self.assertTrue(st["active"])
        self.assertAlmostEqual(st["calm_s"], 240.0)
        st, _ = self.step(st, 40, gap=True)
        self.assertAlmostEqual(st["calm_s"], 240.0)

    def test_gap_time_never_counts_as_calm(self):
        st, _ = self.step(None, 0, pressured=True, rows=[{"job_id": "a"}])
        for m in range(1, 6):
            st, _ = self.step(st, m)                       # 4 min of calm counted
        st, _ = self.step(st, 14, gap=True)                # 8 min unsampled
        for m in range(15, 20):
            st, _ = self.step(st, m)
        self.assertTrue(st["active"], st)                  # 9 min counted, not 19
        self.assertAlmostEqual(st["calm_s"], 540.0)

    def test_floor_holds_across_episodes_and_boot_resets(self):
        st, out = self.step(None, 0, pressured=True, rows=[{"job_id": "a"}])
        self.assertEqual(len(out), 1)
        st["active"] = False
        st, out = self.step(st, 2, pressured=True, rows=[{"job_id": "a"}])
        self.assertEqual(out, [])                          # 5 min floor
        st2, _ = mpx.episode_step(st, now_ts=T0 + 400, mono=5.0, boot="other",
                                  pressured=False, unknown=False, gap=False, rows=[])
        self.assertEqual((st2["boot"], st2["active"], st2["notified"]), ("other", False, {}))

    def test_sampler_is_the_only_sender_and_uses_argv(self):
        st = SamplerState(self)
        sc = Scene(self, memmon.CLAUDE_SESSIONS_DIR)
        reading = SamplerPathTests.reading("CRITICAL", {"swapin_mbs": 0.0})
        snap = {"ts": reading["record"]["ts"], "vm": {}, "pressure": reading["pressure"],
                "orphan_total": 0, "sessions": [], "apps": {}, "worktrees": []}
        with mock.patch("memmon_runner.jobs", return_value=sc.leases):
            memmon.sampler_owners(snap, reading, source=sc.src, ctx=sc.ctx)
            memmon.sampler_owners(snap, reading, source=sc.src, ctx=sc.ctx)
        self.assertEqual(len(st.notes), 1)
        self.assertIn("vitest in", st.notes[0][0])
        self.assertTrue(os.path.exists(memmon.PRESSURE_EPISODE + ".lock"))
        # owners --json never notifies.
        with mock.patch.object(memmon, "gate_stats", return_value={}), \
                mock.patch.object(memmon, "system_block", return_value={
                    "score_level": "CRITICAL", "rates": {}, "pressure_level": "critical",
                    "ram_bytes": RAM}):
            payload = memmon.owners_json(0, source=sc.src, ctx=sc.ctx)
        self.assertTrue(payload["pressure_suggestions"])
        self.assertEqual(len(st.notes), 1)


class EnginePinTests(unittest.TestCase):
    def test_engine_accepts_s2_token_for_unknown_owner_job(self):
        # B42: S1's engine, unchanged, takes an S2-minted stop-job token for a
        # job under an unknown owner, and refuses it once the root restarts.
        st = SamplerState(self)
        ctx = mo.Context(sessions_dir=memmon.CLAUDE_SESSIONS_DIR, classify=classify,
                         commands=memmon.job_tokens)

        def scene():
            src = FakeSource([P(os.getpid(), ppid=0, comm="Python"),
                              P(50, comm="tmux", fp=20 * MB),
                              P(51, ppid=50, pgid=51, comm="zsh"),
                              P(52, ppid=51, pgid=52, comm="node", fp=2 * GB),
                              P(53, ppid=52, pgid=52, comm="node", fp=GB)],
                             argv={50: ["tmux"], 51: ["-zsh"],
                                   52: node("vitest/vitest.mjs", "run"),
                                   53: ["node", "/r/forks.js"]})
            inv = mp.snapshot(src, clock=time.time)
            part = mo.partition(inv, ctx)
            rows = mpx.suggestions(inv, part, kind_of(), ram_bytes=RAM, mint=True)
            return src, rows
        src, rows = scene()
        self.assertEqual(rows[0]["owner_id"].split(":")[0], "unknown")
        eng = fake_engine(src, ctx, memmon.ACTIONS_LOCK)
        out = eng.run(rows[0]["stop"], rows[0]["token"])
        self.assertEqual(out["result"], "stopped", out)
        self.assertEqual(sorted(p for p, _ in eng.sent), [52, 53])
        self.assertIn(50, src.table)

        src, rows = scene()
        src.table[52].start = (src.table[52].start[0] + 1, 0)       # PID reused
        out = fake_engine(src, ctx, memmon.ACTIONS_LOCK).run(rows[0]["stop"], rows[0]["token"])
        self.assertEqual((out["result"], out["reason"]), ("refused", "target_changed"))
        self.assertEqual(src.signals, [])


SLEEP_BUILD = """
import os, sys, time
exec(os.environ['MEMMON_NOTE'])
note("build")
time.sleep(120)
"""
STAND_IN = """
import os, subprocess, sys, time
exec(os.environ['MEMMON_NOTE'])
note("A")
subprocess.Popen([sys.executable, "-c", sys.argv[2], sys.argv[1]], start_new_session=True)
time.sleep(120)
"""


class NeverSignalTests(unittest.TestCase):
    def test_pressure_suggestions_never_signal(self):
        # B40, I-14: real registered sleepers classified as an unmanaged test
        # job, CRITICAL for five sampler minutes, with the sampler, owners
        # --json (MemmonBar's refresh) and the gate all running.
        st = SamplerState(self)
        reg = Registry(self)
        self.addCleanup(reg.cleanup)
        a = reg.spawn(STAND_IN, SLEEP_BUILD).pid
        session_file(memmon.CLAUDE_SESSIONS_DIR, a, reg.ids[a][0], job_id=f"{a:08x}")
        build = reg.wait_notes(2)["build"]

        class Fake(mpx.Classifier):
            def __call__(self, inv, pid):
                return ("test", "vitest") if pid == build else ("other", "x")
        crit = {"level": "CRITICAL", "color": "red", "score": 9, "reasons": ["paging"],
                "rates": {"swapin_mbs": 200.0}, "rates_source": "baseline",
                "level_reason": None, "kernel_level": "critical", "lh_streak": 0,
                "headroom_min": None}
        boom = mock.Mock(side_effect=AssertionError("signal or act on the pressure path"))
        payload = None
        with mock.patch.object(mpx, "Classifier", Fake), \
                mock.patch.object(mpx, "SUGGEST_MIN_BYTES", 1), \
                mock.patch.object(mpx, "SUGGEST_MIN_RAM_FRAC", 0), \
                mock.patch.object(memmon, "pressure", lambda vm: dict(crit)), \
                mock.patch.object(os, "kill", boom), mock.patch.object(os, "killpg", boom), \
                mock.patch.object(ma.Engine, "run", boom), \
                mock.patch.object(ma.Engine, "_signal", boom), \
                mock.patch.object(memmon, "act_cli", boom), \
                mock.patch.object(memmon, "gate_stats", return_value={}):
            for minute in range(5):
                reading = SamplerPathTests.reading("CRITICAL", crit["rates"],
                                                   "critical", mono=60_000.0 + 60 * minute)
                snap = {"ts": reading["record"]["ts"], "vm": {}, "pressure": dict(crit),
                        "orphan_total": 0, "sessions": [], "apps": {}, "worktrees": []}
                memmon.sampler_owners(snap, reading)
                with mock.patch.object(memmon, "learn"):
                    memmon.log_sample(snap, reading)
                payload = memmon.owners_json(0)
                gate_in = json.dumps({"tool_name": "Bash", "session_id": "s",
                                      "tool_input": {"command": "pnpm typecheck"}})
                with mock.patch("sys.stdin", io.StringIO(gate_in)), \
                        mock.patch("sys.stderr", io.StringIO()), \
                        mock.patch.dict(os.environ, {"MEMMON_GATE": "block"}):
                    self.assertEqual(memmon.gate(), 2)
                self.assertTrue(reg.alive(build))
        boom.assert_not_called()
        row = next(r for r in payload["pressure_suggestions"] if r["root"]["pid"] == build)
        self.assertEqual(row["stop"], "stop-job")
        self.assertTrue(st.notes)                            # notified, never stopped
        self.assertTrue(reg.alive(build))
        # Only the test's own explicit act call stops it.
        eng = reg.engine(mo.Context(sessions_dir=memmon.CLAUDE_SESSIONS_DIR),
                         memmon.ACTIONS_LOCK)
        out = eng.run("stop-job", row["token"])
        self.assertEqual(out["result"], "stopped", out)
        self.assertEqual(eng.sent, [(build, signal.SIGTERM)])
        self.assertTrue(reg.alive(a))


if __name__ == "__main__":
    unittest.main()
