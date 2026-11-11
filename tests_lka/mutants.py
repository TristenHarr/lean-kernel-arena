#!/usr/bin/env python3
"""Mutation check of the quiet-board tests: every mutant below breaks one promise the board makes,
and the test suite must fail on each.

    python3 tests_lka/mutants.py            # all mutants
    python3 tests_lka/mutants.py board      # those whose file matches

The tree (build outputs aside) is copied once into a snapshot; the unmutated snapshot must pass
first, since a suite that fails on its own would kill every mutant and prove nothing, and each
mutant is applied to a fresh copy of that snapshot, so edits made to the working tree while the
run goes on reach none of it. Exit status is the number of surviving mutants.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITE_LIMIT_S = 600
SKIP = shutil.ignore_patterns(".git", "_build", "_out", "_results", "__pycache__")

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
    ("lka_quiet/board.py", "        better = sum(1 for d in contenders if d != c and probabilities[d][c] >= threshold)",
     "        better = sum(1 for d in contenders if d != c and probabilities[d][c] > 0.5)",
     "a rank is only taken by a significant win"),
    ("lka_quiet/board.py", "    threshold = 1 - (1 - confidence) / (2 * max(n - 1, 1)) - 1e-12",
     "    threshold = 1 - (1 - confidence) / 2 - 1e-12", "rank decisions are corrected for the comparisons made"),
    ("lka_quiet/board.py", "        worse = sum(1 for d in contenders if d != c and probabilities[c][d] >= threshold)",
     "        worse = 0", "a rank set's right end counts the checkers surely behind"),
    ("lka_quiet/board.py", '        valid = [r for r in cell_records if r.get("valid")]\n',
     "        valid = list(cell_records)\n", "invalid runs never reach the board"),
    ("lka_quiet/board.py", "    if len(epochs) > 1:\n", "    if False:\n",
     "machine epochs are never mixed"),
    ("lka_quiet/board.py", "return values[(len(values) - 1) // 2]", "return sum(values) / len(values)",
     "cells are summarised by their median"),
    ("lka_quiet/board.py", "and any(a < b for a, b in zip(u, v))", "",
     "an equal vector does not dominate"),
    ("lka_quiet/board.py", "relative = None if logs[name] is None or ref_log is None else logs[name] - ref_log",
     "relative = logs[name]", "scores are relative to the reference"),
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
    ("lka_quiet/board.py", "        return (group, keys[name] or (), _file_order(name))",
     "        return (group, (ranks[name] or 0,), _file_order(name))", "rows follow the point estimates"),
    ("lka_quiet/session.py",
     "        \"cap_s\": float(scaled_timeout(test.get(\"timeout\"), None, machine)),\n        \"checker_hash\": checker[\"source_hash\"], \"test_sha256\"",
     "        \"checker_hash\": checker[\"source_hash\"], \"test_sha256\"", "a declared decline carries the test's cap"),
    ("lka_quiet/board.py", "        for metric_id in (m for m in metrics if m in available):\n",
     "        for metric_id in available:\n", "the bootstrap draws the same runs in every process"),
    ("lka_quiet/board.py", "        return factor * scoring[\"caps\"][\"instruction_rate\"] * timeout\n",
     "        return scoring[\"caps\"][\"instruction_rate\"] * timeout\n",
     "a failure costs twice the cap in instructions too"),
    ("lka_quiet/record.py", "instructions = None if None in retired else sum(retired)", "instructions = retired[0]",
     "instructions are every thread's user and kernel count"),
    ("quietbox/preview-from-arena.py", "\"instructions\": result.get(\"instructions\") or None,",
     "\"instructions\": None,", "the preview carries the official instruction counts"),
    ("lka_quiet/board.py", "    return tuple(f\"{name}.yaml\".split(\"/\"))\n", "    return (name,)\n",
     "exact ties keep the Arena's file order"),
    ("lka_quiet/board.py", "        outcome = test.get(\"outcome\")\n        if status in (\"declined\", \"error\", \"timeout\"):\n",
     "        outcome = test.get(\"outcome\")\n        if status == \"declined\":\n",
     "under the arena rule an error or a timeout is a decline"),
    ("lka_quiet/board.py", "            counts[\"invalid_accepted\"] += 1\n", "            pass\n",
     "under the arena rule an accepted invalid proof counts"),
    ("lka_quiet/board.py", "    return (counts[\"invalid_accepted\"], counts[\"valid_rejected\"],",
     "    return (counts[\"valid_rejected\"], counts[\"invalid_accepted\"],",
     "under the arena rule soundness comes before completeness"),
    ("lka_quiet/board.py", "            measure if measure is not None else math.inf, counts[\"declined\"])",
     "            measure if measure is not None else 0, counts[\"declined\"])",
     "a checker that did not check Mathlib ranks below every one that did"),
    ("lka_quiet/board.py", "    return raw if raw and raw > 0 else None\n", "    return raw\n",
     "a zero instruction count is no count"),
    ("lka_quiet/board.py", "            contenders = [n for n in names if keys[n] is not None]\n",
     "            contenders = [n for n in ranked if keys[n] is not None]\n",
     "the arena rule places every checker, the unsound and the reference too"),
    ("lka_quiet/board.py", "factor = measures[reference] / measures[name] if arena else math.exp(-relative)",
     "factor = measures[name] / measures[reference] if arena else math.exp(-relative)",
     "the arena factor is how many times fewer instructions than the reference"),
    ("lka.py", "    return (bad_not_rejected, good_not_accepted, instructions_sort_key, stats[\"declined_count\"])",
     "    return (good_not_accepted, bad_not_rejected, instructions_sort_key, stats[\"declined_count\"])",
     "the arena category is checked against lka.py's own key, not a copy of it"),
    ("lka_quiet/site.py", "        if result[\"rank\"] is None or result[\"factor\"] is None:\n",
     "        if result[\"rank\"] is None:\n", "the forest holds only checkers with a factor"),
    ("lka_quiet/site.py", "            ranked = placed[category[\"id\"]]\n",
     "            ranked = sum(1 for n in names if entries[n][\"categories\"][category[\"id\"]][\"rank\"] is not None)\n",
     "the rank grid shades each category by its own field"),
    ("lka_quiet/site.py", "\"hi\": first[0][\"hi\"], \"ranked\": placed}", "\"hi\": first[0][\"hi\"], \"ranked\": len(rows)}",
     "a leader tile counts every placed checker"),
    ("lka_quiet/board.py", "        if i and probabilities[order[i - 1]][name] < threshold:\n",
     "        if i and probabilities[order[i - 1]][name] < 0.5:\n",
     "a place is shared unless the checker above surely beats it"),
    ("lka_quiet/board.py", "    threshold = 1 - (1 - confidence) / (2 * max(len(order) - 1, 1)) - 1e-12\n",
     "    threshold = confidence\n", "places are corrected for the neighbouring comparisons"),
    ("lka_quiet/board.py", "                    sample += [(test_name, share) for test_name in free]\n",
     "                    sample += [(free[rng.below(len(free))], share) for _ in free]\n",
     "only measurement noise ties a place, never a redraw of the tests"),
    ("lka_quiet/board.py", "        ordered = sorted(contenders, key=lambda n: (keys[n], _file_order(n)))\n",
     "        ordered = sorted(contenders, key=lambda n: (sets[n], _file_order(n)))\n",
     "places follow the point estimates"),
    ("lka_quiet/board.py", "            layers[name] = layer\n", "            layers[name] = 1\n",
     "a dominated checker sits on a later Pareto layer"),
    ("lka_quiet/board.py", "                \"n\": len(tv.values) if tv.values else 0,\n", "                \"n\": 1,\n",
     "a cell reports how many runs it holds"),
    ("lka_quiet/board.py", "                if tv.cdf is None or len(tv.cdf) == 1:\n",
     "                if tv.cdf is not None or len(tv.cdf or ()) == 1:\n",
     "the bootstrap redraws every cell with more than one run"),
    ("lka_quiet/charts.py", "            path.append((front[k - 1][0], y))\n", "            path.append((x, front[k - 1][1]))\n",
     "the staircase turns at the corner the front leaves dominated"),
    ("lka_quiet/site.py", "q is not p and q[\"x\"] <= p[\"x\"] and q[\"y\"] <= p[\"y\"] and (q[\"x\"] < p[\"x\"] or q[\"y\"] < p[\"y\"])",
     "q is not p and q[\"x\"] >= p[\"x\"] and q[\"y\"] >= p[\"y\"] and (q[\"x\"] > p[\"x\"] or q[\"y\"] > p[\"y\"])",
     "in seconds and bytes, less is better"),
    ("lka_quiet/site.py", "beaten_by = [d[\"name\"] for d in ordered if _dominates(vectors[d[\"name\"]], vectors[e[\"name\"]])]",
     "beaten_by = [d[\"name\"] for d in ordered if _dominates(vectors[e[\"name\"]], vectors[d[\"name\"]])]",
     "a layer row names the checkers that beat it, not those it beats"),
    ("lka_quiet/site.py", "closest = [d for d in beaten_by if not any(_dominates(vectors[d], vectors[o]) for o in beaten_by)]",
     "closest = list(beaten_by)", "the closest dominators beat no other dominator"),
    ("lka_quiet/site.py", "        return \"correct\" if status == \"rejected\" else \"wrong\"\n", "        return \"correct\"\n",
     "accepting an invalid proof is marked wrong in the test table"),
    ("lka_quiet/site.py", "(len(ranked) - 1 - place) / (len(ranked) - 1)", "place / (len(ranked) - 1)",
     "a library cell is shaded darkest for the best"),
    ("lka_quiet/encode.py", "        for a in range(len(items) - 1, 0, -1):\n", "        for a in range(0):\n",
     "a re-encoding is drawn from its seed"),
    ("lka_quiet/encode.py",
     "                stack.append((key, True))\n                children = [c for c in references(record) if not _implicit(*c) and not assigned(*c)]\n",
     "                children = [c for c in references(record) if not _implicit(*c) and not assigned(*c)]\n"
     "                stack.extend((child, False) for child in children if child in segment)\n"
     "                stack.append((key, True))\n                children = []\n",
     "a record is emitted after everything it refers to"),
    ("lka_quiet/encode.py", "            counters[kind] += 1\n", "",
     "ids are renumbered densely in the new order"),
    ("lka_quiet/encode.py", "        return {id_key: own, key: {**v, \"name\": n(v[\"name\"]), \"type\": e(v[\"type\"]), \"value\": e(v[\"value\"]),",
     "        return {id_key: own, key: {**v, \"name\": n(v[\"name\"]), \"type\": e(v[\"type\"]), \"value\": v[\"value\"],",
     "a let's value follows its renumbering"),
    ("lka_quiet/encode.py", "        return [(\"n\", value[\"typeName\"]), (\"e\", value[\"struct\"])]\n",
     "        return [(\"e\", value[\"struct\"])]\n", "a projection's structure name is one of its references"),
    ("lka_quiet/encode.py", "            ctors = [{**c, \"name\": n(c[\"name\"]), \"induct\": n(c[\"induct\"]),",
     "            ctors = [{**c, \"name\": n(c[\"name\"]), \"induct\": c[\"induct\"],",
     "a constructor's inductive follows its renumbering"),
    ("lka_quiet/encode.py", "            body.pop(_ID_KEY[k], None)\n", "",
     "a fingerprint does not depend on ids"),
    ("lka_quiet/helper.py", "status=str(run_dir / \"status.json\"), run=req[\"cmd\"], input=req[\"input\"], input_gid=input_gid,",
     "status=str(run_dir / \"status.json\"), run=req[\"cmd\"], input=None, input_gid=input_gid,",
     "every measured run sees its own input alone"),
    ("lka_quiet/cli.py", "        target.chmod(0o640)\n", "        target.chmod(0o644)\n",
     "a staged input is readable by the input group alone"),
    ("lka_quiet/twins.py", "        donors = [d.index for d in decls[:target.index]\n", "        donors = [d.index for d in decls\n",
     "a donor is declared before its target"),
    ("lka_quiet/twins.py", "and d.level_names == target.level_names\n", "\n",
     "a donor has its target's universes"),
    ("lka_quiet/twins.py", "                  and d.type_hash != target.type_hash and d.value_hash != target.value_hash]",
     "                  and d.value_hash != target.value_hash]", "a donor's type is not its target's"),
    ("lka_quiet/twins.py", "    for tried, (t, d) in enumerate(itertools.islice(order, max_tries), start=1):",
     "    for tried, (t, d) in enumerate(order, start=1):", "a twin file tries at most max_tries candidates"),
    ("lka_quiet/cli.py", "        if test.get(\"outcome\") != \"accept\" or not any(fnmatch.fnmatchcase(test[\"name\"], p) for p in twin_from):",
     "        if not any(fnmatch.fnmatchcase(test[\"name\"], p) for p in twin_from):",
     "only a test to be accepted is twinned"),
    ("lka_quiet/cli.py", "\"seed_sha256\": hashlib.sha256(seed.encode()).hexdigest()", "\"seed_sha256\": seed",
     "a round commits to its seed and never holds it"),
    ("lka_quiet/cli.py", "capture_output=True, timeout=timeout_s).returncode == 1",
     "capture_output=True, timeout=timeout_s).returncode != 0", "only the kernel's rejection verifies a twin"),
    ("lka_quiet/cli.py", "    if args.twin and not args.verify_with:\n", "    if False:\n",
     "a twin is never staged unverified"),
    ("lka_quiet/cli.py", "        if \"encoded_from\" not in test and \"twin_of\" not in test and test.get(\"size\") is not None:",
     "        if test.get(\"size\") is not None:", "a board never publishes the size of the bytes measured"),
    ("lka_quiet/cli.py", "        entry = {\"name\": test[\"name\"], \"outcome\": test.get(\"outcome\"), \"official_sha256\": test.get(\"official_sha256\")}",
     "        entry = {\"name\": test[\"name\"], \"outcome\": test.get(\"outcome\"), \"official_sha256\": test.get(\"official_sha256\"), \"sha256\": test.get(\"sha256\")}",
     "a board never publishes the sha256 of the bytes measured"),
]


def run_suite(snapshot: Path, mutant: tuple[str, str, str] | None) -> tuple[str, str]:
    """The suite on a copy of `snapshot` with `mutant` (file, original, mutated) applied:
    ("pass" or "fail", its output), ("hung", why) when it does not finish in SUITE_LIMIT_S, or
    ("broken", why) when the original text is not found once."""
    with tempfile.TemporaryDirectory(prefix="lka-mutant-") as tmp:
        copy = Path(tmp) / "tree"
        shutil.copytree(snapshot, copy)
        if mutant:
            path, original, mutated = mutant
            target = copy / path
            source = target.read_text()
            if source.count(original) != 1:
                return "broken", f"the original text occurs {source.count(original)} times"
            target.write_text(source.replace(original, mutated))
        try:
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests_lka", "-t", "."],
                cwd=copy, capture_output=True, text=True, timeout=SUITE_LIMIT_S,
            )
        except subprocess.TimeoutExpired:
            return "hung", f"the suite did not finish in {SUITE_LIMIT_S} s"
        return ("pass" if result.returncode == 0 else "fail"), result.stderr


def main() -> int:
    selector = sys.argv[1] if len(sys.argv) > 1 else ""
    with tempfile.TemporaryDirectory(prefix="lka-mutant-snapshot-") as tmp:
        snapshot = Path(tmp) / "tree"
        shutil.copytree(ROOT, snapshot, ignore=SKIP)
        verdict, output = run_suite(snapshot, None)
        if verdict != "pass":
            print(output)
            print("the suite fails on an unmutated copy of the tree; no mutant can be judged until it passes")
            return 1
        survivors = 0
        chosen = [m for m in MUTANTS if selector in m[0]]
        for path, original, mutated, promise in chosen:
            verdict, output = run_suite(snapshot, (path, original, mutated))
            if verdict == "broken":
                print(f"BROKEN   {path}: {promise} ({output})")
                survivors += 1
            elif verdict == "fail":
                print(f"killed   {path}: {promise}")
            elif verdict == "hung":
                print(f"killed   {path}: {promise} (the suite hung)")
            else:
                print(f"SURVIVED {path}: {promise}")
                survivors += 1
        print(f"{len(chosen) - survivors}/{len(chosen)} mutants killed")
        return survivors


if __name__ == "__main__":
    sys.exit(main())
