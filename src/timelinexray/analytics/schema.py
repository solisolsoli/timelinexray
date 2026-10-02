"""The documented export schema ``x-post-v1`` and its versioned header-alias dictionary.

``x-post-v1`` is the post-level ("content") CSV export of the X analytics web interface:
one row per post, cumulative counts per post, no account-level (daily) rows. Its columns
map to the canonical fields in :data:`FIELDS`.

Header aliases are data scoped to one export product, one language and one alias-table
version (P6 §2.2). There is no global translation table: a column name that means "views"
in one export is not assumed to mean impressions in another, and a generic "shares" column
is never read as reposts. Matching is exact after a documented normalisation (Unicode NFC,
trimmed and collapsed whitespace, case folding with Turkish dotted/dotless i rules for the
Turkish table); the original header text is always kept.

The Turkish strings are written with Unicode escapes so that repository text stays English;
they are alias data, not prose.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

SCHEMA_ID = "x-post-v1"
PRODUCT = "x-analytics-post-export"
GRAIN = "post"
ALIAS_TABLE_VERSION = 1
LANGUAGES = ("en", "tr")

ID, TIMESTAMP, TEXT, LINK, COUNT = "id", "timestamp", "text", "link", "count"


@dataclass(frozen=True)
class FieldSpec:
    """One canonical field of the schema."""

    name: str
    kind: str
    meaning: str


FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("post_id", ID, "post identifier, kept as the exact digit string"),
    FieldSpec("created_at", TIMESTAMP, "publication time as exported; the raw text is kept"),
    FieldSpec("text", TEXT, "post text; only its SHA-256 is stored"),
    FieldSpec("url", LINK, "post link; only checked against the post id, never stored"),
    FieldSpec("impressions", COUNT, "recorded impressions; not unique viewers"),
    FieldSpec("likes", COUNT, "likes"),
    FieldSpec("engagements", COUNT,
              "aggregate of several interactions; never added to its own components"),
    FieldSpec("bookmarks", COUNT, "bookmarks; no coefficient found in the value-model sum"),
    FieldSpec("shares", COUNT, "generic shares; does not identify DM or copy-link shares"),
    FieldSpec("new_follows", COUNT, "new follows; attribution to this post not established"),
    FieldSpec("replies", COUNT, "replies"),
    FieldSpec("reposts", COUNT, "reposts; whether quotes are separate is not established"),
    FieldSpec("profile_visits", COUNT, "profile visits"),
    FieldSpec("detail_expands", COUNT, "detail expands"),
    FieldSpec("url_clicks", COUNT, "URL clicks"),
    FieldSpec("hashtag_clicks", COUNT, "hashtag clicks"),
    FieldSpec("permalink_clicks", COUNT, "permalink clicks"),
)

FIELD_NAMES: tuple[str, ...] = tuple(field.name for field in FIELDS)
COUNT_FIELDS: tuple[str, ...] = tuple(field.name for field in FIELDS if field.kind == COUNT)


@dataclass(frozen=True)
class HeaderAliases:
    """Header text per canonical field for one (schema, language, alias-table version)."""

    schema: str
    product: str
    language: str
    version: int
    provenance: str
    status: str
    headers: Mapping[str, str]
    #: What establishes ``status``: the id of the recorded finding (``OFFICIAL`` from X's
    #: documentation, ``EMPIRICAL`` from a real export confirmed with ``txray metrics import
    #: --dump-header``) or the hash pin of a transcribed header row; ``None`` while the table
    #: is ``EXTERNAL_RECHECK``. A test refuses ``SUPPORTED`` without it: the status changes
    #: only through such a record, never by editing this file alone.
    confirmed_by: str | None = None

    def key(self) -> str:
        return f"{self.schema}/{self.language}/v{self.version}"


_EN = HeaderAliases(
    schema=SCHEMA_ID,
    product=PRODUCT,
    language="en",
    version=ALIAS_TABLE_VERSION,
    provenance=(
        "English column names of the X analytics post export, written from the documented "
        "column set and aligned one to one with the Turkish row; not checked against an "
        "actual English export"
    ),
    status="EXTERNAL_RECHECK",
    headers=MappingProxyType(
        {
            "post_id": "Post id",
            "created_at": "Date",
            "text": "Post text",
            "url": "Post Link",
            "impressions": "Impressions",
            "likes": "Likes",
            "engagements": "Engagements",
            "bookmarks": "Bookmarks",
            "shares": "Shares",
            "new_follows": "New follows",
            "replies": "Replies",
            "reposts": "Reposts",
            "profile_visits": "Profile visits",
            "detail_expands": "Detail Expands",
            "url_clicks": "URL Clicks",
            "hashtag_clicks": "Hashtag Clicks",
            "permalink_clicks": "Permalink Clicks",
        }
    ),
)

_TR = HeaderAliases(
    schema=SCHEMA_ID,
    product=PRODUCT,
    language="tr",
    version=ALIAS_TABLE_VERSION,
    provenance=(
        "Turkish header row exactly as exported by the X analytics web interface, "
        "transcribed as text on 2026-09-30 (no export file was opened or copied)"
    ),
    status="SUPPORTED",
    confirmed_by="sha256:56e239160bb5d75e0af670671df4fe18232b5c651d416e46bac7504aa9e3d48f",
    headers=MappingProxyType(
        {
            "post_id": "G\u00f6nderi kimli\u011fi",
            "created_at": "Tarih",
            "text": "Metni g\u00f6nderi olarak yay\u0131nla",
            "url": "G\u00f6nderi Ba\u011flant\u0131s\u0131",
            "impressions": "G\u00f6r\u00fcnt\u00fclenmeler",
            "likes": "Be\u011feni",
            "engagements": "Etkile\u015fimler",
            "bookmarks": "Yer \u0130\u015faretleri",
            "shares": "Payla\u015f\u0131mlar",
            "new_follows": "Yeni takip say\u0131s\u0131",
            "replies": "Yan\u0131tlar",
            "reposts": "Yeniden g\u00f6nderiler",
            "profile_visits": "Profil ziyaretleri",
            "detail_expands": "Ayr\u0131nt\u0131lar Geni\u015fletiliyor",
            "url_clicks": "URL T\u0131klanma Say\u0131s\u0131",
            "hashtag_clicks": "Hashtag T\u0131klanma Say\u0131s\u0131",
            "permalink_clicks": ("Kal\u0131c\u0131 Ba\u011flant\u0131 "
                                 "T\u0131klanma Say\u0131s\u0131"),
        }
    ),
)

#: (schema, language) -> alias table. The only entry point for header interpretation.
ALIASES: Mapping[tuple[str, str], HeaderAliases] = MappingProxyType(
    {(SCHEMA_ID, "en"): _EN, (SCHEMA_ID, "tr"): _TR}
)

# Turkish lower-casing: dotted capital I becomes i, plain capital I becomes dotless i.
_TURKISH_LOWER = str.maketrans({"I": "\u0131", "\u0130": "i"})


def normalize_header(text: str, language: str) -> str:
    """Matching key of a header cell: NFC, whitespace collapsed, case folded per language."""
    value = unicodedata.normalize("NFC", text).replace("\ufeff", "")
    value = " ".join(value.split())
    if language == "tr":
        value = value.translate(_TURKISH_LOWER)
    return unicodedata.normalize("NFC", value.casefold())


def aliases_for(schema: str, language: str) -> HeaderAliases:
    try:
        return ALIASES[(schema, language)]
    except KeyError:
        known = ", ".join(f"{s} ({lang})" for s, lang in sorted(ALIASES))
        raise KeyError(f"no header aliases for schema {schema!r} in language {language!r}; "
                       f"known: {known}") from None


@dataclass(frozen=True)
class Column:
    """One column of an export header row and the canonical field it maps to, if any."""

    index: int
    header: str
    field: str | None

    def to_dict(self) -> dict[str, object]:
        return {"index": self.index, "header": self.header, "field": self.field}


@dataclass(frozen=True)
class HeaderResolution:
    aliases: HeaderAliases
    columns: tuple[Column, ...]
    duplicates: tuple[tuple[str, tuple[str, ...]], ...]

    @property
    def field_index(self) -> dict[str, int]:
        return {c.field: c.index for c in self.columns if c.field is not None}

    @property
    def missing_fields(self) -> tuple[str, ...]:
        present = self.field_index
        return tuple(name for name in FIELD_NAMES if name not in present)

    @property
    def unmapped_headers(self) -> tuple[str, ...]:
        return tuple(c.header for c in self.columns if c.field is None)


def resolve_headers(headers: Sequence[str], schema: str, language: str) -> HeaderResolution:
    """Map original header cells to canonical fields with one alias table only."""
    aliases = aliases_for(schema, language)
    lookup = {normalize_header(text, language): field for field, text in aliases.headers.items()}
    columns = []
    seen: dict[str, list[str]] = {}
    for index, header in enumerate(headers):
        field = lookup.get(normalize_header(header, language))
        if field is not None:
            seen.setdefault(field, []).append(header)
        columns.append(Column(index, header, field))
    duplicates = tuple((f, tuple(h)) for f, h in sorted(seen.items()) if len(h) > 1)
    return HeaderResolution(aliases, tuple(columns), duplicates)


def matching_languages(headers: Sequence[str], schema: str) -> dict[str, int]:
    """How many headers each language's table would map (used only for error hints)."""
    counts = {}
    for known_schema, language in sorted(ALIASES):
        if known_schema == schema:
            columns = resolve_headers(headers, schema, language).columns
            counts[language] = sum(column.field is not None for column in columns)
    return counts
