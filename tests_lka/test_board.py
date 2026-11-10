"""The scoring: what a rank means, and that it means it for everyone."""

import copy
import json
import math
import unittest

from lka_quiet.board import BoardError, family_members, rank_sets, score_board, summarize_cells
from lka_quiet.plan import Cell

TESTS = [
    {"name": "mathlib", "outcome": "accept", "timeout": 100},
    {"name": "std", "outcome": "accept", "timeout": 100},
    {"name": "init", "outcome": "accept", "timeout": 100},
    {"name": "perf/a", "outcome": "accept", "timeout": 100},
    {"name": "perf/b", "outcome": "accept", "timeout": 100},
    {"name": "tutorial/good1", "outcome": "accept"},
    {"name": "tutorial/good2", "outcome": "accept"},
    {"name": "bugs/bad1", "outcome": "reject"},
    {"name": "corner-cases/c1", "outcome": "either"},
]

SCORING = {
    "schema": "lka-quiet-scoring/1",
    "season": "test-season",
    "reference": "official",
    "unranked": ["official", "parse-only"],
    "floors": {"wall_s": 0.02, "memory_bytes": 32 * 2**20, "energy_j": 0.1},
    "failure_factor": 2,
    "caps": {"memory_bytes": 15 * 2**30, "power_w": 25},
    "metrics": {
        "T4": {"lane": "4core", "quantity": "wall_s", "title": "Wall time, 4 cores"},
        "T1": {"lane": "1core", "quantity": "wall_s", "title": "Wall time, 1 core"},
        "M": {"lane": "4core", "quantity": "memory_bytes", "title": "Peak memory"},
        "E": {"lane": "4core", "quantity": "energy_j", "title": "Energy"},
    },
    "families": {
        "libraries": {"title": "Libraries", "tests": [{"name": "mathlib", "weight": 0.5}, "init", "std"]},
        "stress": {"title": "Stress", "tests": ["perf/*"]},
        "interactive": {"title": "Interactive", "tests": ["tutorial/*", "corner-cases/*", "bugs/*"]},
        "mathlib": {"title": "Mathlib", "tests": ["mathlib"]},
    },
    "categories": [
        {"id": "overall", "title": "Overall", "metrics": {"T4": 0.35, "T1": 0.25, "M": 0.25, "E": 0.15},
         "families": {"libraries": 0.5, "stress": 0.3, "interactive": 0.2}},
        {"id": "multi-core", "title": "Multi-core speed", "metrics": {"T4": 1},
         "families": {"libraries": 0.5, "stress": 0.3, "interactive": 0.2}},
        {"id": "single-core", "title": "Single-core speed", "metrics": {"T1": 1},
         "families": {"libraries": 0.5, "stress": 0.3, "interactive": 0.2}},
        {"id": "memory", "title": "Memory", "metrics": {"M": 1},
         "families": {"libraries": 0.5, "stress": 0.3, "interactive": 0.2}},
        {"id": "energy", "title": "Energy", "metrics": {"E": 1},
         "families": {"libraries": 0.5, "stress": 0.3, "interactive": 0.2}},
        {"id": "mathlib", "title": "Mathlib", "metrics": {"T4": 0.35, "T1": 0.25, "M": 0.25, "E": 0.15},
         "families": {"mathlib": 1}},
    ],
    "pareto": ["multi-core", "single-core", "memory", "energy"],
    "scaling": {"serial": "1core", "parallel": "4core", "parallel_cpus": 4, "category": "overall"},
    "bootstrap": {"replicates": 200, "seed": "test-season", "confidence": 0.95},
    "sensitivity": {"category": "overall", "factors": [0.5, 1.5]},
}

LANES = ("4core", "1core")


def run(checker, test, lane, status="accepted", wall=1.0, memory=100 * 2**20, energy=10.0, epoch="e1", valid=True, timeout=200.0,
        cap=None):
    return {
        "schema": "lka-quiet-run/1", "checker": checker, "test": test, "lane": lane, "status": status,
        "wall_s": wall, "memory_peak_bytes": memory, "energy_j": energy, "epoch": epoch, "valid": valid,
        "timeout_s": timeout, "cap_s": cap if cap is not None else timeout,
        "counters": {"instructions_u": int(wall * 1e9), "cycles_u": int(wall * 8e8)},
    }


def checker_runs(checker, scale=1.0, mem_scale=1.0, energy_scale=1.0, overrides=None, reps=3, single_core_scale=None,
                 jitter=0.001):
    """Runs of `checker` on every test, `scale` times the reference's wall time."""
    overrides = overrides or {}
    out = []
    for test in TESTS:
        for lane in LANES:
            key = (test["name"], lane)
            if key in overrides:
                out.extend(overrides[key])
                continue
            if test["outcome"] == "reject":
                status = "rejected"
            else:
                status = "accepted"
            lane_scale = scale if lane == "4core" or single_core_scale is None else single_core_scale
            for i in range(reps):
                factor = 1 + jitter * i
                out.append(run(checker, test["name"], lane, status, wall=lane_scale * 2.0 * factor,
                               memory=int(mem_scale * 200 * 2**20), energy=energy_scale * 20.0 * factor))
    return out


def board_for(*run_lists, scoring=SCORING):
    records = [r for runs in run_lists for r in runs]
    checkers = sorted({r["checker"] for r in records})
    return score_board(TESTS, [{"name": c} for c in checkers], records, scoring)


def entry(board, name):
    return next(c for c in board["checkers"] if c["name"] == name)


class Families(unittest.TestCase):
    def test_pinned_weight_and_equal_split(self):
        members = family_members(TESTS, SCORING["families"])
        libraries = dict(members["libraries"])
        self.assertAlmostEqual(libraries["mathlib"], 0.5)
        self.assertAlmostEqual(libraries["init"], 0.25)
        self.assertAlmostEqual(libraries["std"], 0.25)

    def test_only_accept_tests_are_scored(self):
        members = family_members(TESTS, SCORING["families"])
        names = [name for name, _ in members["interactive"]]
        self.assertEqual(names, ["tutorial/good1", "tutorial/good2"])

    def test_weights_sum_to_one(self):
        for family, members in family_members(TESTS, SCORING["families"]).items():
            self.assertAlmostEqual(math.fsum(w for _, w in members), 1.0, msg=family)

    def test_pinned_weights_above_one_are_refused(self):
        families = {"x": {"tests": [{"name": "mathlib", "weight": 0.7}, {"name": "std", "weight": 0.6}]}}
        with self.assertRaises(BoardError):
            family_members(TESTS, families)


class Geometry(unittest.TestCase):
    def test_uniformly_twice_as_fast_is_a_factor_of_two(self):
        board = board_for(checker_runs("official"), checker_runs("fast", scale=0.5, mem_scale=0.5, energy_scale=0.5))
        fast = entry(board, "fast")
        for category in ("overall", "multi-core", "single-core", "memory", "energy", "mathlib"):
            self.assertAlmostEqual(fast["categories"][category]["factor"], 2.0, places=2, msg=category)

    def test_reference_scores_zero(self):
        board = board_for(checker_runs("official"), checker_runs("fast", scale=0.5))
        self.assertAlmostEqual(entry(board, "official")["categories"]["overall"]["score"], 0.0, places=9)

    def test_ranking_does_not_depend_on_the_reference(self):
        runs = [checker_runs("official"), checker_runs("a", scale=0.5), checker_runs("b", scale=0.7, mem_scale=0.3),
                checker_runs("c", scale=1.3, mem_scale=0.9), checker_runs("d", scale=0.9, energy_scale=0.4)]
        board_official = board_for(*runs)
        alt = copy.deepcopy(SCORING)
        alt["reference"] = "c"
        board_c = board_for(*runs, scoring=alt)
        order = lambda board: [c["name"] for c in sorted(
            (c for c in board["checkers"] if c["name"] in "abcd"),
            key=lambda c: c["categories"]["overall"]["score"])]
        self.assertEqual(order(board_official), order(board_c))
        diffs = {c["name"]: c["categories"]["overall"]["score"] for c in board_official["checkers"]}
        alt_scores = {c["name"]: c["categories"]["overall"]["score"] for c in board_c["checkers"]}
        gaps = {name: diffs[name] - alt_scores[name] for name in "abcd"}
        self.assertAlmostEqual(max(gaps.values()) - min(gaps.values()), 0.0, places=9)

    def test_floors_erase_imperceptible_differences(self):
        tiny = {(t["name"], lane): [run("x", t["name"], lane, wall=0.001, memory=2**20, energy=0.01)] * 3
                for t in TESTS if t["outcome"] == "accept" for lane in LANES}
        tinier = {(t["name"], lane): [run("y", t["name"], lane, wall=0.0001, memory=2**10, energy=0.001)] * 3
                  for t in TESTS if t["outcome"] == "accept" for lane in LANES}
        board = board_for(checker_runs("official"), checker_runs("x", overrides=tiny), checker_runs("y", overrides=tinier))
        self.assertAlmostEqual(entry(board, "x")["categories"]["overall"]["score"],
                               entry(board, "y")["categories"]["overall"]["score"], places=12)


class Failures(unittest.TestCase):
    def test_a_timeout_scores_par2(self):
        timeout = {("mathlib", "4core"): [run("slow", "mathlib", "4core", status="timeout", wall=200.0, timeout=200.0)]}
        board = board_for(checker_runs("official"), checker_runs("slow", overrides=timeout))
        cell = entry(board, "slow")["tests"]["mathlib"]["T4"]
        self.assertTrue(cell["failed"])
        self.assertEqual(cell["value"], 400.0)

    def test_a_decline_is_a_failure_for_performance(self):
        declined = {("std", lane): [run("d", "std", lane, status="declined", wall=0.0)] for lane in LANES}
        board = board_for(checker_runs("official"), checker_runs("d", overrides=declined))
        self.assertTrue(entry(board, "d")["tests"]["std"]["T1"]["failed"])
        self.assertTrue(entry(board, "d")["ranked"])

    def test_memory_and_energy_penalties(self):
        crash = {("init", "4core"): [run("c", "init", "4core", status="error", wall=5.0, timeout=200.0)] * 2}
        board = board_for(checker_runs("official"), checker_runs("c", overrides=crash))
        tests = entry(board, "c")["tests"]["init"]
        self.assertEqual(tests["M"]["value"], 2 * 15 * 2**30)
        self.assertEqual(tests["E"]["value"], 2 * 25 * 200.0)


class NoGamingTheCap(unittest.TestCase):
    def test_a_short_self_declared_timeout_buys_nothing(self):
        # quitter's own 1 s limit kills it on mathlib; slow finishes in 150 s. The penalty is the
        # test's cap (200 s here), never the checker's own limit.
        quit_runs = {("mathlib", lane): [run("quitter", "mathlib", lane, status="timeout", wall=1.0, timeout=1.0, cap=200.0)]
                     for lane in LANES}
        slow_runs = {("mathlib", lane): [run("slow", "mathlib", lane, wall=150.0, cap=200.0)] * 3 for lane in LANES}
        board = board_for(checker_runs("official"), checker_runs("quitter", overrides=quit_runs),
                          checker_runs("slow", overrides=slow_runs))
        self.assertEqual(entry(board, "quitter")["tests"]["mathlib"]["T4"]["value"], 400.0)
        quitter = entry(board, "quitter")["categories"]["mathlib"]["score"]
        slow = entry(board, "slow")["categories"]["mathlib"]["score"]
        self.assertGreater(quitter, slow)


class MissingCaps(unittest.TestCase):
    def test_a_failure_without_a_cap_is_refused(self):
        declined = {("std", lane): [dict(run("d", "std", lane, status="declined", wall=0.0), cap_s=None, timeout_s=None)]
                    for lane in LANES}
        with self.assertRaisesRegex(BoardError, "std"):
            board_for(checker_runs("official"), checker_runs("d", overrides=declined))


class DisplayOrder(unittest.TestCase):
    def test_rows_follow_the_point_estimates_even_where_ranks_do_not(self):
        # x's estimate (1.90x) is below y's (1.98x), but x's interval is wide: only a beats x
        # significantly (rank 2), while b beats y significantly too (rank 3). Rows still go by estimate.
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
        ranks = {n: entry(board, n)["categories"]["overall"]["rank"] for n in "abxy"}
        self.assertEqual(ranks, {"a": 1, "b": 2, "x": 2, "y": 3})
        overall = next(c for c in board["categories"] if c["id"] == "overall")
        self.assertEqual(overall["order"][:4], ["a", "b", "y", "x"])


class Gates(unittest.TestCase):
    def test_accepting_a_bad_proof_once_is_unsound(self):
        bad = {("bugs/bad1", "1core"): [run("u", "bugs/bad1", "1core", status="rejected")] * 2
               + [run("u", "bugs/bad1", "1core", status="accepted")]}
        board = board_for(checker_runs("official"), checker_runs("u", scale=0.1, overrides=bad))
        u = entry(board, "u")
        self.assertEqual(u["gates"]["unsound"], ["bugs/bad1"])
        self.assertFalse(u["ranked"])
        self.assertIsNone(u["categories"]["overall"]["rank"])

    def test_wrongly_rejecting_a_good_proof_is_unranked(self):
        wrong = {("std", "4core"): [run("w", "std", "4core", status="rejected")] * 3}
        board = board_for(checker_runs("official"), checker_runs("w", overrides=wrong))
        self.assertEqual(entry(board, "w")["gates"]["wrong_rejects"], ["std"])
        self.assertFalse(entry(board, "w")["ranked"])

    def test_either_tests_never_gate(self):
        either = {("corner-cases/c1", lane): [run("e", "corner-cases/c1", lane, status="rejected")] * 3 for lane in LANES}
        board = board_for(checker_runs("official"), checker_runs("e", overrides=either))
        self.assertTrue(entry(board, "e")["ranked"])

    def test_reference_is_shown_but_not_ranked(self):
        board = board_for(checker_runs("official"), checker_runs("a", scale=0.5))
        self.assertFalse(entry(board, "official")["ranked"])
        self.assertEqual(entry(board, "a")["categories"]["overall"]["rank"], 1)


class Uncertainty(unittest.TestCase):
    def test_deterministic(self):
        runs = [checker_runs("official"), checker_runs("a", scale=0.5), checker_runs("b", scale=0.51)]
        self.assertEqual(json.dumps(board_for(*runs), sort_keys=True), json.dumps(board_for(*runs), sort_keys=True))

    def test_identical_checkers_tie(self):
        board = board_for(checker_runs("official"), checker_runs("a", scale=0.5), checker_runs("b", scale=0.5))
        a, b = entry(board, "a")["categories"]["overall"], entry(board, "b")["categories"]["overall"]
        self.assertEqual(a["rank"], 1)
        self.assertEqual(b["rank"], 1)
        self.assertTrue(a["tied"] and b["tied"])

    def test_a_clear_winner_is_certain(self):
        board = board_for(checker_runs("official"), checker_runs("a", scale=0.1, mem_scale=0.1, energy_scale=0.1),
                          checker_runs("b", scale=0.9))
        a = entry(board, "a")["categories"]["overall"]
        self.assertEqual((a["rank"], a["rank_lo"], a["rank_hi"]), (1, 1, 1))
        self.assertFalse(a["tied"])
        b = entry(board, "b")["categories"]["overall"]
        self.assertEqual(b["rank"], 2)

    def test_mirror_image_specialists_are_tied_with_wide_intervals(self):
        # a is 10x faster than the reference on perf/a and level on perf/b; b the reverse. Which
        # is first depends on which stress tests a re-drawn suite would hold.
        def specialist(name, fast_test, even_test):
            over = {}
            for lane in LANES:
                over[(fast_test, lane)] = [run(name, fast_test, lane, wall=0.2)] * 3
                over[(even_test, lane)] = [run(name, even_test, lane, wall=2.0)] * 3
            # no run-to-run noise: only redrawing the suite's tests can move the order
            return checker_runs(name, scale=0.5, overrides=over, jitter=0.0)

        board = board_for(checker_runs("official", jitter=0.0), specialist("a", "perf/a", "perf/b"),
                          specialist("b", "perf/b", "perf/a"))
        for name in ("a", "b"):
            overall = entry(board, name)["categories"]["overall"]
            self.assertEqual((overall["rank"], overall["rank_lo"], overall["rank_hi"]), (1, 1, 2), name)
            self.assertTrue(overall["tied"], name)
        p = board["head_to_head"]["overall"]["a"]["b"]
        self.assertGreater(p, 0.1)
        self.assertLess(p, 0.9)

    def test_probability_of_beating_is_reported(self):
        board = board_for(checker_runs("official"), checker_runs("a", scale=0.1), checker_runs("b", scale=0.9))
        self.assertEqual(board["head_to_head"]["overall"]["a"]["b"], 1.0)
        self.assertEqual(board["head_to_head"]["overall"]["b"]["a"], 0.0)


class FactorIntervals(unittest.TestCase):
    def test_interval_brackets_the_estimate(self):
        board = board_for(checker_runs("official"), checker_runs("a", scale=0.5, mem_scale=0.7),
                          checker_runs("b", scale=1.3, energy_scale=0.6))
        for name in ("a", "b"):
            for category in board["categories"]:
                result = entry(board, name)["categories"][category["id"]]
                self.assertLessEqual(result["factor_lo"], result["factor"] * (1 + 1e-9), (name, category["id"]))
                self.assertGreaterEqual(result["factor_hi"], result["factor"] * (1 - 1e-9), (name, category["id"]))

    def test_a_uniform_factor_has_a_tight_interval(self):
        board = board_for(checker_runs("official"), checker_runs("a", scale=0.5, mem_scale=0.5, energy_scale=0.5))
        overall = entry(board, "a")["categories"]["overall"]
        self.assertAlmostEqual(overall["factor_lo"], 2.0, delta=0.02)
        self.assertAlmostEqual(overall["factor_hi"], 2.0, delta=0.02)

    def test_mirror_specialists_have_wide_intervals(self):
        def specialist(name, fast_test, even_test):
            over = {}
            for lane in LANES:
                over[(fast_test, lane)] = [run(name, fast_test, lane, wall=0.2)] * 3
                over[(even_test, lane)] = [run(name, even_test, lane, wall=2.0)] * 3
            return checker_runs(name, scale=0.5, overrides=over, jitter=0.0)

        board = board_for(checker_runs("official", jitter=0.0), specialist("a", "perf/a", "perf/b"))
        stress = entry(board, "a")["categories"]["multi-core"]
        self.assertGreater(stress["factor_hi"] / stress["factor_lo"], 1.3)

    def test_the_reference_is_exactly_one(self):
        board = board_for(checker_runs("official"), checker_runs("a", scale=0.5))
        overall = entry(board, "official")["categories"]["overall"]
        self.assertEqual((overall["factor_lo"], overall["factor_hi"]), (1.0, 1.0))


class RankSets(unittest.TestCase):
    """Marginal confidence sets for ranks (Mogstad, Romano, Shaikh & Wilhelm, REStud 2024):
    rank(c) in [1 + #{d surely better}, N - #{d surely worse}], each pairwise decision taken at
    level 1 - alpha / (2 (N - 1)) so the set covers c's rank with probability 1 - alpha."""

    def test_threshold_is_bonferroni_over_the_comparisons_involving_one_checker(self):
        # three contenders: 1 - 0.05 / 4 = 0.9875; a beats b with 0.98, below it
        p = {"a": {"b": 0.98, "c": 1.0}, "b": {"a": 0.02, "c": 1.0}, "c": {"a": 0.0, "b": 0.0}}
        sets = rank_sets(p, ["a", "b", "c"], confidence=0.95)
        self.assertEqual(sets["a"], (1, 2))
        self.assertEqual(sets["b"], (1, 2))
        self.assertEqual(sets["c"], (3, 3))

    def test_a_certain_order_is_a_set_of_points(self):
        p = {"a": {"b": 1.0, "c": 1.0}, "b": {"a": 0.0, "c": 1.0}, "c": {"a": 0.0, "b": 0.0}}
        self.assertEqual(rank_sets(p, ["a", "b", "c"], confidence=0.95), {"a": (1, 1), "b": (2, 2), "c": (3, 3)})

    def test_nothing_known_is_every_rank(self):
        p = {x: {y: 0.5 for y in "abcd" if y != x} for x in "abcd"}
        self.assertEqual(rank_sets(p, list("abcd"), confidence=0.95), {x: (1, 4) for x in "abcd"})

    def test_board_ranks_lie_in_their_sets(self):
        board = board_for(checker_runs("official"), checker_runs("a", scale=0.3), checker_runs("b", scale=0.5, mem_scale=0.4),
                          checker_runs("c", scale=0.9, energy_scale=0.3), checker_runs("d", scale=0.45))
        for name in "abcd":
            for category in board["categories"]:
                r = entry(board, name)["categories"][category["id"]]
                self.assertEqual(r["rank"], r["rank_lo"], (name, category["id"]))
                self.assertLessEqual(r["rank_lo"], r["rank_hi"], (name, category["id"]))


class Pareto(unittest.TestCase):
    def test_a_memory_specialist_is_pareto_optimal(self):
        board = board_for(checker_runs("official"), checker_runs("fast", scale=0.2, mem_scale=2.0),
                          checker_runs("lean", scale=0.8, mem_scale=0.2), checker_runs("worse", scale=0.9, mem_scale=2.5))
        self.assertTrue(entry(board, "fast")["pareto"])
        self.assertTrue(entry(board, "lean")["pareto"])
        self.assertFalse(entry(board, "worse")["pareto"])


    def test_identical_checkers_are_both_pareto_optimal(self):
        board = board_for(checker_runs("official"), checker_runs("a", scale=0.5), checker_runs("b", scale=0.5),
                          checker_runs("c", scale=0.9))
        self.assertTrue(entry(board, "a")["pareto"])
        self.assertTrue(entry(board, "b")["pareto"])
        self.assertFalse(entry(board, "c")["pareto"])


class Pending(unittest.TestCase):
    def test_a_missing_cell_leaves_the_category_pending(self):
        runs = [r for r in checker_runs("p", scale=0.5) if not (r["test"] == "std" and r["lane"] == "1core")]
        board = board_for(checker_runs("official"), runs)
        p = entry(board, "p")
        self.assertIsNone(p["categories"]["single-core"]["score"])
        self.assertIsNone(p["categories"]["overall"]["rank"])
        self.assertIsNotNone(p["categories"]["multi-core"]["score"])
        self.assertLess(board["progress"]["measured"], board["progress"]["needed"])


class Sensitivity(unittest.TestCase):
    def test_a_dominant_checker_is_first_under_every_weighting(self):
        board = board_for(checker_runs("official"), checker_runs("a", scale=0.1, mem_scale=0.1, energy_scale=0.1),
                          checker_runs("b", scale=0.5))
        self.assertEqual(entry(board, "a")["sensitivity"], {"at": 1, "best": 1, "worst": 1})

    def test_the_seasons_own_weighting_lies_inside_the_range(self):
        board = board_for(checker_runs("official"), checker_runs("fast", scale=0.2, mem_scale=1.6, energy_scale=0.3),
                          checker_runs("lean", scale=0.45, mem_scale=0.2, energy_scale=0.5),
                          checker_runs("mid", scale=0.4, mem_scale=0.6, energy_scale=0.6))
        for name in ("fast", "lean", "mid"):
            s = entry(board, name)["sensitivity"]
            self.assertLessEqual(s["best"], s["at"])
            self.assertLessEqual(s["at"], s["worst"])

    def test_a_specialist_moves_with_the_weights(self):
        board = board_for(checker_runs("official"), checker_runs("fast", scale=0.2, mem_scale=1.6, energy_scale=0.3),
                          checker_runs("lean", scale=0.45, mem_scale=0.2, energy_scale=0.5))
        ranges = [entry(board, n)["sensitivity"] for n in ("fast", "lean")]
        self.assertTrue(any(r["best"] != r["worst"] for r in ranges), ranges)


class Scaling(unittest.TestCase):
    def test_parallel_speedup(self):
        board = board_for(checker_runs("official"), checker_runs("par", scale=0.25, single_core_scale=1.0))
        self.assertAlmostEqual(entry(board, "par")["scaling"]["speedup"], 4.0, places=2)
        self.assertAlmostEqual(entry(board, "par")["scaling"]["efficiency"], 1.0, places=2)


class Integrity(unittest.TestCase):
    def test_mixed_epochs_are_refused(self):
        runs = checker_runs("official") + [dict(r, epoch="e2") for r in checker_runs("a", scale=0.5)]
        with self.assertRaises(BoardError):
            board_for(runs)

    def test_invalid_runs_are_ignored(self):
        bogus = [dict(r, wall_s=r["wall_s"] / 100, valid=False) for r in checker_runs("a", scale=0.5)]
        honest = board_for(checker_runs("official"), checker_runs("a", scale=0.5))
        with_bogus = board_for(checker_runs("official"), checker_runs("a", scale=0.5), bogus)
        self.assertEqual(entry(honest, "a")["categories"]["overall"]["score"],
                         entry(with_bogus, "a")["categories"]["overall"]["score"])

    def test_flaky_verdicts_are_flagged(self):
        flaky = {("std", "4core"): [run("f", "std", "4core")] * 2 + [run("f", "std", "4core", status="error")]}
        board = board_for(checker_runs("official"), checker_runs("f", overrides=flaky))
        self.assertIn("std/4core", entry(board, "f")["flaky"])

    def test_summaries_use_medians(self):
        records = [run("a", "std", "4core", wall=w) for w in (1.0, 9.0, 2.0)]
        summary = summarize_cells(records)[Cell("a", "std", "4core")]
        self.assertEqual(summary.median("wall_s"), 2.0)


if __name__ == "__main__":
    unittest.main()
