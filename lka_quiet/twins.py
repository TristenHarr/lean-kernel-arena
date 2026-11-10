"""Invalid twins: fresh invalid proofs that look like inputs a checker has seen.

A twin is a valid export with one theorem's or definition's value replaced by the value of an
earlier declaration of the same kind, with the same universe parameters and a different type, then
re-encoded from the seed. Every constant the donor's value refers to is declared before the donor,
so the twin is well formed; what it lacks is a value of the declared type, which only a type check
can find. A checker that answers a known input from memory, or skims, accepts it and is unsound;
one that checks rejects it at the swapped declaration. Only a twin the official kernel rejects is
kept (`verify`), and only exports it accepts are twinned.
"""

import itertools
import json
from dataclasses import dataclass
from pathlib import Path

from .encode import Resolver, _dump_one, _kind_of, dump, parse, reencode, reencode_lines
from .rng import SplitMix64, seed_from

SWAPPABLE = ("thm", "def")


@dataclass(frozen=True)
class Declaration:
    index: int
    record_index: int
    kind: str
    name: str
    level_names: tuple
    type_hash: str
    value_hash: str | None
    value_id: int | None


@dataclass(frozen=True)
class Twin:
    text: str | None
    target: str
    donor: str
    target_index: int
    donor_index: int
    tried: int
    path: str | None = None


def declarations(records) -> list[Declaration]:
    """Every declaration in order, its first type's for an inductive, with its type and value as
    the structures they denote."""
    resolver = Resolver(records)
    out = []
    for record_index, record in enumerate(records):
        if _kind_of(record) != "decl":
            continue
        kind = next(iter(record))
        body = record[kind]
        if kind == "inductive":
            body = body["types"][0]
        value = body.get("value") if kind != "inductive" else None
        out.append(Declaration(
            index=len(out), record_index=record_index, kind=kind, name=resolver.name(body["name"]),
            level_names=tuple(resolver.name(n) for n in body["levelParams"]),
            type_hash=resolver.hash("e", body["type"]),
            value_hash=resolver.hash("e", value) if value is not None else None, value_id=value))
    return out


def _pairs(decls) -> dict[int, list[int]]:
    """For each swappable declaration, the earlier ones whose value could take its value's place."""
    out = {}
    for target in decls:
        if target.kind not in SWAPPABLE or target.value_id is None:
            continue
        donors = [d.index for d in decls[:target.index]
                  if d.kind == target.kind and d.value_id is not None and d.level_names == target.level_names
                  and d.type_hash != target.type_hash and d.value_hash != target.value_hash]
        if donors:
            out[target.index] = donors
    return out


def candidates(text: str) -> list[tuple[int, int]]:
    """Every (target, donor) pair of declaration indices a twin could swap."""
    return [(t, d) for t, donors in _pairs(declarations(parse(text))).items() for d in donors]


def _shuffled(rng: SplitMix64, items: list) -> list:
    items = list(items)
    for a in range(len(items) - 1, 0, -1):
        b = rng.below(a + 1)
        items[a], items[b] = items[b], items[a]
    return items


def _order(decls, limit: float, seed: str):
    """(target, donor) pairs with the target among the first `limit` declarations: targets in an
    order drawn from the seed, each target's donors in turn."""
    pairs = {t: donors for t, donors in _pairs(decls).items() if t < limit}
    rng = SplitMix64(seed_from("twin", seed))
    for target in _shuffled(rng, sorted(pairs)):
        for donor in _shuffled(rng, pairs[target]):
            yield target, donor


def _spliced(records, decls, target: int, donor: int) -> list[dict]:
    out = list(records)
    record = records[decls[target].record_index]
    kind = next(iter(record))
    out[decls[target].record_index] = {kind: {**record[kind], "value": decls[donor].value_id}}
    return out


MAX_TRIES = 20


def make_twin(text: str, seed: str, *, within: float = 1.0, verify=None, max_tries: int = MAX_TRIES) -> Twin | None:
    """The first twin, in an order drawn from the seed, whose target lies in the first `within` of
    the declarations and which `verify` (the official kernel rejecting it) accepts; None when none
    of the first `max_tries` candidates passes."""
    records = parse(text)
    decls = declarations(records)
    for tried, (target, donor) in enumerate(itertools.islice(_order(decls, within * len(decls), seed), max_tries), start=1):
        out = reencode(dump(_spliced(records, decls, target, donor)), seed)
        if verify is None or verify(out):
            return Twin(text=out, target=decls[target].name, donor=decls[donor].name,
                        target_index=target, donor_index=donor, tried=tried)
    return None


def twin_file(source, target, seed: str, *, prefix_declarations: int, verify=None,
              max_tries: int = MAX_TRIES) -> Twin | None:
    """make_twin for an export of any size: only its first `prefix_declarations` declarations are
    read into memory and the target is one of them; the twin is written to `target`, the rest of
    the source streamed through the re-encoder after the spliced prefix, and `verify` is given the
    path. With a prefix covering the whole export, the twin is make_twin's, byte for byte."""
    source, target = Path(source), Path(target)
    prefix, offset, seen = [], 0, 0
    with open(source, "rb") as f:
        for raw in f:
            offset += len(raw)
            line = raw.decode("utf-8")
            if not line.strip():
                continue
            record = json.loads(line)
            prefix.append(record)
            if _kind_of(record) == "decl":
                seen += 1
                if seen >= prefix_declarations:
                    break
    decls = declarations(prefix)
    complete = seen < prefix_declarations

    def rest():
        with open(source, "rb") as f:
            f.seek(offset)
            for raw in f:
                yield raw.decode("utf-8")

    order = _order(decls, len(decls) if complete else prefix_declarations, seed)
    for tried, (t, d) in enumerate(itertools.islice(order, max_tries), start=1):
        lines = (_dump_one(r) for r in _spliced(prefix, decls, t, d))
        if not complete:
            lines = itertools.chain(lines, rest())
        with open(target, "w", encoding="utf-8") as out:
            for line in reencode_lines(lines, seed):
                out.write(line)
        if verify is None or verify(target):
            return Twin(text=None, target=decls[t].name, donor=decls[d].name, target_index=t, donor_index=d,
                        tried=tried, path=str(target))
    target.unlink(missing_ok=True)
    return None
