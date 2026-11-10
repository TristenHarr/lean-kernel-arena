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
