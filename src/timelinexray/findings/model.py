"""Vocabulary and record validation of the findings memory.

Three fields are kept strictly apart (TimelineXray hard boundary 3):

* ``status`` - the evidence status of the claim: ``SUPPORTED``, ``PARTIAL``, ``NOT_FOUND``,
  ``CONTRADICTED`` or ``EXTERNAL_RECHECK``, with ``status_basis`` saying who set it:
  ``proposed`` (the author on ``create``), ``reported`` (a research report on ``import``) or
  ``reviewed`` (a ``review`` event by a named reviewer; the only basis that confirms it);
* ``freshness`` - whether the finding's evidence was checked against a commit: ``CURRENT``,
  ``STALE``, ``UNVERIFIABLE`` or ``NOT_CHECKED``;
* ``workflow`` - where the finding is in its life: ``draft``, ``imported``, ``reviewed``,
  ``superseded`` or ``retracted``.

A finding record is immutable once written into a ``create``, ``import`` or ``supersede``
event. Corrections are new findings that supersede the old one.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from ..errors import InvalidInput

RECORD_SCHEMA = "timelinexray/finding/v1"

STATUSES = ("SUPPORTED", "PARTIAL", "NOT_FOUND", "CONTRADICTED", "EXTERNAL_RECHECK")
EVIDENCE_CLASSES = (
    "CODE", "PARAM_DEFAULT", "REPO_DOC", "OFFICIAL", "THIRD_PARTY", "EMPIRICAL", "INFERENCE",
)
CODE_CLASSES = ("CODE", "PARAM_DEFAULT", "REPO_DOC")

STATUS_BASES = ("proposed", "reported", "reviewed")

DRAFT = "draft"
IMPORTED = "imported"
REVIEWED = "reviewed"
SUPERSEDED = "superseded"
RETRACTED = "retracted"
WORKFLOWS = (DRAFT, IMPORTED, REVIEWED, SUPERSEDED, RETRACTED)
ACTIVE_WORKFLOWS = (DRAFT, IMPORTED, REVIEWED)

#: Bounded scopes of a claim; there is deliberately no "production" scope.
SCOPES = ("public_code", "public_default", "historical", "external")
DEFAULT_SCOPE = {
    "CODE": "public_code",
    "PARAM_DEFAULT": "public_default",
    "REPO_DOC": "public_code",
    "OFFICIAL": "external",
    "THIRD_PARTY": "external",
    "EMPIRICAL": "external",
    "INFERENCE": "public_code",
}

EVENT_TYPES = ("create", "verify", "review", "supersede", "retract", "import")

AUTHOR_ROLES = ("author", "importer", "maintainer")
REVIEWER_ROLES = ("reviewer", "maintainer")
VERIFIER_ROLE = "verifier"
ROLES = ("author", "importer", "reviewer", "maintainer", VERIFIER_ROLE)

MAX_ID = 128
MAX_TITLE = 300
MAX_TEXT = 4000
MAX_NAME = 64

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}")
_URL = re.compile(r"https?://[^\s]+")
_DATE = re.compile(r"[0-9]{4}(-[0-9]{2}(-[0-9]{2})?)?")


def canonical_json(value: Any) -> bytes:
    """The canonical bytes every hash in the findings memory is computed over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_finding_id(value: object) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise InvalidInput(
            f"invalid finding id {value!r}: use 1-{MAX_ID} characters from letters, digits, "
            "'.', '_', ':' and '-', starting with a letter or digit"
        )
    return value


def check_name(value: object, what: str = "actor name") -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise InvalidInput(
            f"invalid {what} {value!r}: use 1-{MAX_NAME} characters from letters, digits, "
            "'.', '_', '@', '+' and '-' (a role or handle, not a personal name)"
        )
    return value


def check_text(value: object, what: str, *, maximum: int = MAX_TEXT, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise InvalidInput(f"{what} must be a non-empty string")
    if len(value) > maximum:
        raise InvalidInput(f"{what} is longer than {maximum} characters")
    if any(ord(char) < 0x20 and char not in "\n\t" for char in value):
        raise InvalidInput(f"{what} must not contain control characters")
    return value


def check_choice(value: object, choices: tuple[str, ...], what: str) -> str:
    if value not in choices:
        raise InvalidInput(f"{what} must be one of {', '.join(choices)}; got {value!r}")
    return str(value)


def date_precision(value: object) -> str:
    """Precision of an external source date: ``day``, ``month``, ``year`` or ``unknown``."""
    if value is None:
        return "unknown"
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        raise InvalidInput(f"date {value!r} must be YYYY, YYYY-MM or YYYY-MM-DD, or null")
    return {4: "year", 7: "month", 10: "day"}[len(value)]


@dataclass(frozen=True, slots=True)
class Actor:
    """A declared actor. Names are declared, not authenticated; see docs/findings-memory.md."""

    name: str
    role: str

    def __post_init__(self) -> None:
        check_name(self.name)
        check_choice(self.role, ROLES, "actor role")

    @property
    def key(self) -> str:
        """Comparison key for the no-self-approval rule."""
        return self.name.strip().casefold()

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "role": self.role}

    @classmethod
    def from_dict(cls, data: object) -> "Actor":
        if not isinstance(data, dict):
            raise InvalidInput("actor must be an object with name and role")
        return cls(data.get("name"), data.get("role"))  # type: ignore[arg-type]


def web_source(data: object) -> dict[str, Any]:
    """Validate and normalize an external (web) source record; it is stored, never fetched."""
    if not isinstance(data, dict):
        raise InvalidInput("a web source must be an object")
    url = data.get("url")
    if not isinstance(url, str) or not _URL.fullmatch(url) or len(url) > 2000:
        raise InvalidInput(f"web source url {url!r} must be an http(s) URL without spaces")
    retrieved = data.get("retrieved")
    if retrieved is not None:
        date_precision(retrieved)
    return {
        "kind": "web",
        "url": url,
        "publisher": check_text(data.get("publisher", ""), "publisher", maximum=300, empty=True),
        "published": data.get("published"),
        "published_precision": date_precision(data.get("published")),
        "retrieved": retrieved,
        "quote": check_text(data.get("quote", ""), "quote", maximum=MAX_TEXT, empty=True),
    }


def code_citations(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [source for source in record.get("sources", []) if source.get("kind") == "code"]
