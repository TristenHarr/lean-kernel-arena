"""The order runs happen in and when a cell has been measured enough.

A cell is one (checker, test, lane). Runs are made in passes: each pass visits every cell that
still needs a run, once, in an order drawn from the round and the pass number. Slow drift of the
machine (temperature, firmware housekeeping) then spreads over all checkers alike instead of
landing on whichever was measured last.
"""

from dataclasses import dataclass

from .rng import SplitMix64, seed_from
from .stats import median, relative_halfwidth, spread

# Tie-break among equally frequent verdicts: the failure wins over the success.
_SEVERITY = ("timeout", "error", "declined", "rejected", "accepted")


@dataclass(frozen=True, order=True)
class Cell:
    checker: str
    test: str
    lane: str


def _valid(records):
    return [r for r in records if r.get("valid")]


def consensus_status(records) -> str | None:
    counts: dict[str, int] = {}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    if not counts:
        return None
    best = max(counts.values())
    for status in _SEVERITY:
        if counts.get(status) == best:
            return status
    return max(counts, key=counts.get)


def cell_state(records, expected: str | None, policy: dict) -> tuple[str, str]:
    """("done" | "more", why)."""
    runs = _valid(records)
    if any(r["status"] == "timeout" for r in runs):
        return "done", "a timeout is final"
    if expected != "accept":
        needed = policy["verdict_only"]["n"]
        return ("done", "verdicts collected") if len(runs) >= needed else ("more", f"{len(runs)}/{needed} verdicts")
    if not runs:
        return "more", "no runs yet"

    accepted = [r["wall_s"] for r in runs if r["status"] == "accepted"]
    if not accepted:
        needed = policy["failure"]["n"]
        return ("done", "consistent failure") if len(runs) >= needed else ("more", "confirming a failure")

    heavy = policy["heavy"]
    if median(accepted) >= heavy["threshold_s"]:
        if len(accepted) >= heavy["n_min"] and spread(accepted) <= heavy["spread_rel"]:
            return "done", "heavy: spread within bound"
        if len(runs) >= heavy["n_max"]:
            return "done", "heavy: run cap reached"
        return "more", "heavy: measuring"

    scored = policy["scored"]
    if (len(accepted) >= scored["n_min"]
            and relative_halfwidth(accepted, scored.get("confidence", 0.95)) <= scored["ci_rel"]):
        return "done", "median interval within bound"
    if len(runs) >= scored["n_max"]:
        return "done", "run cap reached"
    return "more", "measuring"


def pending_cells(cells, records_by_cell: dict, expected_by_test: dict, policy: dict) -> list[Cell]:
    return [
        cell for cell in cells
        if cell_state(records_by_cell.get(cell, []), expected_by_test.get(cell.test), policy)[0] == "more"
    ]


def plan_pass(cells, round_id: str, pass_index: int) -> list[Cell]:
    rng = SplitMix64(seed_from(round_id, f"pass-{pass_index}"))
    return rng.shuffled(sorted(cells))


def scaled_timeout(test_timeout_s, checker_timeout_s, machine: dict) -> float:
    """The arena's limit (the smaller of the test's and the checker's) scaled to this machine."""
    timeouts = machine["timeouts"]
    limits = [t for t in (test_timeout_s, checker_timeout_s) if t]
    if not limits:
        return timeouts["default_s"]
    return max(timeouts["minimum_s"], min(limits) * timeouts["box_factor"])
