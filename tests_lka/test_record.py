"""A measured run becomes a record: its verdict, its metrics, and whether it may be kept."""

import signal
import struct
import unittest
from pathlib import Path

from lka_quiet.record import RUN_SCHEMA, build_record, correctness_of, status_of
from lka_quiet.sysfs import ACCT_V3

FIXTURES = Path(__file__).parent / "fixtures"

MACHINE = {
    "lanes": {"4core": [0, 1, 2, 3], "1core": [3]},
    "frequency": {"pinned_hz": 2_833_000_000, "tolerance": 0.005, "min_task_clock_s": 0.2},
    "interference": {"max_fraction": 0.005, "floor_ms": 20, "jiffy_ms": 10},
    "sandbox": {"uid": 900},
}

PROC_STAT = "cpu0 100 0 0 900 0 0 0 0\ncpu1 0 0 0 1000 0 0 0 0\ncpu2 0 0 0 1000 0 0 0 0\ncpu3 0 0 0 1000 0 0 0 0\n"
INTERRUPTS = "  CPU0 CPU1 CPU2 CPU3\n LOC: 10 10 10 10 Local timer\n"


def proc_stat_after(busy3: int, busy0: int = 100) -> str:
    return (f"cpu0 {busy0} 0 0 900 0 0 0 0\ncpu1 0 0 0 1000 0 0 0 0\ncpu2 0 0 0 1000 0 0 0 0\n"
            f"cpu3 {busy3} 0 0 1000 0 0 0 0\n")


def acct(uid=900, flag=0, exitcode=0, pid=10):
    return struct.pack(ACCT_V3, flag, 3, 0, exitcode, uid, uid, pid, 1, 1700000000, 0.1,
                       0, 0, 0, 0, 0, 0, 0, 0, b"checker".ljust(16, b"\0"))


def observation(**overrides):
    obs = {
        "shim": {"wall_ns": 3_000_000_000, "exit_code": 0, "signal": None, "maxrss_kb": 500 * 1024},
        "timed_out": False,
        "elapsed_s": 3.01,
        "perf_text": (FIXTURES / "perf-plain-synthesized.json").read_text(),
        "memory_peak": 512 * 2**20,
        "memory_events": "oom 0\noom_kill 0\n",
        "cpu_stat": "usage_usec 2990000\n",
        "stragglers": 0,
        "energy": {"before_uj": 1_000_000, "after_uj": 31_000_000, "max_range_uj": 262_143_328_850},
        "throttle": {"core_before": 4, "core_after": 4, "package_before": 1, "package_after": 1},
        "proc_stat_before": PROC_STAT,
        "proc_stat_after": proc_stat_after(299),
        "interrupts_before": INTERRUPTS,
        "interrupts_after": INTERRUPTS,
        "acct": acct(exitcode=0),
        "stdout": "",
        "stderr": "",
    }
    obs.update(overrides)
    return obs


REQUEST = {
    "checker": "nano-lean",
    "test": "std",
    "lane": "1core",
    "expected": "accept",
    "timeout_s": 600.0,
    "checker_hash": "c" * 64,
    "test_sha256": "t" * 64,
    "epoch": "e" * 16,
    "round": "2026-11",
    "pass": 0,
    "seq": 17,
}


class Status(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(status_of(0, None, False, None, 0), "accepted")
        self.assertEqual(status_of(1, None, False, None, 0), "rejected")
        self.assertEqual(status_of(2, None, False, None, 0), "declined")
        self.assertEqual(status_of(3, None, False, None, 0), "error")

    def test_timeout_wins(self):
        self.assertEqual(status_of(None, None, True, None, 0), "timeout")

    def test_a_crash_is_an_error_whatever_the_exit_code(self):
        crash = {"kind": "signal", "signal": "SIGSEGV", "pid": 3, "comm": "x"}
        self.assertEqual(status_of(1, None, False, crash, 0), "error")

    def test_an_oom_kill_is_an_error(self):
        self.assertEqual(status_of(1, None, False, None, 1), "error")

    def test_the_top_process_killed_by_a_signal_is_an_error(self):
        self.assertEqual(status_of(None, signal.SIGKILL, False, None, 0), "error")


class Correctness(unittest.TestCase):
    """The arena's own semantics (lka.py run_checker_on_test), unchanged."""

    def test_table(self):
        cases = [
            ("declined", "accept", "declined"),
            ("error", "reject", "error"),
            ("timeout", "accept", "error"),
            ("accepted", "either", "either"),
            ("rejected", "either", "either"),
            ("accepted", "accept", "correct"),
            ("rejected", "reject", "correct"),
            ("accepted", "reject", "incorrect"),
            ("rejected", "accept", "incorrect"),
        ]
        for status, expected, correctness in cases:
            self.assertEqual(correctness_of(status, expected), correctness, (status, expected))


class Record(unittest.TestCase):
    def test_clean_run(self):
        record = build_record(REQUEST, observation(), MACHINE)
        self.assertEqual(record["schema"], RUN_SCHEMA)
        self.assertEqual(record["status"], "accepted")
        self.assertEqual(record["correctness"], "correct")
        self.assertTrue(record["valid"], record["invalid_reasons"])
        self.assertEqual(record["wall_s"], 3.0)
        self.assertEqual(record["memory_peak_bytes"], 512 * 2**20)
        self.assertEqual(record["maxrss_bytes"], 500 * 1024 * 1024)
        self.assertAlmostEqual(record["energy_j"], 30.0)
        self.assertEqual(record["counters"]["instructions_u"], 9_100_000_000)
        for key in ("checker", "test", "lane", "timeout_s", "checker_hash", "test_sha256", "epoch", "round", "pass", "seq"):
            self.assertEqual(record[key], REQUEST[key], key)

    def test_effective_frequency(self):
        record = build_record(REQUEST, observation(), MACHINE)
        # (8.414e9 + 0.05e9) cycles / 2.987654321 s
        self.assertAlmostEqual(record["freq_effective_hz"], 8.464e9 / 2.987654321, places=0)

    def test_frequency_drift_invalidates(self):
        machine = {**MACHINE, "frequency": {**MACHINE["frequency"], "pinned_hz": 2_700_000_000}}
        record = build_record(REQUEST, observation(), machine)
        self.assertFalse(record["valid"])
        self.assertTrue(any(r.startswith("frequency:") for r in record["invalid_reasons"]), record["invalid_reasons"])

    def test_frequency_unchecked_on_short_runs(self):
        short = observation(perf_text=(FIXTURES / "perf-hybrid-i9-14900k.json").read_text())
        machine = {**MACHINE, "frequency": {**MACHINE["frequency"], "pinned_hz": 1_000_000_000}}
        record = build_record(REQUEST, short, machine)
        self.assertFalse(any(r.startswith("frequency:") for r in record["invalid_reasons"]))

    def test_frequency_unchecked_when_not_pinned(self):
        machine = {**MACHINE, "frequency": {**MACHINE["frequency"], "pinned_hz": None}}
        self.assertTrue(build_record(REQUEST, observation(), machine)["valid"])

    def test_throttling_invalidates(self):
        obs = observation(throttle={"core_before": 4, "core_after": 5, "package_before": 1, "package_after": 1})
        record = build_record(REQUEST, obs, MACHINE)
        self.assertIn("thermal throttling: core +1, package +0", record["invalid_reasons"])

    def test_interference_on_the_lane_invalidates(self):
        # cpu3 busy 600 jiffies = 6 s, the run itself used 2.99 s: 3 s of someone else on the lane
        record = build_record(REQUEST, observation(proc_stat_after=proc_stat_after(600)), MACHINE)
        self.assertFalse(record["valid"])
        self.assertTrue(any(r.startswith("interference:") for r in record["invalid_reasons"]), record["invalid_reasons"])

    def test_interference_just_above_the_allowance_invalidates(self):
        # 3040 ms busy on cpu3 against 2990 ms used by the run: 50 ms foreign, 20 ms allowed
        record = build_record(REQUEST, observation(proc_stat_after=proc_stat_after(304)), MACHINE)
        self.assertFalse(record["valid"])
        self.assertAlmostEqual(record["foreign_cpu_ms"], 50.0)

    def test_housekeeping_cpu_activity_is_not_interference_in_the_1core_lane(self):
        record = build_record(REQUEST, observation(proc_stat_after=proc_stat_after(299, busy0=5000)), MACHINE)
        self.assertTrue(record["valid"], record["invalid_reasons"])

    def test_jiffy_quantisation_is_tolerated(self):
        record = build_record(REQUEST, observation(proc_stat_after=proc_stat_after(301)), MACHINE)
        self.assertTrue(record["valid"], record["invalid_reasons"])

    def test_multiplexing_invalidates(self):
        obs = observation(perf_text=(FIXTURES / "perf-plain-multiplexed-synthesized.json").read_text())
        record = build_record(REQUEST, obs, MACHINE)
        self.assertFalse(record["valid"])
        self.assertIn("multiplexed: instructions:u ran 50.0% of the time", record["invalid_reasons"])

    def test_crash_masked_by_exit_1_becomes_an_error(self):
        obs = observation(shim={"wall_ns": 10**9, "exit_code": 1, "signal": None},
                          acct=acct(exitcode=0, pid=1) + acct(flag=0x10, exitcode=signal.SIGSEGV, pid=2))
        record = build_record({**REQUEST, "expected": "reject"}, obs, MACHINE)
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["correctness"], "error")
        self.assertEqual(record["crash"]["signal"], "SIGSEGV")

    def test_panic_exit_codes_are_recorded(self):
        obs = observation(shim={"wall_ns": 10**9, "exit_code": 1, "signal": None},
                          acct=acct(exitcode=101 << 8, pid=2) + acct(exitcode=1 << 8, pid=1))
        record = build_record({**REQUEST, "expected": "reject"}, obs, MACHINE)
        self.assertEqual(record["status"], "rejected")
        self.assertEqual(record["descendant_exit_codes"], [1, 101])

    def test_oom_kill(self):
        record = build_record(REQUEST, observation(memory_events="oom 1\noom_kill 1\n",
                                                   shim={"wall_ns": 10**9, "exit_code": 1, "signal": None}), MACHINE)
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["oom_kills"], 1)

    def test_timeout_keeps_metrics(self):
        obs = observation(shim=None, timed_out=True, elapsed_s=600.2)
        record = build_record(REQUEST, obs, MACHINE)
        self.assertEqual(record["status"], "timeout")
        self.assertEqual(record["wall_s"], 600.2)
        self.assertEqual(record["counters"]["instructions_u"], 9_100_000_000)
        self.assertEqual(record["memory_peak_bytes"], 512 * 2**20)

    def test_stragglers_are_flagged(self):
        record = build_record(REQUEST, observation(stragglers=2), MACHINE)
        self.assertEqual(record["stragglers"], 2)
        self.assertTrue(record["valid"])

    def test_no_energy_domain(self):
        record = build_record(REQUEST, observation(energy=None), MACHINE)
        self.assertIsNone(record["energy_j"])

    def test_output_is_bounded(self):
        record = build_record(REQUEST, observation(stdout="x" * 100_000), MACHINE)
        self.assertLessEqual(len(record["stdout"]), 16_500)


if __name__ == "__main__":
    unittest.main()
