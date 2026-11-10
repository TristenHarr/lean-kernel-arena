"""The preview from the official results: what each official number stands for, and what it says it is."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("preview", ROOT / "quietbox" / "preview-from-arena.py")
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)

PUBLISHED = {
    "meta": {"git_revision": "564fe0cc3b5b0000", "timestamp": "2026-11-10 14:02:04 UTC"},
    "checkers": [{"name": "official"}, {"name": "logos-kernel"}],
    "tests": [{"name": "mathlib", "outcome": "accept", "sha256": "m", "timeout": 5680},
              {"name": "tutorial/1", "outcome": "accept", "sha256": "t"}],
    "results": [
        {"checker": "logos-kernel", "test": "mathlib", "status": "accepted", "wall_time": 168.4, "cpu_time": 168.0,
         "max_rss": 9 * 2**30, "checker_hash": "h"},
        {"checker": "logos-kernel", "test": "tutorial/1", "status": "timeout", "wall_time": 0.0, "cpu_time": 0.0,
         "max_rss": 0, "checker_hash": "h", "timeout": 30},
    ],
}


class Records(unittest.TestCase):
    def test_each_official_result_stands_for_both_lanes(self):
        records = preview.records_from(PUBLISHED, "e")
        lanes = {(r["test"], r["lane"]): r for r in records}
        self.assertEqual(lanes[("mathlib", "4core")]["wall_s"], 168.4)
        self.assertEqual(lanes[("mathlib", "1core")]["wall_s"], 168.0)
        self.assertEqual(lanes[("mathlib", "4core")]["memory_peak_bytes"], 9 * 2**30)
        self.assertIsNone(lanes[("mathlib", "4core")]["energy_j"])

    def test_a_failure_is_capped_at_the_tests_limit_not_the_checkers(self):
        records = preview.records_from(PUBLISHED, "e")
        tutorial = next(r for r in records if r["test"] == "tutorial/1")
        self.assertEqual(tutorial["status"], "timeout")
        self.assertEqual(tutorial["cap_s"], 300.0)
        mathlib = next(r for r in records if r["test"] == "mathlib")
        self.assertEqual(mathlib["cap_s"], 5680)


class Source(unittest.TestCase):
    def test_the_preview_names_its_revision_and_when_it_was_published(self):
        meta = preview.describe(PUBLISHED, "https://arena.lean-lang.org/results.json")
        self.assertEqual(meta["arena_revision"], "564fe0cc3b5b0000")
        self.assertEqual(meta["published"], "2026-11-10 14:02:04 UTC")
        self.assertEqual(meta["url"], "https://arena.lean-lang.org/results.json")

    def test_a_local_file_is_read_and_its_copy_is_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "results.json"
            local.write_text(json.dumps(PUBLISHED, indent=3))
            data, source = preview.load_published(str(local), save=None)
            self.assertEqual(data["meta"]["git_revision"], "564fe0cc3b5b0000")
            self.assertEqual(source, str(local))
            copy = Path(tmp) / "kept" / "official-results.json"
            preview.load_published(str(local), save=str(copy))
            self.assertEqual(copy.read_bytes(), local.read_bytes())

    def test_with_no_results_named_it_reads_the_latest_round(self):
        args = preview.arguments(["--season", "s.yaml", "--out", "b.json"])
        self.assertEqual(args.results_json, "https://arena.lean-lang.org/results.json")
        self.assertIsNone(args.save)
        named = preview.arguments(["r.json", "--season", "s.yaml", "--out", "b.json", "--save", "k.json"])
        self.assertEqual((named.results_json, named.save), ("r.json", "k.json"))


if __name__ == "__main__":
    unittest.main()
