"""The board: per-test scores, categories, ranks with their uncertainty, and the gates before them.

A checker's score in a category is a weighted mean of log ratios against the reference checker,

    S(c) = sum_m alpha_m sum_f beta_f sum_{t in f} w_{f,t} ln( max(x_m(c,t), floor_m) / max(x_m(ref,t), floor_m) ),

so exp(-S) is how many times better than the reference it is, as a weighted geometric mean.
Because the reference enters every checker's score through the same additive constant, the
order of the board does not depend on which checker is the reference (Fleming & Wallace,
"How not to lie with statistics", CACM 1986).

A test a checker fails (timeout, error, decline, OOM) is scored as the failure factor times the
cap (PAR-2, as in the SAT Competition): the timeout for time, the memory limit for memory, and
the power ceiling times the timeout for energy.

Uncertainty comes from a stratified bootstrap that redraws every cell's runs and every family's
tests, the same draws for every checker in a replicate. A checker is placed below another only
when the other beats it in at least 97.5% of replicates; otherwise they share a rank.
"""

import bisect
import fnmatch
import math
from dataclasses import dataclass, field

from .plan import Cell, consensus_status
from .rng import SplitMix64, seed_from
from .stats import quantile_nearest_rank

BOARD_SCHEMA = "lka-quiet-board/1"

QUANTITY_FIELDS = {"wall_s": "wall_s", "memory_bytes": "memory_peak_bytes", "energy_j": "energy_j"}

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
    return CellValue(point, False, math.log(max(point, floor)), _bootstrap_median_cdf(len(values)), support)


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
        nonempty = {f for f, m in members.items() if m}
        categories.append({
            "id": category["id"],
            "title": category["title"],
            "metrics": _normalized(category["metrics"], available),
            "families": _normalized(category["families"], nonempty),
        })
    categories = [c for c in categories if c["metrics"] and c["families"]]
    scored_tests = sorted({t for ms in members.values() for t, _ in ms})

    values: dict[str, dict[tuple[str, str], CellValue]] = {}
    for name in names:
        per = {}
        for metric_id in available:
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

    bootstrap = scoring["bootstrap"]
    confidence = bootstrap.get("confidence", 0.95)
    replicates = _bootstrap(names, values, categories, family_split(tests, scoring["families"]), bootstrap)

    entries = {name: {"name": name, "reference": name == reference, "ranked": name in ranked,
                      "gates": gates[name], "categories": {}} for name in names}
    head_to_head = {}
    upper_q = 1 - (1 - confidence) / 2
    for cat in categories:
        cat_id = cat["id"]
        ref_raw = raw[cat_id][reference]
        contenders = [n for n in ranked if raw[cat_id][n] is not None]
        draws = replicates[cat_id]
        wins = {a: {b: 0.0 for b in contenders if b != a} for a in contenders}
        rank_samples = {n: [] for n in contenders}
        factor_samples = {n: [] for n in names}
        for replicate in draws:
            if reference in replicate:
                for n, score in replicate.items():
                    factor_samples[n].append(math.exp(-(score - replicate[reference])))
            scores = {n: replicate[n] for n in contenders}
            for n, r in _competition_ranks(scores).items():
                rank_samples[n].append(r)
            for a in contenders:
                for b in contenders:
                    if a == b:
                        continue
                    if scores[a] < scores[b] - _TIE_EPSILON:
                        wins[a][b] += 1
                    elif abs(scores[a] - scores[b]) <= _TIE_EPSILON:
                        wins[a][b] += 0.5
        count = max(len(draws), 1)
        probabilities = {a: {b: _round(w / count, 4) for b, w in row.items()} for a, row in wins.items()}
        head_to_head[cat_id] = probabilities
        display_rank = {
            c: 1 + sum(1 for d in contenders if d != c and probabilities[d][c] >= upper_q)
            for c in contenders
        }
        rank_counts: dict[int, int] = {}
        for r in display_rank.values():
            rank_counts[r] = rank_counts.get(r, 0) + 1
        for name in names:
            score = raw[cat_id][name]
            relative = None if score is None or ref_raw is None else score - ref_raw
            samples = factor_samples[name] if relative is not None else []
            result = {
                "score": _round(relative),
                "factor": _round(math.exp(-relative), 6) if relative is not None else None,
                "factor_lo": _round(quantile_nearest_rank(samples, (1 - confidence) / 2), 6) if samples else None,
                "factor_hi": _round(quantile_nearest_rank(samples, upper_q), 6) if samples else None,
                "rank": None, "rank_lo": None, "rank_hi": None, "tied": False,
            }
            if name in display_rank:
                samples = rank_samples[name]
                result.update({
                    "rank": display_rank[name],
                    "rank_lo": quantile_nearest_rank(samples, (1 - confidence) / 2) if samples else display_rank[name],
                    "rank_hi": quantile_nearest_rank(samples, upper_q) if samples else display_rank[name],
                    "tied": rank_counts[display_rank[name]] > 1,
                })
            entries[name]["categories"][cat_id] = result

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
    order = {cat["id"]: _display_order(entries, names, cat["id"]) for cat in categories}
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


def _display_order(entries, names, cat_id):
    """Ranked checkers by their estimate (the rank column says which gaps are significant), then
    the unranked with a score, then those still being measured."""
    def key(name):
        result = entries[name]["categories"][cat_id]
        group = 0 if result["rank"] is not None else 1 if result["score"] is not None else 2
        return (group, result["score"] if result["score"] is not None else math.inf, name)
    return sorted(names, key=key)


# ---------------------------------------------------------------------------------------------
# Bootstrap


def _bootstrap(names, values, categories, splits, config) -> dict[str, list[dict]]:
    rng = SplitMix64(seed_from(str(config["seed"]), "bootstrap"))
    replicates = config["replicates"]
    out = {cat["id"]: [] for cat in categories}
    family_ids = sorted({f for cat in categories for f in cat["families"]})
    plans = {family_id: splits[family_id] for family_id in family_ids}

    contenders = list(names)
    for _ in range(replicates):
        logs = {}
        for name in contenders:
            per = {}
            for key, tv in values[name].items():
                if tv.cdf is None:
                    per[key] = tv.log
                else:
                    u = (rng.next_u64() >> 11) / 9007199254740992.0
                    per[key] = tv.support[min(bisect.bisect_left(tv.cdf, u), len(tv.support) - 1)]
            logs[name] = per
        drawn = {}
        for family_id, (pinned, free, free_weight) in plans.items():
            sample = list(pinned)
            if free:
                share = free_weight / len(free)
                sample += [(free[rng.below(len(free))], share) for _ in free]
            drawn[family_id] = sample
        for cat in categories:
            scores = {}
            for name in contenders:
                total = []
                complete = True
                for metric_id, alpha in cat["metrics"].items():
                    for family_id, beta in cat["families"].items():
                        for test_name, weight in drawn[family_id]:
                            value = logs[name].get((metric_id, test_name))
                            if value is None:
                                complete = False
                                break
                            total.append(alpha * beta * weight * value)
                        if not complete:
                            break
                    if not complete:
                        break
                if complete:
                    scores[name] = math.fsum(total)
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
    for variant in variants:
        scores = {n: _category_raw(point_logs[n], variant, members) for n in ranked}
        scores = {n: s for n, s in scores.items() if s is not None}
        for n, r in _competition_ranks(scores).items():
            best[n] = min(best[n], r)
            worst[n] = max(worst[n], r)
    for n in ranked:
        if best[n] != math.inf:
            entries[n]["sensitivity"] = {"best": best[n], "worst": worst[n]}


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
