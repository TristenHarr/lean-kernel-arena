"""The same proofs in new bytes: a lean4export NDJSON file re-encoded from a seed.

Every input the Arena checks is published byte for byte, so a checker could recognise a file and
answer from memory. Re-encoding takes that away without touching what is checked: the term records
(names, levels, expressions) the original has between two declarations are emitted before the
second in a seeded order, each after everything it refers to, and every id is renumbered densely
in the new order; the declarations keep their order, their names and their content. A re-encoded
file has the same structural fingerprint as the original (each declaration with every reference
resolved to what it denotes), so its verdict is the same and its checking is the same work; only
the bytes, the ids and the layout differ.

    names        {"in": i, "str": {"pre": n, "str": s}} | {"in": i, "num": {"pre": n, "i": k}}   (0: anonymous)
    levels       {"il": i, "succ": l} | {"max": [l, l]} | {"imax": [l, l]} | {"param": n}         (0: zero)
    expressions  {"ie": i, kind: ...} for bvar, sort, const, app, lam, forallE, letE, proj, natVal, strVal
    declarations axiom, def, thm, opaque, quot, inductive (types, ctors, recs and their rules)
"""

import hashlib
import json
from array import array

from .rng import SplitMix64, seed_from

KINDS = ("n", "l", "e")
_ID_KEY = {"n": "in", "l": "il", "e": "ie"}
_FIRST_ID = {"n": 1, "l": 1, "e": 0}
DECLARATIONS = ("axiom", "def", "thm", "opaque", "quot", "inductive")


class EncodeError(ValueError):
    pass


def _kind_of(record: dict) -> str:
    for kind, key in _ID_KEY.items():
        if key in record:
            return kind
    if "meta" in record:
        return "meta"
    for key in DECLARATIONS:
        if key in record:
            return "decl"
    raise EncodeError(f"unknown record: {sorted(record)}")


def _body(record: dict, kind: str):
    """The record's one payload key and value (beside its id)."""
    key = next(k for k in record if k != _ID_KEY.get(kind))
    return key, record[key]


def _decl_refs(key: str, value: dict) -> list[tuple[str, int]]:
    out = []
    names = lambda ids: [("n", i) for i in ids]
    if key == "inductive":
        for t in value["types"]:
            out += names([t["name"]] + t["all"] + t["ctors"] + t["levelParams"]) + [("e", t["type"])]
        for c in value["ctors"]:
            out += names([c["name"], c["induct"]] + c["levelParams"]) + [("e", c["type"])]
        for r in value["recs"]:
            out += names([r["name"]] + r["all"] + r["levelParams"]) + [("e", r["type"])]
            for rule in r["rules"]:
                out += [("n", rule["ctor"]), ("e", rule["rhs"])]
        return out
    out += names([value["name"]] + value.get("all", []) + value["levelParams"]) + [("e", value["type"])]
    if "value" in value:
        out.append(("e", value["value"]))
    return out


def references(record: dict) -> list[tuple[str, int]]:
    """Every (kind, id) a record refers to, in a fixed order."""
    kind = _kind_of(record)
    if kind == "meta":
        return []
    if kind == "decl":
        key, value = _body(record, kind)
        return _decl_refs(key, value)
    key, value = _body(record, kind)
    if kind == "n":
        return [("n", value["pre"])]
    if kind == "l":
        if key == "succ":
            return [("l", value)]
        if key in ("max", "imax"):
            return [("l", value[0]), ("l", value[1])]
        if key == "param":
            return [("n", value)]
        raise EncodeError(f"unknown level {key}")
    if key in ("bvar", "natVal", "strVal"):
        return []
    if key == "sort":
        return [("l", value)]
    if key == "const":
        return [("n", value["name"])] + [("l", u) for u in value["us"]]
    if key == "app":
        return [("e", value["fn"]), ("e", value["arg"])]
    if key in ("lam", "forallE"):
        return [("n", value["name"]), ("e", value["type"]), ("e", value["body"])]
    if key == "letE":
        return [("n", value["name"]), ("e", value["type"]), ("e", value["value"]), ("e", value["body"])]
    if key == "proj":
        return [("n", value["typeName"]), ("e", value["struct"])]
    raise EncodeError(f"unknown expression {key}")


def _rewrite(record: dict, new_id) -> dict:
    """The record with every reference (kind, id) replaced by new_id(kind, id)."""
    kind = _kind_of(record)
    n = lambda i: new_id("n", i)
    l = lambda i: new_id("l", i)
    e = lambda i: new_id("e", i)
    if kind == "meta":
        return record
    if kind == "decl":
        key, v = _body(record, kind)
        if key == "inductive":
            types = [{**t, "name": n(t["name"]), "all": [n(i) for i in t["all"]], "ctors": [n(i) for i in t["ctors"]],
                      "levelParams": [n(i) for i in t["levelParams"]], "type": e(t["type"])} for t in v["types"]]
            ctors = [{**c, "name": n(c["name"]), "induct": n(c["induct"]),
                      "levelParams": [n(i) for i in c["levelParams"]], "type": e(c["type"])} for c in v["ctors"]]
            recs = [{**r, "name": n(r["name"]), "all": [n(i) for i in r["all"]],
                     "levelParams": [n(i) for i in r["levelParams"]], "type": e(r["type"]),
                     "rules": [{**rule, "ctor": n(rule["ctor"]), "rhs": e(rule["rhs"])} for rule in r["rules"]]}
                    for r in v["recs"]]
            return {key: {**v, "types": types, "ctors": ctors, "recs": recs}}
        out = {**v, "name": n(v["name"]), "levelParams": [n(i) for i in v["levelParams"]], "type": e(v["type"])}
        if "all" in v:
            out["all"] = [n(i) for i in v["all"]]
        if "value" in v:
            out["value"] = e(v["value"])
        return {key: out}
    key, v = _body(record, kind)
    id_key = _ID_KEY[kind]
    own = new_id(kind, record[id_key])
    if kind == "n":
        return {id_key: own, key: {**v, "pre": n(v["pre"])}}
    if kind == "l":
        if key == "succ":
            return {id_key: own, key: l(v)}
        if key in ("max", "imax"):
            return {id_key: own, key: [l(v[0]), l(v[1])]}
        return {id_key: own, key: n(v)}
    if key in ("bvar", "natVal", "strVal"):
        return {id_key: own, key: v}
    if key == "sort":
        return {id_key: own, key: l(v)}
    if key == "const":
        return {id_key: own, key: {**v, "name": n(v["name"]), "us": [l(u) for u in v["us"]]}}
    if key == "app":
        return {id_key: own, key: {**v, "fn": e(v["fn"]), "arg": e(v["arg"])}}
    if key in ("lam", "forallE"):
        return {id_key: own, key: {**v, "name": n(v["name"]), "type": e(v["type"]), "body": e(v["body"])}}
    if key == "letE":
        return {id_key: own, key: {**v, "name": n(v["name"]), "type": e(v["type"]), "value": e(v["value"]),
                                   "body": e(v["body"])}}
    return {id_key: own, key: {**v, "typeName": n(v["typeName"]), "struct": e(v["struct"])}}


def _dump_one(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def parse(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def dump(records) -> str:
    """The exporter's own layout: one compact JSON object per line, keys sorted."""
    return "".join(_dump_one(r) for r in records)


def _table(records) -> dict:
    table = {}
    for index, record in enumerate(records):
        kind = _kind_of(record)
        if kind in KINDS:
            key = (kind, record[_ID_KEY[kind]])
            if key in table:
                raise EncodeError(f"{kind} {key[1]} is defined twice")
            table[key] = index
    return table


def _implicit(kind: str, i: int) -> bool:
    return (kind, i) in (("n", 0), ("l", 0))


def reencode_lines(lines, seed: str):
    """The re-encoded export, line by line, holding one segment (the term records between two
    declarations) and the map from old ids to new at a time, so an export of any size streams
    through. Each segment is emitted in an order drawn from the seed, depth first so that every
    record follows everything it refers to, and each record takes the next id of its kind as it is
    emitted; a declaration follows its segment, its references renumbered."""
    rng = SplitMix64(seed_from("reencode", seed))
    new_ids = {kind: array("i") for kind in KINDS}
    counters = dict(_FIRST_ID)

    def assigned(kind: str, i: int) -> bool:
        table = new_ids[kind]
        return i < len(table) and table[i] >= 0

    def map_id(kind: str, i: int) -> int:
        if _implicit(kind, i):
            return i
        if not assigned(kind, i):
            raise EncodeError(f"{kind} {i} is referenced but never defined")
        return new_ids[kind][i]

    def shuffled(items):
        items = list(dict.fromkeys(items))
        for a in range(len(items) - 1, 0, -1):
            b = rng.below(a + 1)
            items[a], items[b] = items[b], items[a]
        return items

    def flush(segment: dict):
        for root in shuffled(segment):
            stack = [(root, False)]
            while stack:
                key, expanded = stack.pop()
                kind, i = key
                if assigned(kind, i):
                    continue
                record = segment[key]
                if expanded:
                    table = new_ids[kind]
                    if len(table) <= i:
                        table.extend([-1] * (i + 1 - len(table)))
                    table[i] = counters[kind]
                    counters[kind] += 1
                    yield _dump_one(_rewrite(record, map_id))
                    continue
                stack.append((key, True))
                children = [c for c in references(record) if not _implicit(*c) and not assigned(*c)]
                for child in shuffled(children):
                    if child not in segment:
                        raise EncodeError(f"{child[0]} {child[1]} is referenced but never defined")
                    stack.append((child, False))

    segment: dict = {}
    for line in lines:
        if not line.strip():
            continue
        record = json.loads(line)
        kind = _kind_of(record)
        if kind == "meta":
            yield _dump_one(record)
        elif kind in KINDS:
            key = (kind, record[_ID_KEY[kind]])
            if key in segment or assigned(*key):
                raise EncodeError(f"{kind} {key[1]} is defined twice")
            segment[key] = record
        else:
            yield from flush(segment)
            segment = {}
            yield _dump_one(_rewrite(record, map_id))
    yield from flush(segment)


def reencode(text: str, seed: str) -> str:
    """The export re-encoded from the seed (see reencode_lines)."""
    return "".join(reencode_lines(text.splitlines(), seed))


def reencode_file(source, target, seed: str) -> None:
    """reencode_lines from one file to another, never holding either whole."""
    with open(source, encoding="utf-8") as src, open(target, "w", encoding="utf-8") as dst:
        for line in reencode_lines(src, seed):
            dst.write(line)


class Resolver:
    """What each record of an export denotes: a hash of its structure with every reference resolved
    (ids and layout play no part), and each name's dotted text."""

    def __init__(self, records):
        self.records = records
        self.table = _table(records)
        self.memo = {("n", 0): "n0", ("l", 0): "l0"}
        self.texts = {0: ()}

    def hash(self, kind: str, i: int) -> str:
        memo = self.memo
        stack = [(kind, i)]
        while stack:
            k, j = stack[-1]
            if (k, j) in memo:
                stack.pop()
                continue
            if (k, j) not in self.table:
                raise EncodeError(f"{k} {j} is referenced but never defined")
            record = self.records[self.table[(k, j)]]
            pending = [c for c in references(record) if c not in memo and c != (k, j)]
            if pending:
                stack.extend(pending)
                continue
            body = _rewrite(record, lambda kk, ii, own=(k, j): "self" if (kk, ii) == own else memo[(kk, ii)])
            body.pop(_ID_KEY[k], None)
            memo[(k, j)] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]
            stack.pop()
        return memo[(kind, i)]

    def declaration(self, record: dict) -> str:
        for ref in references(record):
            self.hash(*ref)
        body = _rewrite(record, lambda kk, ii: self.memo[(kk, ii)])
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def name(self, i: int) -> str:
        chain = []
        while i not in self.texts:
            record = self.records[self.table[("n", i)]]
            body = record.get("str") or record.get("num")
            chain.append((i, body["pre"], str(body["str"]) if "str" in record else str(body["i"])))
            i = body["pre"]
        for j, pre, part in reversed(chain):
            self.texts[j] = self.texts[pre] + (part,)
        return ".".join(self.texts[chain[0][0]] if chain else self.texts[i])


def fingerprint(text: str) -> list[str]:
    """One hash per declaration, in order, of the declaration with every reference replaced by the
    hash of what it denotes: equal lists mean the same declarations, whatever the ids and layout."""
    records = parse(text)
    resolver = Resolver(records)
    return [resolver.declaration(r) for r in records if _kind_of(r) == "decl"]
