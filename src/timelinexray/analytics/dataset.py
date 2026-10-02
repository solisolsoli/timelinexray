"""The import contract (P6 §2.2): an export CSV becomes snapshots in a private dataset.

A dataset is a directory holding two files, both readable only by their owner:

``dataset.json``
    format, schema, the list of imports (source file name and SHA-256, every declared
    setting, the original header row, the column mapping, missing fields and unmapped
    headers) and the SHA-256 of ``snapshots.jsonl``.
``snapshots.jsonl``
    one JSON object per snapshot: a post's counts as exported at one capture time and in
    one metric scope.

Rules, one per row of the P6 §2.2 table:

* headers are resolved with one versioned alias table per (schema, language), and the
  original header row is kept;
* post ids stay exact digit strings; values in scientific notation are rejected;
* publication times keep their raw text and declared time zone; UTC is derived only when
  unambiguous, and daylight-saving gaps and folds are recorded, not resolved;
* counts are read with the declared number locale;
* a missing column (``missing-column``) and an empty cell (``blank``) stay distinct from an
  observed zero;
* the schema is post grain; daily account rows are not accepted and nothing is ever
  distributed across posts;
* every snapshot records its capture time, count kind (cumulative, window or unknown) and
  scope; snapshots are identified by (post, capture time, scope) and are never summed;
  importing the same snapshot twice is a no-op and a conflicting one is an error.

Post text and post links are not stored: only a SHA-256 of the text and whether the link
points at the row's post id.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..errors import IntegrityError, TxrayError
from . import schema as sch
from .parsing import (
    MISSING_COLUMN as TIME_MISSING_COLUMN,
    PARSED,
    CellError,
    ParsedTime,
    format_utc,
    load_zone,
    parse_count,
    parse_created_at,
    parse_post_id,
    parse_utc,
)

DATASET_FORMAT = "timelinexray/analytics-dataset/v1"
DATASET_FILE = "dataset.json"
SNAPSHOTS_FILE = "snapshots.jsonl"
MAX_EXPORT_BYTES = 64 * 1024 * 1024
MAX_REPORTED_PROBLEMS = 20

SCOPES = ("organic", "promoted", "combined", "unknown")
COUNT_KINDS = ("cumulative", "window", "unknown")

OBSERVED = "observed"
BLANK = "blank"
MISSING_COLUMN = "missing-column"


class ExportRejected(TxrayError):
    """The export file cannot be imported under the declared schema and settings."""

    code = "export_rejected"


class DatasetError(TxrayError):
    """A dataset directory is missing, of another format, or would become inconsistent."""

    code = "dataset_error"


@dataclass(frozen=True)
class ImportSettings:
    """Everything the user declares about an export; recorded with every import."""

    schema: str
    language: str
    captured_at: datetime
    number_locale: str
    number_locale_source: str
    source_timezone: str
    date_format: str | None
    scope: str
    counts: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "header_language": self.language,
            "captured_at": format_utc(self.captured_at),
            "number_locale": self.number_locale,
            "number_locale_source": self.number_locale_source,
            "source_timezone": self.source_timezone,
            "date_format": self.date_format,
            "scope": self.scope,
            "counts": self.counts,
        }


@dataclass(frozen=True)
class Cell:
    """One count: observed with a value, blank in the export, or its column is missing."""

    status: str
    value: int | None = None

    @property
    def observed(self) -> bool:
        return self.status == OBSERVED

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "value": self.value}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Cell":
        return cls(data["status"], data.get("value"))


@dataclass(frozen=True)
class Snapshot:
    """A post's exported counts at one capture time and in one metric scope."""

    post_id: str
    captured_at: datetime
    scope: str
    counts_kind: str
    import_id: str
    created_at: ParsedTime
    cells: Mapping[str, Cell]
    text_sha256: str | None
    link: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.post_id, format_utc(self.captured_at), self.scope)

    def cell(self, name: str) -> Cell:
        return self.cells[name]

    def to_dict(self) -> dict[str, Any]:
        return {
            "post_id": self.post_id,
            "captured_at": format_utc(self.captured_at),
            "scope": self.scope,
            "counts_kind": self.counts_kind,
            "import_id": self.import_id,
            "created_at": self.created_at.to_dict(),
            "counts": {name: self.cells[name].to_dict() for name in sch.COUNT_FIELDS},
            "text_sha256": self.text_sha256,
            "link": self.link,
        }

    def comparable(self) -> dict[str, Any]:
        """Content that identifies the observation (everything but the import it came from)."""
        data = self.to_dict()
        del data["import_id"]
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Snapshot":
        return cls(
            post_id=data["post_id"],
            captured_at=parse_utc(data["captured_at"]),
            scope=data["scope"],
            counts_kind=data["counts_kind"],
            import_id=data["import_id"],
            created_at=ParsedTime.from_dict(data["created_at"]),
            cells={name: Cell.from_dict(data["counts"][name]) for name in sch.COUNT_FIELDS},
            text_sha256=data.get("text_sha256"),
            link=data["link"],
        )


def post_sort_key(post_id: str) -> tuple[int, str]:
    """Numeric order for digit strings without converting them to numbers."""
    return (len(post_id), post_id)


def _snapshot_order(snapshot: Snapshot) -> tuple[Any, ...]:
    return (*post_sort_key(snapshot.post_id), snapshot.key[1], snapshot.scope)


@dataclass(frozen=True)
class Dataset:
    path: Path
    meta: Mapping[str, Any]
    snapshots: tuple[Snapshot, ...]

    @property
    def imports(self) -> list[Mapping[str, Any]]:
        return list(self.meta["imports"])


@dataclass
class _Problems:
    items: list[str] = field(default_factory=list)

    def add(self, text: str) -> None:
        self.items.append(text)

    def raise_if_any(self, file_name: str) -> None:
        if not self.items:
            return
        shown = self.items[:MAX_REPORTED_PROBLEMS]
        more = len(self.items) - len(shown)
        tail = f"\n  ... and {more} more" if more else ""
        raise ExportRejected(
            f"{file_name}: {len(self.items)} problem(s); nothing was imported:\n  "
            + "\n  ".join(shown) + tail
        )


_STATUS_LINK = re.compile(r"/status(?:es)?/([0-9]+)(?:[/?#]|$)")


def _link_check(raw: str | None, post_id: str) -> str:
    if raw is None:
        return MISSING_COLUMN
    text = raw.strip()
    if not text:
        return BLANK
    match = _STATUS_LINK.search(text)
    return "consistent" if match and match.group(1) == post_id else "inconsistent"


def import_id_for(source_sha256: str, settings: ImportSettings) -> str:
    material = json.dumps({"source_sha256": source_sha256, "settings": settings.to_dict()},
                          sort_keys=True).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def read_header_row(csv_path: str | os.PathLike[str]) -> tuple[str, list[str]]:
    """``(file name, header row)`` of an export, read without touching a data row.

    The file is opened for reading only and read line by line as bytes; each line is
    decoded only when ``csv.reader`` asks for it, and the reader stops after the first
    record (the header line, plus the continuation lines of a quoted header cell). No byte
    of a data row is decoded, parsed or returned: an invalid byte or an unterminated quote
    in a data row is not noticed (``txray metrics import --dump-header``).
    """
    source = Path(csv_path)
    consumed = 0

    def lines(handle: Any) -> Iterable[str]:
        nonlocal consumed
        while True:
            raw = handle.readline()
            if not raw:
                return
            start = 3 if consumed == 0 and raw.startswith(b"\xef\xbb\xbf") else 0
            try:
                text = raw[start:].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ExportRejected(f"{source.name}: not UTF-8 text (invalid byte at offset "
                                     f"{consumed + start + exc.start}); save the export as "
                                     "UTF-8 CSV") from exc
            consumed += len(raw)
            yield text

    try:
        size = source.stat().st_size
        if size > MAX_EXPORT_BYTES:
            raise ExportRejected(f"{source.name}: {size} bytes exceeds the {MAX_EXPORT_BYTES} "
                                 "byte limit for one export")
        with open(source, "rb") as handle:
            try:
                header = next(csv.reader(lines(handle), strict=True))
            except StopIteration:
                header = []
            except csv.Error as exc:
                raise ExportRejected(f"{source.name}: malformed CSV: {exc}") from exc
    except OSError as exc:
        raise ExportRejected(f"cannot read {csv_path}: {exc.strerror or exc}") from exc
    if not header or not any(cell.strip() for cell in header):
        raise ExportRejected(f"{source.name}: the first line must be the header row")
    return source.name, header


def read_export(
    data: bytes, file_name: str, settings: ImportSettings
) -> tuple[dict[str, Any], list[Snapshot]]:
    """Parse export bytes into an import record and its snapshots, or raise ExportRejected."""
    source_sha256 = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExportRejected(
            f"{file_name}: not UTF-8 text (invalid byte at offset {exc.start}); save the export "
            "as UTF-8 CSV"
        ) from exc
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        raise ExportRejected(f"{file_name}: malformed CSV: {exc}") from exc
    if not rows or not any(cell.strip() for cell in rows[0]):
        raise ExportRejected(f"{file_name}: the first line must be the header row")
    header = rows[0]
    resolution = sch.resolve_headers(header, settings.schema, settings.language)
    if resolution.duplicates:
        detail = "; ".join(f"{f}: {', '.join(repr(h) for h in hs)}"
                           for f, hs in resolution.duplicates)
        raise ExportRejected(f"{file_name}: several columns map to one field ({detail})")
    index = resolution.field_index
    if "post_id" not in index:
        raise ExportRejected(_no_post_id_message(file_name, header, settings))

    zone = load_zone(settings.source_timezone)
    import_id = import_id_for(source_sha256, settings)
    problems = _Problems()
    snapshots: dict[tuple[str, str, str], Snapshot] = {}
    blank_cells: Counter[str] = Counter()
    time_issues: Counter[str] = Counter()
    links: Counter[str] = Counter()
    duplicates = 0
    data_rows = 0
    for number, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        data_rows += 1
        where = f"record {number}"
        if len(row) != len(header):
            problems.add(f"{where}: {len(row)} fields, but the header has {len(header)}")
            continue
        try:
            post_id = parse_post_id(row[index["post_id"]])
        except CellError as exc:
            problems.add(f"{where}: {exc}")
            continue
        if "created_at" in index:
            created = parse_created_at(row[index["created_at"]], zone, settings.date_format)
        else:
            created = ParsedTime(None, TIME_MISSING_COLUMN)
        if created.status != PARSED:
            time_issues[created.status] += 1
        if created.utc is not None and created.utc > settings.captured_at:
            problems.add(
                f"{where}: post {post_id} was published at {format_utc(created.utc)}, after the "
                f"declared capture time {format_utc(settings.captured_at)}; check --captured-at "
                "and --source-timezone"
            )
            continue
        cells: dict[str, Cell] = {}
        for name in sch.COUNT_FIELDS:
            if name not in index:
                cells[name] = Cell(MISSING_COLUMN)
                continue
            raw = row[index[name]]
            if not raw.strip():
                cells[name] = Cell(BLANK)
                blank_cells[name] += 1
                continue
            try:
                cells[name] = Cell(OBSERVED, parse_count(raw, settings.number_locale))
            except CellError as exc:
                problems.add(f"{where}, column {header[index[name]]!r}: {exc}")
        if len(cells) != len(sch.COUNT_FIELDS):
            continue
        text_value = row[index["text"]] if "text" in index else ""
        link = _link_check(row[index["url"]] if "url" in index else None, post_id)
        links[link] += 1
        snapshot = Snapshot(
            post_id=post_id,
            captured_at=settings.captured_at,
            scope=settings.scope,
            counts_kind=settings.counts,
            import_id=import_id,
            created_at=created,
            cells=cells,
            text_sha256=(hashlib.sha256(text_value.encode("utf-8")).hexdigest()
                         if text_value.strip() else None),
            link=link,
        )
        earlier = snapshots.get(snapshot.key)
        if earlier is None:
            snapshots[snapshot.key] = snapshot
        elif earlier.comparable() == snapshot.comparable():
            duplicates += 1
        else:
            problems.add(f"{where}: post {post_id} appears again with different values")
    problems.raise_if_any(file_name)
    if not snapshots:
        raise ExportRejected(f"{file_name}: the export has a header row but no post rows")
    record = {
        "import_id": import_id,
        "source": {"file_name": file_name, "sha256": source_sha256, "bytes": len(data)},
        "settings": settings.to_dict(),
        "header": {
            "alias_table": resolution.aliases.key(),
            "alias_status": resolution.aliases.status,
            "original": list(header),
            "columns": [column.to_dict() for column in resolution.columns],
            "missing_fields": list(resolution.missing_fields),
            "unmapped_headers": list(resolution.unmapped_headers),
        },
        "rows": data_rows,
        "snapshots": len(snapshots),
        "identical_duplicate_rows": duplicates,
        "blank_cells": dict(sorted(blank_cells.items())),
        "created_at_issues": dict(sorted(time_issues.items())),
        "links": dict(sorted(links.items())),
    }
    return record, sorted(snapshots.values(), key=_snapshot_order)


def _no_post_id_message(file_name: str, header: list[str], settings: ImportSettings) -> str:
    wanted = sch.aliases_for(settings.schema, settings.language).headers["post_id"]
    message = (
        f"{file_name}: no post id column ({wanted!r}) in the header row for schema "
        f"{settings.schema} ({settings.language}). {settings.schema} is a post-grain schema; "
        "daily account-level exports are not supported and their rows are never distributed "
        "across posts."
    )
    others = {lang: n for lang, n in sch.matching_languages(header, settings.schema).items()
              if lang != settings.language and n}
    if others:
        best = max(others, key=lambda lang: others[lang])
        message += f" The header matches the {best!r} alias table better: try --lang {best}."
    return message


# -- dataset directories ------------------------------------------------------------------


def load_dataset(path: str | os.PathLike[str]) -> Dataset:
    """Read a dataset directory (or its ``dataset.json``) and verify its integrity."""
    root = Path(path)
    if root.name == DATASET_FILE and root.is_file():
        root = root.parent
    meta_path = root / DATASET_FILE
    if not meta_path.is_file():
        raise DatasetError(
            f"{path} is not an analytics dataset (no {DATASET_FILE}); create one with "
            "'txray metrics import'"
        )
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetError(f"{meta_path}: unreadable: {exc}") from exc
    if not isinstance(meta, dict) or meta.get("format") != DATASET_FORMAT:
        raise DatasetError(f"{meta_path}: not a {DATASET_FORMAT} dataset")
    raw = (root / SNAPSHOTS_FILE).read_bytes() if (root / SNAPSHOTS_FILE).is_file() else b""
    digest = hashlib.sha256(raw).hexdigest()
    if digest != meta.get("snapshots_sha256"):
        raise IntegrityError(
            f"{root / SNAPSHOTS_FILE}: SHA-256 {digest} does not match the dataset record "
            f"{meta.get('snapshots_sha256')}; the file was changed outside txray"
        )
    snapshots = tuple(Snapshot.from_dict(json.loads(line))
                      for line in raw.decode("utf-8").splitlines() if line)
    known = {item["import_id"] for item in meta["imports"]}
    unknown = sorted({s.import_id for s in snapshots} - known)
    if unknown:
        raise IntegrityError(f"{root}: snapshots reference unknown imports {unknown}")
    return Dataset(root, meta, snapshots)


def _write_private(path: Path, data: bytes) -> None:
    """Write a dataset file readable by its owner only (0600; the directory is 0700).

    Analytics data is private user data (docs/analytics.md). This deliberately does not use
    ``fsutil.atomic_write``, whose files get the ordinary umask-based mode: privacy here is
    explicit, whatever the umask, and ``tests/test_analytics_import.py`` pins it."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)  # also when a stale temporary file had another mode
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _encode(snapshots: Iterable[Snapshot]) -> bytes:
    lines = [json.dumps(s.to_dict(), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
             for s in snapshots]
    return ("\n".join(lines) + "\n").encode("ascii") if lines else b""


def import_file(
    csv_path: str | os.PathLike[str], out_dir: str | os.PathLike[str], settings: ImportSettings
) -> dict[str, Any]:
    """Import one export file into ``out_dir``; returns the import record plus totals."""
    source = Path(csv_path)
    try:
        size = source.stat().st_size
        if size > MAX_EXPORT_BYTES:
            raise ExportRejected(f"{source.name}: {size} bytes exceeds the {MAX_EXPORT_BYTES} "
                                 "byte limit for one export")
        data = source.read_bytes()
    except OSError as exc:
        raise ExportRejected(f"cannot read {csv_path}: {exc.strerror or exc}") from exc
    record, new_snapshots = read_export(data, source.name, settings)

    out = Path(out_dir)
    existing: Dataset | None = None
    if out.exists():
        if not out.is_dir():
            raise DatasetError(f"{out} exists and is not a directory")
        if (out / DATASET_FILE).exists():
            existing = load_dataset(out)
        elif any(out.iterdir()):
            raise DatasetError(f"{out} is not empty and is not an analytics dataset; "
                               "choose an empty or new directory")
    elif not out.parent.is_dir():
        raise DatasetError(f"parent directory of {out} does not exist")

    imports = list(existing.imports) if existing else []
    by_key = {s.key: s for s in existing.snapshots} if existing else {}
    if existing is not None:
        known = existing.meta["schema"]["id"]
        if known != settings.schema:
            raise DatasetError(f"{out} holds schema {known}, not {settings.schema}")
        if any(item["import_id"] == record["import_id"] for item in imports):
            return _summary(existing.meta, record, 0, len(new_snapshots), already_imported=True)

    added = present = 0
    conflicts = []
    for snapshot in new_snapshots:
        earlier = by_key.get(snapshot.key)
        if earlier is None:
            by_key[snapshot.key] = snapshot
            added += 1
        elif earlier.comparable() == snapshot.comparable():
            present += 1
        else:
            conflicts.append(snapshot.key)
    if conflicts:
        shown = ", ".join(f"post {p} at {c} ({s})" for p, c, s in conflicts[:5])
        raise DatasetError(
            f"{len(conflicts)} snapshot(s) already exist in {out} with different values ({shown}); "
            "cumulative snapshots are never summed or overwritten: import into a new dataset "
            "or check --captured-at"
        )
    record = {**record, "snapshots_added": added, "snapshots_already_present": present}
    imports.append(record)
    snapshots = sorted(by_key.values(), key=_snapshot_order)
    payload = _encode(snapshots)
    meta = {
        "format": DATASET_FORMAT,
        "note": ("Private analytics data. Keep it out of version control and out of any "
                 "directory that TimelineXray or an MCP client serves."),
        "schema": {
            "id": sch.SCHEMA_ID,
            "product": sch.PRODUCT,
            "grain": sch.GRAIN,
            "canonical_fields": list(sch.FIELD_NAMES),
        },
        "imports": imports,
        "snapshot_count": len(snapshots),
        "post_count": len({s.post_id for s in snapshots}),
        "snapshots_sha256": hashlib.sha256(payload).hexdigest(),
    }
    if not out.exists():
        os.mkdir(out, 0o700)
    _write_private(out / SNAPSHOTS_FILE, payload)
    _write_private(out / DATASET_FILE,
                   (json.dumps(meta, indent=1, sort_keys=True, ensure_ascii=True) + "\n")
                   .encode("ascii"))
    return _summary(meta, record, added, present, already_imported=False)


def _summary(meta: Mapping[str, Any], record: Mapping[str, Any], added: int, present: int,
             *, already_imported: bool) -> dict[str, Any]:
    return {
        "import": dict(record),
        "already_imported": already_imported,
        "snapshots_added": added,
        "snapshots_already_present": present,
        "dataset": {
            "snapshots": meta["snapshot_count"],
            "posts": meta["post_count"],
            "imports": len(meta["imports"]),
        },
    }
