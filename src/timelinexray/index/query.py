"""Queries over one commit's index generation: lexical search, symbols, calls, coverage.

Search semantics
----------------
A query is split on whitespace into terms (``literal=True`` keeps the whole query as one
term, spaces included). A hit is one line of an indexed file that contains **every** term
as a case-insensitive substring; ASCII case folding is exact, other scripts follow the
SQLite trigram tokenizer's folding. The FTS5 index only proposes candidate lines: every
term is passed to ``MATCH`` as a double-quoted FTS5 string (inner quotes doubled), so no
query text is ever interpreted as FTS5 syntax or SQL, and each candidate is then checked
in Python, so a hit is never reported for a line that does not contain all terms. Terms
shorter than three characters cannot use the trigram index; they are checked on the
candidates of the longer terms, and a query needs at least one term of three or more
characters. Hits are ordered by path bytes, then line.

Each hit's line span is read back from the blob in the snapshot mirror with
:func:`timelinexray.span.read_span` (after checking the blob against the manifest), so the
``span_sha256`` it reports is exactly what ``txray show`` reports for the same lines.

``--path`` patterns use SQLite ``GLOB`` on the repository path: case-sensitive, ``*`` and
``?`` also match ``/``, ``[...]`` is a character class.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import IntegrityError, InvalidInput, NotFound
from ..snapshot.store import PinRecord, SnapshotStore
from ..span import LineMap, read_span
from ..syntax import CALL_RELATION, CALL_RESOLUTION, SYMBOL_KINDS
from .schema import LINE_BITS, LINE_MASK

SEARCH_DEFAULT_LIMIT = 20
SEARCH_MAX_LIMIT = 1000
SYMBOLS_DEFAULT_LIMIT = 200
SYMBOLS_MAX_LIMIT = 100_000
MAX_QUERY_CHARS = 512
MAX_TERMS = 16
MIN_INDEXED_TERM = 3
MAX_PATTERN_CHARS = 512
SNIPPET_MAX = 240

_GLOB_CHARS = frozenset("*?[")
#: Paths are stored as BLOBs (exact bytes). SQLite built with SQLITE_LIKE_DOESNT_MATCH_BLOBS
#: (for example the Python builds on GitHub's Ubuntu runners) makes GLOB false for any BLOB
#: operand, so the path is cast to TEXT first; the bytes compared are the same.
#: The pattern is bound as bytes (see :func:`path_param`) and cast too.
_PATH_GLOB = "CAST(f.path AS TEXT) GLOB CAST(? AS TEXT)"


# -- input validation ----------------------------------------------------------------------


def _has_control(text: str) -> bool:
    return any((ord(char) < 0x20 and char != "\t") or ord(char) == 0x7F for char in text)


def parse_query(query: object, *, literal: bool = False) -> tuple[str, ...]:
    """The search terms of ``query`` (see the module documentation)."""
    if not isinstance(query, str) or not query.strip():
        raise InvalidInput("search query must be a non-empty string")
    if len(query) > MAX_QUERY_CHARS:
        raise InvalidInput(f"search query is longer than {MAX_QUERY_CHARS} characters")
    if _has_control(query):
        raise InvalidInput("search query must not contain control characters or NUL")
    if literal:
        terms: tuple[str, ...] = (query,)
    else:
        terms = tuple(dict.fromkeys(query.split()))
    if len(terms) > MAX_TERMS:
        raise InvalidInput(f"search query has more than {MAX_TERMS} terms")
    _check_text(query, "search query")
    if not any(len(term) >= MIN_INDEXED_TERM for term in terms):
        raise InvalidInput(
            f"give at least one search term of {MIN_INDEXED_TERM} or more characters "
            "(the trigram index cannot look up shorter terms)"
        )
    return terms


def fts_expression(terms: tuple[str, ...]) -> str:
    """An FTS5 MATCH expression that treats every term as a literal quoted string."""
    quoted = [
        '"' + term.replace('"', '""') + '"' for term in terms if len(term) >= MIN_INDEXED_TERM
    ]
    return " AND ".join(quoted)


def _check_text(value: str, what: str) -> None:
    """Refuse a lone surrogate in a value that is matched against a text column or an FTS
    query: no stored text can contain one, and SQLite cannot bind it."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidInput(
            f"{what} contains a character that is not valid text (a lone surrogate)"
        ) from None


def check_pattern(value: object, what: str, *, path: bool = False) -> str:
    """Validate a name or path pattern. A path pattern (``path=True``) may carry the
    ``surrogateescape`` form of a non-UTF-8 repository path (U+DC80..U+DCFF); any other
    lone surrogate cannot name a path and is refused here, before the database is opened."""
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"{what} must be a non-empty string")
    if len(value) > MAX_PATTERN_CHARS:
        raise InvalidInput(f"{what} is longer than {MAX_PATTERN_CHARS} characters")
    if "\0" in value or _has_control(value):
        raise InvalidInput(f"{what} must not contain control characters or NUL")
    if path:
        path_param(value, what)
    else:
        _check_text(value, what)
    return value


def path_param(pattern: str, what: str = "path pattern") -> bytes:
    """A path pattern as the bytes SQLite should compare: paths are stored as the exact git
    bytes, and a non-UTF-8 name reaches Python as a str with lone surrogates (decoded with
    ``surrogateescape``) that cannot be bound as text. Compare with
    ``CAST(path AS TEXT) GLOB CAST(? AS TEXT)``."""
    try:
        return pattern.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        raise InvalidInput(
            f"{what} contains a character that is not valid text (a lone surrogate)"
        ) from None


def check_limit(value: object, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise InvalidInput(f"limit must be an integer from 1 to {maximum}, got {value!r}")
    return value


def check_kind(value: object) -> str:
    if value not in SYMBOL_KINDS:
        raise InvalidInput(f"unknown symbol kind {value!r}; known kinds: {', '.join(SYMBOL_KINDS)}")
    return str(value)


def _decode_path(raw: bytes | str) -> str:
    if isinstance(raw, str):  # pragma: no cover - paths are stored as BLOB
        return raw
    return bytes(raw).decode("utf-8", "surrogateescape")


# -- records ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Generation:
    commit: str
    tree: str
    manifest_sha256: str
    backends: dict[str, str]
    files: int
    lexical_indexed: int
    syntax_files: int
    symbols: int
    calls: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "tree": self.tree,
            "manifest_sha256": self.manifest_sha256,
            "backends": dict(sorted(self.backends.items())),
            "files": self.files,
            "lexical_indexed": self.lexical_indexed,
            "syntax_files": self.syntax_files,
            "symbols": self.symbols,
            "calls": self.calls,
        }


@dataclass(frozen=True, slots=True)
class SearchHit:
    commit: str
    path: str
    language: str | None
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    snippet: str
    span_sha256: str
    blob_oid: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "path": self.path,
            "language": self.language,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "snippet": self.snippet,
            "span_sha256": self.span_sha256,
            "blob_oid": self.blob_oid,
        }


@dataclass(frozen=True, slots=True)
class SearchResult:
    commit: str
    query: str
    terms: tuple[str, ...]
    literal: bool
    path_glob: str | None
    limit: int
    total: int
    hits: tuple[SearchHit, ...]

    @property
    def truncated(self) -> bool:
        return self.total > len(self.hits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "query": self.query,
            "terms": list(self.terms),
            "literal": self.literal,
            "match": "every term as a case-insensitive substring of one line",
            "path_glob": self.path_glob,
            "limit": self.limit,
            "total": self.total,
            "truncated": self.truncated,
            "hits": [hit.to_dict() for hit in self.hits],
        }


@dataclass(frozen=True, slots=True)
class CallRecord:
    commit: str
    path: str
    caller: str
    caller_kind: str
    callee: str
    qualifier: str | None
    form: str
    line: int
    backend: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "path": self.path,
            "caller": self.caller,
            "caller_kind": self.caller_kind,
            "callee": self.callee,
            "qualifier": self.qualifier,
            "form": self.form,
            "line": self.line,
            "relation": CALL_RELATION,
            "resolution": CALL_RESOLUTION,
            "evidence": "syntactic candidate: a name followed by an argument list",
            "backend": self.backend,
        }


@dataclass(frozen=True, slots=True)
class SymbolRecord:
    commit: str
    path: str
    language: str | None
    kind: str
    name: str
    container: str | None
    qualname: str
    start_line: int
    name_line: int
    end_line: int
    span_sha256: str
    signature: str
    modifiers: tuple[str, ...]
    backend: str
    blob_oid: str
    calls: tuple[CallRecord, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "commit": self.commit,
            "path": self.path,
            "language": self.language,
            "kind": self.kind,
            "name": self.name,
            "container": self.container,
            "qualname": self.qualname,
            "start_line": self.start_line,
            "name_line": self.name_line,
            "end_line": self.end_line,
            "span_sha256": self.span_sha256,
            "signature": self.signature,
            "modifiers": list(self.modifiers),
            "backend": self.backend,
            "blob_oid": self.blob_oid,
        }
        if self.calls is not None:
            data["calls"] = [call.to_dict() for call in self.calls]
        return data


@dataclass(frozen=True, slots=True)
class SymbolsResult:
    commit: str
    filters: dict[str, Any]
    limit: int
    total: int
    symbols: tuple[SymbolRecord, ...]

    @property
    def truncated(self) -> bool:
        return self.total > len(self.symbols)

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "filters": self.filters,
            "limit": self.limit,
            "total": self.total,
            "truncated": self.truncated,
            "symbols": [symbol.to_dict() for symbol in self.symbols],
        }


@dataclass(frozen=True, slots=True)
class CoverageRow:
    path: str
    blob_oid: str
    language: str | None
    classification: str
    lexical_status: str
    lexical_reason: str | None
    line_count: int | None
    indexed_lines: int | None
    syntax_status: str
    syntax_reason: str | None
    backend: str | None
    symbols: int | None
    calls: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "blob_oid": self.blob_oid,
            "language": self.language,
            "classification": self.classification,
            "lexical": {
                "status": self.lexical_status,
                "reason": self.lexical_reason,
                "line_count": self.line_count,
                "indexed_lines": self.indexed_lines,
            },
            "syntax": {
                "status": self.syntax_status,
                "reason": self.syntax_reason,
                "backend": self.backend,
                "symbols": self.symbols,
                "calls": self.calls,
            },
        }


@dataclass(frozen=True)
class Coverage:
    commit: str
    rows: tuple[CoverageRow, ...]
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, files: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {"commit": self.commit, "summary": self.summary}
        if files:
            data["files"] = [row.to_dict() for row in self.rows]
        return data


def summarize(rows: tuple[CoverageRow, ...]) -> dict[str, Any]:
    lexical: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    syntax: Counter[str] = Counter()
    by_language: dict[str, Counter[str]] = {}
    backends: Counter[str] = Counter()
    for row in rows:
        lexical[row.lexical_status] += 1
        if row.lexical_reason:
            skipped[row.lexical_reason] += 1
        syntax[row.syntax_status] += 1
        if row.backend:
            backends[row.backend] += 1
        if row.backend and row.language:
            language = by_language.setdefault(row.language, Counter())
            language["files"] += 1
            language[row.syntax_status] += 1
            language["symbols"] += row.symbols or 0
            language["calls"] += row.calls or 0
    return {
        "files": len(rows),
        "lexical": dict(sorted(lexical.items())),
        "lexical_skipped_by_reason": dict(sorted(skipped.items())),
        "syntax": dict(sorted(syntax.items())),
        "syntax_backends": dict(sorted(backends.items())),
        "languages": {name: dict(sorted(c.items())) for name, c in sorted(by_language.items())},
        "symbols": sum(row.symbols or 0 for row in rows),
        "calls": sum(row.calls or 0 for row in rows),
    }


# -- the queries -----------------------------------------------------------------------------


def generation(db: sqlite3.Connection, pin: PinRecord, root: Path) -> Generation:
    row = db.execute(
        "SELECT commit_id, tree, manifest_sha256, backends, files, lexical_indexed, "
        "syntax_files, symbols, calls FROM generations WHERE commit_id = ?",
        (pin.commit,),
    ).fetchone()
    if row is None:
        raise NotFound(
            f"commit {pin.commit} is not indexed in {root}; run: txray index {pin.commit[:12]}"
        )
    if row[2] != pin.manifest_sha256:
        raise IntegrityError(
            f"the index of {pin.commit} was built from manifest sha256 {row[2]}, but the pin "
            f"records {pin.manifest_sha256}; run: txray index {pin.commit[:12]} --rebuild"
        )
    return Generation(row[0], row[1], row[2], json.loads(row[3]), *row[4:])


def search(
    db: sqlite3.Connection,
    store: SnapshotStore,
    root: Path,
    commit: str,
    query: str,
    *,
    path_glob: str | None = None,
    limit: int = SEARCH_DEFAULT_LIMIT,
    literal: bool = False,
) -> SearchResult:
    pin, manifest = store.load_manifest(commit)
    generation(db, pin, root)
    terms = parse_query(query, literal=literal)
    limit = check_limit(limit, SEARCH_MAX_LIMIT)
    sql = (
        f"SELECT f.path, f.language, f.blob_oid, (blob_lines.rowid & {LINE_MASK}) AS line, "
        "blob_lines.text FROM blob_lines "
        f"JOIN blobs AS b ON b.blob_key = (blob_lines.rowid >> {LINE_BITS}) "
        "JOIN files AS f INDEXED BY files_by_blob ON f.blob_oid = b.blob_oid "
        "WHERE blob_lines MATCH ? AND f.commit_id = ? AND f.lexical_status = 'indexed"
        "'"
    )
    params: list[Any] = [fts_expression(terms), pin.commit]
    if path_glob is not None:
        sql += " AND " + _PATH_GLOB
        params.append(path_param(check_pattern(path_glob, "path pattern", path=True)))
    sql += " ORDER BY f.path, line"
    folded = [term.lower() for term in terms]
    matches: list[tuple[str, str | None, str, int]] = []
    total = 0
    for raw_path, language, oid, line, text in db.execute(sql, params):
        lowered = text.lower()
        if all(term in lowered for term in folded):
            total += 1
            if len(matches) < limit:
                matches.append((_decode_path(raw_path), language, oid, line))
    hits: list[SearchHit] = []
    if matches:
        blobs = _read_verified(store, pin, manifest, {(path, oid) for path, _, oid, _ in matches})
        line_maps: dict[str, LineMap] = {}
        for path, language, oid, line in matches:
            data, sha256 = blobs[oid]
            if oid not in line_maps:
                line_maps[oid] = LineMap.of(data)
            span = read_span(
                data, line, line, commit=pin.commit, path=path, blob_oid=oid,
                line_map=line_maps[oid], blob_sha256=sha256,
            )
            hits.append(
                SearchHit(
                    commit=pin.commit,
                    path=path,
                    language=language,
                    start_line=span.start_line,
                    end_line=span.end_line,
                    start_byte=span.start_byte,
                    end_byte=span.end_byte,
                    snippet=_snippet(span.data.decode("utf-8"), folded),
                    span_sha256=span.sha256,
                    blob_oid=oid,
                )
            )
    return SearchResult(pin.commit, query, terms, literal, path_glob, limit, total, tuple(hits))


def _read_verified(
    store: SnapshotStore, pin: PinRecord, manifest: Any, wanted: set[tuple[str, str]]
) -> dict[str, tuple[bytes, str]]:
    """Blob bytes and SHA-256 for each wanted oid, checked against the manifest."""
    expected: dict[str, tuple[int | None, str | None, str]] = {}
    for path, oid in sorted(wanted):
        entry = manifest.entry(path)
        if entry is None or entry.oid != oid:
            raise IntegrityError(f"index row for {path} does not match the manifest of {pin.commit}")
        expected[oid] = (entry.size, entry.sha256, path)
    blobs: dict[str, tuple[bytes, str]] = {}
    for oid, data in store.repo_for(pin).read_blobs(sorted(expected)):
        size, sha256, path = expected[oid]
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != size or digest != sha256:
            raise IntegrityError(f"blob {oid} for {path} does not match its manifest entry")
        blobs[oid] = (data, digest)
    return blobs


def _snippet(line: str, folded: list[str]) -> str:
    text = line.rstrip("\r\n").strip()
    if len(text) <= SNIPPET_MAX:
        return text
    lowered = text.lower()
    first = min((lowered.find(term) for term in folded if term in lowered), default=0)
    start = max(0, first - SNIPPET_MAX // 3)
    end = start + SNIPPET_MAX - 6
    return ("..." if start else "") + text[start:end] + ("..." if end < len(text) else "")


def _name_suffixes(name: str) -> list[str]:
    """``name`` and every suffix that starts after a ``::`` or ``.`` separator.

    A symbol whose qualified name equals ``name`` has its own name among these (a
    qualified name is the container chain joined with ``::`` or ``.`` plus the name), so
    an exact lookup can be restricted to ``name IN (...)``, which the ``symbols_by_name``
    index answers; without it SQLite scans every symbol of the commit for the ``OR``.
    """
    suffixes = [name]
    for position, char in enumerate(name):
        if char == "." or (char == ":" and name.startswith("::", position)):
            tail = name[position + (2 if char == ":" else 1):]
            if tail and tail not in suffixes:
                suffixes.append(tail)
    return suffixes


def _name_clause(column_a: str, column_b: str, name: str) -> tuple[str, list[str]]:
    if _GLOB_CHARS & set(name):
        return f"({column_a} GLOB ? OR {column_b} GLOB ?)", [name, name]
    suffixes = _name_suffixes(name)
    marks = ", ".join("?" * len(suffixes))
    return (f"({column_a} IN ({marks}) AND ({column_a} = ? OR {column_b} = ?))",
            [*suffixes, name, name])


def _query_prologue(
    db: sqlite3.Connection,
    store: SnapshotStore,
    root: Path,
    commit: str,
    limit: int,
    path_glob: str | None,
) -> tuple[PinRecord, int, list[str], list[Any]]:
    """The pin, the checked limit and the start of the WHERE clause (commit, path glob)
    shared by :func:`symbols` and :func:`calls`."""
    pin = store.get_pin(commit)
    generation(db, pin, root)
    limit = check_limit(limit, SYMBOLS_MAX_LIMIT)
    where = ["f.commit_id = ?"]
    params: list[Any] = [pin.commit]
    if path_glob is not None:
        where.append(_PATH_GLOB)
        params.append(path_param(check_pattern(path_glob, "path pattern", path=True)))
    return pin, limit, where, params


def symbols(
    db: sqlite3.Connection,
    store: SnapshotStore,
    root: Path,
    commit: str,
    *,
    path_glob: str | None = None,
    kind: str | None = None,
    name: str | None = None,
    limit: int = SYMBOLS_DEFAULT_LIMIT,
    with_calls: bool = False,
) -> SymbolsResult:
    pin, limit, where, params = _query_prologue(db, store, root, commit, limit, path_glob)
    if kind is not None:
        where.append("s.kind = ?")
        params.append(check_kind(kind))
    if name is not None:
        clause, values = _name_clause("s.name", "s.qualname", check_pattern(name, "symbol name"))
        where.append(clause)
        params.extend(values)
    filters = {"path": path_glob, "kind": kind, "name": name}
    return _symbols_result(db, pin, where, params, limit, with_calls, filters)


def glob_literal(path: str) -> str:
    """An SQLite GLOB pattern matching exactly ``path``."""
    return "".join(f"[{char}]" if char in "*?[" else char for char in path)


def file_symbols(db: sqlite3.Connection, pin: PinRecord, path: str) -> tuple[SymbolRecord, ...]:
    """The symbols of one file of an indexed commit, selected by exact path equality.

    The same records, in the same order, as ``symbols(path_glob=glob_literal(path),
    limit=SYMBOLS_MAX_LIMIT)`` filtered to ``path`` (the path is compared as the exact
    stored bytes, so a non-UTF-8 name works). The caller has checked the generation of
    ``pin`` (:func:`generation`) on this connection.
    """
    # exact byte equality: no pattern validation, so a valid upstream path with a control
    # character or of any length is looked up instead of refused as a "pattern"
    where = ["f.commit_id = ?", "f.path = ?"]
    params: list[Any] = [pin.commit, path_param(path)]
    filters: dict[str, str | None] = {"path": None, "kind": None, "name": None}
    return _symbols_result(db, pin, where, params, SYMBOLS_MAX_LIMIT, False, filters).symbols


def _symbols_result(
    db: sqlite3.Connection,
    pin: PinRecord,
    where: list[str],
    params: list[Any],
    limit: int,
    with_calls: bool,
    filters: dict[str, str | None],
) -> SymbolsResult:
    base = (
        "FROM files AS f JOIN parses AS p ON p.parse_key = f.parse_key "
        "JOIN symbols AS s ON s.parse_key = f.parse_key WHERE " + " AND ".join(where)
    )
    total = db.execute("SELECT count(*) " + base, params).fetchone()[0]
    rows = db.execute(
        "SELECT f.path, f.language, f.blob_oid, p.backend || '/' || p.backend_version, "
        "s.parse_key, s.ord, s.kind, s.name, s.container, s.qualname, s.start_line, "
        "s.name_line, s.end_line, s.span_sha256, s.signature, s.modifiers "
        + base
        + " ORDER BY f.path, s.start_line, s.ord LIMIT ?",
        [*params, limit],
    ).fetchall()
    call_map: dict[tuple[int, int], list[CallRecord]] = {}
    if with_calls and rows:
        keys = sorted({row[4] for row in rows})
        paths = {row[4]: (_decode_path(row[0]), row[3]) for row in rows}
        qualnames: dict[tuple[int, int], tuple[str, str]] = {}
        for index in range(0, len(keys), 500):
            chunk = keys[index : index + 500]
            marks = ",".join("?" * len(chunk))
            for parse_key, ord_, kind_, qualname in db.execute(
                f"SELECT parse_key, ord, kind, qualname FROM symbols WHERE parse_key IN ({marks})",
                chunk,
            ):
                qualnames[(parse_key, ord_)] = (qualname, kind_)
            for parse_key, caller, callee, qualifier, form, line in db.execute(
                "SELECT parse_key, caller_ord, callee, qualifier, form, line FROM calls "
                f"WHERE parse_key IN ({marks}) ORDER BY parse_key, ord",
                chunk,
            ):
                path, backend = paths[parse_key]
                qualname, caller_kind = qualnames[(parse_key, caller)]
                call_map.setdefault((parse_key, caller), []).append(
                    CallRecord(pin.commit, path, qualname, caller_kind, callee, qualifier, form,
                               line, backend)
                )
    records = tuple(
        SymbolRecord(
            commit=pin.commit,
            path=_decode_path(row[0]),
            language=row[1],
            kind=row[6],
            name=row[7],
            container=row[8],
            qualname=row[9],
            start_line=row[10],
            name_line=row[11],
            end_line=row[12],
            span_sha256=row[13],
            signature=row[14],
            modifiers=tuple(json.loads(row[15])),
            backend=row[3],
            blob_oid=row[2],
            calls=tuple(call_map.get((row[4], row[5]), ())) if with_calls else None,
        )
        for row in rows
    )
    return SymbolsResult(pin.commit, filters, limit, total, records)


def calls(
    db: sqlite3.Connection,
    store: SnapshotStore,
    root: Path,
    commit: str,
    *,
    path_glob: str | None = None,
    callee: str | None = None,
    caller: str | None = None,
    limit: int = SYMBOLS_DEFAULT_LIMIT,
) -> tuple[int, tuple[CallRecord, ...]]:
    """Unresolved call candidates of a commit, optionally filtered; ``(total, records)``."""
    pin, limit, where, params = _query_prologue(db, store, root, commit, limit, path_glob)
    if callee is not None:
        value = check_pattern(callee, "callee name")
        where.append("c.callee GLOB ?" if _GLOB_CHARS & set(value) else "c.callee = ?")
        params.append(value)
    if caller is not None:
        clause, values = _name_clause("s.name", "s.qualname", check_pattern(caller, "caller name"))
        where.append(clause)
        params.extend(values)
    base = (
        "FROM files AS f JOIN parses AS p ON p.parse_key = f.parse_key "
        "JOIN calls AS c ON c.parse_key = f.parse_key "
        "JOIN symbols AS s ON s.parse_key = c.parse_key AND s.ord = c.caller_ord "
        "WHERE " + " AND ".join(where)
    )
    total = db.execute("SELECT count(*) " + base, params).fetchone()[0]
    rows = db.execute(
        "SELECT f.path, p.backend || '/' || p.backend_version, s.qualname, s.kind, c.callee, "
        "c.qualifier, c.form, c.line " + base + " ORDER BY f.path, c.line, c.ord LIMIT ?",
        [*params, limit],
    ).fetchall()
    records = tuple(
        CallRecord(pin.commit, _decode_path(row[0]), row[2], row[3], row[4], row[5], row[6],
                   row[7], row[1])
        for row in rows
    )
    return total, records


def coverage(db: sqlite3.Connection, store: SnapshotStore, root: Path, commit: str) -> Coverage:
    """Every manifest path of the commit with its lexical and syntax status and reason."""
    pin = store.get_pin(commit)
    generation(db, pin, root)
    cursor = db.execute(
        "SELECT f.path, f.blob_oid, f.language, f.classification, f.lexical_status, "
        "f.lexical_reason, b.line_count, b.indexed_lines, f.syntax_status, f.syntax_reason, "
        "CASE WHEN p.parse_key IS NULL THEN NULL "
        "ELSE p.backend || '/' || p.backend_version END, p.symbol_count, p.call_count "
        "FROM files AS f LEFT JOIN parses AS p ON p.parse_key = f.parse_key "
        "LEFT JOIN blobs AS b ON f.lexical_status = 'indexed' AND b.blob_oid = f.blob_oid "
        "WHERE f.commit_id = ? ORDER BY f.path",
        (pin.commit,),
    )
    rows = tuple(CoverageRow(_decode_path(row[0]), *row[1:]) for row in cursor)
    return Coverage(pin.commit, rows, summarize(rows))
