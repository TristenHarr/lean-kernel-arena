#!/usr/bin/python3
"""Calibrate the quiet box: measure what its profile leaves null, and write it in.

    sudo quietbox/calibrate.py soak     machines/n100-quiet.yaml [--minutes 10] [--confirm-minutes 120]
    sudo quietbox/calibrate.py sentinel machines/n100-quiet.yaml --pinned-khz K --manifest M [--runs 30]
    sudo quietbox/calibrate.py box      machines/n100-quiet.yaml --pinned-khz K --manifest M --published results.json
    sudo quietbox/calibrate.py noise    machines/n100-quiet.yaml --label stock --manifest M [--runs 30] [--checker official --test init]
    sudo quietbox/calibrate.py write    machines/n100-quiet.yaml --report /srv/lka/calibration/report.json

Each step appends what it measured to /srv/lka/calibration/; `write` puts the decisions into the
profile (comments kept), which is then committed, so the conditions every run is held to are public.
The decisions themselves are lka_quiet/calibration.py's.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from lka_quiet import calibration, quiet  # noqa: E402
from lka_quiet.preflight import gather_state  # noqa: E402
from lka_quiet.session import build_request  # noqa: E402

OUT = Path("/srv/lka/calibration")
HELPER = "/usr/local/libexec/lka/lka-measure"
HEAT_C = r"""
#include <stdio.h>
int main(void) {
    double a[16], b = 1.0000001, c = 0.9999999;
    for (int i = 0; i < 16; i++) a[i] = i;
    for (unsigned long long n = 0;; n++) {
        for (int i = 0; i < 16; i++) a[i] = a[i] * b + c;
        if (n == 0xFFFFFFFFFFFFFFFFULL) printf("%f\n", a[0]);
    }
}
"""


def load_machine(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text())


def record(name: str, data) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    print(f"recorded {path}")
    return path


def cpus_of(machine) -> list[int]:
    return sorted({c for cpus in machine["lanes"].values() for c in cpus})


def read_khz(cpu: int) -> int:
    return int(Path(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq").read_text())


def soak_once(machine, freq_khz: int, minutes: float, heat: str) -> dict:
    pinned = {**machine, "frequency": {**machine["frequency"], "pinned_hz": freq_khz * 1000}}
    problems = quiet.enter(pinned)
    workers = [subprocess.Popen(["taskset", "-c", str(c), heat]) for c in cpus_of(machine)]
    before = gather_state(0.1)["throttle_total"]
    effective, temps = [], []
    try:
        end = time.monotonic() + minutes * 60
        while time.monotonic() < end:
            time.sleep(5)
            effective += [read_khz(c) for c in cpus_of(machine)]
            temps.append(gather_state(0.1)["temperature_c"] or 0)
    finally:
        for w in workers:
            w.kill()
        quiet.leave()
    after = gather_state(0.1)["throttle_total"]
    return {"freq_khz": freq_khz, "minutes": minutes, "throttle_events": after - before,
            "max_temperature_c": max(temps or [0]), "effective_khz": effective, "quiet_problems": problems}


def cmd_soak(args) -> int:
    machine = load_machine(args.machine)
    top = int(Path("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq").read_text())
    bottom = int(Path("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        heat = Path(tmp) / "heat"
        (Path(tmp) / "heat.c").write_text(HEAT_C)
        subprocess.run(["gcc", "-O2", "-march=native", "-o", str(heat), str(Path(tmp) / "heat.c")], check=True)
        results = []
        freq = top - top % 100000
        while freq >= max(bottom, 1000000):
            result = soak_once(machine, freq, args.minutes, str(heat))
            results.append(result)
            print(f"{freq / 1e6:.1f} GHz: {result['throttle_events']} throttle events, {result['max_temperature_c']:.0f} C")
            try:
                chosen = calibration.choose_frequency([result], args.max_temperature)
                confirm = soak_once(machine, chosen, args.confirm_minutes, str(heat))
                results.append(confirm)
                calibration.choose_frequency([confirm], args.max_temperature)
                record("soak", {"chosen_khz": chosen, "soaks": results})
                print(f"pinned clock: {chosen / 1e6:.1f} GHz, held for {args.confirm_minutes} min")
                return 0
            except ValueError:
                freq -= 100000
    record("soak", {"chosen_khz": None, "soaks": results})
    print("no frequency held: improve cooling or lower the power limit", file=sys.stderr)
    return 1


def _measure(machine_json: Path, request: dict) -> dict:
    result = subprocess.run([HELPER], input=json.dumps(request), capture_output=True, text=True,
                            env={**os.environ, "LKA_MACHINE": str(machine_json)})
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def _checker(name: str) -> dict:
    data = yaml.safe_load((ROOT / "checkers" / f"{name}.yaml").read_text())
    return {**data, "name": name, "source_hash": "calibration"}


def _runs(args, machine, checker_name, test_name, lane, n, label):
    pinned = {**machine, "frequency": {**machine["frequency"], "pinned_hz": args.pinned_khz * 1000}}
    if pinned["timeouts"]["box_factor"] is None:
        pinned["timeouts"] = {**pinned["timeouts"], "box_factor": 3.0}
    tests = {t["name"]: t for t in json.loads(Path(args.manifest).read_text())["tests"]}
    checker, test = _checker(checker_name), tests[test_name]
    walls = []
    with tempfile.TemporaryDirectory() as tmp:
        machine_json = Path(tmp) / "machine.json"
        machine_json.write_text(json.dumps(pinned))
        quiet.enter(pinned)
        try:
            for i in range(n):
                request = build_request(checker, test, lane, pinned, env={"PATH": os.environ.get("LKA_RUN_PATH", "/usr/bin:/bin")},
                                        round_id=f"calibration-{label}", pass_index=i, seq=i, epoch="calibration")
                rec = _measure(machine_json, request)
                if rec["status"] != "accepted":
                    raise RuntimeError(f"{checker_name} on {test_name}: {rec['status']}")
                walls.append(rec["wall_s"])
                print(f"{label} {i + 1}/{n}: {rec['wall_s']:.4f} s{'' if rec['valid'] else ' (invalid: ' + '; '.join(rec['invalid_reasons']) + ')'}")
        finally:
            quiet.leave()
    return walls


def cmd_sentinel(args) -> int:
    machine = load_machine(args.machine)
    walls = _runs(args, machine, machine["sentinel"]["checker"], machine["sentinel"]["test"], "1core", args.runs, "sentinel")
    limits = calibration.sentinel_limits(walls, k=args.k)
    record("sentinel", {"walls": walls, "limits": limits})
    print(f"sentinel: {limits['mean_s']:.5f} s +- {limits['k']} x {limits['sigma_s']:.5f} s")
    return 0


def cmd_box(args) -> int:
    machine = load_machine(args.machine)
    published = json.loads(Path(args.published).read_text())
    there = {r["test"]: r["wall_time"] for r in published["results"]
             if r["checker"] == "official" and r["status"] == "accepted" and r["test"] in args.tests}
    here = {}
    for test in args.tests:
        walls = _runs(args, machine, "official", test, "1core", 3, f"box-{test}")
        here[test] = sorted(walls)[1]
    factor = calibration.box_factor(here, there)
    record("box-factor", {"here": here, "there": there, "box_factor": factor})
    print(f"box factor: {factor}")
    return 0


def cmd_noise(args) -> int:
    machine = load_machine(args.machine)
    summary = {}
    for lane in ("1core", "4core"):
        walls = _runs(args, machine, args.checker, args.test, lane, args.runs, f"noise-{args.label}-{lane}")
        summary[lane] = {**calibration.noise_summary(walls), "walls": walls}
        print(f"{args.label} {lane}: CV {summary[lane]['cv'] * 100:.3f}%, spread {summary[lane]['spread'] * 100:.3f}%")
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "noise.jsonl", "a") as f:
        f.write(json.dumps({"label": args.label, "checker": args.checker, "test": args.test,
                            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **summary}) + "\n")
    return 0


def cmd_write(args) -> int:
    report = json.loads(Path(args.report).read_text())
    profile = Path(args.machine)
    profile.write_text(calibration.write_calibration(profile.read_text(), pinned_hz=report["pinned_khz"] * 1000,
                                                     box_factor=report["box_factor"], limits=report["sentinel_limits"]))
    print(f"calibrated {profile}; commit it, then reinstall /etc/lka/machine.json (quietbox/provision.sh) and write the epoch")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    soak = sub.add_parser("soak")
    soak.add_argument("machine")
    soak.add_argument("--minutes", type=float, default=10)
    soak.add_argument("--confirm-minutes", type=float, default=120)
    soak.add_argument("--max-temperature", type=float, default=80)
    for name in ("sentinel", "box", "noise"):
        p = sub.add_parser(name)
        p.add_argument("machine")
        p.add_argument("--pinned-khz", type=int, required=True)
        p.add_argument("--manifest", required=True)
        p.add_argument("--runs", type=int, default=30)
    sub.choices["sentinel"].add_argument("--k", type=float, default=4)
    sub.choices["box"].add_argument("--published", required=True)
    sub.choices["box"].add_argument("--tests", nargs="+", default=["init-prelude", "init", "std"])
    sub.choices["noise"].add_argument("--label", required=True)
    sub.choices["noise"].add_argument("--checker", default="official")
    sub.choices["noise"].add_argument("--test", default="init")
    write = sub.add_parser("write")
    write.add_argument("machine")
    write.add_argument("--report", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        print("calibrate.py must run as root", file=sys.stderr)
        return 1
    return {"soak": cmd_soak, "sentinel": cmd_sentinel, "box": cmd_box, "noise": cmd_noise, "write": cmd_write}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
