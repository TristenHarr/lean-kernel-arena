"""A measurement session: runs cells in randomized passes until they are measured enough, the time
budget ends, or the sentinel says the machine drifted.

Runs are kept append-only, one JSON file each, under results/<epoch>/runs/. A run counts for a cell
only while the checker's source hash and the test's sha256 are the ones it measured, so a new
checker revision is measured afresh and the old runs stay as history. A sentinel reading outside
its limits quarantines every run made since the last good reading (they stay on disk, marked
invalid) and ends the session.
"""

import json
import os
import time
from pathlib import Path

from .plan import Cell, pending_cells, plan_pass, scaled_timeout

RUN_SCHEMA = "lka-quiet-run/1"


def _safe(name: str) -> str:
    return name.replace("/", "_")


class RunStore:
    def __init__(self, root: Path, epoch: str):
        self.dir = Path(root) / epoch
        self.runs = self.dir / "runs"
        self.epoch = epoch
        self._next = None

    def _files(self):
        if not self.runs.exists():
            return []
        return sorted(p for p in self.runs.iterdir() if p.suffix == ".json")

    def next_seq(self) -> int:
        if self._next is None:
            files = self._files()
            self._next = int(files[-1].name.split("-", 1)[0]) + 1 if files else 0
        return self._next

    def append(self, record: dict) -> dict:
        seq = self.next_seq()
        record = {**record, "seq": seq}
        self.runs.mkdir(parents=True, exist_ok=True)
        name = f"{seq:08d}-{_safe(record['checker'])}-{_safe(record['test'])}-{record['lane']}.json"
        tmp = self.runs / f".{name}.tmp"
        tmp.write_text(json.dumps(record, sort_keys=True, indent=1) + "\n")
        os.replace(tmp, self.runs / name)
        self._next = seq + 1
        return record

    def _quarantine_file(self) -> Path:
        return self.dir / "quarantine.json"

    def quarantine(self, seqs, reason: str) -> None:
        entries = json.loads(self._quarantine_file().read_text()) if self._quarantine_file().exists() else []
        entries.append({"seqs": sorted(seqs), "reason": reason})
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / ".quarantine.json.tmp"
        tmp.write_text(json.dumps(entries, indent=1) + "\n")
        os.replace(tmp, self._quarantine_file())

    def records(self) -> list[dict]:
        quarantined = {}
        if self._quarantine_file().exists():
            for entry in json.loads(self._quarantine_file().read_text()):
                for seq in entry["seqs"]:
                    quarantined.setdefault(seq, entry["reason"])
        out = []
        for path in self._files():
            record = json.loads(path.read_text())
            if record["seq"] in quarantined:
                record["valid"] = False
                record["invalid_reasons"] = record.get("invalid_reasons", []) + [f"quarantined: {quarantined[record['seq']]}"]
            out.append(record)
        return out


def current_records(records, checker_hashes: dict, test_shas: dict) -> dict[Cell, list]:
    by_cell: dict[Cell, list] = {}
    for record in records:
        if record.get("kind") == "sentinel":
            continue
        if checker_hashes.get(record["checker"]) != record.get("checker_hash"):
            continue
        if test_shas.get(record["test"]) != record.get("test_sha256"):
            continue
        by_cell.setdefault(Cell(record["checker"], record["test"], record["lane"]), []).append(record)
    return by_cell


def checker_workdir(checker: dict, machine: dict) -> str:
    """Where the arena runs the checker: its source for url/dir checkers, else its build directory."""
    base = Path(machine["sandbox"]["homes"]) / checker["name"] / "arena" / "_build" / "checkers" / checker["name"]
    return str(base / "src") if checker.get("url") or checker.get("dir") else str(base)


def build_request(checker, test, lane, machine, *, env, round_id, pass_index, seq, epoch) -> dict:
    return {
        "checker": checker["name"],
        "test": test["name"],
        "lane": lane,
        "expected": test.get("outcome"),
        "cmd": checker["run"],
        "workdir": checker_workdir(checker, machine),
        "input": test["path"],
        "timeout_s": float(scaled_timeout(test.get("timeout"), checker.get("timeout"), machine)),
        "cap_s": float(scaled_timeout(test.get("timeout"), None, machine)),
        "env": dict(env),
        "checker_hash": checker["source_hash"],
        "test_sha256": test["sha256"],
        "epoch": epoch,
        "round": round_id,
        "pass": pass_index,
        "seq": seq,
    }


def declined_record(checker, test, lane, machine, *, round_id, epoch) -> dict:
    return {
        "schema": RUN_SCHEMA, "checker": checker["name"], "test": test["name"], "lane": lane,
        "expected": test.get("outcome"), "status": "declined", "correctness": "declined", "declared": True,
        "valid": True, "invalid_reasons": [], "wall_s": 0.0, "memory_peak_bytes": None, "energy_j": None,
        "cap_s": float(scaled_timeout(test.get("timeout"), None, machine)),
        "checker_hash": checker["source_hash"], "test_sha256": test["sha256"], "epoch": epoch,
        "round": round_id, "pass": None,
    }


class Session:
    def __init__(self, *, store: RunStore, checkers, tests, machine, round_id, env, measure, clock=time.monotonic,
                 sentinel=None, log=None):
        self.store = store
        self.checkers = list(checkers)
        self.tests = list(tests)
        self.machine = machine
        self.round_id = round_id
        self.env = env
        self.measure = measure
        self.clock = clock
        self.sentinel = sentinel
        self.log = log or (lambda message: None)
        self.lanes = sorted(machine["lanes"])
        self.policy = machine["repetitions"]
        self.expected = {t["name"]: t.get("outcome") for t in self.tests}
        self.checker_hashes = {c["name"]: c["source_hash"] for c in self.checkers}
        self.test_shas = {t["name"]: t["sha256"] for t in self.tests}
        self._last_good_seq = None
        self._last_sentinel = None

    def _cells(self) -> list[Cell]:
        return [Cell(c["name"], t["name"], lane)
                for c in self.checkers for t in self.tests for lane in self.lanes
                if t["name"] not in c.get("declines", [])]

    def _record_declines(self) -> None:
        current = current_records(self.store.records(), self.checker_hashes, self.test_shas)
        for checker in self.checkers:
            for test in self.tests:
                if test["name"] not in checker.get("declines", []):
                    continue
                for lane in self.lanes:
                    if not current.get(Cell(checker["name"], test["name"], lane)):
                        self.store.append(declined_record(checker, test, lane, self.machine, round_id=self.round_id,
                                                          epoch=self.store.epoch))

    def _read_sentinel(self) -> bool:
        ok, record = self.sentinel()
        record = self.store.append({**record, "kind": "sentinel", "round": self.round_id, "epoch": self.store.epoch})
        self._last_sentinel = self.clock()
        if ok:
            self._last_good_seq = record["seq"]
            return True
        since = self._last_good_seq if self._last_good_seq is not None else -1
        suspect = [r["seq"] for r in self.store.records()
                   if r["seq"] > since and r.get("kind") != "sentinel" and not r.get("declared")]
        if suspect:
            self.store.quarantine(suspect, f"sentinel reading {record.get('wall_s')} s out of limits")
        self.log(f"sentinel out of limits: quarantined {len(suspect)} runs")
        return False

    def _sentinel_due(self) -> bool:
        return (self.sentinel is not None
                and self.clock() - self._last_sentinel >= self.machine["sentinel"]["every_s"])

    def _finish(self, outcome: str, runs: int) -> dict:
        if outcome != "sentinel" and self.sentinel is not None and runs and not self._read_sentinel():
            outcome = "sentinel"
        current = current_records(self.store.records(), self.checker_hashes, self.test_shas)
        pending = pending_cells(self._cells(), current, self.expected, self.policy)
        return {"outcome": outcome, "runs": runs, "pending": len(pending)}

    def run(self, budget_s: float) -> dict:
        deadline = self.clock() + budget_s
        self._record_declines()
        if self.sentinel is not None and not self._read_sentinel():
            return self._finish("sentinel", 0)
        records = self.store.records()
        passes = [r["pass"] for r in records if r.get("round") == self.round_id and isinstance(r.get("pass"), int)]
        pass_index = max(passes) + 1 if passes else 0
        runs = 0
        tests_by_name = {t["name"]: t for t in self.tests}
        checkers_by_name = {c["name"]: c for c in self.checkers}
        while True:
            current = current_records(self.store.records(), self.checker_hashes, self.test_shas)
            pending = pending_cells(self._cells(), current, self.expected, self.policy)
            if not pending:
                return self._finish("complete", runs)
            self.log(f"pass {pass_index}: {len(pending)} cells")
            for cell in plan_pass(pending, self.round_id, pass_index):
                if self.clock() >= deadline:
                    return self._finish("budget", runs)
                if self._sentinel_due() and not self._read_sentinel():
                    return self._finish("sentinel", runs)
                request = build_request(checkers_by_name[cell.checker], tests_by_name[cell.test], cell.lane,
                                        self.machine, env=self.env, round_id=self.round_id, pass_index=pass_index,
                                        seq=self.store.next_seq(), epoch=self.store.epoch)
                record = self.store.append(self.measure(request))
                runs += 1
                self.log(f"{record['checker']} {record['test']} {record['lane']}: {record['status']} "
                         f"{record.get('wall_s', 0):.3f} s{'' if record.get('valid') else ' (invalid)'}")
            pass_index += 1
