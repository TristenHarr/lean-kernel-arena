#!/usr/bin/env python3
"""A preview of the quiet board from the official arena's own published numbers.

    quietbox/preview-from-arena.py results.json --season scoring/season-1.yaml --out _build/preview-board.json

Each official result becomes one run: its wall time stands for the 4-core lane, its CPU time for
the 1-core lane, its max RSS for memory; there is no energy. These are single measurements on
shared CI runners, which is exactly what the quiet board exists to replace, so the board this
writes is marked as a preview in every page rendered from it. It is for designing the pages and
for reading the official numbers under the quiet board's rules, never for publishing as results.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from lka_quiet.cli import score_store  # noqa: E402
from lka_quiet.session import RunStore  # noqa: E402

LANES = (("4core", "wall_time"), ("1core", "cpu_time"))


def records_from(published: dict, epoch: str) -> list[dict]:
    tests = {t["name"]: t for t in published["tests"]}
    out = []
    for result in published["results"]:
        test = tests.get(result["test"])
        if test is None:
            continue
        for lane, field in LANES:
            wall = result.get(field) or 0.0
            if result["status"] == "accepted" and wall <= 0:
                wall = result.get("wall_time") or 0.0
            out.append({
                "schema": "lka-quiet-run/1", "checker": result["checker"], "test": result["test"], "lane": lane,
                "expected": test.get("outcome"), "status": result["status"], "valid": True, "invalid_reasons": [],
                "wall_s": wall, "memory_peak_bytes": result.get("max_rss") or None, "energy_j": None,
                "timeout_s": result.get("timeout") or test.get("timeout") or 300.0,
                "cap_s": test.get("timeout") or 300.0,
                "checker_hash": result.get("checker_hash") or "unknown", "test_sha256": test["sha256"],
                "epoch": epoch, "round": "preview", "pass": 0, "preview": True,
            })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results_json")
    parser.add_argument("--season", required=True)
    parser.add_argument("--machine", default=str(Path(__file__).resolve().parent.parent / "machines" / "n100-quiet.yaml"))
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    published = json.loads(Path(args.results_json).read_text())
    scoring = yaml.safe_load(Path(args.season).read_text())
    revision = published.get("meta", {}).get("git_revision", "unknown")
    epoch = f"preview-{revision[:12]}"
    with tempfile.TemporaryDirectory() as tmp:
        store = RunStore(Path(tmp), epoch=epoch)
        for record in records_from(published, epoch):
            store.append(record)
        hashes = {}
        for record in store.records():
            hashes[record["checker"]] = record["checker_hash"]
        checkers = [{"name": c["name"], "source_hash": hashes.get(c["name"], "unknown")} for c in published["checkers"]]
        tests = [{**t, "path": None} for t in published["tests"]]
        board = score_store(store, tests, checkers, scoring, machine={"name": "official arena runners (preview)"})
    board["tests"] = tests
    board["checker_meta"] = {c["name"]: {k: c.get(k) for k in ("url", "ref", "rev", "version", "threads", "declines")}
                             for c in published["checkers"]}
    board["preview"] = {
        "source": "the official Lean Kernel Arena's published results",
        "arena_revision": revision,
        "note": "Single measurements on shared CI runners re-scored under the quiet board's rules: "
                "wall time stands for the 4-core lane, CPU time for the 1-core lane, max RSS for memory, "
                "no energy. Not quiet-box measurements.",
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(board, indent=1, sort_keys=True) + "\n")
    ranked = sum(1 for c in board["checkers"] if c["ranked"])
    print(f"preview board: {len(board['checkers'])} checkers, {ranked} ranked, written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
