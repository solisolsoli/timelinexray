"""Synthetic history repository and helpers for the findings memory tests.

The fixture is a small bare repository with a base commit and one commit per controlled
mutation (P7 section 7.4): line shift, pure rename, rename with an unrelated edit, changed
literal, duplicated line, changed dependency, a newly added matching file for a negative
finding, a deleted file, and cited bytes that survive only inside a longer line. Every mutation commit holds the base files plus exactly one
change and is chained on the previous commit, so ``main`` reaches all of them.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from timelinexray.findings import Actor, FindingsMemory, Ledger
from timelinexray.index import CodeIndex
from timelinexray.netguard import Allowlist
from timelinexray.snapshot import SnapshotStore
from tests.index_support import add_commit
from tests.support import build_fixture_repo, file_url
from tests.templates import copy_into, metadata, template, write_metadata

WEIGHTS = (
    b"// Synthetic weights for TimelineXray tests.\n"
    b"pub const FAVORITE_WEIGHT: f64 = 0.5;\n"
    b"pub const REPLY_WEIGHT: f64 = 5.0;\n"
    b"pub const CLICK_WEIGHT: f64 = 0.4;\n"
    b"\n"
    b"pub fn score(favorite: f64, reply: f64, click: f64) -> f64 {\n"
    b"    favorite * FAVORITE_WEIGHT + reply * REPLY_WEIGHT + click * CLICK_WEIGHT\n"
    b"}\n"
)
FILTERS = (
    b'FILTERS = [\n'
    b'    "DropDuplicates",\n'
    b'    "AgeFilter",\n'
    b']\n'
    b'\n'
    b'def apply(candidates):\n'
    b'    return [c for c in candidates if c]\n'
)
NOTES = b"# Notes\n\nSynthetic notes for the findings tests.\n"

BASE: dict[str, bytes] = {
    "src/weights.rs": WEIGHTS,
    "src/filters.py": FILTERS,
    "docs/notes.md": NOTES,
}

CLICK_LINE = b"pub const CLICK_WEIGHT: f64 = 0.4;\n"

MUTATIONS: dict[str, dict[str, bytes | None]] = {
    # three lines inserted above the cited constants: CLICK_WEIGHT moves from line 4 to 7
    "shift": {"src/weights.rs": WEIGHTS.replace(
        b"tests.\n", b"tests.\n// padding one\n// padding two\n// padding three\n", 1)},
    # the file moves unchanged
    "rename": {"src/weights.rs": None, "src/params/weights.rs": WEIGHTS},
    # the file moves and an unrelated line changes (so its blob id changes)
    "rename_edit": {"src/weights.rs": None, "src/params/weights.rs": WEIGHTS.replace(
        b"REPLY_WEIGHT: f64 = 5.0", b"REPLY_WEIGHT: f64 = 6.0")},
    # the cited literal changes
    "literal": {"src/weights.rs": WEIGHTS.replace(b"CLICK_WEIGHT: f64 = 0.4", b"CLICK_WEIGHT: f64 = 0.3")},
    # the cited line is duplicated
    "duplicate": {"src/weights.rs": WEIGHTS + b"\n" + CLICK_LINE},
    # only the span dependency (the body of apply) changes
    "dependency": {"src/filters.py": FILTERS.replace(b"if c]", b"if c and c.visible]")},
    # a new file matches the negative search
    "negative": {"src/boost.py": b"BOOST_WEIGHT = 2.0\n"},
    # the cited file is deleted and its content appears nowhere
    "delete": {"src/weights.rs": None},
    # the cited bytes survive only inside a longer line (not a whole-line match)
    "embedded": {"src/weights.rs": WEIGHTS.replace(CLICK_LINE, b"// was: " + CLICK_LINE)},
}


def _files(mutation: dict[str, bytes | None]) -> dict[str, bytes]:
    files = dict(BASE)
    for path, content in mutation.items():
        if content is None:
            files.pop(path, None)
        else:
            files[path] = content
    return files


class HistoryRepo:
    """The history fixture, pinned in one store, with a findings ledger beside it."""

    def __init__(self) -> None:
        # Deterministic commit ids (fixed identity and dates): the repository and its pinned
        # store are built once per run; the store is copied, the upstream repository is
        # shared read-only in place (the store records its URL; no test changes it).
        shared = template("history", self._build)
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-findings-")
        self.root = Path(self._tmp.name)
        self.git_dir = shared / "upstream" / "fixture.git"
        self.commits: dict[str, str] = metadata(shared)["commits"]
        self.url = file_url(self.git_dir)
        self.allowlist = Allowlist([self.url])
        self.store_dir = self.root / "store"
        copy_into(shared / "store", self.store_dir)
        self.store = SnapshotStore(self.store_dir)
        self.index = CodeIndex(self.store)
        self._indexed: set[str] = set()
        self._ledgers = 0

    @staticmethod
    def _build(directory: Path) -> None:
        git_dir, base = build_fixture_repo(directory / "upstream", BASE, frozenset())
        commits: dict[str, str] = {"base": base}
        parent = base
        for name, mutation in MUTATIONS.items():
            parent = add_commit(git_dir, _files(mutation), parent)
            commits[name] = parent
        url = file_url(git_dir)
        store = SnapshotStore(directory / "store")
        for commit in commits.values():
            store.pin(commit, url, allowlist=Allowlist([url]))
        write_metadata(directory, {"commits": commits})

    def indexed(self, *names: str) -> None:
        for name in names:
            if name not in self._indexed:
                self.index.build(self.commits[name])
                self._indexed.add(name)

    def memory(self, clock: Any = None) -> FindingsMemory:
        """A fresh, empty findings memory over the shared store and index."""
        self._ledgers += 1
        directory = self.root / f"ledger-{self._ledgers}"
        ledger = Ledger(directory, clock=clock) if clock else Ledger(directory)
        return FindingsMemory(ledger, self.store, self.index)

    def cite(self, name: str, path: str, lines: str, anchor: str) -> dict[str, Any]:
        return {"commit": self.commits[name], "path": path, "lines": lines, "anchor": anchor}

    def cleanup(self) -> None:
        self._tmp.cleanup()


AUTHOR = Actor("agent-a", "author")
REVIEWER = Actor("reviewer-b", "reviewer")


def spec(repo: HistoryRepo, **overrides: Any) -> dict[str, Any]:
    """A valid ``add`` specification citing CLICK_WEIGHT at the base commit."""
    data: dict[str, Any] = {
        "title": "Synthetic click weight default",
        "claim": "The synthetic fixture sets CLICK_WEIGHT to 0.4 as a public default.",
        "component": "src",
        "evidence_class": "PARAM_DEFAULT",
        "status": "SUPPORTED",
        "citations": [repo.cite("base", "src/weights.rs", "4", "CLICK_WEIGHT")],
    }
    data.update(overrides)
    return data
