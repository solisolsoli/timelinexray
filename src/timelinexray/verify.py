"""Citation verification and re-anchoring (Milestone 3).

A citation is a Milestone 1 span record: commit, repository path, 1-based inclusive line
range, byte range, blob id, blob and span SHA-256 and an anchor (exact text expected to
occur inside the span, once or several times). This module answers two questions about it
and never anything else:

1. **Integrity** (:meth:`Verifier.check`): are the cited bytes, read again from the git
   blob at the citation's own commit, still exactly the recorded bytes?

   ============ ==============================================================================
   ``INTACT``   the span is readable and its bytes (and byte range and blob) equal what the
                citation records; a citation that recorded no hash yet is ``INTACT`` with
                ``baseline`` set (its hashes are observed now, not compared)
   ``CHANGED``  the span is readable but its SHA-256, byte range or blob differs from the
                recorded values
   ``MISSING``  the span cannot be read: commit not pinned or malformed, path not in the
                manifest, not a readable text blob, or a line range that is invalid or
                outside the file
   ============ ==============================================================================

   The anchor verdict (``FOUND``, ``FOUND_MULTIPLE``, ``MISSING``; see
   :mod:`timelinexray.span`) is reported beside the integrity verdict. A citation is
   *usable* when it is ``INTACT`` and its anchor occurs inside the span: ``FOUND`` (once)
   or ``FOUND_MULTIPLE`` (every occurrence lies inside the cited span, so the content is
   confirmed). An anchor that is ``MISSING`` makes the citation unusable.

2. **Re-anchoring** (:meth:`Verifier.relocate`): where are the cited bytes at a newer target
   commit? The cited span is read at its own commit and looked up, byte for byte, in the
   target commit:

   ================================================ =============== ===============
   Situation at the target commit                   Outcome         Freshness
   ================================================ =============== ===============
   same path, same blob                             ``identical``   ``CURRENT``
   same path, exact span bytes once, same lines     ``unchanged``   ``CURRENT``
   exact span bytes once at other lines or, when    ``relocated``   ``CURRENT``
   the path is gone, in exactly one other file
   exact span bytes more than once                  ``ambiguous``   ``UNVERIFIABLE``
   path present, exact span bytes absent            ``changed``     ``STALE``
   path gone and the bytes found nowhere else       ``missing``     ``UNVERIFIABLE``
   the citation is not usable at its own commit     ``unusable``    ``UNVERIFIABLE``
   ================================================ =============== ===============

   Uniqueness is required here, and only here: a span moves to another commit only when
   its exact bytes occur exactly once there. A match counts only when it starts at a line
   start and ends at a line end, so it is a whole line range under the span contract. Of
   several matches none is ever chosen (never a first match): the outcome is ``ambiguous``
   (``UNVERIFIABLE``) with a reason and every match listed as a candidate. When the path still exists, the bytes are only looked up in
   that path; other files are searched only when the path is gone (a rename or move):
   first by identical blob id, then, if the target commit is indexed, through the Milestone
   2 code index (a literal search for the anchor's longest line, whose hits are the only
   lexically indexed files that can contain the span). Files the lexical index skips
   (binary, generated, oversized, not UTF-8) are not searched; a ``missing`` outcome says
   "the lexically indexed files of the target" and counts the skipped files by reason
   (``search.lexical_coverage``). For a ``changed`` span a line diff of the two blobs
   proposes the corresponding region for the reviewer; the proposal is evidence for review
   and never makes a finding current.

A matching hash is text identity only. Nothing here can mark a claim ``SUPPORTED``; only a
reviewer can (see :mod:`timelinexray.findings`).
"""

from __future__ import annotations

import difflib
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from .errors import InvalidInput, NotFound, Refused, SpanRangeError, TxrayError
from .snapshot.store import PinRecord, SnapshotStore, validate_commit_input, validate_repo_path
from .span import (
    CONFIRMING,
    FOUND,
    FOUND_MULTIPLE,
    MISSING as ANCHOR_MISSING,
    LineMap,
    SpanRead,
    find_occurrences,
    read_span,
)

VERIFIER_VERSION = "timelinexray.verify/1"

# Integrity verdicts.
INTACT = "INTACT"
CHANGED = "CHANGED"
MISSING = "MISSING"
VERDICTS = (INTACT, CHANGED, MISSING)

# Freshness of a citation or finding relative to a commit.
CURRENT = "CURRENT"
STALE = "STALE"
UNVERIFIABLE = "UNVERIFIABLE"
NOT_CHECKED = "NOT_CHECKED"
FRESHNESS = (CURRENT, STALE, UNVERIFIABLE, NOT_CHECKED)

# Re-anchoring outcomes.
IDENTICAL = "identical"
UNCHANGED = "unchanged"
RELOCATED = "relocated"
AMBIGUOUS_SPAN = "ambiguous"
CHANGED_SPAN = "changed"
MISSING_SPAN = "missing"
UNUSABLE = "unusable"
OUTCOMES = (IDENTICAL, UNCHANGED, RELOCATED, AMBIGUOUS_SPAN, CHANGED_SPAN, MISSING_SPAN, UNUSABLE)
_OUTCOME_FRESHNESS = {
    IDENTICAL: CURRENT,
    UNCHANGED: CURRENT,
    RELOCATED: CURRENT,
    AMBIGUOUS_SPAN: UNVERIFIABLE,
    CHANGED_SPAN: STALE,
    MISSING_SPAN: UNVERIFIABLE,
    UNUSABLE: UNVERIFIABLE,
}

MAX_ANCHOR_CHARS = 1000
DIFF_MAX_LINES = 80
_DIFF_MAX_FILE_LINES = 50_000
_HEX = frozenset("0123456789abcdef")


def worst_freshness(values: list[str]) -> str:
    """Combine freshness values: ``STALE`` beats ``UNVERIFIABLE`` beats ``CURRENT``."""
    if not values:
        return NOT_CHECKED
    for value in (STALE, UNVERIFIABLE, NOT_CHECKED):
        if value in values:
            return value
    return CURRENT


def _check_sha(value: object, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 64 or not set(value) <= _HEX:
        raise InvalidInput(f"{what} must be 64 lowercase hexadecimal digits")
    return value


@dataclass(frozen=True, slots=True)
class Citation:
    """A code citation: a span record of Milestone 1 plus the anchor it must contain."""

    commit: str
    path: str
    start_line: int
    end_line: int
    anchor: str
    span_sha256: str | None = None
    blob_oid: str | None = None
    blob_sha256: str | None = None
    start_byte: int | None = None
    end_byte: int | None = None

    @classmethod
    def from_span(cls, span: SpanRead, anchor: str) -> "Citation":
        return cls(
            commit=span.commit,
            path=span.path,
            start_line=span.start_line,
            end_line=span.end_line,
            anchor=anchor,
            span_sha256=span.sha256,
            blob_oid=span.blob_oid,
            blob_sha256=span.blob_sha256,
            start_byte=span.start_byte,
            end_byte=span.end_byte,
        )

    @classmethod
    def from_dict(cls, data: object) -> "Citation":
        if not isinstance(data, dict):
            raise InvalidInput("a code citation must be a JSON object")
        commit = data.get("commit")
        if not isinstance(commit, str) or not commit:
            raise InvalidInput("a code citation needs a commit")
        path = validate_repo_path(data.get("path"))  # type: ignore[arg-type]
        start, end = data.get("start_line"), data.get("end_line")
        for value in (start, end):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise InvalidInput("start_line and end_line must be positive integers")
        anchor = data.get("anchor")
        if not isinstance(anchor, str) or not anchor or len(anchor) > MAX_ANCHOR_CHARS:
            raise InvalidInput(
                f"a code citation needs a non-empty anchor of at most {MAX_ANCHOR_CHARS} characters"
            )
        offsets = []
        for key in ("start_byte", "end_byte"):
            value = data.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                      or value < 0):
                raise InvalidInput(f"{key} must be a non-negative integer")
            offsets.append(value)
        blob_oid = data.get("blob_oid")
        if blob_oid is not None and (not isinstance(blob_oid, str) or not set(blob_oid) <= _HEX
                                     or len(blob_oid) not in (40, 64)):
            raise InvalidInput("blob_oid must be a full hexadecimal object id")
        return cls(
            commit=commit,
            path=path,
            start_line=start,  # type: ignore[arg-type]
            end_line=end,  # type: ignore[arg-type]
            anchor=anchor,
            span_sha256=_check_sha(data.get("span_sha256"), "span_sha256"),
            blob_oid=blob_oid,
            blob_sha256=_check_sha(data.get("blob_sha256"), "blob_sha256"),
            start_byte=offsets[0],
            end_byte=offsets[1],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "code",
            "commit": self.commit,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "blob_oid": self.blob_oid,
            "blob_sha256": self.blob_sha256,
            "span_sha256": self.span_sha256,
            "anchor": self.anchor,
        }

    @property
    def lines(self) -> str:
        return f"{self.start_line}-{self.end_line}"

    def label(self) -> str:
        return f"{self.path}:{self.lines}@{self.commit[:12]}"


@dataclass(frozen=True, slots=True)
class CitationCheck:
    """Integrity of one citation at its own commit."""

    citation: Citation
    verdict: str
    reason: str | None
    anchor_verdict: str | None
    anchor_lines: tuple[int, ...]
    observed: Citation | None
    baseline: bool

    @property
    def usable(self) -> bool:
        return self.verdict == INTACT and self.anchor_verdict in CONFIRMING

    def to_dict(self) -> dict[str, Any]:
        return {
            "citation": self.citation.to_dict(),
            "verdict": self.verdict,
            "reason": self.reason,
            "anchor_verdict": self.anchor_verdict,
            "anchor_lines": list(self.anchor_lines),
            "observed": self.observed.to_dict() if self.observed else None,
            "baseline": self.baseline,
            "usable": self.usable,
        }


@dataclass(frozen=True, slots=True)
class Relocation:
    """Where a citation's exact bytes are at a target commit, and what that means."""

    citation: Citation
    target: str
    outcome: str
    reason: str | None
    current: Citation | None = None
    proposed: dict[str, Any] | None = None
    candidates: tuple[dict[str, Any], ...] = ()
    anchor_at_target: dict[str, Any] | None = None
    search: dict[str, Any] | None = None
    diff: str | None = None
    own: CitationCheck | None = field(default=None, compare=False)

    @property
    def freshness(self) -> str:
        return _OUTCOME_FRESHNESS[self.outcome]

    @property
    def moved(self) -> bool:
        return self.outcome == RELOCATED

    def to_dict(self) -> dict[str, Any]:
        return {
            "citation": self.citation.to_dict(),
            "target": self.target,
            "outcome": self.outcome,
            "freshness": self.freshness,
            "reason": self.reason,
            "current": self.current.to_dict() if self.current else None,
            "proposed": self.proposed,
            "candidates": list(self.candidates),
            "anchor_at_target": self.anchor_at_target,
            "search": self.search,
            "diff": self.diff,
        }


@dataclass(slots=True)
class _Blob:
    pin: PinRecord
    path: str
    oid: str
    data: bytes
    sha256: str
    lines: LineMap


def _whole_line_matches(data: bytes, lines: LineMap, needle: bytes) -> list[tuple[int, int]]:
    """1-based ``(start_line, end_line)`` of every occurrence that is a whole line range."""
    matches = []
    for offset in find_occurrences(data, needle):
        if offset and data[offset - 1 : offset] != b"\n":
            continue
        end = offset + len(needle)
        if not needle.endswith(b"\n") and end != len(data):
            continue
        matches.append((lines.line_of_offset(offset), lines.line_of_offset(end - 1)))
    return matches


def _split_lines(data: bytes) -> list[bytes]:
    lines = LineMap.of(data)
    return [data[start : lines.line_end(number + 1)] for number, start in enumerate(lines.starts)]


def map_region(old: bytes, new: bytes, start: int, end: int) -> tuple[int, int] | None:
    """Lines of ``new`` that a line diff aligns with lines ``start-end`` of ``old``.

    Deterministic (:class:`difflib.SequenceMatcher` without the junk heuristic). Returns
    ``None`` when the cited lines were deleted outright or a file is too large to diff.
    """
    old_lines, new_lines = _split_lines(old), _split_lines(new)
    if max(len(old_lines), len(new_lines)) > _DIFF_MAX_FILE_LINES:
        return None
    lo, hi = start - 1, end
    mapped: list[int] = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            first, last = max(i1, lo), min(i2, hi)
            mapped.extend(j1 + (k - i1) for k in range(first, last))
        elif tag == "replace" and i1 < hi and i2 > lo:
            mapped.extend(range(j1, j2))
        elif tag == "insert" and lo < i1 < hi:
            mapped.extend(range(j1, j2))
    if not mapped:
        return None
    return min(mapped) + 1, max(mapped) + 1


def _unified(old: bytes, new: bytes, old_label: str, new_label: str) -> str:
    # LF-only lines (the rule of every line number in this tool): str.splitlines() would also
    # break at CR, FF, VT, FS-RS, NEL, LS and PS and shift the hunk headers
    old_text = [line.decode("utf-8", "replace") for line in _split_lines(old)]
    new_text = [line.decode("utf-8", "replace") for line in _split_lines(new)]
    lines = list(difflib.unified_diff(old_text, new_text, old_label, new_label, n=1))
    if len(lines) > DIFF_MAX_LINES:
        lines = lines[:DIFF_MAX_LINES] + [f"... ({len(lines) - DIFF_MAX_LINES} more diff lines)\n"]
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines)


class Verifier:
    """Checks and re-anchors citations against a snapshot store (and optionally an index)."""

    def __init__(self, store: SnapshotStore, index: Any | None = None) -> None:
        self.store = store
        self.index = index
        self._blobs: dict[tuple[str, str], _Blob | TxrayError] = {}
        self._indexed: dict[str, bool] = {}
        self._pinned: set[str] = set()
        self._coverage: dict[str, dict[str, Any] | None] = {}

    # -- blobs -------------------------------------------------------------------------

    def _blob(self, commit: str, path: str) -> _Blob:
        """Verified text blob of ``path`` at a pinned ``commit`` (errors are cached too)."""
        key = (commit, path)
        cached = self._blobs.get(key)
        if cached is None:
            try:
                pin, entry, data = self.store.read_blob(commit, path)
                cached = _Blob(pin, entry.path, entry.oid, data,
                               hashlib.sha256(data).hexdigest(), LineMap.of(data))
            except TxrayError as exc:
                cached = exc
            self._blobs[key] = cached
        if isinstance(cached, TxrayError):
            raise cached
        return cached

    def prefetch(self, commit: str, paths: Iterable[str]) -> None:
        """Read the blobs of ``paths`` at ``commit`` into the cache through one git process.

        Every later :meth:`check` or :meth:`relocate` touching them then reads nothing; the
        cached results (bytes or errors) are exactly what one-by-one reads would give.
        """
        full = self.full_commit(commit)
        missing = [path for path in dict.fromkeys(paths) if (full, path) not in self._blobs]
        if not missing:
            return
        pin, results = self.store.read_blobs(full, missing)
        for path, result in results.items():
            if isinstance(result, TxrayError):
                self._blobs[(full, path)] = result
            else:
                entry, data = result
                self._blobs[(full, path)] = _Blob(pin, entry.path, entry.oid, data,
                                                  hashlib.sha256(data).hexdigest(),
                                                  LineMap.of(data))

    def full_commit(self, commit: str) -> str:
        """The pinned full commit id for ``commit`` (raises when it is not pinned).

        A full id that resolved once is remembered: pins are never removed, so the answer
        cannot change (a prefix is looked up every time, since a later pin could make it
        ambiguous).
        """
        if commit in self._pinned:
            return commit
        full = self.store.get_pin(commit).commit
        if commit == full:
            self._pinned.add(full)
        return full

    def is_indexed(self, commit: str) -> bool:
        """Whether the code index has a current generation for ``commit`` (only yes is cached)."""
        if self.index is None:
            return False
        if commit not in self._indexed:
            try:
                self.index.generation(commit)
            except TxrayError:
                return False
            self._indexed[commit] = True
        return True

    def lexical_coverage(self, commit: str) -> dict[str, Any] | None:
        """How many files of indexed ``commit`` the lexical index holds, and the skipped
        ones by reason (FA-025); ``None`` if the coverage cannot be read. Cached."""
        if commit not in self._coverage:
            try:
                rows = self.index.coverage(commit).rows  # type: ignore[union-attr]
            except TxrayError:
                self._coverage[commit] = None
            else:
                skipped: dict[str, int] = {}
                for row in rows:
                    if row.lexical_status != "indexed":
                        reason = row.lexical_reason or row.lexical_status
                        skipped[reason] = skipped.get(reason, 0) + 1
                self._coverage[commit] = {
                    "files": len(rows),
                    "lexically_indexed": len(rows) - sum(skipped.values()),
                    "not_lexically_indexed": dict(sorted(skipped.items())),
                }
        return self._coverage[commit]

    # -- integrity ---------------------------------------------------------------------

    def resolve(self, commit: str, path: str, start: int, end: int, anchor: str) -> CitationCheck:
        """Read a new citation's span and return its check (``observed`` holds the record)."""
        return self.check(Citation(commit, path, start, end, anchor))

    def check(self, citation: Citation) -> CitationCheck:
        """Integrity of ``citation`` at its own commit (see the module documentation)."""

        def missing(reason: str) -> CitationCheck:
            return CitationCheck(citation, MISSING, reason, None, (), None, False)

        try:
            commit = validate_commit_input(citation.commit)
        except InvalidInput:
            return missing("invalid_commit")
        try:
            full = self.full_commit(commit)
        except NotFound:
            return missing("commit_not_pinned")
        except InvalidInput:
            return missing("commit_ambiguous")
        try:
            blob = self._blob(full, citation.path)
        except NotFound:
            return missing("path_missing")
        except Refused:
            return missing("not_a_text_blob")
        except InvalidInput:
            return missing("invalid_path")
        try:
            span = read_span(
                blob.data, citation.start_line, citation.end_line, commit=full,
                path=blob.path, blob_oid=blob.oid, anchor=citation.anchor,
                line_map=blob.lines, blob_sha256=blob.sha256,
            )
        except SpanRangeError:
            return missing("invalid_range")
        observed = Citation.from_span(span, citation.anchor)
        assert span.anchor is not None
        anchor = span.anchor
        recorded = (citation.span_sha256, citation.blob_sha256, citation.blob_oid,
                    citation.start_byte, citation.end_byte)
        baseline = all(value is None for value in recorded)
        reason = None
        if citation.span_sha256 is not None and citation.span_sha256 != observed.span_sha256:
            reason = "span_sha256_differs"
        elif citation.blob_sha256 is not None and citation.blob_sha256 != observed.blob_sha256:
            reason = "blob_sha256_differs"
        elif citation.blob_oid is not None and citation.blob_oid != observed.blob_oid:
            reason = "blob_oid_differs"
        elif (citation.start_byte, citation.end_byte) != (None, None) and (
            (citation.start_byte, citation.end_byte) != (observed.start_byte, observed.end_byte)
        ):
            reason = "byte_range_differs"
        return CitationCheck(
            citation=citation,
            verdict=CHANGED if reason else INTACT,
            reason=reason,
            anchor_verdict=anchor.verdict,
            anchor_lines=anchor.lines,
            observed=observed,
            baseline=baseline,
        )

    # -- re-anchoring ------------------------------------------------------------------

    def relocate(self, citation: Citation, target: str, *, propose: bool = True) -> Relocation:
        """Re-anchor ``citation`` on the pinned ``target`` commit (see the module docs).

        ``propose=False`` skips the line-diff proposal and unified diff for a ``changed``
        span (a caller that only asks where a span is, such as the digest's affected
        findings, needs neither); the outcome and freshness are the same either way.
        """
        target = self.full_commit(target)
        own = self.check(citation)
        if not own.usable:
            detail = own.reason or own.anchor_verdict
            return Relocation(
                citation, target, UNUSABLE,
                f"the citation is not usable at its own commit: {own.verdict}"
                + (f" ({detail})" if detail else "")
                + (f", anchor {own.anchor_verdict}" if own.anchor_verdict not in (None, *CONFIRMING) else ""),
                own=own,
            )
        assert own.observed is not None and own.observed.start_byte is not None
        source = self._blob(own.observed.commit, citation.path)
        span = source.data[own.observed.start_byte : own.observed.end_byte]
        _, manifest = self.store.load_manifest(target)
        entry = manifest.entry(citation.path)
        readable = None
        if entry is not None and entry.type == "blob" and not entry.is_symlink:
            try:
                readable = self._blob(target, citation.path)
            except (Refused, NotFound):
                readable = None
        if readable is not None:
            return self._relocate_in_path(citation, own, source, span, readable, target,
                                          propose=propose)
        return self._relocate_elsewhere(citation, own, source, span, manifest, target)

    def _current(self, blob: _Blob, start: int, end: int, anchor: str) -> Citation:
        span = read_span(blob.data, start, end, commit=blob.pin.commit, path=blob.path,
                         blob_oid=blob.oid, anchor=anchor, line_map=blob.lines,
                         blob_sha256=blob.sha256)
        return Citation.from_span(span, anchor)

    def _relocate_in_path(
        self, citation: Citation, own: CitationCheck, source: _Blob, span: bytes,
        target_blob: _Blob, target: str, *, propose: bool = True,
    ) -> Relocation:
        assert own.observed is not None
        old = own.observed
        if target_blob.oid == source.oid:
            current = replace(old, commit=target)
            return Relocation(citation, target, IDENTICAL, None, current=current, own=own)
        matches = _whole_line_matches(target_blob.data, target_blob.lines, span)
        if len(matches) == 1:
            start, end = matches[0]
            current = self._current(target_blob, start, end, citation.anchor)
            same = (start, end) == (old.start_line, old.end_line)
            reason = None if same else (
                f"the cited bytes moved from lines {old.lines} to {start}-{end}")
            return Relocation(citation, target, UNCHANGED if same else RELOCATED, reason,
                              current=current, own=own)
        anchor_all = find_occurrences(target_blob.data, citation.anchor.encode("utf-8"))
        anchor_info = {
            "path": citation.path,
            "count": len(anchor_all),
            "lines": sorted({target_blob.lines.line_of_offset(o) for o in anchor_all})[:50],
        }
        if len(matches) > 1:
            candidates = tuple({"path": citation.path, "start_line": s, "end_line": e}
                               for s, e in matches)
            return Relocation(
                citation, target, AMBIGUOUS_SPAN,
                f"the cited bytes occur {len(matches)} times in {citation.path} at the target; "
                "no occurrence is chosen",
                candidates=candidates, anchor_at_target=anchor_info, own=own,
            )
        proposed = None
        diff = None
        if not propose:
            return Relocation(
                citation, target, CHANGED_SPAN,
                f"the cited bytes of lines {old.lines} are not in {citation.path} at the "
                "target; no line-diff proposal was requested",
                anchor_at_target=anchor_info, own=own,
            )
        region = map_region(source.data, target_blob.data, old.start_line, old.end_line)
        if region is not None:
            proposal = self._current(target_blob, region[0], region[1], citation.anchor)
            proposed = {
                "path": citation.path,
                "start_line": region[0],
                "end_line": region[1],
                "span_sha256": proposal.span_sha256,
                "anchor_verdict": self._anchor_verdict(target_blob, region, citation.anchor),
                "method": "line diff of the two blobs (difflib, no junk heuristic)",
            }
            diff = _unified(
                span, target_blob.data[proposal.start_byte : proposal.end_byte],  # type: ignore[index]
                f"a/{citation.path} (lines {old.lines} at {old.commit[:12]})",
                f"b/{citation.path} (lines {region[0]}-{region[1]} at {target[:12]})",
            )
        where = (f"; the line diff aligns them with lines {region[0]}-{region[1]}"
                 if region else "; the line diff aligns them with no remaining lines")
        return Relocation(
            citation, target, CHANGED_SPAN,
            f"the cited bytes of lines {old.lines} are not in {citation.path} at the target"
            + where,
            proposed=proposed, anchor_at_target=anchor_info, diff=diff, own=own,
        )

    @staticmethod
    def _anchor_verdict(blob: _Blob, region: tuple[int, int], anchor: str) -> str:
        start, end = blob.lines.byte_range(region[0], region[1])
        count = len(find_occurrences(blob.data[start:end], anchor.encode("utf-8")))
        return ANCHOR_MISSING if count == 0 else FOUND if count == 1 else FOUND_MULTIPLE

    def _relocate_elsewhere(
        self, citation: Citation, own: CitationCheck, source: _Blob, span: bytes,
        manifest: Any, target: str,
    ) -> Relocation:
        candidates: set[str] = {
            entry.path for entry in manifest.entries
            if entry.oid == source.oid and entry.type == "blob" and not entry.is_symlink
            and entry.path != citation.path
        }
        search: dict[str, Any] = {"by_blob_id": sorted(candidates)}
        complete = False
        if self.is_indexed(target):
            fragment = max((line.strip() for line in citation.anchor.splitlines()), key=len,
                           default="")[:512]
            try:
                result = self.index.search(target, fragment, literal=True, limit=1000)
            except InvalidInput as exc:
                search["index"] = f"not searched: {exc}"
            else:
                hit_paths = {hit.path for hit in result.hits} - {citation.path}
                candidates |= hit_paths
                complete = not result.truncated
                search["index"] = {"query": fragment, "hits": result.total,
                                   "truncated": result.truncated, "paths": sorted(hit_paths)[:50]}
                search["lexical_coverage"] = self.lexical_coverage(target)
        else:
            search["index"] = "not searched: the target commit is not indexed (run txray index)"
        matches: list[dict[str, Any]] = []
        for path in sorted(candidates):
            try:
                blob = self._blob(target, path)
            except (Refused, NotFound, InvalidInput):
                continue
            for start, end in _whole_line_matches(blob.data, blob.lines, span):
                matches.append({"path": path, "start_line": start, "end_line": end,
                                "blob": blob})
        search["complete"] = complete
        public = tuple({k: v for k, v in m.items() if k != "blob"} for m in matches)
        by_blob = [m for m in matches if m["blob"].oid == source.oid]
        if len(matches) == 1 and (complete or by_blob):
            match = matches[0]
            current = self._current(match["blob"], match["start_line"], match["end_line"],
                                    citation.anchor)
            return Relocation(
                citation, target, RELOCATED,
                f"{citation.path} is not in the target; the cited bytes are at "
                f"{match['path']}:{match['start_line']}-{match['end_line']}",
                current=current, search=search, own=own,
            )
        if len(matches) > 1 or (len(matches) == 1 and not complete):
            why = (f"the cited bytes occur {len(matches)} times in other files"
                   if len(matches) > 1 else
                   "the cited bytes were found once, but the file search was incomplete")
            return Relocation(
                citation, target, AMBIGUOUS_SPAN,
                f"{citation.path} is not in the target; {why}; no occurrence is chosen",
                candidates=public, search=search, own=own,
            )
        if complete:
            coverage = search.get("lexical_coverage")
            scope = "the lexically indexed files of the target"
            if coverage:
                skipped = coverage["not_lexically_indexed"]
                scope += (f" ({coverage['lexically_indexed']} of {coverage['files']} files; "
                          + (f"{sum(skipped.values())} not lexically indexed: "
                             + ", ".join(f"{reason} {n}" for reason, n in skipped.items())
                             + ", not searched" if skipped else "none skipped") + ")")
        else:
            scope = "files with the same blob id only"
        return Relocation(
            citation, target, MISSING_SPAN,
            f"{citation.path} is not a readable text file in the target and the cited bytes "
            f"were not found elsewhere (searched: {scope})",
            search=search, own=own,
        )
