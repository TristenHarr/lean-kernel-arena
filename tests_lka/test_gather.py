"""Gathering a round's large exports for the box: each test's file found by its published sha256 among
the given directories, hard-linked under the name staging reads, and every test accounted for."""

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gather", ROOT / "quietbox" / "gather-exports.py")
gather = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gather)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Gather(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.old = root / "old-round" / "mathlib"
        self.old.mkdir(parents=True)
        self.new = root / "inputs" / "std"
        self.new.mkdir(parents=True)
        (self.old / "mathlib.ndjson").write_bytes(b"mathlib bytes\n")
        (self.new / "std.ndjson").write_bytes(b"std bytes\n")
        (self.new / "std-old.ndjson").write_bytes(b"an older std, longer\n")
        (root / "inputs" / "cedar.mismatch.ndjson").write_bytes(b"cedar, another build\n")
        self.results = {"tests": [
            {"name": "mathlib", "outcome": "accept", "sha256": sha(b"mathlib bytes\n"), "size": 14},
            {"name": "std", "outcome": "accept", "sha256": sha(b"std bytes\n"), "size": 10},
            {"name": "cedar", "outcome": "accept", "sha256": sha(b"the official cedar\n"), "size": 19},
            {"name": "reachability", "outcome": "accept", "sha256": sha(b"nowhere\n"), "size": 8},
            {"name": "tutorial/x", "outcome": "accept", "sha256": sha(b"in the tarball\n"), "size": 15},
        ]}
        self.roots = [root / "old-round", root / "inputs"]
        self.out = root / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_test_is_found_by_its_sha256_and_linked_by_name(self):
        report = gather.gather(self.results, self.roots, self.out, names=["mathlib", "std", "reachability"],
                               allow_mismatch={})
        self.assertEqual(report["found"], ["mathlib", "std"])
        self.assertEqual(report["missing"], ["reachability"])
        self.assertEqual((self.out / "std.ndjson").read_bytes(), b"std bytes\n")
        self.assertEqual(os.stat(self.out / "mathlib.ndjson").st_ino, os.stat(self.old / "mathlib.ndjson").st_ino)

    def test_a_file_of_the_wrong_size_is_never_hashed(self):
        hashed = []
        original = gather._sha_file
        gather._sha_file = lambda path: hashed.append(path) or original(path)
        try:
            gather.gather(self.results, self.roots, self.out, names=["std"], allow_mismatch={})
        finally:
            gather._sha_file = original
        self.assertEqual([p.name for p in hashed], ["std.ndjson"])

    def test_an_allowed_mismatch_is_linked_by_name_and_reported(self):
        report = gather.gather(self.results, self.roots, self.out, names=["cedar"],
                               allow_mismatch={"cedar": "cedar.mismatch.ndjson"})
        self.assertEqual(report["mismatched"], ["cedar"])
        self.assertEqual((self.out / "cedar.ndjson").read_bytes(), b"cedar, another build\n")

    def test_the_manifest_records_what_each_file_is(self):
        report = gather.gather(self.results, self.roots, self.out, names=["mathlib", "std"], allow_mismatch={})
        manifest = json.loads((self.out / "SHA256SUMS.json").read_text())
        self.assertEqual(manifest["std"], sha(b"std bytes\n"))
        self.assertEqual(report["bytes"], 14 + 10)

    def test_gathering_again_changes_nothing(self):
        gather.gather(self.results, self.roots, self.out, names=["std"], allow_mismatch={})
        before = os.stat(self.out / "std.ndjson").st_ino
        gather.gather(self.results, self.roots, self.out, names=["std"], allow_mismatch={})
        self.assertEqual(os.stat(self.out / "std.ndjson").st_ino, before)

    def test_the_large_tests_are_those_not_in_the_tarball(self):
        self.assertEqual(gather.large_tests(self.results, tarball_members={"tutorial/x"}),
                         ["cedar", "mathlib", "reachability", "std"])


if __name__ == "__main__":
    unittest.main()
