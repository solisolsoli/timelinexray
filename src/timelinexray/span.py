"""Exact byte spans of git blobs, addressed by 1-based inclusive line ranges.

Byte-to-line mapping
--------------------
This is the contract every citation relies on. It operates on the raw blob bytes read
from the git object store; nothing is decoded, re-encoded or normalised.

1. Only LF (0x0A) ends a line. A line is its content bytes followed by its terminator,
   the LF. A CR immediately before the LF stays part of the line, so a CRLF line ends in
   ``b"\\r\\n"``. A CR that is not followed by LF is ordinary content.
2. If the blob does not end in LF, the bytes after the last LF form a final, unterminated
   line. A blob that ends in LF has no additional empty line after it.
3. An empty blob has zero lines, so
   ``line_count = blob.count(b"\\n") + (1 if blob and not blob.endswith(b"\\n") else 0)``.
4. Lines are numbered from 1. The span ``A-B`` requires ``1 <= A <= B <= line_count``
   and is the contiguous byte range from the first byte of line A through the last byte
   of line B, including line B's terminator when it has one. Out-of-range requests are
   errors; they are never clamped.
5. Byte offsets are 0-based, ``start_byte`` inclusive and ``end_byte`` exclusive, so
   ``blob[start_byte:end_byte]`` is the span. Concatenating spans ``1-k`` and
   ``(k+1)-n`` reproduces the blob byte for byte; CRLF endings, a missing final newline
   and a leading byte-order mark are preserved exactly.

Hashes: ``span_sha256`` is the SHA-256 of exactly the span bytes; ``blob_sha256`` is the
SHA-256 of the whole blob. Equal hashes establish text identity only, never that a claim
about the text is true.

Anchors: an anchor is matched as exact UTF-8 bytes (case-sensitive, no whitespace or
line-ending normalisation) against the span bytes only. An occurrence that starts or ends
outside the span is not seen. Overlapping occurrences are counted separately. The verdict
is ``FOUND`` for exactly one occurrence, ``FOUND_MULTIPLE`` for more than one and
``MISSING`` for none; every occurrence is reported, and none is ever selected as "the"
match. Both ``FOUND`` and ``FOUND_MULTIPLE`` confirm that the anchor text is inside the
cited span (:data:`CONFIRMING`); uniqueness matters only when a span is relocated to
another commit, and relocation matches the exact span bytes, never an anchor occurrence
(see :mod:`timelinexray.verify`).
"""

from __future__ import annotations

import base64
import bisect
import hashlib
import re
from dataclasses import dataclass
from typing import Any

from .errors import InvalidInput, SpanRangeError

FOUND = "FOUND"
FOUND_MULTIPLE = "FOUND_MULTIPLE"
MISSING = "MISSING"
ANCHOR_VERDICTS = (FOUND, FOUND_MULTIPLE, MISSING)
#: Anchor verdicts that confirm the anchor text occurs inside the cited span.
CONFIRMING = frozenset({FOUND, FOUND_MULTIPLE})

_RANGE = re.compile(r"([0-9]+)(?:-([0-9]+))?")


def parse_line_range(text: str) -> tuple[int, int]:
    """Parse ``"A-B"`` or ``"A"`` into ``(A, B)``; bounds are checked later."""
    match = _RANGE.fullmatch(text.strip()) if isinstance(text, str) else None
    if not match:
        raise InvalidInput(
            f"invalid line range {text!r}: expected A-B or A, with A and B positive integers"
        )
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) is not None else start
    return start, end


@dataclass(frozen=True, slots=True)
class LineMap:
    """Byte offset of the first byte of every line of a blob (see the module contract)."""

    starts: tuple[int, ...]
    size: int

    @classmethod
    def of(cls, data: bytes) -> "LineMap":
        if not data:
            return cls((), 0)
        starts = [0]
        size = len(data)
        index = data.find(b"\n")
        while index != -1 and index + 1 < size:
            starts.append(index + 1)
            index = data.find(b"\n", index + 1)
        return cls(tuple(starts), size)

    @property
    def line_count(self) -> int:
        return len(self.starts)

    def line_end(self, line: int) -> int:
        """Exclusive end offset of 1-based ``line``, terminator included."""
        return self.starts[line] if line < len(self.starts) else self.size

    def line_of_offset(self, offset: int) -> int:
        """1-based line containing byte ``offset``."""
        return bisect.bisect_right(self.starts, offset)

    def byte_range(self, start: int, end: int, *, label: str = "blob") -> tuple[int, int]:
        """Validate ``start-end`` and return its ``(start_byte, end_byte)``."""
        if start < 1 or end < 1:
            raise SpanRangeError(f"invalid line range {start}-{end}: line numbers start at 1")
        if start > end:
            raise SpanRangeError(f"invalid line range {start}-{end}: start line is after end line")
        count = self.line_count
        if count == 0:
            raise SpanRangeError(
                f"line range {start}-{end} is past end of file: {label} is empty (0 lines)"
            )
        if end > count:
            noun = "line" if count == 1 else "lines"
            raise SpanRangeError(
                f"line range {start}-{end} is past end of file: {label} has {count} {noun}"
            )
        return self.starts[start - 1], self.line_end(end)


def find_occurrences(data: bytes, needle: bytes) -> list[int]:
    """Offsets of every (possibly overlapping) occurrence of ``needle`` in ``data``."""
    if not needle:
        raise InvalidInput("anchor must not be empty")
    offsets: list[int] = []
    index = data.find(needle)
    while index != -1:
        offsets.append(index)
        index = data.find(needle, index + 1)
    return offsets


@dataclass(frozen=True, slots=True)
class AnchorCheck:
    anchor: str
    verdict: str
    lines: tuple[int, ...]  # 1-based file line where each occurrence starts
    byte_offsets: tuple[int, ...]  # 0-based blob offset of each occurrence

    @property
    def count(self) -> int:
        return len(self.byte_offsets)

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor": self.anchor,
            "verdict": self.verdict,
            "count": self.count,
            "lines": list(self.lines),
            "byte_offsets": list(self.byte_offsets),
        }


@dataclass(frozen=True, slots=True)
class SpanRead:
    """An exact span of a blob at a commit, with integrity metadata."""

    commit: str
    path: str
    blob_oid: str
    blob_sha256: str
    blob_size: int
    line_count: int
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    data: bytes
    sha256: str
    lf_lines: int
    crlf_lines: int
    unterminated_lines: int
    anchor: AnchorCheck | None

    @property
    def text(self) -> str | None:
        try:
            return self.data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def to_dict(self) -> dict[str, Any]:
        text = self.text
        result: dict[str, Any] = {
            "commit": self.commit,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "line_count": self.line_count,
            "start_byte": self.start_byte,
            "end_byte": self.end_byte,
            "byte_length": len(self.data),
            "blob": {"oid": self.blob_oid, "sha256": self.blob_sha256, "size": self.blob_size},
            "span_sha256": self.sha256,
            "line_terminators": {
                "lf": self.lf_lines,
                "crlf": self.crlf_lines,
                "none": self.unterminated_lines,
            },
            "encoding": "utf-8" if text is not None else None,
            "text": text,
            "anchor": self.anchor.to_dict() if self.anchor else None,
        }
        if text is None:
            result["base64"] = base64.b64encode(self.data).decode("ascii")
        return result


def read_span(
    blob: bytes,
    start: int,
    end: int,
    *,
    commit: str,
    path: str,
    blob_oid: str,
    anchor: str | None = None,
    line_map: LineMap | None = None,
    blob_sha256: str | None = None,
) -> SpanRead:
    """Cut lines ``start-end`` out of ``blob`` according to the module contract.

    ``line_map`` and ``blob_sha256``, when given, must be ``LineMap.of(blob)`` and the
    SHA-256 of ``blob``; a caller cutting many spans from one blob computes them once.
    """
    lines = line_map if line_map is not None else LineMap.of(blob)
    start_byte, end_byte = lines.byte_range(start, end, label=path)
    data = blob[start_byte:end_byte]
    total_lf = data.count(b"\n")
    crlf = data.count(b"\r\n")
    check: AnchorCheck | None = None
    if anchor is not None:
        offsets = [start_byte + off for off in find_occurrences(data, anchor.encode("utf-8"))]
        verdict = MISSING if not offsets else FOUND if len(offsets) == 1 else FOUND_MULTIPLE
        check = AnchorCheck(
            anchor,
            verdict,
            tuple(lines.line_of_offset(off) for off in offsets),
            tuple(offsets),
        )
    return SpanRead(
        commit=commit,
        path=path,
        blob_oid=blob_oid,
        blob_sha256=blob_sha256 if blob_sha256 is not None else hashlib.sha256(blob).hexdigest(),
        blob_size=len(blob),
        line_count=lines.line_count,
        start_line=start,
        end_line=end,
        start_byte=start_byte,
        end_byte=end_byte,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        lf_lines=total_lf - crlf,
        crlf_lines=crlf,
        unterminated_lines=0 if data.endswith(b"\n") else 1,
        anchor=check,
    )
