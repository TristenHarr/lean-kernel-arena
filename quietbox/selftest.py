#!/usr/bin/python3
"""The measurement chain's acceptance test: real runs through lka-measure, each held to what it
must report. Run as root on a provisioned quiet box (or in a privileged container):

    quietbox/selftest.py --setup            # make the selftest checker, its input and a machine profile
    quietbox/selftest.py [--no-systemd] [--expect-quiet]

Every case runs a small shell command as a checker would. The profile it measures under is the
machine's own (/etc/lka/machine.json when present) with small memory and process caps, so the
out-of-memory and fork cases trip quickly. Exit status: the number of failed cases.
"""

import argparse
import grp
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKER = "selftest"
STATE = Path("/srv/lka")
LOOP_C = r"""
#include <stdio.h>
int main(void) {
    volatile unsigned long long s = 0;
    for (unsigned long long i = 0; i < 200000000ULL; i++) s += i;
    printf("%llu\n", (unsigned long long)s);
    return 0;
}
"""


def user_name(prefix: str) -> str:
    return prefix + hashlib.sha256(CHECKER.encode()).hexdigest()[:12]


def base_machine() -> dict:
    installed = Path("/etc/lka/machine.json")
    if installed.exists():
        return json.loads(installed.read_text())
    cpus = sorted(os.sched_getaffinity(0))
    lane = cpus[-4:] if len(cpus) >= 5 else cpus
    rapl = "/sys/class/powercap/intel-rapl:0"
    return {
        "name": "selftest",
        "housekeeping": [cpus[0]],
        "lanes": {"4core": lane, "1core": [lane[-1]]},
        "frequency": {"pinned_hz": None, "tolerance": 0.005, "min_task_clock_s": 0.2},
        "memory": {"max_bytes": 16106127360, "pids_max": 32768},
        "interference": {"max_fraction": 0.005, "floor_ms": 20, "jiffy_ms": 10},
        "energy": {"rapl_domain": rapl if os.access(f"{rapl}/energy_uj", os.R_OK) else None},
        "sandbox": {"user_prefix": "lkc-", "homes": str(STATE / "checkers"), "tmp_bytes": 268435456,
                    "input_group": "lka-input"},
        "paths": {"state": str(STATE), "tests": str(STATE / "tests"), "checkers": str(STATE / "checkers"),
                  "results": str(STATE / "results"), "acct": "/var/log/lka/pacct",
                  "helper": "/usr/local/libexec/lka/lka-measure", "shim": "/usr/local/libexec/lka/lka-shim"},
    }


def selftest_machine() -> dict:
    machine = base_machine()
    machine = {**machine, "name": f"{machine.get('name', 'machine')}-selftest"}
    machine["memory"] = {"max_bytes": 512 * 2**20, "pids_max": 128}
    machine["sandbox"] = {**machine["sandbox"], "tmp_bytes": 256 * 2**20}
    return machine


def setup() -> None:
    machine = base_machine()
    prefix = machine["sandbox"]["user_prefix"]
    user = user_name(prefix)
    home = Path(machine["sandbox"]["homes"]) / CHECKER
    try:
        pwd.getpwnam(user)
    except KeyError:
        subprocess.run(["useradd", "--system", "--user-group", "--home-dir", str(home), "--no-create-home",
                        "--shell", "/usr/sbin/nologin", user], check=True)
    account = pwd.getpwnam(user)
    (home / "bin").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "loop.c"
        source.write_text(LOOP_C)
        subprocess.run(["gcc", "-O1", "-o", str(home / "bin" / "loop"), str(source)], check=True)
    for path in [home, *home.rglob("*")]:
        os.chown(path, account.pw_uid, account.pw_gid)
    home.chmod(0o750)
    group = machine["sandbox"]["input_group"]
    try:
        grp.getgrnam(group)
    except KeyError:
        subprocess.run(["groupadd", "--system", group], check=True)
    gid = grp.getgrnam(group).gr_gid
    tests = Path(machine["paths"]["tests"])
    tests.mkdir(parents=True, exist_ok=True)
    os.chown(tests, 0, gid)
    tests.chmod(0o2750)
    for name, text in (("selftest.ndjson", '{"meta": "selftest"}\n'), ("other.ndjson", '{"meta": "another input"}\n')):
        (tests / name).write_text(text)
        os.chown(tests / name, 0, gid)
        (tests / name).chmod(0o640)
    acct = Path(machine["paths"]["acct"])
    acct.parent.mkdir(parents=True, exist_ok=True)
    acct.touch(mode=0o600, exist_ok=True)
    if shutil.which("accton"):
        subprocess.run(["accton", str(acct)], check=True)
    print(f"selftest checker {user} at {home}; input {tests / 'selftest.ndjson'}")


def run(machine_path: Path, helper: list[str], cmd: str, lane: str = "4core", timeout: float = 30.0,
        expected: str = "accept") -> dict:
    machine = json.loads(machine_path.read_text())
    request = {
        "checker": CHECKER, "test": "selftest", "lane": lane, "expected": expected, "cmd": cmd,
        "workdir": str(Path(machine["sandbox"]["homes"]) / CHECKER),
        "input": str(Path(machine["paths"]["tests"]) / "selftest.ndjson"),
        "timeout_s": timeout, "env": {"PATH": "/usr/local/bin:/usr/bin:/bin"},
        "checker_hash": "selftest", "test_sha256": "selftest", "epoch": "selftest", "round": "selftest",
        "pass": 0, "seq": 0,
    }
    result = subprocess.run(helper, input=json.dumps(request), capture_output=True, text=True,
                            env={**os.environ, "LKA_MACHINE": str(machine_path)})
    if result.returncode != 0:
        raise RuntimeError(f"lka-measure failed ({result.returncode}): {result.stderr.strip()}")
    return json.loads(result.stdout)


def cases(env_bytes: int):
    """(name, command, lane, timeout, expected outcome, check(record) -> problem or None)."""
    status = lambda want: (lambda r: None if r["status"] == want else f"status {r['status']}, want {want}")
    return [
        ("exit 0 accepts", "exit 0", "4core", 30, "accept", status("accepted")),
        ("exit 1 rejects", "exit 1", "4core", 30, "reject", status("rejected")),
        ("exit 2 declines", "exit 2", "4core", 30, "accept", status("declined")),
        ("a crash behind `|| exit 1` is an error", "sh -c 'kill -SEGV $$' || exit 1", "4core", 30, "reject",
         lambda r: None if r["status"] == "error" and (r.get("crash") or {}).get("signal") == "SIGSEGV"
         else f"status {r['status']}, crash {r.get('crash')}" if r.get("crash_detection")
         else "process accounting is off, crash detection unavailable"),
        ("the top process killed by a signal is an error", "kill -ABRT $$", "4core", 30, "reject", status("error")),
        ("a timeout is a timeout with its metrics", "sleep 30", "4core", 1, "accept",
         lambda r: None if r["status"] == "timeout" and 0.9 < r["wall_s"] < 3 and r["memory_peak_bytes"]
         else f"status {r['status']}, wall {r['wall_s']}, memory {r['memory_peak_bytes']}"),
        ("exceeding the memory cap is an OOM error",
         "python3 -c 'b = bytearray(1 << 30); b[::4096] = b\"x\" * len(b[::4096])'", "4core", 60, "accept",
         lambda r: None if r["status"] == "error" and r["oom_kills"] >= 1 else f"status {r['status']}, oom {r['oom_kills']}"),
        ("no network", "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), 2)\"", "4core", 30,
         "reject", status("rejected")),
        ("loopback works", "python3 -c \"import socket; s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\"",
         "4core", 30, "accept", status("accepted")),
        ("not root, no new privileges",
         "test \"$(id -u)\" != 0 && grep -q '^NoNewPrivs:[[:space:]]*1' /proc/self/status", "4core", 30, "accept",
         status("accepted")),
        ("the staged tests are read-only", "touch \"$(dirname \"$IN\")/written\" 2>/dev/null && exit 0 || exit 1",
         "4core", 30, "reject", status("rejected")),
        ("the run reads its own input", "test \"$(cat \"$IN\")\" = '{\"meta\": \"selftest\"}'", "4core", 30, "accept",
         status("accepted")),
        ("no other staged input is visible", "test -e \"$(dirname \"$IN\")/other.ndjson\" && exit 0 || exit 1",
         "4core", 30, "reject", status("rejected")),
        ("the store shows the run's input alone", "test \"$(ls -A \"$(dirname \"$IN\")\")\" = selftest.ndjson", "4core", 30,
         "accept", status("accepted")),
        ("the input is read-only", "echo x >> \"$IN\" 2>/dev/null && exit 0 || exit 1", "4core", 30, "reject",
         status("rejected")),
        ("system paths are read-only", "touch /etc/lka-written 2>/dev/null && exit 0 || exit 1", "4core", 30,
         "reject", status("rejected")),
        ("home is writable", "echo hi > \"$HOME/marker\"", "4core", 30, "accept", status("accepted")),
        ("a run's writes are gone by the next run", "test -e \"$HOME/marker\" && exit 1 || exit 0", "4core", 30,
         "accept", status("accepted")),
        ("the process cap holds a fork bomb", "for i in $(seq 400); do sleep 5 & done; wait", "4core", 30, "accept",
         lambda r: None if r["status"] != "accepted" and r["wall_s"] < 20 else f"status {r['status']}, wall {r['wall_s']}"),
        ("one core in the 1-core lane", "test \"$(nproc)\" = 1 && test \"$THREADS\" = 1", "1core", 30, "accept",
         status("accepted")),
        ("the lane's cores in the 4-core lane", "test \"$(nproc)\" = \"$THREADS\"", "4core", 30, "accept",
         status("accepted")),
        ("the environment block has a fixed size", f"test \"$(wc -c < /proc/$$/environ)\" = {env_bytes}", "4core", 30,
         "accept", status("accepted")),
        ("wall time is the checker's own", "sleep 1", "1core", 30, "accept",
         lambda r: None if 1.0 <= r["wall_s"] < 1.02 else f"wall {r['wall_s']:.4f} s"),
        ("processes left behind are counted and removed", "(sleep 30 &); exit 0", "4core", 30, "accept",
         lambda r: None if r["stragglers"] >= 1 and r["status"] == "accepted" else f"stragglers {r['stragglers']}"),
        ("peak memory covers what was allocated",
         "python3 -c 'b = bytearray(200 << 20); b[::4096] = b\"x\" * len(b[::4096])'", "4core", 60, "accept",
         lambda r: None if r["memory_peak_bytes"] >= 200 << 20 else f"peak {r['memory_peak_bytes']}"),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--no-systemd", action="store_true")
    parser.add_argument("--expect-quiet", action="store_true", help="every run must also be valid")
    parser.add_argument("--helper", default="/usr/local/libexec/lka/lka-measure")
    args = parser.parse_args()
    if os.geteuid() != 0:
        print("selftest must run as root", file=sys.stderr)
        return 1
    if args.setup:
        setup()
        return 0

    sys.path.insert(0, str(HERE.parent))
    from lka_quiet.helper import ENV_BYTES

    helper = [args.helper] + (["--no-systemd"] if args.no_systemd else [])
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        machine_path = Path(tmp) / "machine.json"
        machine_path.write_text(json.dumps(selftest_machine()))
        for name, cmd, lane, timeout, expected, check in cases(ENV_BYTES):
            try:
                record = run(machine_path, helper, cmd, lane, timeout, expected)
                problem = check(record)
                if problem is None and args.expect_quiet and not record["valid"]:
                    problem = "invalid: " + "; ".join(record["invalid_reasons"])
            except Exception as error:
                problem = f"{type(error).__name__}: {error}"
            failures += problem is not None
            print(f"{'ok  ' if problem is None else 'FAIL'} {name}" + (f": {problem}" if problem else ""))

        machine = selftest_machine()
        user = user_name(machine["sandbox"]["user_prefix"])
        staged = Path(machine["paths"]["tests"]) / "selftest.ndjson"
        outside = subprocess.run(["setpriv", f"--reuid={user}", f"--regid={user}", "--init-groups", "--",
                                  "cat", str(staged)], capture_output=True)
        ok = outside.returncode != 0
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} a checker's user cannot read a staged input outside a run")

        loop = [run(machine_path, helper, "$HOME/bin/loop", "1core") for _ in range(5)]
        counts = [r["counters"]["instructions_u"] for r in loop]
        spread = (max(counts) - min(counts)) / min(counts) if all(counts) else float("inf")
        ok = spread < 0.001
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} user instructions repeat within 0.1%: {min(counts):,} .. {max(counts):,}")
        walls = [r["wall_s"] for r in loop]
        print(f"     loop wall times: {', '.join(f'{w:.4f}' for w in walls)} s")
        energy = [r["energy_j"] for r in loop]
        if all(e is not None for e in energy):
            print(f"     loop package energy: {', '.join(f'{e:.2f}' for e in energy)} J")
    print(f"{failures} failed")
    return failures


if __name__ == "__main__":
    sys.exit(main())
