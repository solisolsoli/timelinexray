"""Milestone 2 symbols of one blob, from the code index when it has the commit.

:class:`SymbolSource` answers "which declarations does this file have at this commit?"
with the records of :mod:`timelinexray.syntax` (the M2 extractors):

* when a :class:`timelinexray.index.CodeIndex` is given and the commit is indexed, the
  symbols and the file's syntax status come from the index (``CodeIndex.symbols`` and
  ``CodeIndex.coverage``);
* otherwise the same M2 registry extracts them from the blob text, with the same
  validation the index build applies.

Both paths produce the same records for the same blob and backend (tested), so a diff or
digest does not depend on whether the commits were indexed.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import TxrayError
from ..index import CodeIndex, CoverageRow, ReadSession
from ..span import LineMap
from ..syntax import Registry, backend_key, default_registry, extract_checked
from .rules import NATIVE_LANGUAGES

NOT_NATIVE = "not-native"
NOT_UTF8 = "not-utf8"
UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class SymbolInfo:
    kind: str
    name: str
    qualname: str
    container: str | None
    start_line: int
    name_line: int
    end_line: int
    signature: str
    modifiers: tuple[str, ...]

    @property
    def span(self) -> int:
        return self.end_line - self.start_line


@dataclass(frozen=True, slots=True)
class FileSymbols:
    status: str  # parsed, partial, failed, unsupported, not-utf8, not-native
    reason: str | None
    backend: str | None
    symbols: tuple[SymbolInfo, ...]


def _sort_key(symbol: SymbolInfo) -> tuple[int, int, int, str, str]:
    return (symbol.start_line, symbol.name_line, symbol.end_line, symbol.kind, symbol.qualname)


class SymbolSource:
    """Symbols per blob; see the module documentation."""

    def __init__(self, index: CodeIndex | None = None, registry: Registry | None = None) -> None:
        self.index = index
        self.registry = registry if registry is not None else (
            index.registry if index is not None else default_registry()
        )
        self._coverage: dict[str, dict[str, CoverageRow] | None] = {}
        self._session: ReadSession | None = None

    def close(self) -> None:
        """Release the index read session, if one was opened (the source stays usable)."""
        if self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self) -> SymbolSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _indexed_rows(self, commit: str) -> dict[str, CoverageRow] | None:
        if self.index is None:
            return None
        if commit not in self._coverage:
            try:
                rows = self.index.coverage(commit).rows
                self._coverage[commit] = {row.path: row for row in rows}
            except TxrayError:
                self._coverage[commit] = None
        return self._coverage[commit]

    def for_blob(
        self, commit: str, path: str, language: str | None, data: bytes, text: str | None
    ) -> FileSymbols:
        if language not in NATIVE_LANGUAGES:
            return FileSymbols(NOT_NATIVE, None, None, ())
        if text is None:
            return FileSymbols(NOT_UTF8, None, None, ())
        rows = self._indexed_rows(commit)
        row = rows.get(path) if rows is not None else None
        if row is not None and row.syntax_status in ("parsed", "partial", "failed"):
            return self._from_index(commit, path, row)
        backend = self.registry.select(language)
        if backend is None:
            return FileSymbols(UNSUPPORTED, "no syntax backend available", None, ())
        extraction = extract_checked(backend, text, language, LineMap.of(data).line_count)
        symbols = tuple(
            sorted(
                (
                    SymbolInfo(
                        kind=symbol.kind,
                        name=symbol.name,
                        qualname=symbol.qualname,
                        container=symbol.container,
                        start_line=symbol.start_line,
                        name_line=symbol.name_line,
                        end_line=symbol.end_line,
                        signature=symbol.signature,
                        modifiers=tuple(symbol.modifiers),
                    )
                    for symbol in extraction.symbols
                ),
                key=_sort_key,
            )
        )
        return FileSymbols(extraction.status, extraction.reason, backend_key(backend), symbols)

    def _from_index(self, commit: str, path: str, row: CoverageRow) -> FileSymbols:
        assert self.index is not None
        if self._session is None:  # one connection, pin lookup and generation check per commit
            self._session = self.index.read_session()
        symbols = tuple(
            sorted(
                (
                    SymbolInfo(
                        kind=record.kind,
                        name=record.name,
                        qualname=record.qualname,
                        container=record.container,
                        start_line=record.start_line,
                        name_line=record.name_line,
                        end_line=record.end_line,
                        signature=record.signature,
                        modifiers=tuple(record.modifiers),
                    )
                    for record in self._session.file_symbols(commit, path)
                ),
                key=_sort_key,
            )
        )
        return FileSymbols(row.syntax_status, row.syntax_reason, row.backend, symbols)
