"""The quiet board's pages: built from board.json beside the arena's own site, under quiet/.

    quiet/                     the board: leaders, the overall ranking, correctness, trade-offs
    quiet/category/<id>/       one category: its ranking, rank intervals and head-to-head matrix
    quiet/checker/<name>/      one checker: every category, every test, every metric
    quiet/methodology/         how it is measured and scored, with the season's rules
    quiet/board.json           the data every page is drawn from
"""

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


def fmt_quantity(value, quantity: str) -> str:
    return {"wall_s": fmt_seconds, "memory_bytes": fmt_bytes, "energy_j": fmt_joules}[quantity](value)


def rank_label(result: dict) -> str:
    if result.get("rank") is None:
        return "–"
    return f"{result['rank']}=" if result.get("tied") else str(result["rank"])


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
        if result["rank"] is None:
            continue
        rows.append({"name": name, "rank": rank_label(result), "factor": result["factor"],
                     "lo": result["factor_lo"], "hi": result["factor_hi"], "tied": result["tied"],
                     "href": f"{href_prefix}checker/{name}/"})
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
    return {"names": names, "label": label, "factor": first[0]["factor"], "lo": first[0]["lo"],
            "hi": first[0]["hi"], "ranked": len(rows)}


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
    rows = []
    for name in names:
        cells = []
        for category in categories:
            result = entries[name]["categories"][category["id"]]
            ranked = sum(1 for n in names if entries[n]["categories"][category["id"]]["rank"] is not None)
            if result["rank"] is None or ranked < 2:
                cells.append({"text": "–", "q": None})
                continue
            goodness = (ranked - result["rank"]) / (ranked - 1)
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


def render(board: dict, season: dict, machine: dict, env, outdir: Path, build_info: dict) -> None:
    if board.get("schema") != BOARD_SCHEMA:
        raise SiteError(f"board.json is {board.get('schema')!r}; this site renders {BOARD_SCHEMA}: score it again with quiet-score")
    env.filters.update({"factor": charts.factor_text, "seconds": fmt_seconds, "bytes": fmt_bytes,
                        "joules": fmt_joules})
    env.globals.update({"rank_label": rank_label, "interval_text": interval_text, "fmt_quantity": fmt_quantity})
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

    ranked_overall = forest_rows(board, "overall", "")
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

    tiles = [{"category": c, "leaders": leaders(board, c["id"])} for c in categories]
    context = {
        **common, "root_path": "../", "quiet_root": "", "nav": "board", "tiles": tiles,
        "overall_svg": _svg(charts.forest(forest_rows(board, "overall", ""), title="Overall",
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
    env.get_template("quiet/index.html").stream(context).dump(str(root / "index.html"))

    for category in categories:
        directory = root / "category" / category["id"]
        directory.mkdir(parents=True, exist_ok=True)
        rows = forest_rows(board, category["id"], "../../")
        names = [r["name"] for r in rows]
        env.get_template("quiet/category.html").stream({
            **common, "root_path": "../../../", "quiet_root": "../../", "nav": "categories", "category": category,
            "description": descriptions.get(category["id"], ""),
            "forest_svg": _svg(charts.forest(rows, title=category["title"], subtitle=descriptions.get(category["id"], ""))),
            "rows": rows, "results": {n: entries[n]["categories"][category["id"]] for n in entries},
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

    directory = root / "methodology"
    directory.mkdir(parents=True, exist_ok=True)
    env.get_template("quiet/methodology.html").stream({
        **common, "root_path": "../../", "quiet_root": "../", "nav": "methodology",
    }).dump(str(directory / "index.html"))
