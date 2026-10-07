"""Inventory tests: the libproc ABI guard, the degraded fallback, CPU deltas
and the strict system reader. Only the test's own process is read for real."""

import ctypes
import errno
import os
import subprocess
import sys
import unittest
from unittest import mock

import memmon_procs as mp
from testkit import FakeSource, P


class StubLib:
    """A libproc whose calls return what the test scripts."""

    def __init__(self, info_ret, info_errno=0, rusage_ret=0):
        self.info_ret, self.info_errno, self.rusage_ret = info_ret, info_errno, rusage_ret

    def proc_pidinfo(self, pid, flavor, arg, buf, size):
        ctypes.set_errno(self.info_errno)
        return self.info_ret

    def proc_pid_rusage(self, pid, flavor, buf):
        return self.rusage_ret


def stub_source(lib):
    src = mp.LibprocSource.__new__(mp.LibprocSource)
    src.lib, src.my_uid, src._timebase = lib, os.getuid(), (1, 1)
    return src


class LibprocTests(unittest.TestCase):
    def test_abi_struct_sizes_pinned(self):
        self.assertEqual(ctypes.sizeof(mp.BSDInfo), 136)
        self.assertEqual(ctypes.sizeof(mp.RUsageV4), 296)

    def test_self_check_passes_on_real_libproc(self):
        # A16b: no false degradation when libproc works.
        self.assertIsNone(mp.DEGRADED_REASON)
        src = mp.default_source()
        self.assertEqual(src.name, "libproc")
        self.assertIsNone(mp.self_check(src))
        inv = mp.snapshot(src)
        self.assertEqual(inv.kind, "libproc")
        me = inv.procs[os.getpid()]
        self.assertEqual(me.ppid, os.getppid())
        self.assertEqual(me.pgid, os.getpgid(0))
        self.assertGreater(me.footprint, 0)
        self.assertGreaterEqual(me.lifetime_max, me.footprint)
        self.assertEqual(inv.cwd(os.getpid()), os.getcwd())
        self.assertEqual(os.path.realpath(inv.argv(os.getpid())[0]),
                         os.path.realpath(inv.path(os.getpid())))

    def test_self_check_catches_wrong_timebase(self):
        src = mp.default_source()
        numer, denom = src.timebase()
        with mock.patch.object(src, "timebase", return_value=(numer * 50, denom)):
            self.assertIn("cpu ticks disagree", mp.self_check(src))

    def test_self_check_catches_impossible_footprint(self):
        self.assertEqual(mp.self_check(mp.default_source(), memsize=1),
                         "footprint out of range")

    def test_short_read_is_unreadable_not_zero(self):
        p = stub_source(StubLib(info_ret=100)).read(4242)
        self.assertFalse(p.visible)
        self.assertIsNone(p.footprint)
        self.assertIsNone(p.start)

    def test_eperm_is_invisible_not_zero(self):
        p = stub_source(StubLib(info_ret=0, info_errno=errno.EPERM)).read(1)
        self.assertFalse(p.visible)
        self.assertIsNone(p.footprint)

    def test_esrch_means_gone(self):
        self.assertIsNone(stub_source(StubLib(info_ret=0, info_errno=errno.ESRCH)).read(9))

    def test_unavailable_not_zero_when_rusage_fails(self):
        real = mp.default_source()
        src = stub_source(None)
        src.lib = mock.Mock(proc_pidinfo=real.lib.proc_pidinfo,
                            proc_pid_rusage=lambda *a: -1)
        p = src.read(os.getpid())
        self.assertTrue(p.visible)
        self.assertIsNotNone(p.start)
        self.assertIsNone(p.footprint)
        self.assertIsNone(p.cpu_ticks)

    def test_rusage_only_zero_return_accepted(self):
        real = mp.default_source()
        src = stub_source(None)
        src.lib = mock.Mock(proc_pidinfo=real.lib.proc_pidinfo,
                            proc_pid_rusage=lambda *a: 1)
        self.assertIsNone(src.read(os.getpid()).footprint)


TOP = """Processes: 3 total
PhysMem: 10G used (1G wired, 1G compressor), 2G unused.

PID    MEM
101    512M
102    1.5G
"""
PS = """  101     1   101   {uid} Wed Oct  7 18:06:44 2026     Ss   /bin/zsh
  102   101   101   {uid} Wed Oct  7 18:07:00 2026     S+   /usr/bin/node
  103   101   101   {uid} Wed Oct  7 18:07:01 2026     Z    (node)
"""


class DegradedTests(unittest.TestCase):
    def fake_run(self, cmd, **kw):
        out = TOP if cmd[0] == "top" else PS.format(uid=os.getuid())
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def test_libproc_unavailable_falls_back_to_ps_and_top(self):
        # A16: degraded inventory, memory from top, second-resolution identity.
        with mock.patch.object(mp, "_load_libproc", return_value=(None, None)):
            src, reason = mp._probe()
        self.assertIsNone(src)
        self.assertEqual(reason, "libproc not found")
        with mock.patch.object(mp, "_LIBPROC", None):
            self.assertEqual(mp.default_source().name, "degraded")
        inv = mp.snapshot(mp.PsTopSource(run=self.fake_run))
        self.assertEqual(inv.kind, "degraded")
        self.assertEqual(inv.procs[102].footprint, int(1.5 * (1 << 30)))
        self.assertEqual(inv.procs[102].start[1], 0)
        self.assertTrue(inv.procs[103].zombie)
        self.assertIsNone(inv.procs[102].cpu_ticks)

    def test_failed_self_check_degrades(self):
        with mock.patch.object(mp, "self_check", return_value="pbi_pid/pbi_ppid mismatch"):
            src, reason = mp._probe()
        self.assertIsNone(src)
        self.assertEqual(reason, "pbi_pid/pbi_ppid mismatch")

    def test_env_forces_degraded_path(self):
        with mock.patch.dict(os.environ, {"MEMMON_INVENTORY": "top"}):
            src = mp.default_source()
            self.assertEqual(src.name, "degraded")
            self.assertEqual(mp.degraded_reason(src), "MEMMON_INVENTORY=top")
        self.assertEqual(mp.default_source().name, "libproc")


class CpuTests(unittest.TestCase):
    def test_cpu_delta_and_identity_change(self):
        src = FakeSource([P(10, ticks=0), P(11, ticks=0), P(12, ticks=0)])
        first = mp.snapshot(src, clock=lambda: 0.0, mono=lambda: 0)
        src.table[10].cpu_ticks = 24_000_000          # x 125/3 = 1 s
        src.table[11] = P(11, start=(5, 5), ticks=99)  # PID reused
        del src.table[12]
        src.table[13] = P(13, ticks=5)
        second = mp.snapshot(src, clock=lambda: 1.0, mono=lambda: 1_000_000_000)
        cores = mp.cpu_cores(mp.tick_table(first), second, first.mono_ns)
        self.assertAlmostEqual(cores[10], 1.0)
        self.assertNotIn(11, cores)   # warming up, not 0
        self.assertNotIn(13, cores)
        self.assertNotIn(12, cores)

    def test_tick_table_caps_keeping_busiest(self):
        src = FakeSource([P(i, ticks=i) for i in range(2, 12)])
        table = mp.tick_table(mp.snapshot(src), cap=3)
        self.assertEqual(sorted(table.values()), [9, 10, 11])


VMSTAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                  100.
Pages wired down:                             200.
Pages purgeable:                               10.
Anonymous pages:                              1000.
Pages stored in compressor:                  9999.
Pages occupied by compressor:                  50.
"""


class StrictReaderTests(unittest.TestCase):
    def sysctl(self, name):
        return {"kern.memorystatus_vm_pressure_level": 2, "hw.memsize": 1 << 34}[name]

    def run_ok(self, out=VMSTAT, rc=0):
        return lambda cmd, **kw: subprocess.CompletedProcess(cmd, rc, out, "")

    def test_used_basis_excludes_stored_in_compressor(self):
        r = mp.read_system_strict(run=self.run_ok(), sysctl=self.sysctl)
        self.assertEqual(r["used_bytes"], (1000 - 10 + 200 + 50) * 16384)
        self.assertEqual(r["pressure_level"], "warning")
        self.assertEqual(r["ram_bytes"], 1 << 34)

    def test_raises_rather_than_defaulting(self):
        with self.assertRaises(KeyError):
            mp.read_system_strict(run=self.run_ok(VMSTAT.replace("Anonymous", "Anon")),
                                  sysctl=self.sysctl)
        with self.assertRaises(OSError):
            mp.read_system_strict(run=self.run_ok(rc=1), sysctl=self.sysctl)

        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        with self.assertRaises(subprocess.TimeoutExpired):
            mp.read_system_strict(run=hang, sysctl=self.sysctl)
        with self.assertRaises(ValueError):
            mp.read_system_strict(run=self.run_ok(),
                                  sysctl=lambda n: 3 if "level" in n else 1)

    def test_real_reader_works(self):
        r = mp.read_system_strict()
        self.assertGreater(r["used_bytes"], 0)
        self.assertIn(r["pressure_level"], ("normal", "warning", "critical"))


if __name__ == "__main__":
    unittest.main()
