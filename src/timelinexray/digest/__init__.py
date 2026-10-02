"""Reviewed-change digests for a commit range (Milestone 5b).

* :class:`DigestBuilder` builds the digest document (JSON-ready) from diffs of pinned
  commits: summary counts by class, parameter-default table with timelines and
  reversions, registration changes, affected findings (through a read-only provider),
  unknown changes, intermediate history and health.
* :func:`render_markdown` renders it; :func:`digest_json` serialises it canonically.
* :mod:`timelinexray.digest.update` runs a scheduled update: guarded fetch, pin of the new
  head, digest against the previous pin, exceptional events.

A digest is generated from diffs only; commit messages are never read. Nothing is posted:
digests are local files.
"""

from __future__ import annotations

import json
from typing import Any

from .build import DIGEST_SCHEMA, STATEMENTS, DigestBuilder
from .findings import (
    AffectedFinding,
    AffectedFindingsProvider,
    ChangedRegion,
    NullFindingsProvider,
    default_findings_provider,
)
from .render import render_markdown


def digest_json(document: dict[str, Any]) -> str:
    """Canonical JSON text of a digest document (sorted keys, ASCII, trailing newline)."""
    return json.dumps(document, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def digest_filename(document: dict[str, Any], fmt: str) -> str:
    rng = document["range"]
    extension = "md" if fmt == "md" else "json"
    return f"digest-{rng['old']['commit'][:12]}-{rng['new']['commit'][:12]}.{extension}"


__all__ = [
    "DIGEST_SCHEMA",
    "STATEMENTS",
    "AffectedFinding",
    "AffectedFindingsProvider",
    "ChangedRegion",
    "DigestBuilder",
    "NullFindingsProvider",
    "default_findings_provider",
    "digest_filename",
    "digest_json",
    "render_markdown",
]
