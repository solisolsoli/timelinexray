"""Two-commit diffs and change classification (Milestone 5b).

* :class:`DiffEngine` - path, blob and line-level differences between two pinned commits,
  read from git objects (never a working tree), attributed to Milestone 2 symbols and
  classified into :data:`CLASSES` with citations on both commits.
* :class:`SymbolSource` - Milestone 2 symbols from the code index when the commit is
  indexed, else from the same syntax registry.
* :mod:`timelinexray.diff.history` - lineage between pins and exceptional events.

Commit messages are never read or used as evidence. Classification is mechanical and
heuristic; an item states what the rule saw, not a reviewed interpretation.
"""

from __future__ import annotations

from .engine import DiffEngine
from .history import Event, Lineage, is_ancestor
from .model import ChangeItem, Citation, CommitDiff, CommitRef, FileChange, Hunk
from .rules import CLASS_EVIDENCE, CLASS_TITLES, CLASSES, CLASSIFIER_VERSION
from .symbols import SymbolSource

__all__ = [
    "CLASSES",
    "CLASSIFIER_VERSION",
    "CLASS_EVIDENCE",
    "CLASS_TITLES",
    "ChangeItem",
    "Citation",
    "CommitDiff",
    "CommitRef",
    "DiffEngine",
    "Event",
    "FileChange",
    "Hunk",
    "Lineage",
    "SymbolSource",
    "is_ancestor",
]
