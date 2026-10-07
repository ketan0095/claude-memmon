"""Owner model tests: the partition (I-5), titles and row disambiguation,
child jobs, history, growth and CPU baselines (I-7), the schema 2 payload,
legacy compatibility, and public-data hygiene (I-12). Everything runs on an
injected process table except where a row says otherwise."""

import contextlib
import io
import json
import os
import plistlib
import re
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import memmon
import memmon_owners as mo
import memmon_procs as mp
from testkit import MB, FakeSource, P, TempState, session_file

T0 = 1_791_400_000


def make_bundle(root, name, bundle_id):
    path = os.path.join(root, "Applications", f"{name}.app")
    os.makedirs(os.path.join(path, "Contents"), exist_ok=True)
    with open(os.path.join(path, "Contents", "Info.plist"), "wb") as fh:
        plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleName": name}, fh)
    return path


def make_repo(root, project, worktree=None):
    main = os.path.join(root, project)
    os.makedirs(os.path.join(main, ".git"), exist_ok=True)
    if not worktree:
        return main
    wt = os.path.join(root, worktree)
    os.makedirs(wt, exist_ok=True)
    with open(os.path.join(wt, ".git"), "w") as fh:
        fh.write(f"gitdir: {main}/.git/worktrees/{worktree}\n")
    return wt


def fake_lsof(table):
    """lsof -F output for pid -> [(type, device, name)], honouring -p."""
    def run(args):
        pids = [int(x) for x in args[args.index("-p") + 1].split(",")]
        out = []
        for pid in pids:
            out.append(f"p{pid}")
            for i, (kind, dev, name) in enumerate(table.get(pid, ())):
                out += [f"f{i + 3}", f"t{kind}", f"d{dev}", f"n{name}"]
        return "\n".join(out) + "\n"
    return run


def tool_shell(cmd):
    return ["/bin/zsh", "-c", f"source ~/.snapshot.sh && eval '{cmd}' < /dev/null"]


class OwnerBase(unittest.TestCase):
    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.close)
        self.root = self.state.root
        self.sessions = memmon.CLAUDE_SESSIONS_DIR
        self.ctx = mo.Context(sessions_dir=self.sessions,
                              socks_dir=os.path.join(self.root, "socks"),
                              codex_home=os.path.join(self.root, "codex"),
                              classify=lambda c: memmon.classify_command(c, {}))

    def claude(self, pid, job, ppid=1, cwd=None, **kw):
        p = P(pid, ppid=ppid, start=(T0 + pid, 0), comm="2.1.293", **kw)
        session_file(self.sessions, pid, T0 + pid, job_id=job, cwd=cwd)
        return p

    def build(self, procs, **kw):
        src = FakeSource(procs, **kw)
        inv = mp.snapshot(src, clock=lambda: T0 + 5000, mono=lambda: 10**12)
        return src, inv, mo.partition(inv, self.ctx)

    def payload(self, inv, part, cpu=None, history=None, reason="warming up"):
        return mo.owners_payload(mo.Sample(inv, part, cpu or {}, reason), self.ctx,
                                 history=history, now=inv.ts)

    def assert_partition(self, inv, part):
        """I-5: every visible, live PID in exactly one owner."""
        live = {pid for pid, p in inv.procs.items() if p.visible and not p.zombie}
        seen = [pid for o in part.owners.values() for pid in o.members]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), live)
        self.assertEqual(set(part.owner_of), live)


class PartitionTests(OwnerBase):
    def test_owner_partition_every_pid_exactly_once(self):
        procs = [
            self.claude(100, "aaaa0001"),
            P(101, ppid=100, pgid=100), P(102, ppid=100),
            P(103, ppid=102, pgid=102), P(104, ppid=103, pgid=102),
            P(200, comm="launchd-child"), P(201, ppid=200),
            P(300, uid=0, visible=False), P(301, ppid=300),
            P(400, ppid=100, zombie=True),
        ]
        _, inv, part = self.build(procs)
        self.assert_partition(inv, part)
        self.assertEqual(part.hidden, 1)
        fp = mo.owner_footprints(part, inv)
        self.assertEqual(sum(v for v in fp.values() if v),
                         sum(p.footprint for p in inv.procs.values()
                             if p.visible and not p.zombie))

    def test_nested_owners(self):
        # A13: a worker inside another session's tree, an orphan and an app
        # helper under a worktree cwd.
        wt = make_repo(self.root, "acme-web", "checkout")
        code = make_bundle(self.root, "Code", "com.example.code")
        procs = [
            self.claude(300, "0000aaaa", cwd=wt),
            P(301, ppid=300),                                 # tool shell
            self.claude(302, "0000bbbb", ppid=301, cwd=wt),   # nested worker
            P(303, ppid=302, pgid=302),
            P(310, comm="node"),                              # orphan
            P(320, comm="Code Helper"),
        ]
        paths = {320: f"{code}/Contents/Frameworks/Code Helper.app/Contents/MacOS/Code Helper"}
        argv = {301: tool_shell("claude -p review"),
                310: ["node", f"{wt}/node_modules/typescript/bin/tsc", "-b"]}
        _, inv, part = self.build(procs, argv=argv, paths=paths,
                                  cwds={310: wt, 320: wt})
        self.assert_partition(inv, part)
        outer, inner = part.owners["claude:0000aaaa"], part.owners["claude:0000bbbb"]
        self.assertEqual(sorted(outer.members), [300, 301])
        self.assertEqual(sorted(inner.members), [302, 303])
        self.assertEqual(part.owners[part.owner_of[310]].kind, "unknown")
        self.assertEqual(part.owners[part.owner_of[320]].kind, "app")

    def test_nested_owner_root_is_never_a_child_job(self):
        procs = [self.claude(300, "0000aaaa"), P(301, ppid=300),
                 self.claude(302, "0000bbbb", ppid=301)]
        _, inv, part = self.build(procs, argv={301: tool_shell("claude -p x")})
        jobs = mo.child_jobs(part.owners["claude:0000aaaa"], inv, part, self.ctx)
        self.assertEqual([j["members"] for j in jobs], [[301]])

    def test_codex_shapes_and_shared_services(self):
        # A12
        codex = self.ctx.codex_home
        os.makedirs(os.path.join(codex, "thread-writer-locks"))
        threads = [f"0000000{i}-aaaa-bbbb-cccc-00000000000{i}" for i in range(1, 4)]
        with open(os.path.join(codex, "session_index.jsonl"), "w") as fh:
            for i, t in enumerate(threads):
                fh.write(json.dumps({"id": t, "thread_name": f"Billing tests {i}"}) + "\n")
                open(os.path.join(codex, "thread-writer-locks", t + ".lock"), "w").close()
        exec_thread = "0000000e-eeee-eeee-eeee-00000000000e"
        locks = [("REG", "0x1000010", f"/x/.codex/thread-writer-locks/{t}.lock")
                 for t in threads]
        self.ctx.lsof = fake_lsof({
            201: [("unix", "0xd0d0", "/private/tmp/codex-daemon-501/aaaa")] + locks,
            210: [("unix", "0xc1c1", "->0xd0d0")],
            220: [("REG", "0x1000010", "/x/.codex/sessions/2026/10/08/"
                   f"rollout-2026-10-08T01-00-00-{exec_thread}.jsonl")]})
        brave = make_bundle(self.root, "Brave Browser", "com.example.brave")
        main = f"{brave}/Contents/MacOS/Brave Browser"
        helper = (f"{brave}/Contents/Frameworks/Brave Browser Framework.framework/"
                  "Helpers/Brave Browser Helper.app/Contents/MacOS/Brave Browser Helper")
        vm = ("/System/Library/Frameworks/Virtualization.framework/Versions/A/XPCServices/"
              "com.apple.Virtualization.VirtualMachine.xpc/Contents/MacOS/"
              "com.apple.Virtualization.VirtualMachine")
        procs = [
            P(200, comm="codex"), P(201, ppid=200, comm="codex"),
            P(202, ppid=201, comm="node_repl", pgid=202),
            P(211, comm="zsh"), P(210, ppid=211, comm="codex"),
            self.claude(230, "0000cccc"), P(231, ppid=230),
            P(220, ppid=231, pgid=231, comm="codex"), P(221, ppid=220, comm="node"),
            P(240, start=(T0, 0), comm="com.apple.Virtua", fp=5000 * MB),
            P(241, start=(T0 - 3, 0), comm="limactl"),
            P(250, comm="Brave Browser"), P(251, ppid=250, comm="Brave Browser He"),
            P(260, comm="Brave Browser"), P(261, ppid=260, comm="Brave Browser He"),
        ]
        argv = {200: ["codex", "app-server", "daemon", "pid-update-loop"],
                201: ["codex", "app-server", "--listen", "unix://"],
                210: ["codex", "--model", "m"],
                220: ["codex", "exec", "-C", "/tmp/acme-api", "-s", "read-only"],
                231: tool_shell("bash codex.sh run"),
                241: ["limactl", "hostagent", "--pidfile", "/x/_lima/colima/ha.pid", "colima"],
                250: [main], 260: [main], 251: [helper, "--type=renderer"],
                261: [helper, "--type=gpu-process"]}
        paths = {240: vm, 250: main, 260: main, 251: helper, 261: helper}
        _, inv, part = self.build(procs, argv=argv, paths=paths)
        self.assert_partition(inv, part)
        by_kind = {}
        for o in part.owners.values():
            by_kind.setdefault(o.kind, []).append(o)
        daemon = by_kind["codex-app"]
        self.assertEqual(len(daemon), 1)
        self.assertEqual(sorted(daemon[0].members), [200, 201, 202])
        self.assertEqual([o.members for o in by_kind["codex-ui"]], [[210]])
        execs = by_kind["codex"]
        self.assertEqual([o.owner_id for o in execs], [f"codex:{exec_thread}"])
        self.assertEqual(sorted(execs[0].members), [220, 221])
        self.assertEqual(sorted(part.owners["claude:0000cccc"].members), [230, 231])
        self.assertEqual(len(by_kind["service"]), 1)
        self.assertEqual(sorted(by_kind["service"][0].members), [240, 241])
        self.assertEqual(len(by_kind["app"]), 1)
        self.assertEqual(sorted(by_kind["app"][0].members), [250, 251, 260, 261])

        rows = {r["kind"]: r for r in self.payload(inv, part)["owners"]}
        self.assertEqual(rows["codex-app"]["confidence"], "shared")
        self.assertEqual(rows["codex-app"]["actions"], [])
        self.assertIsNone(rows["codex-app"]["token"])
        self.assertEqual(rows["codex-app"]["shared_with"],
                         ["Billing tests 0", "Billing tests 1", "Billing tests 2"])
        self.assertEqual(rows["codex-ui"]["actions"], [])
        self.assertIsNone(rows["codex-ui"]["token"])
        self.assertIn("runs in Codex daemon", rows["codex-ui"]["title"])
        self.assertEqual(rows["codex"]["actions"], ["end-session"])
        self.assertEqual(rows["service"]["confidence"], "shared")
        self.assertEqual(rows["service"]["actions"], [])
        self.assertEqual(rows["service"]["stop_command"], "colima stop")
        self.assertEqual(rows["app"]["title"], "Brave Browser")
        self.assertEqual([i["pid"] for i in rows["app"]["instances"]], [250, 260])
        self.assertEqual(rows["app"]["actions"], ["quit-app"])
        body = mo.decode_token(rows["app"]["token"])
        self.assertEqual(body["bundle_id"], "com.example.brave")
        self.assertEqual(len(body["instances"]), 2)

    def test_codex_tui_without_daemon_evidence_is_its_own_owner(self):
        # R3: no lsof evidence of a daemon connection -> the safe default, rule 2.
        procs = [P(210, comm="codex"), P(212, ppid=210, pgid=212),
                 P(213, comm="codex")]                    # childless, no evidence
        _, inv, part = self.build(procs, argv={210: ["codex"], 213: ["codex"]})
        owner = part.owners[part.owner_of[212]]
        self.assertEqual(owner.kind, "codex")
        self.assertEqual(owner.root, 210)
        lone = part.owners[part.owner_of[213]]
        self.assertEqual((lone.kind, lone.owner_id), ("codex", "codex-proc:213.1700000213"))

    def test_codex_tui_ownership_r3(self):
        # R3 probe fixture: a daemon-connected TUI is a pointer; in-process
        # TUIs (holding a writer lock, with or without a rollout) are rule 2;
        # a stale lock file on disk creates nothing.
        codex = self.ctx.codex_home
        os.makedirs(os.path.join(codex, "thread-writer-locks"))
        u1, u2, u3, u4 = (f"{d * 8}-{d * 4}-4{d * 3}-8{d * 3}-{d * 12}" for d in "1234")
        open(os.path.join(codex, "thread-writer-locks", f"{u4}.lock"), "w").close()
        with open(os.path.join(codex, "session_index.jsonl"), "w") as fh:
            fh.write(json.dumps({"id": u2, "thread_name": "Checkout refactor"}) + "\n")
            fh.write(json.dumps({"id": u1, "thread_name": "Billing cleanup"}) + "\n")
        lock = lambda u: ("REG", "0x1000010", f"/h/.codex/thread-writer-locks/{u}.lock")
        roll = lambda u: ("REG", "0x1000010",
                          f"/h/.codex/sessions/2026/10/08/rollout-2026-10-08T02-00-00-{u}.jsonl")
        self.ctx.lsof = fake_lsof({
            50001: [("unix", "0xd0d0", "/private/tmp/codex-daemon-501/aaaa"), lock(u1), roll(u1)],
            51000: [("unix", "0xc1c1", "->0xd0d0")],
            52000: [lock(u2), roll(u2), ("REG", "0x1000010", "/h/.codex/state_5.sqlite")],
            53000: [lock(u3)],
            54000: [lock(u4.replace("4", "5"))]})
        procs = [P(50000, comm="codex"), P(50001, ppid=50000, comm="codex"),
                 P(900, comm="zsh"), P(51000, ppid=900, comm="codex"),
                 P(901, comm="zsh"), P(52000, ppid=901, comm="codex"),
                 P(52001, ppid=52000), P(52002, ppid=52000), P(52003, ppid=52002),
                 P(902, comm="zsh"), P(53000, ppid=902, comm="codex"),
                 P(53001, ppid=53000),
                 P(903, comm="zsh"), P(54000, ppid=903, comm="codex")]   # holds a lock, no children
        argv = {50000: ["codex", "app-server", "daemon", "pid-update-loop"],
                50001: ["codex", "app-server", "--listen", "unix://"],
                51000: ["codex", "--model", "gpt-fixture"],
                52000: ["codex", "--disable", "daemon_auto_start"],
                53000: ["codex", "--disable", "daemon_auto_start"],
                54000: ["codex", "--disable", "daemon_auto_start"]}
        _, inv, part = self.build(procs, argv=argv)
        self.assert_partition(inv, part)
        of = lambda pid: part.owners[part.owner_of[pid]]
        self.assertEqual((of(50001).kind, of(50001).root), ("codex-app", 50000))
        self.assertEqual(of(51000).kind, "codex-ui")
        self.assertEqual(of(51000).members, [51000])
        self.assertEqual(of(52000).owner_id, f"codex:{u2}")
        self.assertEqual(sorted(of(52000).members), [52000, 52001, 52002, 52003])
        self.assertEqual(of(53000).owner_id, f"codex:{u3}")
        self.assertEqual(sorted(of(53000).members), [53000, 53001])
        self.assertEqual((of(54000).kind, of(54000).members), ("codex", [54000]))
        self.assertFalse(any(u4 in oid for oid in part.owners))
        rows = {r["owner_id"]: r for r in self.payload(inv, part)["owners"]}
        self.assertEqual(rows[f"codex:{u2}"]["title"], "Checkout refactor")
        self.assertEqual(rows[f"codex:{u2}"]["actions"], ["end-session"])
        self.assertEqual(rows[f"codex:{u3}"]["title"], "Codex · 33333333")
        ui = next(r for r in rows.values() if r["kind"] == "codex-ui")
        self.assertEqual((ui["actions"], ui["token"], ui["confidence"]), ([], None, "shared"))
        app = next(r for r in rows.values() if r["kind"] == "codex-app")
        self.assertEqual(app["shared_with"], ["Billing cleanup"])

    def test_gui_app_vm_carries_the_apps_quit_action(self):
        # S1.6/M5a: a VM run by Docker Desktop quits with Docker Desktop; a VM
        # paired with a Lima instance only gets a copyable command.
        docker = make_bundle(self.root, "Docker", "com.docker.docker")
        vm = "/System/Library/Frameworks/Virtualization.framework/x/com.apple.Virtualization.VirtualMachine"
        procs = [P(700, comm="com.docker.backend"), P(710, start=(T0, 0)),
                 P(730, start=(T0 + 9000, 0), comm="limactl"),          # usernet helper
                 P(720, start=(T0 + 5000, 0)), P(721, start=(T0 + 4998, 0), comm="limactl")]
        _, inv, part = self.build(procs, paths={700: f"{docker}/Contents/MacOS/com.docker.backend",
                                                710: vm, 720: vm},
                                  argv={721: ["limactl", "hostagent", "--pidfile",
                                              "/x/_lima/colima-dev/ha.pid", "colima-dev"]})
        rows = {r["owner_id"]: r for r in self.payload(inv, part)["owners"]}
        app = rows["app:com.docker.docker"]
        gui_vm = next(r for r in rows.values()
                      if r["owner_id"].startswith("service:vm:virtualization-"))
        self.assertEqual(gui_vm["actions"], ["quit-app"])
        self.assertEqual(gui_vm["token"], app["token"])
        self.assertEqual(gui_vm["quit_app_owner_id"], "app:com.docker.docker")
        helper = rows["service:vm:lima-helper"]
        self.assertEqual((helper["actions"], helper["token"]), ([], None))
        lima = rows["service:vm:colima-dev"]
        self.assertEqual((lima["actions"], lima["token"], lima["stop_command"]),
                         ([], None, "colima stop -p dev"))

    def test_python_app_bundle_outside_applications_is_not_an_app(self):
        path = ("/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/"
                "Versions/3.9/Resources/Python.app/Contents/MacOS/Python")
        _, inv, part = self.build([P(500, comm="Python")], paths={500: path})
        self.assertEqual(part.owners[part.owner_of[500]].kind, "unknown")

    def test_stale_session_file_and_spares_are_not_sessions(self):
        procs = [P(600, start=(T0 + 999, 0)), P(601, start=(T0 + 601, 0))]
        session_file(self.sessions, 600, T0 + 600, job_id="stale001")   # PID reused
        session_file(self.sessions, 601, T0 + 601, job_id="spare001", spare=True)
        _, inv, part = self.build(procs)
        self.assertEqual({o.kind for o in part.owners.values()}, {"unknown"})

    def test_empty_session_id_uses_job_id(self):
        p = P(700, start=(T0 + 700, 0))
        session_file(self.sessions, 700, T0 + 700, job_id="job00700", session_id="")
        _, inv, part = self.build([p])
        self.assertIn("claude:job00700", part.owners)

    def test_managed_job_rule(self):
        procs = [P(800, comm="python3"), P(801, ppid=800, pgid=801),
                 self.claude(810, "0000dddd"), P(811, ppid=810),
                 P(812, ppid=811, pgid=811), P(813, ppid=812, pgid=813)]
        self.ctx.leases = [
            {"id": "run-standalone", "child_pid": 801, "child_start": [T0 - 1e9 + 801, 801],
             "label": "unit tests"},
            {"id": "run-inside", "child_pid": 813, "child_start": [1_700_000_813, 813]},
        ]
        procs[1] = P(801, ppid=800, pgid=801, start=(T0 - 1e9 + 801, 801))
        _, inv, part = self.build(procs, argv={811: tool_shell("memmon run -- pnpm test")})
        self.assertIn("job:run-standalone", part.owners)
        self.assertNotIn("job:run-inside", part.owners)      # enclosed by the session
        self.assertEqual(part.owner_of[813], "claude:0000dddd")
        rows = {r["owner_id"]: r for r in self.payload(inv, part)["owners"]}
        self.assertEqual(rows["job:run-standalone"]["actions"], ["stop-managed-job"])
        job = rows["claude:0000dddd"]["jobs"][1]
        self.assertTrue(job["managed"])
        self.assertEqual(job["action"], "stop-managed-job")
        body = mo.decode_token(job["token"])
        self.assertEqual(body["target"]["pid"], 813)
        self.assertEqual(body["run_id"], "run-inside")


class PresentationTests(OwnerBase):
    def test_two_sessions_distinct_titles_and_worktrees(self):
        # A1
        a = make_repo(self.root, "acme-web", "checkout")
        b = make_repo(self.root, "acme-web", "search")
        self.ctx.titles_by_job = {"0000a001": "Checkout refactor",
                                  "0000a002": "Search indexing"}
        _, inv, part = self.build([self.claude(10, "0000a001", cwd=a),
                                   self.claude(11, "0000a002", cwd=b)])
        rows = self.payload(inv, part)["owners"]
        self.assertEqual({(r["title"], r["project"], r["worktree"]) for r in rows},
                         {("Checkout refactor", "acme-web", "checkout"),
                          ("Search indexing", "acme-web", "search")})
        for r in rows:
            self.assertNotRegex(r["title"], r"\b1[01]\b")       # no PID on the face

    def test_colliding_rows_get_started_suffix(self):
        # A2
        a = make_repo(self.root, "acme-web")
        self.ctx.titles_by_job = {"0000a001": "Checkout refactor",
                                  "0000a002": "Checkout refactor"}
        _, inv, part = self.build([self.claude(10, "0000a001", cwd=a),
                                   self.claude(11, "0000a002", cwd=a)])
        titles = sorted(r["title"] for r in self.payload(inv, part)["owners"])
        for pid, t in zip((10, 11), titles):
            hhmm = time.strftime("%H:%M", time.localtime(T0 + pid))
            self.assertEqual(t, f"Checkout refactor · started {hhmm}")

    def test_title_falls_back_to_short_id(self):
        _, inv, part = self.build([self.claude(10, "0000a001")])
        self.assertEqual(self.payload(inv, part)["owners"][0]["title"], "0000a001")

    def test_child_jobs_activity_and_conversation(self):
        procs = [self.claude(10, "0000a001"), P(11, ppid=10, pgid=10, comm="npm"),
                 P(20, ppid=10, fp=10 * MB), P(21, ppid=20, pgid=20, fp=4000 * MB),
                 P(30, ppid=10, fp=50 * MB)]
        argv = {20: tool_shell("pnpm --filter web typecheck"),
                30: tool_shell("pnpm dev")}
        _, inv, part = self.build(procs, argv=argv)
        row = self.payload(inv, part, cpu={10: 0.1})["owners"][0]
        jobs = {j["label"]: j for j in row["jobs"]}
        self.assertEqual(jobs["typecheck"]["kind"], "build")
        self.assertEqual(jobs["typecheck"]["action"], "stop-job")
        self.assertEqual(jobs["typecheck"]["member_count"], 2)
        self.assertEqual(jobs["dev"]["kind"], "server")
        self.assertEqual(jobs["dev"]["action"], "stop-server")
        self.assertEqual(row["activity"], "Building · typecheck")
        convo = row["jobs"][0]
        self.assertEqual((convo["kind"], convo["label"], convo["member_count"]),
                         ("conversation", "Conversation", 2))
        self.assertIsNone(convo["token"])
        self.assertIsNone(convo["action"])
        body = mo.decode_token(jobs["typecheck"]["token"])
        self.assertEqual((body["action"], body["target"]["pid"], body["owner_root"]["pid"]),
                         ("stop-job", 20, 10))

    def test_listening_socket_makes_a_server(self):
        procs = [self.claude(10, "0000a001"), P(20, ppid=10), P(21, ppid=20, pgid=20)]
        self.ctx.listening = {21}
        _, inv, part = self.build(procs, argv={20: tool_shell("node script.js")})
        self.assertEqual(self.payload(inv, part)["owners"][0]["jobs"][1]["kind"], "server")

    def test_activity_working_idle_and_unknown(self):
        _, inv, part = self.build([self.claude(10, "0000a001")])
        self.assertEqual(self.payload(inv, part, {10: 0.5})["owners"][0]["activity"], "Working")
        self.assertEqual(self.payload(inv, part, {10: 0.05})["owners"][0]["activity"], "Idle")
        self.assertIsNone(self.payload(inv, part, {})["owners"][0]["activity"])

    def test_unavailable_not_zero(self):
        # I-7: no footprint, no CPU, no history -> null with a reason; sorts last.
        procs = [self.claude(10, "0000a001"), P(50)]
        src, inv, part = self.build(procs)
        inv.procs[10].footprint = None
        rows = self.payload(inv, part)["owners"]
        self.assertEqual(rows[-1]["owner_id"], "claude:0000a001")
        last = rows[-1]
        self.assertIsNone(last["footprint_bytes"])
        self.assertEqual(last["footprint_reason"], "not measured")
        self.assertIsNone(last["cpu_cores"])
        self.assertEqual(last["cpu_reason"], "warming up")
        self.assertIsNone(last["growth_bytes_per_10min"])
        self.assertEqual(last["growth_reason"], "not enough history")

    def test_degraded_inventory_mints_no_tokens(self):
        src, inv, part = self.build([self.claude(10, "0000a001"), P(11, ppid=10)],
                                    argv={11: tool_shell("pnpm test")})
        src.name = "degraded"
        payload = self.payload(inv, part)
        self.assertEqual(payload["inventory"], "degraded")
        row = payload["owners"][0]
        self.assertIsNone(row["token"])
        self.assertEqual(row["actions"], [])
        self.assertEqual([j["token"] for j in row["jobs"]], [None, None])

    def test_token_round_trip(self):
        body = {"v": 1, "action": "stop-job", "x": "é"}
        self.assertEqual(mo.decode_token(mo.mint_token(body)), body)
        for bad in ("!!!", mo.mint_token({"v": 2}), mo.mint_token([1])):
            with self.assertRaises(ValueError):
                mo.decode_token(bad)


class HistoryTests(OwnerBase):
    def test_growth_needs_five_samples_over_ten_minutes(self):
        now = T0
        four = [[now - 720 + 180 * i, 100 * MB + i * MB] for i in range(4)]
        self.assertEqual(mo.growth(four, now), (None, "not enough history"))
        six = [[now - 750 + 150 * i, 100 * MB + i * 10 * MB] for i in range(6)]
        g, reason = mo.growth(six, now)
        self.assertIsNone(reason)
        self.assertAlmostEqual(g, 40 * MB, delta=MB)

    def test_gap_resets_growth_window(self):
        now = T0
        before = [[now - 1200 + 120 * i, 100 * MB] for i in range(6)]
        after = [[now - 100, 120 * MB], [now, 125 * MB]]
        samples = [s for s in before if s[0] < now - 300] + after   # 200 s+ gap
        self.assertEqual(mo.growth(samples, now), (None, "not enough history"))

    def test_history_caps_samples_and_owners(self):
        hist = {}
        for i in range(70):
            hist = mo.update_history(hist, {"a": i, "b": None}, T0 + i)
        self.assertEqual(len(hist["owners"]["a"]["samples"]), 60)
        self.assertNotIn("b", hist["owners"])
        for i in range(205):
            hist = mo.update_history(hist, {f"o{i}": 1}, T0 + 100 + i)
        self.assertEqual(len(hist["owners"]), 200)
        self.assertNotIn("a", hist["owners"])          # least recently seen goes
        self.assertIn("o204", hist["owners"])
        crowd = {f"small{i}": i for i in range(250)}
        crowd["big"] = 10**12
        hist = mo.update_history({}, crowd, T0)       # all seen in one tick
        self.assertEqual(len(hist["owners"]), 200)
        self.assertIn("big", hist["owners"])
        self.assertNotIn("small0", hist["owners"])

    def test_history_written_atomically_and_small(self):
        path = memmon.OWNERS_HISTORY
        hist = {}
        for i in range(60):
            hist = mo.update_history(hist, {f"claude:{j:08x}": 9_556_302_848 + i
                                            for j in range(200)}, T0 + 60 * i)
        mo.write_json_atomic(path, hist)
        self.assertLess(os.path.getsize(path), 600_000)
        self.assertEqual([f for f in os.listdir(self.root) if f.endswith(".tmp")], [])

    def test_sampler_ticks_cpu_from_persisted_baseline(self):
        # A17: two separate --log processes 60 s apart share only files.
        procs = [self.claude(10, "0000a001", ticks=0), P(11, ppid=10, ticks=0)]
        src = FakeSource(procs)
        awake = [5 * 10**9]
        with mock.patch.object(mo, "awake_ns", lambda: awake[0]), \
                mock.patch.object(mo, "boot_id", lambda: "boot-1"):
            first = memmon.owners_sampler_tick(src, self.ctx, clock=lambda: T0,
                                               mono=lambda: 100 * 10**9)
            self.assertEqual(first["baseline_problem"], "warming up")
            self.assertEqual(first["cpu"], {})
            src.table[10].cpu_ticks = 24_000_000 * 30            # 30 s of CPU
            awake[0] += 60 * 10**9
            second = memmon.owners_sampler_tick(src, self.ctx, clock=lambda: T0 + 60,
                                                mono=lambda: 160 * 10**9)
        self.assertIsNone(second["baseline_problem"])
        self.assertAlmostEqual(second["cpu"][10], 0.5)
        hist = mo.read_json(memmon.OWNERS_HISTORY, {})
        row = hist["owners"]["claude:0000a001"]
        self.assertEqual(len(row["samples"]), 2)
        self.assertAlmostEqual(row["cpu"][1], 0.5)
        base = mo.read_json(memmon.CPU_BASELINE, {})
        self.assertEqual(base["boot_id"], "boot-1")
        self.assertIn(f"10.{T0 + 10}.0", base["procs"])

    def test_baseline_invalid_after_gap_wake_or_reboot(self):
        src = FakeSource([P(10)])
        inv = mp.snapshot(src, mono=lambda: 1000 * 10**9)
        good = {"boot_id": "b", "mono_ns": 940 * 10**9, "awake_ns": 40 * 10**9, "procs": {}}
        self.assertIsNone(mo.baseline_problem(good, inv, "b", 100 * 10**9))
        self.assertEqual(mo.baseline_problem(good, inv, "other", 100 * 10**9), "warming up")
        old = {**good, "mono_ns": 800 * 10**9}                        # 200 s gap
        self.assertEqual(mo.baseline_problem(old, inv, "b", 240 * 10**9), "warming up")
        self.assertEqual(mo.baseline_problem(good, inv, "b", 60 * 10**9),
                         "warming up")                                # slept 40 s
        self.assertEqual(mo.baseline_problem({}, inv, "b", 0), "warming up")

    def test_payload_growth_from_history(self):
        _, inv, part = self.build([self.claude(10, "0000a001")])
        now = inv.ts
        hist = {"owners": {"claude:0000a001": {"samples": [
            [now - 720 + 120 * i, 100 * MB + i * 12 * MB] for i in range(7)]}}}
        row = self.payload(inv, part, history=hist)["owners"][0]
        self.assertAlmostEqual(row["growth_bytes_per_10min"], 60 * MB, delta=MB)
        self.assertIsNone(row["growth_reason"])


class LegacyCompatTests(unittest.TestCase):
    LEGACY_KEYS = {"ts", "vm", "pressure", "blocked", "gate", "jobs", "sessions",
                   "idle_sessions", "orphans", "orphan_total", "overhead",
                   "service_owner", "worktrees", "other_heavy", "apps"}

    def setUp(self):
        self.state = TempState()
        self.addCleanup(self.state.close)

    def test_legacy_json_adds_only_schema_version(self):
        # A18
        snap = {k: {"marker": k} for k in self.LEGACY_KEYS}
        snap["pressure"] = {"level": "HEALTHY"}
        out = io.StringIO()
        with mock.patch.object(memmon, "collect", return_value=dict(snap)), \
                mock.patch("sys.argv", ["memmon", "--json"]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(memmon.main(), 0)
        self.assertEqual(json.loads(out.getvalue()), {**snap, "schema_version": 2})

    def test_legacy_collect_never_touches_new_inventory(self):
        # A18: --json/--log values still come from top, not libproc.
        with mock.patch.object(mp, "snapshot", side_effect=AssertionError("libproc")), \
                mock.patch.object(mp, "default_source", side_effect=AssertionError("libproc")):
            snap = memmon.collect()
        self.assertEqual(set(snap), self.LEGACY_KEYS)

    def test_log_row_shape_unchanged(self):
        snap = {"ts": T0, "vm": {}, "pressure": {"level": "HEALTHY"}, "orphan_total": 0,
                "sessions": [], "apps": {}, "worktrees": [], "overhead": {}}
        with mock.patch.object(memmon, "learn"):
            memmon.log_sample(snap)
        with open(memmon.SNAPSHOT) as fh:
            row = json.load(fh)
        self.assertEqual(set(row), {"ts", "ram_used", "swap_used", "swap_total",
                                    "free_pct", "load", "orphan", "swapins", "swapouts",
                                    "pressure", "_lh_streak", "sessions", "apps",
                                    "worktrees", "worktree_tags", "overhead"})

    def test_runway_copy_is_a_trend_to_the_floor(self):
        # P11: the estimate is to the 20 % floor, never "runs out".
        pres = {"level": "DANGER", "reasons": ["paging"], "headroom_min": 12}
        _, msg = memmon.gate_decision("Bash", "pnpm typecheck", pres, {}, "warn")
        self.assertIn("about 12 min until free memory reaches the 20 % floor "
                      "(trend estimate)", msg)
        self.assertNotIn("runs out", msg)
        text = memmon.render({"ts": T0, "vm": {"ram_total": 1, "swap_total": 0},
                              "pressure": {**pres, "color": "red", "advice": ""},
                              "sessions": [], "orphans": [], "orphan_total": 0,
                              "overhead": {}, "worktrees": [], "other_heavy": [],
                              "apps": {}, "jobs": [], "gate": {}}, on=False)
        self.assertIn("floor (trend estimate)", text)
        self.assertNotIn("runs out", text)


class OwnersCliTests(unittest.TestCase):
    def test_owners_json_schema_and_embedded_gate(self):
        state = TempState()
        self.addCleanup(state.close)
        src = FakeSource([P(10), P(11, ppid=10)])
        gate = {"installed": True, "paused": False, "policy": {"mode": "warn"},
                "counts": {"stopped": 1}, "history": {"events": []}, "pending_retry": []}
        ctx = mo.Context(sessions_dir=memmon.CLAUDE_SESSIONS_DIR)
        with mock.patch.object(memmon, "gate_stats", return_value=gate), \
                mock.patch.object(memmon, "gate_installed", return_value=True), \
                mock.patch.object(memmon, "pressure", return_value={"level": "WATCH"}), \
                mock.patch.object(memmon, "read_vm", return_value={}):
            payload = memmon.owners_json(0, source=src, ctx=ctx,
                                         system_reader=lambda: {"ram_bytes": 8,
                                                                "used_bytes": 4,
                                                                "pressure_level": "normal"})
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["gate"], gate)                  # verbatim (D10)
        self.assertEqual(payload["source"], "live")
        self.assertEqual(payload["system"]["used_bytes"], 4)
        self.assertEqual(payload["system"]["score_level"], "WATCH")
        self.assertEqual(payload["system"]["ncpu"], os.cpu_count())
        self.assertIsNone(payload["system"]["cpu_cores"])       # window 0, no baseline
        self.assertEqual(payload["protection"],
                         {"summary": "on", "gate": "on", "route": "off",
                          "unmanaged_heavy": 0})
        self.assertIsNone(payload["cpu_window_s"])
        json.dumps(payload)

    def test_failed_strict_read_is_null_with_reason(self):
        def boom():
            raise OSError("vm_stat exited 1")
        with mock.patch.object(memmon, "pressure", return_value={"level": "HEALTHY"}), \
                mock.patch.object(memmon, "read_vm", return_value={}):
            block = memmon.system_block(boom)
        self.assertIsNone(block["used_bytes"])
        self.assertIsNone(block["pressure_level"])
        self.assertIn("vm_stat exited 1", block["reason"])

    def test_protection_states(self):
        with mock.patch.object(memmon, "gate_installed", return_value=False):
            self.assertEqual(memmon.protection_block(3)["summary"], "off")
        with mock.patch.object(memmon, "gate_installed", return_value=True), \
                mock.patch.object(memmon, "pause_until", return_value=float("inf")):
            self.assertEqual(memmon.protection_block(0)["summary"], "paused")
        with mock.patch.object(memmon, "gate_installed", return_value=True), \
                mock.patch.object(memmon, "pause_until", return_value=0):
            p = memmon.protection_block(2)
        self.assertEqual((p["summary"], p["unmanaged_heavy"], p["route"]),
                         ("partial", 2, "off"))


class PublicHygieneTests(unittest.TestCase):
    # Built from fragments so this file does not match its own patterns.
    HOME_RE = re.compile("/" + "Users/[A-Za-z][A-Za-z0-9._-]{2,}/")
    MAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+" + "@" + r"[A-Za-z0-9-]+\.[A-Za-z.]{2,}")
    COMMON = {"runner", "admin", "ubuntu", "travis", "jenkins", "github", "worker"}
    ALLOWED_MAIL = {"noreply@anthropic.com"}

    def files(self):
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"],
                                 cwd=here, capture_output=True, text=True, timeout=10)
        except Exception:
            self.skipTest("not a git checkout")
        if out.returncode != 0:
            self.skipTest("not a git checkout")
        return [os.path.join(here, f) for f in out.stdout.splitlines()]

    def test_public_hygiene(self):
        # I-12
        user = os.environ.get("USER") or ""
        check_user = len(user) >= 6 and user.lower() not in self.COMMON
        hits = []
        for path in self.files():
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if self.HOME_RE.search(line):
                    hits.append(f"{path}:{n}: home path")
                for m in self.MAIL_RE.findall(line):
                    if m not in self.ALLOWED_MAIL and not m.endswith(".png"):
                        hits.append(f"{path}:{n}: email {m}")
                if check_user and user.lower() in line.lower():
                    hits.append(f"{path}:{n}: local username")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
