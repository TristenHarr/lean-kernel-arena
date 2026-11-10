"""Invalid twins: a valid export with one declaration's value swapped for an earlier one's of another
type, re-encoded, so that a checker answering a known input from memory accepts an invalid proof.

The rich fixtures are exports the official kernel accepts: subject-reduction-redex from the Arena's
corner cases, and init-prefix, the first 400 declarations of the Arena's init export. With
LKA_OFFICIAL_KERNEL naming a built official checker, every twin of them is one the kernel rejects."""

import gzip
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from lka_quiet.encode import fingerprint, parse
from lka_quiet.twins import candidates, declarations, make_twin, twin_file

EXPORTS = {p.name.removesuffix(".ndjson.gz"): p for p in sorted((Path(__file__).parent / "fixtures" / "exports").glob("*.ndjson.gz"))}
RICH = ("init-prefix", "subject-reduction-redex")


def read(name: str) -> str:
    with gzip.open(EXPORTS[name], "rt") as f:
        return f.read()


class Candidates(unittest.TestCase):
    def test_a_donor_comes_before_its_target_with_the_same_universes_and_another_type(self):
        for name in RICH:
            text = read(name)
            decls = declarations(parse(text))
            pairs = candidates(text)
            with self.subTest(export=name):
                self.assertGreater(len(pairs), 3)
                for target, donor in pairs:
                    t, d = decls[target], decls[donor]
                    self.assertLess(donor, target)
                    self.assertEqual(t.kind, d.kind)
                    self.assertIn(t.kind, ("thm", "def"))
                    self.assertEqual(t.level_names, d.level_names)
                    self.assertNotEqual(t.type_hash, d.type_hash)

    def test_an_export_with_nothing_to_swap_has_no_candidates(self):
        self.assertEqual(candidates(read("let-value-type-mismatch")), [])
        self.assertEqual(candidates(read("nested-nonuniform-param")), [])


class Twins(unittest.TestCase):
    def test_exactly_one_declaration_changes_and_it_takes_the_donors_value(self):
        for name in RICH:
            text = read(name)
            twin = make_twin(text, "season-1")
            with self.subTest(export=name):
                before, after = fingerprint(text), fingerprint(twin.text)
                self.assertEqual(len(before), len(after))
                changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
                self.assertEqual(changed[:1], [twin.target_index])
                twin_decls = declarations(parse(twin.text))
                original = declarations(parse(text))
                self.assertEqual(twin_decls[twin.target_index].value_hash, original[twin.donor_index].value_hash)
                self.assertEqual(twin_decls[twin.target_index].type_hash, original[twin.target_index].type_hash)
                self.assertEqual(twin.target, original[twin.target_index].name)
                self.assertEqual(twin.donor, original[twin.donor_index].name)

    def test_only_declarations_after_the_target_can_change_with_it(self):
        text = read("alg-conv-trans-acc")
        twin = make_twin(text, "x")
        before, after = fingerprint(text), fingerprint(twin.text)
        self.assertEqual(before[:twin.target_index], after[:twin.target_index])

    def test_the_twin_is_re_encoded(self):
        text = read("alg-conv-trans-acc")
        twin = make_twin(text, "season-1")
        self.assertNotEqual(twin.text.splitlines()[1:40], text.splitlines()[1:40])
        self.assertNotEqual(twin.text, make_twin(text, "season-2").text)

    def test_a_seed_gives_one_twin_and_seeds_differ(self):
        text = read("subject-reduction-redex")
        self.assertEqual(make_twin(text, "a").text, make_twin(text, "a").text)
        targets = {(make_twin(text, s).target_index, make_twin(text, s).donor_index) for s in "abcdefgh"}
        self.assertGreater(len(targets), 1)

    def test_within_keeps_the_target_early(self):
        text = read("subject-reduction-redex")
        total = len(declarations(parse(text)))
        for seed in "abcdefgh":
            twin = make_twin(text, seed, within=0.5)
            self.assertLess(twin.target_index, total * 0.5)

    def test_candidates_are_tried_until_one_is_verified(self):
        text = read("alg-conv-trans-acc")
        seen = []

        def verify(candidate_text):
            seen.append(candidate_text)
            return len(seen) == 3

        twin = make_twin(text, "season-1", verify=verify)
        self.assertEqual(len(seen), 3)
        self.assertEqual(twin.text, seen[-1])
        self.assertEqual(twin.tried, 3)

    def test_at_most_max_tries_candidates_are_tried(self):
        seen = []
        self.assertIsNone(make_twin(read("init-prefix"), "s", verify=lambda text: seen.append(1) and False, max_tries=5))
        self.assertEqual(len(seen), 5)

    def test_no_twin_when_nothing_verifies(self):
        self.assertIsNone(make_twin(read("alg-conv-trans-acc"), "s", verify=lambda _: False))
        self.assertIsNone(make_twin(read("let-value-type-mismatch"), "s"))


class TwinFiles(unittest.TestCase):
    """twin_file: the same twin for an export of any size, reading only a prefix of declarations into
    memory and streaming the rest through the re-encoder."""

    def twin(self, name, seed, prefix, verify=None):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "in.ndjson", Path(tmp) / "out.ndjson"
            source.write_text(read(name))
            twin = twin_file(source, target, seed, prefix_declarations=prefix,
                             verify=(lambda path: verify(path.read_text())) if verify else None)
            return twin, (target.read_text() if twin else None)

    def test_a_whole_file_prefix_is_the_in_memory_twin(self):
        text = read("subject-reduction-redex")
        total = len(declarations(parse(text)))
        for seed in ("season-1", "x"):
            twin, out = self.twin("subject-reduction-redex", seed, total)
            expected = make_twin(text, seed)
            self.assertEqual((twin.target_index, twin.donor_index), (expected.target_index, expected.donor_index))
            self.assertEqual(out, expected.text)

    def test_the_target_lies_in_the_prefix_and_nothing_after_it_changes_but_the_target(self):
        text = read("init-prefix")
        before = fingerprint(text)
        for seed in "abcd":
            twin, out = self.twin("init-prefix", seed, 120)
            after = fingerprint(out)
            with self.subTest(seed=seed):
                self.assertLess(twin.target_index, 120)
                self.assertEqual([i for i, (a, b) in enumerate(zip(before, after)) if a != b], [twin.target_index])
                self.assertEqual(len(after), len(before))

    def test_candidates_are_tried_until_one_is_verified(self):
        seen = []
        twin, out = self.twin("init-prefix", "season-1", 200, verify=lambda text: seen.append(text) or len(seen) == 2)
        self.assertEqual(twin.tried, 2)
        self.assertEqual(out, seen[-1])

    def test_a_file_tries_at_most_max_tries_candidates(self):
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "in.ndjson", Path(tmp) / "out.ndjson"
            source.write_text(read("init-prefix"))
            twin = twin_file(source, target, "s", prefix_declarations=300, max_tries=4,
                             verify=lambda path: seen.append(path) and False)
            self.assertIsNone(twin)
            self.assertEqual(len(seen), 4)
            self.assertFalse(target.exists())

    def test_no_twin_when_the_prefix_has_nothing_to_swap(self):
        twin, out = self.twin("let-value-type-mismatch", "s", 10)
        self.assertIsNone(twin)


KERNEL = os.environ.get("LKA_OFFICIAL_KERNEL")


@unittest.skipUnless(KERNEL and Path(KERNEL).exists(), "LKA_OFFICIAL_KERNEL names no built official checker")
class TheKernelRejectsEveryTwin(unittest.TestCase):
    def verdict(self, text: str) -> int:
        with tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False) as f:
            f.write(text)
        try:
            return subprocess.run([KERNEL, f.name], capture_output=True, timeout=300).returncode
        finally:
            os.unlink(f.name)

    def test_a_twin_of_an_accepted_export_is_rejected(self):
        for name in RICH:
            text = read(name)
            self.assertEqual(self.verdict(text), 0, name)
            for seed in ("season-1", "round-2026-12", "x"):
                twin = make_twin(text, seed)
                with self.subTest(export=name, seed=seed, target=twin.target, donor=twin.donor):
                    self.assertEqual(self.verdict(twin.text), 1)


if __name__ == "__main__":
    unittest.main()
