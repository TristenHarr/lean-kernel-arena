"""The measurement helper: one run of one checker on one test, as root, on the quiet box.

`quietbox/lka-measure` is the only program the board's runner may start as root. It reads one
request (JSON on stdin), refuses anything outside the checker's own home and the staged tests,
and measures the run:

    systemd scope (Delegate=yes) in lka.slice
      helper/   this process and perf, on the housekeeping CPUs
      run/      the checker, on the lane's CPUs, under the memory, swap and process caps
                (entered by lka-shim, which also unshares network and mounts and drops root)

Before and after the run it reads RAPL, the thermal-throttle counters, per-CPU time and
interrupts, and the process-accounting file; afterwards the run cgroup's peak memory, OOM kills
and CPU use. `lka_quiet.record.build_record` turns those into the run record printed on stdout.
"""

import grp
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .perf import EVENTS
from .record import build_record

HELPER_VERSION = "1"

# Every run's environment block is padded to this many bytes: the size of the environment moves
# the initial stack, and with it the alignment of everything a program puts there.
ENV_BYTES = 16384

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_CHECKER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_FORBIDDEN_ENV = {"LD_PRELOAD", "LD_AUDIT", "LD_DEBUG", "LKA_PAD"}
_FIXED_ENV = ("IN", "LC_ALL", "THREADS", "HOME", "USER", "LOGNAME", "TMPDIR")


class HelperError(ValueError):
    pass


def checker_user(name: str, prefix: str) -> str:
    return prefix + hashlib.sha256(name.encode()).hexdigest()[:12]


def checker_home(name: str, machine: dict) -> str:
    return os.path.join(machine["sandbox"]["homes"], name)


def _inside(path: str, root: str) -> bool:
    root = root.rstrip("/")
    return path == root or path.startswith(root + "/")


def validate_request(req: dict, machine: dict) -> None:
    name = req.get("checker")
    if not isinstance(name, str) or not _CHECKER_NAME.match(name) or ".." in name:
        raise HelperError(f"checker name {name!r} is not a plain name")
    if req.get("lane") not in machine["lanes"]:
        raise HelperError(f"lane {req.get('lane')!r} is not one of {sorted(machine['lanes'])}")
    workdir = req.get("workdir")
    if not isinstance(workdir, str) or not _inside(os.path.normpath(workdir), checker_home(name, machine)):
        raise HelperError(f"workdir {workdir!r} is outside the checker's home")
    test_input = req.get("input")
    tests_root = os.path.realpath(machine["paths"]["tests"])
    if not isinstance(test_input, str) or not _inside(os.path.realpath(test_input), tests_root):
        raise HelperError(f"input {test_input!r} is not a staged test")
    cmd = req.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        raise HelperError("cmd must be a non-empty string")
    timeout = req.get("timeout_s")
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise HelperError(f"timeout_s {timeout!r} must be positive")
    env = req.get("env", {})
    if not isinstance(env, dict):
        raise HelperError("env must be an object")
    for key, value in env.items():
        if not isinstance(key, str) or not _ENV_NAME.match(key) or key in _FORBIDDEN_ENV:
            raise HelperError(f"env name {key!r} is not allowed")
        if not isinstance(value, str) or "\n" in value or "\0" in value:
            raise HelperError(f"env value of {key} must be one line of text")


def canonical_env(req: dict, machine: dict, lane_cpus, user: str) -> list[tuple[str, str]]:
    """The run's whole environment, sorted, padded to ENV_BYTES."""
    env = {k: v for k, v in req.get("env", {}).items() if k not in _FIXED_ENV}
    env.update({
        "IN": req["input"],
        "LC_ALL": "C",
        "THREADS": str(len(lane_cpus)),
        "HOME": checker_home(req["checker"], machine),
        "USER": user,
        "LOGNAME": user,
        "TMPDIR": "/tmp",
    })
    entries = sorted(env.items())
    used = sum(len(f"{k}={v}".encode()) + 1 for k, v in entries)
    pad = ENV_BYTES - used - len("LKA_PAD=") - 1
    if pad < 0:
        raise HelperError(f"the environment needs {used} bytes, more than {ENV_BYTES}")
    entries.append(("LKA_PAD", "x" * pad))
    return entries


def env_file_text(entries) -> str:
    return "".join(f"{k}={v}\n" for k, v in entries)


def perf_command(out: str, ctl: str, ack: str) -> list[str]:
    return ["perf", "stat", "-j", "-o", out, "-D", "-1", "--control", f"fifo:{ctl},{ack}",
            "-e", ",".join(EVENTS), "--"]


def shim_command(shim, *, cgroup, uid, gid, workdir, env_path, tmp_bytes, overlay, ctl, ack, status, run,
                 input=None, input_gid=None, hide=None) -> list[str]:
    """The shim's command line; with `input`, `input_gid` and `hide` (the shim refuses to run
    without them) the run sees the staged inputs under `hide` as `input` alone."""
    isolation = []
    if input is not None:
        isolation = ["--input", input, "--input-gid", str(input_gid), "--hide", hide]
    return [shim, "--cgroup", str(cgroup), "--uid", str(uid), "--gid", str(gid), "--workdir", workdir,
            "--env", env_path, "--tmp-bytes", str(tmp_bytes), "--overlay", overlay,
            "--ctl", ctl, "--ack", ack, "--status", status, *isolation, "--", "/bin/sh", "-c", run]


def cpu_list(cpus) -> str:
    cpus = sorted(set(cpus))
    ranges, start, prev = [], cpus[0], cpus[0]
    for cpu in cpus[1:] + [None]:
        if cpu is not None and cpu == prev + 1:
            prev = cpu
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        if cpu is not None:
            start = prev = cpu
    return ",".join(ranges)


def configure_run_cgroup(path: Path, *, cpus, memory_bytes: int, pids: int) -> None:
    (path / "cpuset.cpus").write_text(cpu_list(cpus))
    (path / "cpuset.mems").write_text("0")
    (path / "memory.max").write_text(str(memory_bytes))
    (path / "memory.swap.max").write_text("0")
    (path / "memory.oom.group").write_text("1")
    (path / "pids.max").write_text(str(pids))


# ---------------------------------------------------------------------------------------------
# Root only: the run itself


CGROUP_ROOT = Path("/sys/fs/cgroup")


def _own_cgroup() -> Path:
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            return CGROUP_ROOT / line[3:].lstrip("/")
    raise HelperError("not on the unified cgroup hierarchy")


def _read(path, default=""):
    try:
        return Path(path).read_text()
    except OSError:
        return default


def _read_int(path) -> int | None:
    text = _read(path).strip()
    return int(text) if text.lstrip("-").isdigit() else None


def _throttle_counts(cpus) -> dict:
    base = Path("/sys/devices/system/cpu")
    core = sum(_read_int(base / f"cpu{c}/thermal_throttle/core_throttle_count") or 0 for c in cpus)
    package = _read_int(base / f"cpu{cpus[0]}/thermal_throttle/package_throttle_count") or 0
    return {"core": core, "package": package}


def _energy_uj(machine: dict) -> int | None:
    domain = machine.get("energy", {}).get("rapl_domain")
    return _read_int(Path(domain) / "energy_uj") if domain else None


def _warm(path: str) -> None:
    with open(path, "rb", buffering=0) as f:
        while f.read(1 << 22):
            pass


class _Drain:
    """Reads a pipe to its end on a thread, keeping its head and its tail."""

    def __init__(self, pipe, head: int = 1 << 16, tail: int = 1 << 16):
        import threading
        self.head, self.tail, self.dropped = bytearray(), bytearray(), 0
        self.limits = (head, tail)
        self.thread = threading.Thread(target=self._read, args=(pipe,), daemon=True)
        self.thread.start()

    def _read(self, pipe):
        head_limit, tail_limit = self.limits
        with pipe:
            for chunk in iter(lambda: pipe.read1(1 << 16), b""):
                room = head_limit - len(self.head)
                if room > 0:
                    self.head += chunk[:room]
                    chunk = chunk[room:]
                self.tail += chunk
                if len(self.tail) > tail_limit:
                    self.dropped += len(self.tail) - tail_limit
                    del self.tail[:len(self.tail) - tail_limit]

    def text(self, wait_s: float = 30.0) -> str:
        self.thread.join(wait_s)
        middle = f"\n[... {self.dropped} bytes omitted ...]\n" if self.dropped else ""
        return self.head.decode(errors="replace") + middle + self.tail.decode(errors="replace")


def _populated(cgroup: Path) -> bool:
    for line in _read(cgroup / "cgroup.events").splitlines():
        if line.startswith("populated "):
            return line.split()[1] == "1"
    return False


def _kill_and_drain(cgroup: Path, deadline_s: float = 30.0) -> None:
    (cgroup / "cgroup.kill").write_text("1")
    end = time.monotonic() + deadline_s
    while _populated(cgroup) and time.monotonic() < end:
        time.sleep(0.01)


_CONTROLLERS = "+cpuset +memory +pids +cpu"


def enter_private_cgroup() -> Path:
    """Where systemd is not PID 1 (a container), make this process's own subtree under lka/."""
    lka = CGROUP_ROOT / "lka"
    (CGROUP_ROOT / "cgroup.subtree_control").write_text(_CONTROLLERS)
    lka.mkdir(exist_ok=True)
    (lka / "cgroup.subtree_control").write_text(_CONTROLLERS)
    scope = lka / f"run-{os.getpid()}"
    scope.mkdir()
    (scope / "cgroup.procs").write_text(str(os.getpid()))
    return scope


def leave_private_cgroup(scope: Path) -> None:
    idle = CGROUP_ROOT / "lka-idle"
    idle.mkdir(exist_ok=True)
    (idle / "cgroup.procs").write_text(str(os.getpid()))
    for child in (scope / "helper", scope / "run"):
        if child.exists():
            child.rmdir()
    scope.rmdir()


def measured_command(req: dict, machine: dict, *, uid: int, gid: int, input_gid: int, run_cg: Path,
                     run_dir: Path) -> list[str]:
    """perf, counting under control, around the shim around the checker's command: the run in its
    cgroup as the checker's user, its home behind an overlay, and its own input alone in view."""
    return perf_command(str(run_dir / "perf.json"), str(run_dir / "ctl"), str(run_dir / "ack")) + shim_command(
        machine["paths"]["shim"], cgroup=run_cg, uid=uid, gid=gid, workdir=req["workdir"],
        env_path=str(run_dir / "env"), tmp_bytes=machine["sandbox"]["tmp_bytes"],
        overlay=checker_home(req["checker"], machine), ctl=str(run_dir / "ctl"), ack=str(run_dir / "ack"),
        status=str(run_dir / "status.json"), run=req["cmd"], input=req["input"], input_gid=input_gid,
        hide=machine["paths"]["tests"])


def measure_inside_scope(req: dict, machine: dict, scope: Path | None = None) -> dict:
    """Run one measurement; this process is alone in `scope`, a cgroup delegated to it."""
    import pwd

    validate_request(req, machine)
    lane_cpus = machine["lanes"][req["lane"]]
    user = checker_user(req["checker"], machine["sandbox"]["user_prefix"])
    account = pwd.getpwnam(user)

    scope = scope or _own_cgroup()
    helper_cg, run_cg = scope / "helper", scope / "run"
    helper_cg.mkdir(exist_ok=True)
    (helper_cg / "cgroup.procs").write_text(str(os.getpid()))
    (scope / "cgroup.subtree_control").write_text(_CONTROLLERS)
    (helper_cg / "cpuset.cpus").write_text(cpu_list(machine["housekeeping"]))
    run_cg.mkdir()
    configure_run_cgroup(run_cg, cpus=lane_cpus, memory_bytes=machine["memory"]["max_bytes"],
                         pids=machine["memory"]["pids_max"])

    run_dir = Path(machine["paths"]["state"]) / "run" / f"{os.getpid()}-{time.monotonic_ns()}"
    run_dir.mkdir(parents=True, mode=0o700)
    ctl, ack = run_dir / "ctl", run_dir / "ack"
    os.mkfifo(ctl, 0o600)
    os.mkfifo(ack, 0o600)
    env_path, status_path, perf_path = run_dir / "env", run_dir / "status.json", run_dir / "perf.json"
    env_path.write_text(env_file_text(canonical_env(req, machine, lane_cpus, user)))

    _warm(req["input"])
    acct_path = Path(machine["paths"]["acct"])
    acct_offset = acct_path.stat().st_size if acct_path.exists() else None

    command = measured_command(req, machine, uid=account.pw_uid, gid=account.pw_gid,
                               input_gid=grp.getgrnam(machine["sandbox"]["input_group"]).gr_gid,
                               run_cg=run_cg, run_dir=run_dir)

    before = {
        "energy": _energy_uj(machine),
        "throttle": _throttle_counts(lane_cpus),
        "proc_stat": _read("/proc/stat"),
        "interrupts": _read("/proc/interrupts"),
    }
    started = time.monotonic()
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    readers = [_Drain(proc.stdout), _Drain(proc.stderr)]
    # perf exits when the shim does; a process the checker left behind may still hold the output
    # pipes, so the run ends with perf, never with the pipes.
    timed_out = False
    try:
        proc.wait(timeout=req["timeout_s"])
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_and_drain(run_cg)
        proc.wait()
    elapsed = time.monotonic() - started
    after = {
        "energy": _energy_uj(machine),
        "throttle": _throttle_counts(lane_cpus),
        "proc_stat": _read("/proc/stat"),
        "interrupts": _read("/proc/interrupts"),
    }

    stragglers = 0
    if _populated(run_cg):
        stragglers = len(_read(run_cg / "cgroup.procs").split())
        _kill_and_drain(run_cg)
    stdout, stderr = (reader.text() for reader in readers)

    shim = None
    if not timed_out and status_path.exists() and status_path.stat().st_size:
        shim = json.loads(status_path.read_text())

    acct = b""
    if acct_offset is not None and acct_path.exists():
        with open(acct_path, "rb") as f:
            # a rotation during the run leaves a shorter, new file: then all of it is the run's
            f.seek(acct_offset if acct_path.stat().st_size >= acct_offset else 0)
            acct = f.read()

    max_range = _read_int(Path(machine["energy"]["rapl_domain"]) / "max_energy_range_uj") \
        if machine.get("energy", {}).get("rapl_domain") else None
    energy = None
    if before["energy"] is not None and after["energy"] is not None and max_range:
        energy = {"before_uj": before["energy"], "after_uj": after["energy"], "max_range_uj": max_range}

    observation = {
        "shim": shim,
        "timed_out": timed_out,
        "elapsed_s": elapsed,
        "perf_text": _read(perf_path),
        "memory_peak": _read_int(run_cg / "memory.peak"),
        "memory_events": _read(run_cg / "memory.events"),
        "cpu_stat": _read(run_cg / "cpu.stat"),
        "stragglers": stragglers,
        "energy": energy,
        "throttle": {"core_before": before["throttle"]["core"], "core_after": after["throttle"]["core"],
                     "package_before": before["throttle"]["package"], "package_after": after["throttle"]["package"]},
        "proc_stat_before": before["proc_stat"],
        "proc_stat_after": after["proc_stat"],
        "interrupts_before": before["interrupts"],
        "interrupts_after": after["interrupts"],
        "acct": acct,
        "stdout": stdout,
        "stderr": stderr,
    }
    record = build_record(req, observation, {**machine, "sandbox": {**machine["sandbox"], "uid": account.pw_uid}})
    record.update({
        "machine": machine["name"],
        "lane_cpus": lane_cpus,
        "helper": HELPER_VERSION,
        "crash_detection": acct_offset is not None,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })

    run_cg.rmdir()
    shutil.rmtree(run_dir, ignore_errors=True)
    return record


def main(argv: list[str]) -> int:
    """`lka-measure [--no-systemd]`: one request on stdin, one run record on stdout; `--build` builds a
    checker and `--remove` removes a pull request's checker (see build.py).

    Under systemd it re-runs itself inside a fresh delegated scope in lka.slice; with
    --no-systemd it makes its own cgroup subtree instead."""
    if os.geteuid() != 0:
        print("lka-measure must run as root", file=sys.stderr)
        return 2
    machine_path = os.environ.get("LKA_MACHINE", "/etc/lka/machine.json")
    if "--remove" in argv:
        from .build import BuildError, remove_checker
        try:
            record = remove_checker(json.loads(sys.stdin.read()), json.loads(Path(machine_path).read_text()))
        except (BuildError, json.JSONDecodeError, KeyError) as error:
            print(f"lka-measure --remove: {error}", file=sys.stderr)
            return 2
        sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
        return 0 if record["ok"] else 1
    if "--build" in argv:
        from .build import BuildError, run_build
        try:
            record = run_build(json.loads(sys.stdin.read()), json.loads(Path(machine_path).read_text()))
        except (BuildError, json.JSONDecodeError, KeyError) as error:
            print(f"lka-measure --build: {error}", file=sys.stderr)
            return 2
        sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
        return 0 if record["ok"] else 1
    no_systemd = "--no-systemd" in argv
    if not no_systemd and "--inside" not in argv:
        scope = ["systemd-run", "--scope", "--quiet", "--collect", "--slice=lka.slice", "-p", "Delegate=yes",
                 "--setenv", f"LKA_MACHINE={machine_path}", "--", sys.executable, os.path.abspath(argv[0]), "--inside"]
        return subprocess.run(scope).returncode
    scope = None
    try:
        machine = json.loads(Path(machine_path).read_text())
        request = json.loads(sys.stdin.read())
        if no_systemd:
            scope = enter_private_cgroup()
        record = measure_inside_scope(request, machine, scope)
    except (HelperError, json.JSONDecodeError, KeyError) as error:
        print(f"lka-measure: {error}", file=sys.stderr)
        return 2
    finally:
        if scope is not None:
            leave_private_cgroup(scope)
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    return 0
