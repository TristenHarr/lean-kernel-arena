"""Re-encoding: the same declarations in new bytes, so no checker can answer a known file from memory.

The fixtures are exports from the Arena's own tests (tests_lka/fixtures/exports). With
LKA_OFFICIAL_KERNEL naming a built official checker (checkers/official), the kernel's verdict on
each re-encoded file is checked against its verdict on the original."""

import gzip
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from lka_quiet.encode import EncodeError, dump, fingerprint, parse, reencode, reencode_file, reencode_lines, references

EXPORTS = sorted((Path(__file__).parent / "fixtures" / "exports").glob("*.ndjson.gz"))
SEEDS = ("season-1", "round-2026-12", "x")
ID_KEYS = {"in": "n", "il": "l", "ie": "e"}


def read(path: Path) -> str:
    with gzip.open(path, "rt") as f:
        return f.read()


def kinds_and_ids(records):
    for r in records:
        for key, kind in ID_KEYS.items():
            if key in r:
                yield kind, r[key]


def declaration_names(text: str) -> list:
    records = parse(text)
    names = {}
    for r in records:
        if "in" in r:
            body = r.get("str") or r.get("num")
            names[r["in"]] = (names.get(body["pre"], ()) + (str(body.get("str", body.get("i"))),))
    out = []
    for r in records:
        for key in ("axiom", "def", "thm", "opaque", "quot"):
            if key in r:
                out.append((key, ".".join(names[r[key]["name"]])))
        if "inductive" in r:
            out.append(("inductive", tuple(".".join(names[t["name"]]) for t in r["inductive"]["types"])))
    return out


class Reencode(unittest.TestCase):
    def test_there_are_fixtures(self):
        self.assertGreaterEqual(len(EXPORTS), 10)

    def test_the_layout_is_the_exporters(self):
        for path in EXPORTS:
            with self.subTest(export=path.name):
                text = read(path)
                self.assertEqual(dump(parse(text)), text)

    def test_every_declaration_is_kept(self):
        for path in EXPORTS:
            text = read(path)
            for seed in SEEDS:
                with self.subTest(export=path.name, seed=seed):
                    self.assertEqual(fingerprint(reencode(text, seed)), fingerprint(text))

    def test_declarations_keep_their_order_and_names(self):
        for path in EXPORTS:
            text = read(path)
            with self.subTest(export=path.name):
                self.assertEqual(declaration_names(reencode(text, "season-1")), declaration_names(text))

    def test_the_bytes_change(self):
        for path in EXPORTS:
            text = read(path)
            if text.count("\n") < 30:
                continue
            with self.subTest(export=path.name):
                self.assertNotEqual(reencode(text, "season-1"), text)

    def test_every_reference_points_back(self):
        for path in EXPORTS:
            records = parse(reencode(read(path), "season-1"))
            defined = {("n", 0), ("l", 0)}
            with self.subTest(export=path.name):
                for r in records:
                    for ref in references(r):
                        self.assertIn(ref, defined, r)
                    for key, kind in ID_KEYS.items():
                        if key in r:
                            defined.add((kind, r[key]))

    def test_ids_are_dense_in_the_new_order(self):
        first = {"n": 1, "l": 1, "e": 0}
        for path in EXPORTS:
            records = parse(reencode(read(path), "round-2026-12"))
            with self.subTest(export=path.name):
                for kind in "nle":
                    ids = [i for k, i in kinds_and_ids(records) if k == kind]
                    self.assertEqual(ids, list(range(first[kind], first[kind] + len(ids))))

    def test_the_meta_record_stays_first(self):
        for path in EXPORTS:
            text = reencode(read(path), "x")
            self.assertIn('"meta"', text.splitlines()[0])

    def test_a_seed_gives_one_encoding_and_seeds_differ(self):
        text = read(next(p for p in EXPORTS if "alg-conv-trans-acc" in p.name))
        self.assertEqual(reencode(text, "a"), reencode(text, "a"))
        self.assertNotEqual(reencode(text, "a"), reencode(text, "b"))

    def test_reencoding_twice_keeps_every_declaration(self):
        text = read(EXPORTS[0])
        self.assertEqual(fingerprint(reencode(reencode(text, "a"), "b")), fingerprint(text))


class Streaming(unittest.TestCase):
    def test_output_comes_before_the_input_ends(self):
        lines = read(next(p for p in EXPORTS if "init-prefix" in p.name)).splitlines(keepends=True)
        consumed = []

        def source():
            for line in lines:
                consumed.append(line)
                yield line

        stream = reencode_lines(source(), "season-1")
        first = [next(stream) for _ in range(50)]
        self.assertEqual(len(first), 50)
        self.assertLess(len(consumed), len(lines) / 4)

    def test_a_file_streams_to_the_same_bytes(self):
        for path in EXPORTS:
            text = read(path)
            with tempfile.TemporaryDirectory() as tmp, self.subTest(export=path.name):
                source, target = Path(tmp) / "in.ndjson", Path(tmp) / "out.ndjson"
                source.write_text(text)
                reencode_file(source, target, "season-1")
                self.assertEqual(target.read_text(), reencode(text, "season-1"))


class Refusals(unittest.TestCase):
    BASE = [{"in": 1, "str": {"pre": 0, "str": "A"}}, {"ie": 0, "sort": 0},
            {"axiom": {"isUnsafe": False, "levelParams": [], "name": 1, "type": 0}}]

    def test_a_dangling_reference_is_refused(self):
        records = self.BASE[:1] + [{"axiom": {"isUnsafe": False, "levelParams": [], "name": 1, "type": 7}}]
        with self.assertRaises(EncodeError):
            reencode(dump(records), "s")

    def test_a_record_no_declaration_uses_stays_before_the_next_declaration(self):
        records = self.BASE[:2] + [{"ie": 1, "bvar": 0}] + self.BASE[2:] + [{"in": 2, "str": {"pre": 1, "str": "B"}}]
        for seed in SEEDS:
            out = parse(reencode(dump(records), seed))
            self.assertEqual(len(out), len(records))
            self.assertIn("axiom", out[3])
            self.assertEqual({tuple(sorted(r)) for r in out[:3]}, {("in", "str"), ("ie", "sort"), ("bvar", "ie")})
            self.assertEqual(out[4]["str"]["str"], "B")

    def test_an_id_defined_twice_is_refused(self):
        records = self.BASE[:2] + [{"ie": 0, "bvar": 0}] + self.BASE[2:]
        with self.assertRaises(EncodeError):
            reencode(dump(records), "s")


class Fingerprint(unittest.TestCase):
    def test_a_changed_term_changes_the_fingerprint(self):
        text = read(next(p for p in EXPORTS if "proof-param-ok" in p.name))
        records = parse(text)
        index = next(i for i, r in enumerate(records) if "bvar" in r and r["bvar"] == 0)
        changed = [dict(r) for r in records]
        changed[index] = {**records[index], "bvar": 1}
        self.assertNotEqual(fingerprint(dump(changed)), fingerprint(text))

    def test_a_renamed_binder_changes_the_fingerprint(self):
        text = read(next(p for p in EXPORTS if "proof-param-ok" in p.name))
        records = parse(text)
        index = next(i for i, r in enumerate(records) if "lam" in r)
        other = next(r["in"] for r in records if "in" in r and r["in"] != records[index]["lam"]["name"])
        changed = list(records)
        changed[index] = {**records[index], "lam": {**records[index]["lam"], "name": other}}
        self.assertNotEqual(fingerprint(dump(changed)), fingerprint(text))


KERNEL = os.environ.get("LKA_OFFICIAL_KERNEL")


@unittest.skipUnless(KERNEL and Path(KERNEL).exists(), "LKA_OFFICIAL_KERNEL names no built official checker")
class TheKernelAgrees(unittest.TestCase):
    def verdict(self, text: str) -> int:
        with tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False) as f:
            f.write(text)
        try:
            return subprocess.run([KERNEL, f.name], capture_output=True, timeout=300).returncode
        finally:
            os.unlink(f.name)

    def test_every_re_encoding_gets_the_originals_verdict(self):
        verdicts = set()
        for path in EXPORTS:
            text = read(path)
            original = self.verdict(text)
            verdicts.add(original)
            for seed in SEEDS:
                with self.subTest(export=path.name, seed=seed):
                    self.assertEqual(self.verdict(reencode(text, seed)), original)
        self.assertEqual(verdicts, {0, 1})


if __name__ == "__main__":
    unittest.main()
