"""The quiet board's commands, dispatched from lka.py:

    lka.py quiet-stage    stage the official round's test inputs, byte for byte, by sha256
    lka.py quiet-build    build checkers on the quiet box, each as its own user (root helper)
    lka.py quiet-measure  run a measurement session against the staged inputs
    lka.py quiet-score    score a results store into board.json

Everything here reads and writes plain files; only quiet-build and quiet-measure reach the box's
root helper, through the command given with --helper.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from .board import score_board
from .session import RunStore, Session, current_records

MANIFEST_SCHEMA = "lka-quiet-tests/1"

_VOLATILE_EXACT = {
    "PWD", "OLDPWD", "SHLVL", "_", "TERM", "DISPLAY", "TMPDIR", "TMP", "TEMP", "TEMPDIR", "NIX_BUILD_TOP",
    "HOME", "USER", "LOGNAME", "IN", "LC_ALL", "THREADS", "MAIL", "HOSTNAME", "COLUMNS", "LINES",
    "INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID", "LS_COLORS", "OLDPWD", "SHELL", "MOTD_SHOWN",
}
_VOLATILE_PREFIXES = ("XDG_", "SSH_", "GITHUB_", "RUNNER_", "ACTIONS_", "LKA_", "SUDO_", "DBUS_", "LESS")


class StageError(RuntimeError):
    pass


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tarball_members(tarball: Path | None) -> dict[str, bytes]:
    """Test name -> bytes for every safe member good/<name>.ndjson or bad/<name>.ndjson."""
    members: dict[str, bytes] = {}
    if tarball is None or not Path(tarball).exists():
        return members
    with tarfile.open(tarball, "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if not member.isfile() or name.startswith("/") or ".." in Path(name).parts:
                continue
            for prefix in ("good/", "bad/"):
                if name.startswith(prefix) and name.endswith(".ndjson"):
                    data = tar.extractfile(member).read()
                    members[name[len(prefix):-len(".ndjson")]] = data
    return members


def _put(store: Path, sha: str, write) -> Path:
    target = store / sha[:2] / f"{sha}.ndjson"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        write(tmp)
        os.replace(tmp, target)
        target.chmod(0o644)
    return target


def _link_or_copy(source: Path, target: Path, link: bool) -> None:
    if link:
        try:
            os.link(os.path.realpath(source), target)
            return
        except OSError:
            pass
    shutil.copyfile(source, target)


def stage_tests(results_json: Path, *, tarball: Path | None, sources, store: Path, allow_mismatch: set,
                link: bool = False) -> dict:
    """The manifest of the round's tests, each stored by its sha256. With `link`, a source file on
    the store's file system is hard-linked rather than copied; it must then never change."""
    published = json.loads(Path(results_json).read_text())
    from_tarball = _tarball_members(tarball)
    staged, problems = [], []
    for test in sorted(published["tests"], key=lambda t: t["name"]):
        name, official = test["name"], test["sha256"]
        candidates = []
        if name in from_tarball:
            data = from_tarball[name]
            candidates.append((hashlib.sha256(data).hexdigest(), lambda tmp, data=data: tmp.write_bytes(data)))
        for source in sources:
            path = Path(source) / f"{name}.ndjson"
            if path.is_file():
                candidates.append((_sha_file(path), lambda tmp, path=path: _link_or_copy(path, tmp, link)))
        exact = next((c for c in candidates if c[0] == official), None)
        if exact is not None:
            chosen, match = exact, True
        elif candidates and name in allow_mismatch:
            chosen, match = candidates[0], False
        else:
            problems.append(f"{name}: " + ("no file" if not candidates else
                                          f"no file with sha256 {official[:12]}… (have {', '.join(c[0][:12] for c in candidates)})"))
            continue
        sha, write = chosen
        path = _put(Path(store), sha, write)
        entry = {"name": name, "outcome": test.get("outcome"), "sha256": sha, "official_sha256": official,
                 "official_match": match, "path": str(path), "size": path.stat().st_size}
        if test.get("timeout") is not None:
            entry["timeout"] = test["timeout"]
        staged.append(entry)
    if problems:
        raise StageError("cannot stage:\n  " + "\n  ".join(problems))
    return {"schema": MANIFEST_SCHEMA, "arena_revision": published.get("meta", {}).get("git_revision"),
            "tests": staged}


def run_env(environ) -> dict:
    """The environment a checker runs with: the arena's devShell, without anything that varies per job."""
    out = {}
    for key, value in environ.items():
        if key in _VOLATILE_EXACT or key.startswith(_VOLATILE_PREFIXES):
            continue
        if not key.replace("_", "").isalnum() or not key[0].isalpha() or key.upper() != key:
            continue
        if "\n" in value or "\0" in value:
            continue
        out[key] = value
    return out


def score_store(store: RunStore, tests, checkers, scoring: dict, machine: dict) -> dict:
    hashes = {c["name"]: c["source_hash"] for c in checkers}
    shas = {t["name"]: t["sha256"] for t in tests}
    current = current_records(store.records(), hashes, shas)
    records = [r for cell_records in current.values() for r in cell_records]
    board = score_board(tests, [{"name": c["name"]} for c in checkers], records, scoring)
    board["machine"] = machine
    return board


# ---------------------------------------------------------------------------------------------
# Command handlers; `lka` is lka.py's module namespace (YAML loading, checker hashing).


def _helper_measure(helper: list[str]):
    def measure(request: dict) -> dict:
        result = subprocess.run(helper, input=json.dumps(request), capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(helper)} failed ({result.returncode}): {result.stderr.strip()}")
        return json.loads(result.stdout)
    return measure


def _sentinel(machine, tests_by_name, checkers_by_name, measure, env, epoch, round_id):
    from .session import build_request
    config = machine["sentinel"]
    limits = config["limits"]
    checker, test = checkers_by_name[config["checker"]], tests_by_name[config["test"]]

    def read() -> tuple[bool, dict]:
        request = build_request(checker, test, "1core", machine, env=env, round_id=round_id, pass_index=-1,
                                seq=-1, epoch=epoch)
        record = measure(request)
        ok = record["status"] == "accepted" and record["valid"]
        if ok and limits:
            ok = abs(record["wall_s"] - limits["mean_s"]) <= limits["k"] * limits["sigma_s"]
        return ok, record
    return read


def load_manifest(path: Path) -> list[dict]:
    manifest = json.loads(Path(path).read_text())
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise SystemExit(f"{path} is not a staged-tests manifest")
    return manifest["tests"]


def cmd_quiet_stage(args, lka) -> int:
    try:
        manifest = stage_tests(Path(args.published) / "results.json",
                               tarball=Path(args.published) / "lean-arena-tests.tar.gz",
                               sources=[Path(s) for s in args.source], store=Path(args.store),
                               allow_mismatch=set(args.allow_mismatch or []), link=args.link)
    except StageError as error:
        print(error, file=sys.stderr)
        return 1
    Path(args.manifest).parent.mkdir(parents=True, exist_ok=True)
    Path(args.manifest).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    flagged = [t["name"] for t in manifest["tests"] if not t["official_match"]]
    print(f"staged {len(manifest['tests'])} tests of arena revision {manifest['arena_revision']}"
          + (f"; not the official bytes: {', '.join(flagged)}" if flagged else ""))
    return 0


def _checkers(lka, pattern: str | None) -> list[dict]:
    checkers = lka.find_items_by_pattern(pattern, "checkers") if pattern else lka.load_checkers()
    for checker in checkers:
        checker["source_hash"] = lka.source_hash(checker, "checkers")
    return checkers


def cmd_quiet_measure(args, lka) -> int:
    import fnmatch
    import yaml
    machine = yaml.safe_load(Path(args.machine).read_text())
    if not args.calibrating:
        missing = [k for k, v in (("frequency.pinned_hz", machine["frequency"]["pinned_hz"]),
                                  ("timeouts.box_factor", machine["timeouts"]["box_factor"]),
                                  ("sentinel.limits", machine["sentinel"]["limits"])) if v is None]
        if missing:
            print(f"{args.machine} is not calibrated ({', '.join(missing)}); run quietbox/calibrate.py, "
                  "or pass --calibrating for a calibration round", file=sys.stderr)
            return 1
    if args.calibrating and machine["timeouts"]["box_factor"] is None:
        machine["timeouts"]["box_factor"] = 3.0
    epoch = Path(args.epoch_file).read_text().strip()
    tests = load_manifest(Path(args.manifest))
    if args.test:
        tests = [t for t in tests if any(fnmatch.fnmatch(t["name"], p) for p in args.test)]
    all_checkers = _checkers(lka, None)
    checkers = [c for c in all_checkers if not args.checker or fnmatch.fnmatch(c["name"], args.checker)]
    env = run_env(os.environ)
    measure = _helper_measure(args.helper.split())
    store = RunStore(Path(args.results), epoch=epoch)
    sentinel = None
    if not args.no_sentinel:
        sentinel = _sentinel(machine, {t["name"]: t for t in load_manifest(Path(args.manifest))},
                             {c["name"]: c for c in all_checkers}, measure, env, epoch, args.round)
    session = Session(store=store, checkers=checkers, tests=tests, machine=machine, round_id=args.round, env=env,
                      measure=measure, sentinel=sentinel, log=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
    summary = session.run(budget_s=args.budget_hours * 3600)
    print(json.dumps(summary))
    return 0 if summary["outcome"] in ("complete", "budget") else 3


def cmd_quiet_score(args, lka) -> int:
    import yaml
    scoring = yaml.safe_load(Path(args.season).read_text())
    machine = yaml.safe_load(Path(args.machine).read_text())
    epoch = Path(args.epoch_file).read_text().strip() if args.epoch_file else args.epoch
    tests = load_manifest(Path(args.manifest))
    checkers = _checkers(lka, None)
    board = score_store(RunStore(Path(args.results), epoch=epoch), tests, checkers, scoring, machine)
    board["tests"] = tests
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(board, indent=1, sort_keys=True) + "\n")
    print(f"scored {len(board['checkers'])} checkers, {board['progress']['measured']}/{board['progress']['needed']} cells")
    return 0


def add_parsers(subparsers) -> None:
    stage = subparsers.add_parser("quiet-stage", help="Stage the official round's test inputs for the quiet board")
    stage.add_argument("--published", required=True, metavar="DIR",
                       help="Directory holding the official results.json and lean-arena-tests.tar.gz")
    stage.add_argument("--source", action="append", default=[], metavar="DIR",
                       help="Directory of <test>.ndjson exports to take bytes from (repeatable)")
    stage.add_argument("--store", required=True, help="Directory the inputs are stored in, by sha256")
    stage.add_argument("--manifest", required=True, help="Manifest file to write")
    stage.add_argument("--link", action="store_true",
                       help="Hard-link sources on the store's file system instead of copying (they must never change)")
    stage.add_argument("--allow-mismatch", action="append", metavar="TEST",
                       help="Stage this test even if no file has the official bytes (flagged on the board)")

    measure = subparsers.add_parser("quiet-measure", help="Run a quiet-board measurement session")
    measure.add_argument("--machine", required=True, help="Machine profile (machines/*.yaml)")
    measure.add_argument("--manifest", required=True, help="Staged-tests manifest (see quiet-stage)")
    measure.add_argument("--results", required=True, help="Results store directory")
    measure.add_argument("--epoch-file", default="/etc/lka/epoch", help="File holding the machine epoch")
    measure.add_argument("--round", required=True, help="Round name, e.g. 2026-11")
    measure.add_argument("--checker", help="Glob of checkers to measure (default: all)")
    measure.add_argument("--test", nargs="+", help="Globs of tests to measure (default: all staged)")
    measure.add_argument("--budget-hours", type=float, default=20.0)
    measure.add_argument("--helper", default="sudo -n /usr/local/libexec/lka/lka-measure",
                         help="Command that measures one request (JSON on stdin)")
    measure.add_argument("--calibrating", action="store_true", help="Measure before the machine is calibrated")
    measure.add_argument("--no-sentinel", action="store_true")

    score = subparsers.add_parser("quiet-score", help="Score a quiet-board results store into board.json")
    score.add_argument("--season", required=True, help="Scoring rules (scoring/*.yaml)")
    score.add_argument("--machine", required=True, help="Machine profile (machines/*.yaml)")
    score.add_argument("--manifest", required=True)
    score.add_argument("--results", required=True)
    score.add_argument("--epoch-file")
    score.add_argument("--epoch")
    score.add_argument("--out", default="_build/board.json")


COMMANDS = {"quiet-stage": cmd_quiet_stage, "quiet-measure": cmd_quiet_measure, "quiet-score": cmd_quiet_score}
