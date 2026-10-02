"""Module boundaries: no package reaches into another package's private helpers.

A *unit* is a first-level subpackage of ``timelinexray`` (``timelinexray.diff``,
``timelinexray.syntax``, ...); the modules directly inside ``timelinexray`` form the root
unit. Across units, code may only use public names: no ``from x import _name``, no import
of a private module (``_brace``, ``_python``) and no ``module._name`` attribute access on a
module imported from another unit. Shared helpers are published as small public internal
APIs instead (for example ``timelinexray.fsutil.atomic_write``,
``timelinexray.syntax.extract_checked`` and ``timelinexray.syntax.source``).
"""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO_ROOT

PACKAGE_ROOT = REPO_ROOT / "src" / "timelinexray"


def _module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _unit(module: str, root: Path) -> str:
    parts = module.split(".")
    if len(parts) >= 2 and (root / parts[1]).is_dir():
        return ".".join(parts[:2])
    return parts[0]


def _private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _is_module(qualified: str, root: Path) -> bool:
    relative = Path(*qualified.split(".")[1:])
    return (root / relative).with_suffix(".py").is_file() or (
        root / relative / "__init__.py").is_file()


def _resolve(current: str, is_package: bool, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    base = current.split(".")
    if not is_package:
        base = base[:-1]
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def violations(root: Path = PACKAGE_ROOT) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        module = _module_name(path, root)
        own = _unit(module, root)
        tree = ast.parse(path.read_text("utf-8"), str(path))
        foreign_aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                target = _resolve(module, path.name == "__init__.py", node)
                if not target.startswith("timelinexray"):
                    continue
                for alias in node.names:
                    full = f"{target}.{alias.name}"
                    candidate = full if _is_module(full, root) else target
                    if _unit(candidate, root) == own:
                        continue
                    if any(_private(part) for part in candidate.split(".")) or _private(alias.name):
                        found.append(f"{module}: imports {full}")
                    if _is_module(full, root):
                        foreign_aliases[alias.asname or alias.name] = full
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("timelinexray") and _unit(alias.name, root) != own:
                        if any(_private(part) for part in alias.name.split(".")):
                            found.append(f"{module}: imports {alias.name}")
                        if alias.asname:
                            foreign_aliases[alias.asname] = alias.name
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in foreign_aliases and _private(node.attr)):
                found.append(f"{module}: uses {foreign_aliases[node.value.id]}.{node.attr}")
    return found


class ModuleBoundaryTest(unittest.TestCase):
    def test_no_cross_package_private_access(self) -> None:
        self.assertEqual(violations(), [])

    def test_the_checker_finds_private_access(self) -> None:
        probe = ("from ..syntax._brace import match_delimiters\n"
                 "from ..snapshot.store import _atomic_write\n"
                 "from ..syntax.source import mask\n"
                 "from ..index import build as b\nb._extract(1)\n")
        with tempfile.TemporaryDirectory(prefix="txray-boundary-") as tmp:
            root = Path(tmp) / "timelinexray"
            files = {"__init__.py": "", "diff/__init__.py": "", "diff/probe.py": probe,
                     "syntax/__init__.py": "", "syntax/_brace.py": "", "syntax/source.py": "",
                     "snapshot/__init__.py": "", "snapshot/store.py": "",
                     "index/__init__.py": "", "index/build.py": "def _extract(x): pass\n"}
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, "utf-8")
            found = violations(root)
        self.assertEqual(found, [
            "timelinexray.diff.probe: imports timelinexray.syntax._brace.match_delimiters",
            "timelinexray.diff.probe: imports timelinexray.snapshot.store._atomic_write",
            "timelinexray.diff.probe: uses timelinexray.index.build._extract",
        ])

    def test_public_internal_apis_exist(self) -> None:
        from timelinexray import fsutil, syntax
        from timelinexray.syntax import source

        self.assertTrue(callable(fsutil.atomic_write))
        self.assertTrue(callable(syntax.extract_checked))
        self.assertEqual(sorted(source.__all__), ["Lines", "Masked", "mask", "match_delimiters"])


if __name__ == "__main__":
    unittest.main()
