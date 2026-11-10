"""The quiet board's pages: built from board.json beside the arena's own site, under quiet/.

    quiet/                     the board: leaders, the overall ranking, correctness, trade-offs
    quiet/category/<id>/       one category: its places, their ranges and head-to-head matrix
    quiet/pareto/              the Pareto fronts: every pair of Pareto categories, the layers, Mathlib and
                               each library in seconds and bytes
    quiet/libraries/           every library: each checker's time on four cores and on one, memory, instructions
    quiet/tests/               every test and every checker's verdict on it, as the Arena tabulates them
    quiet/test/<name>/         one test: each checker's measurements, with the spread of its runs
    quiet/checker/<name>/      one checker: every category, every test, every metric
    quiet/methodology/         how it is measured and scored, with the season's rules
    quiet/board.json           the data every page is drawn from
"""

import itertools
import json
import math
from pathlib import Path

from markupsafe import Markup

from . import charts
from .board import BOARD_SCHEMA

QUIET_DIR = "quiet"


def fmt_seconds(s) -> str:
    if s is None:
        return "–"
    if s < 1:
        return f"{s * 1000:.0f} ms" if s >= 0.0095 else f"{s * 1000:.1f} ms"
    if s < 60:
        return f"{s:.2f} s" if s < 10 else f"{s:.1f} s"
    minutes, seconds = divmod(s, 60)
    if minutes < 60:
        return f"{int(minutes)} min {seconds:02.0f} s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)} h {int(minutes):02d} min"


def fmt_bytes(b) -> str:
    if b is None:
        return "–"
    for unit, size in (("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if b >= size:
            value = b / size
            return f"{value:.2f} {unit}" if value < 10 else f"{value:.1f} {unit}"
    return f"{b} B"


def fmt_joules(j) -> str:
    if j is None:
        return "–"
    return f"{j / 1000:.2f} kJ" if j >= 1000 else f"{j:.1f} J"


def fmt_instructions(n) -> str:
    """An instruction count as the Arena writes one (lka.py format_instructions)."""
    if n is None:
        return "–"
    for prefix, size in (("T", 10**12), ("G", 10**9), ("M", 10**6), ("k", 10**3)):
        if n >= size:
            return f"{n / size:.1f}\u202f{prefix}"
    return str(int(n))


def fmt_time_tick(s) -> str:
    """A round duration as an axis reads it: 500 ms, 30 s, 5 min, 2 h."""
    if s < 1:
        return f"{s * 1000:g} ms"
    if s < 60:
        return f"{s:g} s"
    if s < 3600:
        return f"{s / 60:g} min"
    return f"{s / 3600:g} h"


def fmt_bytes_tick(b) -> str:
    """A power of two as an axis reads it: 512 MiB, 8 GiB."""
    for unit, size in (("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if b >= size:
            return f"{b / size:g} {unit}"
    return f"{b:g} B"


_AXES = {"wall_s": (charts.time_ticks, fmt_time_tick), "memory_bytes": (charts.byte_ticks, fmt_bytes_tick),
          "instructions": (charts.log_ticks, fmt_instructions), "energy_j": (charts.log_ticks, fmt_joules)}


def fmt_quantity(value, quantity: str) -> str:
    return {"wall_s": fmt_seconds, "memory_bytes": fmt_bytes, "energy_j": fmt_joules,
            "instructions": fmt_instructions}[quantity](value)


def rank_label(result: dict) -> str:
    """A checker's place in a category, "=" when it shares it."""
    if result.get("place") is None:
        return "–"
    return f"{result['place']}=" if result.get("place_tied") else str(result["place"])


def interval_text(result: dict) -> str:
    if result.get("factor") is None:
        return "–"
    return f"{charts.factor_text(result['factor'])} [{charts._plain(result['factor_lo'])}, {charts._plain(result['factor_hi'])}]"


def _entries(board) -> dict:
    return {c["name"]: c for c in board["checkers"]}


def forest_rows(board, category_id: str, href_prefix: str) -> list[dict]:
    entries = _entries(board)
    order = next(c["order"] for c in board["categories"] if c["id"] == category_id)
    rows = []
    for name in order:
        result = entries[name]["categories"][category_id]
        if result["rank"] is None or result["factor"] is None:
            continue
        rows.append({"name": name, "rank": rank_label(result), "factor": result["factor"],
                     "lo": result["factor_lo"], "hi": result["factor_hi"], "tied": result["tied"],
                     "href": f"{href_prefix}checker/{name}/"})
    return rows


def arena_rows(board, category_id: str, href_prefix: str) -> list[dict]:
    """A category under the arena rule as the Arena's board lists it: every checker, in order, with
    the counts that place it and the measurement that breaks their ties."""
    entries = _entries(board)
    category = next(c for c in board["categories"] if c["id"] == category_id)
    quantity = board["metrics"][next(iter(category["metrics"]))]["quantity"]
    rows = []
    for name in category["order"]:
        result = entries[name]["categories"][category_id]
        counts = result.get("arena") or {}
        rows.append({
            "name": name, "href": f"{href_prefix}checker/{name}/", "reference": entries[name]["reference"],
            "pending": result["rank"] is None, "place": rank_label(result),
            "rank_lo": result["rank_lo"], "rank_hi": result["rank_hi"],
            "invalid_accepted": counts.get("invalid_accepted"), "valid_rejected": counts.get("valid_rejected"),
            "declined": counts.get("declined"), "measure": fmt_quantity(counts.get("measure"), quantity),
            "factor": result["factor"], "lo": result["factor_lo"], "hi": result["factor_hi"],
        })
    return rows


def leaders(board, category_id: str) -> dict:
    rows = forest_rows(board, category_id, "")
    if not rows:
        return {"names": [], "label": "", "factor": None, "lo": None, "hi": None, "ranked": 0}
    first = [r for r in rows if r["rank"] in ("1", "1=")]
    names = [r["name"] for r in first]
    if len(names) == 1:
        label = names[0]
    elif len(names) <= 3:
        label = " = ".join(names)
    else:
        label = f"{names[0]}, {names[1]} and {len(names) - 2} more, tied"
    placed = sum(1 for e in board["checkers"] if e["categories"][category_id]["rank"] is not None)
    return {"names": names, "label": label, "factor": first[0]["factor"], "lo": first[0]["lo"],
            "hi": first[0]["hi"], "ranked": placed}


def unranked(board, category_id: str, baselines=()) -> list[dict]:
    """Checkers in the category without a rank, and why."""
    out = []
    for entry in board["checkers"]:
        result = entry["categories"].get(category_id, {})
        if result.get("rank") is not None:
            continue
        if entry["reference"]:
            why = "the reference"
        elif entry["name"] in baselines:
            why = "a baseline, not a checker"
        elif entry["gates"]["unsound"]:
            why = f"accepted {len(entry['gates']['unsound'])} invalid proof(s)"
        elif entry["gates"]["wrong_rejects"]:
            why = f"rejected {len(entry['gates']['wrong_rejects'])} valid proof(s)"
        elif not entry["ranked"]:
            why = "shown for comparison"
        else:
            why = "still being measured"
        out.append({"name": entry["name"], "why": why, "factor": result.get("factor"),
                    "interval": interval_text(result) if result.get("factor") is not None else "–"})
    return out


def _pareto_categories(board, season) -> list[str]:
    rendered = {c["id"] for c in board["categories"]}
    return [c for c in season.get("pareto", []) if c in rendered]


def _titles(board) -> dict:
    return {c["id"]: c["title"] for c in board["categories"]}


def pair_views(board, season) -> list[dict]:
    """Every pair of the season's Pareto categories: each ranked checker's two factors and the
    front of that pair alone."""
    titles = _titles(board)
    views = []
    for a, b in itertools.combinations(_pareto_categories(board, season), 2):
        points = []
        for entry in board["checkers"]:
            fa, fb = entry["categories"][a].get("factor"), entry["categories"][b].get("factor")
            if entry["ranked"] and fa and fb:
                points.append({"name": entry["name"], "x": fa, "y": fb})
        views.append({"x": a, "y": b, "x_title": titles[a], "y_title": titles[b], "points": pareto_2d(points)})
    return views


def _dominates(u, v) -> bool:
    return all(a <= b for a, b in zip(u, v)) and any(a < b for a, b in zip(u, v))


def layer_rows(board, season) -> list[dict]:
    """The ranked checkers by Pareto layer, then by overall place; each with the checkers that beat
    it on every Pareto category at once."""
    cats = _pareto_categories(board, season)
    entries = [e for e in board["checkers"] if e.get("pareto_layer")]
    vectors = {e["name"]: [e["categories"][c]["score"] for c in cats] for e in entries}

    def key(e):
        place = e["categories"].get("overall", {}).get("place")
        return (e["pareto_layer"], place if place is not None else math.inf, e["name"])

    ordered = sorted(entries, key=key)
    rows = []
    for e in ordered:
        beaten_by = [d["name"] for d in ordered if _dominates(vectors[d["name"]], vectors[e["name"]])]
        closest = [d for d in beaten_by if not any(_dominates(vectors[d], vectors[o]) for o in beaten_by)]
        rows.append({"name": e["name"], "layer": e["pareto_layer"],
                     "factors": {c: e["categories"][c]["factor"] for c in cats},
                     "dominated_by": beaten_by, "closest": closest})
    return rows


def parallel_rows(board, season) -> list[dict]:
    cats = _pareto_categories(board, season)
    rows = []
    for entry in board["checkers"]:
        factors = {c: entry["categories"][c].get("factor") for c in cats}
        if entry["ranked"] and cats and all(factors.values()):
            rows.append({"name": entry["name"], "values": factors, "front": entry["pareto"]})
    return rows


def _front_min(points):
    """Mark each point not dominated in both coordinates (smaller is better)."""
    for p in points:
        p["pareto"] = not any(
            q is not p and q["x"] <= p["x"] and q["y"] <= p["y"] and (q["x"] < p["x"] or q["y"] < p["y"])
            for q in points)
    return points


def absolute_points(board, test: str, x_metric: str, y_metric: str) -> list[dict]:
    """One test's two measurements, in their own units, for every ranked checker and the
    reference; the front is taken with less as better on both."""
    points = []
    for entry in board["checkers"]:
        if not (entry["ranked"] or entry["reference"]):
            continue
        cells = entry.get("tests", {}).get(test, {})
        cx, cy = cells.get(x_metric), cells.get(y_metric)
        if cx and cy and not cx["failed"] and not cy["failed"] and cx["value"] and cy["value"]:
            points.append({"name": entry["name"], "x": cx["value"], "y": cy["value"]})
    return _front_min(points)


def _checker_order(board) -> list[str]:
    overall = next((c for c in board["categories"] if c["id"] == "overall"), None)
    return list(overall["order"]) if overall else [e["name"] for e in board["checkers"]]


_FAILURES = {"declined": "declined", "timeout": "out of time", "error": "error", "rejected": "rejected"}


def _row_note(entry) -> str | None:
    if entry["reference"]:
        return "reference"
    if entry["gates"]["unsound"]:
        return "unsound"
    if entry["gates"]["wrong_rejects"]:
        return "incomplete"
    if not entry["ranked"]:
        return "not ranked"
    return None


def library_matrix(board, metric: str) -> dict:
    """Each checker's measurement on each library, the libraries in the season's order, the
    checkers in the overall order. A ranked checker's cell is shaded by its place among the ranked
    checkers on that library; the others are shown unshaded, with the reason they are not ranked,
    and a failure says what it was."""
    family = board["families"].get("libraries", {"members": []})
    libraries = [name for name, _ in family["members"]]
    quantity = board["metrics"][metric]["quantity"]
    entries = _entries(board)
    columns = {}
    for library in libraries:
        cells = (entries[n].get("tests", {}).get(library, {}).get(metric) for n in entries if entries[n]["ranked"])
        columns[library] = sorted(c["value"] for c in cells if c and not c["failed"] and c["value"])
    rows = []
    for name in _checker_order(board):
        entry = entries[name]
        cells = []
        for library in libraries:
            cell = entry.get("tests", {}).get(library, {}).get(metric)
            if cell is None:
                cells.append({"text": "–", "failed": False, "factor": None, "q": None, "test": library})
            elif cell["failed"]:
                status = entry.get("verdicts", {}).get(library)
                cells.append({"text": _FAILURES.get(status, "failed"), "failed": True, "factor": None, "q": None,
                              "test": library})
            else:
                q = None
                ranked = columns[library]
                if entry["ranked"] and cell["value"] in ranked:
                    place = ranked.index(cell["value"])
                    goodness = 1.0 if len(ranked) < 2 else (len(ranked) - 1 - place) / (len(ranked) - 1)
                    q = charts.seq_step(goodness)
                cells.append({"text": fmt_quantity(cell["value"], quantity), "failed": False,
                              "factor": (1 / cell["ratio"]) if cell.get("ratio") else None, "q": q, "test": library})
        if any(c["text"] != "–" for c in cells):
            rows.append({"name": name, "cells": cells, "reference": entry["reference"], "note": _row_note(entry)})
    return {"libraries": libraries, "metric": metric, "title": board["metrics"][metric]["title"], "rows": rows}


_GLYPHS = {"correct": "✓", "wrong": "✗", "declined": "–", "timeout": "⏱", "error": "!", None: ""}


def _verdict_kind(outcome, status) -> str | None:
    if status is None:
        return None
    if status in ("declined", "error", "timeout"):
        return "declined"
    if outcome == "accept":
        return "correct" if status == "accepted" else "wrong"
    if outcome == "reject":
        return "correct" if status == "rejected" else "wrong"
    return "correct"


def test_groups(board) -> list[dict]:
    """Every test and every checker's verdict on it, grouped as the Arena groups them (by the part
    of the name before its first slash; whole libraries and other single tests first). The
    reference is the first column, the others follow the overall order."""
    entries = _entries(board)
    reference = board["reference"]
    columns = [reference] + [n for n in _checker_order(board) if n != reference]
    groups: dict[str, list] = {}
    for test in board.get("tests", []):
        name = test["name"]
        group = name.split("/", 1)[0] if "/" in name else ""
        cells = []
        for checker in columns:
            status = entries[checker].get("verdicts", {}).get(name)
            kind = _verdict_kind(test.get("outcome"), status)
            timing = entries[checker].get("tests", {}).get(name, {}).get("T4")
            if status == "accepted" and timing and not timing["failed"]:
                text = fmt_seconds(timing["value"])
            elif status in ("timeout", "error"):
                text = _GLYPHS[status]
            else:
                text = _GLYPHS[kind]
            cells.append({"checker": checker, "status": status, "kind": kind, "text": text})
        groups.setdefault(group, []).append({"name": name, "outcome": test.get("outcome"), "cells": cells})
    out = []
    for group in sorted(groups, key=lambda g: (g != "", g)):
        tests = groups[group]
        summary = []
        for i, checker in enumerate(columns):
            kinds = [t["cells"][i]["kind"] for t in tests]
            summary.append({"checker": checker, "correct": kinds.count("correct"), "wrong": kinds.count("wrong"),
                            "declined": kinds.count("declined")})
        out.append({"name": group, "tests": tests, "summary": summary})
    return out


def test_rows(board, test: str, metric: str, href_prefix: str) -> list[dict]:
    """One test's measurement by every checker that ran it, for its bar chart."""
    rows = []
    for entry in board["checkers"]:
        cell = entry.get("tests", {}).get(test, {}).get(metric)
        if cell is None:
            continue
        kind = "reference" if entry["reference"] else "ranked" if entry["ranked"] else "unranked"
        rows.append({"name": entry["name"], "value": None if cell["failed"] else cell["value"],
                     "lo": cell.get("lo"), "hi": cell.get("hi"), "failed": cell["failed"], "kind": kind,
                     "href": f"{href_prefix}checker/{entry['name']}/"})
    return rows


def pareto_2d(points):
    """Mark each point not dominated in both coordinates (bigger is better)."""
    for p in points:
        p["pareto"] = not any(
            q is not p and q["x"] >= p["x"] and q["y"] >= p["y"] and (q["x"] > p["x"] or q["y"] > p["y"])
            for q in points)
    return points


def rank_grid(board) -> dict:
    entries = _entries(board)
    categories = board["categories"]
    overall = next(c for c in categories if c["id"] == "overall")
    names = [n for n in overall["order"] if entries[n]["ranked"]]
    placed = {c["id"]: sum(1 for e in entries.values() if e["categories"][c["id"]]["rank"] is not None)
              for c in categories}
    rows = []
    for name in names:
        cells = []
        for category in categories:
            result = entries[name]["categories"][category["id"]]
            ranked = placed[category["id"]]
            if result["rank"] is None or ranked < 2:
                cells.append({"text": "–", "q": None})
                continue
            goodness = (ranked - result["place"]) / (ranked - 1)
            cells.append({"text": rank_label(result), "q": charts.seq_step(goodness),
                          "title": f"{name}: rank {rank_label(result)} of {ranked} in {category['title']}, "
                                   f"interval {result['rank_lo']}–{result['rank_hi']}"})
        rows.append({"name": name, "cells": cells})
    return {"categories": categories, "rows": rows}


def checker_tests(board, entry, season) -> list[dict]:
    families = {}
    for family_id, members in board["families"].items():
        for test_name, _ in members["members"]:
            families.setdefault(test_name, family_id)
    tests_by_name = {t["name"]: t for t in board.get("tests", [])}
    rows = []
    for test_name, metrics in sorted(entry.get("tests", {}).items()):
        rows.append({"name": test_name, "family": families.get(test_name, ""),
                     "outcome": tests_by_name.get(test_name, {}).get("outcome"), "metrics": metrics})
    return rows


def strip_groups(board, entry, metric_id: str):
    groups = []
    for family_id in ("libraries", "stress", "interactive"):
        family = board["families"].get(family_id)
        if not family:
            continue
        tests = []
        for test_name, _ in family["members"]:
            cell = entry.get("tests", {}).get(test_name, {}).get(metric_id)
            if cell is None:
                continue
            ratio = cell.get("ratio")
            factor = (1 / ratio) if ratio else None
            tests.append((test_name, factor, cell["failed"]))
        groups.append((family["title"], tests))
    return groups


def _svg(text: str) -> Markup:
    return Markup(text)


class SiteError(ValueError):
    pass


def render(board: dict, season: dict, machine: dict, env, outdir: Path, build_info: dict,
           home_at_root: bool = False) -> None:
    """With `home_at_root`, the board's home is also the site's index (the arena's own index is then
    arena.html, which lka.py build-site --quiet-home writes)."""
    if board.get("schema") != BOARD_SCHEMA:
        raise SiteError(f"board.json is {board.get('schema')!r}; this site renders {BOARD_SCHEMA}: score it again with quiet-score")
    env.filters.update({"factor": charts.factor_text, "seconds": fmt_seconds, "bytes": fmt_bytes,
                        "joules": fmt_joules, "instructions": fmt_instructions})
    env.globals.update({"rank_label": rank_label, "interval_text": interval_text, "fmt_quantity": fmt_quantity,
                        "arena_index": "arena.html" if home_at_root else "./"})
    root = Path(outdir) / QUIET_DIR
    root.mkdir(parents=True, exist_ok=True)
    (root / "board.json").write_text(json.dumps(board, indent=1, sort_keys=True) + "\n")

    entries = _entries(board)
    categories = board["categories"]
    descriptions = {c["id"]: c.get("description", "") for c in season["categories"]}
    baselines = [n for n in season.get("unranked", []) if n != board["reference"]]
    common = {
        "board": board, "season": season, "machine": machine, "build_info": build_info,
        "categories": categories, "descriptions": descriptions, "preview": board.get("preview"),
        "metrics": {m: board["metrics"][m] for m in season["metrics"] if m in board["metrics"]},
        "progress_pct": 100.0 * board["progress"]["measured"] / max(board["progress"]["needed"], 1),
    }

    def home(prefix: str, root_path: str) -> dict:
        ranked_overall = forest_rows(board, "overall", prefix)
        multi = next((c for c in categories if c["id"] == "multi-core"), None)
        memory = next((c for c in categories if c["id"] == "memory"), None)
        points = []
        if multi and memory:
            for entry in board["checkers"]:
                m, r = entry["categories"].get("multi-core", {}), entry["categories"].get("memory", {})
                if entry["ranked"] and m.get("factor") and r.get("factor"):
                    points.append({"name": entry["name"], "x": m["factor"], "y": r["factor"]})
        pareto_svg = charts.pareto(pareto_2d(points), title="Speed and memory",
                                   x_label="faster with 4 cores (× official)", y_label="less memory (× official)",
                                   subtitle="Each ranked checker; those no other checker beats on both axes are labelled.")
        overall_names = [r["name"] for r in ranked_overall]
        h2h = board["head_to_head"].get("overall", {})
        scaling = sorted(((e["name"], e["scaling"]["speedup"]) for e in board["checkers"]
                          if e["scaling"] and e["ranked"]), key=lambda item: -item[1])
        sensitivity = [(r["name"], entries[r["name"]]["sensitivity"]["at"],
                        entries[r["name"]]["sensitivity"]["best"], entries[r["name"]]["sensitivity"]["worst"])
                       for r in ranked_overall if entries[r["name"]].get("sensitivity")]
        return {
            **common, "root_path": root_path, "quiet_root": prefix, "nav": "board",
            "tiles": [{"category": c, "leaders": leaders(board, c["id"])} for c in categories],
            "overall_svg": _svg(charts.forest(ranked_overall, title="Overall",
                                              subtitle="Weighted geometric mean over every metric and family, 95% bootstrap intervals")),
            "overall_rows": ranked_overall,
            "overall_unranked": unranked(board, "overall", baselines),
            "grid": rank_grid(board),
            "pareto_svg": _svg(pareto_svg), "pareto_points": points,
            "h2h_svg": _svg(charts.heatmap(overall_names, h2h, title="Head to head, overall",
                                           subtitle="How often the row checker scores better than the column checker over the bootstrap replicates")),
            "h2h_names": overall_names, "h2h": h2h,
            "scaling_svg": _svg(charts.scaled_bars(scaling, title="Speed-up from one core to four", ideal=4.0, unit="×",
                                                   subtitle="Weighted geometric mean of 1-core time over 4-core time")),
            "scaling": scaling,
            "sensitivity_svg": _svg(charts.range_bars(sensitivity, title="Rank under other weightings",
                                                      subtitle="Each metric and family weight halved and raised by half")),
            "sensitivity": sensitivity,
        }

    env.get_template("quiet/index.html").stream(home("", "../")).dump(str(root / "index.html"))
    if home_at_root:
        env.get_template("quiet/index.html").stream(home(f"{QUIET_DIR}/", "")).dump(str(Path(outdir) / "index.html"))

    for category in categories:
        directory = root / "category" / category["id"]
        directory.mkdir(parents=True, exist_ok=True)
        rows = forest_rows(board, category["id"], "../../")
        names = [r["name"] for r in rows]
        arena = category["rule"] == "arena"
        subtitle = ("The measurement the rule ranks on, against the reference; places by the rule" if arena
                    else descriptions.get(category["id"], ""))
        env.get_template("quiet/category.html").stream({
            **common, "root_path": "../../../", "quiet_root": "../../", "nav": "categories", "category": category,
            "description": descriptions.get(category["id"], ""),
            "forest_svg": _svg(charts.forest(rows, title=category["title"], subtitle=subtitle)),
            "rows": rows, "results": {n: entries[n]["categories"][category["id"]] for n in entries},
            "arena_rows": arena_rows(board, category["id"], "../../") if arena else None,
            "unranked": unranked(board, category["id"], baselines),
            "h2h_svg": _svg(charts.heatmap(names, board["head_to_head"].get(category["id"], {}),
                                           title=f"Head to head, {category['title'].lower()}")),
        }).dump(str(directory / "index.html"))

    for entry in board["checkers"]:
        directory = root / "checker" / entry["name"]
        directory.mkdir(parents=True, exist_ok=True)
        strips = []
        for metric_id in season["metrics"]:
            metric = board["metrics"].get(metric_id, {})
            if not metric.get("available"):
                continue
            strips.append({"metric": metric, "id": metric_id, "svg": _svg(charts.strip(
                strip_groups(board, entry, metric_id), title=f"{entry['name']}: {metric['title']}",
                subtitle="Each test's factor against the reference; failures in the left gutter"))})
        env.get_template("quiet/checker.html").stream({
            **common, "root_path": "../../../", "quiet_root": "../../", "nav": "checkers", "entry": entry,
            "meta": board.get("checker_meta", {}).get(entry["name"], {}),
            "strips": strips, "tests": checker_tests(board, entry, season),
        }).dump(str(directory / "index.html"))

    def page(*parts) -> tuple[Path, str]:
        directory = root.joinpath(*parts)
        directory.mkdir(parents=True, exist_ok=True)
        depth = sum(part.count("/") + 1 for part in parts)
        return directory, "../" * depth

    directory, prefix = page("pareto")
    views = pair_views(board, season)
    layers = layer_rows(board, season)
    pareto_ids = _pareto_categories(board, season)
    titles = _titles(board)
    libraries = board["families"].get("libraries", {"members": []})["members"]
    has = lambda metric: board["metrics"].get(metric, {}).get("available")
    absolute = []
    for test_name, _ in libraries:
        for xm, ym in (("T4", "M"), ("T1", "M")):
            if has(xm) and has(ym):
                points = absolute_points(board, test_name, xm, ym)
                ref = next((p for p in points if p["name"] == board["reference"]), None)
                absolute.append({"test": test_name, "x": xm, "y": ym, "points": points, "svg": _svg(charts.pareto(
                    points, title=f"{test_name}: {board['metrics'][xm]['title'].lower()} and peak memory",
                    x_label=board["metrics"][xm]["title"].lower(), y_label="peak memory",
                    better=("low", "low"), x_ticks=charts.time_ticks, y_ticks=charts.byte_ticks,
                    x_format=fmt_time_tick, y_format=fmt_bytes_tick, x_value=fmt_seconds, y_value=fmt_bytes,
                    labels="all" if test_name == "mathlib" else "front",
                    reference=(ref["x"], ref["y"]) if ref else None,
                    width=780 if test_name == "mathlib" else 380, height=500 if test_name == "mathlib" else 300))})
    env.get_template("quiet/pareto.html").stream({
        **common, "root_path": prefix + "../", "quiet_root": prefix, "nav": "pareto",
        "parallel_svg": _svg(charts.parallel(parallel_rows(board, season), [(c, titles[c]) for c in pareto_ids],
                                             title="Every Pareto category at once",
                                             subtitle="Each line is a ranked checker, its factor on every axis; the front in colour")),
        "pair_svgs": [{"view": v, "svg": _svg(charts.pareto(
            v["points"], title=f"{v['x_title']} and {v['y_title']}", x_label=f"{v['x_title'].lower()} (× {board['reference']})",
            y_label=f"{v['y_title'].lower()} (× {board['reference']})", width=380, height=320))} for v in views],
        "layers": layers, "pareto_ids": pareto_ids, "titles": titles,
        "mathlib_charts": [a for a in absolute if a["test"] == "mathlib"],
        "library_charts": [a for a in absolute if a["test"] != "mathlib" and a["x"] == "T4"],
    }).dump(str(directory / "index.html"))

    directory, prefix = page("libraries")
    env.get_template("quiet/libraries.html").stream({
        **common, "root_path": prefix + "../", "quiet_root": prefix, "nav": "libraries",
        "matrices": [library_matrix(board, m) for m in season["metrics"] if has(m)],
    }).dump(str(directory / "index.html"))

    directory, prefix = page("tests")
    groups = test_groups(board)
    env.get_template("quiet/tests.html").stream({
        **common, "root_path": prefix + "../", "quiet_root": prefix, "nav": "tests",
        "groups": groups, "columns": [c["checker"] for c in groups[0]["tests"][0]["cells"]] if groups else [],
    }).dump(str(directory / "index.html"))

    for test in board.get("tests", []):
        directory, prefix = page("test", test["name"])
        bars = []
        for metric_id in season["metrics"]:
            metric = board["metrics"].get(metric_id, {})
            rows = test_rows(board, test["name"], metric_id, prefix)
            if not metric.get("available") or not rows:
                continue
            ref = next((r["value"] for r in rows if r["name"] == board["reference"]), None)
            ticks, tick_format = _AXES.get(metric["quantity"], (charts.log_ticks, None))
            bars.append({"metric": metric, "svg": _svg(charts.value_bars(
                rows, title=f"{test['name']}: {metric['title'].lower()}", reference=ref,
                fmt=lambda v, q=metric["quantity"]: fmt_quantity(v, q), ticks=ticks, tick_format=tick_format))})
        verdicts = [{"name": e["name"], "status": e.get("verdicts", {}).get(test["name"]),
                     "kind": _verdict_kind(test.get("outcome"), e.get("verdicts", {}).get(test["name"]))}
                    for e in board["checkers"]]
        env.get_template("quiet/test.html").stream({
            **common, "root_path": prefix + "../", "quiet_root": prefix, "nav": "tests", "test": test,
            "bars": bars, "verdicts": verdicts,
        }).dump(str(directory / "index.html"))

    directory = root / "methodology"
    directory.mkdir(parents=True, exist_ok=True)
    env.get_template("quiet/methodology.html").stream({
        **common, "root_path": "../../", "quiet_root": "../", "nav": "methodology",
    }).dump(str(directory / "index.html"))
