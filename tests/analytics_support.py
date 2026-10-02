"""Synthetic analytics exports for the analytics tests. Every value here is invented.

The Turkish header row is written with named escapes, independently of the alias table in
``timelinexray.analytics.schema``, so that the tests compare two separate transcriptions of
the transcribed header row (and a SHA-256 of that row pins both).
"""

from __future__ import annotations

import csv
import datetime as dt
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from timelinexray.analytics.dataset import ImportSettings
from tests.support import REPO_ROOT

CANONICAL = (
    "post_id", "created_at", "text", "url", "impressions", "likes", "engagements",
    "bookmarks", "shares", "new_follows", "replies", "reposts", "profile_visits",
    "detail_expands", "url_clicks", "hashtag_clicks", "permalink_clicks",
)

EN_HEADERS = (
    "Post id", "Date", "Post text", "Post Link", "Impressions", "Likes", "Engagements",
    "Bookmarks", "Shares", "New follows", "Replies", "Reposts", "Profile visits",
    "Detail Expands", "URL Clicks", "Hashtag Clicks", "Permalink Clicks",
)

_o = "\N{LATIN SMALL LETTER O WITH DIAERESIS}"
_u = "\N{LATIN SMALL LETTER U WITH DIAERESIS}"
_g = "\N{LATIN SMALL LETTER G WITH BREVE}"
_i = "\N{LATIN SMALL LETTER DOTLESS I}"
_s = "\N{LATIN SMALL LETTER S WITH CEDILLA}"
_I = "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}"

#: The Turkish post-export header row as exported by the X analytics interface.
TR_HEADER_ROW = (
    f"G{_o}nderi kimli{_g}i,Tarih,Metni g{_o}nderi olarak yay{_i}nla,"
    f"G{_o}nderi Ba{_g}lant{_i}s{_i},G{_o}r{_u}nt{_u}lenmeler,Be{_g}eni,Etkile{_s}imler,"
    f"Yer {_I}{_s}aretleri,Payla{_s}{_i}mlar,Yeni takip say{_i}s{_i},Yan{_i}tlar,"
    f"Yeniden g{_o}nderiler,Profil ziyaretleri,Ayr{_i}nt{_i}lar Geni{_s}letiliyor,"
    f"URL T{_i}klanma Say{_i}s{_i},Hashtag T{_i}klanma Say{_i}s{_i},"
    f"Kal{_i}c{_i} Ba{_g}lant{_i} T{_i}klanma Say{_i}s{_i}"
)
#: SHA-256 of the UTF-8 bytes of the header row exactly as transcribed (336 bytes).
TR_HEADER_ROW_SHA256 = "56e239160bb5d75e0af670671df4fe18232b5c651d416e46bac7504aa9e3d48f"
TR_HEADERS = tuple(TR_HEADER_ROW.split(","))

HEADERS = {"en": EN_HEADERS, "tr": TR_HEADERS}

CAPTURED = dt.datetime(2026, 9, 30, 12, 0, tzinfo=dt.timezone.utc)


def post(post_id: str, created: str = "2026-09-27T12:00:00Z", **counts: object) -> dict[str, str]:
    """One synthetic export row keyed by canonical field; unspecified counts are blank."""
    row = {"post_id": post_id, "created_at": created, "text": f"synthetic post {post_id}",
           "url": f"https://example.invalid/someone/status/{post_id}"}
    row.update({name: str(value) for name, value in counts.items()})
    return row


def write_export(
    path: Path,
    rows: Sequence[Mapping[str, str]],
    *,
    lang: str = "en",
    fields: Sequence[str] = CANONICAL,
    headers: Sequence[str] | None = None,
) -> Path:
    """Write a synthetic export CSV (UTF-8) with the chosen header language."""
    names = HEADERS[lang]
    header = list(headers) if headers is not None else [names[CANONICAL.index(f)] for f in fields]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow([row.get(f, "") for f in fields])
    return path


def settings(**overrides: Any) -> ImportSettings:
    values: dict[str, Any] = {
        "schema": "x-post-v1",
        "language": "en",
        "captured_at": CAPTURED,
        "number_locale": "en",
        "number_locale_source": "header-language",
        "source_timezone": "UTC",
        "date_format": None,
        "scope": "combined",
        "counts": "cumulative",
    }
    values.update(overrides)
    return ImportSettings(**values)


def run_txray(argv: Sequence[str], env: Mapping[str, str] | None = None,
              ) -> subprocess.CompletedProcess[bytes]:
    """Run ``python -m timelinexray`` in a fresh process (analytics commands go offline)."""
    full_env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src"), **(env or {})}
    return subprocess.run([sys.executable, "-m", "timelinexray", *argv], capture_output=True,
                          env=full_env, timeout=120)


def run_python(code: str, *args: str, env: Mapping[str, str] | None = None,
               ) -> subprocess.CompletedProcess[bytes]:
    full_env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src"), **(env or {})}
    return subprocess.run([sys.executable, "-c", code, *args], capture_output=True,
                          env=full_env, timeout=120)
