#!/usr/bin/env python3
"""Small static lint (developer and CI script, ``ast`` only, stdlib only).

Not part of the ``timelinexray`` package and never imported by it. It reports, for every
``.py`` file under ``src`` and ``tests`` (and ``scripts``):

* unused imports (F401-style). Not reported: ``from __future__`` imports, names listed in
  ``__all__``, every import in an ``__init__.py`` (re-exports of the package surface), names
  used in annotations or strings of annotations (including ``TYPE_CHECKING`` blocks), and
  lines carrying ``# noqa`` or ``# noqa: F401``;
* unused top-level private names (``_name`` functions, classes and assignments that no
  other name in the same file refers to), except dunder names and ``# noqa`` lines.

    python scripts/check_lint.py [--root DIR]

Exit status 0 when clean, 1 otherwise (one ``path:line: message`` per problem).
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TREES = ("src", "tests", "scripts")
NOQA = re.compile(r"#\s*noqa\b(?::\s*([A-Z0-9, ]+))?")


def _noqa(lines: list[str], lineno: int, code: str) -> bool:
    match = NOQA.search(lines[lineno - 1]) if 0 < lineno <= len(lines) else None
    if not match:
        return False
    return match.group(1) is None or code in [c.strip() for c in match.group(1).split(",")]


def _all_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            for item in ast.walk(value):
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    names.add(item.value)
    return names


def _used_names(tree: ast.Module) -> set[str]:
    """Every name read anywhere, including names inside string annotations."""
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            continue
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # string annotations / forward references: harvest identifiers cheaply
            if len(node.value) < 200:
                used.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", node.value))
    return used


def check_source(text: str, name: str, *, package_init: bool = False) -> list[str]:
    tree = ast.parse(text, name)
    lines = text.splitlines()
    exported = _all_names(tree)
    used = _used_names(tree)
    problems: list[str] = []
    if not package_init:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name.split(".")[0]
                if bound in used or bound in exported:
                    continue
                if alias.asname and alias.asname == alias.name:
                    continue  # explicit re-export spelling: ``import x as x``
                if _noqa(lines, node.lineno, "F401"):
                    continue
                problems.append(f"{name}:{node.lineno}: unused import {bound}")
    defined: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.setdefault(node.name, node.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    defined.setdefault(target.id, node.lineno)
    for private, lineno in sorted(defined.items(), key=lambda item: item[1]):
        if not private.startswith("_") or (private.startswith("__") and private.endswith("__")):
            continue
        if private in used or private in exported or _noqa(lines, lineno, "F811"):
            continue
        problems.append(f"{name}:{lineno}: unused private name {private}")
    return problems


def check(root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    for tree_name in TREES:
        for path in sorted((root / tree_name).rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            problems += check_source(path.read_text("utf-8"), relative,
                                     package_init=path.name == "__init__.py")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report unused imports and unused private names.")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root to check")
    args = parser.parse_args(argv)
    problems = check(args.root)
    for problem in problems:
        print(problem)
    if problems:
        print(f"{len(problems)} problem(s)", file=sys.stderr)
        return 1
    print("lint: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
