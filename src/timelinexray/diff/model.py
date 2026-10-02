"""Records of a two-commit diff: citations, hunks, file changes and classified items.

Every record serialises to plain JSON types with :meth:`to_dict`; key order is fixed by
the callers (``sort_keys``), list order is part of the contract and deterministic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

#: Citation roles. ``span`` cites the changed lines themselves; ``context`` cites the line
#: next to a pure insertion or deletion (nothing changed on that side); ``absent`` means the
#: path does not exist at that commit; ``empty`` means the blob has no lines; ``not-text``
#: means the entry is not read as text (binary, oversize, symlink, submodule).
SPAN = "span"
CONTEXT = "context"
ABSENT = "absent"
EMPTY = "empty"
NOT_TEXT = "not-text"


@dataclass(frozen=True, slots=True)
class Citation:
    """An exact, commit-pinned line span (or an explicit reason why there is none)."""

    commit: str
    path: str | None
    role: str
    start_line: int | None = None
    end_line: int | None = None
    span_sha256: str | None = None
    blob_oid: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "path": self.path,
            "role": self.role,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "span_sha256": self.span_sha256,
            "blob_oid": self.blob_oid,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class Hunk:
    """One changed region: lines ``old_start..`` replaced by lines ``new_start..``.

    As in unified diffs, a side with count 0 has ``start`` = the line after which the
    other side's lines are inserted (0 = before the first line).
    """

    old_start: int
    old_count: int
    new_start: int
    new_count: int

    @property
    def old_lines(self) -> range:
        return range(self.old_start, self.old_start + self.old_count) if self.old_count else range(0)

    @property
    def new_lines(self) -> range:
        return range(self.new_start, self.new_start + self.new_count) if self.new_count else range(0)

    def to_dict(self) -> dict[str, int]:
        return {
            "old_start": self.old_start,
            "old_count": self.old_count,
            "new_start": self.new_start,
            "new_count": self.new_count,
        }


@dataclass(frozen=True, slots=True)
class CommitRef:
    commit: str
    tree: str
    committer_time: str
    manifest_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "tree": self.tree,
            "committer_time": self.committer_time,
            "manifest_sha256": self.manifest_sha256,
        }


@dataclass(frozen=True, slots=True)
class ChangeItem:
    """One classified change with citations on both commits."""

    change_class: str
    kind: str
    status: str  # file status: added, removed, modified, renamed
    old_path: str | None
    new_path: str | None
    summary: str
    old: Citation
    new: Citation
    detail: dict[str, Any] = field(default_factory=dict)
    old_symbols: tuple[str, ...] = ()
    new_symbols: tuple[str, ...] = ()
    hunks: tuple[Hunk, ...] = ()

    @property
    def path(self) -> str:
        return self.new_path if self.new_path is not None else (self.old_path or "")

    def to_dict(self) -> dict[str, Any]:
        data = {
            "class": self.change_class,
            "kind": self.kind,
            "status": self.status,
            "old_path": self.old_path,
            "new_path": self.new_path,
            "summary": self.summary,
            "detail": self.detail,
            "old": self.old.to_dict(),
            "new": self.new.to_dict(),
            "symbols": {"old": list(self.old_symbols), "new": list(self.new_symbols)},
            "hunks": [hunk.to_dict() for hunk in self.hunks],
        }
        data["id"] = item_id(data)
        return data


def item_id(data: dict[str, Any]) -> str:
    """A stable identifier of an item: the SHA-256 of its canonical JSON, shortened."""
    body = {key: value for key, value in data.items() if key != "id"}
    text = json.dumps(body, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return "i-" + hashlib.sha256(text.encode("ascii")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class FileChange:
    """A path-level change between two trees, with blob identities and line hunks."""

    status: str  # added, removed, modified, renamed
    old_path: str | None
    new_path: str | None
    old_oid: str | None
    new_oid: str | None
    old_mode: str | None
    new_mode: str | None
    similarity: int | None  # percent, for renames
    old_classification: str | None
    new_classification: str | None
    analysis: str  # "text", or "not-text:<reason>"
    hunks: tuple[Hunk, ...]
    lines_added: int
    lines_removed: int
    old_parse: str | None
    new_parse: str | None

    @property
    def path(self) -> str:
        return self.new_path if self.new_path is not None else (self.old_path or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "old_path": self.old_path,
            "new_path": self.new_path,
            "old_oid": self.old_oid,
            "new_oid": self.new_oid,
            "old_mode": self.old_mode,
            "new_mode": self.new_mode,
            "similarity": self.similarity,
            "old_classification": self.old_classification,
            "new_classification": self.new_classification,
            "analysis": self.analysis,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "parse": {"old": self.old_parse, "new": self.new_parse},
            "hunks": [hunk.to_dict() for hunk in self.hunks],
        }


@dataclass(frozen=True, slots=True)
class CommitDiff:
    """The classified difference between two pinned commits."""

    old: CommitRef
    new: CommitRef
    files: tuple[FileChange, ...]
    items: tuple[ChangeItem, ...]
    rename_detection: str  # "complete" or "limited"
    classifier_version: int
    symbol_backends: tuple[str, ...]

    def counts(self) -> dict[str, Any]:
        from .rules import CLASSES

        by_class = {name: 0 for name in CLASSES}
        for item in self.items:
            by_class[item.change_class] += 1
        by_status = {name: 0 for name in ("added", "removed", "modified", "renamed")}
        for change in self.files:
            by_status[change.status] += 1
        return {
            "items_by_class": by_class,
            "files_by_status": by_status,
            "items": len(self.items),
            "files": len(self.files),
            "lines_added": sum(change.lines_added for change in self.files),
            "lines_removed": sum(change.lines_removed for change in self.files),
        }

    def parse_coverage(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for change in self.files:
            for status in (change.old_parse, change.new_parse):
                if status is not None:
                    counts[status] = counts.get(status, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "old": self.old.to_dict(),
            "new": self.new.to_dict(),
            "classifier_version": self.classifier_version,
            "rename_detection": self.rename_detection,
            "symbol_backends": list(self.symbol_backends),
            "counts": self.counts(),
            "parse_coverage": self.parse_coverage(),
            "files": [change.to_dict() for change in self.files],
            "items": [item.to_dict() for item in self.items],
        }
