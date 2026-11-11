"""The quiet board's commands, dispatched from lka.py:

    lka.py quiet-stage    stage the official round's test inputs, byte for byte, by sha256; with a round
                          seed, measure them re-encoded and add invalid twins (encode_round)
    lka.py quiet-build    build checkers on the quiet box, each as its own user (root helper)
    lka.py quiet-measure  run a measurement session against the staged inputs
    lka.py quiet-score    score a results store into board.json

Everything here reads and writes plain files; only quiet-build and quiet-measure reach the box's
root helper, through the command given with --helper.
"""

import fnmatch
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
from .encode import reencode_file
from .session import RunStore, Session, current_records
from .twins import twin_file

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
    """File `sha` in the store. Inputs are readable by the store's group alone (lka-input, which a
    measured run is given and a build never is), so no checker can read a round's inputs, its
    re-encodings or its twins, outside the one run each is measured in."""
    target = store / sha[:2] / f"{sha}.ndjson"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.parent.chmod(0o750)
        tmp = target.with_suffix(".tmp")
        write(tmp)
        os.replace(tmp, target)
        target.chmod(0o640)
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


def _store_file(store: Path, produce) -> tuple[str | None, Path | None, object]:
    """Write a file with `produce(path)` beside the store, then file it by its sha256; the digest
    and path are None when `produce` left no file. Returns what `produce` returned too."""
    store.mkdir(parents=True, exist_ok=True)
    scratch = store / f".scratch-{os.getpid()}.ndjson"
    try:
        produced = produce(scratch)
        if not scratch.exists():
            return None, None, produced
        digest = _sha_file(scratch)
        path = _put(store, digest, lambda target: os.replace(scratch, target))
    finally:
        scratch.unlink(missing_ok=True)
    return digest, path, produced


def encode_round(manifest: dict, *, seed: str, store: Path, twin_from, twin_prefix: int = 400,
                 verify=None, twin_tries: int = 20) -> dict:
    """The round's inputs from the staged ones: every test re-encoded from the round's seed (the file
    measured from then on, its declarations and verdict unchanged, its bytes new), and for each test
    to be accepted whose name matches a `twin_from` pattern, an invalid twin `fresh/<name>` (see
    twins.py) that `verify` confirms, the official kernel rejecting it. The manifest commits to the
    seed by its sha256 and never holds it; whoever has the seed can rebuild every input. A test
    with no verified twin among its first `twin_tries` candidates is named in `twins_refused`."""
    store = Path(store)
    tests, twins, refused = [], [], []
    for test in manifest["tests"]:
        source = Path(test["path"])
        digest, path, _ = _store_file(store, lambda scratch: reencode_file(source, scratch, seed))
        tests.append({**test, "sha256": digest, "path": str(path), "size": path.stat().st_size,
                      "encoded_from": test["sha256"]})
    for test in manifest["tests"]:
        if test.get("outcome") != "accept" or not any(fnmatch.fnmatchcase(test["name"], p) for p in twin_from):
            continue
        source = Path(test["path"])
        digest, path, twin = _store_file(store, lambda scratch: twin_file(
            source, scratch, seed, prefix_declarations=twin_prefix, verify=verify, max_tries=twin_tries))
        if twin is None:
            refused.append(test["name"])
            continue
        entry = {"name": f"fresh/{test['name']}", "outcome": "reject", "sha256": digest, "path": str(path),
                 "size": path.stat().st_size, "twin_of": test["name"], "official_sha256": None,
                 "official_match": None}
        if test.get("timeout") is not None:
            entry["timeout"] = test["timeout"]
        twins.append(entry)
    return {**manifest, "tests": tests + sorted(twins, key=lambda t: t["name"]),
            "seed_sha256": hashlib.sha256(seed.encode()).hexdigest(), "twins_refused": sorted(refused)}


def missing_inputs(results_json: Path, *, tarball: Path | None, sources, allow_mismatch) -> list[str]:
    """The round's tests that neither the test tarball nor any source directory holds with the
    published bytes (a test allowed to mismatch needs only a file): the exports to build before
    staging, as a new round brings new tests."""
    published = json.loads(Path(results_json).read_text())
    from_tarball = _tarball_members(tarball)
    missing = []
    for test in sorted(published["tests"], key=lambda t: t["name"]):
        name = test["name"]
        if name in from_tarball and hashlib.sha256(from_tarball[name]).hexdigest() == test["sha256"]:
            continue
        files = [Path(s) / f"{name}.ndjson" for s in sources]
        files = [f for f in files if f.is_file()]
        if any(_sha_file(f) == test["sha256"] for f in files) or (files and name in allow_mismatch):
            continue
        missing.append(name)
    return missing


def cmd_quiet_missing(args, lka) -> int:
    missing = missing_inputs(Path(args.published) / "results.json",
                             tarball=Path(args.published) / "lean-arena-tests.tar.gz",
                             sources=[Path(s) for s in args.source], allow_mismatch=set(args.allow_mismatch or []))
    sys.stdout.write("".join(f"{name}\n" for name in missing))
    return 0


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


def score_store(store: RunStore, tests, checkers, scoring: dict, machine: dict, extra=()) -> dict:
    """The board from a store's runs, and those of `extra` stores of the same epoch (a pull request's
    checkers, measured on their own beside the board)."""
    hashes = {c["name"]: c["source_hash"] for c in checkers}
    shas = {t["name"]: t["sha256"] for t in tests}
    runs = list(store.records()) + [r for other in extra for r in other.records()]
    current = current_records(runs, hashes, shas)
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


def read_manifest(path: Path) -> dict:
    manifest = json.loads(Path(path).read_text())
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise SystemExit(f"{path} is not a staged-tests manifest")
    return manifest


def load_manifest(path: Path) -> list[dict]:
    return read_manifest(path)["tests"]


def public_tests(tests) -> list[dict]:
    """What a board publishes of its tests while their round runs: each test's name, outcome, limit
    and the Arena's sha256, and the test an invalid twin was made from; never the bytes measured
    (their sha256, path or size), which the round's seed reveals when the round closes."""
    out = []
    for test in tests:
        entry = {"name": test["name"], "outcome": test.get("outcome"), "official_sha256": test.get("official_sha256")}
        for key in ("timeout", "twin_of"):
            if test.get(key) is not None:
                entry[key] = test[key]
        if "encoded_from" not in test and "twin_of" not in test and test.get("size") is not None:
            entry["size"] = test["size"]
        out.append(entry)
    return out


def kernel_rejects(kernel: str, timeout_s: float = 3600.0):
    """The check a twin must pass: `kernel` (the official checker) rejects the file, exit code 1."""
    def verify(path) -> bool:
        try:
            return subprocess.run([kernel, str(path)], capture_output=True, timeout=timeout_s).returncode == 1
        except subprocess.TimeoutExpired:
            return False
    return verify


def cmd_quiet_stage(args, lka) -> int:
    if args.season and not args.twin:
        import yaml
        inputs = yaml.safe_load(Path(args.season).read_text())["inputs"]
        args.twin = list(inputs["twin"])
        args.twin_prefix = args.twin_prefix or inputs["twin_prefix"]
    if args.twin and not args.verify_with:
        print("an invalid twin is staged only once the official kernel rejects it: give --verify-with", file=sys.stderr)
        return 1
    if args.twin and not args.round_seed:
        print("invalid twins are drawn from the round's seed: give --round-seed", file=sys.stderr)
        return 1
    try:
        manifest = stage_tests(Path(args.published) / "results.json",
                               tarball=Path(args.published) / "lean-arena-tests.tar.gz",
                               sources=[Path(s) for s in args.source], store=Path(args.store),
                               allow_mismatch=set(args.allow_mismatch or []), link=args.link)
    except StageError as error:
        print(error, file=sys.stderr)
        return 1
    if args.round_seed:
        seed = Path(args.round_seed).read_text().strip()
        manifest = encode_round(manifest, seed=seed, store=Path(args.store), twin_from=args.twin or [],
                                twin_prefix=args.twin_prefix or 400,
                                verify=kernel_rejects(args.verify_with) if args.verify_with else None)
        twins = sum(1 for t in manifest["tests"] if t.get("twin_of"))
        print(f"re-encoded from the round's seed (sha256 {manifest['seed_sha256'][:16]}…), {twins} invalid twins"
              + (f"; no verified twin for {', '.join(manifest['twins_refused'])}" if manifest["twins_refused"] else ""))
    Path(args.manifest).parent.mkdir(parents=True, exist_ok=True)
    Path(args.manifest).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    flagged = [t["name"] for t in manifest["tests"] if t["official_match"] is False]
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
    built, not_built = built_checkers(all_checkers, load_builds(Path(args.results), epoch))
    if not_built:
        print(f"not built at their current source, so not measured: {', '.join(c['name'] for c in not_built)}")
    checkers = [c for c in built if not args.checker or fnmatch.fnmatch(c["name"], args.checker)]
    env = run_env(os.environ)
    measure = _helper_measure(args.helper.split())
    store = RunStore(Path(args.results), epoch=epoch)
    sentinel = None
    if not args.no_sentinel:
        sentinel = _sentinel(machine, {t["name"]: t for t in load_manifest(Path(args.manifest))},
                             {c["name"]: c for c in all_checkers}, measure, env, epoch, args.round)
    session = Session(store=store, checkers=checkers, tests=tests, machine=machine, round_id=args.round, env=env,
                      measure=measure, sentinel=sentinel, log=lambda m: print(time.strftime("%H:%M:%S"), m, flush=True))
    summary = session.run(budget_s=args.budget_hours * 3600, max_passes=args.passes)
    print(json.dumps(summary))
    return 0 if summary["outcome"] in ("complete", "budget", "passes") else 3


def _builds_dir(results: Path, epoch: str) -> Path:
    return Path(results) / epoch / "builds"


def load_builds(results: Path, epoch: str) -> dict:
    directory = _builds_dir(results, epoch)
    if not directory.exists():
        return {}
    return {p.stem: json.loads(p.read_text()) for p in sorted(directory.glob("*.json"))}


def built_checkers(checkers, builds: dict) -> tuple[list, list]:
    """The checkers whose current source built, and those that did not (or were never built)."""
    ok, not_built = [], []
    for checker in checkers:
        record = builds.get(checker["name"])
        if record and record.get("ok") and record.get("source_hash") == checker["source_hash"]:
            ok.append(checker)
        else:
            not_built.append(checker)
    return ok, not_built


def cmd_quiet_build(args, lka) -> int:
    import fnmatch
    epoch = Path(args.epoch_file).read_text().strip()
    checkers = [c for c in _checkers(lka, None) if fnmatch.fnmatch(c["name"], args.checker)]
    builds = load_builds(Path(args.results), epoch)
    directory = _builds_dir(Path(args.results), epoch)
    directory.mkdir(parents=True, exist_ok=True)
    failed = 0
    for checker in checkers:
        previous = builds.get(checker["name"])
        if not args.force and previous and previous.get("ok") and previous.get("source_hash") == checker["source_hash"]:
            print(f"{checker['name']}: built already at {checker['source_hash'][:12]}")
            continue
        request = {"checker": checker["name"], "arena": args.arena, "timeout_s": args.timeout_hours * 3600}
        result = subprocess.run(args.helper.split() + ["--build"], input=json.dumps(request), capture_output=True, text=True)
        try:
            record = json.loads(result.stdout)
        except json.JSONDecodeError:
            record = {"checker": checker["name"], "ok": False, "exit_code": result.returncode,
                      "log_tail": result.stderr[-16384:]}
        record.update({"source_hash": checker["source_hash"], "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        (directory / f"{checker['name']}.json").write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
        failed += not record["ok"]
        print(f"{checker['name']}: {'built' if record['ok'] else 'FAILED'} in {record.get('duration_s', 0):.0f} s")
    return 1 if failed else 0


def cmd_quiet_score(args, lka) -> int:
    import yaml
    scoring = yaml.safe_load(Path(args.season).read_text())
    machine = yaml.safe_load(Path(args.machine).read_text())
    epoch = Path(args.epoch_file).read_text().strip() if args.epoch_file else args.epoch
    manifest = read_manifest(Path(args.manifest))
    tests = manifest["tests"]
    checkers = _checkers(lka, None)
    extra = [RunStore(Path(d), epoch=epoch) for d in args.extra_results or []]
    board = score_store(RunStore(Path(args.results), epoch=epoch), tests, checkers, scoring, machine, extra=extra)
    board["tests"] = public_tests(tests)
    if manifest.get("seed_sha256"):
        board["round_inputs"] = {"seed_sha256": manifest["seed_sha256"],
                                 "twins": sum(1 for t in tests if t.get("twin_of")),
                                 "twins_refused": manifest.get("twins_refused", [])}
    builds = load_builds(Path(args.results), epoch)
    board["builds"] = {c["name"]: {k: builds.get(c["name"], {}).get(k) for k in ("ok", "source_hash", "built_at", "duration_s")}
                       for c in checkers}
    board["checker_meta"] = {c["name"]: {k: c.get(k) for k in ("url", "ref", "rev", "version", "threads", "declines")}
                             for c in checkers}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(board, indent=1, sort_keys=True) + "\n")
    print(f"scored {len(board['checkers'])} checkers, {board['progress']['measured']}/{board['progress']['needed']} cells")
    return 0


def cmd_quiet_pr_stage(args, lka) -> int:
    import jsonschema
    from .pr import PRError, stage_pr_arena
    schema = lka.load_schema("checker")
    try:
        names = stage_pr_arena(Path(args.base), Path(args.head), args.number, Path(args.out),
                               validate=lambda data, path: jsonschema.validate(data, schema))
    except PRError as error:
        print(error, file=sys.stderr)
        return 1
    Path(args.names_file).write_text("".join(f"{n}\n" for n in names))
    print(f"staged {len(names)} checker(s) of pull request {args.number}: {', '.join(names) or 'none'}")
    return 0


def cmd_quiet_pr_report(args, lka) -> int:
    import fnmatch
    from .pr import pr_report
    board = json.loads(Path(args.board).read_text())
    names = [c["name"] for c in board["checkers"] if fnmatch.fnmatchcase(c["name"], args.checkers)]
    sys.stdout.write(pr_report(board, names, machine=args.machine_name))
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
    stage.add_argument("--round-seed", metavar="FILE",
                       help="The round's secret seed: measure every input re-encoded from it (only its sha256 is recorded)")
    stage.add_argument("--twin", action="append", default=[], metavar="PATTERN",
                       help="Add an invalid twin fresh/<test> of each test to be accepted matching PATTERN (repeatable)")
    stage.add_argument("--twin-prefix", type=int, metavar="N",
                       help="A twin's swapped declaration is among the first N of its source (default: the season's, else 400)")
    stage.add_argument("--season", metavar="FILE",
                       help="Take the tests to twin and the prefix from the season's inputs (with --round-seed)")
    stage.add_argument("--verify-with", metavar="KERNEL",
                       help="The official checker; a twin is staged only if it rejects it (required with --twin)")

    missing = subparsers.add_parser("quiet-missing", help="Name the round's tests no tarball or export holds")
    missing.add_argument("--published", required=True, metavar="DIR")
    missing.add_argument("--source", action="append", default=[], metavar="DIR")
    missing.add_argument("--allow-mismatch", action="append", metavar="TEST")

    measure = subparsers.add_parser("quiet-measure", help="Run a quiet-board measurement session")
    measure.add_argument("--machine", required=True, help="Machine profile (machines/*.yaml)")
    measure.add_argument("--manifest", required=True, help="Staged-tests manifest (see quiet-stage)")
    measure.add_argument("--results", required=True, help="Results store directory")
    measure.add_argument("--epoch-file", default="/etc/lka/epoch", help="File holding the machine epoch")
    measure.add_argument("--round", required=True, help="Round name, e.g. 2026-11")
    measure.add_argument("--checker", help="Glob of checkers to measure (default: all)")
    measure.add_argument("--test", nargs="+", help="Globs of tests to measure (default: all staged)")
    measure.add_argument("--budget-hours", type=float, default=20.0)
    measure.add_argument("--passes", type=int, help="Stop after this many passes (each pending cell once per pass)")
    measure.add_argument("--helper", default="sudo -n /usr/local/libexec/lka/lka-measure",
                         help="Command that measures one request (JSON on stdin)")
    measure.add_argument("--calibrating", action="store_true", help="Measure before the machine is calibrated")
    measure.add_argument("--no-sentinel", action="store_true")

    build = subparsers.add_parser("quiet-build", help="Build checkers on the quiet box, each as its own user")
    build.add_argument("--checker", default="*", help="Glob of checkers to build (default: all)")
    build.add_argument("--arena", default="/srv/lka/arena", help="The staged arena tree to build from")
    build.add_argument("--results", required=True, help="Results store directory (build records are kept there)")
    build.add_argument("--epoch-file", default="/etc/lka/epoch")
    build.add_argument("--timeout-hours", type=float, default=3.0)
    build.add_argument("--force", action="store_true", help="Rebuild even if this source built already")
    build.add_argument("--helper", default="sudo -n /usr/local/libexec/lka/lka-measure")

    pr_stage = subparsers.add_parser("quiet-pr-stage", help="Stage a pull request's checkers beside master's")
    pr_stage.add_argument("--base", required=True, help="master's tree (the board's code comes from here alone)")
    pr_stage.add_argument("--head", required=True, help="The pull request's head, read as data")
    pr_stage.add_argument("--number", required=True, type=int, help="The pull request's number")
    pr_stage.add_argument("--out", required=True, help="Where to stage the tree, state/pr/<number>/arena")
    pr_stage.add_argument("--names-file", required=True, help="File to list the staged checkers' names in")

    pr_report = subparsers.add_parser("quiet-pr-report", help="The pull request comment from a scored board")
    pr_report.add_argument("--board", required=True)
    pr_report.add_argument("--checkers", required=True, help="Glob of the PR's checkers, pr<N>-*")
    pr_report.add_argument("--machine-name", default="quiet box")

    score = subparsers.add_parser("quiet-score", help="Score a quiet-board results store into board.json")
    score.add_argument("--season", required=True, help="Scoring rules (scoring/*.yaml)")
    score.add_argument("--machine", required=True, help="Machine profile (machines/*.yaml)")
    score.add_argument("--manifest", required=True)
    score.add_argument("--results", required=True)
    score.add_argument("--extra-results", action="append", metavar="DIR",
                       help="Another results store of the same epoch (a pull request's) to score beside it")
    score.add_argument("--epoch-file")
    score.add_argument("--epoch")
    score.add_argument("--out", default="_build/board.json")


COMMANDS = {"quiet-stage": cmd_quiet_stage, "quiet-missing": cmd_quiet_missing, "quiet-build": cmd_quiet_build, "quiet-measure": cmd_quiet_measure,
            "quiet-score": cmd_quiet_score, "quiet-pr-stage": cmd_quiet_pr_stage, "quiet-pr-report": cmd_quiet_pr_report}
