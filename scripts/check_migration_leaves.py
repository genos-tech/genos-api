#!/usr/bin/env python3
"""Fail when an app's migrations have more than one leaf (MIP-1098).

Two PRs that each add `0220_*` pass CI alone and only conflict once both are
on `main`: `manage.py migrate` then refuses to run ("Conflicting migrations").
This finds that from the files alone, with the standard library, so it runs in
seconds with no install: it reads every `dependencies = [...]` with `ast`.

usage: check_migration_leaves.py [repo_root]   (default: the current directory)
exit 0: one leaf per app.  exit 1: a conflict, printed.  exit 2: could not read.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _deps(path: Path) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(dependencies, replaces) of the `Migration` class in `path`, as
    (app_label, name) pairs. Anything not a literal pair is ignored (a
    `swappable_dependency(...)` call names a setting, not a migration)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    out: dict[str, set[tuple[str, str]]] = {"dependencies": set(), "replaces": set()}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Migration":
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    target = stmt.targets[0]
                    if isinstance(target, ast.Name) and target.id in out:
                        if not isinstance(stmt.value, (ast.List, ast.Tuple)):
                            continue
                        # One element at a time: a list that also holds a call such as
                        # `migrations.swappable_dependency(...)` is not a literal as a whole.
                        for element in stmt.value.elts:
                            try:
                                item = ast.literal_eval(element)
                            except (ValueError, SyntaxError):
                                continue
                            if isinstance(item, (tuple, list)) and len(item) == 2:
                                out[target.id].add((str(item[0]), str(item[1])))
    return out["dependencies"], out["replaces"]


def leaves_by_app(root: Path) -> dict[str, list[str]]:
    """{app label: leaf migration names}, one app per `migrations/` folder found."""
    found: dict[str, list[str]] = {}
    for folder in sorted(root.rglob("migrations")):
        if not folder.is_dir() or ".venv" in folder.parts or "node_modules" in folder.parts:
            continue
        files = sorted(p for p in folder.glob("[0-9]*.py"))
        if not files:
            continue
        label = folder.parent.name  # the app package: origin, search_engine
        names = {p.stem for p in files}
        depended_on: set[str] = set()
        for path in files:
            deps, replaces = _deps(path)
            # Only a dependency naming THIS app counts: `0001_initial` exists in every app, so
            # matching on the name alone let one app's dependency retire another app's leaf.
            depended_on |= {n for (app, n) in deps if app == label and n in names}
            # A squashed migration replaces others: they are no longer leaves.
            depended_on |= {n for (app, n) in replaces if app == label and n in names}
        found[label] = sorted(names - depended_on)
    return found


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path.cwd()
    try:
        found = leaves_by_app(root)
    except (OSError, SyntaxError) as exc:
        print(f"could not read the migrations: {exc}", file=sys.stderr)
        return 2
    if not found:
        print("no migrations folder found", file=sys.stderr)
        return 2
    bad = {app: leaves for app, leaves in found.items() if len(leaves) != 1}
    for app, leaves in sorted(found.items()):
        print(f"{app}: {', '.join(leaves)}")
    if bad:
        for app, leaves in sorted(bad.items()):
            print(
                f"\nCONFLICT in {app}: {len(leaves)} leaf migrations ({', '.join(leaves)}). "
                "`manage.py migrate` will refuse to run. Rebase and renumber, or add a "
                "merge migration.",
                file=sys.stderr,
            )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
