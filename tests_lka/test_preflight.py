"""The machine's fingerprint (its epoch) and the checks a session starts with."""

import unittest

from lka_quiet.preflight import check_state, epoch_of, fingerprint

FACTS = {
    "cpu_model": "Intel(R) N100",
    "microcode": "0x1c",
    "kernel": "6.8.0-48-generic",
    "cmdline": "BOOT_IMAGE=/vmlinuz root=UUID=x ro nohz_full=1-3 rcu_nocbs=1-3 irqaffinity=0",
    "bios": "ADLNV106 2024-03-01",
    "memory": "1 x 16384 MB DDR4 3200 MT/s",
    "thp": "madvise",
    "governor": {"0": "performance", "1": "performance", "2": "performance", "3": "performance"},
    "scaling_min_khz": {"0": 2400000, "1": 2400000, "2": 2400000, "3": 2400000},
    "scaling_max_khz": {"0": 2400000, "1": 2400000, "2": 2400000, "3": 2400000},
    "no_turbo": 0,
    "swap_bytes": 0,
    "perf_version": "perf version 6.8.12",
    "lka_helper": "1",
    "nixpkgs": "0ad6f47e",
}

MACHINE = {
    "name": "n100-quiet",
    "housekeeping": [0],
    "lanes": {"4core": [0, 1, 2, 3], "1core": [3]},
    "frequency": {"pinned_hz": 2_400_000_000, "tolerance": 0.005, "min_task_clock_s": 0.2},
}


class Fingerprint(unittest.TestCase):
    def test_stable_under_key_order(self):
        shuffled = dict(reversed(list(FACTS.items())))
        self.assertEqual(fingerprint(FACTS), fingerprint(shuffled))

    def test_every_fact_matters(self):
        base = fingerprint(FACTS)
        for key in FACTS:
            changed = dict(FACTS)
            changed[key] = "something else"
            self.assertNotEqual(fingerprint(changed), base, key)

    def test_epoch_names_the_machine(self):
        epoch = epoch_of(MACHINE, FACTS)
        self.assertTrue(epoch.startswith("n100-quiet-"))
        self.assertEqual(len(epoch), len("n100-quiet-") + 16)


class State(unittest.TestCase):
    def test_a_quiet_machine_passes(self):
        state = {"load1": 0.02, "busy_processes": [], "governors": FACTS["governor"],
                 "cur_khz": {"0": 2400000, "1": 2400000, "2": 2400000, "3": 2400000},
                 "swap_bytes": 0, "throttle_total": 0, "temperature_c": 48.0}
        self.assertEqual(check_state(state, MACHINE), [])

    def test_load_and_competitors(self):
        state = {"load1": 1.4, "busy_processes": [("cargo", 93.0)], "governors": FACTS["governor"],
                 "cur_khz": {"0": 2400000, "1": 2400000, "2": 2400000, "3": 2400000},
                 "swap_bytes": 0, "throttle_total": 0, "temperature_c": 48.0}
        problems = check_state(state, MACHINE)
        self.assertTrue(any("load" in p for p in problems))
        self.assertTrue(any("cargo" in p for p in problems))

    def test_unpinned_clock(self):
        state = {"load1": 0.0, "busy_processes": [], "governors": {**FACTS["governor"], "2": "powersave"},
                 "cur_khz": {"0": 2400000, "1": 2400000, "2": 800000, "3": 2400000},
                 "swap_bytes": 0, "throttle_total": 0, "temperature_c": 48.0}
        problems = check_state(state, MACHINE)
        self.assertTrue(any("governor" in p and "cpu2" in p for p in problems), problems)
        self.assertTrue(any("frequency" in p and "cpu2" in p for p in problems), problems)

    def test_swap_and_heat(self):
        state = {"load1": 0.0, "busy_processes": [], "governors": FACTS["governor"],
                 "cur_khz": {"0": 2400000, "1": 2400000, "2": 2400000, "3": 2400000},
                 "swap_bytes": 1 << 30, "throttle_total": 0, "temperature_c": 91.0}
        problems = check_state(state, MACHINE)
        self.assertTrue(any("swap" in p for p in problems))
        self.assertTrue(any("temperature" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
