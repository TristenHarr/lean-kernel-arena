#!/usr/bin/env python3
"""Gather a round's large exports for the quiet box, from wherever they were built.

    quietbox/gather-exports.py --results results.json --tarball lean-arena-tests.tar.gz \\
        --search ~/exports --search /mnt/build/arena-inputs --out ~/quiet-box-exports \\
        --allow-mismatch cedar=cedar.mismatch.ndjson

Every test the round publishes outside its test tarball (Mathlib, the other libraries) is found by
its published sha256 among the files of the same size under the --search directories, and hard-linked
(copied across file systems) into --out as <test>.ndjson, the name quiet-stage reads. A test whose
export is not deterministic may be named with --allow-mismatch and is then linked by its file name and
reported. SHA256SUMS.json in --out records each file's sha256, so the box can check what it was sent.
Exit status 1 if any test is missing.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
from pathlib import Path


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 22):
            digest.update(chunk)
    return digest.hexdigest()


def large_tests(results: dict, tarball_members) -> list[str]:
    """The tests a round publishes outside its test tarball."""
    return sorted(t["name"] for t in results["tests"] if t["name"] not in tarball_members)


def tarball_members(tarball: Path) -> set[str]:
    with tarfile.open(tarball, "r:gz") as tar:
        names = set()
        for member in tar.getmembers():
            for prefix in ("good/", "bad/"):
                if member.isfile() and member.name.startswith(prefix) and member.name.endswith(".ndjson"):
                    names.add(member.name[len(prefix):-len(".ndjson")])
        return names


def _place(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if os.stat(target).st_ino == os.stat(source).st_ino:
            return
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def gather(results: dict, roots, out: Path, *, names, allow_mismatch: dict) -> dict:
    out = Path(out)
    by_size: dict[int, list[Path]] = {}
    by_name: dict[str, list[Path]] = {}
    for root in roots:
        for path in sorted(Path(root).rglob("*.ndjson")):
            if path.is_file() and not path.is_symlink():
                by_size.setdefault(path.stat().st_size, []).append(path)
                by_name.setdefault(path.name, []).append(path)
    tests = {t["name"]: t for t in results["tests"]}
    hashed: dict[Path, str] = {}
    found, mismatched, missing, sums, total = [], [], [], {}, 0
    for name in names:
        test = tests[name]
        match = None
        for path in by_size.get(test.get("size"), []):
            if path not in hashed:
                hashed[path] = _sha_file(path)
            if hashed[path] == test["sha256"]:
                match = path
                break
        target = out / f"{name}.ndjson"
        if match is not None:
            _place(match, target)
            found.append(name)
            sums[name] = test["sha256"]
            total += match.stat().st_size
        elif name in allow_mismatch and by_name.get(allow_mismatch[name]):
            source = by_name[allow_mismatch[name]][0]
            _place(source, target)
            mismatched.append(name)
            sums[name] = _sha_file(source)
            total += source.stat().st_size
        else:
            missing.append(name)
    out.mkdir(parents=True, exist_ok=True)
    (out / "SHA256SUMS.json").write_text(json.dumps(sums, indent=1, sort_keys=True) + "\n")
    return {"found": found, "mismatched": mismatched, "missing": missing, "bytes": total}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", required=True, help="The round's results.json")
    parser.add_argument("--tarball", required=True, help="The round's lean-arena-tests.tar.gz")
    parser.add_argument("--search", action="append", required=True, metavar="DIR")
    parser.add_argument("--out", required=True)
    parser.add_argument("--allow-mismatch", action="append", default=[], metavar="TEST=FILE")
    args = parser.parse_args()
    results = json.loads(Path(args.results).read_text())
    allow = dict(item.split("=", 1) for item in args.allow_mismatch)
    names = large_tests(results, tarball_members(Path(args.tarball)))
    report = gather(results, [Path(d) for d in args.search], Path(args.out), names=names, allow_mismatch=allow)
    print(f"{len(report['found'])} exports found by sha256, {report['bytes'] / 2**30:.1f} GiB in {args.out}")
    if report["mismatched"]:
        print(f"not the official bytes (allowed): {', '.join(report['mismatched'])}")
    if report["missing"]:
        print(f"MISSING: {', '.join(report['missing'])}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
