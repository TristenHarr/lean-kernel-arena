"""Building a checker on the quiet box.

Each checker has its own system user (in the group lka-checkers) and home, holding its toolchains
and its own copy of the arena tree. Its build is the arena's own: `lka.py build-checker <name>`
in the arena flake's devShell, run as that user in a memory-capped scope, so recipes that train
PGO with `../../../../lka.py build-test init` find exactly the layout they expect. Builds may reach
the internet (sources, toolchains, crates) but nothing near: an nftables policy on the group's
sockets drops every private, shared and link-local destination. Runs get no network at all.

A pull request's version of a checker is built as pr<N>-<name>, from the PR's own staged tree
(state/pr/<N>/arena): its own user, home and build record, so it can never stand in for the board's
build of the same checker. Only such a checker can be removed, when its PR closes.

    quietbox/lka-measure --build      (root; one build request on stdin, a build record on stdout)
    quietbox/lka-measure --remove     (root; {"checker": "pr<N>-<name>"} on stdin)
"""

import os
import re
import subprocess
import time
from pathlib import Path

CHECKER_GROUP = "lka-checkers"

_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_PR_NAME = re.compile(r"^pr([1-9][0-9]*)-[a-z0-9][a-z0-9._-]*$")
_NEAR = (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8",
    "0.0.0.0/8", "224.0.0.0/4", "240.0.0.0/4",
)
_NEAR6 = ("fc00::/7", "fe80::/10", "ff00::/8", "::1/128")
_EXCLUDES = ("/_build/checkers/", "/_out/", "/_results/", "/.git/", "/__pycache__/")
DEVSHELL_PATH = "/nix/var/nix/profiles/default/bin:/usr/local/bin:/usr/bin:/bin"


class BuildError(ValueError):
    pass


def validate_build_request(req: dict, machine: dict) -> None:
    name = req.get("checker")
    if not isinstance(name, str) or not _NAME.match(name) or ".." in name:
        raise BuildError(f"checker name {name!r} is not a plain name")
    pr = _PR_NAME.match(name)
    state = machine["paths"]["state"]
    staged = os.path.join(state, "pr", pr.group(1), "arena") if pr else os.path.join(state, "arena")
    if os.path.normpath(str(req.get("arena", ""))) != staged:
        raise BuildError(f"the arena tree to build {name} from must be {staged}")
    timeout = req.get("timeout_s")
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise BuildError(f"timeout_s {timeout!r} must be positive")


def build_plan(req: dict, machine: dict, *, user: str, uid: int, gid: int) -> list[dict]:
    name = req["checker"]
    home = os.path.join(machine["sandbox"]["homes"], name)
    arena = os.path.join(home, "arena")
    env = [f"HOME={home}", f"USER={user}", f"LOGNAME={user}", f"PATH={DEVSHELL_PATH}", "LC_ALL=C.UTF-8",
           f"XDG_CACHE_HOME={home}/.cache", "NIX_REMOTE=daemon"]
    devshell = ["nix", "develop", f"path:{arena}", "--command", "python3", "lka.py", "build-checker", name]
    return [
        {"argv": ["install", "-d", "-o", str(uid), "-g", str(gid), "-m", "0750", home], "cwd": "/"},
        {"argv": ["rsync", "-a", "--delete", *[f"--exclude={e}" for e in _EXCLUDES],
                  req["arena"].rstrip("/") + "/", arena + "/"], "cwd": "/"},
        {"argv": ["chown", "-R", f"{uid}:{gid}", home], "cwd": "/"},
        {"argv": ["systemd-run", "--scope", "--quiet", "--collect", "--slice=lka-build.slice",
                  "-p", f"MemoryMax={machine['memory']['max_bytes']}", "-p", "MemorySwapMax=0",
                  "-p", f"TasksMax={machine['memory']['pids_max']}", "--",
                  "setpriv", f"--reuid={uid}", f"--regid={gid}", "--init-groups", "--no-new-privs", "--",
                  "env", "-i", *env, *devshell], "cwd": arena},
    ]


def validate_removal(name) -> None:
    if not isinstance(name, str) or not _PR_NAME.match(name) or ".." in name or "/" in name:
        raise BuildError(f"{name!r} is not a pull request's checker; only those are ever removed")


def removal_plan(name: str, machine: dict, *, user: str) -> list[dict]:
    home = os.path.join(machine["sandbox"]["homes"], name)
    return [
        {"argv": ["pkill", "-KILL", "-u", user], "cwd": "/", "may_fail": True},
        {"argv": ["userdel", user], "cwd": "/", "may_fail": True},
        {"argv": ["rm", "-rf", "--one-file-system", "--", home], "cwd": "/"},
    ]


def nft_ruleset(gid: int) -> str:
    """The checkers' egress policy: loopback and DNS, then nothing private, then the internet."""
    near4 = ", ".join(_NEAR)
    near6 = ", ".join(_NEAR6)
    return f"""table inet lka_checkers {{
    chain output {{
        type filter hook output priority filter; policy accept;
        meta skgid {gid} oifname "lo" accept
        meta skgid {gid} udp dport 53 accept
        meta skgid {gid} tcp dport 53 accept
        meta skgid {gid} ip daddr {{ {near4} }} drop
        meta skgid {gid} ip6 daddr {{ {near6} }} drop
    }}
}}
"""


# ---------------------------------------------------------------------------------------------
# Root only


def ensure_user(name: str, machine: dict) -> tuple[str, int, int]:
    import grp
    import pwd
    from .helper import checker_user
    user = checker_user(name, machine["sandbox"]["user_prefix"])
    try:
        grp.getgrnam(CHECKER_GROUP)
    except KeyError:
        subprocess.run(["groupadd", "--system", CHECKER_GROUP], check=True)
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        home = os.path.join(machine["sandbox"]["homes"], name)
        subprocess.run(["useradd", "--system", "--gid", CHECKER_GROUP, "--home-dir", home, "--no-create-home",
                        "--shell", "/usr/sbin/nologin", "--comment", f"lka checker {name}", user], check=True)
        account = pwd.getpwnam(user)
    return user, account.pw_uid, account.pw_gid


def run_build(req: dict, machine: dict) -> dict:
    validate_build_request(req, machine)
    user, uid, gid = ensure_user(req["checker"], machine)
    plan = build_plan(req, machine, user=user, uid=uid, gid=gid)
    log_dir = Path(machine["paths"]["state"]) / "build-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{req['checker']}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.log"
    started = time.monotonic()
    exit_code = 0
    with open(log_path, "wb") as log:
        for step in plan:
            log.write(("$ " + " ".join(step["argv"]) + "\n").encode())
            log.flush()
            try:
                result = subprocess.run(step["argv"], cwd=step["cwd"], stdout=log, stderr=subprocess.STDOUT,
                                        timeout=max(1.0, req["timeout_s"] - (time.monotonic() - started)))
                exit_code = result.returncode
            except subprocess.TimeoutExpired:
                exit_code = 124
            if exit_code != 0:
                break
    tail = log_path.read_bytes()[-16384:].decode(errors="replace")
    return {"checker": req["checker"], "user": user, "exit_code": exit_code, "ok": exit_code == 0,
            "duration_s": round(time.monotonic() - started, 3), "log": str(log_path), "log_tail": tail}


def remove_checker(req: dict, machine: dict) -> dict:
    from .helper import checker_user
    name = req.get("checker")
    validate_removal(name)
    user = checker_user(name, machine["sandbox"]["user_prefix"])
    for step in removal_plan(name, machine, user=user):
        result = subprocess.run(step["argv"], cwd=step["cwd"], capture_output=True)
        if result.returncode != 0 and not step.get("may_fail"):
            return {"checker": name, "ok": False, "step": step["argv"][0], "stderr": result.stderr.decode()[-4096:]}
    return {"checker": name, "ok": True}
