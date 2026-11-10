"""Reading `perf stat -j` output: hybrid PMU names fold together, nothing multiplexed slips through."""

import math
import unittest
from pathlib import Path

from lka_quiet.perf import COUNTERS, PerfParseError, counters_from_perf, parse_perf_json

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class ParseEvents(unittest.TestCase):
    def test_hybrid_pmus_are_summed_into_one_event(self):
        events = parse_perf_json(fixture("perf-hybrid-i9-14900k.json"))
        self.assertEqual(events["instructions:u"].value, 249782291)
        self.assertEqual(events["instructions:k"].value, 917973)
        self.assertEqual(events["cycles:u"].value, 107209029)
        self.assertEqual(events["cycles:k"].value, 4137081)

    def test_a_pmu_that_never_ran_does_not_count_as_multiplexing(self):
        # On a hybrid CPU the idle PMU reports `<not counted>` with 0% running: the task was
        # never scheduled there, which is not a sampled estimate.
        events = parse_perf_json(fixture("perf-hybrid-i9-14900k.json"))
        self.assertEqual(events["instructions:u"].running_fraction, 1.0)

    def test_a_task_split_over_core_types_is_unscaled_and_summed(self):
        # One process pinned to a P-core and an identical one to an E-core. perf scales each
        # PMU's count by enabled/running (here ~46% and ~54%); the true total is the sum of
        # value * event-runtime / enabled, enabled being the tasks' run time (task-clock's).
        text = fixture("perf-hybrid-two-pmus.json")
        events = parse_perf_json(text)
        atom = 1633762426 * 62405322 / 135654234
        core = 1393015827 * 73248912 / 135654234
        self.assertAlmostEqual(events["instructions:u"].value, atom + core, delta=1)
        self.assertEqual(events["instructions:u"].running_fraction, 1.0)
        # the two processes ran the same loop: their unscaled counts agree
        self.assertAlmostEqual(atom / core, 1.0, delta=0.002)

    def test_core_type_split_is_not_multiplexing(self):
        _, problems = counters_from_perf(parse_perf_json(fixture("perf-hybrid-two-pmus.json")))
        self.assertFalse(any(p.startswith("multiplexed") for p in problems), problems)

    def test_time_units_become_seconds(self):
        events = parse_perf_json(fixture("perf-hybrid-i9-14900k.json"))
        self.assertAlmostEqual(events["duration_time"].value, 0.024116482)
        self.assertAlmostEqual(events["task-clock"].value, 0.020971735)

    def test_plain_pmu_names(self):
        events = parse_perf_json(fixture("perf-plain-synthesized.json"))
        self.assertEqual(events["instructions:u"].value, 9_100_000_000)
        self.assertEqual(events["page-faults"].value, 250123)

    def test_unsupported_counter_is_absent(self):
        events = parse_perf_json(fixture("perf-plain-multiplexed-synthesized.json"))
        self.assertNotIn("cycles:k", events)

    def test_running_fraction_reports_multiplexing(self):
        events = parse_perf_json(fixture("perf-plain-multiplexed-synthesized.json"))
        self.assertEqual(events["instructions:u"].running_fraction, 0.5)

    def test_non_json_lines_are_ignored(self):
        text = "# started on Mon\n\n" + fixture("perf-plain-synthesized.json")
        self.assertIn("cycles:u", parse_perf_json(text))

    def test_empty_output_is_an_error(self):
        with self.assertRaises(PerfParseError):
            parse_perf_json("")


class Counters(unittest.TestCase):
    def test_full_set(self):
        counters, problems = counters_from_perf(parse_perf_json(fixture("perf-plain-synthesized.json")))
        self.assertEqual(problems, [])
        self.assertEqual(set(counters), set(COUNTERS))
        self.assertEqual(counters["instructions_u"], 9_100_000_000)
        self.assertEqual(counters["cycles_k"], 50_000_000)
        self.assertAlmostEqual(counters["task_clock_s"], 2.987654321)
        self.assertEqual(counters["major_faults"], 0)

    def test_multiplexed_event_is_a_problem(self):
        _, problems = counters_from_perf(parse_perf_json(fixture("perf-plain-multiplexed-synthesized.json")))
        self.assertIn("multiplexed: instructions:u ran 50.0% of the time", problems)

    def test_missing_events_are_problems(self):
        counters, problems = counters_from_perf(parse_perf_json(fixture("perf-plain-multiplexed-synthesized.json")))
        self.assertIn("not counted: cycles:k", problems)
        self.assertIn("not counted: context-switches", problems)
        self.assertIsNone(counters["cycles_k"])

    def test_counts_are_integers(self):
        counters, _ = counters_from_perf(parse_perf_json(fixture("perf-hybrid-i9-14900k.json")))
        for name in ("instructions_u", "instructions_k", "cycles_u", "cycles_k", "page_faults"):
            self.assertIsInstance(counters[name], int, name)
        self.assertTrue(math.isfinite(counters["task_clock_s"]))


if __name__ == "__main__":
    unittest.main()
