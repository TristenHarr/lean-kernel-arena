"""The quiet state of the box, entered before a session and left after it.

Entering pins every CPU's clock to the calibrated frequency, sends every interrupt and every
system process to the housekeeping CPUs, holds the CPUs out of deep idle states, stops the timers
that wake up to do maintenance, and turns swap off. Everything is planned from a snapshot of the
machine taken first, and leaving plays the snapshot back, so the box ends exactly as it began.
The snapshot of the first entry is kept until the matching leave, so entering twice cannot lose it.

    quietbox/lka-quiet on|off|status      (root; the plan is lka_quiet.quiet.plan_enter/plan_leave)
"""

import json
import os
import subprocess
from pathlib import Path

CPU = "/sys/devices/system/cpu"
SNAPSHOT_FILE = Path("/run/lka/quiet-snapshot.json")
MAINTENANCE_TIMERS = (
    "apt-daily.timer", "apt-daily-upgrade.timer", "unattended-upgrades.service", "man-db.timer", "fstrim.timer",
    "e2scrub_all.timer", "logrotate.timer", "motd-news.timer", "fwupd-refresh.timer", "update-notifier-download.timer",
    "snapd.refresh.timer", "sysstat-collect.timer", "dpkg-db-backup.timer", "plocate-updatedb.timer",
)
CONFINED_UNITS = ("system.slice", "user.slice", "init.scope")


def cpu_mask(cpus) -> str:
    return format(sum(1 << c for c in cpus), "x")


def _write(path: str, value) -> dict:
    return {"kind": "write", "path": path, "value": str(value)}


def _run(*argv) -> dict:
    return {"kind": "run", "argv": list(argv)}


def plan_enter(snapshot: dict, machine: dict) -> list[dict]:
    pinned_khz = int(machine["frequency"]["pinned_hz"] // 1000)
    housekeeping = machine["housekeeping"]
    mask = cpu_mask(housekeeping)
    actions = []
    for cpu in snapshot["cpus"]:
        base = f"{CPU}/cpu{cpu}/cpufreq"
        actions.append(_write(f"{base}/scaling_governor", "performance"))
        actions.append(_write(f"{base}/energy_performance_preference", "performance"))
        actions.append(_write(f"{base}/scaling_max_freq", pinned_khz))
        actions.append(_write(f"{base}/scaling_min_freq", pinned_khz))
    for irq, current in sorted(snapshot["irq_affinity"].items(), key=lambda item: int(item[0])):
        if current != mask:
            actions.append(_write(f"/proc/irq/{irq}/smp_affinity", mask))
    actions.append(_write("/proc/irq/default_smp_affinity", mask))
    allowed = ",".join(str(c) for c in housekeeping)
    for unit in CONFINED_UNITS:
        actions.append(_run("systemctl", "set-property", "--runtime", unit, f"AllowedCPUs={allowed}"))
    for timer in snapshot["active_timers"]:
        actions.append(_run("systemctl", "stop", timer))
    for swap in snapshot["swaps"]:
        actions.append(_run("swapoff", swap))
    actions.append(_run("systemd-run", "--unit=lka-cpu-latency", "--quiet", "-p", f"AllowedCPUs={allowed}",
                        "/usr/bin/python3", "-c",
                        "import os,signal; fd=os.open('/dev/cpu_dma_latency', os.O_WRONLY); "
                        "os.write(fd, (0).to_bytes(4, 'little')); signal.pause()"))
    return actions


def plan_leave(snapshot: dict) -> list[dict]:
    actions = [_run("systemctl", "stop", "lka-cpu-latency")]
    for cpu in snapshot["cpus"]:
        base = f"{CPU}/cpu{cpu}/cpufreq"
        actions.append(_write(f"{base}/scaling_min_freq", snapshot["min_khz"][str(cpu)]))
        actions.append(_write(f"{base}/scaling_max_freq", snapshot["max_khz"][str(cpu)]))
        actions.append(_write(f"{base}/scaling_governor", snapshot["governor"][str(cpu)]))
        if snapshot["epp"].get(str(cpu)):
            actions.append(_write(f"{base}/energy_performance_preference", snapshot["epp"][str(cpu)]))
    for irq, value in sorted(snapshot["irq_affinity"].items(), key=lambda item: int(item[0])):
        actions.append(_write(f"/proc/irq/{irq}/smp_affinity", value))
    actions.append(_write("/proc/irq/default_smp_affinity", snapshot["default_irq_affinity"]))
    for unit in CONFINED_UNITS:
        actions.append(_run("systemctl", "set-property", "--runtime", unit, f"AllowedCPUs={snapshot['allowed_cpus'].get(unit, '')}"))
    for swap in snapshot["swaps"]:
        actions.append(_run("swapon", swap))
    for timer in snapshot["active_timers"]:
        actions.append(_run("systemctl", "start", timer))
    return actions


def snapshot_to_keep(*, existing, current):
    """The snapshot to restore at the end: the one taken before the first entry."""
    return existing if existing is not None else current


# ---------------------------------------------------------------------------------------------
# The live machine (root, on the box)


def _read(path: str, default: str = "") -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def take_snapshot() -> dict:
    cpus = sorted(int(p.name[3:]) for p in Path(CPU).glob("cpu[0-9]*") if (p / "cpufreq").exists())
    irqs = {}
    for directory in Path("/proc/irq").iterdir():
        if directory.name.isdigit() and (directory / "smp_affinity").exists():
            irqs[directory.name] = _read(str(directory / "smp_affinity"))
    active = subprocess.run(["systemctl", "list-units", "--state=active", "--no-legend", "--plain", *MAINTENANCE_TIMERS],
                            capture_output=True, text=True).stdout.split()
    allowed = {}
    for unit in CONFINED_UNITS:
        out = subprocess.run(["systemctl", "show", "-p", "AllowedCPUs", "--value", unit], capture_output=True, text=True)
        allowed[unit] = out.stdout.strip()
    return {
        "cpus": cpus,
        "governor": {str(c): _read(f"{CPU}/cpu{c}/cpufreq/scaling_governor") for c in cpus},
        "epp": {str(c): _read(f"{CPU}/cpu{c}/cpufreq/energy_performance_preference") for c in cpus},
        "min_khz": {str(c): int(_read(f"{CPU}/cpu{c}/cpufreq/scaling_min_freq", "0")) for c in cpus},
        "max_khz": {str(c): int(_read(f"{CPU}/cpu{c}/cpufreq/scaling_max_freq", "0")) for c in cpus},
        "irq_affinity": irqs,
        "default_irq_affinity": _read("/proc/irq/default_smp_affinity"),
        "allowed_cpus": allowed,
        "active_timers": [name for name in active if name in MAINTENANCE_TIMERS],
        "swaps": [line.split()[0] for line in _read("/proc/swaps").splitlines()[1:] if line.split()],
    }


def apply(actions) -> list[str]:
    """Carry out a plan; an interrupt the kernel will not move (a per-CPU one) is reported, not fatal."""
    problems = []
    for action in actions:
        if action["kind"] == "write":
            try:
                Path(action["path"]).write_text(action["value"])
            except OSError as error:
                problems.append(f"{action['path']}: {error.strerror}")
        else:
            result = subprocess.run(action["argv"], capture_output=True, text=True)
            if result.returncode != 0:
                problems.append(f"{' '.join(action['argv'][:4])}: {result.stderr.strip()}")
    return problems


def enter(machine: dict) -> list[str]:
    existing = json.loads(SNAPSHOT_FILE.read_text()) if SNAPSHOT_FILE.exists() else None
    snapshot = snapshot_to_keep(existing=existing, current=take_snapshot())
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(json.dumps(snapshot, indent=1))
    os.chmod(SNAPSHOT_FILE, 0o600)
    return apply(plan_enter(snapshot, machine))


def leave() -> list[str]:
    if not SNAPSHOT_FILE.exists():
        return ["not in the quiet state: no snapshot to restore"]
    snapshot = json.loads(SNAPSHOT_FILE.read_text())
    problems = apply(plan_leave(snapshot))
    SNAPSHOT_FILE.unlink()
    return problems
