"""Staging the official round's inputs, the runs' environment, and scoring a results store."""

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from lka_quiet.cli import StageError, run_env, score_store, stage_tests
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
