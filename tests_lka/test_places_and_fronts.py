"""A category's places, as a competition reads them, and the Pareto layers, which need no weights.

A place follows the point estimate on the season's own suite: the board's order, never skipping
back. Two neighbours share a place only when the noise of their runs cannot separate them; what
a different draw of tests would do is the rank range beside it, not the place."""

import itertools
import random
import unittest

from lka_quiet.board import pareto_layers, places, score_board
from tests_lka.test_board import LANES, SCORING, TESTS, board_for, checker_runs, entry, run


def overall(board, name, key=None):
    result = entry(board, name)["categories"]["overall"]
    return result if key is None else result[key]


def specialist(name, fast_test, even_test, fast_wall=0.2):
    over = {}
    for lane in LANES:
        over[(fast_test, lane)] = [run(name, fast_test, lane, wall=fast_wall)] * 3
        over[(even_test, lane)] = [run(name, even_test, lane, wall=2.0)] * 3
    return checker_runs(name, scale=0.5, overrides=over, jitter=0.0)


class Places(unittest.TestCase):
    def test_places_follow_the_estimates_where_rank_sets_do_not(self):
        def wide(name):
            over = {}
            for lane in LANES:
                over[("perf/a", lane)] = [run(name, "perf/a", lane, wall=2.0 * 0.05)] * 3
                over[("perf/b", lane)] = [run(name, "perf/b", lane, wall=2.0 * 1.0)] * 3
            return checker_runs(name, scale=0.5, overrides=over, jitter=0.0)

        board = board_for(checker_runs("official", jitter=0.0),
                          checker_runs("a", scale=0.1, mem_scale=0.1, energy_scale=0.1, jitter=0.0),
                          checker_runs("b", scale=0.5, mem_scale=0.5, energy_scale=0.5, jitter=0.0),
                          checker_runs("y", scale=0.505, mem_scale=0.505, energy_scale=0.505, jitter=0.0),
                          wide("x"))
        self.assertEqual({n: overall(board, n, "place") for n in "abyx"}, {"a": 1, "b": 2, "y": 3, "x": 4})
        self.assertEqual({n: overall(board, n, "rank") for n in "abyx"}, {"a": 1, "b": 2, "y": 3, "x": 2})

    def test_runs_that_overlap_share_a_place(self):
        board = board_for(checker_runs("official"), checker_runs("a", scale=0.5, jitter=0.05),
                          checker_runs("b", scale=0.501, jitter=0.05), checker_runs("c", scale=0.1, jitter=0.05))
        self.assertEqual([overall(board, n, "place") for n in "cab"], [1, 2, 2])
        self.assertEqual([overall(board, n, "place_tied") for n in "cab"], [False, True, True])

    def test_a_different_draw_of_tests_does_not_share_a_place(self):
        board = board_for(checker_runs("official", jitter=0.0), specialist("a", "perf/a", "perf/b", fast_wall=0.19),
                          specialist("b", "perf/b", "perf/a"))
        self.assertEqual((overall(board, "a", "place"), overall(board, "b", "place")), (1, 2))
        self.assertFalse(overall(board, "a", "place_tied"))
        self.assertEqual((overall(board, "b", "rank_lo"), overall(board, "b", "rank_hi")), (1, 2))

    def test_identical_checkers_share_a_place(self):
        board = board_for(checker_runs("official"), checker_runs("a", scale=0.5), checker_runs("b", scale=0.5))
        self.assertEqual((overall(board, "a", "place"), overall(board, "b", "place")), (1, 1))
        self.assertTrue(overall(board, "a", "place_tied") and overall(board, "b", "place_tied"))

    def test_places_never_go_back_in_any_category(self):
        rng = random.Random(7)
        lists = [checker_runs("official")]
        for name in "abcdefg":
            lists.append(checker_runs(name, scale=rng.uniform(0.2, 1.5), mem_scale=rng.uniform(0.2, 1.5),
                                      energy_scale=rng.uniform(0.2, 1.5), jitter=rng.choice([0.0, 0.01, 0.05])))
        board = board_for(*lists)
        for category in board["categories"]:
            placed = [entry(board, n)["categories"][category["id"]]["place"] for n in category["order"]
                      if entry(board, n)["categories"][category["id"]]["place"] is not None]
            with self.subTest(category=category["id"]):
                self.assertEqual(placed, sorted(placed))
                self.assertEqual(placed[0], 1)
                for i, p in enumerate(placed):
                    self.assertIn(p, (placed[i - 1] if i else 1, i + 1))

    def test_the_unranked_have_no_place(self):
        board = board_for(checker_runs("official"), checker_runs("a", scale=0.5))
        self.assertIsNone(overall(board, "official", "place"))
        self.assertEqual(overall(board, "a", "place"), 1)

    def test_the_rank_sets_are_untouched(self):
        board = board_for(checker_runs("official", jitter=0.0), specialist("a", "perf/a", "perf/b"),
                          specialist("b", "perf/b", "perf/a"))
        for name in "ab":
            self.assertEqual((overall(board, name, "rank"), overall(board, name, "rank_lo"), overall(board, name, "rank_hi")),
                             (1, 1, 2))


class PlacesFromProbabilities(unittest.TestCase):
    """places(order, p, confidence): down the order, a checker shares the place above it unless the
    one above beats it in at least 1 - alpha / (2 (N - 1)) of the measurement-noise replicates."""

    def test_threshold_is_bonferroni_over_the_neighbouring_comparisons(self):
        p = {"a": {"b": 0.98}, "b": {"c": 0.99}}
        self.assertEqual(places(["a", "b", "c"], p, 0.95), {"a": (1, True), "b": (1, True), "c": (3, False)})

    def test_a_certain_order_is_one_place_each(self):
        p = {"a": {"b": 1.0}, "b": {"c": 1.0}}
        self.assertEqual(places(["a", "b", "c"], p, 0.95), {"a": (1, False), "b": (2, False), "c": (3, False)})

    def test_a_chain_of_ties_shares_one_place(self):
        p = {"a": {"b": 0.6}, "b": {"c": 0.6}, "c": {"d": 1.0}}
        self.assertEqual(places(list("abcd"), p, 0.95),
                         {"a": (1, True), "b": (1, True), "c": (1, True), "d": (4, False)})

    def test_one_checker_is_first_alone(self):
        self.assertEqual(places(["a"], {}, 0.95), {"a": (1, False)})


class Layers(unittest.TestCase):
    def test_non_dominated_sorting(self):
        vectors = {"a": (1, 4), "b": (2, 2), "c": (4, 1), "d": (3, 3), "e": (5, 5), "f": (2, 2)}
        self.assertEqual(pareto_layers(vectors), {"a": 1, "b": 1, "c": 1, "f": 1, "d": 2, "e": 3})

    def test_layers_are_antichains_each_covered_by_the_one_before(self):
        rng = random.Random(11)
        for _ in range(200):
            vectors = {f"c{i}": tuple(rng.choice([1, 2, 3, 4, 5]) for _ in range(3)) for i in range(rng.randint(1, 12))}
            layers = pareto_layers(vectors)

            def dominates(u, v):
                return all(a <= b for a, b in zip(u, v)) and any(a < b for a, b in zip(u, v))

            for a, b in itertools.permutations(vectors, 2):
                if layers[a] == layers[b]:
                    self.assertFalse(dominates(vectors[a], vectors[b]))
            for name, layer in layers.items():
                if layer > 1:
                    self.assertTrue(any(dominates(vectors[o], vectors[name]) for o in vectors if layers[o] == layer - 1))

    def test_the_board_places_each_ranked_checker_on_a_layer(self):
        board = board_for(checker_runs("official"), checker_runs("fast", scale=0.2, mem_scale=2.0),
                          checker_runs("lean", scale=0.8, mem_scale=0.2), checker_runs("worse", scale=0.9, mem_scale=2.5),
                          checker_runs("worst", scale=1.2, mem_scale=3.0, energy_scale=1.5))
        layers = {n: entry(board, n)["pareto_layer"] for n in ("fast", "lean", "worse", "worst", "official")}
        self.assertEqual(layers, {"fast": 1, "lean": 1, "worse": 2, "worst": 3, "official": None})
        self.assertEqual({n: entry(board, n)["pareto"] for n in ("fast", "lean", "worse")},
                         {"fast": True, "lean": True, "worse": False})

    def test_layers_use_the_pareto_categories_that_were_measured(self):
        lists = [checker_runs("official"), checker_runs("fast", scale=0.2, mem_scale=2.0),
                 checker_runs("lean", scale=0.8, mem_scale=0.2), checker_runs("worse", scale=0.9, mem_scale=2.5)]
        records = [{**r, "energy_j": None} for runs in lists for r in runs]
        board = score_board(TESTS, [{"name": n} for n in ("official", "fast", "lean", "worse")], records, SCORING)
        self.assertNotIn("energy", [c["id"] for c in board["categories"]])
        self.assertEqual({n: entry(board, n)["pareto_layer"] for n in ("fast", "lean", "worse")},
                         {"fast": 1, "lean": 1, "worse": 2})


class TestByTest(unittest.TestCase):
    def test_every_test_has_its_verdict_bad_and_corner_cases_too(self):
        over = {("bugs/bad1", "4core"): [run("a", "bugs/bad1", "4core", status="declined")] * 3}
        board = board_for(checker_runs("official"), checker_runs("a", scale=0.5, overrides=over))
        verdicts = entry(board, "a")["verdicts"]
        self.assertEqual(set(verdicts), {t["name"] for t in TESTS})
        self.assertEqual(verdicts["bugs/bad1"], "declined")
        self.assertEqual(verdicts["mathlib"], "accepted")
        self.assertEqual(entry(board, "official")["verdicts"]["corner-cases/c1"], "accepted")

    def test_correctness_counts_as_the_arena_tabulates_them(self):
        over = {("bugs/bad1", "4core"): [run("a", "bugs/bad1", "4core", status="accepted")] * 3,
                ("std", "4core"): [run("a", "std", "4core", status="rejected")] * 3,
                ("init", "4core"): [run("a", "init", "4core", status="timeout")] * 1,
                ("corner-cases/c1", "4core"): [run("a", "corner-cases/c1", "4core", status="error")] * 2}
        board = board_for(checker_runs("official"), checker_runs("a", overrides=over))
        c = entry(board, "a")["correctness"]
        self.assertEqual((c["bad_rejected"], c["bad_accepted"], c["bad_declined"]), (0, 1, 0))
        self.assertEqual((c["good_accepted"], c["good_rejected"], c["good_declined"]), (5, 1, 1))
        self.assertEqual((c["either_answered"], c["either_declined"]), (0, 1))
        self.assertEqual(c["declined_or_failed"], 2)

    def test_a_scored_cell_carries_its_runs_spread(self):
        over = {("std", "4core"): [run("a", "std", "4core", wall=w) for w in (1.0, 1.2, 1.1)]}
        board = board_for(checker_runs("official"), checker_runs("a", overrides=over))
        cell = entry(board, "a")["tests"]["std"]["T4"]
        self.assertEqual((cell["value"], cell["lo"], cell["hi"], cell["n"]), (1.1, 1.0, 1.2, 3))

    def test_a_failed_cell_has_no_spread(self):
        over = {("std", "4core"): [run("a", "std", "4core", status="timeout")] * 2}
        board = board_for(checker_runs("official"), checker_runs("a", overrides=over))
        cell = entry(board, "a")["tests"]["std"]["T4"]
        self.assertTrue(cell["failed"])
        self.assertEqual((cell["lo"], cell["hi"], cell["n"]), (None, None, 0))


if __name__ == "__main__":
    unittest.main()
