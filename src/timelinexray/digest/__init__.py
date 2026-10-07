"""Reviewed-change digests for a commit range (Milestone 5b).

* :class:`DigestBuilder` builds the digest document (JSON-ready) from diffs of pinned
  commits: summary counts by class, parameter-default table with timelines and
  reversions, registration changes, affected findings (through a read-only provider),
  unknown changes, intermediate history and health.
* :func:`render_markdown` renders the main digest (summary first, bounded size),
  :func:`render_appendix` every item the main digest only counts, :func:`digest_json`
  the complete document canonically; :func:`write_digests` writes them.
* :mod:`timelinexray.digest.update` runs a scheduled update: guarded fetch, pin of the new
  head, digest against the previous pin, exceptional events.

A digest is generated from diffs only; commit messages are never read. Nothing is posted:
digests are local files.
"""

from __future__ import annotations

import json
from typing import Any

from ..fsutil import atomic_write
from .build import DIGEST_SCHEMA, STATEMENTS, DigestBuilder
from .findings import (
    AffectedFinding,
    AffectedFindingsProvider,
    ChangedRegion,
    NullFindingsProvider,
    default_findings_provider,
)
from .render import digest_names, render_appendix, render_markdown

#: Digest formats: the main Markdown digest, its appendix, the JSON document.
FORMATS = ("md", "appendix", "json")


def digest_json(document: dict[str, Any]) -> str:
    """Canonical JSON text of a digest document (sorted keys, ASCII, trailing newline)."""
    return json.dumps(document, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def digest_filename(document: dict[str, Any], fmt: str) -> str:
    """``digest-<old>-<new>.md``, ``digest-<old>-<new>-appendix.md`` or ``….json``."""
    main, appendix, data = digest_names(document)
    return {"md": main, "appendix": appendix}.get(fmt, data)


def render_digest(document: dict[str, Any], fmt: str) -> str:
    """The text of one digest format (see :data:`FORMATS`)."""
    if fmt == "md":
        return render_markdown(document)
    if fmt == "appendix":
        return render_appendix(document)
    return digest_json(document)


def write_digests(out_dir: Any, document: dict[str, Any], formats: tuple[str, ...]) -> list[str]:
    """Write the requested formats into ``out_dir`` atomically; ``md`` brings its appendix,
    because the main file counts what only the appendix lists. Returns the file names."""
    wanted: list[str] = []
    for fmt in formats:
        for name in (("md", "appendix") if fmt == "md" else (fmt,)):
            if name not in wanted:
                wanted.append(name)
    out_dir.mkdir(exist_ok=True)
    written = []
    for fmt in wanted:
        name = digest_filename(document, fmt)
        atomic_write(out_dir / name, render_digest(document, fmt).encode("utf-8"))
        written.append(name)
    return written


__all__ = [
    "DIGEST_SCHEMA",
    "STATEMENTS",
    "AffectedFinding",
    "AffectedFindingsProvider",
    "ChangedRegion",
    "DigestBuilder",
    "NullFindingsProvider",
    "default_findings_provider",
    "FORMATS",
    "digest_filename",
    "digest_json",
    "render_appendix",
    "render_digest",
    "render_markdown",
    "write_digests",
]
