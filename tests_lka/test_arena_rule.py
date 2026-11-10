"""Instructions as a scored quantity, and the Arena's own rule as a category: the official board's
order, reproduced exactly from the same results, so the two boards can be read side by side."""

import copy
import gzip
import importlib.util
import json
import random
import unittest
from pathlib import Path

import yaml

import lka
from lka_quiet.board import BoardError, score_board

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"

spec = importlib.util.spec_from_file_location("preview", ROOT / "quietbox" / "preview-from-arena.py")
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)

TESTS = [
    {"name": "mathlib", "outcome": "accept", "timeout": 100},
    {"name": "std", "outcome": "accept", "timeout": 100},
    {"name": "tutorial/good", "outcome": "accept"},
    {"name": "bugs/bad1", "outcome": "reject"},
    {"name": "bugs/bad2", "outcome": "reject"},
    {"name": "corner-cases/c1", "outcome": "either"},
]

SCORING = {
    "schema": "lka-quiet-scoring/1",
    "season": "test-season",
    "reference": "official",
    "unranked": ["official", "parse-only"],
    "floors": {"wall_s": 0.02, "memory_bytes": 32 * 2**20, "energy_j": 0.1, "instructions": 1e8},
    "failure_factor": 2,
    "caps": {"memory_bytes": 15 * 2**30, "power_w": 25, "instruction_rate": 6e9},
    "metrics": {
        "T4": {"lane": "4core", "quantity": "wall_s", "title": "Wall time, 4 cores", "unit": "s"},
        "I": {"lane": "4core", "quantity": "instructions", "title": "Instructions", "unit": ""},
    },
    "families": {
        "libraries": {"title": "Libraries", "tests": ["mathlib", "std"]},
        "small": {"title": "Small", "tests": ["tutorial/*"]},
        "mathlib": {"title": "Mathlib", "tests": ["mathlib"]},
    },
    "categories": [
        {"id": "speed", "title": "Speed", "metrics": {"T4": 1}, "families": {"libraries": 0.5, "small": 0.5}},
        {"id": "instructions", "title": "Instructions", "metrics": {"I": 1},
         "families": {"libraries": 0.5, "small": 0.5}},
        {"id": "arena", "title": "Arena order", "rule": "arena", "metrics": {"I": 1}, "families": {"mathlib": 1}},
    ],
    "pareto": [],
    "bootstrap": {"replicates": 200, "seed": "test-season", "confidence": 0.95},
}

EXPECTED = {"accept": "accepted", "reject": "rejected", "either": "accepted"}


def records_of(checker, spec):
    """One run per test on the 4-core lane: each test answered correctly unless `spec` says
    otherwise. `spec["mathlib"]` (a number) is the Mathlib instruction count; any other entry is a
    status, or a (status, instructions) pair."""
    out = []
    for test in TESTS:
        name = test["name"]
        status, instructions = EXPECTED[test["outcome"]], 1e9
        given = spec.get(name)
        if isinstance(given, tuple):
            status, instructions = given
        elif isinstance(given, str):
            status = given
        elif given is not None:
            instructions = given
        out.append({"schema": "lka-quiet-run/1", "checker": checker, "test": name, "lane": "4core",
                    "status": status, "valid": True, "wall_s": 1.0, "memory_peak_bytes": 2**27, "energy_j": 1.0,
                    "instructions": instructions if status == "accepted" else None,
                    "timeout_s": test.get("timeout", 300.0), "cap_s": test.get("timeout", 300.0), "epoch": "e1"})
    return out


def arena_board(specs, order=None, scoring=SCORING):
    specs = {"official": {"mathlib": 9e11}, **specs}
    records = [r for name, spec in specs.items() for r in records_of(name, spec)]
    names = order or list(specs)
    return score_board(TESTS, [{"name": n} for n in names], records, scoring)


def category(board, cat_id):
    return next(c for c in board["categories"] if c["id"] == cat_id)


def result(board, name, cat_id="arena"):
    return next(c for c in board["checkers"] if c["name"] == name)["categories"][cat_id]


def tests_of(board, name):
    return next(c for c in board["checkers"] if c["name"] == name)["tests"]


class InstructionsMetric(unittest.TestCase):
    def test_half_the_instructions_everywhere_is_a_factor_of_two(self):
        board = arena_board({"half": {"mathlib": 4.5e11, "std": 5e8, "tutorial/good": 5e8}})
        self.assertAlmostEqual(result(board, "half", "instructions")["factor"], 2.0, places=9)
        self.assertAlmostEqual(result(board, "official", "instructions")["factor"], 1.0, places=12)

    def test_a_failure_costs_twice_the_cap_at_the_arenas_instruction_rate(self):
        board = arena_board({"slow": {"std": "timeout"}})
        cell = tests_of(board, "slow")["std"]["I"]
        self.assertTrue(cell["failed"])
        self.assertEqual(cell["value"], 2 * 100 * 6e9)

    def test_counts_below_the_floor_are_equal(self):
        board = arena_board({"official": {"mathlib": 9e11, "tutorial/good": 9e7},
                             "lean": {"mathlib": 9e11, "tutorial/good": 1e6}})
        self.assertEqual(tests_of(board, "lean")["tutorial/good"]["I"]["ratio"], 1.0)

    def test_instructions_enter_no_category_that_does_not_name_them(self):
        board = arena_board({"frugal": {"mathlib": 1e11, "std": 1e8, "tutorial/good": 1e8}})
        self.assertAlmostEqual(result(board, "frugal", "speed")["factor"], 1.0, places=12)
        self.assertGreater(result(board, "frugal", "instructions")["factor"], 1.0)

    def test_the_metric_is_unavailable_without_counts(self):
        records = [{**r, "instructions": None} for n in ("official", "a") for r in records_of(n, {})]
        board = score_board(TESTS, [{"name": "official"}, {"name": "a"}], records,
                            {**SCORING, "categories": SCORING["categories"][:2]})
        self.assertFalse(board["metrics"]["I"]["available"])
        self.assertEqual([c["id"] for c in board["categories"]], ["speed"])


class ArenaRule(unittest.TestCase):
    def order(self, board):
        return category(board, "arena")["order"]

    def test_fewest_mathlib_instructions_first(self):
        board = arena_board({"a": {"mathlib": 4e11}, "b": {"mathlib": 5e11}})
        self.assertEqual(self.order(board), ["a", "b", "official"])

    def test_soundness_before_speed(self):
        board = arena_board({"fast": {"mathlib": 1e11, "bugs/bad1": "accepted"}, "slow": {"mathlib": 8e11}})
        self.assertEqual(self.order(board), ["slow", "official", "fast"])

    def test_two_invalid_proofs_accepted_rank_below_one(self):
        board = arena_board({"one": {"mathlib": 8e11, "bugs/bad1": "accepted"},
                             "two": {"mathlib": 1e11, "bugs/bad1": "accepted", "bugs/bad2": "accepted"}})
        self.assertEqual(self.order(board), ["official", "one", "two"])

    def test_completeness_after_soundness_and_before_speed(self):
        board = arena_board({"incomplete": {"mathlib": 1e11, "std": "rejected"},
                             "unsound": {"mathlib": 1e11, "bugs/bad1": "accepted"},
                             "slow": {"mathlib": 8e11}})
        self.assertEqual(self.order(board), ["slow", "official", "incomplete", "unsound"])

    def test_not_checking_mathlib_ranks_below_every_checker_that_does_and_above_the_incomplete(self):
        board = arena_board({"declines": {"mathlib": "declined"},
                             "times-out": {"mathlib": "timeout"},
                             "very-slow": {"mathlib": 5e12},
                             "incomplete": {"mathlib": 1e11, "std": "rejected"}})
        self.assertEqual(self.order(board), ["official", "very-slow", "declines", "times-out", "incomplete"])

    def test_an_accepted_mathlib_without_a_count_ranks_as_not_checked(self):
        board = arena_board({"uncounted": {"mathlib": ("accepted", None)}, "very-slow": {"mathlib": 5e12}})
        self.assertEqual(self.order(board), ["official", "very-slow", "uncounted"])
        self.assertIsNotNone(result(board, "uncounted")["rank"])
        self.assertIsNone(result(board, "uncounted")["factor"])

    def test_a_zero_count_is_no_count(self):
        board = arena_board({"zero": {"mathlib": ("accepted", 0)}, "very-slow": {"mathlib": 5e12}})
        self.assertEqual(self.order(board), ["official", "very-slow", "zero"])
        self.assertIsNone(result(board, "zero")["arena"]["measure"])

    def test_errors_and_timeouts_are_declines_not_wrong_answers(self):
        board = arena_board({"crashy": {"bugs/bad1": "error", "std": "timeout"},
                             "polite": {"bugs/bad1": "declined", "std": "declined"}})
        for name in ("crashy", "polite"):
            self.assertEqual(result(board, name)["arena"],
                             {"invalid_accepted": 0, "valid_rejected": 0, "declined": 2, "measure": 1e9}, name)
        self.assertEqual(result(board, "crashy")["rank"], result(board, "polite")["rank"])

    def test_declines_break_the_last_tie(self):
        board = arena_board({"many": {"mathlib": "declined", "std": "declined", "tutorial/good": "declined"},
                             "few": {"mathlib": "declined"}})
        self.assertEqual(self.order(board), ["official", "few", "many"])

    def test_a_corner_case_counts_only_when_declined(self):
        board = arena_board({"answers": {"corner-cases/c1": "rejected"},
                             "skips": {"corner-cases/c1": "declined"}})
        self.assertEqual(result(board, "answers")["arena"]["declined"], 0)
        self.assertEqual(result(board, "answers")["arena"]["valid_rejected"], 0)
        self.assertEqual(result(board, "skips")["arena"]["declined"], 1)

    def test_exact_ties_keep_the_arenas_file_order_whatever_order_they_arrive_in(self):
        specs = {"kefir": {"mathlib": "declined"}, "kefir-wasm": {"mathlib": "declined"}}
        for given in (["official", "kefir", "kefir-wasm"], ["kefir-wasm", "official", "kefir"]):
            board = arena_board(specs, order=given)
            self.assertEqual(self.order(board), ["official", "kefir-wasm", "kefir"])
            self.assertEqual(result(board, "kefir")["rank"], result(board, "kefir-wasm")["rank"])
            self.assertTrue(result(board, "kefir")["tied"])

    def test_every_checker_has_a_place_the_reference_and_the_unsound_too(self):
        board = arena_board({"parse-only": {"mathlib": 1e10, "bugs/bad1": "accepted", "bugs/bad2": "accepted"},
                             "a": {"mathlib": 4e11}})
        self.assertEqual([result(board, n)["rank"] for n in self.order(board)], [1, 2, 3])
        self.assertEqual(self.order(board), ["a", "official", "parse-only"])

    def test_distinct_counts_are_certain_places(self):
        board = arena_board({"a": {"mathlib": 4e11}, "b": {"mathlib": "declined"},
                             "c": {"mathlib": 1e11, "bugs/bad1": "accepted"}})
        for place, name in enumerate(self.order(board), start=1):
            r = result(board, name)
            self.assertEqual((r["rank"], r["rank_lo"], r["rank_hi"], r["tied"]), (place, place, place, False), name)

    def test_the_factor_is_mathlib_instructions_against_the_reference(self):
        board = arena_board({"a": {"mathlib": 4.5e11}, "b": {"mathlib": "declined"}})
        self.assertAlmostEqual(result(board, "a")["factor"], 2.0, places=9)
        self.assertEqual(result(board, "a")["arena"]["measure"], 4.5e11)
        self.assertIsNone(result(board, "b")["factor"])
        self.assertIsNone(result(board, "b")["arena"]["measure"])

    def test_the_counts_are_on_the_board(self):
        board = arena_board({"a": {"mathlib": 4e11, "bugs/bad2": "accepted", "std": "rejected",
                                   "tutorial/good": "declined", "corner-cases/c1": "timeout"}})
        self.assertEqual(result(board, "a")["arena"],
                         {"invalid_accepted": 1, "valid_rejected": 1, "declined": 2, "measure": 4e11})

    def test_an_unmeasured_test_leaves_the_place_pending(self):
        records = [r for n in ("official", "a", "b") for r in records_of(n, {"mathlib": 1e11})
                   if not (r["checker"] == "b" and r["test"] == "bugs/bad2")]
        board = score_board(TESTS, [{"name": n} for n in ("official", "a", "b")], records, SCORING)
        self.assertIsNone(result(board, "b")["rank"])
        self.assertEqual(category(board, "arena")["order"][-1], "b")

    def test_the_rule_is_on_the_category(self):
        board = arena_board({})
        self.assertEqual(category(board, "arena")["rule"], "arena")
        self.assertEqual(category(board, "speed")["rule"], "weighted")

    def test_the_arena_rule_takes_one_metric(self):
        scoring = copy.deepcopy(SCORING)
        scoring["categories"][2]["metrics"] = {"I": 0.5, "T4": 0.5}
        with self.assertRaises(BoardError):
            arena_board({}, scoring=scoring)

    def test_an_unknown_rule_is_refused(self):
        scoring = copy.deepcopy(SCORING)
        scoring["categories"][2]["rule"] = "borda"
        with self.assertRaises(BoardError):
            arena_board({}, scoring=scoring)


def official_order(published: dict) -> list[str]:
    """The order of the Arena's own board: lka.py's statistics and its key, sorted as build-site sorts."""
    results = {(r["checker"], r["test"]): r for r in published["results"]}
    checkers = [dict(c) for c in published["checkers"]]
    for checker in checkers:
        checker["stats"] = lka.compute_checker_stats(checker, published["tests"], results)
    checkers.sort(key=lambda c: lka.board_order_key(c["stats"]))
    return [c["name"] for c in checkers]


def season(replicates=100):
    scoring = yaml.safe_load((ROOT / "scoring" / "season-1.yaml").read_text())
    scoring["bootstrap"]["replicates"] = replicates
    return scoring


def preview_board(published: dict, scoring: dict) -> dict:
    records = preview.records_from(published, "e")
    tests = [{**t, "path": None} for t in published["tests"]]
    return score_board(tests, [{"name": c["name"]} for c in published["checkers"]], records, scoring)


class TheOfficialRound(unittest.TestCase):
    """Round 564fe0cc of the official Arena (tests_lka/fixtures/arena-round-564fe0cc.json.gz: its
    results.json with each result's verdict and measurements, nothing else)."""

    @classmethod
    def setUpClass(cls):
        with gzip.open(FIXTURES / "arena-round-564fe0cc.json.gz") as f:
            cls.published = json.load(f)
        cls.board = preview_board(cls.published, season())

    def test_the_arena_category_is_the_official_order(self):
        self.assertEqual(category(self.board, "arena")["order"], official_order(self.published))

    def test_the_top_of_the_official_board(self):
        self.assertEqual(category(self.board, "arena")["order"][:3], ["nano-lean", "logos-kernel", "sokonanoda"])

    def test_every_checker_of_the_round_has_a_place(self):
        self.assertEqual(len(self.published["checkers"]), 36)
        for c in self.board["checkers"]:
            self.assertIsNotNone(c["categories"]["arena"]["rank"], c["name"])

    def test_the_counts_are_the_arenas_counts(self):
        results = {(r["checker"], r["test"]): r for r in self.published["results"]}
        for c in self.published["checkers"]:
            stats = lka.compute_checker_stats(c, self.published["tests"], results)
            ours = result(self.board, c["name"])["arena"]
            with self.subTest(checker=c["name"]):
                self.assertEqual(ours["invalid_accepted"], stats["reject_total"] - stats["reject_correct"])
                self.assertEqual(ours["valid_rejected"], stats["accept_total"] - stats["accept_correct"])
                self.assertEqual(ours["declined"], stats["declined_count"])
                self.assertEqual(ours["measure"], stats["mathlib_instructions"] or None)

    def test_the_instructions_category_ranks_the_round(self):
        ranked = [n for n in category(self.board, "instructions")["order"]
                  if result(self.board, n, "instructions")["rank"] is not None]
        self.assertGreaterEqual(len(ranked), 20)


POOL = ["official", "kefir", "kefir-wasm", "nano-lean", "nanoclo", "nanoclo-fortran", "pestka", "pestka-wasm",
        "tenet", "tenet-hs", "a", "b"]
STATUSES = ["accepted", "rejected", "declined", "error", "timeout"]


def random_round(rng: random.Random) -> dict:
    names = ["official"] + rng.sample(POOL[1:], rng.randint(2, len(POOL) - 1))
    file_order = sorted(names, key=lambda n: f"{n}.yaml")
    tests = ([{"name": "mathlib", "outcome": "accept", "sha256": "m", "timeout": 1000}]
             + [{"name": f"good/{i}", "outcome": "accept", "sha256": f"g{i}"} for i in range(rng.randint(1, 4))]
             + [{"name": f"bad/{i}", "outcome": "reject", "sha256": f"b{i}"} for i in range(rng.randint(1, 4))]
             + [{"name": f"odd/{i}", "outcome": "either", "sha256": f"o{i}"} for i in range(rng.randint(0, 2))])
    counts = [rng.choice([1, 2, 3]) * 10**rng.randint(9, 12) for _ in range(3)] + [0]
    results = []
    for name in names:
        faithful = rng.random() < 0.6
        for test in tests:
            right = {"accept": "accepted", "reject": "rejected", "either": rng.choice(["accepted", "rejected"])}
            status = right[test["outcome"]] if faithful or rng.random() < 0.5 else rng.choice(STATUSES)
            if name == "official":
                status = right[test["outcome"]]
            results.append({"checker": name, "test": test["name"], "status": status,
                            "instructions": rng.choice(counts) if status == "accepted" else 0,
                            "wall_time": 1.0, "cpu_time": 1.0, "max_rss": 2**27, "checker_hash": name})
    return {"meta": {"git_revision": "r"}, "checkers": [{"name": n} for n in file_order], "tests": tests,
            "results": results}


class AgainstTheArenasOwnCode(unittest.TestCase):
    def test_random_rounds_order_as_the_arena_orders_them(self):
        rng = random.Random(564)
        scoring = season(replicates=100)
        scoring["categories"] = [c for c in scoring["categories"] if c["id"] == "arena"]
        for key in ("sensitivity", "scaling"):
            scoring.pop(key)
        scoring["pareto"] = []
        for index in range(150):
            published = random_round(rng)
            with self.subTest(round=index):
                board = preview_board(published, scoring)
                self.assertEqual(category(board, "arena")["order"], official_order(published))


if __name__ == "__main__":
    unittest.main()
