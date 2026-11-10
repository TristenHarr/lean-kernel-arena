"""The season's rules and the machine profile: valid, consistent with each other and with the arena."""

import fnmatch
import json
import math
import unittest
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parent.parent
SEASONS = sorted((ROOT / "scoring").glob("*.yaml"))
MACHINES = sorted((ROOT / "machines").glob("*.yaml"))


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def schema(name: str) -> dict:
    return json.loads((ROOT / "schemas" / f"{name}.json").read_text())


def arena_test_names() -> list[str]:
    """Names the arena's tests are published under: each YAML's path, and tutorial/<sub> for the tutorial."""
    names = []
    for path in sorted((ROOT / "tests").rglob("*.yaml")):
        names.append(str(path.relative_to(ROOT / "tests").with_suffix("")))
    names.append("tutorial/good_example")
    return names


class Machines(unittest.TestCase):
    def test_there_is_a_machine(self):
        self.assertTrue(MACHINES)

    def test_valid(self):
        for path in MACHINES:
            with self.subTest(machine=path.name):
                jsonschema.validate(load(path), schema("machine"))

    def test_lanes_and_housekeeping(self):
        for path in MACHINES:
            machine = load(path)
            with self.subTest(machine=path.name):
                housekeeping = set(machine["housekeeping"])
                for lane, cpus in machine["lanes"].items():
                    if len(cpus) == 1:
                        self.assertFalse(housekeeping & set(cpus), f"the single-CPU lane {lane} is not housekeeping")

    def test_the_name_is_the_file_name(self):
        for path in MACHINES:
            self.assertEqual(load(path)["name"], path.stem)


class Seasons(unittest.TestCase):
    def test_there_is_a_season(self):
        self.assertTrue(SEASONS)

    def test_valid(self):
        for path in SEASONS:
            with self.subTest(season=path.name):
                jsonschema.validate(load(path), schema("scoring"))

    def test_the_season_is_the_file_name(self):
        for path in SEASONS:
            self.assertEqual(load(path)["season"], path.stem)

    def test_categories_name_known_metrics_and_families(self):
        for path in SEASONS:
            season = load(path)
            for category in season["categories"]:
                with self.subTest(season=path.name, category=category["id"]):
                    self.assertLessEqual(set(category["metrics"]), set(season["metrics"]))
                    self.assertLessEqual(set(category["families"]), set(season["families"]))

    def test_category_weights_sum_to_one(self):
        for path in SEASONS:
            for category in load(path)["categories"]:
                with self.subTest(season=path.name, category=category["id"]):
                    self.assertAlmostEqual(math.fsum(category["metrics"].values()), 1.0)
                    self.assertAlmostEqual(math.fsum(category["families"].values()), 1.0)

    def test_category_ids_are_unique(self):
        for path in SEASONS:
            ids = [c["id"] for c in load(path)["categories"]]
            self.assertEqual(len(ids), len(set(ids)), path.name)

    def test_pinned_family_weights_leave_room(self):
        for path in SEASONS:
            for family_id, family in load(path)["families"].items():
                pinned = math.fsum(t["weight"] for t in family["tests"] if isinstance(t, dict))
                self.assertLessEqual(pinned, 1.0, f"{path.name}: {family_id}")

    def test_every_family_pattern_names_an_arena_test(self):
        names = arena_test_names()
        for path in SEASONS:
            for family_id, family in load(path)["families"].items():
                for entry in family["tests"]:
                    pattern = entry["name"] if isinstance(entry, dict) else entry
                    with self.subTest(season=path.name, family=family_id, pattern=pattern):
                        self.assertTrue(any(fnmatch.fnmatchcase(n, pattern) for n in names),
                                        f"{pattern} matches no test under tests/")

    def test_cross_references(self):
        for path in SEASONS:
            season = load(path)
            ids = {c["id"] for c in season["categories"]}
            with self.subTest(season=path.name):
                self.assertLessEqual(set(season["pareto"]), ids)
                self.assertIn(season["sensitivity"]["category"], ids)
                self.assertIn(season["scaling"]["category"], ids)
                self.assertTrue((ROOT / "checkers" / f"{season['reference']}.yaml").exists())
                self.assertIn(season["reference"], season["unranked"])


class SeasonOnMachine(unittest.TestCase):
    def test_every_season_fits_every_machine(self):
        for season_path in SEASONS:
            season = load(season_path)
            for machine_path in MACHINES:
                machine = load(machine_path)
                with self.subTest(season=season_path.name, machine=machine_path.name):
                    for metric_id, metric in season["metrics"].items():
                        self.assertIn(metric["lane"], machine["lanes"], metric_id)
                    scaling = season["scaling"]
                    self.assertEqual(scaling["parallel_cpus"], len(machine["lanes"][scaling["parallel"]]))
                    self.assertEqual(len(machine["lanes"][scaling["serial"]]), 1)
                    self.assertEqual(season["caps"]["memory_bytes"], machine["memory"]["max_bytes"])


if __name__ == "__main__":
    unittest.main()
