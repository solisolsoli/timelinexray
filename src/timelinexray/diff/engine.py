"""Two-commit diffs from git objects, classified into change items with citations.

:class:`DiffEngine` compares the verified manifests of two pinned commits (path, mode and
blob id of every entry), detects renames, cuts line hunks from the blob bytes of both
sides, attributes them to Milestone 2 symbols and classifies them. No working tree is used
and commit messages are never read.

Classification of one changed file (first matching rule wins for the whole file):

1. Not text on either side (binary, oversize, symlink, submodule) -> ``generated-vendored``
   when the manifest says generated/vendored, else ``unknown`` ("not compared as text").
2. Every existing side has a path class -> that class for the whole file: ``license``
   (license/notice file names), ``generated-vendored`` (manifest reason), ``docs-only``,
   ``test-only``, ``build-dependency`` (build-system and dependency files).
3. Otherwise the content decides, per item:

   * ``parameter-default`` - a ``param!`` declaration, const/static, literal field or
     config key whose value differs (and ``param!`` declarations added or removed);
   * ``registration`` - a list registering components whose entries were added, removed
     or reordered (or such a list added or removed);
   * each remaining hunk: ``cosmetic`` when the comment- and whitespace-insensitive
     tokens of both sides are equal; ``test-only`` inside test code; ``license`` when every
     changed line is license header text; ``build-dependency`` when every changed code line
     is an import or bodyless module declaration; else ``scoring-logic``,
     ``model-config`` or ``unknown`` from path and symbol names
     (:func:`timelinexray.diff.rules.logic_class`). An ``unknown`` item records why in
     ``detail.unknown_reason``.

Adjacent hunks of the same class inside the same symbol form one item. Every item cites an
exact span on each side, read with :func:`timelinexray.span.read_span` from the blob bytes.
"""

from __future__ import annotations

import difflib
import hashlib
import posixpath
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any

from ..errors import IntegrityError
from ..snapshot.manifest import Manifest, ManifestEntry
from ..snapshot.store import PinRecord, SnapshotStore
from .analysis import PARAM_MACRO, BlobAnalysis, BlobView, RegList, ValueDecl, make_analysis
from .hunks import align_hunks, line_hunks
from .model import (
    ABSENT,
    CONTEXT,
    EMPTY,
    NOT_TEXT,
    ChangeItem,
    Citation,
    CommitDiff,
    CommitRef,
    FileChange,
    Hunk,
)
from .rules import (
    BUILD_DEPENDENCY,
    CLASSIFIER_VERSION,
    COSMETIC,
    GENERATED_VENDORED,
    LICENSE,
    NATIVE_LANGUAGES,
    PARAMETER_DEFAULT,
    REGISTRATION,
    TEST_ONLY,
    UNKNOWN,
    class_rank,
    is_key_value_config,
    logic_match,
    path_class,
)
from .symbols import FileSymbols, SymbolSource

ADDED, REMOVED, MODIFIED, RENAMED = "added", "removed", "modified", "renamed"

#: Above this many removed x added candidate pairs, inexact rename detection only pairs
#: files with the same base name (reported as ``rename_detection: limited``).
RENAME_PAIR_LIMIT = 40_000
RENAME_MIN_SIMILARITY = 50

_CACHE_BYTES = 128 * 1024 * 1024
_UNREADABLE = frozenset({"binary", "oversize", "symlink", "submodule"})
_LICENSE_LINE = re.compile(
    rb"(?i)(spdx-license-identifier|copyright|licensed under|license, version|apache license|"
    rb"mit license|all rights reserved|licenses/license|without warranties)"
)


def _path_key(path: str) -> bytes:
    return path.encode("utf-8", "surrogateescape")


def _readable(entry: ManifestEntry) -> bool:
    return entry.type == "blob" and not entry.is_symlink and entry.reason not in _UNREADABLE


@dataclass
class _Builder:
    change_class: str
    kind: str
    summary: str
    old: Citation
    new: Citation
    detail: dict[str, Any] = field(default_factory=dict)
    old_symbols: tuple[str, ...] = ()
    new_symbols: tuple[str, ...] = ()
    old_span: tuple[int, int] | None = None  # lines this item covers, for hunk attachment
    new_span: tuple[int, int] | None = None
    hunks: list[Hunk] = field(default_factory=list)


class DiffEngine:
    """Diffs between pinned commits of one snapshot store (see the module documentation)."""

    def __init__(self, store: SnapshotStore, symbols: SymbolSource | None = None) -> None:
        self.store = store
        self.symbols = symbols if symbols is not None else SymbolSource()
        self._analyses: dict[tuple[str, str | None, bool], BlobAnalysis] = {}
        self._cached_bytes = 0

    # -- blobs ---------------------------------------------------------------------------

    def _load(self, wanted: list[tuple[PinRecord, ManifestEntry]]) -> None:
        """Read and analyse every wanted blob that is not cached yet (one batch per mirror).

        Each blob keeps the pin it was requested for: symbols may come from that commit's
        index generation.
        """
        by_mirror: dict[str, tuple[PinRecord, dict[str, tuple[PinRecord, ManifestEntry]]]] = {}
        for pin, entry in wanted:
            if self._cache_key(entry) in self._analyses:
                continue
            slot = by_mirror.setdefault(pin.mirror, (pin, {}))
            slot[1].setdefault(entry.oid, (pin, entry))
        for mirror_pin, entries in by_mirror.values():
            repo = self.store.repo_for(mirror_pin)
            for oid, data in repo.read_blobs(sorted(entries)):
                pin, entry = entries[oid]
                if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
                    raise IntegrityError(
                        f"blob {oid} for {entry.path} does not match its manifest entry"
                    )
                self._remember(pin, entry, data)

    def _cache_key(self, entry: ManifestEntry) -> tuple[str, str | None, bool]:
        return (entry.oid, entry.language, is_key_value_config(entry.path))

    def _remember(self, pin: PinRecord, entry: ManifestEntry, data: bytes) -> BlobAnalysis:
        key = self._cache_key(entry)
        cached = self._analyses.get(key)
        if cached is not None:
            return cached
        text: str | None
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        source, commit, path, language = self.symbols, pin.commit, entry.path, entry.language

        def load_symbols() -> FileSymbols:
            return source.for_blob(commit, path, language, data, text)

        analysis = make_analysis(entry.oid, entry.path, entry.language, data, load_symbols)
        while self._analyses and self._cached_bytes + len(data) > _CACHE_BYTES:
            oldest = next(iter(self._analyses))
            self._cached_bytes -= len(self._analyses.pop(oldest).data)
        self._analyses[key] = analysis
        self._cached_bytes += len(data)
        return analysis

    def _analysis(self, pin: PinRecord, entry: ManifestEntry) -> BlobAnalysis:
        cached = self._analyses.get(self._cache_key(entry))
        if cached is None:
            self._load([(pin, entry)])
            cached = self._analyses[self._cache_key(entry)]
        return cached

    # -- the diff ------------------------------------------------------------------------

    def diff(self, old: str, new: str) -> CommitDiff:
        old_pin, old_manifest = self.store.load_manifest(old)
        new_pin, new_manifest = self.store.load_manifest(new)
        old_entries = {entry.path: entry for entry in old_manifest.entries}
        new_entries = {entry.path: entry for entry in new_manifest.entries}
        removed = sorted((p for p in old_entries if p not in new_entries), key=_path_key)
        added = sorted((p for p in new_entries if p not in old_entries), key=_path_key)
        modified = sorted(
            (
                p
                for p in old_entries
                if p in new_entries
                and (old_entries[p].oid, old_entries[p].mode, old_entries[p].type)
                != (new_entries[p].oid, new_entries[p].mode, new_entries[p].type)
            ),
            key=_path_key,
        )
        wanted = [(old_pin, old_entries[p]) for p in removed + modified if _readable(old_entries[p])]
        wanted += [(new_pin, new_entries[p]) for p in added + modified if _readable(new_entries[p])]
        self._load(wanted)
        renames, detection = self._renames(old_pin, new_pin, old_entries, new_entries, removed, added)
        renamed_old = {pair[0] for pair in renames}
        renamed_new = {pair[1] for pair in renames}

        pairs: list[tuple[str, str | None, str | None, int | None]] = []
        pairs += [(MODIFIED, p, p, None) for p in modified]
        pairs += [(RENAMED, o, n, s) for o, n, s in renames]
        pairs += [(REMOVED, p, None, None) for p in removed if p not in renamed_old]
        pairs += [(ADDED, None, p, None) for p in added if p not in renamed_new]
        pairs.sort(key=lambda pair: (_path_key(pair[2] if pair[2] is not None else pair[1] or ""),
                                     _path_key(pair[1] or "")))

        files: list[FileChange] = []
        items: list[ChangeItem] = []
        backends: set[str] = set()
        for status, old_path, new_path, similarity in pairs:
            change, file_items = self._file(
                status,
                old_pin, old_entries.get(old_path) if old_path else None,
                new_pin, new_entries.get(new_path) if new_path else None,
                similarity,
            )
            files.append(change)
            items.extend(file_items)
            for side_entry in (old_entries.get(old_path or ""), new_entries.get(new_path or "")):
                if side_entry is not None and _readable(side_entry):
                    analysis = self._analyses.get(self._cache_key(side_entry))
                    if analysis is not None and analysis.symbols_loaded and analysis.symbols.backend:
                        backends.add(analysis.symbols.backend)
        items.sort(key=_item_order)
        return CommitDiff(
            old=_ref(old_pin, old_manifest),
            new=_ref(new_pin, new_manifest),
            files=tuple(files),
            items=tuple(items),
            rename_detection=detection,
            classifier_version=CLASSIFIER_VERSION,
            symbol_backends=tuple(sorted(backends)),
        )

    # -- renames -------------------------------------------------------------------------

    def _renames(
        self,
        old_pin: PinRecord,
        new_pin: PinRecord,
        old_entries: dict[str, ManifestEntry],
        new_entries: dict[str, ManifestEntry],
        removed: list[str],
        added: list[str],
    ) -> tuple[list[tuple[str, str, int]], str]:
        pairs: list[tuple[str, str, int]] = []
        used_old: set[str] = set()
        used_new: set[str] = set()
        by_oid: dict[tuple[str, str], list[str]] = {}
        for path in added:
            entry = new_entries[path]
            by_oid.setdefault((entry.oid, entry.type), []).append(path)
        for path in removed:
            entry = old_entries[path]
            candidates = [p for p in by_oid.get((entry.oid, entry.type), []) if p not in used_new]
            if candidates:
                target = candidates[0]
                pairs.append((path, target, 100))
                used_old.add(path)
                used_new.add(target)
        rest_old = [p for p in removed if p not in used_old and _readable(old_entries[p])]
        rest_new = [p for p in added if p not in used_new and _readable(new_entries[p])]
        detection = "complete"
        if len(rest_old) * len(rest_new) > RENAME_PAIR_LIMIT:
            detection = "limited"
        profiles: dict[str, tuple[Counter[bytes], int]] = {}

        def profile(pin: PinRecord, entry: ManifestEntry) -> tuple[Counter[bytes], int]:
            key = f"{pin.commit}:{entry.path}"
            if key not in profiles:
                lines = self._analysis(pin, entry).lines
                profiles[key] = (Counter(lines), len(lines))
            return profiles[key]

        scored: list[tuple[int, bytes, bytes, str, str]] = []
        for old_path in rest_old:
            for new_path in rest_new:
                if detection == "limited" and posixpath.basename(old_path) != posixpath.basename(new_path):
                    continue
                old_count = old_entries[old_path].size or 0
                new_count = new_entries[new_path].size or 0
                if not old_count or not new_count or 2 * min(old_count, new_count) < max(old_count, new_count) * 0.5:
                    continue
                a, la = profile(old_pin, old_entries[old_path])
                b, lb = profile(new_pin, new_entries[new_path])
                if not la or not lb or 2 * min(la, lb) * 100 < RENAME_MIN_SIMILARITY * (la + lb):
                    continue
                common = sum((a & b).values())
                score = (200 * common) // (la + lb)
                if score >= RENAME_MIN_SIMILARITY:
                    scored.append((-score, _path_key(old_path), _path_key(new_path), old_path, new_path))
        scored.sort()
        for negative, _, _, old_path, new_path in scored:
            if old_path in used_old or new_path in used_new:
                continue
            pairs.append((old_path, new_path, -negative))
            used_old.add(old_path)
            used_new.add(new_path)
        return pairs, detection

    # -- one file ------------------------------------------------------------------------

    def _file(
        self,
        status: str,
        old_pin: PinRecord,
        old_entry: ManifestEntry | None,
        new_pin: PinRecord,
        new_entry: ManifestEntry | None,
        similarity: int | None,
    ) -> tuple[FileChange, list[ChangeItem]]:
        old_path = old_entry.path if old_entry else None
        new_path = new_entry.path if new_entry else None
        readable = all(entry is None or _readable(entry) for entry in (old_entry, new_entry))
        old_a = self._analysis(old_pin, old_entry) if old_entry and readable else None
        new_a = self._analysis(new_pin, new_entry) if new_entry and readable else None
        sides = [(e.path, e.reason) for e in (old_entry, new_entry) if e is not None]
        path_decided = all(path_class(path, reason) is not None for path, reason in sides)
        if readable:
            old_lines = old_a.lines if old_a else []
            new_lines = new_a.lines if new_a else []
            hunks = line_hunks(old_lines, new_lines)
            if old_a is not None and new_a is not None and not path_decided:
                hunks = align_hunks(hunks, old_lines, new_lines,
                                    _anchor_spans(old_a), _anchor_spans(new_a))
            analysis = "text"
        else:
            hunks = []
            reasons = sorted({e.reason or e.type for e in (old_entry, new_entry)
                              if e is not None and not _readable(e)})
            analysis = "not-text:" + ",".join(reasons)
        change = FileChange(
            status=status,
            old_path=old_path,
            new_path=new_path,
            old_oid=old_entry.oid if old_entry else None,
            new_oid=new_entry.oid if new_entry else None,
            old_mode=old_entry.mode if old_entry else None,
            new_mode=new_entry.mode if new_entry else None,
            similarity=similarity,
            old_classification=old_entry.classification if old_entry else None,
            new_classification=new_entry.classification if new_entry else None,
            analysis=analysis,
            hunks=tuple(hunks),
            lines_added=sum(h.new_count for h in hunks),
            lines_removed=sum(h.old_count for h in hunks),
            old_parse=None,
            new_parse=None,
        )
        old_view = BlobView(old_pin.commit, old_path, old_a) if old_a and old_path else None
        new_view = BlobView(new_pin.commit, new_path, new_a) if new_a and new_path else None
        context = _FileContext(change, old_pin.commit, new_pin.commit, old_entry, new_entry,
                               old_view, new_view, hunks)
        builders = context.classify()
        items = [context.finish(builder) for builder in builders]
        change = replace(change, old_parse=_parse_status(old_a), new_parse=_parse_status(new_a))
        return change, items


def _anchor_spans(analysis: BlobAnalysis | None) -> list[tuple[int, int]]:
    """Spans a hunk boundary should not cut: symbols (not imports) and registration lists."""
    if analysis is None:
        return []
    spans = [(s.start_line, s.end_line) for s in analysis.symbols.symbols if s.kind != "import"]
    spans += [(item.start_line, item.end_line) for item in analysis.lists]
    return spans


def _parse_status(analysis: BlobAnalysis | None) -> str | None:
    """The syntax status of a side whose symbols were needed (``None`` when they were not)."""
    if analysis is None or not analysis.symbols_loaded or analysis.symbols.status == "not-native":
        return None
    return analysis.symbols.status


def _ref(pin: PinRecord, manifest: Manifest) -> CommitRef:
    return CommitRef(pin.commit, manifest.tree, manifest.committer_time, pin.manifest_sha256)


def _item_order(item: ChangeItem) -> tuple[Any, ...]:
    return (
        class_rank(item.change_class),
        _path_key(item.path),
        item.old.start_line or 0,
        item.new.start_line or 0,
        item.kind,
        item.summary,
    )


@dataclass
class _FileContext:
    change: FileChange
    old_commit: str
    new_commit: str
    old_entry: ManifestEntry | None
    new_entry: ManifestEntry | None
    old_view: BlobView | None
    new_view: BlobView | None
    hunks: list[Hunk]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(p for p in (self.change.old_path, self.change.new_path) if p is not None)

    # -- citations -----------------------------------------------------------------------

    def side_citation(self, side: str, hunks: list[Hunk], *, whole: bool = False) -> Citation:
        view = self.old_view if side == "old" else self.new_view
        entry = self.old_entry if side == "old" else self.new_entry
        commit = self.old_commit if side == "old" else self.new_commit
        if entry is None:
            return Citation(commit, None, ABSENT, note="path absent at this commit")
        if view is None:
            reason = entry.reason or entry.type
            return Citation(commit, entry.path, NOT_TEXT, blob_oid=entry.oid,
                            note=f"not read as text ({reason})")
        count = view.analysis.line_count
        if count == 0:
            return Citation(commit, entry.path, EMPTY, blob_oid=entry.oid, note="empty blob")
        if whole:
            return view.cite(1, count)
        lines = [line for hunk in hunks for line in (hunk.old_lines if side == "old" else hunk.new_lines)]
        if lines:
            return view.cite(min(lines), max(lines))
        anchors = [hunk.old_start if side == "old" else hunk.new_start for hunk in hunks]
        anchor = min(anchors) if anchors else 0
        line = min(max(anchor, 1), count)
        note = f"nothing changed on this side; the change applies after line {anchor}" if anchor \
            else "nothing changed on this side; the change applies before line 1"
        return view.cite(line, line, role=CONTEXT, note=note)

    def finish(self, builder: _Builder) -> ChangeItem:
        return ChangeItem(
            change_class=builder.change_class,
            kind=builder.kind,
            status=self.change.status,
            old_path=self.change.old_path,
            new_path=self.change.new_path,
            summary=builder.summary,
            old=builder.old,
            new=builder.new,
            detail=builder.detail,
            old_symbols=builder.old_symbols,
            new_symbols=builder.new_symbols,
            hunks=tuple(sorted(builder.hunks, key=lambda h: (h.old_start, h.new_start))),
        )

    # -- classification ------------------------------------------------------------------

    def classify(self) -> list[_Builder]:
        change = self.change
        if change.analysis != "text":
            reasons = {e.reason for e in (self.old_entry, self.new_entry) if e is not None}
            cls = GENERATED_VENDORED if reasons & {"generated", "vendored"} else UNKNOWN
            return [_Builder(cls, "file", self._file_summary("not compared as text: "
                                                               + change.analysis[len("not-text:"):]),
                             self.side_citation("old", [], whole=True),
                             self.side_citation("new", [], whole=True),
                             {"unknown_reason": "not-text"} if cls == UNKNOWN else {})]
        sides = [(e.path, e.reason) for e in (self.old_entry, self.new_entry) if e is not None]
        path_classes = [path_class(path, reason) for path, reason in sides]
        if all(cls is not None for cls in path_classes):
            cls = min(path_classes, key=class_rank)  # type: ignore[arg-type]
            return [self._file_item(cls)]
        if not self.hunks:
            return [self._unchanged_content_item()]
        builders = self._values() + self._registrations()
        builders += self._regions(builders)
        return builders

    def _file_summary(self, text: str) -> str:
        change = self.change
        prefix = {ADDED: "file added", REMOVED: "file removed", MODIFIED: "file modified",
                  RENAMED: f"file renamed from {change.old_path} ({change.similarity}% similar)"}
        return f"{prefix[change.status]}; {text}"

    def _file_item(self, cls: str) -> _Builder:
        change = self.change
        whole = not self.hunks
        text = (f"-{change.lines_removed} +{change.lines_added} lines" if self.hunks
                else "content unchanged")
        return _Builder(cls, "file", self._file_summary(text),
                        self.side_citation("old", self.hunks, whole=whole),
                        self.side_citation("new", self.hunks, whole=whole),
                        hunks=list(self.hunks))

    def _unchanged_content_item(self) -> _Builder:
        change = self.change
        if change.status == RENAMED:
            cls, kind, text = COSMETIC, "moved", "content identical"
        else:
            cls, kind = UNKNOWN, "mode"
            text = f"file mode changed {change.old_mode} -> {change.new_mode}; content unchanged"
        return _Builder(cls, kind, self._file_summary(text),
                        self.side_citation("old", [], whole=True),
                        self.side_citation("new", [], whole=True),
                        {"unknown_reason": "mode-only"} if cls == UNKNOWN else {})

    def _attribute(self, side: str, hunks: list[Hunk]) -> tuple[str, ...]:
        view = self.old_view if side == "old" else self.new_view
        if view is None:
            return ()
        lines = [line for hunk in hunks for line in (hunk.old_lines if side == "old" else hunk.new_lines)]
        if not lines:
            anchors = [(hunk.old_start if side == "old" else hunk.new_start) for hunk in hunks]
            lines = [max(anchor, 1) for anchor in anchors if view.analysis.line_count]
            lines = [min(line, view.analysis.line_count) for line in lines]
        return view.analysis.attribute(lines)

    def _touching(self, old_span: tuple[int, int] | None, new_span: tuple[int, int] | None) -> list[Hunk]:
        found = []
        for hunk in self.hunks:
            if old_span and any(old_span[0] <= line <= old_span[1] for line in hunk.old_lines):
                found.append(hunk)
            elif new_span and any(new_span[0] <= line <= new_span[1] for line in hunk.new_lines):
                found.append(hunk)
        return found

    # -- parameter defaults --------------------------------------------------------------

    def _values(self) -> list[_Builder]:
        old_values = self._side_values(self.old_view)
        new_values = self._side_values(self.new_view)
        builders: list[_Builder] = []
        for key in sorted(set(old_values) | set(new_values)):
            old_decl, new_decl = old_values.get(key), new_values.get(key)
            if old_decl is not None and new_decl is not None:
                if old_decl.value == new_decl.value:
                    continue
                kind = "value-changed"
            elif new_decl is not None and new_decl.kind == PARAM_MACRO:
                kind = "added"
            elif old_decl is not None and old_decl.kind == PARAM_MACRO:
                kind = "removed"
            else:
                continue
            builders.append(self._value_builder(kind, old_decl, new_decl))
        return builders

    def _side_values(self, view: BlobView | None) -> dict[tuple[str, str, int], ValueDecl]:
        if view is None:
            return {}
        if self.change.status in (ADDED, REMOVED):
            # Only param! declarations are reported as added or removed; skip the rest.
            if b"param!" not in view.analysis.data:
                return {}
            return {d.key: d for d in view.analysis.values if d.kind == PARAM_MACRO}
        return {d.key: d for d in view.analysis.values}

    def _value_builder(self, kind: str, old_decl: ValueDecl | None, new_decl: ValueDecl | None) -> _Builder:
        decl = new_decl or old_decl
        assert decl is not None
        old_span = (old_decl.start_line, old_decl.end_line) if old_decl else None
        new_span = (new_decl.start_line, new_decl.end_line) if new_decl else None
        touching = self._touching(old_span, new_span)
        old_cite = (self.old_view.cite(*old_span) if old_decl and self.old_view
                    else self.side_citation("old", touching))
        new_cite = (self.new_view.cite(*new_span) if new_decl and self.new_view
                    else self.side_citation("new", touching))
        if kind == "value-changed":
            summary = f"{decl.name}: public default {old_decl.value} -> {new_decl.value}"  # type: ignore[union-attr]
        elif kind == "added":
            summary = f"{decl.name} declared with public default {decl.value}"
        else:
            summary = f"{decl.name} removed (public default was {decl.value})"
        detail = {
            "name": decl.name,
            "declaration": decl.kind,
            "ordinal": decl.ordinal,
            "type": decl.value_type,
            "flag": decl.flag,
            "old_value": old_decl.value if old_decl else None,
            "new_value": new_decl.value if new_decl else None,
            "literal": bool((old_decl or decl).literal and (new_decl or decl).literal),
            "value_scope": "public default",
        }
        return _Builder(PARAMETER_DEFAULT, kind, summary, old_cite, new_cite, detail,
                        (decl.name,) if old_decl else (), (decl.name,) if new_decl else (),
                        old_span, new_span)

    # -- registrations -------------------------------------------------------------------

    def _registrations(self) -> list[_Builder]:
        old_lists = {item.key: item for item in self.old_view.analysis.lists} if self.old_view else {}
        new_lists = {item.key: item for item in self.new_view.analysis.lists} if self.new_view else {}
        builders: list[_Builder] = []
        for key in sorted(set(old_lists) | set(new_lists)):
            old_list, new_list = old_lists.get(key), new_lists.get(key)
            old_keys = [entry.key for entry in old_list.entries] if old_list else []
            new_keys = [entry.key for entry in new_list.entries] if new_list else []
            added = _multiset_minus(new_keys, old_keys)
            removed = _multiset_minus(old_keys, new_keys)
            moved = _moved(old_keys, new_keys)
            if not (added or removed or moved):
                continue
            if old_list is None:
                kind = "list-added"
            elif new_list is None:
                kind = "list-removed"
            else:
                kind = "entries-changed"
            builders.append(self._list_builder(kind, old_list, new_list, added, removed, moved))
        return builders

    def _list_builder(self, kind: str, old_list: RegList | None, new_list: RegList | None,
                      added: list[str], removed: list[str], moved: list[str]) -> _Builder:
        reg = new_list or old_list
        assert reg is not None
        old_span = (old_list.start_line, old_list.end_line) if old_list else None
        new_span = (new_list.start_line, new_list.end_line) if new_list else None
        touching = self._touching(old_span, new_span)
        old_cite = (self.old_view.cite(*old_span) if old_list and self.old_view
                    else self.side_citation("old", touching))
        new_cite = (self.new_view.cite(*new_span) if new_list and self.new_view
                    else self.side_citation("new", touching))
        parts = []
        if added:
            parts.append("added " + ", ".join(added))
        if removed:
            parts.append("removed " + ", ".join(removed))
        if moved:
            parts.append("reordered " + ", ".join(moved))
        summary = f"{reg.label}: " + "; ".join(parts)
        detail = {
            "list": reg.label,
            "container": reg.container,
            "binding": reg.binding,
            "ordinal": reg.ordinal,
            "added": added,
            "removed": removed,
            "reordered": moved,
            "old_entries": [entry.key for entry in old_list.entries] if old_list else None,
            "new_entries": [entry.key for entry in new_list.entries] if new_list else None,
        }
        return _Builder(REGISTRATION, kind, summary, old_cite, new_cite, detail,
                        (reg.container,) if old_list and reg.container else (),
                        (reg.container,) if new_list and reg.container else (),
                        old_span, new_span)

    # -- remaining hunks -----------------------------------------------------------------

    def _regions(self, explained: list[_Builder]) -> list[_Builder]:
        old_cov: set[int] = set()
        new_cov: set[int] = set()
        for builder in explained:
            if builder.old_span:
                old_cov.update(range(builder.old_span[0], builder.old_span[1] + 1))
            if builder.new_span:
                new_cov.update(range(builder.new_span[0], builder.new_span[1] + 1))
        old_a = self.old_view.analysis if self.old_view else None
        new_a = self.new_view.analysis if self.new_view else None
        regions: list[tuple[str, Hunk, str | None, tuple[str, str] | None]] = []
        for hunk in self.hunks:
            match: tuple[str, str] | None = None
            old_lines, new_lines = list(hunk.old_lines), list(hunk.new_lines)
            touched = bool(old_cov.intersection(old_lines) or new_cov.intersection(new_lines))
            rest_old = [line for line in old_lines if line not in old_cov]
            rest_new = [line for line in new_lines if line not in new_cov]
            same = _same_tokens(old_a, rest_old, new_a, rest_new)
            if same and touched:
                for builder in explained:
                    if hunk in self._touching(builder.old_span, builder.new_span):
                        builder.hunks.append(hunk)
                continue
            if same:
                cls = COSMETIC
            elif self._in_tests(old_a, old_lines, new_a, new_lines):
                cls = TEST_ONLY
            elif self._license_lines(old_a, old_lines, new_a, new_lines):
                cls = LICENSE
            elif self._declarations_only(old_a, old_lines, new_a, new_lines):
                cls = BUILD_DEPENDENCY
            else:
                names = self._attribute("old", [hunk]) + self._attribute("new", [hunk])
                cls, rule, matched = logic_match(self.paths, names)
                if rule is not None and matched is not None:
                    match = (rule, matched)
            regions.append((cls, hunk, self._group_symbol(hunk), match))
        builders: list[_Builder] = []
        group: list[Hunk] = []
        group_matches: list[tuple[str, str]] = []
        current: tuple[str, str | None] | None = None
        for cls, hunk, symbol, match in regions + [("", Hunk(0, 0, 0, 0), None, None)]:
            if current is not None and (cls, symbol) != current:
                builder = self._region_builder(current[0], group)
                if group_matches:  # which name rule decided a logic class, in hunk order
                    builder.detail["matched_by"] = [{"rule": rule, "name": name} for rule, name
                                                    in dict.fromkeys(group_matches)]
                builders.append(builder)
                group, group_matches = [], []
            current = (cls, symbol)
            group.append(hunk)
            if match is not None:
                group_matches.append(match)
        return builders

    def _group_symbol(self, hunk: Hunk) -> str | None:
        if self.new_view is not None and hunk.new_count:
            symbol = self.new_view.analysis.innermost(hunk.new_start)
            return symbol.qualname if symbol else None
        if self.old_view is not None and hunk.old_count:
            symbol = self.old_view.analysis.innermost(hunk.old_start)
            return symbol.qualname if symbol else None
        return None

    @staticmethod
    def _in_tests(old_a: BlobAnalysis | None, old_lines: list[int],
                  new_a: BlobAnalysis | None, new_lines: list[int]) -> bool:
        if not old_lines and not new_lines:
            return False
        old_ok = not old_lines or (old_a is not None and old_a.in_test_ranges(old_lines))
        new_ok = not new_lines or (new_a is not None and new_a.in_test_ranges(new_lines))
        return old_ok and new_ok

    @staticmethod
    def _license_lines(old_a: BlobAnalysis | None, old_lines: list[int],
                       new_a: BlobAnalysis | None, new_lines: list[int]) -> bool:
        texts = [old_a.lines[line - 1] for line in old_lines] if old_a else []
        texts += [new_a.lines[line - 1] for line in new_lines] if new_a else []
        texts = [text for text in texts if text.strip()]
        return bool(texts) and all(_LICENSE_LINE.search(text) for text in texts)

    @staticmethod
    def _declarations_only(old_a: BlobAnalysis | None, old_lines: list[int],
                           new_a: BlobAnalysis | None, new_lines: list[int]) -> bool:
        """Every changed code line on both sides is an import or bodyless module declaration
        (and at least one side has such a line)."""
        verdicts = []
        for analysis, lines in ((old_a, old_lines), (new_a, new_lines)):
            if not lines:
                continue
            if analysis is None:
                return False
            verdict = analysis.only_declarations(lines)
            if verdict is False:
                return False
            verdicts.append(verdict)
        return any(verdict is True for verdict in verdicts)

    def _unknown_reason(self) -> str:
        """Why a text region stayed ``unknown``: ``no-rule`` when its language is parsed
        (symbols, values and lists were read and no rule matched), else ``not-parsed`` (only
        path and token rules could apply)."""
        for view in (self.new_view, self.old_view):
            if view is not None and view.analysis.language in NATIVE_LANGUAGES:
                return "no-rule"
        return "not-parsed"

    def _region_builder(self, cls: str, hunks: list[Hunk]) -> _Builder:
        old_symbols = self._attribute("old", hunks)
        new_symbols = self._attribute("new", hunks)
        removed = sum(h.old_count for h in hunks)
        added = sum(h.new_count for h in hunks)
        where = ", ".join(dict.fromkeys(new_symbols + old_symbols)) or "module level"
        if cls == COSMETIC:
            text = "comments, whitespace or line endings only"
        elif cls == TEST_ONLY:
            text = "test code"
        elif cls == LICENSE:
            text = "license header lines"
        elif cls == BUILD_DEPENDENCY:
            text = "import or module declarations"
        else:
            text = "code or configuration"
        if self.change.status in (ADDED, REMOVED):
            summary = self._file_summary(f"{text}, {added or removed} lines in {where}")
        else:
            summary = (f"{text}: {len(hunks)} hunk{'s' if len(hunks) != 1 else ''}, "
                       f"-{removed} +{added} lines in {where}")
            if self.change.status == RENAMED:
                summary += f" (renamed from {self.change.old_path}, {self.change.similarity}% similar)"
        detail = {"unknown_reason": self._unknown_reason()} if cls == UNKNOWN else {}
        return _Builder(cls, "region", summary,
                        self.side_citation("old", hunks), self.side_citation("new", hunks),
                        detail, old_symbols, new_symbols, hunks=list(hunks))


def _same_tokens(old_a: BlobAnalysis | None, old_lines: list[int],
                 new_a: BlobAnalysis | None, new_lines: list[int]) -> bool:
    """Whether both sides' lines have the same comment/whitespace-insensitive tokens."""
    if not old_lines or not new_lines:
        side, lines = (new_a, new_lines) if not old_lines else (old_a, old_lines)
        if not lines:
            return True
        if side is None or not side.may_lack_code(lines):
            return False
        return side.tokens(lines) == ()
    old_tokens = old_a.tokens(old_lines) if old_a else ()
    new_tokens = new_a.tokens(new_lines) if new_a else ()
    return old_tokens == new_tokens


def _multiset_minus(left: list[str], right: list[str]) -> list[str]:
    remaining = Counter(right)
    out = []
    for key in left:
        if remaining[key]:
            remaining[key] -= 1
        else:
            out.append(key)
    return out


def _moved(old_keys: list[str], new_keys: list[str]) -> list[str]:
    """Entries present on both sides whose relative order changed."""
    common = Counter(old_keys) & Counter(new_keys)
    budget = Counter(common)
    old_common = []
    for key in old_keys:
        if budget[key]:
            budget[key] -= 1
            old_common.append(key)
    budget = Counter(common)
    new_common = []
    for key in new_keys:
        if budget[key]:
            budget[key] -= 1
            new_common.append(key)
    if old_common == new_common:
        return []
    matcher = difflib.SequenceMatcher(None, old_common, new_common, autojunk=False)
    kept = Counter()
    for block in matcher.get_matching_blocks():
        for key in new_common[block.b : block.b + block.size]:
            kept[key] += 1
    moved = []
    for key in new_common:
        if kept[key]:
            kept[key] -= 1
        else:
            moved.append(key)
    return moved
