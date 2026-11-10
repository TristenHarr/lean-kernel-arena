"""The board: per-test scores, categories, ranks with their uncertainty, and the gates before them.

A checker's score in a category is a weighted mean of log ratios against the reference checker,

    S(c) = sum_m alpha_m sum_f beta_f sum_{t in f} w_{f,t} ln( max(x_m(c,t), floor_m) / max(x_m(ref,t), floor_m) ),

so exp(-S) is how many times better than the reference it is, as a weighted geometric mean.
Because the reference enters every checker's score through the same additive constant, the
order of the board does not depend on which checker is the reference (Fleming & Wallace,
"How not to lie with statistics", CACM 1986).

A test a checker fails (timeout, error, decline, OOM) is scored as the failure factor times the
cap (PAR-2, as in the SAT Competition): the timeout for time, the memory limit for memory, the
power ceiling times the timeout for energy, and the timeout at the Arena's own instruction rate
for instructions.

A category under the arena rule ranks as the official Arena's board does (lka.py
board_order_key), lexicographically: fewest invalid proofs not rejected, then fewest valid proofs
not accepted, then the one measurement it names (the Arena's: instructions on Mathlib; a checker
that did not produce it ranks below every checker that did), then fewest tests declined, an error
or a timeout counting as a decline. Exact ties keep the order in which the Arena reads its
checkers, checkers/<name>.yaml in path order, as do exact ties in every other category.

Uncertainty comes from a stratified bootstrap that redraws every cell's runs and every family's
tests, the same draws for every checker in a replicate. Each checker's rank comes with its
marginal confidence set (Mogstad, Romano, Shaikh & Wilhelm, 2024): one checker is placed below
another only when the other beats it in nearly every replicate, at a level corrected for the
number of comparisons; otherwise they share a rank.
"""

import bisect
import fnmatch
import math
from dataclasses import dataclass, field

from .plan import Cell, consensus_status
from .rng import SplitMix64, seed_from
from .stats import quantile_nearest_rank

BOARD_SCHEMA = "lka-quiet-board/2"

QUANTITY_FIELDS = {"wall_s": "wall_s", "memory_bytes": "memory_peak_bytes", "energy_j": "energy_j",
                   "instructions": "instructions"}

RULES = ("weighted", "arena")

_TIE_EPSILON = 1e-12


class BoardError(ValueError):
    pass


def _round(x, digits=10):
    return None if x is None else round(x, digits)


# ---------------------------------------------------------------------------------------------
# Cells


@dataclass
class CellSummary:
    cell: Cell
    status: str | None
    valid: list[dict]
    invalid: int
    timeout_s: float | None
    cap_s: float | None
    flaky: bool
    accepted_any: bool
    accepted: list[dict] = field(default_factory=list)

    def values(self, quantity: str) -> list[float] | None:
        key = QUANTITY_FIELDS[quantity]
        values = [r.get(key) for r in self.accepted]
        if not values or any(v is None for v in values):
            return None
        return sorted(values)

    def median(self, quantity: str) -> float | None:
        """The order statistic x_((n+1)/2): the median, the lower median for even n."""
        values = self.values(quantity)
        if values is None:
            return None
        return values[(len(values) - 1) // 2]


def summarize_cells(records) -> dict[Cell, CellSummary]:
    grouped: dict[Cell, list[dict]] = {}
    for record in records:
        grouped.setdefault(Cell(record["checker"], record["test"], record["lane"]), []).append(record)
    out = {}
    for cell, cell_records in grouped.items():
        valid = [r for r in cell_records if r.get("valid")]
        timeouts = [r.get("timeout_s") for r in valid if r.get("timeout_s")]
        caps = [r.get("cap_s") for r in valid if r.get("cap_s")]
        out[cell] = CellSummary(
            cell=cell,
            status=consensus_status(valid),
            valid=valid,
            invalid=len(cell_records) - len(valid),
            timeout_s=max(timeouts) if timeouts else None,
            cap_s=max(caps) if caps else (max(timeouts) if timeouts else None),
            flaky=len({r["status"] for r in valid}) > 1,
            accepted_any=any(r["status"] == "accepted" for r in valid),
            accepted=[r for r in valid if r["status"] == "accepted"],
        )
    return out


# ---------------------------------------------------------------------------------------------
# Families and categories


def _matches(name: str, pattern) -> tuple[bool, float | None]:
    if isinstance(pattern, dict):
        return fnmatch.fnmatchcase(name, pattern["name"]), pattern.get("weight")
    return fnmatch.fnmatchcase(name, pattern), None


def family_split(tests, families: dict) -> dict[str, tuple[list[tuple[str, float]], list[str], float]]:
    """Each family's scored tests (outcome accept): the pinned ones with their weights, the free
    ones, and the weight the free ones share equally."""
    out = {}
    for family_id, family in families.items():
        pinned: dict[str, float] = {}
        free: list[str] = []
        for test in tests:
            if test.get("outcome") != "accept":
                continue
            name = test["name"]
            for pattern in family["tests"]:
                hit, weight = _matches(name, pattern)
                if hit:
                    if weight is None:
                        free.append(name)
                    else:
                        pinned[name] = weight
                    break
        pinned_total = math.fsum(pinned.values())
        if pinned_total > 1 + 1e-12:
            raise BoardError(f"family {family_id}: pinned weights sum to {pinned_total} > 1")
        if free:
            out[family_id] = (list(pinned.items()), free, 1 - pinned_total)
        elif pinned:
            out[family_id] = ([(name, w / pinned_total) for name, w in pinned.items()], [], 0.0)
        else:
            out[family_id] = ([], [], 0.0)
    return out


def family_members(tests, families: dict) -> dict[str, list[tuple[str, float]]]:
    """Each family's scored tests and their weights, summing to one."""
    out = {}
    for family_id, (pinned, free, free_weight) in family_split(tests, families).items():
        members = list(pinned)
        if free:
            members += [(name, free_weight / len(free)) for name in free]
        out[family_id] = members
    return out


def _normalized(weights: dict, allowed) -> dict:
    kept = {k: v for k, v in weights.items() if k in allowed and v > 0}
    total = math.fsum(kept.values())
    return {k: v / total for k, v in kept.items()} if total > 0 else {}


# ---------------------------------------------------------------------------------------------
# Per-test values


@dataclass
class CellValue:
    value: float | None
    failed: bool
    log: float | None
    cdf: list[float] | None
    support: list[float] | None
    values: list[float] | None = None

    def drawn(self, pick: int | None) -> tuple[float, float]:
        """The (floored log, raw value) of the run a bootstrap replicate drew, or of the point estimate."""
        if pick is None:
            return self.log, self.value
        return self.support[pick], self.values[pick]


def _bootstrap_median_cdf(n: int) -> list[float]:
    """P(X_(k) <= v_j), j = 1..n, for the k = (n+1)//2-th order statistic of n draws with replacement."""
    k = (n + 1) // 2
    cdf = []
    for j in range(1, n + 1):
        p = j / n
        cdf.append(math.fsum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1)))
    cdf[-1] = 1.0
    return cdf


def _penalty(metric: dict, summary: CellSummary, scoring: dict) -> float:
    """Twice the test's cap: the test's own limit, never a shorter one the checker set for itself."""
    factor = scoring.get("failure_factor", 2)
    if not summary.cap_s:
        raise BoardError(f"{summary.cell.checker} on {summary.cell.test} ({summary.cell.lane}) failed with no cap to score it at")
    timeout = summary.cap_s
    quantity = metric["quantity"]
    if quantity == "wall_s":
        return factor * timeout
    if quantity == "memory_bytes":
        return factor * scoring["caps"]["memory_bytes"]
    if quantity == "energy_j":
        return factor * scoring["caps"]["power_w"] * timeout
    if quantity == "instructions":
        return factor * scoring["caps"]["instruction_rate"] * timeout
    raise BoardError(f"unknown quantity {quantity}")


def _test_value(summary: CellSummary | None, metric: dict, scoring: dict) -> CellValue | None:
    if summary is None or summary.status is None:
        return None
    floor = scoring["floors"][metric["quantity"]]
    if summary.status != "accepted":
        value = _penalty(metric, summary, scoring)
        return CellValue(value, True, math.log(max(value, floor)), None, None)
    values = summary.values(metric["quantity"])
    if values is None:
        return None
    support = [math.log(max(v, floor)) for v in values]
    point = values[(len(values) - 1) // 2]
    return CellValue(point, False, math.log(max(point, floor)), _bootstrap_median_cdf(len(values)), support, values)


# ---------------------------------------------------------------------------------------------
# Scoring


def _category_raw(logs: dict, category: dict, members: dict) -> float | None:
    """sum_m alpha_m sum_f beta_f sum_t w_t L[m][t]; None if any needed value is missing."""
    total = []
    for metric_id, alpha in category["metrics"].items():
        for family_id, beta in category["families"].items():
            for test_name, weight in members[family_id]:
                value = logs.get((metric_id, test_name))
                if value is None:
                    return None
                total.append(alpha * beta * weight * value)
    return math.fsum(total)


def rank_sets(probabilities: dict, contenders, confidence: float) -> dict:
    """Each contender's marginal confidence set for its rank (Mogstad, Romano, Shaikh & Wilhelm,
    Review of Economic Studies 2024): [1 + #{d surely better}, N - #{d surely worse}], where d is
    surely better than c when d beats c in at least 1 - alpha / (2 (N - 1)) of the replicates, a
    Bonferroni bound over the N - 1 comparisons involving c. Since no d is both surely better and
    surely worse, the set is never empty, and the rank shown is its left end."""
    contenders = list(contenders)
    n = len(contenders)
    threshold = 1 - (1 - confidence) / (2 * max(n - 1, 1)) - 1e-12
    sets = {}
    for c in contenders:
        better = sum(1 for d in contenders if d != c and probabilities[d][c] >= threshold)
        worse = sum(1 for d in contenders if d != c and probabilities[c][d] >= threshold)
        sets[c] = (1 + better, n - worse)
    return sets


def _compare(a: tuple, b: tuple) -> int:
    """-1, 0 or 1 as key a is better than, level with, or worse than key b, lexicographically,
    with differences within the tie tolerance counted as level."""
    for x, y in zip(a, b):
        if x < y - _TIE_EPSILON:
            return -1
        if y < x - _TIE_EPSILON:
            return 1
    return 0


def _file_order(name: str) -> tuple:
    """Where the Arena reads a checker: checkers/<name>.yaml, in path order (lka.py load_yaml_files)."""
    return tuple(f"{name}.yaml".split("/"))


def _competition_ranks(scores: dict) -> dict:
    return {
        c: 1 + sum(1 for d, sd in scores.items() if d != c and sd < sc - _TIE_EPSILON)
        for c, sc in scores.items()
    }


def _gates(name, tests, summaries, lanes):
    unsound, wrong_rejects = [], []
    for test in tests:
        outcome = test.get("outcome")
        lane_summaries = [summaries.get(Cell(name, test["name"], lane)) for lane in lanes]
        if outcome == "reject" and any(s and s.accepted_any for s in lane_summaries):
            unsound.append(test["name"])
        if outcome == "accept" and any(s and s.status == "rejected" for s in lane_summaries):
            wrong_rejects.append(test["name"])
    return {"unsound": unsound, "wrong_rejects": wrong_rejects}


def _correctness(name, tests, summaries, lane):
    counts = {"good_total": 0, "good_accepted": 0, "bad_total": 0, "bad_rejected": 0,
              "either_total": 0, "either_answered": 0, "declined_or_failed": 0}
    for test in tests:
        summary = summaries.get(Cell(name, test["name"], lane))
        status = summary.status if summary else None
        outcome = test.get("outcome")
        if outcome == "accept":
            counts["good_total"] += 1
            counts["good_accepted"] += status == "accepted"
        elif outcome == "reject":
            counts["bad_total"] += 1
            counts["bad_rejected"] += status == "rejected"
        else:
            counts["either_total"] += 1
            counts["either_answered"] += status in ("accepted", "rejected")
        if status in ("declined", "error", "timeout"):
            counts["declined_or_failed"] += 1
    return counts


def _arena_counts(name, tests, summaries, lane) -> dict | None:
    """The Arena's counts (lka.py compute_checker_stats) from each test's verdict: invalid proofs not
    rejected, valid proofs not accepted, and tests declined, an error or a timeout being a decline
    and a corner case counting only when declined. None while any test is unmeasured."""
    counts = {"invalid_accepted": 0, "valid_rejected": 0, "declined": 0}
    for test in tests:
        summary = summaries.get(Cell(name, test["name"], lane))
        status = summary.status if summary else None
        if status is None:
            return None
        outcome = test.get("outcome")
        if status in ("declined", "error", "timeout"):
            counts["declined"] += 1
        elif outcome == "reject" and status != "rejected":
            counts["invalid_accepted"] += 1
        elif outcome == "accept" and status != "accepted":
            counts["valid_rejected"] += 1
    return counts


def _arena_measure(tv: CellValue | None, pick: int | None = None) -> float | None:
    """The measurement the arena rule ranks on, if the checker produced it: the test accepted and
    counted (the Arena treats a missing or zero count as no count)."""
    if tv is None or tv.failed:
        return None
    _, raw = tv.drawn(pick)
    return raw if raw and raw > 0 else None


def _arena_key(counts: dict, measure: float | None) -> tuple:
    return (counts["invalid_accepted"], counts["valid_rejected"],
            measure if measure is not None else math.inf, counts["declined"])


def _arena_term(category: dict, members: dict) -> tuple[str, str]:
    terms = sorted({(metric_id, test_name) for metric_id in category["metrics"]
                    for family_id in category["families"] for test_name, _ in members[family_id]})
    if len(terms) != 1:
        raise BoardError(f"category {category['id']}: the arena rule ranks on one measurement of one test, "
                         f"not {len(terms)}")
    return terms[0]


def score_board(tests, checkers, records, scoring: dict) -> dict:
    records = list(records)
    epochs = sorted({r.get("epoch") for r in records})
    if len(epochs) > 1:
        raise BoardError(f"records from several machine epochs cannot share a board: {epochs}")

    names = [c["name"] for c in checkers]
    reference = scoring["reference"]
    if reference not in names:
        raise BoardError(f"the reference checker {reference} has no results")
    summaries = summarize_cells(r for r in records if r["checker"] in names)
    metrics = scoring["metrics"]
    lanes = sorted({m["lane"] for m in metrics.values()})

    available = {
        metric_id for metric_id, metric in metrics.items()
        if any(r.get(QUANTITY_FIELDS[metric["quantity"]]) is not None
               for r in records if r.get("lane") == metric["lane"] and r.get("valid") and r.get("status") == "accepted")
    }
    members = family_members(tests, scoring["families"])
    categories = []
    for category in scoring["categories"]:
        rule = category.get("rule", "weighted")
        if rule not in RULES:
            raise BoardError(f"category {category['id']}: unknown rule {rule!r}")
        if rule == "arena" and len(category["metrics"]) != 1:
            raise BoardError(f"category {category['id']}: the arena rule ranks on one metric")
        nonempty = {f for f, m in members.items() if m}
        categories.append({
            "id": category["id"],
            "title": category["title"],
            "rule": rule,
            "metrics": _normalized(category["metrics"], available),
            "families": _normalized(category["families"], nonempty),
        })
    categories = [c for c in categories if c["metrics"] and c["families"]]
    arena_terms = {c["id"]: _arena_term(c, members) for c in categories if c["rule"] == "arena"}
    scored_tests = sorted({t for ms in members.values() for t, _ in ms})

    values: dict[str, dict[tuple[str, str], CellValue]] = {}
    for name in names:
        per = {}
        for metric_id in (m for m in metrics if m in available):
            metric = metrics[metric_id]
            for test_name in scored_tests:
                tv = _test_value(summaries.get(Cell(name, test_name, metric["lane"])), metric, scoring)
                if tv is not None:
                    per[(metric_id, test_name)] = tv
        values[name] = per

    gates = {name: _gates(name, tests, summaries, lanes) for name in names}
    unranked = set(scoring.get("unranked", []))
    ranked = [n for n in names if n not in unranked and not gates[n]["unsound"] and not gates[n]["wrong_rejects"]]

    point_logs = {name: {k: tv.log for k, tv in values[name].items()} for name in names}
    raw = {cat["id"]: {name: _category_raw(point_logs[name], cat, members) for name in names} for cat in categories}
    counts = {cat["id"]: {name: _arena_counts(name, tests, summaries, metrics[arena_terms[cat["id"]][0]]["lane"])
                          for name in names}
              for cat in categories if cat["rule"] == "arena"}

    bootstrap = scoring["bootstrap"]
    confidence = bootstrap.get("confidence", 0.95)
    replicates = _bootstrap(names, values, categories, family_split(tests, scoring["families"]), bootstrap,
                            arena_terms, counts)

    entries = {name: {"name": name, "reference": name == reference, "ranked": name in ranked,
                      "gates": gates[name], "categories": {}} for name in names}
    head_to_head = {}
    order = {}
    upper_q = 1 - (1 - confidence) / 2
    for cat in categories:
        cat_id = cat["id"]
        arena = cat["rule"] == "arena"
        if arena:
            term = arena_terms[cat_id]
            measures = {n: _arena_measure(values[n].get(term)) for n in names}
            keys = {n: _arena_key(counts[cat_id][n], measures[n]) if counts[cat_id][n] else None for n in names}
            logs = {n: math.log(m) if m is not None else None for n, m in measures.items()}
            contenders = [n for n in names if keys[n] is not None]
        else:
            keys = {n: (raw[cat_id][n],) if raw[cat_id][n] is not None else None for n in names}
            logs = raw[cat_id]
            contenders = [n for n in ranked if keys[n] is not None]
        draws = replicates[cat_id]
        wins = {a: {b: 0.0 for b in contenders if b != a} for a in contenders}
        factor_samples = {n: [] for n in names}
        for replicate in draws:
            ref_log = replicate.get(reference, (None, None))[1]
            if ref_log is not None:
                for n, (_, log) in replicate.items():
                    if log is not None:
                        factor_samples[n].append(math.exp(-(log - ref_log)))
            for a in contenders:
                for b in contenders:
                    if a == b:
                        continue
                    verdict = _compare(replicate[a][0], replicate[b][0])
                    if verdict < 0:
                        wins[a][b] += 1
                    elif verdict == 0:
                        wins[a][b] += 0.5
        count = max(len(draws), 1)
        exact = {a: {b: w / count for b, w in row.items()} for a, row in wins.items()}
        head_to_head[cat_id] = {a: {b: _round(p, 4) for b, p in row.items()} for a, row in exact.items()}
        sets = rank_sets(exact, contenders, confidence)
        rank_counts: dict[int, int] = {}
        for lo, _ in sets.values():
            rank_counts[lo] = rank_counts.get(lo, 0) + 1
        ref_log = logs[reference]
        for name in names:
            relative = None if logs[name] is None or ref_log is None else logs[name] - ref_log
            samples = factor_samples[name] if relative is not None else []
            factor = None
            if relative is not None:
                factor = measures[reference] / measures[name] if arena else math.exp(-relative)
            result = {
                "score": _round(relative),
                "factor": _round(factor, 6),
                "factor_lo": _round(quantile_nearest_rank(samples, (1 - confidence) / 2), 6) if samples else None,
                "factor_hi": _round(quantile_nearest_rank(samples, upper_q), 6) if samples else None,
                "rank": None, "rank_lo": None, "rank_hi": None, "tied": False,
            }
            if name in sets:
                lo, hi = sets[name]
                result.update({"rank": lo, "rank_lo": lo, "rank_hi": hi, "tied": rank_counts[lo] > 1})
            if arena:
                result["arena"] = {**counts[cat_id][name], "measure": measures[name]} if counts[cat_id][name] else None
            entries[name]["categories"][cat_id] = result
        order[cat_id] = _display_order(names, keys, {n: entries[n]["categories"][cat_id]["rank"] for n in names})

    _pareto(entries, ranked, scoring.get("pareto", []))
    _sensitivity(entries, ranked, names, point_logs, categories, members, scoring.get("sensitivity"))
    _scaling(entries, names, summaries, scoring.get("scaling"), categories, members)

    lane_for_counts = scoring.get("scaling", {}).get("parallel", lanes[0])
    for name in names:
        entry = entries[name]
        entry["correctness"] = _correctness(name, tests, summaries, lane_for_counts)
        entry["flaky"] = sorted(f"{cell.test}/{cell.lane}" for cell, s in summaries.items()
                                if cell.checker == name and s.flaky)
        entry["invalid_runs"] = sum(s.invalid for cell, s in summaries.items() if cell.checker == name)
        tests_out: dict = {}
        for (metric_id, test_name), tv in sorted(values[name].items()):
            ref = values[reference].get((metric_id, test_name))
            tests_out.setdefault(test_name, {})[metric_id] = {
                "value": _round(tv.value),
                "failed": tv.failed,
                "ratio": _round(math.exp(tv.log - ref.log), 8) if ref is not None else None,
            }
        entry["tests"] = tests_out

    needed = len(names) * len(tests) * len(lanes)
    measured = sum(1 for cell, s in summaries.items() if s.valid and cell.lane in lanes)
    return {
        "schema": BOARD_SCHEMA,
        "season": scoring.get("season"),
        "epoch": epochs[0] if epochs else None,
        "reference": reference,
        "metrics": {m: {**metrics[m], "available": m in available} for m in metrics},
        "families": {f: {"title": scoring["families"][f].get("title", f),
                         "members": [[t, _round(w, 12)] for t, w in members[f]]} for f in members},
        "categories": [{**cat, "order": order[cat["id"]]} for cat in categories],
        "checkers": [entries[n] for n in names],
        "head_to_head": head_to_head,
        "progress": {"measured": measured, "needed": needed},
        "bootstrap": {"replicates": bootstrap["replicates"], "seed": bootstrap["seed"], "confidence": confidence},
    }


def _display_order(names, keys, ranks):
    """Ranked checkers by their estimate (the rank column says which gaps are significant), then
    the unranked with a score, then those still being measured; exact ties in the Arena's order."""
    def key(name):
        group = 0 if ranks[name] is not None else 1 if keys[name] is not None else 2
        return (group, keys[name] or (), _file_order(name))
    return sorted(names, key=key)


# ---------------------------------------------------------------------------------------------
# Bootstrap


def _bootstrap(names, values, categories, splits, config, arena_terms, counts) -> dict[str, list[dict]]:
    """Each category's replicates: for every checker it can score, its key (what ranks it) and the
    log its factor is drawn from (None when it has no factor). Every category of a replicate sees
    the same run drawn from each cell and the same tests drawn from each family."""
    rng = SplitMix64(seed_from(str(config["seed"]), "bootstrap"))
    replicates = config["replicates"]
    out = {cat["id"]: [] for cat in categories}
    family_ids = sorted({f for cat in categories for f in cat["families"]})
    plans = {family_id: splits[family_id] for family_id in family_ids}

    for _ in range(replicates):
        picks = {}
        for name in names:
            per = {}
            for key, tv in values[name].items():
                if tv.cdf is None:
                    per[key] = None
                else:
                    u = (rng.next_u64() >> 11) / 9007199254740992.0
                    per[key] = min(bisect.bisect_left(tv.cdf, u), len(tv.support) - 1)
            picks[name] = per
        drawn = {}
        for family_id, (pinned, free, free_weight) in plans.items():
            sample = list(pinned)
            if free:
                share = free_weight / len(free)
                sample += [(free[rng.below(len(free))], share) for _ in free]
            drawn[family_id] = sample
        for cat in categories:
            scores = {}
            for name in names:
                if cat["rule"] == "arena":
                    if counts[cat["id"]][name] is None:
                        continue
                    term = arena_terms[cat["id"]]
                    measure = _arena_measure(values[name].get(term), picks[name].get(term))
                    scores[name] = (_arena_key(counts[cat["id"]][name], measure),
                                    math.log(measure) if measure is not None else None)
                    continue
                total = []
                complete = True
                for metric_id, alpha in cat["metrics"].items():
                    for family_id, beta in cat["families"].items():
                        for test_name, weight in drawn[family_id]:
                            key = (metric_id, test_name)
                            tv = values[name].get(key)
                            if tv is None:
                                complete = False
                                break
                            total.append(alpha * beta * weight * tv.drawn(picks[name][key])[0])
                        if not complete:
                            break
                    if not complete:
                        break
                if complete:
                    score = math.fsum(total)
                    scores[name] = ((score,), score)
            out[cat["id"]].append(scores)
    return out


# ---------------------------------------------------------------------------------------------
# Pareto front, weight sensitivity, parallel scaling


def _pareto(entries, ranked, category_ids):
    vectors = {}
    for name in ranked:
        scores = [entries[name]["categories"].get(c, {}).get("score") for c in category_ids]
        if category_ids and all(s is not None for s in scores):
            vectors[name] = scores
    for name in entries:
        entries[name]["pareto"] = False
    for name, vector in vectors.items():
        dominated = any(
            all(o <= v for o, v in zip(other, vector)) and any(o < v for o, v in zip(other, vector))
            for other_name, other in vectors.items() if other_name != name
        )
        entries[name]["pareto"] = not dominated


def _sensitivity(entries, ranked, names, point_logs, categories, members, config):
    for name in names:
        entries[name]["sensitivity"] = None
    if not config:
        return
    base = next((c for c in categories if c["id"] == config["category"]), None)
    if base is None:
        return
    variants = [base]
    for axis in ("metrics", "families"):
        for key in base[axis]:
            for factor in config["factors"]:
                weights = dict(base[axis])
                weights[key] *= factor
                total = math.fsum(weights.values())
                variants.append({**base, axis: {k: v / total for k, v in weights.items()}})
    best = {n: math.inf for n in ranked}
    worst = {n: 0 for n in ranked}
    at = {}
    for index, variant in enumerate(variants):
        scores = {n: _category_raw(point_logs[n], variant, members) for n in ranked}
        scores = {n: s for n, s in scores.items() if s is not None}
        for n, r in _competition_ranks(scores).items():
            best[n] = min(best[n], r)
            worst[n] = max(worst[n], r)
            if index == 0:
                at[n] = r
    for n in ranked:
        if best[n] != math.inf:
            entries[n]["sensitivity"] = {"at": at[n], "best": best[n], "worst": worst[n]}


def _scaling(entries, names, summaries, config, categories, members):
    for name in names:
        entries[name]["scaling"] = None
    if not config:
        return
    category = next((c for c in categories if c["id"] == config.get("category")), None)
    if category is None:
        return
    weights = {}
    for family_id, beta in category["families"].items():
        for test_name, w in members[family_id]:
            weights[test_name] = weights.get(test_name, 0) + beta * w
    for name in names:
        logs, total = [], []
        for test_name, weight in weights.items():
            serial = summaries.get(Cell(name, test_name, config["serial"]))
            parallel = summaries.get(Cell(name, test_name, config["parallel"]))
            if not serial or not parallel or serial.status != "accepted" or parallel.status != "accepted":
                continue
            s, p = serial.median("wall_s"), parallel.median("wall_s")
            if not s or not p:
                continue
            logs.append(weight * math.log(s / p))
            total.append(weight)
        if total:
            speedup = math.exp(math.fsum(logs) / math.fsum(total))
            entries[name]["scaling"] = {
                "speedup": _round(speedup, 6),
                "efficiency": _round(speedup / config["parallel_cpus"], 6),
                "tests": len(total),
            }
