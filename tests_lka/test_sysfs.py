"""Kernel interfaces a measured run is read from: cgroup v2, RAPL, throttling, CPU time, IRQs, process accounting."""

import signal
import struct
import unittest

from lka_quiet.sysfs import (
    ACCT_V3,
    AcctRecord,
    crash_of,
    energy_delta_uj,
    lane_busy_jiffies,
    lane_interrupts,
    parse_acct_v3,
    parse_cpu_stat,
    parse_flat_keyed,
    parse_interrupts,
    parse_proc_stat,
)

PROC_STAT = """\
cpu  400 0 100 1000 20 5 5 0 0 0
cpu0 100 0 25 250 5 2 1 0 0 0
cpu1 100 0 25 250 5 1 1 0 0 0
cpu2 100 0 25 250 5 1 1 0 0 0
cpu3 100 0 25 250 5 1 2 0 0 0
intr 12345 0 0
ctxt 999
btime 1700000000
"""

INTERRUPTS = """\
            CPU0       CPU1       CPU2       CPU3
   0:         20          0          0          0  IR-IO-APIC    2-edge      timer
   9:          0          4          0          0  IR-IO-APIC    9-fasteoi   acpi
 120:       5000          0          0         17  IR-PCI-MSIX-0000:01:00.0    0-edge      nvme0q0
 NMI:          1          1          1          1   Non-maskable interrupts
 LOC:     100000      90000      80000         12   Local timer interrupts
 ERR:          7
 MIS:          0
"""


def acct_record(flag=0, exitcode=0, uid=0, pid=1, ppid=0, btime=1700000000, comm=b"checker"):
    return struct.pack(ACCT_V3, flag, 3, 0, exitcode, uid, uid, pid, ppid, btime, 0.5,
                       0, 0, 0, 0, 0, 0, 0, 0, comm.ljust(16, b"\0"))


class FlatKeyed(unittest.TestCase):
    def test_memory_events(self):
        events = parse_flat_keyed("low 0\nhigh 0\nmax 3\noom 1\noom_kill 1\noom_group_kill 0\n")
        self.assertEqual(events["oom_kill"], 1)
        self.assertEqual(events["max"], 3)

    def test_cpu_stat(self):
        stat = parse_cpu_stat("usage_usec 1500\nuser_usec 1000\nsystem_usec 500\nnr_periods 0\n")
        self.assertEqual(stat["usage_usec"], 1500)

    def test_blank_and_malformed_lines_are_skipped(self):
        self.assertEqual(parse_flat_keyed("\nnot-a-pair\nk 7\n"), {"k": 7})


class Rapl(unittest.TestCase):
    def test_plain_delta(self):
        self.assertEqual(energy_delta_uj(1_000, 6_000, 262_143_328_850), 5_000)

    def test_wraparound(self):
        top = 262_143_328_850
        self.assertEqual(energy_delta_uj(top - 100, 400, top), 500)

    def test_equal_readings(self):
        self.assertEqual(energy_delta_uj(42, 42, 1000), 0)


class ProcStat(unittest.TestCase):
    def test_per_cpu_busy_and_total(self):
        cpus = parse_proc_stat(PROC_STAT)
        self.assertEqual(sorted(cpus), [0, 1, 2, 3])
        busy, total = cpus[3]
        # busy = user + nice + system + irq + softirq + steal
        self.assertEqual(busy, 100 + 0 + 25 + 1 + 2 + 0)
        self.assertEqual(total, 100 + 0 + 25 + 250 + 5 + 1 + 2 + 0)

    def test_aggregate_line_is_not_a_cpu(self):
        self.assertNotIn(-1, parse_proc_stat(PROC_STAT))

    def test_lane_busy_delta(self):
        before = parse_proc_stat(PROC_STAT)
        after = {cpu: (busy + 10 * (cpu + 1), total + 100) for cpu, (busy, total) in before.items()}
        self.assertEqual(lane_busy_jiffies(before, after, [3]), 40)
        self.assertEqual(lane_busy_jiffies(before, after, [0, 1, 2, 3]), 100)


class Interrupts(unittest.TestCase):
    def test_columns_per_cpu(self):
        per_cpu = parse_interrupts(INTERRUPTS)
        self.assertEqual(per_cpu[0], 20 + 0 + 5000 + 1 + 100000)
        self.assertEqual(per_cpu[3], 0 + 0 + 17 + 1 + 12)

    def test_single_value_rows_are_not_spread_over_cpus(self):
        per_cpu = parse_interrupts(INTERRUPTS)
        self.assertEqual(per_cpu[1], 4 + 1 + 90000)

    def test_lane_delta(self):
        before = parse_interrupts(INTERRUPTS)
        after = dict(before)
        after[3] += 9
        after[0] += 1000
        self.assertEqual(lane_interrupts(before, after, [3]), 9)


class ProcessAccounting(unittest.TestCase):
    def test_record_size_is_the_kernels(self):
        self.assertEqual(struct.calcsize(ACCT_V3), 64)

    def test_parse_exit_code(self):
        records = parse_acct_v3(acct_record(exitcode=101 << 8, uid=1234, pid=77, comm=b"nl-fast"))
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.uid, 1234)
        self.assertEqual(record.pid, 77)
        self.assertEqual(record.comm, "nl-fast")
        self.assertEqual(record.exit_status, 101)
        self.assertIsNone(record.signal)

    def test_parse_signal_death(self):
        record = parse_acct_v3(acct_record(flag=0x10, exitcode=signal.SIGSEGV, uid=5))[0]
        self.assertEqual(record.signal, signal.SIGSEGV)
        self.assertIsNone(record.exit_status)
        self.assertFalse(record.core_dumped)

    def test_core_dump_bit(self):
        record = parse_acct_v3(acct_record(flag=0x18, exitcode=signal.SIGABRT | 0x80, uid=5))[0]
        self.assertEqual(record.signal, signal.SIGABRT)
        self.assertTrue(record.core_dumped)

    def test_several_records_and_a_torn_tail(self):
        blob = acct_record(pid=1) + acct_record(pid=2) + b"\x00" * 10
        self.assertEqual([r.pid for r in parse_acct_v3(blob)], [1, 2])

    def test_other_versions_are_rejected(self):
        bad = bytearray(acct_record())
        bad[1] = 2
        with self.assertRaises(ValueError):
            parse_acct_v3(bytes(bad))


class Crash(unittest.TestCase):
    def rec(self, **kw) -> AcctRecord:
        return parse_acct_v3(acct_record(**kw))[0]

    def test_segfault_of_a_sandbox_process_is_a_crash(self):
        records = [self.rec(uid=900, pid=1, exitcode=1 << 8), self.rec(uid=900, pid=2, flag=0x10, exitcode=signal.SIGSEGV)]
        crash = crash_of(records, sandbox_uid=900)
        self.assertEqual(crash, {"kind": "signal", "signal": "SIGSEGV", "pid": 2, "comm": "checker"})

    def test_abort_is_a_crash(self):
        crash = crash_of([self.rec(uid=900, flag=0x10, exitcode=signal.SIGABRT)], sandbox_uid=900)
        self.assertEqual(crash["signal"], "SIGABRT")

    def test_other_users_are_not_the_run(self):
        self.assertIsNone(crash_of([self.rec(uid=0, flag=0x10, exitcode=signal.SIGSEGV)], sandbox_uid=900))

    def test_sigkill_and_sigterm_are_not_crashes(self):
        # Our own timeout (cgroup.kill) and a checker stopping its own workers end processes by signal too
        records = [self.rec(uid=900, flag=0x10, exitcode=signal.SIGKILL), self.rec(uid=900, flag=0x10, exitcode=signal.SIGTERM)]
        self.assertIsNone(crash_of(records, sandbox_uid=900))

    def test_clean_exits_are_not_crashes(self):
        self.assertIsNone(crash_of([self.rec(uid=900, exitcode=0), self.rec(uid=900, exitcode=1 << 8)], sandbox_uid=900))


if __name__ == "__main__":
    unittest.main()
