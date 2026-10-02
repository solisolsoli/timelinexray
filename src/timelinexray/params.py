"""Parameter declarations at a pinned commit and their history over the pinned commits.

A *parameter* here is any declaration with a literal default that the Milestone 5b value
extraction recognises (:attr:`timelinexray.diff.analysis.BlobAnalysis.values`): a Rust
``param!(Name, type, "flag", value)`` declaration, a ``const``/``static``, a Java or Scala
field with a literal value, or a Python ``UPPER_CASE`` constant. Every value is a **public
default at its commit**, never a production value.

* :meth:`ParamResolver.declarations` answers "where is ``name`` declared at this commit and
  with which default?" The Milestone 2 code index finds the declarations (``name`` or
  qualified name; kinds ``const``, ``static``, ``field``, ``param``); the blob is then read
  from the mirror and analysed with the same extraction the diff and digest use, so the
  value, type and flag string agree with ``txray diff``. The commit must be indexed.
* :meth:`ParamResolver.history` follows the declarations over the **pinned** commits of
  one line of history (the first-parent chain from the oldest to the newest pin of the
  same mirror, or an explicit ``base``..``head``), from git objects already in the mirror:
  nothing is fetched and commit messages are never read. Each pinned commit contributes
  the value of every declaration; a commit that is not indexed is checked only at the
  paths where indexed commits declare the name (and says so). Changes (``declared``,
  ``value-changed``, ``removed``) cite the declaration on both sides; reversions (a value
  that comes back after a different one) are listed per declaration. Unpinned commits
  between two pins are reported as gaps; pinned commits that are not on the line (reached
  through a merge, diverged, or outside an explicit range) are listed, not compared.

Config-file keys (YAML, TOML, INI, JSON) are compared by ``txray diff`` but are not
reachable here by name: the index has no symbol for them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .diff import Lineage, SymbolSource
from .diff.analysis import PARAM_MACRO, VALUE_KINDS, BlobAnalysis, ValueDecl, make_analysis
from .diff.symbols import FileSymbols, SymbolInfo
from .errors import GitError, InvalidInput, IntegrityError, NotFound
from .gitio import run_local
from .index import SEARCH_MAX_LIMIT, SYMBOLS_MAX_LIMIT, CodeIndex, ReadSession, SearchHit
from .snapshot.manifest import ManifestEntry
from .snapshot.store import PinRecord, SnapshotStore

MAX_NAME_CHARS = 256
#: Pinned commits considered by :meth:`ParamResolver.history` (as ``list_commits``).
MAX_HISTORY_COMMITS = 200
#: Commits walked along first parents before giving up on finding the base.
MAX_WALK = 200_000
#: Lookups by name are exact; these characters would make a GLOB in the index.
_GLOB_CHARS = frozenset("*?[")

LOOKUP_INDEX = "index"
LOOKUP_KNOWN_PATHS = "known-paths"

DECLARED = "declared"
VALUE_CHANGED = "value-changed"
REMOVED = "removed"

NO_DEFAULT_NOTE = (
    "no literal default extracted: the value is computed, or the declaration is in test code"
)


def public_default_note(commit: str) -> str:
    """The sentence every shown value carries (spec boundary 5)."""
    return f"public default at commit {commit}; not a production value"


def check_name(name: object) -> str:
    """Validate a parameter name: non-empty, printable, no GLOB characters."""
    if not isinstance(name, str) or not name:
        raise InvalidInput("a parameter name is required")
    if len(name) > MAX_NAME_CHARS:
        raise InvalidInput(f"parameter names are at most {MAX_NAME_CHARS} characters")
    if any(ord(char) < 32 or char == "\x7f" for char in name):
        raise InvalidInput("parameter names must not contain control characters")
    if _GLOB_CHARS & set(name):
        raise InvalidInput(
            "parameter names are matched exactly (no * ? or [); list candidates with "
            "txray symbols --name GLOB or find_symbols"
        )
    return name


def _short(qualname: str) -> str:
    return qualname.rsplit("::", 1)[-1].rsplit(".", 1)[-1]


def name_matches(qualname: str, name: str) -> bool:
    """Whether a declaration's qualified name is ``name`` or ends in it as its own segment."""
    return qualname == name or _short(qualname) == name or (
        ("::" in name or "." in name) and (qualname.endswith("::" + name)
                                         or qualname.endswith("." + name))
    )


@dataclass(frozen=True, slots=True)
class ParamDeclaration:
    """One declaration of a parameter at one commit, with its public default."""

    commit: str
    path: str
    language: str | None
    declaration: str  # "param!", "const", "static" or "field"
    symbol_kind: str  # the Milestone 2 symbol kind ("param", "const", "static", "field")
    name: str
    qualname: str
    container: str | None
    ordinal: int  # among declarations of the same kind group and name in this file
    start_line: int
    end_line: int
    span_sha256: str
    blob_oid: str
    value: str | None  # normalised source text of the literal default; None if computed
    value_type: str | None  # the declared type of a param! (its second argument)
    flag: str | None  # the flag string of a param! (its third argument)
    literal: bool
    signature: str

    @property
    def key(self) -> tuple[str, str, str, int]:
        return (self.path, self.declaration, self.qualname, self.ordinal)

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "path": self.path,
            "language": self.language,
            "declaration": self.declaration,
            "symbol_kind": self.symbol_kind,
            "name": self.name,
            "qualname": self.qualname,
            "container": self.container,
            "ordinal": self.ordinal,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "span_sha256": self.span_sha256,
            "blob_oid": self.blob_oid,
            "value": self.value,
            "type": self.value_type,
            "flag": self.flag,
            "literal": self.literal,
            "signature": self.signature,
            "value_note": public_default_note(self.commit) if self.value is not None
            else NO_DEFAULT_NOTE,
        }


@dataclass(frozen=True, slots=True)
class ParamSnapshot:
    """The declarations of one name at one pinned commit."""

    commit: str
    committer_time: str
    indexed: bool
    lookup: str  # LOOKUP_INDEX or LOOKUP_KNOWN_PATHS
    paths_checked: tuple[str, ...]
    declarations: tuple[ParamDeclaration, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "committer_time": self.committer_time,
            "indexed": self.indexed,
            "lookup": self.lookup,
            "paths_checked": list(self.paths_checked),
            "declarations": [item.to_dict() for item in self.declarations],
        }


@dataclass(frozen=True, slots=True)
class ParamChange:
    event: str  # DECLARED, VALUE_CHANGED or REMOVED
    commit: str  # the pinned commit where the new state is first seen
    committer_time: str
    previous_commit: str  # the pinned commit before it on the line
    old: ParamDeclaration | None
    new: ParamDeclaration | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "commit": self.commit,
            "committer_time": self.committer_time,
            "previous_commit": self.previous_commit,
            "old_value": self.old.value if self.old else None,
            "new_value": self.new.value if self.new else None,
            "old": self.old.to_dict() if self.old else None,
            "new": self.new.to_dict() if self.new else None,
        }


@dataclass(frozen=True, slots=True)
class ParamTimeline:
    """The values of one declaration (path, kind, qualified name, ordinal) over the line."""

    path: str
    declaration: str
    qualname: str
    ordinal: int
    points: tuple[tuple[str, str, str | None], ...]  # (commit, committer_time, value)
    changes: tuple[ParamChange, ...]
    reversions: tuple[dict[str, Any], ...]
    current: ParamDeclaration | None  # the declaration at the head, if present there

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "declaration": self.declaration,
            "qualname": self.qualname,
            "ordinal": self.ordinal,
            "points": [
                {"commit": commit, "committer_time": time, "value": value}
                for commit, time, value in self.points
            ],
            "changes": [change.to_dict() for change in self.changes],
            "reversions": list(self.reversions),
            "current": self.current.to_dict() if self.current else None,
        }


@dataclass(frozen=True, slots=True)
class ParamHistory:
    name: str
    base: str
    head: str
    order: str
    commits: tuple[ParamSnapshot, ...]
    timelines: tuple[ParamTimeline, ...]
    history_complete: bool
    gaps: tuple[str, ...]
    not_on_line: tuple[str, ...]  # pinned commits of the mirror that are not on the line
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base": self.base,
            "head": self.head,
            "order": self.order,
            "commits": [snapshot.to_dict() for snapshot in self.commits],
            "timelines": [timeline.to_dict() for timeline in self.timelines],
            "history_complete": self.history_complete,
            "gaps": list(self.gaps),
            "not_on_line": list(self.not_on_line),
            "warnings": list(self.warnings),
        }


def returns(values: list[Any]) -> list[dict[str, Any]]:
    """Positions where a value comes back to an earlier value after a different one.

    The same rule as the digest's intermediate reversions, over the value sequence of one
    declaration; ``left_at`` is the index of the earlier value, ``back_at`` the index where
    it returns.
    """
    found = []
    for later in range(2, len(values)):
        for earlier in range(later - 1):
            if values[earlier] == values[later] and any(
                values[between] != values[earlier] for between in range(earlier + 1, later)
            ):
                found.append({"left_at": earlier, "back_at": later, "value": values[later]})
                break
    return found


@dataclass(frozen=True, slots=True)
class _Source:
    """A file to analyse at one commit: from the index (no manifest read) or the manifest."""

    path: str
    oid: str
    language: str | None
    entry: ManifestEntry | None  # set when the source came from the manifest
    indexed: bool


def _sort_key(symbol: SymbolInfo) -> tuple[int, int, int, str, str]:
    return (symbol.start_line, symbol.name_line, symbol.end_line, symbol.kind, symbol.qualname)


class ParamResolver:
    """Declarations and history of named parameters in one snapshot store."""

    def __init__(
        self,
        store: SnapshotStore,
        index: CodeIndex | None = None,
        *,
        check: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.index = index if index is not None else CodeIndex(store)
        self.check = check if check is not None else (lambda: None)
        self._registry_symbols = SymbolSource(None, registry=self.index.registry)
        self._session: ReadSession | None = None  # opened by the first indexed file lookup
        self._blobs: dict[str, bytes] = {}
        self._analyses: dict[tuple[str, str | None], BlobAnalysis] = {}

    # -- one commit ------------------------------------------------------------------------

    def declarations(self, commit: str, name: str) -> ParamSnapshot:
        """Every declaration of ``name`` at an indexed, pinned commit (NotFound otherwise)."""
        name = check_name(name)
        pin = self.store.get_pin(commit)
        sources = self._indexed_sources(pin, name)
        if sources is None:
            raise NotFound(f"commit {pin.commit} is not indexed; run: txray index {pin.commit[:12]}")
        self._load([(pin, sources)])
        return self._snapshot(pin, name, sources, True, LOOKUP_INDEX)

    def mentions(
        self, commit: str, name: str, declarations: Iterable[ParamDeclaration], limit: int
    ) -> tuple[int, list[SearchHit]]:
        """Lines containing ``name`` (lexical, case-insensitive) outside the declarations.

        Returns the number of matching lines in the whole commit (declarations included)
        and up to ``limit`` hits outside the cited declaration spans, in index order.
        """
        spans = [(d.path, d.start_line, d.end_line) for d in declarations]
        result = self.index.search(commit, name, literal=True,
                                   limit=min(limit + len(spans) * 4 + 16, SEARCH_MAX_LIMIT))
        hits = [
            hit for hit in result.hits
            if not any(hit.path == path and start <= hit.start_line <= end
                       for path, start, end in spans)
        ]
        return result.total, hits[:limit]

    # -- sources ---------------------------------------------------------------------------

    def _indexed_sources(self, pin: PinRecord, name: str) -> list[_Source] | None:
        """The files declaring ``name`` at an indexed commit, or ``None`` if not indexed."""
        if not self.index.path.is_file():
            return None
        try:
            result = self.index.symbols(pin.commit, name=name, limit=SYMBOLS_MAX_LIMIT)
        except NotFound:
            return None
        sources: list[_Source] = []
        seen: set[str] = set()
        for record in result.symbols:
            if record.kind in VALUE_KINDS and record.path not in seen:
                seen.add(record.path)
                sources.append(_Source(record.path, record.blob_oid, record.language, None, True))
        return sources

    def _manifest_sources(self, pin: PinRecord, paths: Iterable[str]) -> list[_Source]:
        """The given paths as they exist in the manifest of a commit (not indexed)."""
        sources = []
        for path in paths:
            try:
                _, entry = self.store.lookup(pin.commit, path)
            except NotFound:
                continue  # the path does not exist at this commit
            if entry.type != "blob" or entry.is_symlink or entry.classification == "excluded":
                continue
            sources.append(_Source(entry.path, entry.oid, entry.language, entry, False))
        return sources

    def _load(self, wanted: Iterable[tuple[PinRecord, list[_Source]]]) -> None:
        """Read every blob not cached yet, one ``git cat-file --batch`` per mirror."""
        by_mirror: dict[str, tuple[PinRecord, dict[str, ManifestEntry | None]]] = {}
        for pin, sources in wanted:
            for source in sources:
                if source.oid in self._blobs:
                    continue
                slot = by_mirror.setdefault(pin.mirror, (pin, {}))
                if source.entry is not None or source.oid not in slot[1]:
                    slot[1][source.oid] = source.entry
        for pin, entries in by_mirror.values():
            self.check()
            repo = self.store.repo_for(pin)
            for oid, data in repo.read_blobs(sorted(entries)):
                entry = entries[oid]
                if entry is not None and (len(data) != entry.size
                                          or hashlib.sha256(data).hexdigest() != entry.sha256):
                    raise IntegrityError(
                        f"blob {oid} for {entry.path} does not match its manifest entry"
                    )
                self._blobs[oid] = data

    def _index_symbols(self, commit: str, path: str) -> FileSymbols:
        """The file's Milestone 2 symbols from the index generation of ``commit``."""
        if self._session is None:
            self._session = self.index.read_session()
        records = self._session.file_symbols(commit, path)
        symbols = tuple(sorted(
            (
                SymbolInfo(
                    kind=record.kind, name=record.name, qualname=record.qualname,
                    container=record.container, start_line=record.start_line,
                    name_line=record.name_line, end_line=record.end_line,
                    signature=record.signature, modifiers=tuple(record.modifiers),
                )
                for record in records
            ),
            key=_sort_key,
        ))
        backend = records[0].backend if records else None
        return FileSymbols("parsed", None, backend, symbols)

    def _analysis(self, pin: PinRecord, source: _Source) -> BlobAnalysis:
        key = (source.oid, source.language)
        cached = self._analyses.get(key)
        if cached is not None:
            return cached
        if source.oid not in self._blobs:
            self._load([(pin, [source])])
        data = self._blobs[source.oid]
        commit, path, language = pin.commit, source.path, source.language

        def load_symbols() -> FileSymbols:
            if source.indexed:
                return self._index_symbols(commit, path)
            text: str | None
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            return self._registry_symbols.for_blob(commit, path, language, data, text)

        analysis = make_analysis(source.oid, source.path, source.language, data, load_symbols)
        self._analyses[key] = analysis
        return analysis

    # -- declarations ----------------------------------------------------------------------

    def _snapshot(
        self, pin: PinRecord, name: str, sources: list[_Source], indexed: bool, lookup: str
    ) -> ParamSnapshot:
        found: list[ParamDeclaration] = []
        for source in sources:
            self.check()
            found.extend(self._declarations_in(pin, source, name))
        found.sort(key=lambda d: (d.path.encode("utf-8", "surrogateescape"), d.start_line,
                                  d.ordinal))
        return ParamSnapshot(pin.commit, pin.committer_time, indexed, lookup,
                             tuple(source.path for source in sources), tuple(found))

    def _declarations_in(self, pin: PinRecord, source: _Source, name: str) -> list[ParamDeclaration]:
        analysis = self._analysis(pin, source)
        if analysis.text is None:
            return []
        values: dict[tuple[int, int], ValueDecl] = {
            (decl.start_line, decl.end_line): decl for decl in analysis.values
        }
        found = []
        for symbol in analysis.symbols.symbols:
            if symbol.kind not in VALUE_KINDS or not name_matches(symbol.qualname, name):
                continue
            decl = values.get((symbol.start_line, symbol.end_line))
            span = analysis.span(pin.commit, source.path, symbol.start_line, symbol.end_line)
            kind = PARAM_MACRO if symbol.kind == "param" else symbol.kind
            found.append(ParamDeclaration(
                commit=pin.commit,
                path=source.path,
                language=source.language,
                declaration=decl.kind if decl else kind,
                symbol_kind=symbol.kind,
                name=symbol.name,
                qualname=symbol.qualname,
                container=symbol.container,
                ordinal=decl.ordinal if decl else 0,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                span_sha256=span.sha256,
                blob_oid=source.oid,
                value=decl.value if decl else None,
                value_type=decl.value_type if decl else None,
                flag=decl.flag if decl else None,
                literal=bool(decl and decl.literal),
                signature=symbol.signature,
            ))
        return found

    # -- the line of pinned commits --------------------------------------------------------

    def _first_parent_chain(self, head_pin: PinRecord, base: str) -> list[str] | None:
        """Commits from ``base`` to ``head`` (both included) along first parents, else the
        shortest parent path (:meth:`Lineage.chain`), else ``None``. One git process."""
        repo = self.store.repo_for(head_pin)
        proc = run_local(
            repo.git_dir,
            ["rev-list", "--first-parent", f"--max-count={MAX_WALK}", head_pin.commit, "--"],
            check=False,
        )
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()
            raise GitError(f"git rev-list {head_pin.commit} failed: {detail}")
        ids = proc.stdout.decode("ascii", "replace").split()
        if base in ids:
            return list(reversed(ids[: ids.index(base) + 1]))
        return Lineage(repo).chain(base, head_pin.commit)

    def _line(
        self, base: str | None, head: str | None
    ) -> tuple[list[PinRecord], list[str], list[str], list[str], str]:
        """Pinned commits in history order, gaps, side commits, warnings and the order."""
        pins = self.store.list_pins()
        if not pins:
            raise NotFound("the snapshot store has no pinned commits; run: txray pin <commit>")
        head_pin = (self.store.get_pin(head) if head is not None
                    else max(pins, key=lambda p: (p.committer_time, p.commit)))
        same = [p for p in pins if p.mirror == head_pin.mirror]
        warnings = []
        if len(same) != len(pins):
            warnings.append(f"{len(pins) - len(same)} pinned commit(s) of another upstream "
                            "mirror are not on this line and were not compared")
        base_pin = (self.store.get_pin(base) if base is not None
                    else min(same, key=lambda p: (p.committer_time, p.commit)))
        if base_pin.mirror != head_pin.mirror:
            raise InvalidInput(f"{base_pin.commit} and {head_pin.commit} come from different "
                               "upstream mirrors")
        by_commit = {p.commit: p for p in same}
        chain = self._first_parent_chain(head_pin, base_pin.commit)
        if chain is None:
            if base is not None or head is not None:
                raise InvalidInput(f"{base_pin.commit} is not an ancestor of {head_pin.commit}")
            ordered = sorted(same, key=lambda p: (p.committer_time, p.commit))
            warnings.append("the oldest pin is not an ancestor of the newest: commits are "
                            "ordered by committer time and gaps cannot be determined")
            return ordered, ["ancestry unknown"], [], warnings, "committer time (no common line)"
        on_line = [by_commit[c] for c in chain if c in by_commit]
        gaps = []
        unpinned = 0
        previous = None
        for commit in chain:
            if commit in by_commit:
                if previous is not None and unpinned:
                    gaps.append(f"{unpinned} unpinned commit(s) between {previous[:12]} and "
                                f"{commit[:12]}")
                previous, unpinned = commit, 0
            else:
                unpinned += 1
        chain_set = set(chain)
        side = sorted(c for c in by_commit if c not in chain_set)
        if side and base is None and head is None:
            warnings.append(f"{len(side)} pinned commit(s) are not on the first-parent line "
                            "from the oldest to the newest pin (reached through a merge, or "
                            "diverged; see not_on_line) and were not compared")
        if len(on_line) > MAX_HISTORY_COMMITS:
            warnings.append(f"only the newest {MAX_HISTORY_COMMITS} pinned commits of the line "
                            "were compared")
            on_line = on_line[-MAX_HISTORY_COMMITS:]
        return on_line, gaps, side, warnings, "first-parent chain, oldest first"

    def history(self, name: str, *, base: str | None = None, head: str | None = None) -> ParamHistory:
        """The declarations of ``name`` over the pinned commits of one line of history."""
        name = check_name(name)
        line, gaps, side, warnings, order = self._line(base, head)
        sources: dict[str, list[_Source] | None] = {}
        for pin in line:
            self.check()
            sources[pin.commit] = self._indexed_sources(pin, name)
        if all(value is None for value in sources.values()):
            raise NotFound("none of the pinned commits on this line is indexed; run: "
                           f"txray index {line[-1].commit[:12]}")
        known: list[str] = []
        for value in sources.values():
            for source in value or ():
                if source.path not in known:
                    known.append(source.path)
        unindexed = [pin.commit for pin in line if sources[pin.commit] is None]
        for pin in line:
            if sources[pin.commit] is None:
                self.check()
                sources[pin.commit] = self._manifest_sources(pin, known)
        self._load((pin, sources[pin.commit] or []) for pin in line)
        snapshots = [
            self._snapshot(pin, name, sources[pin.commit] or [], pin.commit not in unindexed,
                           LOOKUP_KNOWN_PATHS if pin.commit in unindexed else LOOKUP_INDEX)
            for pin in line
        ]
        if unindexed:
            warnings.append(
                f"{len(unindexed)} pinned commit(s) are not indexed: only the "
                f"{len(known)} path(s) where indexed commits declare {name!r} were checked "
                "there (lookup: known-paths); run txray index on them for a full answer"
            )
        timelines = self._timelines(snapshots)
        if not any(s.declarations for s in snapshots):
            warnings.append(f"no declaration of {name!r} at any pinned commit of this line")
        return ParamHistory(
            name=name,
            base=line[0].commit,
            head=line[-1].commit,
            order=order,
            commits=tuple(snapshots),
            timelines=tuple(timelines),
            history_complete=not gaps,
            gaps=tuple(gaps),
            not_on_line=tuple(side),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _timelines(snapshots: list[ParamSnapshot]) -> list[ParamTimeline]:
        keys: list[tuple[str, str, str, int]] = []
        for snapshot in snapshots:
            for decl in snapshot.declarations:
                if decl.key not in keys:
                    keys.append(decl.key)
        keys.sort(key=lambda k: (k[0].encode("utf-8", "surrogateescape"), k[1], k[2], k[3]))
        timelines = []
        for key in keys:
            points: list[tuple[str, str, str | None]] = []
            changes: list[ParamChange] = []
            states: list[tuple[str, tuple[str, Any]]] = []  # (commit, state) at each change
            previous: ParamDeclaration | None = None
            previous_commit: str | None = None
            for snapshot in snapshots:
                current = next((d for d in snapshot.declarations if d.key == key), None)
                if current is not None:
                    points.append((snapshot.commit, snapshot.committer_time, current.value))
                if previous_commit is not None:
                    event = None
                    if previous is None and current is not None:
                        event = DECLARED
                    elif previous is not None and current is None:
                        event = REMOVED
                    elif previous is not None and current is not None \
                            and previous.value != current.value:
                        event = VALUE_CHANGED
                    if event is not None:
                        changes.append(ParamChange(event, snapshot.commit,
                                                   snapshot.committer_time, previous_commit,
                                                   previous, current))
                state = ("value", current.value) if current is not None else ("absent", None)
                if not states or states[-1][1] != state:
                    states.append((snapshot.commit, state))
                previous, previous_commit = current, snapshot.commit
            reversions = []
            for item in returns([state for _, state in states]):
                kind, value = item["value"]
                reversions.append({
                    "value": value if kind == "value" else None,
                    "left_at": states[item["left_at"] + 1][0],
                    "back_at": states[item["back_at"]][0],
                })
            timelines.append(ParamTimeline(
                path=key[0], declaration=key[1], qualname=key[2], ordinal=key[3],
                points=tuple(points), changes=tuple(changes), reversions=tuple(reversions),
                current=previous,
            ))
        return timelines


__all__ = [
    "DECLARED",
    "LOOKUP_INDEX",
    "LOOKUP_KNOWN_PATHS",
    "MAX_HISTORY_COMMITS",
    "MAX_NAME_CHARS",
    "NO_DEFAULT_NOTE",
    "REMOVED",
    "VALUE_CHANGED",
    "ParamChange",
    "ParamDeclaration",
    "ParamHistory",
    "ParamResolver",
    "ParamSnapshot",
    "ParamTimeline",
    "check_name",
    "name_matches",
    "public_default_note",
    "returns",
]
