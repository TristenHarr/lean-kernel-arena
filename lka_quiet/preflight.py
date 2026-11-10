"""The quiet box's identity and readiness.

The fingerprint is every fact about the machine that can move a measurement: processor and
microcode, firmware, kernel and its command line, memory, clock policy, transparent huge pages,
swap, perf and the helper's version, the toolchain set. Its hash names the machine's epoch; runs
of different epochs never share a board. `check_state` is what a session verifies before it
starts and between batches: no load, no competitor, the clock pinned, no swap, not hot.
"""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

MAX_LOAD = 0.3
MAX_TEMPERATURE_C = 80.0
COMPETITOR_CPU_PERCENT = 5.0


def fingerprint(facts: dict) -> str:
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()


def epoch_of(machine: dict, facts: dict) -> str:
    return f"{machine['name']}-{fingerprint(facts)[:16]}"


def check_state(state: dict, machine: dict) -> list[str]:
    problems = []
    if state["load1"] > MAX_LOAD:
        problems.append(f"load {state['load1']:.2f} above {MAX_LOAD}")
    for name, percent in state["busy_processes"]:
        problems.append(f"competitor: {name} at {percent:.0f}% of a core")
    pinned_khz = (machine["frequency"]["pinned_hz"] or 0) / 1000
    cpus = sorted({c for cpus in machine["lanes"].values() for c in cpus})
    for cpu in cpus:
        governor = state["governors"].get(str(cpu))
        if governor != "performance":
            problems.append(f"governor of cpu{cpu} is {governor}, not performance")
        current = state["cur_khz"].get(str(cpu))
        if pinned_khz and current and abs(current / pinned_khz - 1) > machine["frequency"]["tolerance"]:
            problems.append(f"frequency of cpu{cpu} is {current / 1e6:.3f} GHz, pinned {pinned_khz / 1e6:.3f} GHz")
    if state["swap_bytes"]:
        problems.append(f"swap in use or enabled: {state['swap_bytes']} bytes")
    if state["temperature_c"] is not None and state["temperature_c"] > MAX_TEMPERATURE_C:
        problems.append(f"temperature {state['temperature_c']:.0f} C above {MAX_TEMPERATURE_C:.0f} C")
    return problems


# ---------------------------------------------------------------------------------------------
# Reading the live machine (on the box)


def _read(path, default="") -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _cpus() -> list[int]:
    return sorted(int(p.name[3:]) for p in Path("/sys/devices/system/cpu").glob("cpu[0-9]*"))


def _run(cmd) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def gather_facts() -> dict:
    cpuinfo = _read("/proc/cpuinfo")
    model = re.search(r"^model name\s*:\s*(.+)$", cpuinfo, re.M)
    microcode = re.search(r"^microcode\s*:\s*(.+)$", cpuinfo, re.M)
    base = "/sys/devices/system/cpu"
    cpus = _cpus()
    dmi = "/sys/class/dmi/id"
    memory = _run(["dmidecode", "-t", "memory"])
    sticks = re.findall(r"^\s*Size: (\d+ [MG]B)\s*$.*?^\s*Type: (\S+).*?^\s*Speed: ([^\n]+)$", memory, re.M | re.S)
    return {
        "cpu_model": model.group(1) if model else "",
        "microcode": microcode.group(1) if microcode else "",
        "kernel": os.uname().release,
        "cmdline": _read("/proc/cmdline"),
        "bios": f"{_read(f'{dmi}/bios_version')} {_read(f'{dmi}/bios_date')}",
        "memory": "; ".join(" ".join(s) for s in sticks) or _read("/proc/meminfo").splitlines()[0],
        "thp": re.sub(r".*\[(\w+)\].*", r"\1", _read("/sys/kernel/mm/transparent_hugepage/enabled")),
        "governor": {str(c): _read(f"{base}/cpu{c}/cpufreq/scaling_governor") for c in cpus},
        "scaling_min_khz": {str(c): int(_read(f"{base}/cpu{c}/cpufreq/scaling_min_freq", "0") or 0) for c in cpus},
        "scaling_max_khz": {str(c): int(_read(f"{base}/cpu{c}/cpufreq/scaling_max_freq", "0") or 0) for c in cpus},
        "no_turbo": int(_read(f"{base}/intel_pstate/no_turbo", "-1") or -1),
        "swap_bytes": sum(int(line.split()[2]) * 1024 for line in _read("/proc/swaps").splitlines()[1:] if line.split()),
        "perf_version": _run(["perf", "--version"]),
        "lka_helper": _read("/usr/local/libexec/lka/VERSION"),
        "nixpkgs": _read("/etc/lka/nixpkgs-rev"),
    }


def gather_state(sample_s: float = 2.0) -> dict:
    import time
    base = "/sys/devices/system/cpu"
    cpus = _cpus()

    def ticks():
        out = {}
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
                name = (proc / "comm").read_text().strip()
                out[int(proc.name)] = (name, int(fields[11]) + int(fields[12]))
            except (OSError, IndexError, ValueError):
                continue
        return out

    first = ticks()
    time.sleep(sample_s)
    second = ticks()
    hz = os.sysconf("SC_CLK_TCK")
    me = os.getpid()
    busy = []
    for pid, (name, total) in second.items():
        if pid == me or pid not in first:
            continue
        percent = (total - first[pid][1]) / hz / sample_s * 100
        if percent >= COMPETITOR_CPU_PERCENT:
            busy.append((name, percent))
    zones = [int(_read(p, "0") or 0) / 1000 for p in Path("/sys/class/thermal").glob("thermal_zone*/temp")]
    throttle = sum(int(_read(f"{base}/cpu{c}/thermal_throttle/core_throttle_count", "0") or 0) for c in cpus)
    return {
        "load1": os.getloadavg()[0],
        "busy_processes": sorted(busy, key=lambda b: -b[1]),
        "governors": {str(c): _read(f"{base}/cpu{c}/cpufreq/scaling_governor") for c in cpus},
        "cur_khz": {str(c): int(_read(f"{base}/cpu{c}/cpufreq/scaling_cur_freq", "0") or 0) for c in cpus},
        "swap_bytes": sum(int(line.split()[2]) * 1024 for line in _read("/proc/swaps").splitlines()[1:] if line.split()),
        "throttle_total": throttle,
        "temperature_c": max(zones) if zones else None,
    }
