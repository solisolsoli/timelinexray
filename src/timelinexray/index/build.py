"""Building one commit's index generation from its verified manifest and git blobs.

Blob bytes come only from the snapshot mirror (``GitRepo.read_blobs``, checked against the
git object id) and are compared with the manifest's SHA-256 before use; the working tree
is never read. A build runs in one SQLite transaction, so a failure leaves no partial
generation behind.

Incremental rule: a blob whose lexical rows already exist is not read again for the
lexical index, and a blob that already has a parse under the selected backend and version
is not parsed again. Only blobs that are new to the index are read from git. ``rebuild``
re-derives every blob of the commit in place (same internal keys), so other generations
that share those blobs stay consistent.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from ..errors import IntegrityError
from ..snapshot.classify import PARSED_CANDIDATE, TEXT
from ..snapshot.manifest import Manifest, ManifestEntry
from ..snapshot.store import PinRecord, SnapshotStore
from ..span import LineMap
from ..syntax import Backend, Extraction, Registry, backend_key, extract_checked
from .schema import LINE_BITS, LINE_MASK

# lexical_status values
INDEXED = "indexed"
SKIPPED = "skipped"
NOT_UTF8 = "not-utf8"
# syntax_status values beyond the extraction statuses (parsed / partial / failed)
NOT_APPLICABLE = "not-applicable"
UNSUPPORTED = "unsupported"

_CHUNK = 500


@dataclass
class BuildReport:
    commit: str
    manifest_sha256: str
    up_to_date: bool
    rebuild: bool
    backends: dict[str, str]
    files: int = 0
    lexical: Counter[str] = field(default_factory=Counter)  # indexed / skip reasons
    syntax: Counter[str] = field(default_factory=Counter)  # parsed / partial / failed / ...
    symbols: int = 0
    calls: int = 0
    symbols_by_language: dict[str, int] = field(default_factory=dict)
    blobs_read: int = 0
    lexical_new: int = 0
    lexical_reused: int = 0
    parses_new: int = 0
    parses_reused: int = 0
    lines_inserted: int = 0
    seconds: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "manifest_sha256": self.manifest_sha256,
            "up_to_date": self.up_to_date,
            "rebuild": self.rebuild,
            "backends": dict(sorted(self.backends.items())),
            "files": self.files,
            "lexical": dict(sorted(self.lexical.items())),
            "syntax": dict(sorted(self.syntax.items())),
            "symbols": self.symbols,
            "calls": self.calls,
            "symbols_by_language": dict(sorted(self.symbols_by_language.items())),
            "work": {
                "blobs_read": self.blobs_read,
                "lexical_new": self.lexical_new,
                "lexical_reused": self.lexical_reused,
                "parses_new": self.parses_new,
                "parses_reused": self.parses_reused,
                "lines_inserted": self.lines_inserted,
            },
            "seconds": {key: round(value, 3) for key, value in self.seconds.items()},
        }


@dataclass(frozen=True, slots=True)
class _Plan:
    entry: ManifestEntry
    lexical_reason: str | None  # None: indexed lexically
    backend: Backend | None  # the extractor to use, when the path is a native language file


def _plan(entry: ManifestEntry, registry: Registry) -> _Plan:
    if entry.classification not in (PARSED_CANDIDATE, TEXT):
        return _Plan(entry, entry.reason or entry.classification, None)
    if not entry.utf8:
        return _Plan(entry, NOT_UTF8, None)
    backend = None
    if entry.classification == PARSED_CANDIDATE and entry.language:
        backend = registry.select(entry.language)
    return _Plan(entry, None, backend)


def _chunks(items: list[Any], size: int = _CHUNK) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _lines(text: str) -> list[str]:
    """Line contents without terminators, numbered like :class:`timelinexray.span.LineMap`."""
    if not text:
        return []
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return lines


class _Builder:
    def __init__(
        self,
        db: sqlite3.Connection,
        store: SnapshotStore,
        pin: PinRecord,
        manifest: Manifest,
        registry: Registry,
        *,
        rebuild: bool,
    ) -> None:
        self.db = db
        self.store = store
        self.pin = pin
        self.manifest = manifest
        self.registry = registry
        self.rebuild = rebuild
        self.plans = [_plan(entry, registry) for entry in manifest.entries]
        self.report = BuildReport(
            commit=pin.commit,
            manifest_sha256=pin.manifest_sha256,
            up_to_date=False,
            rebuild=rebuild,
            backends=self.backend_map(),
        )

    def backend_map(self) -> dict[str, str]:
        languages = sorted({p.entry.language for p in self.plans if p.backend and p.entry.language})
        chosen: dict[str, str] = {}
        for language in languages:
            backend = self.registry.select(language)
            if backend is not None:
                chosen[language] = backend_key(backend)
        return chosen

    # -- helpers --------------------------------------------------------------------------

    def existing_blob_keys(self, oids: Iterable[str]) -> dict[str, int]:
        found: dict[str, int] = {}
        for chunk in _chunks(sorted(set(oids))):
            marks = ",".join("?" * len(chunk))
            for oid, key in self.db.execute(
                f"SELECT blob_oid, blob_key FROM blobs WHERE blob_oid IN ({marks})", chunk
            ):
                found[oid] = key
        return found

    def existing_parses(self, wanted: set[tuple[str, str, str, str]]) -> dict[tuple[str, str, str, str], int]:
        found: dict[tuple[str, str, str, str], int] = {}
        oids = sorted({item[0] for item in wanted})
        for chunk in _chunks(oids):
            marks = ",".join("?" * len(chunk))
            for row in self.db.execute(
                "SELECT blob_oid, language, backend, backend_version, parse_key FROM parses "
                f"WHERE blob_oid IN ({marks})",
                chunk,
            ):
                key = (row[0], row[1], row[2], row[3])
                if key in wanted:
                    found[key] = row[4]
        return found

    # -- the build ------------------------------------------------------------------------

    def run(self) -> BuildReport:
        clock = time.perf_counter
        report = self.report
        started = clock()
        by_oid: dict[str, ManifestEntry] = {}
        lexical_oids: set[str] = set()
        parse_wanted: set[tuple[str, str, str, str]] = set()
        backends: dict[tuple[str, str, str, str], Backend] = {}
        for plan in self.plans:
            if plan.lexical_reason is not None:
                continue
            entry = plan.entry
            by_oid.setdefault(entry.oid, entry)
            lexical_oids.add(entry.oid)
            if plan.backend is not None and entry.language:
                key = (entry.oid, entry.language, plan.backend.name, plan.backend.version)
                parse_wanted.add(key)
                backends[key] = plan.backend

        blob_keys = self.existing_blob_keys(lexical_oids)
        parse_keys = self.existing_parses(parse_wanted)
        if self.rebuild:
            lexical_needed = set(lexical_oids)
            parse_needed = set(parse_wanted)
        else:
            lexical_needed = lexical_oids - set(blob_keys)
            parse_needed = parse_wanted - set(parse_keys)
        report.lexical_reused = len(lexical_oids) - len(lexical_needed)
        report.parses_reused = len(parse_wanted) - len(parse_needed)
        parses_by_oid: dict[str, list[tuple[str, str, str, str]]] = {}
        for key in parse_needed:
            parses_by_oid.setdefault(key[0], []).append(key)
        to_read = sorted(lexical_needed | set(parses_by_oid))
        report.seconds["plan"] = clock() - started

        repo = self.store.repo_for(self.pin)
        read_seconds = lexical_seconds = parse_seconds = 0.0
        mark = clock()
        for oid, data in repo.read_blobs(to_read):
            now = clock()
            read_seconds += now - mark
            entry = by_oid[oid]
            if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
                raise IntegrityError(
                    f"blob {oid} for {entry.path} does not match its manifest entry"
                )
            report.blobs_read += 1
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise IntegrityError(
                    f"blob {oid} for {entry.path} is recorded as UTF-8 but does not decode: {exc}"
                ) from exc
            line_map = LineMap.of(data)
            if oid in lexical_needed:
                blob_keys[oid] = self.store_lexical(oid, entry, text, line_map, blob_keys.get(oid))
                report.lexical_new += 1
            lexical_done = clock()
            lexical_seconds += lexical_done - now
            for key in sorted(parses_by_oid.get(oid, ())):
                extraction = extract_checked(backends[key], text, key[1], line_map.line_count)
                parse_keys[key] = self.store_parse(key, extraction, data, line_map, parse_keys.get(key))
                report.parses_new += 1
            mark = clock()
            parse_seconds += mark - lexical_done
        report.seconds["read"] = read_seconds
        report.seconds["lexical"] = lexical_seconds
        report.seconds["syntax"] = parse_seconds

        mark = clock()
        self.store_generation(parse_keys, backends)
        report.seconds["write"] = clock() - mark
        report.seconds["total"] = clock() - started
        return report

    def store_lexical(
        self, oid: str, entry: ManifestEntry, text: str, line_map: LineMap, key: int | None
    ) -> int:
        db = self.db
        lines = _lines(text)
        if len(lines) != line_map.line_count:  # pragma: no cover - both split on LF only
            raise IntegrityError(f"line count mismatch for blob {oid}")
        rows_text = [(number, line.rstrip("\r")) for number, line in enumerate(lines, 1)]
        rows_text = [(number, line) for number, line in rows_text if line.strip()]
        if key is None:
            cursor = db.execute(
                "INSERT INTO blobs (blob_oid, sha256, size, line_count, indexed_lines) "
                "VALUES (?, ?, ?, ?, ?)",
                (oid, entry.sha256, entry.size, line_map.line_count, len(rows_text)),
            )
            key = int(cursor.lastrowid)  # type: ignore[arg-type]
        else:
            low = key << LINE_BITS
            db.execute(
                "DELETE FROM blob_lines WHERE rowid BETWEEN ? AND ?", (low, low | LINE_MASK)
            )
            db.execute(
                "UPDATE blobs SET sha256 = ?, size = ?, line_count = ?, indexed_lines = ? "
                "WHERE blob_key = ?",
                (entry.sha256, entry.size, line_map.line_count, len(rows_text), key),
            )
        if line_map.line_count >= 1 << LINE_BITS:
            raise IntegrityError(f"blob {oid} has too many lines for the index")
        base = key << LINE_BITS
        db.executemany(
            "INSERT INTO blob_lines (rowid, text) VALUES (?, ?)",
            [(base | number, line) for number, line in rows_text],
        )
        self.report.lines_inserted += len(rows_text)
        return key

    def store_parse(
        self,
        key: tuple[str, str, str, str],
        extraction: Extraction,
        data: bytes,
        line_map: LineMap,
        parse_key: int | None,
    ) -> int:
        db = self.db
        oid, language, backend, version = key
        values = (
            extraction.status,
            extraction.reason,
            len(extraction.symbols),
            len(extraction.calls),
        )
        if parse_key is None:
            cursor = db.execute(
                "INSERT INTO parses (blob_oid, language, backend, backend_version, status, "
                "reason, symbol_count, call_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (oid, language, backend, version, *values),
            )
            parse_key = int(cursor.lastrowid)  # type: ignore[arg-type]
        else:
            db.execute("DELETE FROM symbols WHERE parse_key = ?", (parse_key,))
            db.execute("DELETE FROM calls WHERE parse_key = ?", (parse_key,))
            db.execute(
                "UPDATE parses SET status = ?, reason = ?, symbol_count = ?, call_count = ? "
                "WHERE parse_key = ?",
                (*values, parse_key),
            )
        symbol_rows = []
        for ord_, symbol in enumerate(extraction.symbols):
            start, end = line_map.byte_range(symbol.start_line, symbol.end_line)
            symbol_rows.append(
                (
                    parse_key, ord_, symbol.kind, symbol.name, symbol.container,
                    symbol.qualname, symbol.start_line, symbol.name_line, symbol.end_line,
                    hashlib.sha256(data[start:end]).hexdigest(), symbol.signature,
                    json.dumps(list(symbol.modifiers)),
                )
            )
        db.executemany(
            "INSERT INTO symbols (parse_key, ord, kind, name, container, qualname, start_line, "
            "name_line, end_line, span_sha256, signature, modifiers) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            symbol_rows,
        )
        db.executemany(
            "INSERT INTO calls (parse_key, ord, caller_ord, callee, qualifier, form, line) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (parse_key, ord_, call.caller, call.callee, call.qualifier, call.form, call.line)
                for ord_, call in enumerate(extraction.calls)
            ],
        )
        return parse_key

    def store_generation(
        self,
        parse_keys: dict[tuple[str, str, str, str], int],
        backends: dict[tuple[str, str, str, str], Backend],
    ) -> None:
        db = self.db
        report = self.report
        commit = self.pin.commit
        db.execute("DELETE FROM files WHERE commit_id = ?", (commit,))
        db.execute("DELETE FROM generations WHERE commit_id = ?", (commit,))
        statuses: dict[int, tuple[str, str | None, int, int]] = {}
        wanted = sorted(set(parse_keys.values()))
        for chunk in _chunks(wanted):
            marks = ",".join("?" * len(chunk))
            for row in db.execute(
                "SELECT parse_key, status, reason, symbol_count, call_count FROM parses "
                f"WHERE parse_key IN ({marks})",
                chunk,
            ):
                statuses[row[0]] = (row[1], row[2], row[3], row[4])
        rows = []
        lexical_indexed = syntax_files = 0
        by_language: Counter[str] = Counter()
        for plan in self.plans:
            entry = plan.entry
            parse_key: int | None = None
            if plan.lexical_reason is not None:
                lexical_status, lexical_reason = SKIPPED, plan.lexical_reason
                syntax_status, syntax_reason = SKIPPED, plan.lexical_reason
            else:
                lexical_status, lexical_reason = INDEXED, None
                lexical_indexed += 1
                if entry.classification == TEXT:
                    syntax_status, syntax_reason = NOT_APPLICABLE, None
                elif plan.backend is None or not entry.language:
                    syntax_status = UNSUPPORTED
                    syntax_reason = f"no available syntax backend for {entry.language}"
                else:
                    key = (entry.oid, entry.language, plan.backend.name, plan.backend.version)
                    parse_key = parse_keys[key]
                    syntax_status, syntax_reason, symbol_count, call_count = statuses[parse_key]
                    syntax_files += 1
                    report.symbols += symbol_count
                    report.calls += call_count
                    by_language[entry.language] += symbol_count
            report.lexical[lexical_status if lexical_reason is None else lexical_reason] += 1
            report.syntax[syntax_status] += 1
            rows.append(
                (
                    commit, entry.path.encode("utf-8", "surrogateescape"), entry.oid,
                    entry.language, entry.classification, lexical_status, lexical_reason,
                    syntax_status, syntax_reason, parse_key,
                )
            )
        db.executemany(
            "INSERT INTO files (commit_id, path, blob_oid, language, classification, "
            "lexical_status, lexical_reason, syntax_status, syntax_reason, parse_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        report.files = len(rows)
        report.symbols_by_language = dict(sorted(by_language.items()))
        db.execute(
            "INSERT INTO generations (commit_id, tree, manifest_sha256, backends, files, "
            "lexical_indexed, syntax_files, symbols, calls) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                commit, self.manifest.tree, self.pin.manifest_sha256,
                json.dumps(report.backends, sort_keys=True), len(rows), lexical_indexed,
                syntax_files, report.symbols, report.calls,
            ),
        )


def build_generation(
    db: sqlite3.Connection,
    store: SnapshotStore,
    commit: str,
    registry: Registry,
    *,
    rebuild: bool = False,
) -> BuildReport:
    """Build (or confirm up to date) the generation of ``commit``; see the module docstring."""
    pin, manifest = store.load_manifest(commit)
    builder = _Builder(db, store, pin, manifest, registry, rebuild=rebuild)
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            "SELECT manifest_sha256, backends FROM generations WHERE commit_id = ?", (pin.commit,)
        ).fetchone()
        current = (pin.manifest_sha256, json.dumps(builder.report.backends, sort_keys=True))
        if row is not None and tuple(row) == current and not rebuild:
            db.execute("ROLLBACK")
            report = builder.report
            report.up_to_date = True
            _fill_from_generation(db, report)
            return report
        report = builder.run()
        db.execute("COMMIT")
        return report
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def _fill_from_generation(db: sqlite3.Connection, report: BuildReport) -> None:
    commit = report.commit
    row = db.execute(
        "SELECT files, symbols, calls FROM generations WHERE commit_id = ?", (commit,)
    ).fetchone()
    report.files, report.symbols, report.calls = row
    for status, reason, count in db.execute(
        "SELECT lexical_status, lexical_reason, count(*) FROM files WHERE commit_id = ? "
        "GROUP BY lexical_status, lexical_reason",
        (commit,),
    ):
        report.lexical[status if reason is None else reason] += count
    for status, count in db.execute(
        "SELECT syntax_status, count(*) FROM files WHERE commit_id = ? GROUP BY syntax_status",
        (commit,),
    ):
        report.syntax[status] += count
    report.symbols_by_language = {
        language: total
        for language, total in db.execute(
            "SELECT f.language, sum(p.symbol_count) FROM files AS f "
            "JOIN parses AS p ON p.parse_key = f.parse_key WHERE f.commit_id = ? "
            "GROUP BY f.language ORDER BY f.language",
            (commit,),
        )
    }
