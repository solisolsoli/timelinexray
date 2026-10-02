"""The research-import format: findings as delivered by the research reports (P1-P7).

The format is described by ``schemas/research-import.schema.json``: one object whose only
key ``findings`` holds finding objects with exactly the fields ``id``, ``title``,
``claim``, ``component``, ``evidence_class``, ``status``, ``sources`` (``code`` sources
with ``path``, ``lines``, ``commit`` and ``anchor``; ``web`` sources with ``url``,
``publisher``, ``published``, ``retrieved`` and ``quote``), ``creator_relevance``,
``creator_controllable``, ``implication``, ``volatility``, ``misuse_risk`` and ``note``.
:func:`validate` implements the same rules in code (the test suite compares the two).

A file may be JSON, YAML (the strict subset of :mod:`timelinexray.findings.yamlsub`), or
Markdown holding exactly one fenced code block that starts with ``findings:``.

Imported findings get the id ``<source-label>:<report id>``, keep the report's own status
(basis ``reported``) and enter workflow ``imported``; importing never promotes anything.
Code sources are resolved against the snapshot store where possible; the report's own
``commit`` and ``lines`` strings are kept, so :func:`from_records` reproduces the imported
findings exactly (round trip).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import InvalidInput
from ..snapshot.store import validate_repo_path
from . import yamlsub
from .model import (
    DEFAULT_SCOPE,
    EVIDENCE_CLASSES,
    RECORD_SCHEMA,
    STATUSES,
    canonical_json,
    date_precision,
    sha256_hex,
)

IMPORT_SCHEMA_ID = "timelinexray/research-import/v1"
MAX_FILE_BYTES = 16 * 1024 * 1024

FINDING_FIELDS = (
    "id", "title", "claim", "component", "evidence_class", "status", "sources",
    "creator_relevance", "creator_controllable", "implication", "volatility", "misuse_risk",
    "note",
)
ATTRIBUTE_FIELDS = (
    "creator_relevance", "creator_controllable", "implication", "volatility", "misuse_risk",
    "note",
)
CODE_SOURCE_FIELDS = ("kind", "path", "lines", "commit", "anchor")
WEB_SOURCE_FIELDS = ("kind", "url", "publisher", "published", "retrieved", "quote")
ENUMS: dict[str, tuple[str, ...]] = {
    "evidence_class": EVIDENCE_CLASSES,
    "status": STATUSES,
    "creator_relevance": ("none", "low", "medium", "high"),
    "creator_controllable": ("yes", "partly", "no"),
    "volatility": ("stable", "param", "experiment", "unknown"),
    "misuse_risk": ("low", "medium", "high"),
}
TEXT_FIELDS = {"id": 64, "title": 300, "claim": 4000, "component": 200, "implication": 4000,
               "note": 4000}
NON_EMPTY = frozenset({"id", "title", "claim", "component"})

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
LINES_PATTERN = r"^[1-9][0-9]*(-[1-9][0-9]*)?$"
COMMIT_PATTERN = r"^[0-9a-f]{7,40}$"
DATE_PATTERN = r"^[0-9]{4}(-[0-9]{2}(-[0-9]{2})?)?$"
URL_PATTERN = r"^https?://[^\s]+$"
LABEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$"
MAX_ANCHOR = 1000

_RE = {name: re.compile(value) for name, value in (
    ("id", ID_PATTERN), ("lines", LINES_PATTERN), ("commit", COMMIT_PATTERN),
    ("date", DATE_PATTERN), ("url", URL_PATTERN), ("label", LABEL_PATTERN))}
_FENCE = re.compile(r"^(```|~~~)[^\n]*\n(.*?)^\1[ \t]*$", re.MULTILINE | re.DOTALL)


def check_label(value: object) -> str:
    if not isinstance(value, str) or not _RE["label"].match(value):
        raise InvalidInput(f"invalid source label {value!r}: 1-32 characters from letters, "
                           "digits, '.', '_' and '-'")
    return value


@dataclass(frozen=True, slots=True)
class Document:
    name: str
    format: str
    sha256: str
    data: Any


def load(path: Path) -> Document:
    """Read a research-import file (JSON, YAML subset, or Markdown with one findings block)."""
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise InvalidInput(f"{path.name} is larger than {MAX_FILE_BYTES} bytes")
        raw = path.read_bytes()
    except FileNotFoundError:
        raise InvalidInput(f"research-import file {path.name} does not exist") from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InvalidInput(f"{path.name} is not UTF-8 text") from None
    suffix = path.suffix.lower()
    if suffix in (".md", ".markdown"):
        blocks = [m.group(2) for m in _FENCE.finditer(text)
                  if m.group(2).lstrip().startswith("findings:")]
        if len(blocks) != 1:
            raise InvalidInput(f"{path.name}: expected exactly one fenced code block starting "
                               f"with 'findings:', found {len(blocks)}")
        form, data = "markdown", yamlsub.load(blocks[0])
    elif suffix == ".json" or (suffix not in (".yaml", ".yml") and text.lstrip()[:1] == "{"):
        try:
            form, data = "json", json.loads(text)
        except json.JSONDecodeError as exc:
            raise InvalidInput(f"{path.name} is not valid JSON: {exc}") from None
    else:
        form, data = "yaml", yamlsub.load(text)
    return Document(path.name, form, hashlib.sha256(raw).hexdigest(), data)


def _text_problem(value: object, name: str, where: str) -> str | None:
    if not isinstance(value, str):
        return f"{where}.{name} must be a string"
    if name in NON_EMPTY and not value.strip():
        return f"{where}.{name} must not be empty"
    if name in TEXT_FIELDS and len(value) > TEXT_FIELDS[name]:
        return f"{where}.{name} is longer than {TEXT_FIELDS[name]} characters"
    return None


def _source_problems(source: object, where: str) -> list[str]:
    if not isinstance(source, dict):
        return [f"{where} must be an object"]
    kind = source.get("kind")
    if kind == "code":
        fields = CODE_SOURCE_FIELDS
    elif kind == "web":
        fields = WEB_SOURCE_FIELDS
    else:
        return [f"{where}.kind must be 'code' or 'web'"]
    problems = [f"{where}: missing {name}" for name in fields if name not in source]
    problems += [f"{where}: unknown field {name}" for name in source if name not in fields]
    if problems:
        return problems
    if kind == "code":
        path = source["path"]
        try:
            validate_repo_path(path)
        except InvalidInput as exc:
            problems.append(f"{where}.path: {exc}")
        if not isinstance(source["lines"], str) or not _RE["lines"].match(source["lines"]):
            problems.append(f"{where}.lines must be 'A-B' or 'A' with positive integers")
        if not isinstance(source["commit"], str) or not _RE["commit"].match(source["commit"]):
            problems.append(f"{where}.commit must be 7-40 lowercase hexadecimal digits")
        anchor = source["anchor"]
        if not isinstance(anchor, str) or not anchor or len(anchor) > MAX_ANCHOR:
            problems.append(f"{where}.anchor must be a non-empty string of at most "
                            f"{MAX_ANCHOR} characters")
    else:
        if not isinstance(source["url"], str) or not _RE["url"].match(source["url"]):
            problems.append(f"{where}.url must be an http(s) URL without spaces")
        for name in ("publisher", "quote"):
            if not isinstance(source[name], str):
                problems.append(f"{where}.{name} must be a string")
        published = source["published"]
        if published is not None and (not isinstance(published, str)
                                      or not _RE["date"].match(published)):
            problems.append(f"{where}.published must be YYYY, YYYY-MM, YYYY-MM-DD or null")
        retrieved = source["retrieved"]
        if not isinstance(retrieved, str) or not _RE["date"].match(retrieved):
            problems.append(f"{where}.retrieved must be YYYY, YYYY-MM or YYYY-MM-DD")
    return problems


def validate(data: object) -> list[str]:
    """Every way ``data`` departs from the research-import schema (empty when valid)."""
    if not isinstance(data, dict) or set(data) != {"findings"}:
        return ["the file must be an object whose only key is 'findings'"]
    findings = data["findings"]
    if not isinstance(findings, list) or not findings:
        return ["'findings' must be a non-empty list"]
    problems: list[str] = []
    seen: set[str] = set()
    for number, finding in enumerate(findings):
        where = f"findings[{number}]"
        if not isinstance(finding, dict):
            problems.append(f"{where} must be an object")
            continue
        if isinstance(finding.get("id"), str):
            where = f"{where} ({finding['id']})"
        problems += [f"{where}: missing {name}" for name in FINDING_FIELDS if name not in finding]
        problems += [f"{where}: unknown field {name}" for name in finding
                     if name not in FINDING_FIELDS]
        for name, value in finding.items():
            if name in ENUMS:
                if value not in ENUMS[name]:
                    problems.append(f"{where}.{name} must be one of {', '.join(ENUMS[name])}")
            elif name in TEXT_FIELDS:
                problem = _text_problem(value, name, where)
                if problem:
                    problems.append(problem)
        identifier = finding.get("id")
        if isinstance(identifier, str):
            if not _RE["id"].match(identifier):
                problems.append(f"{where}.id must match {ID_PATTERN}")
            elif identifier in seen:
                problems.append(f"{where}: duplicate id {identifier}")
            seen.add(identifier)
        sources = finding.get("sources")
        if "sources" in finding:
            if not isinstance(sources, list):
                problems.append(f"{where}.sources must be a list")
            else:
                for index, source in enumerate(sources):
                    problems += _source_problems(source, f"{where}.sources[{index}]")
    return problems


def to_record(
    finding: dict[str, Any], label: str, document: Document,
    resolve: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """The findings-memory record of one validated research finding."""
    sources = []
    for source in finding["sources"]:
        if source["kind"] == "code":
            sources.append(resolve(source))
        else:
            sources.append({
                "kind": "web",
                "url": source["url"],
                "publisher": source["publisher"],
                "published": source["published"],
                "published_precision": date_precision(source["published"]),
                "retrieved": source["retrieved"],
                "quote": source["quote"],
            })
    return {
        "schema": RECORD_SCHEMA,
        "finding_id": f"{label}:{finding['id']}",
        "title": finding["title"],
        "claim": finding["claim"],
        "component": finding["component"],
        "evidence_class": finding["evidence_class"],
        "status": finding["status"],
        "scope": DEFAULT_SCOPE[finding["evidence_class"]],
        "sources": sources,
        "dependencies": [],
        "negative": None,
        "limitations": [],
        "attributes": {name: finding[name] for name in ATTRIBUTE_FIELDS},
        "origin": {
            "kind": "import",
            "source_label": label,
            "report_id": finding["id"],
            "file": document.name,
            "file_sha256": document.sha256,
            "finding_sha256": sha256_hex(canonical_json(finding)),
        },
    }


def from_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Research-import data reconstructed from imported records (field order of the schema)."""
    findings = []
    for record in records:
        origin = record.get("origin") or {}
        sources = []
        for source in record["sources"]:
            if source["kind"] == "code":
                reported = source.get("reported") or {}
                sources.append({
                    "kind": "code",
                    "path": source["path"],
                    "lines": reported.get("lines", f"{source['start_line']}-{source['end_line']}"),
                    "commit": reported.get("commit", source["commit"]),
                    "anchor": source["anchor"],
                })
            else:
                sources.append({name: source[name] for name in WEB_SOURCE_FIELDS})
        finding: dict[str, Any] = {
            "id": origin.get("report_id", record["finding_id"]),
            "title": record["title"],
            "claim": record["claim"],
            "component": record["component"],
            "evidence_class": record["evidence_class"],
            "status": record["status"],
            "sources": sources,
        }
        attributes = record.get("attributes") or {}
        for name in ATTRIBUTE_FIELDS:
            finding[name] = attributes.get(name, "")
        findings.append(finding)
    return {"findings": findings}
