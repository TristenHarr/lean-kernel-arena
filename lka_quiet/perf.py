"""`perf stat -j` output, read into the counters a run record carries.

Each JSON line names one event. On hybrid CPUs every hardware event appears once per PMU
(`cpu_core/instructions:u/`, `cpu_atom/instructions:u/`), and each PMU counts only while a task
runs on its core type. perf reports such a count scaled up by enabled/running time, as if it were
a sample; the true count is value * running / enabled, where running is the line's
`event-runtime` and enabled is the tasks' run time (task-clock's own runtime). The true total of
an event is the sum over its PMUs, and its coverage, the sum of running / enabled, is 1 whenever
the hardware counted all the time the tasks ran. Coverage below 1 means the counter shared its
slot with others (multiplexing): its value is a scaled estimate, and the board never keeps one.
`pcnt-running` is printed truncated to whole percent, so the exact runtimes are used instead.
"""

import json
from dataclasses import dataclass

# The events the measurement helper asks perf for, in the order it asks.
EVENTS = (
    "task-clock",
    "instructions:u",
    "instructions:k",
    "cycles:u",
    "cycles:k",
    "context-switches",
    "cpu-migrations",
    "page-faults",
    "major-faults",
)

# Run-record field for each event.
COUNTERS = {
    "task_clock_s": "task-clock",
    "instructions_u": "instructions:u",
    "instructions_k": "instructions:k",
    "cycles_u": "cycles:u",
    "cycles_k": "cycles:k",
    "context_switches": "context-switches",
    "cpu_migrations": "cpu-migrations",
    "page_faults": "page-faults",
    "major_faults": "major-faults",
}

_UNIT_SECONDS = {"ns": 1e-9, "": 1e-9, "usec": 1e-6, "msec": 1e-3, "sec": 1.0}
_TIME_EVENTS = ("duration_time", "task-clock")
# Runtimes are integer nanoseconds per PMU; their sum may fall a hair short of task-clock's.
_FULL_COVERAGE = 0.9999


class PerfParseError(ValueError):
    pass


@dataclass
class Event:
    value: float
    running_fraction: float


def _event_name(raw: str) -> str:
    """`cpu_core/instructions:u/` -> `instructions:u`; plain names pass through."""
    if "/" in raw:
        parts = [p for p in raw.split("/") if p]
        if len(parts) >= 2:
            return parts[1]
    return raw


def parse_perf_json(text: str) -> dict[str, Event]:
    lines: list[tuple[str, float, float, float]] = []
    seen_any = False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "event" not in record or "counter-value" not in record:
            continue
        seen_any = True
        try:
            value = float(record["counter-value"])
        except (TypeError, ValueError):
            continue
        name = _event_name(record["event"])
        if name in _TIME_EVENTS:
            value *= _UNIT_SECONDS.get(record.get("unit", ""), 1e-9)
        runtime = float(record.get("event-runtime", 0) or 0)
        percent = float(record.get("pcnt-running", 100.0))
        lines.append((name, value, runtime, percent))
    if not seen_any:
        raise PerfParseError("perf wrote no event records")

    enabled = next((runtime for name, _, runtime, _ in lines if name == "task-clock" and runtime > 0), None)
    raw: dict[str, float] = {}
    coverage: dict[str, float] = {}
    for name, value, runtime, percent in lines:
        if name in _TIME_EVENTS:
            fraction = 1.0
        elif enabled:
            fraction = min(runtime / enabled, 1.0)
        else:
            fraction = percent / 100.0
        raw[name] = raw.get(name, 0.0) + value * fraction
        coverage[name] = coverage.get(name, 0.0) + fraction

    events: dict[str, Event] = {}
    for name, total in raw.items():
        covered = min(coverage[name], 1.0)
        if covered >= _FULL_COVERAGE:
            events[name] = Event(total, 1.0)
        elif covered > 0:
            events[name] = Event(total / covered, covered)
    return events


def counters_from_perf(events: dict[str, Event]) -> tuple[dict, list[str]]:
    """The run record's counters and the reasons, if any, they cannot be trusted."""
    counters: dict = {}
    problems: list[str] = []
    for field, event_name in COUNTERS.items():
        event = events.get(event_name)
        if event is None:
            counters[field] = None
            problems.append(f"not counted: {event_name}")
            continue
        if event.running_fraction < 1.0:
            problems.append(f"multiplexed: {event_name} ran {event.running_fraction * 100:.1f}% of the time")
        counters[field] = event.value if field == "task_clock_s" else int(round(event.value))
    return counters, problems
