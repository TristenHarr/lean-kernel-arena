"""A pull request on the quiet box: its checkers, renamed pr<N>-<name>, staged into a copy of
master's tree (never its board code), measured beside the board, and reported on the PR."""

import os
import tempfile
import unittest
from pathlib import Path

import yaml

from lka_quiet.pr import PRError, changed_checkers, pr_report, stage_pr_arena


def tree(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


BASE = {
    "lka.py": "# master's lka.py\n",
    "lka_quiet/board.py": "# master's board\n",
    "checkers/a.yaml": "url: https://example.invalid/a\nrev: aaa\nrun: a \"$IN\"\n",
    "checkers/b.yaml": "url: https://example.invalid/b\nrev: bbb\nrun: b \"$IN\"\n",
    "checkers/official.yaml": "dir: official\nbuild: lake build\nrun: .lake/build/bin/kernel \"$IN\"\n",
    "checkers/official/Main.lean": "-- master\n",
    "_out/index.html": "built\n",
}


def accept(data, path):
    if not isinstance(data, dict) or "run" not in data:
        raise ValueError(f"{path}: no run")


class Staging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.base = tree(root / "base", BASE)
        head = dict(BASE)
        del head["checkers/a.yaml"]
        head["checkers/b.yaml"] = "url: https://example.invalid/b\nrev: b2\nrun: b \"$IN\"\n"
        head["checkers/c.yaml"] = "url: https://example.invalid/c\nrev: ccc\nrun: c \"$IN\"\n"
        head["checkers/official/Main.lean"] = "-- the PR's\n"
        head["lka_quiet/board.py"] = "import os; os.system('the PR tries to change the board')\n"
        self.head = tree(root / "head", head)
        self.out = root / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_checkers_a_pr_adds_or_changes(self):
        self.assertEqual(changed_checkers(self.base, self.head), ["b", "c", "official"])

    def test_staged_under_their_pr_names_beside_masters_checkers(self):
        names = stage_pr_arena(self.base, self.head, 12, self.out, validate=accept)
        self.assertEqual(names, ["pr12-b", "pr12-c", "pr12-official"])
        self.assertEqual((self.out / "checkers" / "pr12-b.yaml").read_text(), (self.head / "checkers" / "b.yaml").read_text())
        self.assertEqual((self.out / "checkers" / "b.yaml").read_text(), (self.base / "checkers" / "b.yaml").read_text())
        self.assertTrue((self.out / "checkers" / "a.yaml").exists())

    def test_a_checkers_own_directory_moves_with_it(self):
        stage_pr_arena(self.base, self.head, 12, self.out, validate=accept)
        data = yaml.safe_load((self.out / "checkers" / "pr12-official.yaml").read_text())
        self.assertEqual(data["dir"], "pr12-official")
        self.assertEqual((self.out / "checkers" / "pr12-official" / "Main.lean").read_text(), "-- the PR's\n")
        self.assertEqual((self.out / "checkers" / "official" / "Main.lean").read_text(), "-- master\n")

    def test_the_pr_never_brings_its_board_code(self):
        stage_pr_arena(self.base, self.head, 12, self.out, validate=accept)
        self.assertEqual((self.out / "lka_quiet" / "board.py").read_text(), "# master's board\n")
        self.assertEqual((self.out / "lka.py").read_text(), "# master's lka.py\n")

    def test_build_outputs_are_left_behind(self):
        stage_pr_arena(self.base, self.head, 12, self.out, validate=accept)
        self.assertFalse((self.out / "_out").exists())

    def test_a_link_in_a_checkers_directory_stays_a_link(self):
        os.symlink("/etc/passwd", self.head / "checkers" / "official" / "passwd")
        stage_pr_arena(self.base, self.head, 12, self.out, validate=accept)
        staged = self.out / "checkers" / "pr12-official" / "passwd"
        self.assertTrue(staged.is_symlink())
        self.assertEqual(os.readlink(staged), "/etc/passwd")

    def test_an_invalid_checker_is_refused_with_its_reason(self):
        (self.head / "checkers" / "c.yaml").write_text("url: https://example.invalid/c\n")
        with self.assertRaises(PRError) as caught:
            stage_pr_arena(self.base, self.head, 12, self.out, validate=accept)
        self.assertIn("c.yaml", str(caught.exception))

    def test_a_pr_without_checkers_stages_nothing(self):
        same = tree(Path(self.tmp.name) / "same", BASE)
        self.assertEqual(changed_checkers(self.base, same), [])


class Command(unittest.TestCase):
    def test_the_stage_command_checks_the_arenas_schema_and_lists_the_names(self):
        import types
        import lka
        from lka_quiet.cli import cmd_quiet_pr_stage
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = tree(root / "base", {"checkers/a.yaml": "url: https://example.invalid/a\nrev: aaaaaaa\nbuild: make\nrun: a\n"})
            head = tree(root / "head", {"checkers/a.yaml": "url: https://example.invalid/a\nrev: aaaaaaa\nbuild: make\nrun: a\n",
                                        "checkers/n.yaml": "url: https://example.invalid/n\nrev: abcdef1\nbuild: make\nrun: n\n"})
            args = types.SimpleNamespace(base=str(base), head=str(head), number=9, out=str(root / "out"),
                                         names_file=str(root / "names"))
            self.assertEqual(cmd_quiet_pr_stage(args, lka), 0)
            self.assertEqual((root / "names").read_text().split(), ["pr9-n"])
            (head / "checkers" / "n.yaml").write_text("not_a_field: 1\n")
            self.assertEqual(cmd_quiet_pr_stage(args, lka), 1)


class Report(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests_lka.test_board import board_for, checker_runs, run
        over = {("bugs/bad1", "4core"): [run("pr3-cheat", "bugs/bad1", "4core", status="accepted")] * 3}
        cls.board = board_for(checker_runs("official"), checker_runs("fast", scale=0.2),
                              checker_runs("pr3-new", scale=0.5), checker_runs("pr3-cheat", scale=0.1, overrides=over))

    def test_every_category_with_its_place_and_range(self):
        text = pr_report(self.board, ["pr3-new"], machine="N100 quiet box")
        self.assertIn("pr3-new", text)
        self.assertIn("N100 quiet box", text)
        entry = next(c for c in self.board["checkers"] if c["name"] == "pr3-new")
        for category in self.board["categories"]:
            result = entry["categories"][category["id"]]
            with self.subTest(category=category["id"]):
                self.assertIn(f"| {category['title']} | {result['place']}", text)
        self.assertIn("2.00×", text)

    def test_an_unsound_checker_says_so_first(self):
        text = pr_report(self.board, ["pr3-cheat"], machine="box")
        self.assertIn("accepted 1 invalid proof", text)
        self.assertLess(text.index("accepted 1 invalid proof"), text.index("| Category |"))


if __name__ == "__main__":
    unittest.main()
