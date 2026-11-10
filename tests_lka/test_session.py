"""A measurement session: what it runs, in what order, when it stops, what it keeps."""

import json
import tempfile
import unittest
from pathlib import Path

from lka_quiet.plan import Cell
from lka_quiet.session import RunStore, Session, build_request, current_records

POLICY = {
    "scored": {"n_min": 7, "n_max": 15, "ci_rel": 0.01, "confidence": 0.95},
    "heavy": {"threshold_s": 60, "n_min": 3, "n_max": 5, "spread_rel": 0.01},
    "verdict_only": {"n": 3},
    "failure": {"n": 2},
}

MACHINE = {
    "name": "test-box",
    "lanes": {"4core": [0, 1, 2, 3], "1core": [3]},
    "timeouts": {"box_factor": 2.0, "default_s": 21600, "minimum_s": 30},
    "repetitions": POLICY,
    "sandbox": {"user_prefix": "lkc-", "homes": "/srv/lka/checkers", "tmp_bytes": 1 << 30},
    "sentinel": {"checker": "official", "test": "init-prelude", "every_s": 1000, "limits": {"mean_s": 1.0, "sigma_s": 0.01, "k": 4}},
}

CHECKERS = [
    {"name": "official", "dir": "official", "run": '.lake/build/bin/kernel "$IN"', "declines": [],
     "source_hash": "h-official"},
    {"name": "fast", "url": "https://example.invalid/fast", "run": 'fast "$IN"', "declines": ["mathlib"],
     "source_hash": "h-fast", "timeout": 30},
]

TESTS = [
    {"name": "init-prelude", "outcome": "accept", "sha256": "s-prelude", "path": "/srv/lka/tests/s-prelude.ndjson", "timeout": 50},
    {"name": "mathlib", "outcome": "accept", "sha256": "s-mathlib", "path": "/srv/lka/tests/s-mathlib.ndjson", "timeout": 5680},
    {"name": "bugs/bad", "outcome": "reject", "sha256": "s-bad", "path": "/srv/lka/tests/s-bad.ndjson"},
]


class FakeMeasure:
    """Stands in for lka-measure: answers each request as the test's outcome, at 1 s."""

    def __init__(self, clock=None, invalid_first=0, wall=1.0):
        self.calls = []
        self.clock = clock
        self.invalid_first = invalid_first
        self.wall = wall

    def __call__(self, request):
        self.calls.append(request)
        if self.clock is not None:
            self.clock.now += self.wall
        status = {"accept": "accepted", "reject": "rejected", "either": "accepted"}[request["expected"]]
        valid = len(self.calls) > self.invalid_first
        return {
            "schema": "lka-quiet-run/1", **{k: request[k] for k in (
                "checker", "test", "lane", "expected", "timeout_s", "checker_hash", "test_sha256",
                "epoch", "round", "pass", "seq")},
            "status": status, "wall_s": self.wall, "memory_peak_bytes": 1 << 26, "energy_j": 1.0,
            "valid": valid, "invalid_reasons": [] if valid else ["interference: test"],
        }


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def session(tmp, measure=None, clock=None, sentinel=None, round_id="round-1", checkers=CHECKERS):
    store = RunStore(Path(tmp), epoch="epoch-1")
    clock = clock or Clock()
    return Session(store=store, checkers=checkers, tests=TESTS, machine=MACHINE, round_id=round_id,
                   env={"PATH": "/nix/store/shell/bin:/usr/bin:/bin"}, measure=measure or FakeMeasure(clock),
                   clock=clock, sentinel=sentinel), store


class Store(unittest.TestCase):
    def test_round_trip_and_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), epoch="e")
            a = store.append({"checker": "c", "test": "t", "lane": "1core", "valid": True})
            b = store.append({"checker": "c", "test": "t/x", "lane": "1core", "valid": True})
            self.assertEqual((a["seq"], b["seq"]), (0, 1))
            again = RunStore(Path(tmp), epoch="e")
            self.assertEqual([r["seq"] for r in again.records()], [0, 1])
            self.assertEqual(again.next_seq(), 2)

    def test_epochs_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            RunStore(Path(tmp), epoch="e1").append({"checker": "c", "test": "t", "lane": "1core", "valid": True})
            self.assertEqual(RunStore(Path(tmp), epoch="e2").records(), [])

    def test_quarantine_marks_runs_invalid_but_keeps_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), epoch="e")
            for _ in range(3):
                store.append({"checker": "c", "test": "t", "lane": "1core", "valid": True, "invalid_reasons": []})
            store.quarantine([1, 2], "sentinel out of limits")
            records = RunStore(Path(tmp), epoch="e").records()
            self.assertEqual([r["valid"] for r in records], [True, False, False])
            self.assertIn("quarantined: sentinel out of limits", records[2]["invalid_reasons"])

    def test_files_are_complete_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), epoch="e")
            store.append({"checker": "c", "test": "perf/x", "lane": "4core", "valid": True})
            files = list((Path(tmp) / "e" / "runs").iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text())["test"], "perf/x")
            self.assertNotIn("/", files[0].name.replace(".json", ""))


class Identity(unittest.TestCase):
    def test_only_records_of_the_current_checker_and_test_count(self):
        records = [
            {"checker": "c", "test": "t", "lane": "1core", "checker_hash": "old", "test_sha256": "s", "valid": True},
            {"checker": "c", "test": "t", "lane": "1core", "checker_hash": "new", "test_sha256": "s", "valid": True},
            {"checker": "c", "test": "t", "lane": "1core", "checker_hash": "new", "test_sha256": "other", "valid": True},
            {"checker": "c", "test": "t", "lane": "1core", "checker_hash": "new", "test_sha256": "s", "valid": True,
             "kind": "sentinel"},
        ]
        current = current_records(records, {"c": "new"}, {"t": "s"})
        self.assertEqual(len(current[Cell("c", "t", "1core")]), 1)


class Requests(unittest.TestCase):
    def test_fields(self):
        request = build_request(CHECKERS[1], TESTS[0], "1core", MACHINE, env={"PATH": "/p"}, round_id="r",
                                pass_index=2, seq=9, epoch="e")
        self.assertEqual(request["workdir"], "/srv/lka/checkers/fast/arena/_build/checkers/fast/src")
        self.assertEqual(request["input"], "/srv/lka/tests/s-prelude.ndjson")
        self.assertEqual(request["cmd"], 'fast "$IN"')
        self.assertEqual(request["timeout_s"], 60.0)  # min(50, 30) x 2.0
        self.assertEqual(request["expected"], "accept")
        self.assertEqual((request["checker_hash"], request["test_sha256"]), ("h-fast", "s-prelude"))
        self.assertEqual((request["round"], request["pass"], request["seq"], request["epoch"]), ("r", 2, 9, "e"))
        self.assertEqual(request["env"], {"PATH": "/p"})

    def test_a_run_only_checker_runs_in_its_own_directory(self):
        simple = {"name": "overfull", "run": "./check.py \"$IN\"", "declines": [], "source_hash": "h"}
        request = build_request(simple, TESTS[0], "1core", MACHINE, env={}, round_id="r", pass_index=0, seq=0, epoch="e")
        self.assertEqual(request["workdir"], "/srv/lka/checkers/overfull/arena/_build/checkers/overfull")


class Sessions(unittest.TestCase):
    def test_declines_are_recorded_without_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess, store = session(tmp)
            sess.run(budget_s=1e9)
            declined = [r for r in store.records() if r["checker"] == "fast" and r["test"] == "mathlib"]
            self.assertEqual(len(declined), 2)
            self.assertTrue(all(r["declared"] and r["status"] == "declined" for r in declined))
            self.assertFalse(any(c["checker"] == "fast" and c["test"] == "mathlib" for c in sess.measure.calls))

    def test_runs_to_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess, store = session(tmp)
            summary = sess.run(budget_s=1e9)
            self.assertEqual(summary["outcome"], "complete")
            counts = {}
            for call in sess.measure.calls:
                key = (call["checker"], call["test"], call["lane"])
                counts[key] = counts.get(key, 0) + 1
            self.assertEqual(counts[("official", "mathlib", "4core")], 7)
            self.assertEqual(counts[("official", "bugs/bad", "1core")], 3)
            self.assertEqual(summary["pending"], 0)

    def test_passes_interleave(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess, _ = session(tmp)
            sess.run(budget_s=1e9)
            first_pass = [c for c in sess.measure.calls if c["pass"] == 0]
            cells = {(c["checker"], c["test"], c["lane"]) for c in first_pass}
            self.assertEqual(len(first_pass), len(cells), "each cell once per pass")
            self.assertEqual(len(cells), 2 * 3 * 2 - 2)

    def test_budget_stops_and_the_next_session_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            sess, store = session(tmp, clock=clock)
            summary = sess.run(budget_s=5)
            self.assertEqual(summary["outcome"], "budget")
            done_first = len(sess.measure.calls)
            self.assertLessEqual(done_first, 6)
            sess2, _ = session(tmp, clock=clock)
            summary2 = sess2.run(budget_s=1e9)
            self.assertEqual(summary2["outcome"], "complete")
            self.assertGreater(min(c["pass"] for c in sess2.measure.calls), -1)
            seqs = [r["seq"] for r in store.records()]
            self.assertEqual(seqs, sorted(set(seqs)))

    def test_invalid_runs_are_repeated(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            sess, _ = session(tmp, measure=FakeMeasure(clock, invalid_first=4), clock=clock)
            sess.run(budget_s=1e9)
            official_mathlib = [c for c in sess.measure.calls if (c["checker"], c["test"]) == ("official", "mathlib")]
            self.assertGreaterEqual(len(official_mathlib), 14)

    def test_a_failed_sentinel_quarantines_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            readings = iter([True, False])

            def sentinel():
                return next(readings), {"checker": "official", "test": "init-prelude", "lane": "1core",
                                        "kind": "sentinel", "valid": True, "wall_s": 1.0}

            machine = {**MACHINE, "sentinel": {**MACHINE["sentinel"], "every_s": 4}}
            store = RunStore(Path(tmp), epoch="epoch-1")
            sess = Session(store=store, checkers=CHECKERS, tests=TESTS, machine=machine, round_id="round-1",
                           env={}, measure=FakeMeasure(clock), clock=clock, sentinel=sentinel)
            summary = sess.run(budget_s=1e9)
            self.assertEqual(summary["outcome"], "sentinel")
            measured = [r for r in store.records() if r.get("kind") != "sentinel" and not r.get("declared")]
            self.assertTrue(measured)
            self.assertTrue(all(not r["valid"] for r in measured))
            self.assertTrue(all(any("quarantined" in reason for reason in r["invalid_reasons"]) for r in measured))

    def test_runs_before_the_last_good_reading_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            readings = iter([True, True, False])

            def sentinel():
                return next(readings), {"checker": "official", "test": "init-prelude", "lane": "1core",
                                        "kind": "sentinel", "valid": True, "wall_s": 1.0}

            machine = {**MACHINE, "sentinel": {**MACHINE["sentinel"], "every_s": 4}}
            store = RunStore(Path(tmp), epoch="epoch-1")
            sess = Session(store=store, checkers=CHECKERS, tests=TESTS, machine=machine, round_id="round-1",
                           env={}, measure=FakeMeasure(clock), clock=clock, sentinel=sentinel)
            self.assertEqual(sess.run(budget_s=1e9)["outcome"], "sentinel")
            records = store.records()
            good = max(r["seq"] for r in records if r.get("kind") == "sentinel" and r["seq"] < max(
                s["seq"] for s in records if s.get("kind") == "sentinel"))
            measured = [r for r in records if r.get("kind") != "sentinel" and not r.get("declared")]
            before = [r for r in measured if r["seq"] < good]
            after = [r for r in measured if r["seq"] > good]
            self.assertEqual(len(before), 4)
            self.assertTrue(all(r["valid"] for r in before))
            self.assertEqual(len(after), 4)
            self.assertTrue(all(not r["valid"] for r in after))

    def test_a_new_checker_revision_is_measured_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess, _ = session(tmp)
            sess.run(budget_s=1e9)
            revised = [CHECKERS[0], {**CHECKERS[1], "source_hash": "h-fast-2"}]
            sess2, _ = session(tmp, checkers=revised, round_id="round-2")
            sess2.run(budget_s=1e9)
            self.assertTrue(sess2.measure.calls)
            self.assertEqual({c["checker"] for c in sess2.measure.calls}, {"fast"})


if __name__ == "__main__":
    unittest.main()
