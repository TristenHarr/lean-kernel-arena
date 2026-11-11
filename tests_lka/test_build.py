"""Building a checker on the quiet box: as its own user, from the arena's own recipe, internet only."""

import unittest

from lka_quiet.build import (CHECKER_GROUP, BuildError, build_plan, nft_ruleset, removal_plan, validate_build_request,
                             validate_removal)

MACHINE = {
    "memory": {"max_bytes": 16106127360, "pids_max": 32768},
    "sandbox": {"user_prefix": "lkc-", "homes": "/srv/lka/checkers", "tmp_bytes": 4294967296},
    "paths": {"state": "/srv/lka"},
}


def request(**overrides):
    req = {"checker": "nano-lean", "arena": "/srv/lka/arena", "timeout_s": 7200}
    req.update(overrides)
    return req


class Requests(unittest.TestCase):
    def test_valid(self):
        validate_build_request(request(), MACHINE)

    def test_names_are_plain(self):
        for name in ("../x", "a b", "", "A"):
            with self.assertRaises(BuildError):
                validate_build_request(request(checker=name), MACHINE)

    def test_the_arena_tree_is_the_staged_one(self):
        with self.assertRaises(BuildError):
            validate_build_request(request(arena="/home/someone/arena"), MACHINE)


class PullRequests(unittest.TestCase):
    """A pull request's checker is pr<N>-<name>: its own user, home and tree, so it can never stand in
    for the board's build of the same checker."""

    def test_a_pr_checker_builds_from_its_own_pr_tree(self):
        validate_build_request(request(checker="pr12-nano-lean", arena="/srv/lka/pr/12/arena"), MACHINE)

    def test_never_from_another_pr_or_the_boards(self):
        for arena in ("/srv/lka/pr/13/arena", "/srv/lka/arena", "/srv/lka/pr/12/arena/../../13/arena"):
            with self.subTest(arena=arena), self.assertRaises(BuildError):
                validate_build_request(request(checker="pr12-nano-lean", arena=arena), MACHINE)

    def test_a_board_checker_never_builds_from_a_pr_tree(self):
        with self.assertRaises(BuildError):
            validate_build_request(request(checker="nano-lean", arena="/srv/lka/pr/12/arena"), MACHINE)

    def test_only_a_pr_checker_can_be_removed(self):
        validate_removal("pr7-kefir")
        for name in ("kefir", "pr-kefir", "pr7-", "pr07-kefir", "pr7-../x", "pr7-a b"):
            with self.subTest(name=name), self.assertRaises(BuildError):
                validate_removal(name)

    def test_removal_takes_the_user_and_the_home(self):
        argv = [step["argv"] for step in removal_plan("pr7-kefir", MACHINE, user="lkc-0123456789ab")]
        self.assertEqual(argv, [["pkill", "-KILL", "-u", "lkc-0123456789ab"],
                                ["userdel", "lkc-0123456789ab"],
                                ["rm", "-rf", "--one-file-system", "--", "/srv/lka/checkers/pr7-kefir"]])


class Plan(unittest.TestCase):
    def setUp(self):
        self.plan = build_plan(request(), MACHINE, user="lkc-abc", uid=1501, gid=1500)
        self.argv = [step["argv"] for step in self.plan]

    def test_the_arena_tree_is_copied_without_build_outputs_or_history(self):
        sync = next(a for a in self.argv if a[0] == "rsync")
        self.assertIn("--delete", sync)
        for excluded in ("/_build/checkers/", "/_out/", "/_results/", "/.git/"):
            self.assertIn(f"--exclude={excluded}", sync)
        self.assertEqual(sync[-2:], ["/srv/lka/arena/", "/srv/lka/checkers/nano-lean/arena/"])

    def test_the_home_belongs_to_the_checker(self):
        self.assertIn(["chown", "-R", "1501:1500", "/srv/lka/checkers/nano-lean"], self.argv)

    def test_the_build_is_the_arenas_own_recipe_as_the_checkers_user(self):
        build = self.argv[-1]
        self.assertEqual(build[:2], ["systemd-run", "--scope"])
        self.assertIn("--slice=lka-build.slice", build)
        self.assertIn("MemoryMax=16106127360", build)
        self.assertIn("MemorySwapMax=0", build)
        joined = " ".join(build)
        self.assertIn("setpriv --reuid=1501 --regid=1500 --init-groups --no-new-privs", joined)
        self.assertIn("HOME=/srv/lka/checkers/nano-lean", joined)
        self.assertIn("nix develop path:/srv/lka/checkers/nano-lean/arena --command python3 lka.py build-checker nano-lean", joined)

    def test_steps_run_in_the_checkers_arena(self):
        self.assertEqual(self.plan[-1]["cwd"], "/srv/lka/checkers/nano-lean/arena")


class Firewall(unittest.TestCase):
    def test_checkers_reach_the_internet_and_nothing_near(self):
        rules = nft_ruleset(1500)
        self.assertIn(f"meta skgid {1500}", rules)
        for private in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16",
                        "127.0.0.0/8", "fc00::/7", "fe80::/10"):
            self.assertIn(private, rules)
        self.assertIn("udp dport 53 accept", rules)
        self.assertIn("drop", rules)

    def test_the_group_name(self):
        self.assertEqual(CHECKER_GROUP, "lka-checkers")


if __name__ == "__main__":
    unittest.main()
