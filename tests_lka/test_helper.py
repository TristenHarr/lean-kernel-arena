"""The measurement helper's decisions, short of the root-only system calls."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from lka_quiet.helper import (
    ENV_BYTES,
    HelperError,
    canonical_env,
    checker_user,
    configure_run_cgroup,
    env_file_text,
    perf_command,
    shim_command,
    validate_request,
)
from lka_quiet.perf import EVENTS

MACHINE = {
    "lanes": {"4core": [0, 1, 2, 3], "1core": [3]},
    "housekeeping": [0],
    "memory": {"max_bytes": 16106127360, "pids_max": 32768},
    "sandbox": {"user_prefix": "lkc-", "homes": "/srv/lka/checkers", "tmp_bytes": 4294967296},
    "paths": {"tests": "/srv/lka/tests", "checkers": "/srv/lka/checkers"},
}


def request(**overrides):
    req = {
        "checker": "nano-lean",
        "test": "std",
        "lane": "4core",
        "expected": "accept",
        "cmd": 'target/release/nl-fast -q -j 4 "$IN"',
        "workdir": "/srv/lka/checkers/nano-lean/src",
        "input": "/srv/lka/tests/ab/abcdef.ndjson",
        "timeout_s": 600.0,
        "env": {"PATH": "/nix/store/x-shell/bin:/usr/bin:/bin"},
    }
    req.update(overrides)
    return req


class Users(unittest.TestCase):
    def test_name(self):
        digest = hashlib.sha256(b"nano-lean").hexdigest()[:12]
        self.assertEqual(checker_user("nano-lean", "lkc-"), f"lkc-{digest}")

    def test_fits_a_linux_user_name(self):
        self.assertLessEqual(len(checker_user("a-very-long-checker-name-indeed", "lkc-")), 32)

    def test_distinct(self):
        self.assertNotEqual(checker_user("tenet", "lkc-"), checker_user("tenet-hs", "lkc-"))


class Requests(unittest.TestCase):
    def test_valid(self):
        validate_request(request(), MACHINE)

    def test_unknown_lane(self):
        with self.assertRaisesRegex(HelperError, "lane"):
            validate_request(request(lane="8core"), MACHINE)

    def test_workdir_must_be_the_checkers_own(self):
        with self.assertRaisesRegex(HelperError, "workdir"):
            validate_request(request(workdir="/srv/lka/checkers/sokonanoda/src"), MACHINE)

    def test_workdir_escape_by_dotdot(self):
        with self.assertRaisesRegex(HelperError, "workdir"):
            validate_request(request(workdir="/srv/lka/checkers/nano-lean/../sokonanoda"), MACHINE)

    def test_input_must_be_a_staged_test(self):
        with self.assertRaisesRegex(HelperError, "input"):
            validate_request(request(input="/etc/shadow"), MACHINE)

    def test_input_escape_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            tests = Path(tmp) / "tests"
            tests.mkdir()
            (tests / "evil.ndjson").symlink_to("/etc/hostname")
            machine = {**MACHINE, "paths": {**MACHINE["paths"], "tests": str(tests)}}
            with self.assertRaisesRegex(HelperError, "input"):
                validate_request(request(input=str(tests / "evil.ndjson")), machine)

    def test_checker_name_shape(self):
        with self.assertRaisesRegex(HelperError, "checker"):
            validate_request(request(checker="../x"), MACHINE)

    def test_command_must_be_text(self):
        for bad in ("", None, ["sh"]):
            with self.assertRaisesRegex(HelperError, "cmd"):
                validate_request(request(cmd=bad), MACHINE)

    def test_timeout_must_be_positive(self):
        with self.assertRaisesRegex(HelperError, "timeout"):
            validate_request(request(timeout_s=0), MACHINE)

    def test_env_names_and_values(self):
        for env in ({"lower": "x"}, {"A B": "x"}, {"PATH": "a\nb"}, {"LD_PRELOAD": "/evil.so"}):
            with self.assertRaisesRegex(HelperError, "env"):
                validate_request(request(env=env), MACHINE)


class Environment(unittest.TestCase):
    def test_fixed_entries(self):
        env = dict(canonical_env(request(), MACHINE, lane_cpus=[0, 1, 2, 3], user="lkc-x"))
        self.assertEqual(env["IN"], "/srv/lka/tests/ab/abcdef.ndjson")
        self.assertEqual(env["LC_ALL"], "C")
        self.assertEqual(env["THREADS"], "4")
        self.assertEqual(env["HOME"], "/srv/lka/checkers/nano-lean")
        self.assertEqual(env["USER"], "lkc-x")
        self.assertEqual(env["TMPDIR"], "/tmp")
        self.assertEqual(env["PATH"], "/nix/store/x-shell/bin:/usr/bin:/bin")

    def test_one_core_lane_says_one_thread(self):
        env = dict(canonical_env(request(lane="1core"), MACHINE, lane_cpus=[3], user="lkc-x"))
        self.assertEqual(env["THREADS"], "1")

    def test_request_cannot_override_fixed_entries(self):
        env = dict(canonical_env(request(env={"IN": "/x", "HOME": "/root", "PATH": "/bin"}), MACHINE,
                                 lane_cpus=[3], user="lkc-x"))
        self.assertEqual(env["IN"], "/srv/lka/tests/ab/abcdef.ndjson")
        self.assertEqual(env["HOME"], "/srv/lka/checkers/nano-lean")

    def test_padded_to_the_same_size_for_every_checker_and_test(self):
        short = env_file_text(canonical_env(request(), MACHINE, lane_cpus=[3], user="lkc-x"))
        long = env_file_text(canonical_env(
            request(checker="a-much-longer-checker-name", workdir="/srv/lka/checkers/a-much-longer-checker-name/src",
                    input="/srv/lka/tests/ff/" + "f" * 64 + ".ndjson"),
            MACHINE, lane_cpus=[3], user="lkc-y"))
        size = lambda text: sum(len(line.encode()) + 1 for line in text.splitlines())
        self.assertEqual(size(short), ENV_BYTES)
        self.assertEqual(size(long), ENV_BYTES)

    def test_sorted_and_deterministic(self):
        env = canonical_env(request(env={"ZED": "1", "ALPHA": "2"}), MACHINE, lane_cpus=[3], user="lkc-x")
        names = [k for k, _ in env if k != "LKA_PAD"]
        self.assertEqual(names, sorted(names))


class Commands(unittest.TestCase):
    def test_perf_starts_disabled_under_control(self):
        cmd = perf_command("/run/lka/r/perf.json", "/run/lka/r/ctl", "/run/lka/r/ack")
        self.assertEqual(cmd[:4], ["perf", "stat", "-j", "-o"])
        self.assertIn("-D", cmd)
        self.assertEqual(cmd[cmd.index("-D") + 1], "-1")
        self.assertEqual(cmd[cmd.index("--control") + 1], "fifo:/run/lka/r/ctl,/run/lka/r/ack")
        self.assertEqual(cmd[cmd.index("-e") + 1], ",".join(EVENTS))
        self.assertEqual(cmd[-1], "--")

    def test_shim_runs_the_command_through_sh(self):
        cmd = shim_command("/usr/local/libexec/lka/lka-shim", cgroup="/sys/fs/cgroup/x/run", uid=1500, gid=1500,
                           workdir="/srv/lka/checkers/c/src", env_path="/run/lka/r/env", tmp_bytes=4096,
                           overlay="/srv/lka/checkers/c", ctl="/run/lka/r/ctl", ack="/run/lka/r/ack",
                           status="/run/lka/r/status.json", run='a "$IN"')
        self.assertEqual(cmd[0], "/usr/local/libexec/lka/lka-shim")
        self.assertEqual(cmd[-4:], ["--", "/bin/sh", "-c", 'a "$IN"'])
        self.assertEqual(cmd[cmd.index("--uid") + 1], "1500")
        self.assertEqual(cmd[cmd.index("--overlay") + 1], "/srv/lka/checkers/c")


class Cgroups(unittest.TestCase):
    def test_run_cgroup_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            run.mkdir()
            for name in ("cpuset.cpus", "cpuset.mems", "memory.max", "memory.swap.max", "memory.oom.group", "pids.max"):
                (run / name).write_text("")
            configure_run_cgroup(run, cpus=[1, 2, 3], memory_bytes=1234, pids=99)
            self.assertEqual((run / "cpuset.cpus").read_text(), "1-3")
            self.assertEqual((run / "cpuset.mems").read_text(), "0")
            self.assertEqual((run / "memory.max").read_text(), "1234")
            self.assertEqual((run / "memory.swap.max").read_text(), "0")
            self.assertEqual((run / "memory.oom.group").read_text(), "1")
            self.assertEqual((run / "pids.max").read_text(), "99")

    def test_cpu_list_format(self):
        from lka_quiet.helper import cpu_list
        self.assertEqual(cpu_list([3]), "3")
        self.assertEqual(cpu_list([0, 1, 2, 3]), "0-3")
        self.assertEqual(cpu_list([0, 2, 3, 5]), "0,2-3,5")


if __name__ == "__main__":
    unittest.main()
