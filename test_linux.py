"""Linux readings: /proc parsers, PSI, the procfs inventory, session
identity, the systemd sampler install. Pure parsers run everywhere; anything
that reads the live /proc runs on Linux only."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import memmon
import memmon_linux as lx
import memmon_owners as mo
import memmon_telemetry as tm

HERE = os.path.dirname(os.path.abspath(__file__))
LINUX = sys.platform.startswith("linux")
on_linux = unittest.skipUnless(LINUX, "reads the live /proc")

MEMINFO = """MemTotal:       32858576 kB
MemFree:        24000000 kB
MemAvailable:    3942000 kB
SwapTotal:       8388604 kB
SwapFree:        6291452 kB
Unevictable:        1024 kB
Mlocked:               0 kB
HugePages_Total:       0
"""
PSI = """some avg10=31.50 avg60=12.00 avg300=3.10 total=123456
full avg10=6.20 avg60=2.00 avg300=0.50 total=65432
"""


class ParserTests(unittest.TestCase):
    def test_meminfo_in_bytes_and_free_pct(self):
        mi = lx.meminfo(MEMINFO)
        self.assertEqual(mi["MemTotal"], 32858576 * 1024)
        self.assertEqual(mi["HugePages_Total"], 0)
        self.assertEqual(lx.free_pct(mi), 11)          # 3.9 of 31.3 GB
        with self.assertRaises(ValueError):
            lx.free_pct({})

    def test_stat_comm_with_spaces_and_parens(self):
        text = ("4242 (tsc (worker) x) S 4000 4242 4242 0 -1 4194560 1 0 0 0 "
                "150 25 0 0 20 0 9 0 264851 1000 200 18446744073709551615")
        st = lx.parse_stat(text)
        self.assertEqual(st["comm"], "tsc (worker) x")
        self.assertEqual((st["state"], st["ppid"], st["pgid"]), ("S", 4000, 4242))
        self.assertEqual((st["utime"], st["stime"], st["starttime"]), (150, 25, 264851))

    def test_ticks_to_start(self):
        with mock.patch.object(lx, "_btime", 1_794_000_000), \
                mock.patch.object(lx, "CLK_TCK", 100):
            self.assertEqual(lx.ticks_to_start(264851), (1_794_002_648, 510_000))

    def test_psi_parse_and_level(self):
        p = lx.psi(PSI)
        self.assertEqual(p, {"some": 31.5, "full": 6.2})
        self.assertEqual(lx.psi_level({"some": 0.0, "full": 0.0}), 1)
        self.assertEqual(lx.psi_level({"some": 10.0, "full": 0.0}), 2)
        self.assertEqual(lx.psi_level({"some": 1.0, "full": 5.0}), 2)
        self.assertEqual(lx.psi_level({"some": 90.0, "full": 15.0}), 4)
        self.assertEqual(lx.psi("garbage line\n"), {})

    def test_psi_off_reads_empty(self):
        with mock.patch.object(lx, "_read", side_effect=FileNotFoundError):
            self.assertEqual(lx.psi(), {})


class ScoreTests(unittest.TestCase):
    """PSI only ever adds points to a Linux row; a Mac row scores as before."""

    BASE = {"free_pct": 50, "swap_used": 0, "ram_total": 32 << 30, "load": 0.5, "ncpu": 8}
    QUIET = {"swapin_mbs": 0.0, "swapout_mbs": 0.0, "swap_growth_mbmin": 0.0}

    def level(self, **vm):
        return tm.score(dict(self.BASE, **vm), self.QUIET, None)

    def test_mac_row_unchanged(self):
        out = self.level()
        self.assertEqual((out["level"], out["score"]), ("HEALTHY", 0))

    def test_psi_tiers(self):
        self.assertEqual(self.level(psi_some=0.0, psi_full=0.0)["score"], 0)
        self.assertEqual(self.level(psi_some=12.0, psi_full=0.0)["score"], 1)
        self.assertEqual(self.level(psi_some=30.0, psi_full=0.0)["score"], 2)
        self.assertEqual(self.level(psi_some=50.0, psi_full=20.0)["score"], 4)

    def test_stall_plus_low_memory_is_critical(self):
        out = self.level(free_pct=10, psi_some=60.0, psi_full=20.0)
        self.assertEqual(out["level"], "CRITICAL", out)
        self.assertTrue(any("memory stall" in r for r in out["reasons"]))

    def test_low_memory_alone_is_watch(self):
        self.assertEqual(self.level(free_pct=15, psi_some=0.0, psi_full=0.0)["level"], "WATCH")


class SessionIdentityTests(unittest.TestCase):
    def test_numeric_procstart_matches_ticks_exactly(self):
        p = mock.Mock(start=(1_794_002_648, 510_000), start_ticks=264851)
        self.assertTrue(mo._same_start({"procStart": "264851"}, p))
        self.assertFalse(mo._same_start({"procStart": "264852"}, p))
        self.assertFalse(mo._same_start({"procStart": "264851"}, None))
        no_ticks = mock.Mock(start=(1, 0), start_ticks=None)
        self.assertFalse(mo._same_start({"procStart": "264851"}, no_ticks))

    def test_macos_ctime_procstart_still_parses(self):
        p = mock.Mock(start=(1_700_000_000, 0), start_ticks=None)
        rec = {"procStart": time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(1_700_000_001))}
        self.assertTrue(mo._same_start(rec, p))


@on_linux
class ProcfsTests(unittest.TestCase):
    def setUp(self):
        import memmon_procs as mp
        self.mp = mp
        self.src = mp.default_source()

    def test_default_source_is_procfs_and_self_check_passes(self):
        self.assertEqual(self.src.name, "procfs")
        self.assertIsNone(self.mp.procfs_self_check(self.src))

    def test_child_identity_argv_cwd_and_listing(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "tag"],
                                 cwd=tempfile.gettempdir())
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        time.sleep(0.2)
        p = self.src.read(child.pid)
        self.assertEqual(p.ppid, os.getpid())
        self.assertTrue(p.visible)
        self.assertGreater(p.footprint, 0)
        with open(f"/proc/{child.pid}/stat") as fh:
            self.assertEqual(p.start_ticks, lx.parse_stat(fh.read())["starttime"])
        self.assertLessEqual(abs(p.start[0] - time.time()), 5)
        self.assertEqual(self.src.argv(child.pid)[-1], "tag")
        self.assertEqual(os.path.realpath(self.src.cwd(child.pid)),
                         os.path.realpath(tempfile.gettempdir()))
        import memmon_runner
        self.assertIn(child.pid, memmon_runner._procfs_children(os.getpid()))
        self.assertIs(memmon_runner._child_lister(self.src), memmon_runner._procfs_children)

    def test_gone_pid_reads_none(self):
        child = subprocess.Popen(["true"])
        child.wait()
        self.assertIsNone(self.src.read(child.pid))

    def test_reapers_include_pid_1(self):
        self.assertIn(1, lx.reapers())
        self.assertTrue(memmon.reparented(1))
        self.assertFalse(memmon.reparented(os.getpid()))

    def test_system_and_vm_readers(self):
        sys_ = self.mp.read_system_strict()
        self.assertGreater(sys_["ram_bytes"], sys_["used_bytes"] > 0)
        self.assertIn(sys_["pressure_level"], ("normal", "warning", "critical"))
        vm = memmon.read_vm(fast=True)
        for key in ("free_pct", "swap_used", "swapins", "swapouts", "ram_total", "boot", "load"):
            self.assertIn(key, vm)
        r = tm.read_pressure_strict()
        self.assertEqual(r["page_size"], os.sysconf("SC_PAGE_SIZE"))
        self.assertEqual(r["boot"], lx.boot_id())

    def test_clocks_count_from_boot_in_every_process(self):
        out = subprocess.run([sys.executable, "-c",
                              "import memmon_telemetry as t; print(t.SYSTEM_CLOCK.awake())"],
                             cwd=HERE, capture_output=True, text=True, check=True)
        self.assertLess(abs(float(out.stdout) - tm.SYSTEM_CLOCK.awake()), 5)


@on_linux
class NotifyTests(unittest.TestCase):
    def test_notify_send_gets_text_as_one_argument(self):
        calls = []
        text = 'vitest in acme-web" $(id) "'
        memmon.notify(text, "memmon", "sub", run=lambda argv, **kw: calls.append(argv))
        self.assertEqual(calls[0][:3], ["notify-send", "--app-name=memmon", "memmon"])
        self.assertEqual(calls[0][3], f"sub\n{text}")

    def test_headless_box_posts_nothing(self):
        with mock.patch("shutil.which", return_value=None), \
                mock.patch.object(subprocess, "Popen") as popen:
            memmon.notify("x")
        popen.assert_not_called()


@on_linux
class InstallTests(unittest.TestCase):
    """install.sh --sampler into a temp HOME with systemctl and loginctl
    stubbed, so no real unit is loaded."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="memmon-home-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.stubs = os.path.join(self.home, "stubs")
        os.makedirs(self.stubs)
        self.calls = os.path.join(self.home, "calls.log")
        for name, body in (("systemctl", "exit 0"), ("loginctl", "echo yes")):
            path = os.path.join(self.stubs, name)
            with open(path, "w") as fh:
                fh.write(f'#!/bin/sh\necho "{name} $*" >> "{self.calls}"\n{body}\n')
            os.chmod(path, 0o755)
        self.env = {"HOME": self.home, "PATH": f"{self.stubs}:/usr/bin:/bin",
                    "TMPDIR": self.home}

    def install(self, *flags):
        return subprocess.run(["/bin/bash", os.path.join(HERE, "install.sh"), *flags],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def test_sampler_is_a_user_timer(self):
        out = self.install("--sampler")
        self.assertEqual(out.returncode, 0, out.stderr)
        units = os.path.join(self.home, ".config/systemd/user")
        with open(os.path.join(units, "dev.memmon.sampler.service")) as fh:
            svc = fh.read()
        with open(os.path.join(units, "dev.memmon.sampler.timer")) as fh:
            timer = fh.read()
        self.assertIn(f"ExecStart=/usr/bin/python3 {self.home}/.claude/memmon/memmon.py --log", svc)
        self.assertNotIn("Nice=", svc)
        self.assertIn("OnUnitActiveSec=60s", timer)
        with open(self.calls) as fh:
            log = fh.read()
        self.assertIn("systemctl --user enable --now dev.memmon.sampler.timer", log)
        self.assertTrue(os.path.exists(os.path.join(self.home, ".claude/memmon/memmon_linux.py")))
        self.assertFalse(os.path.exists(os.path.join(self.home, "Library")))

    def test_menubar_refused(self):
        out = self.install("--menubar")
        self.assertEqual(out.returncode, 1)
        self.assertIn("macOS-only", out.stderr)

    def test_uninstall_removes_units(self):
        self.assertEqual(self.install("--sampler").returncode, 0)
        out = self.install("--uninstall")
        self.assertEqual(out.returncode, 0, out.stderr)
        units = os.path.join(self.home, ".config/systemd/user")
        self.assertFalse(os.path.exists(os.path.join(units, "dev.memmon.sampler.timer")))
        with open(self.calls) as fh:
            self.assertIn("systemctl --user disable --now dev.memmon.sampler.timer", fh.read())

    def test_gate_needs_zsh(self):
        with mock.patch.dict(os.environ):
            env = dict(self.env, PATH=self.stubs + ":/nonexistent")
            os.symlink("/usr/bin/python3", os.path.join(self.stubs, "python3"))
            for tool in ("uname", "dirname", "cat", "mkdir", "cp", "mv", "chmod", "date", "id"):
                src = shutil.which(tool)
                if src:
                    os.symlink(src, os.path.join(self.stubs, tool))
            out = subprocess.run(["/bin/bash", os.path.join(HERE, "install.sh"), "--gate"],
                                 env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 1)
        self.assertIn("needs zsh", out.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude/settings.json")))


if __name__ == "__main__":
    unittest.main()
