"""Native code index (Milestone 2): lexical search, symbols and call candidates per commit.

:class:`CodeIndex` builds and queries the index of pinned commits in a snapshot store.

* **Input.** Only manifest entries classified ``parsed-candidate`` or ``text`` are indexed,
  and only from blob bytes in the snapshot mirror, verified against the manifest SHA-256.
  Every manifest path gets a coverage row: lexical status ``indexed`` or ``skipped`` with a
  reason (the manifest's exclusion reason, or ``not-utf8``), and a syntax status
  (``parsed``, ``partial``, ``failed``, ``not-applicable`` for text files, ``unsupported``
  when no backend is available, ``skipped`` when not indexed), plus the backend used.
* **Lexical index.** SQLite FTS5 with the trigram tokenizer over the non-blank lines of
  each blob; search semantics are in :mod:`timelinexray.index.query`.
* **Symbols and calls.** From :mod:`timelinexray.syntax`, cached per
  ``(blob_oid, language, backend, backend_version)``. Symbol spans carry the SHA-256 of
  their exact bytes. Call candidates are syntactic and unresolved.
* **Scoping.** Each query takes a commit, starts from that commit's ``files`` rows and
  returns that commit id in every result; see :mod:`timelinexray.index.schema`.
* **Incremental builds.** Indexing a commit reads and parses only blobs the index does not
  have yet; the result of every query equals a clean build's (tested on fixtures and on
  two upstream commits).
"""

from __future__ import annotations

import sqlite3
import weakref
from pathlib import Path
from types import TracebackType

from ..snapshot.store import PinRecord, SnapshotStore
from ..syntax import Registry, default_registry
from . import query as _query
from .build import BuildReport, build_generation
from .query import (
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
    SYMBOLS_DEFAULT_LIMIT,
    SYMBOLS_MAX_LIMIT,
    CallRecord,
    Coverage,
    CoverageRow,
    Generation,
    SearchHit,
    SearchResult,
    SymbolRecord,
    SymbolsResult,
    glob_literal,
    path_param,
)
from .schema import (
    INDEX_SCHEMA_VERSION,
    database_path,
    journal_mode,
    open_for_read,
    open_for_write,
)

INDEX_DIRECTORY = "index"


class CodeIndex:
    """The code index of one snapshot store (``<store>/index`` unless ``root`` is given)."""

    def __init__(
        self,
        store: SnapshotStore,
        root: Path | str | None = None,
        registry: Registry | None = None,
    ) -> None:
        self.store = store
        self.root = Path(root) if root is not None else store.root / INDEX_DIRECTORY
        self.registry = registry if registry is not None else default_registry()

    @property
    def path(self) -> Path:
        return database_path(self.root)

    def _check_common(self, commit: str, path_glob: str | None) -> None:
        """Validate arguments and the pin before the database is opened."""
        if path_glob is not None:
            _query.check_pattern(path_glob, "path pattern", path=True)
        self.store.get_pin(commit)

    def build(self, commit: str, *, rebuild: bool = False) -> BuildReport:
        """Index a pinned commit; incremental unless ``rebuild`` re-derives all its blobs."""
        db = open_for_write(self.root)
        try:
            return build_generation(db, self.store, commit, self.registry, rebuild=rebuild)
        finally:
            db.close()

    def generation(self, commit: str) -> Generation:
        pin = self.store.get_pin(commit)
        db = open_for_read(self.root)
        try:
            return _query.generation(db, pin, self.root)
        finally:
            db.close()

    def search(
        self,
        commit: str,
        query: str,
        *,
        path_glob: str | None = None,
        limit: int = SEARCH_DEFAULT_LIMIT,
        literal: bool = False,
    ) -> SearchResult:
        _query.parse_query(query, literal=literal)
        _query.check_limit(limit, SEARCH_MAX_LIMIT)
        self._check_common(commit, path_glob)
        db = open_for_read(self.root)
        try:
            return _query.search(
                db, self.store, self.root, commit, query,
                path_glob=path_glob, limit=limit, literal=literal,
            )
        finally:
            db.close()

    def symbols(
        self,
        commit: str,
        *,
        path_glob: str | None = None,
        kind: str | None = None,
        name: str | None = None,
        limit: int = SYMBOLS_DEFAULT_LIMIT,
        with_calls: bool = False,
    ) -> SymbolsResult:
        _query.check_limit(limit, SYMBOLS_MAX_LIMIT)
        if kind is not None:
            _query.check_kind(kind)
        if name is not None:
            _query.check_pattern(name, "symbol name")
        self._check_common(commit, path_glob)
        db = open_for_read(self.root)
        try:
            return _query.symbols(
                db, self.store, self.root, commit,
                path_glob=path_glob, kind=kind, name=name, limit=limit, with_calls=with_calls,
            )
        finally:
            db.close()

    def calls(
        self,
        commit: str,
        *,
        path_glob: str | None = None,
        callee: str | None = None,
        caller: str | None = None,
        limit: int = SYMBOLS_DEFAULT_LIMIT,
    ) -> tuple[int, tuple[CallRecord, ...]]:
        _query.check_limit(limit, SYMBOLS_MAX_LIMIT)
        for value, what in ((callee, "callee name"), (caller, "caller name")):
            if value is not None:
                _query.check_pattern(value, what)
        self._check_common(commit, path_glob)
        db = open_for_read(self.root)
        try:
            return _query.calls(
                db, self.store, self.root, commit,
                path_glob=path_glob, callee=callee, caller=caller, limit=limit,
            )
        finally:
            db.close()

    def read_session(self) -> ReadSession:
        """A :class:`ReadSession` for many per-file lookups (see its documentation)."""
        return ReadSession(self)

    def coverage(self, commit: str) -> Coverage:
        self._check_common(commit, None)
        db = open_for_read(self.root)
        try:
            return _query.coverage(db, self.store, self.root, commit)
        finally:
            db.close()


class ReadSession:
    """One read-only connection reused for many per-file lookups of one command.

    ``CodeIndex.symbols`` opens a connection, resolves the pin and checks the generation
    for every call, which dominates a diff or digest that asks for the symbols of
    hundreds of files. A session does each of those once: one connection (in WAL mode a
    single read snapshot, so every lookup sees the same committed generation), one pin
    lookup and one generation integrity check per commit, and an exact-path query instead
    of a GLOB scan. Results equal ``CodeIndex.symbols`` for the same path (tested).
    Close it with ``close()`` or ``with``; an unclosed session closes its connection when
    it is collected.
    """

    def __init__(self, index: CodeIndex) -> None:
        self._index = index
        self._db: sqlite3.Connection | None = None
        self._pins: dict[str, PinRecord] = {}
        self._finalizer: weakref.finalize | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._db is None:
            db = open_for_read(self._index.root)
            try:
                if journal_mode(db) == "wal":
                    db.execute("BEGIN")  # a read snapshot; never blocks a writer in WAL mode
            except BaseException:
                db.close()
                raise
            self._db = db
            self._finalizer = weakref.finalize(self, db.close)
        return self._db

    def file_symbols(self, commit: str, path: str) -> tuple[SymbolRecord, ...]:
        """The symbols of ``path`` at ``commit`` (an indexed, pinned commit)."""
        db = self._connection()
        pin = self._pins.get(commit)
        if pin is None:
            pin = self._index.store.get_pin(commit)
            _query.generation(db, pin, self._index.root)
            self._pins[commit] = pin
        return _query.file_symbols(db, pin, path)

    def close(self) -> None:
        if self._finalizer is not None:
            self._finalizer()
            self._finalizer = None
        self._db = None
        self._pins.clear()

    def __enter__(self) -> ReadSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


__all__ = [
    "INDEX_DIRECTORY",
    "INDEX_SCHEMA_VERSION",
    "SEARCH_DEFAULT_LIMIT",
    "SEARCH_MAX_LIMIT",
    "SYMBOLS_DEFAULT_LIMIT",
    "SYMBOLS_MAX_LIMIT",
    "BuildReport",
    "CallRecord",
    "CodeIndex",
    "Coverage",
    "CoverageRow",
    "Generation",
    "ReadSession",
    "SearchHit",
    "SearchResult",
    "SymbolRecord",
    "SymbolsResult",
    "glob_literal",
    "path_param",
]
