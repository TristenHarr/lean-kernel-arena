"""Entering and leaving the quiet state: every change planned from a snapshot, every change undone."""

import unittest

from lka_quiet.quiet import cpu_mask, plan_enter, plan_leave

MACHINE = {
    "housekeeping": [0],
    "lanes": {"4core": [0, 1, 2, 3], "1core": [3]},
    "frequency": {"pinned_hz": 2_400_000_000},
}

SNAPSHOT = {
    "cpus": [0, 1, 2, 3],
    "governor": {"0": "powersave", "1": "powersave", "2": "powersave", "3": "powersave"},
    "epp": {"0": "balance_performance", "1": "balance_performance", "2": "balance_performance", "3": "balance_performance"},
    "min_khz": {"0": 700000, "1": 700000, "2": 700000, "3": 700000},
    "max_khz": {"0": 3400000, "1": 3400000, "2": 3400000, "3": 3400000},
    "irq_affinity": {"120": "f", "121": "f", "0": "1"},
    "default_irq_affinity": "f",
    "allowed_cpus": {"system.slice": "", "user.slice": "", "init.scope": ""},
    "active_timers": ["apt-daily.timer", "apt-daily-upgrade.timer", "fstrim.timer"],
    "swaps": ["/swap.img"],
}


def writes(actions):
    return {a["path"]: a["value"] for a in actions if a["kind"] == "write"}


def commands(actions):
    return [a["argv"] for a in actions if a["kind"] == "run"]


class Masks(unittest.TestCase):
    def test_hex_masks(self):
        self.assertEqual(cpu_mask([0]), "1")
        self.assertEqual(cpu_mask([0, 1, 2, 3]), "f")
        self.assertEqual(cpu_mask([3]), "8")
        self.assertEqual(cpu_mask([0, 9]), "201")


class Enter(unittest.TestCase):
    def test_clock_is_pinned_on_every_cpu(self):
        w = writes(plan_enter(SNAPSHOT, MACHINE))
        for cpu in range(4):
            base = f"/sys/devices/system/cpu/cpu{cpu}/cpufreq"
            self.assertEqual(w[f"{base}/scaling_governor"], "performance")
            self.assertEqual(w[f"{base}/scaling_max_freq"], "2400000")
            self.assertEqual(w[f"{base}/scaling_min_freq"], "2400000")
            self.assertEqual(w[f"{base}/energy_performance_preference"], "performance")

    def test_max_is_lowered_before_min_is_raised_or_the_kernel_refuses(self):
        paths = [a["path"] for a in plan_enter(SNAPSHOT, MACHINE) if a["kind"] == "write"]
        for cpu in range(4):
            base = f"/sys/devices/system/cpu/cpu{cpu}/cpufreq"
            self.assertLess(paths.index(f"{base}/scaling_max_freq"), paths.index(f"{base}/scaling_min_freq"))

    def test_interrupts_go_to_housekeeping(self):
        w = writes(plan_enter(SNAPSHOT, MACHINE))
        self.assertEqual(w["/proc/irq/120/smp_affinity"], "1")
        self.assertEqual(w["/proc/irq/default_smp_affinity"], "1")
        self.assertNotIn("/proc/irq/0/smp_affinity", w, "already on housekeeping")

    def test_system_is_confined_to_housekeeping(self):
        runs = commands(plan_enter(SNAPSHOT, MACHINE))
        for unit in ("system.slice", "user.slice", "init.scope"):
            self.assertIn(["systemctl", "set-property", "--runtime", unit, "AllowedCPUs=0"], runs)

    def test_timers_stopped_swap_off_latency_held(self):
        runs = commands(plan_enter(SNAPSHOT, MACHINE))
        self.assertIn(["systemctl", "stop", "apt-daily.timer"], runs)
        self.assertIn(["swapoff", "/swap.img"], runs)
        self.assertTrue(any(r[:2] == ["systemd-run", "--unit=lka-cpu-latency"] for r in runs))


class Leave(unittest.TestCase):
    def test_leave_restores_the_snapshot(self):
        w = writes(plan_leave(SNAPSHOT))
        base = "/sys/devices/system/cpu/cpu2/cpufreq"
        self.assertEqual(w[f"{base}/scaling_governor"], "powersave")
        self.assertEqual(w[f"{base}/scaling_max_freq"], "3400000")
        self.assertEqual(w[f"{base}/scaling_min_freq"], "700000")
        self.assertEqual(w["/proc/irq/120/smp_affinity"], "f")
        runs = commands(plan_leave(SNAPSHOT))
        self.assertIn(["systemctl", "set-property", "--runtime", "system.slice", "AllowedCPUs="], runs)
        self.assertIn(["systemctl", "start", "apt-daily.timer"], runs)
        self.assertIn(["swapon", "/swap.img"], runs)
        self.assertIn(["systemctl", "stop", "lka-cpu-latency"], runs)

    def test_min_is_lowered_before_max_is_raised(self):
        paths = [a["path"] for a in plan_leave(SNAPSHOT) if a["kind"] == "write"]
        base = "/sys/devices/system/cpu/cpu0/cpufreq"
        self.assertLess(paths.index(f"{base}/scaling_min_freq"), paths.index(f"{base}/scaling_max_freq"))

    def test_entering_twice_still_restores_the_first_snapshot(self):
        from lka_quiet.quiet import snapshot_to_keep
        quiet_now = {**SNAPSHOT, "governor": {str(c): "performance" for c in range(4)}}
        self.assertIs(snapshot_to_keep(existing=SNAPSHOT, current=quiet_now), SNAPSHOT)
        self.assertIs(snapshot_to_keep(existing=None, current=quiet_now), quiet_now)


if __name__ == "__main__":
    unittest.main()
