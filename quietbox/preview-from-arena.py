#!/usr/bin/env python3
"""A preview of the quiet board from the official arena's own published numbers.

    quietbox/preview-from-arena.py --season scoring/season-1.yaml --out _build/preview-board.json \
        --save _build/official-results.json

With no results file it fetches the arena's latest published results (and keeps a copy with --save,
for build-site); a path or URL names another round.

Each official result becomes one run: its wall time stands for the 4-core lane, its CPU time for
the 1-core lane, its max RSS for memory, its instruction count for instructions; there is no energy. These are single measurements on
shared CI runners, which is exactly what the quiet board exists to replace, so the board this
writes is marked as a preview in every page rendered from it. It is for designing the pages and
for reading the official numbers under the quiet board's rules, never for publishing as results.
"""

import argparse
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from lka_quiet.cli import score_store  # noqa: E402
from lka_quiet.session import RunStore  # noqa: E402

LANES = (("4core", "wall_time"), ("1core", "cpu_time"))
LATEST = "https://arena.lean-lang.org/results.json"
NOTE = ("Single measurements on shared CI runners re-scored under the quiet board's rules: "
        "wall time stands for the 4-core lane, CPU time for the 1-core lane, max RSS for memory, "
        "the instruction count for instructions, no energy. Not quiet-box measurements.")


def load_published(source: str, save: str | None) -> tuple[dict, str]:
    if source.startswith(("https://", "http://")):
        with urllib.request.urlopen(source, timeout=120) as response:
            text = response.read().decode()
    else:
        text = Path(source).read_text()
    data = json.loads(text)
    if save:
        Path(save).parent.mkdir(parents=True, exist_ok=True)
        Path(save).write_text(text)
    return data, source


def describe(published: dict, source: str) -> dict:
    meta = published.get("meta", {})
    return {
        "source": "the official Lean Kernel Arena's published results",
        "url": source,
        "arena_revision": meta.get("git_revision", "unknown"),
        "revision_url": meta.get("github_url"),
        "published": meta.get("timestamp"),
        "note": NOTE,
    }


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
                "instructions": result.get("instructions") or None,
                "timeout_s": result.get("timeout") or test.get("timeout") or 300.0,
                "cap_s": test.get("timeout") or 300.0,
                "checker_hash": result.get("checker_hash") or "unknown", "test_sha256": test["sha256"],
                "epoch": epoch, "round": "preview", "pass": 0, "preview": True,
            })
    return out


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results_json", nargs="?", default=LATEST,
                        help=f"the official results, a path or a URL (default: the latest, {LATEST})")
    parser.add_argument("--save", help="keep a copy of the results read, for build-site --results")
    parser.add_argument("--season", required=True)
    parser.add_argument("--machine", default=str(Path(__file__).resolve().parent.parent / "machines" / "n100-quiet.yaml"))
    parser.add_argument("--out", required=True)
    return parser.parse_args(argv)


def main() -> int:
    args = arguments()

    published, source = load_published(args.results_json, args.save)
    scoring = yaml.safe_load(Path(args.season).read_text())
    preview = describe(published, source)
    epoch = f"preview-{preview['arena_revision'][:12]}"
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
    board["preview"] = preview
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(board, indent=1, sort_keys=True) + "\n")
    ranked = sum(1 for c in board["checkers"] if c["ranked"])
    print(f"preview board from {source}, arena {preview['arena_revision'][:12]} published {preview['published']}: "
          f"{len(board['checkers'])} checkers, {ranked} ranked, written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
