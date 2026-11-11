"""What provisioning leaves on the box for the workflows: every command a job runs is there, the runner
finds Nix, the runner's download is the one pinned, and sudo grants exactly what the jobs start."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROVISION = (ROOT / "quietbox" / "provision.sh").read_text()
UNIT = (ROOT / "quietbox" / "lka-runner.service").read_text()
LOCK = ROOT / "quietbox" / "actions-runner.lock"
WORKFLOWS = [p.read_text() for p in sorted((ROOT / ".github").rglob("*.yml"))]


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


if __name__ == "__main__":
    unittest.main()
