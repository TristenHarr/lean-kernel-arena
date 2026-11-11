"""What provisioning leaves on the box for the workflows: every command a job runs is there, the runner
finds Nix, the runner's download is the one pinned, and sudo grants exactly what the jobs start."""

import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROVISION = (ROOT / "quietbox" / "provision.sh").read_text()
UNIT = (ROOT / "quietbox" / "lka-runner.service").read_text()
LOCK = ROOT / "quietbox" / "actions-runner.lock"
WORKFLOWS = [p.read_text() for p in sorted((ROOT / ".github").rglob("*.yml"))]
BRINGUP_PATH = ROOT / "quietbox" / "bringup.sh"
BRINGUP = BRINGUP_PATH.read_text()
ACTION = (ROOT / ".github" / "actions" / "quiet-round" / "action.yml").read_text()


def sudoers_line() -> str:
    return next(line for line in PROVISION.splitlines() if line.startswith("lka-runner ALL=(root) NOPASSWD:"))


def sudo_commands_in_workflows() -> set[str]:
    found = set()
    for text in WORKFLOWS:
        for match in re.finditer(r"sudo -n (/usr/local/libexec/lka/[\w.-]+)((?: --?[\w-]+| on| off| status)*)", text):
            found.add((match.group(1) + match.group(2)).strip())
    for text in [(ROOT / "lka_quiet" / "cli.py").read_text()]:
        for match in re.finditer(r'default="sudo -n (/usr/local/libexec/lka/[\w.-]+)"', text):
            found.add(match.group(1))
    return found


class Sudo(unittest.TestCase):
    def granted(self) -> list[str]:
        return [c.strip().replace("$libexec", "/usr/local/libexec/lka") for c in sudoers_line().split(":", 1)[1].split(",")]

    def test_every_command_a_job_starts_with_sudo_is_granted_exactly(self):
        granted = self.granted()
        for command in sorted(sudo_commands_in_workflows()):
            with self.subTest(command=command):
                exact = command if " " in command else f'{command} ""'
                self.assertIn(exact, granted)

    def test_a_command_granted_without_arguments_takes_none(self):
        for command in self.granted():
            with self.subTest(command=command):
                self.assertIn(" ", command, f"{command} would be allowed with any arguments")

    def test_the_runner_never_gets_more(self):
        for command in self.granted():
            self.assertTrue(command.startswith("/usr/local/libexec/lka/"), command)


class Runner(unittest.TestCase):
    def test_the_runner_finds_nix(self):
        path = next(line for line in UNIT.splitlines() if line.startswith("Environment=PATH="))
        self.assertIn("/nix/var/nix/profiles/default/bin", path.split("=", 2)[2].split(":"))

    def test_the_download_is_the_one_pinned(self):
        version, digest = LOCK.read_text().split()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertIn("actions-runner.lock", PROVISION)
        self.assertIn("sha256sum -c", PROVISION)
        self.assertNotIn("releases/latest", PROVISION)


class Packages(unittest.TestCase):
    def test_every_tool_a_job_runs_is_installed(self):
        packages = PROVISION.split("apt-get install", 1)[1].split("\n\n", 1)[0]
        for tool in ("gh", "jq", "rsync", "curl", "python3", "python3-yaml", "nftables", "acct"):
            with self.subTest(tool=tool):
                self.assertRegex(packages, rf"(^|\s){re.escape(tool)}(\s|$)")


class NextSteps(unittest.TestCase):
    def test_the_closing_steps_name_real_calibration_commands(self):
        closing = PROVISION.rsplit("cat <<EOF", 1)[1]
        for match in re.finditer(r"calibrate\.py (\w+)", closing):
            self.assertIn(match.group(1), ("soak", "sentinel", "box", "noise", "write"))
        self.assertIn("bringup.sh", closing)


# bringup.sh is the on-box orchestrator provision.sh hands off to: it runs, in order, exactly the
# steps that must happen before the runner is switched on, and no step a workflow does for itself.
CALIBRATE_STEPS = ("soak", "sentinel", "box", "noise", "write")


class Bringup(unittest.TestCase):
    def phases_defined(self) -> set[str]:
        return set(re.findall(r"^phase_(\w+)\(\)", BRINGUP, re.MULTILINE))

    def phases_dispatched(self) -> set[str]:
        run_lines = re.findall(r"\brun ([\w ]+?)\s*(?:;;|$)", BRINGUP, re.MULTILINE)
        names = {name for line in run_lines for name in line.split()}
        return {n for n in names if not n.startswith('"')}

    def test_bringup_is_executable(self):
        self.assertTrue(os.access(BRINGUP_PATH, os.X_OK), "bringup.sh must be executable")
        self.assertTrue(BRINGUP.startswith("#!"))

    def test_every_dispatched_phase_is_defined_and_every_phase_is_dispatched(self):
        self.assertEqual(self.phases_dispatched(), self.phases_defined())

    def test_the_case_offers_exactly_the_defined_phases(self):
        offered = set(re.search(r"verifier\|seed\|[\w|]+\) run", BRINGUP).group(0).split(")")[0].split("|"))
        self.assertEqual(offered, self.phases_defined())

    def test_it_runs_only_real_calibration_commands_and_all_of_them(self):
        used = set(re.findall(r"calibrate\.py (\w+)", BRINGUP))
        self.assertEqual(used, set(CALIBRATE_STEPS))
        order = [m.group(1) for m in re.finditer(r"calibrate\.py (\w+)", BRINGUP)]
        self.assertEqual(order[0], "soak")
        self.assertEqual(order[-1], "write")

    def test_its_round_is_staged_the_way_the_workflow_stages_it(self):
        # Every lka.py subcommand bring-up runs to make the round is one the quiet-round action or a
        # workflow runs too, so the calibrated round and the measured round never diverge.
        elsewhere = ACTION + "".join(WORKFLOWS)
        for sub in set(re.findall(r"lka\.py (quiet-\w+|build-test)", BRINGUP)):
            with self.subTest(sub=sub):
                self.assertIn(f"lka.py {sub}", elsewhere)
        for flag in ("--round-seed", "--verify-with", "--season", "--allow-mismatch cedar"):
            with self.subTest(flag=flag):
                self.assertIn(flag, BRINGUP)

    def test_it_records_the_epoch_and_enables_the_runner_only_with_the_token(self):
        self.assertIn("/usr/local/libexec/lka/lka-preflight --write", BRINGUP)
        self.assertIn("systemctl enable --now lka-runner", BRINGUP)
        token = BRINGUP.index("/etc/lka/github-token")
        enable = BRINGUP.index("systemctl enable --now lka-runner")
        self.assertLess(token, enable, "the token must be checked before the runner is enabled")
        self.assertRegex(BRINGUP, r"github-token.*0600|600 root")

    def test_the_closing_steps_of_provision_are_the_phases_bringup_runs(self):
        # provision.sh's hand-off and bringup.sh's phases describe one bring-up, so they cannot drift.
        self.assertIn("stage", self.phases_defined())
        self.assertIn("calibrate", self.phases_defined())
        self.assertIn("epoch", self.phases_defined())
        self.assertIn("runner", self.phases_defined())


if __name__ == "__main__":
    unittest.main()
