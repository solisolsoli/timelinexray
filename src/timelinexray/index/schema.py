"""The on-disk index: one SQLite database per snapshot store.

Layout: ``<store>/index/index.sqlite3``. Two kinds of tables live in it.

Commit-scoped (the commit id is a column of every row)
    ``generations`` - one row per indexed commit: the manifest SHA-256 it was built from,
    the syntax backend chosen for each language, and totals.
    ``files`` - one row per manifest path of that commit: blob id, classification, lexical
    status and reason, syntax status and reason, and the parse it uses.

Content-addressed (keyed by blob id, shared by every commit containing the blob)
    ``blobs`` - blob id, SHA-256, size, line count and number of indexed lines.
    ``blob_lines`` - the FTS5 table (``trigram`` tokenizer) holding one row per non-blank
    line; its rowid is ``(blob_key << 24) | line``.
    ``parses`` - one row per ``(blob_oid, language, backend, backend_version)``: the parse
    cache key of P7 section 2.3, with status, reason and counts.
    ``symbols`` / ``calls`` - the records of each parse.

A query always starts from ``files`` rows of one commit, so content-addressed rows are only
reachable through a commit, and every result carries that commit. Keeping lines, symbols
and calls per blob is what makes an incremental build touch only blobs that are new to the
index; per-commit copies of the FTS rows would repeat about the same text for every commit.
Integer keys (``blob_key``, ``parse_key``) are internal and never appear in results, so an
incremental and a clean build compare equal even though their keys differ.

Reader isolation. The database runs in WAL journal mode (set when it is created and
confirmed by every writer, so an index file made before WAL mode is converted the next time
``txray index`` opens it): a reader sees the last committed generation and never waits for
a running build, which holds one write transaction for its whole duration. Readers open
read-only with a busy timeout of :data:`READ_BUSY_TIMEOUT` seconds, the most a reader can
wait on the rare lock a rollback-journal file still needs; a wait inside SQLite's C library
cannot be interrupted by a signal, so this bound is what keeps the MCP time budget honest.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from ..errors import IntegrityError, NotFound, TxrayError

#: ``PRAGMA user_version`` of the database; bump when a table or the line rules change.
INDEX_SCHEMA_VERSION = 1
#: Bits of an FTS rowid that hold the line number (lines are below 16,777,216).
LINE_BITS = 24
LINE_MASK = (1 << LINE_BITS) - 1

DATABASE_NAME = "index.sqlite3"
#: Busy timeout (seconds) of read-only connections; in WAL mode readers never wait for a
#: writer, so this only bounds the wait on an index file not yet converted to WAL.
READ_BUSY_TIMEOUT = 5.0
#: Busy timeout (seconds) of the one writer (``txray index``) when another writer runs.
WRITE_BUSY_TIMEOUT = 60.0
JOURNAL_MODE = "wal"

SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (
    blob_key INTEGER PRIMARY KEY,
    blob_oid TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    line_count INTEGER NOT NULL,
    indexed_lines INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS blob_lines USING fts5(text, tokenize = 'trigram');
CREATE TABLE IF NOT EXISTS parses (
    parse_key INTEGER PRIMARY KEY,
    blob_oid TEXT NOT NULL,
    language TEXT NOT NULL,
    backend TEXT NOT NULL,
    backend_version TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    symbol_count INTEGER NOT NULL,
    call_count INTEGER NOT NULL,
    UNIQUE (blob_oid, language, backend, backend_version)
);
CREATE TABLE IF NOT EXISTS symbols (
    parse_key INTEGER NOT NULL,
    ord INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    container TEXT,
    qualname TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    name_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    span_sha256 TEXT NOT NULL,
    signature TEXT NOT NULL,
    modifiers TEXT NOT NULL,
    PRIMARY KEY (parse_key, ord)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS calls (
    parse_key INTEGER NOT NULL,
    ord INTEGER NOT NULL,
    caller_ord INTEGER NOT NULL,
    callee TEXT NOT NULL,
    qualifier TEXT,
    form TEXT NOT NULL,
    line INTEGER NOT NULL,
    PRIMARY KEY (parse_key, ord)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS generations (
    commit_id TEXT PRIMARY KEY,
    tree TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    backends TEXT NOT NULL,
    files INTEGER NOT NULL,
    lexical_indexed INTEGER NOT NULL,
    syntax_files INTEGER NOT NULL,
    symbols INTEGER NOT NULL,
    calls INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS files (
    commit_id TEXT NOT NULL,
    path BLOB NOT NULL,
    blob_oid TEXT NOT NULL,
    language TEXT,
    classification TEXT NOT NULL,
    lexical_status TEXT NOT NULL,
    lexical_reason TEXT,
    syntax_status TEXT NOT NULL,
    syntax_reason TEXT,
    parse_key INTEGER,
    PRIMARY KEY (commit_id, path)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS files_by_blob ON files (blob_oid, commit_id);
CREATE INDEX IF NOT EXISTS symbols_by_name ON symbols (name);
"""


def database_path(root: Path) -> Path:
    return root / DATABASE_NAME


def _check_fts5(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("CREATE VIRTUAL TABLE temp.txray_probe USING fts5(x, tokenize = 'trigram')")
        connection.execute("DROP TABLE temp.txray_probe")
    except sqlite3.OperationalError as exc:
        raise TxrayError(
            f"this Python's SQLite {sqlite3.sqlite_version} lacks FTS5 with the trigram "
            f"tokenizer (SQLite 3.34 or newer built with FTS5 is required): {exc}"
        ) from exc


def _check_version(connection: sqlite3.Connection, path: Path) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != INDEX_SCHEMA_VERSION:
        raise IntegrityError(
            f"index {path} has schema version {version}, this txray reads version "
            f"{INDEX_SCHEMA_VERSION}; delete the file and run txray index again"
        )


def journal_mode(connection: sqlite3.Connection) -> str:
    """The journal mode of an open connection's database (``wal`` once converted)."""
    return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()


def _ensure_wal(connection: sqlite3.Connection) -> None:
    """Switch the database to WAL mode; a file another connection holds locked keeps its
    mode until the next writer opens it (readers of such a file wait at most
    :data:`READ_BUSY_TIMEOUT`)."""
    if journal_mode(connection) == JOURNAL_MODE:
        return
    try:
        connection.execute(f"PRAGMA journal_mode = {JOURNAL_MODE}")
    except sqlite3.OperationalError:
        pass  # locked by a reader right now; the next writer converts it


def open_for_write(root: Path) -> sqlite3.Connection:
    """Open (creating if needed) the index database in autocommit mode, in WAL mode."""
    root.mkdir(parents=True, exist_ok=True)
    path = database_path(root)
    connection = sqlite3.connect(path, timeout=WRITE_BUSY_TIMEOUT, isolation_level=None)
    try:
        _check_fts5(connection)
        connection.execute("PRAGMA foreign_keys = OFF")
        _ensure_wal(connection)
        if connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0:
            # Idempotent, so two processes creating the same new index cannot conflict.
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + SCHEMA
                + f"\nPRAGMA user_version = {INDEX_SCHEMA_VERSION};\nCOMMIT;\n"
            )
        _check_version(connection, path)
    except BaseException:
        connection.close()
        raise
    return connection


def open_for_read(root: Path) -> sqlite3.Connection:
    """Open the index database read-only; :class:`NotFound` if it does not exist yet.

    The connection never waits for a writer of a WAL-mode file and at most
    :data:`READ_BUSY_TIMEOUT` seconds otherwise (``sqlite3.OperationalError`` then).
    """
    path = database_path(root)
    if not path.is_file():
        raise NotFound(f"no code index at {path}; run: txray index <commit>")
    uri = "file:" + quote(str(path.resolve())) + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=READ_BUSY_TIMEOUT)
    try:
        _check_fts5(connection)
        _check_version(connection, path)
    except BaseException:
        connection.close()
        raise
    return connection
