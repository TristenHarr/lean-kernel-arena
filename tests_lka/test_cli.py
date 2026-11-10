"""Staging the official round's inputs, the runs' environment, and scoring a results store."""

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from lka_quiet.cli import StageError, built_checkers, encode_round, run_env, score_store, stage_tests
from lka_quiet.encode import fingerprint
from lka_quiet.session import RunStore


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


GOOD = b'{"meta": "good"}\n'
BAD = b'{"meta": "bad"}\n'
EITHER = b'{"meta": "either"}\n'
BIG = b'{"meta": "mathlib"}\n' * 4


def published(tmp: Path, cedar_bytes=b'{"meta": "cedar"}\n') -> Path:
    results = {
        "meta": {"git_revision": "abc123"},
        "tests": [
            {"name": "tutorial/good1", "outcome": "accept", "sha256": sha(GOOD), "size": len(GOOD)},
            {"name": "bugs/bad1", "outcome": "reject", "sha256": sha(BAD), "size": len(BAD)},
            {"name": "corner-cases/c1", "outcome": "either", "sha256": sha(EITHER), "size": len(EITHER)},
            {"name": "mathlib", "outcome": "accept", "sha256": sha(BIG), "size": len(BIG), "timeout": 5680},
            {"name": "cedar", "outcome": "accept", "sha256": sha(cedar_bytes), "size": len(cedar_bytes), "timeout": 940},
        ],
    }
    (tmp / "results.json").write_text(json.dumps(results))
    with tarfile.open(tmp / "lean-arena-tests.tar.gz", "w:gz") as tar:
        for arcname, data in (("good/tutorial/good1.ndjson", GOOD), ("bad/bugs/bad1.ndjson", BAD)):
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return tmp


def exports(tmp: Path, cedar=b'{"meta": "cedar, rebuilt"}\n') -> Path:
    root = tmp / "exports"
    (root / "corner-cases").mkdir(parents=True, exist_ok=True)
    (root / "mathlib.ndjson").write_bytes(BIG)
    (root / "corner-cases" / "c1.ndjson").write_bytes(EITHER)
    (root / "cedar.ndjson").write_bytes(cedar)
    return root


class Staging(unittest.TestCase):
    def test_every_test_staged_by_its_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = published(tmp, cedar_bytes=b'{"meta": "cedar, rebuilt"}\n')
            manifest = stage_tests(src / "results.json", tarball=src / "lean-arena-tests.tar.gz",
                                   sources=[exports(tmp)], store=tmp / "store", allow_mismatch=set())
            self.assertEqual(manifest["arena_revision"], "abc123")
            by_name = {t["name"]: t for t in manifest["tests"]}
            self.assertEqual(set(by_name), {"tutorial/good1", "bugs/bad1", "corner-cases/c1", "mathlib", "cedar"})
            for test in manifest["tests"]:
                path = Path(test["path"])
                self.assertEqual(sha(path.read_bytes()), test["sha256"])
                self.assertTrue(test["official_match"])
                self.assertEqual(path.parent.parent, tmp / "store")
            self.assertEqual(by_name["mathlib"]["timeout"], 5680)
            self.assertEqual(by_name["bugs/bad1"]["outcome"], "reject")

    def test_identical_bytes_are_stored_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = published(tmp, cedar_bytes=b'{"meta": "cedar, rebuilt"}\n')
            first = stage_tests(src / "results.json", tarball=src / "lean-arena-tests.tar.gz", sources=[exports(tmp)],
                                store=tmp / "store", allow_mismatch=set())
            second = stage_tests(src / "results.json", tarball=src / "lean-arena-tests.tar.gz", sources=[exports(tmp)],
                                 store=tmp / "store", allow_mismatch=set())
            self.assertEqual(first, second)

    def test_linking_shares_the_source_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = published(tmp, cedar_bytes=b'{"meta": "cedar, rebuilt"}\n')
            manifest = stage_tests(src / "results.json", tarball=src / "lean-arena-tests.tar.gz", sources=[exports(tmp)],
                                   store=tmp / "store", allow_mismatch=set(), link=True)
            mathlib = Path(next(t["path"] for t in manifest["tests"] if t["name"] == "mathlib"))
            self.assertEqual(mathlib.stat().st_ino, (tmp / "exports" / "mathlib.ndjson").stat().st_ino)

    def test_a_mismatch_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = published(tmp)
            with self.assertRaisesRegex(StageError, "cedar"):
                stage_tests(src / "results.json", tarball=src / "lean-arena-tests.tar.gz", sources=[exports(tmp)],
                            store=tmp / "store", allow_mismatch=set())

    def test_an_allowed_mismatch_is_staged_and_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = published(tmp)
            manifest = stage_tests(src / "results.json", tarball=src / "lean-arena-tests.tar.gz",
                                   sources=[exports(tmp)], store=tmp / "store", allow_mismatch={"cedar"})
            cedar = next(t for t in manifest["tests"] if t["name"] == "cedar")
            self.assertFalse(cedar["official_match"])
            self.assertEqual(cedar["sha256"], sha(b'{"meta": "cedar, rebuilt"}\n'))
            self.assertEqual(cedar["official_sha256"], sha(b'{"meta": "cedar"}\n'))

    def test_a_missing_test_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = published(tmp)
            with self.assertRaisesRegex(StageError, "mathlib"):
                stage_tests(src / "results.json", tarball=src / "lean-arena-tests.tar.gz", sources=[],
                            store=tmp / "store", allow_mismatch={"cedar"})

    def test_tarball_members_cannot_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = published(tmp, cedar_bytes=b'{"meta": "cedar, rebuilt"}\n')
            with tarfile.open(src / "lean-arena-tests.tar.gz", "w:gz") as tar:
                info = tarfile.TarInfo("good/../../evil.ndjson")
                info.size = len(GOOD)
                tar.addfile(info, io.BytesIO(GOOD))
            with self.assertRaisesRegex(StageError, "tutorial/good1"):
                stage_tests(src / "results.json", tarball=src / "lean-arena-tests.tar.gz", sources=[exports(tmp)],
                            store=tmp / "store", allow_mismatch=set())
            self.assertFalse((tmp / "evil.ndjson").exists())


EXPORTS = Path(__file__).parent / "fixtures" / "exports"
ROUND_TESTS = {"subject-reduction-redex": "accept", "init-prefix": "accept", "proj-maybe-prop": "accept",
               "let-value-type-mismatch": "either", "proof-param-swap": "either"}


def staged_round(tmp: Path) -> dict:
    """A manifest staging real exports of the Arena's tests."""
    import gzip
    root = tmp / "exports"
    root.mkdir()
    tests = []
    for name, outcome in ROUND_TESTS.items():
        data = gzip.open(EXPORTS / f"{name}.ndjson.gz").read()
        (root / f"{name}.ndjson").write_bytes(data)
        tests.append({"name": name, "outcome": outcome, "sha256": sha(data), "size": len(data), "timeout": 300})
    (tmp / "results.json").write_text(json.dumps({"meta": {"git_revision": "r1"}, "tests": tests}))
    return stage_tests(tmp / "results.json", tarball=None, sources=[root], store=tmp / "store", allow_mismatch=set())


class RoundInputs(unittest.TestCase):
    def test_every_test_is_measured_in_new_bytes_with_the_same_declarations(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            staged = staged_round(tmp)
            manifest = encode_round(staged, seed="round-2026-12 secret", store=tmp / "store", twin_from=[])
            self.assertEqual(manifest["seed_sha256"], sha(b"round-2026-12 secret"))
            originals = {t["name"]: t for t in staged["tests"]}
            for test in manifest["tests"]:
                original = originals[test["name"]]
                path = Path(test["path"])
                with self.subTest(test=test["name"]):
                    self.assertEqual(test["sha256"], sha(path.read_bytes()))
                    self.assertNotEqual(test["sha256"], original["sha256"])
                    self.assertEqual(test["official_sha256"], original["official_sha256"])
                    self.assertEqual(test["encoded_from"], original["sha256"])
                    self.assertEqual(fingerprint(path.read_text()), fingerprint(Path(original["path"]).read_text()))
                    self.assertEqual((test["outcome"], test["timeout"]), (original["outcome"], original["timeout"]))
                    self.assertEqual(test["size"], path.stat().st_size)

    def test_a_seed_gives_one_set_of_inputs_and_another_seed_another(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            staged = staged_round(tmp)
            a = encode_round(staged, seed="a", store=tmp / "store", twin_from=["*"])
            again = encode_round(staged, seed="a", store=tmp / "store", twin_from=["*"])
            b = encode_round(staged, seed="b", store=tmp / "store", twin_from=["*"])
            shas = lambda m: [t["sha256"] for t in m["tests"]]
            self.assertEqual(shas(a), shas(again))
            self.assertTrue(set(shas(a)).isdisjoint(shas(b)))

    def test_twins_are_invalid_tests_of_the_accepted_tests_the_patterns_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            staged = staged_round(tmp)
            manifest = encode_round(staged, seed="s", store=tmp / "store", twin_from=["subject-*", "init-prefix"],
                                    twin_prefix=150)
            fresh = {t["name"]: t for t in manifest["tests"] if t["name"].startswith("fresh/")}
            self.assertEqual(set(fresh), {"fresh/subject-reduction-redex", "fresh/init-prefix"})
            originals = {t["name"]: t for t in staged["tests"]}
            for name, twin in fresh.items():
                source = originals[twin["twin_of"]]
                before = fingerprint(Path(source["path"]).read_text())
                after = fingerprint(Path(twin["path"]).read_text())
                with self.subTest(twin=name):
                    self.assertEqual(twin["outcome"], "reject")
                    self.assertEqual(twin["timeout"], source["timeout"])
                    self.assertEqual(twin["sha256"], sha(Path(twin["path"]).read_bytes()))
                    self.assertEqual(sum(a != b for a, b in zip(before, after)), 1)
            self.assertEqual(manifest["twins_refused"], [])

    def test_only_accepted_tests_are_twinned(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            manifest = encode_round(staged_round(tmp), seed="s", store=tmp / "store", twin_from=["*"])
            twinned = {t["twin_of"] for t in manifest["tests"] if t["name"].startswith("fresh/")}
            self.assertTrue(twinned <= {n for n, o in ROUND_TESTS.items() if o == "accept"})

    def test_a_test_without_a_verified_twin_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            manifest = encode_round(staged_round(tmp), seed="s", store=tmp / "store", twin_from=["*"],
                                    verify=lambda path: False)
            self.assertEqual([t for t in manifest["tests"] if t["name"].startswith("fresh/")], [])
            self.assertEqual(sorted(manifest["twins_refused"]), ["init-prefix", "proj-maybe-prop", "subject-reduction-redex"])

    def test_the_seed_itself_is_never_in_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            manifest = encode_round(staged_round(tmp), seed="do not publish me", store=tmp / "store", twin_from=["*"])
            self.assertNotIn("do not publish me", json.dumps(manifest))


class Environment(unittest.TestCase):
    def test_volatile_variables_are_dropped(self):
        env = run_env({"PATH": "/nix/store/a/bin:/usr/bin", "PWD": "/x", "OLDPWD": "/y", "SHLVL": "2",
                       "_": "/usr/bin/python3", "GITHUB_RUN_ID": "1", "RUNNER_TEMP": "/t", "ACTIONS_X": "1",
                       "SSH_AUTH_SOCK": "/s", "DISPLAY": ":0", "XDG_RUNTIME_DIR": "/run/user/1",
                       "TMPDIR": "/tmp/nix-shell.x", "NIX_BUILD_TOP": "/tmp/nix", "TERM": "xterm",
                       "LKA_MACHINE": "/etc/lka/machine.json", "JAVA_HOME": "/nix/store/jdk"})
        self.assertEqual(env, {"PATH": "/nix/store/a/bin:/usr/bin", "JAVA_HOME": "/nix/store/jdk"})


class Scoring(unittest.TestCase):
    def test_a_store_scores_to_the_same_bytes_twice(self):
        from tests_lka.test_board import SCORING, TESTS, checker_runs
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), epoch="e1")
            for record in checker_runs("official") + checker_runs("fast", scale=0.5, mem_scale=0.5, energy_scale=0.5):
                store.append({**record, "checker_hash": f"h-{record['checker']}", "test_sha256": f"s-{record['test']}"})
            tests = [{**t, "sha256": f"s-{t['name']}"} for t in TESTS]
            checkers = [{"name": "official", "source_hash": "h-official"}, {"name": "fast", "source_hash": "h-fast"}]
            first = score_store(store, tests, checkers, SCORING, machine={"name": "box"})
            second = score_store(store, tests, checkers, SCORING, machine={"name": "box"})
            self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
            fast = next(c for c in first["checkers"] if c["name"] == "fast")
            self.assertAlmostEqual(fast["categories"]["overall"]["factor"], 2.0, places=2)
            self.assertEqual(first["machine"], {"name": "box"})

    def test_stale_revisions_do_not_score(self):
        from tests_lka.test_board import SCORING, TESTS, checker_runs
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), epoch="e1")
            for record in checker_runs("official") + checker_runs("fast", scale=0.5):
                store.append({**record, "checker_hash": f"h-{record['checker']}", "test_sha256": f"s-{record['test']}"})
            tests = [{**t, "sha256": f"s-{t['name']}"} for t in TESTS]
            checkers = [{"name": "official", "source_hash": "h-official"}, {"name": "fast", "source_hash": "h-fast-new"}]
            board = score_store(store, tests, checkers, SCORING, machine={"name": "box"})
            fast = next(c for c in board["checkers"] if c["name"] == "fast")
            self.assertIsNone(fast["categories"]["overall"]["score"])


if __name__ == "__main__":
    unittest.main()


class Builds(unittest.TestCase):
    def test_only_the_current_source_counts_as_built(self):
        checkers = [{"name": "a", "source_hash": "h1"}, {"name": "b", "source_hash": "h2"},
                    {"name": "c", "source_hash": "h3"}, {"name": "d", "source_hash": "h4"}]
        builds = {"a": {"ok": True, "source_hash": "h1"}, "b": {"ok": True, "source_hash": "old"},
                  "c": {"ok": False, "source_hash": "h3"}}
        ok, not_built = built_checkers(checkers, builds)
        self.assertEqual([c["name"] for c in ok], ["a"])
        self.assertEqual([c["name"] for c in not_built], ["b", "c", "d"])
