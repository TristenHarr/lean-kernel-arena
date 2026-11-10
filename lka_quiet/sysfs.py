"""Parsers for the kernel interfaces a measured run is read from.

Every parser takes the text (or bytes) of one file, so it is tested without a kernel; the
measurement helper reads the files and hands them over.
"""

import signal
import struct
from dataclasses import dataclass

# struct acct_v3 from <linux/acct.h>: flag, version, tty, exitcode, uid, gid, pid, ppid, btime,
# etime (float), eight comp_t counters, comm[16]; native little-endian on x86-64.
ACCT_V3 = "<BBHIIIIIIf8H16s"
_ACCT_SIZE = struct.calcsize(ACCT_V3)
_AXSIG = 0x10
_ACORE = 0x08

# Signals that end a process because it failed, as opposed to being stopped (SIGKILL by the
# timeout or the OOM killer, SIGTERM/SIGINT/SIGHUP/SIGPIPE by its own parent or pipeline).
CRASH_SIGNALS = frozenset({
    signal.SIGSEGV, signal.SIGBUS, signal.SIGILL, signal.SIGFPE,
    signal.SIGABRT, signal.SIGSYS, signal.SIGTRAP,
})


def parse_flat_keyed(text: str) -> dict[str, int]:
    """`key value` lines (memory.events, cpu.stat, ...)."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            out[parts[0]] = int(parts[1])
        except ValueError:
            continue
    return out


parse_cpu_stat = parse_flat_keyed


def energy_delta_uj(before: int, after: int, max_range_uj: int) -> int:
    """Microjoules between two RAPL readings; the counter wraps at max_energy_range_uj."""
    if after >= before:
        return after - before
    return after + max_range_uj - before


def parse_proc_stat(text: str) -> dict[int, tuple[int, int]]:
    """Per-CPU (busy, total) jiffies from /proc/stat; busy excludes idle and iowait."""
    cpus: dict[int, tuple[int, int]] = {}
    for line in text.splitlines():
        if not line.startswith("cpu") or line.startswith("cpu "):
            continue
        fields = line.split()
        try:
            cpu = int(fields[0][3:])
            values = [int(v) for v in fields[1:]]
        except ValueError:
            continue
        values += [0] * (8 - len(values))
        user, nice, system, idle, iowait, irq, softirq, steal = values[:8]
        busy = user + nice + system + irq + softirq + steal
        cpus[cpu] = (busy, busy + idle + iowait)
    return cpus


def lane_busy_jiffies(before: dict, after: dict, cpus) -> int:
    return sum(after[c][0] - before[c][0] for c in cpus)


def parse_interrupts(text: str) -> dict[int, int]:
    """Total interrupts per CPU column of /proc/interrupts; global single-count rows are skipped."""
    lines = text.splitlines()
    if not lines:
        return {}
    header = lines[0].split()
    cpus = [int(name[3:]) for name in header if name.startswith("CPU")]
    totals = {cpu: 0 for cpu in cpus}
    for line in lines[1:]:
        if ":" not in line:
            continue
        counts = []
        for token in line.split(":", 1)[1].split():
            if not token.isdigit():
                break
            counts.append(int(token))
        if len(counts) != len(cpus):
            continue
        for cpu, count in zip(cpus, counts):
            totals[cpu] += count
    return totals


def lane_interrupts(before: dict, after: dict, cpus) -> int:
    return sum(after[c] - before[c] for c in cpus)


@dataclass(frozen=True)
class AcctRecord:
    uid: int
    gid: int
    pid: int
    ppid: int
    btime: int
    comm: str
    exit_status: int | None
    signal: int | None
    core_dumped: bool


def parse_acct_v3(blob: bytes) -> list[AcctRecord]:
    """BSD process accounting records (version 3); a torn record at the end is left for later."""
    records = []
    for offset in range(0, len(blob) - _ACCT_SIZE + 1, _ACCT_SIZE):
        fields = struct.unpack_from(ACCT_V3, blob, offset)
        flag, version, _tty, exitcode, uid, gid, pid, ppid, btime, _etime = fields[:10]
        comm = fields[18]
        if version & 0x7F != 3:
            raise ValueError(f"process accounting record version {version & 0x7F}, expected 3")
        killed = bool(flag & _AXSIG) or (exitcode & 0x7F) != 0
        records.append(AcctRecord(
            uid=uid,
            gid=gid,
            pid=pid,
            ppid=ppid,
            btime=btime,
            comm=comm.split(b"\0", 1)[0].decode(errors="replace"),
            exit_status=None if killed else (exitcode >> 8) & 0xFF,
            signal=(exitcode & 0x7F) if killed else None,
            core_dumped=bool(flag & _ACORE) or bool(exitcode & 0x80),
        ))
    return records


def crash_of(records, sandbox_uid: int) -> dict | None:
    """The first process of the run that died of a failure signal, if any."""
    for record in records:
        if record.uid != sandbox_uid or record.signal is None:
            continue
        if record.signal in CRASH_SIGNALS:
            return {
                "kind": "signal",
                "signal": signal.Signals(record.signal).name,
                "pid": record.pid,
                "comm": record.comm,
            }
    return None
