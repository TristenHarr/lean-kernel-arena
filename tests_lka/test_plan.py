"""Which runs come next: randomized interleaved passes, adaptive stopping, scaled timeouts."""

import unittest

from lka_quiet.plan import Cell, cell_state, consensus_status, pending_cells, plan_pass, scaled_timeout

POLICY = {
    "scored": {"n_min": 7, "n_max": 15, "ci_rel": 0.01, "confidence": 0.95},
    "heavy": {"threshold_s": 60, "n_min": 3, "n_max": 5, "spread_rel": 0.01},
    "verdict_only": {"n": 3},
    "failure": {"n": 2},
}


def runs(walls, status="accepted"):
    return [{"status": status, "wall_s": w, "valid": True} for w in walls]


class State(unittest.TestCase):
    def test_no_runs_needs_more(self):
        self.assertEqual(cell_state([], "accept", POLICY)[0], "more")

    def test_small_test_needs_seven(self):
        self.assertEqual(cell_state(runs([1.0] * 6), "accept", POLICY)[0], "more")
        self.assertEqual(cell_state(runs([1.0] * 7), "accept", POLICY)[0], "done")

    def test_small_test_continues_while_the_interval_is_wide(self):
        walls = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.1]
        self.assertEqual(cell_state(runs(walls), "accept", POLICY)[0], "more")

    def test_small_test_stops_at_the_cap(self):
        walls = [1.0, 1.2] * 8
        state, reason = cell_state(runs(walls[:15]), "accept", POLICY)
        self.assertEqual(state, "done")
        self.assertIn("cap", reason)

    def test_heavy_test_needs_three_tight_runs(self):
        self.assertEqual(cell_state(runs([100.0, 100.5]), "accept", POLICY)[0], "more")
        self.assertEqual(cell_state(runs([100.0, 100.5, 100.2]), "accept", POLICY)[0], "done")

    def test_heavy_test_with_spread_runs_more_up_to_five(self):
        self.assertEqual(cell_state(runs([100.0, 103.0, 100.2]), "accept", POLICY)[0], "more")
        self.assertEqual(cell_state(runs([100.0, 103.0, 100.2, 99.0, 104.0]), "accept", POLICY)[0], "done")

    def test_a_timeout_is_final(self):
        state, reason = cell_state(runs([600.0], status="timeout"), "accept", POLICY)
        self.assertEqual(state, "done")
        self.assertIn("timeout", reason)

    def test_bad_tests_need_three_verdicts(self):
        self.assertEqual(cell_state(runs([0.1, 0.1], status="rejected"), "reject", POLICY)[0], "more")
        self.assertEqual(cell_state(runs([0.1] * 3, status="rejected"), "reject", POLICY)[0], "done")

    def test_either_tests_are_verdict_only(self):
        self.assertEqual(cell_state(runs([0.1] * 3, status="accepted"), "either", POLICY)[0], "done")

    def test_consistent_failure_on_a_good_test_stops_at_two(self):
        self.assertEqual(cell_state(runs([1.0], status="error"), "accept", POLICY)[0], "more")
        self.assertEqual(cell_state(runs([1.0, 1.0], status="error"), "accept", POLICY)[0], "done")

    def test_mixed_verdicts_keep_measuring(self):
        mixed = runs([1.0] * 3) + runs([1.0], status="error")
        self.assertEqual(cell_state(mixed, "accept", POLICY)[0], "more")

    def test_invalid_runs_do_not_count(self):
        cell_runs = runs([1.0] * 7)
        for r in cell_runs[:3]:
            r["valid"] = False
        self.assertEqual(cell_state(cell_runs, "accept", POLICY)[0], "more")


class Consensus(unittest.TestCase):
    def test_majority(self):
        self.assertEqual(consensus_status(runs([1] * 3) + runs([1], status="error")), "accepted")

    def test_tie_goes_to_the_failure(self):
        self.assertEqual(consensus_status(runs([1]) + runs([1], status="timeout")), "timeout")

    def test_empty(self):
        self.assertIsNone(consensus_status([]))


class Passes(unittest.TestCase):
    CELLS = [Cell(c, t, l) for c in ("a", "b", "c") for t in ("x", "y") for l in ("1core", "4core")]

    def test_a_pass_is_a_permutation(self):
        order = plan_pass(self.CELLS, "round-1", 0)
        self.assertEqual(sorted(order), sorted(self.CELLS))

    def test_deterministic_and_input_order_independent(self):
        self.assertEqual(plan_pass(self.CELLS, "round-1", 0), plan_pass(list(reversed(self.CELLS)), "round-1", 0))

    def test_each_pass_reorders(self):
        self.assertNotEqual(plan_pass(self.CELLS, "round-1", 0), plan_pass(self.CELLS, "round-1", 1))

    def test_rounds_differ(self):
        self.assertNotEqual(plan_pass(self.CELLS, "round-1", 0), plan_pass(self.CELLS, "round-2", 0))

    def test_no_checker_is_systematically_first(self):
        firsts = {plan_pass(self.CELLS, "round-1", i)[0].checker for i in range(40)}
        self.assertEqual(firsts, {"a", "b", "c"})

    def test_pending_cells_skip_done_cells(self):
        expected = {("x"): "accept", ("y"): "reject"}
        records = {Cell("a", "x", "1core"): runs([1.0] * 7), Cell("a", "y", "1core"): runs([0.1] * 3, status="rejected")}
        cells = [Cell("a", "x", "1core"), Cell("a", "y", "1core"), Cell("a", "x", "4core")]
        self.assertEqual(pending_cells(cells, records, expected, POLICY), [Cell("a", "x", "4core")])


class Timeouts(unittest.TestCase):
    MACHINE = {"timeouts": {"box_factor": 2.5, "default_s": 21600, "minimum_s": 30}}

    def test_scaled(self):
        self.assertEqual(scaled_timeout(100, None, self.MACHINE), 250)

    def test_smallest_of_test_and_checker(self):
        self.assertEqual(scaled_timeout(100, 40, self.MACHINE), 100)

    def test_default_when_the_arena_sets_none(self):
        self.assertEqual(scaled_timeout(None, None, self.MACHINE), 21600)

    def test_minimum(self):
        self.assertEqual(scaled_timeout(5, None, self.MACHINE), 30)


if __name__ == "__main__":
    unittest.main()
