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


def parse(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def dump(records) -> str:
    """The exporter's own layout: one compact JSON object per line, keys sorted."""
    return "".join(json.dumps(r, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n" for r in records)


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


def reencode(text: str, seed: str) -> str:
    """The export in a seeded order with dense new ids. The term records the original has before
    each declaration (and after the last) are emitted in an order drawn from the seed, depth first
    so that each follows everything it refers to; records no declaration uses stay among them."""
    records = parse(text)
    table = _table(records)
    rng = SplitMix64(seed_from("reencode", seed))
    emitted: set = set()
    order: list[dict] = []

    def emit(kind: str, i: int):
        stack = [(kind, i, False)]
        while stack:
            k, j, expanded = stack.pop()
            if _implicit(k, j) or (k, j) in emitted:
                continue
            if (k, j) not in table:
                raise EncodeError(f"{k} {j} is referenced but never defined")
            record = records[table[(k, j)]]
            if expanded:
                emitted.add((k, j))
                order.append(record)
                continue
            stack.append((k, j, True))
            children = [c for c in references(record) if not _implicit(*c) and c not in emitted]
            for c in rng_shuffled(children):
                stack.append((c[0], c[1], False))

    def rng_shuffled(items):
        items = list(dict.fromkeys(items))
        for a in range(len(items) - 1, 0, -1):
            b = rng.below(a + 1)
            items[a], items[b] = items[b], items[a]
        return items

    segment: list[tuple[str, int]] = []
    for record in records:
        kind = _kind_of(record)
        if kind == "meta":
            order.append(record)
        elif kind in KINDS:
            segment.append((kind, record[_ID_KEY[kind]]))
        else:
            for ref in references(record):
                if not _implicit(*ref) and ref not in table:
                    raise EncodeError(f"{ref[0]} {ref[1]} is referenced but never defined")
            for key in rng_shuffled(segment):
                emit(*key)
            segment = []
            order.append(record)
    for key in rng_shuffled(segment):
        emit(*key)

    renumber = {}
    counters = dict(_FIRST_ID)
    for record in order:
        kind = _kind_of(record)
        if kind in KINDS:
            renumber[(kind, record[_ID_KEY[kind]])] = counters[kind]
            counters[kind] += 1

    def new_id(kind, i):
        return i if _implicit(kind, i) else renumber[(kind, i)]

    return dump(_rewrite(r, new_id) for r in order)


def fingerprint(text: str) -> list[str]:
    """One hash per declaration, in order, of the declaration with every reference replaced by the
    hash of what it denotes: equal lists mean the same declarations, whatever the ids and layout."""
    records = parse(text)
    table = _table(records)
    memo = {("n", 0): "n0", ("l", 0): "l0"}

    def resolve(kind, i):
        key = (kind, i)
        if key in memo:
            return memo[key]
        stack = [key]
        while stack:
            k, j = stack[-1]
            if (k, j) in memo:
                stack.pop()
                continue
            record = records[table[(k, j)]]
            pending = [c for c in references(record) if c not in memo and c != (k, j)]
            if pending:
                stack.extend(pending)
                continue
            body = _rewrite(record, lambda kk, ii, own=(k, j): "self" if (kk, ii) == own else memo[(kk, ii)])
            body.pop(_ID_KEY[k], None)
            memo[(k, j)] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:32]
            stack.pop()
        return memo[key]

    out = []
    for record in records:
        if _kind_of(record) == "decl":
            for ref in references(record):
                resolve(*ref)
            body = _rewrite(record, lambda kk, ii: memo[(kk, ii)])
            out.append(hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest())
    return out
