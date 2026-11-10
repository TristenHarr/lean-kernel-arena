"""The board's pages: what each table and chart is drawn from, and the whole site rendered from a
real official round, every page present and every link inside the board resolving."""

import copy
import gzip
import json
import re
import tempfile
import unittest
from pathlib import Path

import yaml

import lka
from lka_quiet import charts, site
from lka_quiet.board import score_board
from tests_lka.test_arena_rule import (FIXTURES, SCORING, TESTS, arena_board, official_order, preview_board,
                                       records_of, season)

ROOT = Path(__file__).resolve().parent.parent


class Formatting(unittest.TestCase):
    def test_instructions_read_as_the_arena_writes_them(self):
        for count in (0, 999, 1_000, 41_000_000, 9_141_000_000, 407_316_543_210, 1_284_400_000_000, 3_369_200_000_000):
            with self.subTest(count=count):
                self.assertEqual(site.fmt_instructions(count), lka.format_instructions(count))
        self.assertEqual(site.fmt_instructions(None), "–")

    def test_every_quantity_has_a_format(self):
        self.assertEqual(site.fmt_quantity(407_316_543_210, "instructions"), "407.3 G")
        self.assertEqual(site.fmt_quantity(2.5, "wall_s"), "2.50 s")
        self.assertEqual(site.fmt_quantity(2**30, "memory_bytes"), "1.00 GiB")
        self.assertEqual(site.fmt_quantity(12.0, "energy_j"), "12.0 J")


class Rows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.board = arena_board({"a": {"mathlib": 4.5e11}, "declines": {"mathlib": "declined"},
                                 "unsound": {"mathlib": 1e11, "bugs/bad1": "accepted"}})

    def test_the_forest_holds_only_checkers_with_a_factor(self):
        names = [r["name"] for r in site.forest_rows(self.board, "arena", "")]
        self.assertEqual(names, ["a", "official", "unsound"])

    def test_the_arena_table_is_the_whole_order_with_the_arenas_columns(self):
        rows = site.arena_rows(self.board, "arena", "../../")
        self.assertEqual([r["name"] for r in rows], ["a", "official", "declines", "unsound"])
        a, official, declines, unsound = rows
        self.assertEqual((a["place"], a["invalid_accepted"], a["valid_rejected"], a["declined"]), ("1", 0, 0, 0))
        self.assertEqual(a["measure"], "450.0 G")
        self.assertAlmostEqual(a["factor"], 2.0, places=6)
        self.assertTrue(official["reference"])
        self.assertEqual((declines["measure"], declines["factor"], declines["declined"]), ("–", None, 1))
        self.assertEqual((unsound["place"], unsound["invalid_accepted"]), ("4", 1))
        self.assertEqual(a["href"], "../../checker/a/")

    def test_a_leader_tile_counts_every_placed_checker(self):
        tile = site.leaders(self.board, "arena")
        self.assertEqual(tile["names"], ["a"])
        self.assertEqual(tile["ranked"], 4)

    def test_a_pending_checker_closes_the_arena_table(self):
        records = [r for name in ("official", "a", "b") for r in records_of(name, {})
                   if not (r["checker"] == "a" and r["test"] == "std")]
        board = score_board(TESTS, [{"name": n} for n in ("official", "a", "b")], records, SCORING)
        rows = site.arena_rows(board, "arena", "")
        self.assertEqual([r["name"] for r in rows][-1], "a")
        self.assertTrue(rows[-1]["pending"])
        self.assertEqual(rows[-1]["place"], "–")

    def test_the_rank_grid_shades_each_category_by_its_own_field(self):
        scoring = copy.deepcopy(SCORING)
        scoring["categories"][0]["id"] = "overall"
        board = arena_board({"a": {"mathlib": 4.5e11}, "b": {"mathlib": 5e11},
                             "unsound": {"mathlib": 1e11, "bugs/bad1": "accepted"}}, scoring=scoring)
        grid = site.rank_grid(board)
        arena_column = [c["id"] for c in grid["categories"]].index("arena")
        shades = {row["name"]: row["cells"][arena_column]["q"] for row in grid["rows"]}
        self.assertEqual(set(shades), {"a", "b"})
        self.assertEqual(shades["a"], charts.seq_step(1.0))
        self.assertEqual(shades["b"], charts.seq_step(2 / 3))


class TickFormats(unittest.TestCase):
    def test_round_times_read_round(self):
        self.assertEqual([site.fmt_time_tick(v) for v in (1, 30, 60, 120, 300, 1800, 3600, 7200)],
                         ["1 s", "30 s", "1 min", "2 min", "5 min", "30 min", "1 h", "2 h"])
        self.assertEqual(site.fmt_time_tick(0.5), "500 ms")

    def test_powers_of_two_read_whole(self):
        self.assertEqual([site.fmt_bytes_tick(2**k) for k in (20, 29, 30, 33)], ["1 MiB", "512 MiB", "1 GiB", "8 GiB"])


class Places(unittest.TestCase):
    def test_a_label_is_the_place_and_a_shared_place_says_so(self):
        self.assertEqual(site.rank_label({"place": 2, "place_tied": True, "rank": 1}), "2=")
        self.assertEqual(site.rank_label({"place": 3, "place_tied": False, "rank": 2}), "3")
        self.assertEqual(site.rank_label({"place": None, "rank": None}), "–")


def season_board():
    from tests_lka.test_board import board_for, checker_runs
    return board_for(checker_runs("official"), checker_runs("fast", scale=0.2, mem_scale=2.0),
                     checker_runs("lean", scale=0.8, mem_scale=0.2), checker_runs("worse", scale=0.9, mem_scale=2.5),
                     checker_runs("worst", scale=1.2, mem_scale=3.0, energy_scale=1.5))


class Fronts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests_lka.test_board import SCORING as BOARD_SCORING
        cls.board = season_board()
        cls.season = BOARD_SCORING

    def test_one_view_per_pair_of_pareto_categories(self):
        views = site.pair_views(self.board, self.season)
        self.assertEqual([(v["x"], v["y"]) for v in views],
                         [("multi-core", "single-core"), ("multi-core", "memory"), ("multi-core", "energy"),
                          ("single-core", "memory"), ("single-core", "energy"), ("memory", "energy")])
        speed_memory = views[1]
        front = {p["name"] for p in speed_memory["points"] if p["pareto"]}
        self.assertEqual(front, {"fast", "lean"})
        self.assertEqual({p["name"] for p in speed_memory["points"]}, {"fast", "lean", "worse", "worst"})

    def test_layers_say_who_dominates_whom(self):
        rows = site.layer_rows(self.board, self.season)
        self.assertEqual([(r["name"], r["layer"]) for r in rows],
                         [("fast", 1), ("lean", 1), ("worse", 2), ("worst", 3)])
        worse = rows[2]
        self.assertEqual(worse["dominated_by"], ["fast", "lean"])
        self.assertEqual(rows[3]["dominated_by"], ["fast", "lean", "worse"])
        self.assertEqual(rows[0]["dominated_by"], [])

    def test_the_closest_dominators_are_those_beating_no_other_dominator(self):
        rows = {r["name"]: r for r in site.layer_rows(self.board, self.season)}
        self.assertEqual(rows["worse"]["closest"], ["fast", "lean"])
        self.assertEqual(rows["worst"]["closest"], ["worse"])
        self.assertEqual(rows["fast"]["closest"], [])

    def test_the_parallel_rows_hold_every_pareto_category(self):
        rows = site.parallel_rows(self.board, self.season)
        self.assertEqual({r["name"] for r in rows}, {"fast", "lean", "worse", "worst"})
        for r in rows:
            self.assertEqual(set(r["values"]), {"multi-core", "single-core", "memory", "energy"})
        self.assertEqual({r["name"] for r in rows if r["front"]}, {"fast", "lean"})

    def test_absolute_points_take_less_as_better(self):
        points = site.absolute_points(self.board, "mathlib", "T4", "M")
        by = {p["name"]: p for p in points}
        self.assertEqual(set(by), {"official", "fast", "lean", "worse", "worst"})
        self.assertAlmostEqual(by["fast"]["x"], 0.4004, places=9)
        self.assertEqual({n for n, p in by.items() if p["pareto"]}, {"fast", "lean"})


class Libraries(unittest.TestCase):
    def test_the_matrix_has_every_library_and_marks_failures(self):
        from tests_lka.test_board import board_for, checker_runs, run, LANES
        over = {("init", lane): [run("slow", "init", lane, status="timeout")] for lane in LANES}
        board = board_for(checker_runs("official"), checker_runs("fast", scale=0.5),
                          checker_runs("slow", scale=1.5, overrides=over))
        matrix = site.library_matrix(board, "T4")
        self.assertEqual(matrix["libraries"], ["mathlib", "std", "init"])
        rows = {r["name"]: r for r in matrix["rows"]}
        self.assertEqual(list(rows), ["fast", "slow", "official"])
        init = dict(zip(matrix["libraries"], rows["slow"]["cells"]))["init"]
        self.assertTrue(init["failed"])
        mathlib = dict(zip(matrix["libraries"], rows["fast"]["cells"]))["mathlib"]
        self.assertEqual(mathlib["text"], "1.00 s")
        self.assertAlmostEqual(mathlib["factor"], 2.0, places=6)
        self.assertEqual(mathlib["q"], charts.seq_step(1.0))


class LibrariesUnranked(unittest.TestCase):
    def test_only_ranked_checkers_are_shaded_and_a_failure_says_what_it_was(self):
        from tests_lka.test_board import board_for, checker_runs, run, LANES
        over = {("bugs/bad1", "4core"): [run("cheat", "bugs/bad1", "4core", status="accepted")] * 3}
        declines = {("std", lane): [run("shy", "std", lane, status="declined")] for lane in LANES}
        board = board_for(checker_runs("official"), checker_runs("fast", scale=0.5),
                          checker_runs("cheat", scale=0.01, overrides=over), checker_runs("shy", overrides=declines))
        matrix = site.library_matrix(board, "T4")
        rows = {r["name"]: r for r in matrix["rows"]}
        cheat = dict(zip(matrix["libraries"], rows["cheat"]["cells"]))["mathlib"]
        self.assertIsNone(cheat["q"])
        self.assertEqual(rows["cheat"]["note"], "unsound")
        self.assertEqual(rows["official"]["note"], "reference")
        fast = dict(zip(matrix["libraries"], rows["fast"]["cells"]))["mathlib"]
        self.assertEqual(fast["q"], charts.seq_step(1.0))
        shy = dict(zip(matrix["libraries"], rows["shy"]["cells"]))["std"]
        self.assertEqual((shy["failed"], shy["text"]), (True, "declined"))


class Tests(unittest.TestCase):
    def test_every_test_once_in_its_group_and_every_verdict_judged(self):
        from tests_lka.test_board import TESTS as BOARD_TESTS, board_for, checker_runs, run
        over = {("bugs/bad1", "4core"): [run("a", "bugs/bad1", "4core", status="accepted")] * 3,
                ("std", "4core"): [run("a", "std", "4core", status="declined")] * 3}
        board = board_for(checker_runs("official"), checker_runs("a", overrides=over))
        board["tests"] = BOARD_TESTS
        groups = site.test_groups(board)
        listed = [t["name"] for g in groups for t in g["tests"]]
        self.assertEqual(sorted(listed), sorted(t["name"] for t in BOARD_TESTS))
        self.assertEqual([g["name"] for g in groups][:1], [""])
        cells = {(t["name"], c["checker"]): c for g in groups for t in g["tests"] for c in t["cells"]}
        self.assertEqual(cells[("bugs/bad1", "a")]["kind"], "wrong")
        self.assertEqual(cells[("bugs/bad1", "official")]["kind"], "correct")
        self.assertEqual(cells[("std", "a")]["kind"], "declined")
        self.assertEqual(cells[("mathlib", "a")]["text"], "2.00 s")


class RenderedRound(unittest.TestCase):
    """The site from round 564fe0cc of the official Arena, as `lka.py build-site --quiet-home` renders it."""

    @classmethod
    def setUpClass(cls):
        with gzip.open(FIXTURES / "arena-round-564fe0cc.json.gz") as f:
            cls.published = json.load(f)
        board = preview_board(cls.published, season())
        board["tests"] = [{**t, "path": None} for t in cls.published["tests"]]
        board["preview"] = {"source": "the official Lean Kernel Arena's published results", "url": "fixture",
                            "arena_revision": cls.published["meta"]["git_revision"], "revision_url": None,
                            "published": cls.published["meta"]["timestamp"], "note": "A test render."}
        board["machine"] = {"name": "official arena runners (preview)"}
        cls.board = board
        cls.tmp = tempfile.TemporaryDirectory(prefix="lka-site-")
        cls.out = Path(cls.tmp.name)
        machine = yaml.safe_load((ROOT / "machines" / "n100-quiet.yaml").read_text())
        site.render(board, season(), {**board["machine"], **machine}, lka.make_template_env(ROOT / "templates"),
                    cls.out, cls.published["meta"], home_at_root=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def page(self, *parts) -> str:
        return (self.out.joinpath(*parts)).read_text()

    def test_every_category_has_a_page(self):
        for category in self.board["categories"]:
            with self.subTest(category=category["id"]):
                self.assertTrue((self.out / "quiet" / "category" / category["id"] / "index.html").exists())
        ids = [c["id"] for c in self.board["categories"]]
        self.assertIn("instructions", ids)
        self.assertIn("arena", ids)

    def test_the_arena_page_lists_every_checker_in_the_official_order(self):
        html = self.page("quiet", "category", "arena", "index.html")
        table = html[html.index('id="arena-order"'):]
        table = table[:table.index("</table>")]
        self.assertEqual(re.findall(r'data-checker="([^"]+)"', table), official_order(self.published))

    def test_the_arena_page_states_the_rule(self):
        html = self.page("quiet", "category", "arena", "index.html").lower()
        for phrase in ("invalid proofs accepted", "valid proofs rejected", "instructions on mathlib", "tests declined"):
            self.assertIn(phrase, html)

    def test_the_home_page_has_a_tile_for_every_category(self):
        for home in (self.page("index.html"), self.page("quiet", "index.html")):
            for category in self.board["categories"]:
                self.assertIn(f'category/{category["id"]}/', home)

    def test_every_link_inside_the_board_resolves(self):
        quiet = (self.out / "quiet").resolve()
        pages = sorted(self.out.rglob("*.html"))
        self.assertGreater(len(pages), 40)
        broken = []
        for page in pages:
            for href in re.findall(r'href="([^"]+)"', page.read_text()):
                if re.match(r"[a-z]+:", href) or href.startswith("#"):
                    continue
                target = (page.parent / href.split("#")[0]).resolve()
                if quiet not in (target, *target.parents):
                    continue
                if href.split("#")[0].endswith("/") or target.is_dir():
                    target = target / "index.html"
                if not target.exists():
                    broken.append(f"{page.relative_to(self.out)} -> {href}")
        self.assertEqual(broken, [])

    def test_the_new_pages_are_there(self):
        for parts in (("pareto",), ("libraries",), ("tests",), ("test", "mathlib"), ("test", "perf", "app-lam"),
                      ("test", "tutorial", "001_basicDef")):
            with self.subTest(page="/".join(parts)):
                self.assertTrue((self.out / "quiet").joinpath(*parts, "index.html").exists())

    def test_the_pareto_page_shows_every_front(self):
        html = self.page("quiet", "pareto", "index.html")
        self.assertEqual(html.count("chart-parallel"), 1)
        pairs, mathlib, other_libraries = 3, 2, 7
        self.assertEqual(html.count("chart-pareto"), pairs + mathlib + other_libraries)
        self.assertIn("logos-kernel", html)

    def test_the_libraries_page_has_every_library(self):
        html = self.page("quiet", "libraries", "index.html")
        for library in ("mathlib", "init", "std", "cslib", "cedar", "con-leche", "navier-stokes-euler", "reachability"):
            self.assertIn(f"test/{library}/", html)

    def test_the_tests_page_has_every_test_and_every_checker(self):
        html = self.page("quiet", "tests", "index.html")
        for test in self.published["tests"]:
            self.assertIn(f'data-test="{test["name"]}"', html)
        for checker in self.published["checkers"]:
            self.assertIn(f'checker/{checker["name"]}/', html)

    def test_every_checker_has_a_page(self):
        for checker in self.published["checkers"]:
            with self.subTest(checker=checker["name"]):
                self.assertTrue((self.out / "quiet" / "checker" / checker["name"] / "index.html").exists())

    def test_the_board_data_is_published_beside_the_pages(self):
        published = json.loads(self.page("quiet", "board.json"))
        self.assertEqual(published["schema"], self.board["schema"])
        self.assertEqual([c["id"] for c in published["categories"]], [c["id"] for c in self.board["categories"]])


if __name__ == "__main__":
    unittest.main()
