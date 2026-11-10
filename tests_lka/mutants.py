#!/usr/bin/env python3
"""Mutation check of the quiet-board tests: every mutant below breaks one promise the board makes,
and the test suite must fail on each.

    python3 tests_lka/mutants.py            # all mutants
    python3 tests_lka/mutants.py board      # those whose file matches

Each mutant is applied to a scratch copy of lka_quiet/ and tests_lka/; the working tree is never
touched. Exit status is the number of surviving mutants.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (file, original text, mutated text, the promise it breaks)
MUTANTS = [
    ("lka_quiet/rng.py", "limit = (1 << 64) - ((1 << 64) % n)", "limit = 1 << 64",
     "below() draws by rejection, not by plain modulo"),
    ("lka_quiet/rng.py", "for i in range(len(out) - 1, 0, -1):", "for i in range(len(out) - 1, 1, -1):",
     "a shuffle can move every element"),
    ("lka_quiet/rng.py", '"\\x1f".join(parts)', '"".join(parts)',
     "seed parts are separated"),
    ("lka_quiet/stats.py", "if cumulative / denominator <= tail:", "if cumulative / denominator < tail * 2:",
     "median intervals reach the requested coverage"),
    ("lka_quiet/stats.py", "return (values[mid - 1] + values[mid]) / 2", "return values[mid]",
     "the median of an even sample is the mean of its middle pair"),
    ("lka_quiet/perf.py", "raw[name] = raw.get(name, 0.0) + value * fraction", "raw[name] = value * fraction",
     "hybrid PMUs are summed"),
    ("lka_quiet/perf.py", "fraction = min(runtime / enabled, 1.0)", "fraction = 1.0",
     "perf's enabled/running scaling of a hybrid PMU is undone"),
    ("lka_quiet/perf.py", "        if event.running_fraction < 1.0:\n", "        if event.running_fraction < 0.4:\n",
     "a multiplexed counter is never kept"),
    ("lka_quiet/perf.py", 'value *= _UNIT_SECONDS.get(record.get("unit", ""), 1e-9)', "value *= 1e-9",
     "task-clock in msec becomes seconds"),
    ("lka_quiet/sysfs.py", "return after + max_range_uj - before", "return after - before",
     "RAPL wraparound"),
    ("lka_quiet/sysfs.py", "busy = user + nice + system + irq + softirq + steal", "busy = user + system",
     "busy time counts every non-idle state"),
    ("lka_quiet/sysfs.py", "if len(counts) != len(cpus):", "if not counts:",
     "single-count interrupt rows are not spread over CPUs"),
    ("lka_quiet/sysfs.py", "if record.signal in CRASH_SIGNALS:", "if record.signal is not None:",
     "SIGKILL/SIGTERM are not crashes"),
    ("lka_quiet/sysfs.py", "if record.uid != sandbox_uid or record.signal is None:", "if record.signal is None:",
     "only the sandbox user's processes are the run"),
    ("lka_quiet/record.py", "if crash is not None or oom_kills > 0 or signal is not None:", "if crash is not None:",
     "OOM kills and signal deaths are errors"),
    ("lka_quiet/record.py", 'if abs(deviation) > frequency.get("tolerance", 0.005):', "if False:",
     "a run at the wrong frequency is repeated"),
    ("lka_quiet/record.py", "    if core_throttles or package_throttles:\n", "    if False:\n",
     "a throttled run is repeated"),
    ("lka_quiet/record.py", "    if foreign_ms > allowed_ms:\n", "    if foreign_ms > 10 * allowed_ms:\n",
     "interference on the lane is caught"),
    ("lka_quiet/record.py", "    lane_ms = lane_busy_jiffies(before, after, lane_cpus) * jiffy_ms",
     "    lane_ms = lane_busy_jiffies(before, after, sorted(before)) * jiffy_ms",
     "only the lane's CPUs count as interference"),
    ("lka_quiet/record.py", "crash = None if timed_out else crash_of(acct_records, sandbox_uid)", "crash = None",
     "a crash masked by `|| exit 1` is an error"),
    ("lka_quiet/record.py", 'wall_s = shim["wall_ns"] / 1e9 if shim else float(obs["elapsed_s"])',
     'wall_s = float(obs["elapsed_s"])', "wall time is the shim's clock, not the helper's"),
    ("lka_quiet/plan.py", 'if any(r["status"] == "timeout" for r in runs):', "if False:",
     "a timeout is final"),
    ("lka_quiet/plan.py", "    runs = _valid(records)\n", "    runs = list(records)\n",
     "invalid runs do not count"),
    ("lka_quiet/plan.py", 'and relative_halfwidth(accepted, scored.get("confidence", 0.95)) <= scored["ci_rel"]):',
     "):", "small tests run until the median interval is tight"),
    ("lka_quiet/plan.py", "    return rng.shuffled(sorted(cells))", "    return rng.shuffled(cells)",
     "a pass's order does not depend on input order"),
    ("lka_quiet/plan.py", "return max(timeouts[\"minimum_s\"], min(limits) * timeouts[\"box_factor\"])",
     "return max(timeouts[\"minimum_s\"], max(limits) * timeouts[\"box_factor\"])",
     "the smaller of the test's and the checker's timeout applies"),
    ("lka_quiet/board.py", 'factor = scoring.get("failure_factor", 2)', "factor = 1",
     "failures score PAR-2"),
    ("lka_quiet/board.py", "    support = [math.log(max(v, floor)) for v in values]\n    point = values[(len(values) - 1) // 2]\n    return CellValue(point, False, math.log(max(point, floor)),",
     "    support = [math.log(v) for v in values]\n    point = values[(len(values) - 1) // 2]\n    return CellValue(point, False, math.log(point),",
     "floors erase imperceptible differences"),
    ("lka_quiet/board.py", 'if outcome == "reject" and any(s and s.accepted_any for s in lane_summaries):',
     'if outcome == "reject" and any(s and s.status == "accepted" for s in lane_summaries):',
     "one accepted bad proof in any run is unsound"),
    ("lka_quiet/board.py", 'if outcome == "accept" and any(s and s.status == "rejected" for s in lane_summaries):',
     "if False:", "wrongly rejecting a good proof gates the ranking"),
    ("lka_quiet/board.py", "sample += [(free[rng.below(len(free))], share) for _ in free]",
     "sample += [(t, share) for t in free]", "the bootstrap redraws each family's tests"),
    ("lka_quiet/board.py", "if d != c and probabilities[d][c] >= upper_q", "if d != c and probabilities[d][c] > 0.5",
     "a rank is only taken by a significant win"),
    ("lka_quiet/board.py", '        valid = [r for r in cell_records if r.get("valid")]\n',
     "        valid = list(cell_records)\n", "invalid runs never reach the board"),
    ("lka_quiet/board.py", "    if len(epochs) > 1:\n", "    if False:\n",
     "machine epochs are never mixed"),
    ("lka_quiet/board.py", "return values[(len(values) - 1) // 2]", "return sum(values) / len(values)",
     "cells are summarised by their median"),
    ("lka_quiet/board.py", "and any(o < v for o, v in zip(other, vector))", "",
     "an equal vector does not dominate"),
    ("lka_quiet/board.py", "relative = None if score is None or ref_raw is None else score - ref_raw",
     "relative = score", "scores are relative to the reference"),
    ("lka_quiet/board.py", "                weights[key] *= factor\n", "                weights[key] *= 1\n",
     "the sensitivity view perturbs the weights"),
    ("lka_quiet/board.py", 'if test.get("outcome") != "accept":', "if False:",
     "only good tests are scored for performance"),
    ("lka_quiet/session.py", "        if checker_hashes.get(record[\"checker\"]) != record.get(\"checker_hash\"):\n            continue\n", "",
     "a run counts only for the checker revision it measured"),
    ("lka_quiet/session.py", "since = self._last_good_seq if self._last_good_seq is not None else -1", "since = -1",
     "a bad sentinel reading quarantines only the runs since the last good one"),
    ("lka_quiet/session.py", "                if self.clock() >= deadline:\n", "                if False:\n",
     "a session ends with its time budget"),
    ("lka_quiet/session.py", "        self._record_declines()\n", "",
     "declared declines are recorded without running"),
    ("lka_quiet/plan.py", "    if any(r.get(\"declared\") for r in runs):\n", "    if False:\n",
     "a declared decline is final"),
    ("lka_quiet/board.py", "    timeout = summary.cap_s\n", "    timeout = summary.timeout_s or summary.cap_s\n",
     "a failure is scored at the test's cap, never the checker's own shorter limit"),
    ("lka_quiet/board.py", "        return (group, result[\"score\"] if result[\"score\"] is not None else math.inf, name)",
     "        return (group, result[\"rank\"] or 0, name)", "rows follow the point estimates"),
    ("lka_quiet/session.py",
     "        \"cap_s\": float(scaled_timeout(test.get(\"timeout\"), None, machine)),\n        \"checker_hash\": checker[\"source_hash\"], \"test_sha256\"",
     "        \"checker_hash\": checker[\"source_hash\"], \"test_sha256\"", "a declared decline carries the test's cap"),
]


def run_mutant(path: str, original: str, mutated: str) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="lka-mutant-") as tmp:
        tmp = Path(tmp)
        shutil.copytree(ROOT / "lka_quiet", tmp / "lka_quiet")
        shutil.copytree(ROOT / "tests_lka", tmp / "tests_lka")
        target = tmp / path
        source = target.read_text()
        if source.count(original) != 1:
            return False, f"the original text occurs {source.count(original)} times"
        target.write_text(source.replace(original, mutated))
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests_lka", "-t", "."],
            cwd=tmp, capture_output=True, text=True, timeout=600,
        )
        return result.returncode != 0, ""


def main() -> int:
    selector = sys.argv[1] if len(sys.argv) > 1 else ""
    survivors = 0
    chosen = [m for m in MUTANTS if selector in m[0]]
    for path, original, mutated, promise in chosen:
        killed, problem = run_mutant(path, original, mutated)
        if problem:
            print(f"BROKEN   {path}: {promise} ({problem})")
            survivors += 1
        elif killed:
            print(f"killed   {path}: {promise}")
        else:
            print(f"SURVIVED {path}: {promise}")
            survivors += 1
    print(f"{len(chosen) - survivors}/{len(chosen)} mutants killed")
    return survivors


if __name__ == "__main__":
    sys.exit(main())
