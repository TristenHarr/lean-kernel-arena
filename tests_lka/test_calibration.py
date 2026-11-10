"""Calibration decisions: the pinned clock, the sentinel's limits, the box factor, the profile written."""

import unittest

from lka_quiet.calibration import (
    box_factor,
    choose_frequency,
    noise_summary,
    sentinel_limits,
    write_calibration,
)


def soak(freq_khz, throttles=0, temps=(60, 70, 75), effective=None):
    return {"freq_khz": freq_khz, "throttle_events": throttles, "max_temperature_c": max(temps),
            "effective_khz": effective if effective is not None else [freq_khz] * 10}


class Frequency(unittest.TestCase):
    def test_the_highest_clean_frequency_wins(self):
        results = [soak(3400000, throttles=3), soak(3000000, throttles=0, temps=(82, 86)),
                   soak(2800000), soak(2400000)]
        self.assertEqual(choose_frequency(results, max_temperature_c=80.0), 2800000)

    def test_a_clock_that_sags_is_not_held(self):
        results = [soak(3000000, effective=[3000000] * 8 + [2900000, 2850000]), soak(2600000)]
        self.assertEqual(choose_frequency(results, max_temperature_c=85.0), 2600000)

    def test_nothing_clean_is_an_error(self):
        with self.assertRaises(ValueError):
            choose_frequency([soak(2000000, throttles=1)], max_temperature_c=85.0)


class Sentinel(unittest.TestCase):
    def test_mean_sigma_and_k(self):
        limits = sentinel_limits([1.00, 1.01, 0.99, 1.00, 1.02, 0.98] * 5, k=4)
        self.assertAlmostEqual(limits["mean_s"], 1.0, places=6)
        self.assertAlmostEqual(limits["sigma_s"], (0.005 / 29) ** 0.5, places=9)  # sample standard deviation
        self.assertEqual(limits["k"], 4)

    def test_needs_enough_readings(self):
        with self.assertRaises(ValueError):
            sentinel_limits([1.0] * 5, k=4)


class BoxFactor(unittest.TestCase):
    def test_median_ratio_rounded_up_to_a_tenth(self):
        here = {"init-prelude": 0.25, "init": 210.0, "std": 380.0}
        there = {"init-prelude": 0.1, "init": 100.0, "std": 160.0, "mathlib": 3600.0}
        self.assertEqual(box_factor(here, there), 2.4)

    def test_never_below_one(self):
        self.assertEqual(box_factor({"a": 1.0}, {"a": 2.0}), 1.0)


class Noise(unittest.TestCase):
    def test_coefficient_of_variation_and_spread(self):
        summary = noise_summary([1.00, 1.01, 0.99, 1.00])
        self.assertAlmostEqual(summary["cv"], 0.008165, places=5)
        self.assertAlmostEqual(summary["spread"], 0.02, places=6)
        self.assertEqual(summary["n"], 4)


class Write(unittest.TestCase):
    PROFILE = """frequency:
  pinned_hz: null            # calibrated: the highest frequency held on all cores for 2 h unthrottled
  tolerance: 0.005
timeouts:
  box_factor: null           # calibrated: official's wall time here over its wall time on the arena's runner
  default_s: 300
sentinel:
  checker: official
  limits: null               # calibrated: mean and 3-sigma band of the sentinel's wall time
"""

    def test_values_replace_the_nulls_and_keep_the_comments(self):
        out = write_calibration(self.PROFILE, pinned_hz=2_800_000_000, box_factor=2.4,
                                limits={"mean_s": 0.0412, "sigma_s": 0.0003, "k": 4})
        self.assertIn("  pinned_hz: 2800000000      # calibrated:", out)  # the comment keeps its column
        self.assertIn("  box_factor: 2.4", out)
        self.assertIn("  limits: {mean_s: 0.0412, sigma_s: 0.0003, k: 4}", out)
        self.assertIn("tolerance: 0.005", out)

    def test_a_missing_key_is_refused(self):
        with self.assertRaises(ValueError):
            write_calibration("frequency:\n  tolerance: 0.005\n", pinned_hz=1, box_factor=1.0,
                              limits={"mean_s": 1, "sigma_s": 0, "k": 4})


if __name__ == "__main__":
    unittest.main()
