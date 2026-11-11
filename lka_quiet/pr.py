"""A pull request on the quiet box.

The workflow that runs it always comes from master, and so does every line of the board's own code;
the pull request's head is only read, as data. The checkers it adds or changes are staged into a
copy of master's tree under the names pr<N>-<name> (with their own users, homes and build records,
so a PR's build can never stand in for the board's), built in the sandbox, measured on the round's
inputs beside every checker on the board, and reported on the PR.

    lka.py quiet-pr-stage    stage a PR's checkers into state/pr/<N>/arena
    lka.py quiet-pr-report   the PR comment, from a board scored with the PR's runs
"""

import filecmp
import shutil
from pathlib import Path

import yaml

from .charts import factor_text

_LEFT_BEHIND = shutil.ignore_patterns(".git", "_build", "_out", "_results", "__pycache__")


class PRError(ValueError):
    pass


def _checker_files(root: Path) -> dict[str, list[Path]]:
    """Each checker's definition and, if it names one, its own directory's files."""
    out = {}
    for definition in sorted((root / "checkers").glob("*.yaml")):
        name = definition.stem
        files = [definition]
        data = yaml.safe_load(definition.read_text()) or {}
        directory = data.get("dir") if isinstance(data, dict) else None
        if isinstance(directory, str) and (root / "checkers" / directory).is_dir():
            files += sorted(p for p in (root / "checkers" / directory).rglob("*") if p.is_file() or p.is_symlink())
        out[name] = files
    return out


def _same(base: Path, head: Path, base_files: list[Path], head_files: list[Path]) -> bool:
    relative = lambda root, files: [f.relative_to(root) for f in files]
    if relative(base, base_files) != relative(head, head_files):
        return False
    for a, b in zip(base_files, head_files):
        if a.is_symlink() or b.is_symlink():
            if not (a.is_symlink() and b.is_symlink() and a.readlink() == b.readlink()):
                return False
        elif not filecmp.cmp(a, b, shallow=False):
            return False
    return True


def changed_checkers(base: Path, head: Path) -> list[str]:
    """The checkers the head adds or changes (definition or own directory); removals are not run."""
    base, head = Path(base), Path(head)
    before, after = _checker_files(base), _checker_files(head)
    return sorted(name for name, files in after.items()
                  if name not in before or not _same(base, head, before[name], files))


def stage_pr_arena(base: Path, head: Path, number: int, out: Path, *, validate) -> list[str]:
    """master's tree at `out`, with each checker the PR adds or changes beside the others as
    pr<number>-<name>: its definition, and its own directory under the new name. `validate(data,
    path)` (lka.py's schema) must accept every one; the names staged are returned."""
    base, head, out = Path(base), Path(head), Path(out)
    names = changed_checkers(base, head)
    problems = []
    for name in names:
        definition = head / "checkers" / f"{name}.yaml"
        try:
            validate(yaml.safe_load(definition.read_text()), definition.relative_to(head))
        except Exception as error:
            problems.append(f"checkers/{name}.yaml: {error}")
    if problems:
        raise PRError("the pull request's checkers are not valid:\n  " + "\n  ".join(problems))
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(base, out, symlinks=True, ignore=_LEFT_BEHIND)
    staged = []
    for name in names:
        new = f"pr{number}-{name}"
        data = yaml.safe_load((head / "checkers" / f"{name}.yaml").read_text())
        directory = data.get("dir")
        if isinstance(directory, str) and (head / "checkers" / directory).is_dir():
            shutil.copytree(head / "checkers" / directory, out / "checkers" / new, symlinks=True)
            data["dir"] = new
        (out / "checkers" / f"{new}.yaml").write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        staged.append(new)
    return staged


def pr_report(board: dict, names, *, machine: str) -> str:
    """The comment on the pull request: each of its checkers' correctness, then its place, range and
    factor in every category, against every checker on the board."""
    entries = {e["name"]: e for e in board["checkers"]}
    placed = {c["id"]: sum(1 for e in board["checkers"] if e["categories"][c["id"]].get("place") is not None)
              for c in board["categories"]}
    lines = [f"Measured on the {machine}, on this round's inputs, beside every checker on the board."]
    for name in names:
        entry = entries[name]
        lines += ["", f"### {name}"]
        gates = entry["gates"]
        if gates["unsound"]:
            lines.append(f"**Not ranked: accepted {len(gates['unsound'])} invalid proof(s)**: {', '.join(gates['unsound'])}")
        if gates["wrong_rejects"]:
            lines.append(f"**Not ranked: rejected {len(gates['wrong_rejects'])} valid proof(s)**: "
                         f"{', '.join(gates['wrong_rejects'])}")
        c = entry["correctness"]
        lines.append(f"Valid proofs accepted {c['good_accepted']} of {c['good_total']}, invalid proofs rejected "
                     f"{c['bad_rejected']} of {c['bad_total']}, declined or failed {c['declined_or_failed']}.")
        lines += ["", "| Category | Place | Under a redrawn suite | × " + board["reference"] + " |", "|---|---|---|---|"]
        for category in board["categories"]:
            result = entry["categories"][category["id"]]
            place = "–" if result.get("place") is None else \
                f"{result['place']}{'=' if result.get('place_tied') else ''} of {placed[category['id']]}"
            spread = f"{result['rank_lo']}–{result['rank_hi']}" if result.get("rank_lo") is not None else "–"
            factor = factor_text(result["factor"]) if result.get("factor") else "–"
            lines.append(f"| {category['title']} | {place} | {spread} | {factor} |")
    return "\n".join(lines) + "\n"
