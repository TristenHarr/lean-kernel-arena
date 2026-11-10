"""One measured run, as the board keeps it.

The measurement helper gathers raw observations (the shim's status, perf's counters, the run
cgroup's files, RAPL, throttle counters, per-CPU time and interrupts, process accounting);
`build_record` turns them into the record, decides its verdict, and checks that the run was
measured under the conditions the machine profile promises. A record with `valid: false` is
kept for the audit trail and repeated; it never reaches a median.
"""

from .perf import counters_from_perf, parse_perf_json, PerfParseError
from .sysfs import (
    crash_of,
    energy_delta_uj,
    lane_busy_jiffies,
    lane_interrupts,
    parse_acct_v3,
    parse_cpu_stat,
    parse_flat_keyed,
    parse_interrupts,
    parse_proc_stat,
)

RUN_SCHEMA = "lka-quiet-run/1"

OUTPUT_HEAD = 8192
OUTPUT_TAIL = 8192

_REQUEST_FIELDS = (
    "checker", "test", "lane", "expected", "timeout_s", "checker_hash", "test_sha256",
    "epoch", "round", "pass", "seq",
)


def status_of(exit_code, signal, timed_out: bool, crash, oom_kills: int) -> str:
    if timed_out:
        return "timeout"
    if crash is not None or oom_kills > 0 or signal is not None:
        return "error"
    return {0: "accepted", 1: "rejected", 2: "declined"}.get(exit_code, "error")


def correctness_of(status: str, expected: str | None) -> str:
    """The arena's verdict semantics (lka.py run_checker_on_test)."""
    if status == "declined":
        return "declined"
    if status in ("error", "timeout"):
        return "error"
    if expected == "either":
        return "either"
    if (expected == "accept" and status == "accepted") or (expected == "reject" and status == "rejected"):
        return "correct"
    return "incorrect"


def bound_text(text: str, head: int = OUTPUT_HEAD, tail: int = OUTPUT_TAIL) -> str:
    if len(text) <= head + tail:
        return text
    dropped = len(text) - head - tail
    return f"{text[:head]}\n[... {dropped} characters omitted ...]\n{text[-tail:]}"


def _frequency_problem(counters: dict, machine: dict) -> tuple[float | None, str | None]:
    cycles = [counters.get("cycles_u"), counters.get("cycles_k")]
    task_clock = counters.get("task_clock_s")
    if None in cycles or not task_clock:
        return None, None
    effective = sum(cycles) / task_clock
    frequency = machine.get("frequency", {})
    pinned = frequency.get("pinned_hz")
    if not pinned or task_clock < frequency.get("min_task_clock_s", 0.2):
        return effective, None
    deviation = effective / pinned - 1
    if abs(deviation) > frequency.get("tolerance", 0.005):
        return effective, (f"frequency: ran at {effective / 1e9:.4f} GHz, pinned {pinned / 1e9:.4f} GHz "
                           f"({deviation * 100:+.2f}%)")
    return effective, None


def _interference(obs: dict, lane_cpus: list[int], wall_s: float, run_usage_usec: int, machine: dict):
    """Milliseconds of CPU on the lane's CPUs that the run did not use, and whether that is too much."""
    interference = machine.get("interference", {})
    jiffy_ms = interference.get("jiffy_ms", 10)
    before = parse_proc_stat(obs["proc_stat_before"])
    after = parse_proc_stat(obs["proc_stat_after"])
    lane_ms = lane_busy_jiffies(before, after, lane_cpus) * jiffy_ms
    foreign_ms = max(0.0, lane_ms - run_usage_usec / 1000)
    allowed_ms = max(interference.get("floor_ms", 20),
                     interference.get("max_fraction", 0.005) * wall_s * 1000 * len(lane_cpus))
    problem = None
    if foreign_ms > allowed_ms:
        problem = (f"interference: {foreign_ms:.0f} ms of foreign CPU on lane CPUs {lane_cpus} "
                   f"(allowed {allowed_ms:.0f} ms)")
    return foreign_ms, problem


def build_record(request: dict, obs: dict, machine: dict) -> dict:
    problems: list[str] = []
    lane_cpus = machine["lanes"][request["lane"]]

    try:
        counters, perf_problems = counters_from_perf(parse_perf_json(obs.get("perf_text") or ""))
    except PerfParseError as error:
        counters, perf_problems = {}, [f"perf: {error}"]
    problems += perf_problems

    shim = obs.get("shim")
    timed_out = bool(obs.get("timed_out"))
    wall_s = shim["wall_ns"] / 1e9 if shim else float(obs["elapsed_s"])

    memory_events = parse_flat_keyed(obs.get("memory_events", ""))
    oom_kills = memory_events.get("oom_kill", 0)
    run_usage_usec = parse_cpu_stat(obs.get("cpu_stat", "")).get("usage_usec", 0)

    acct_records = parse_acct_v3(obs.get("acct", b""))
    sandbox_uid = machine["sandbox"]["uid"]
    crash = None if timed_out else crash_of(acct_records, sandbox_uid)
    exit_codes = sorted({r.exit_status for r in acct_records
                         if r.uid == sandbox_uid and r.exit_status not in (None, 0)})

    exit_code = shim.get("exit_code") if shim else None
    top_signal = shim.get("signal") if shim else None
    status = status_of(exit_code, top_signal, timed_out, crash, oom_kills)

    effective_hz, frequency_problem = _frequency_problem(counters, machine)
    if frequency_problem:
        problems.append(frequency_problem)

    throttle = obs.get("throttle") or {}
    core_throttles = throttle.get("core_after", 0) - throttle.get("core_before", 0)
    package_throttles = throttle.get("package_after", 0) - throttle.get("package_before", 0)
    if core_throttles or package_throttles:
        problems.append(f"thermal throttling: core +{core_throttles}, package +{package_throttles}")

    foreign_ms, interference_problem = _interference(obs, lane_cpus, wall_s, run_usage_usec, machine)
    if interference_problem:
        problems.append(interference_problem)

    lane_irqs = lane_interrupts(parse_interrupts(obs["interrupts_before"]),
                                parse_interrupts(obs["interrupts_after"]), lane_cpus)

    energy = obs.get("energy")
    energy_j = None
    if energy:
        energy_j = energy_delta_uj(energy["before_uj"], energy["after_uj"], energy["max_range_uj"]) / 1e6

    maxrss_kb = shim.get("maxrss_kb") if shim else None

    record = {"schema": RUN_SCHEMA}
    for key in _REQUEST_FIELDS:
        record[key] = request.get(key)
    record.update({
        "status": status,
        "correctness": correctness_of(status, request.get("expected")),
        "exit_code": exit_code,
        "signal": top_signal,
        "timed_out": timed_out,
        "crash": crash,
        "oom_kills": oom_kills,
        "descendant_exit_codes": exit_codes,
        "stragglers": obs.get("stragglers", 0),
        "wall_s": wall_s,
        "counters": counters,
        "cpu_usage_s": run_usage_usec / 1e6,
        "memory_peak_bytes": obs.get("memory_peak"),
        "maxrss_bytes": maxrss_kb * 1024 if maxrss_kb is not None else None,
        "energy_j": energy_j,
        "freq_effective_hz": effective_hz,
        "throttles": {"core": core_throttles, "package": package_throttles},
        "foreign_cpu_ms": foreign_ms,
        "lane_irqs": lane_irqs,
        "valid": not problems,
        "invalid_reasons": problems,
        "stdout": bound_text(obs.get("stdout") or ""),
        "stderr": bound_text(obs.get("stderr") or ""),
    })
    return record
