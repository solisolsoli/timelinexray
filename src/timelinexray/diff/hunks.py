"""Line hunks between two blobs, on the byte-to-line contract of :mod:`timelinexray.span`.

Lines are cut from blob bytes exactly as :class:`timelinexray.span.LineMap` numbers them
(only LF ends a line; a CR stays in its line; a final unterminated line counts), so every
hunk line number is a valid ``txray show`` line number on its side. Lines are compared as
bytes: a changed line ending (LF to CRLF) is a changed line.

The matcher is :class:`difflib.SequenceMatcher` without its "junk" heuristic, run on the
lines left after trimming the common prefix and suffix. Its hunks can differ from
``git diff`` (a different longest-match algorithm) but always satisfy: applying the hunks
to the old lines yields exactly the new lines (tested).
"""

from __future__ import annotations

import difflib

from ..span import LineMap
from .model import Hunk


def split_lines(data: bytes, line_map: LineMap | None = None) -> list[bytes]:
    lines = line_map if line_map is not None else LineMap.of(data)
    return [data[start : lines.line_end(index + 1)] for index, start in enumerate(lines.starts)]


def line_hunks(old: list[bytes], new: list[bytes]) -> list[Hunk]:
    """Zero-context hunks turning ``old`` into ``new`` (1-based line numbers)."""
    prefix = 0
    limit = min(len(old), len(new))
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < limit - prefix
        and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]
    ):
        suffix += 1
    old_mid = old[prefix : len(old) - suffix]
    new_mid = new[prefix : len(new) - suffix]
    if not old_mid and not new_mid:
        return []
    if not old_mid or not new_mid:
        opcodes = [("replace", 0, len(old_mid), 0, len(new_mid))]
    else:
        matcher = difflib.SequenceMatcher(None, old_mid, new_mid, autojunk=False)
        opcodes = matcher.get_opcodes()
    hunks: list[Hunk] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        i1, i2, j1, j2 = i1 + prefix, i2 + prefix, j1 + prefix, j2 + prefix
        old_count, new_count = i2 - i1, j2 - j1
        hunks.append(
            Hunk(
                old_start=i1 + 1 if old_count else i1,
                old_count=old_count,
                new_start=j1 + 1 if new_count else j1,
                new_count=new_count,
            )
        )
    return hunks


def align_hunks(
    hunks: list[Hunk],
    old: list[bytes],
    new: list[bytes],
    old_spans: list[tuple[int, int]],
    new_spans: list[tuple[int, int]],
) -> list[Hunk]:
    """Slide pure insertions and deletions to the equivalent position that cuts the fewest
    declaration spans.

    A block of inserted (or deleted) lines can often move up or down without changing the
    result, when the line just before it equals its last line. Among the equivalent
    positions inside the unchanged lines around the block, prefer the one where no span
    (1-based inclusive line ranges, typically Milestone 2 symbols) straddles a block
    boundary, then one ending in a blank line, then the matcher's own position. This is
    the symbol-aware counterpart of git's indent heuristic; the result still satisfies
    :func:`apply_hunks`.
    """
    result: list[Hunk] = []
    for index, hunk in enumerate(hunks):
        if hunk.old_count and hunk.new_count:
            result.append(hunk)
            continue
        inserted = hunk.new_count > 0
        lines = new if inserted else old
        spans = new_spans if inserted else old_spans
        count = hunk.new_count if inserted else hunk.old_count
        start = (hunk.new_start if inserted else hunk.old_start) - 1  # 0-based block start
        previous = result[-1] if result else None
        low = _side_end(previous, inserted) if previous else 0
        following = hunks[index + 1] if index + 1 < len(hunks) else None
        high = _side_start(following, inserted) if following else len(lines)
        candidates = [start]
        position = start
        while position - 1 >= low and lines[position - 1] == lines[position + count - 1]:
            position -= 1
            candidates.append(position)
        position = start
        while position + count < high and lines[position] == lines[position + count]:
            position += 1
            candidates.append(position)

        def score(candidate: int) -> tuple[int, int, int, int]:
            first, last = candidate + 1, candidate + count
            cuts = sum(
                1
                for a, b in spans
                if (a < first <= b < last) or (first < a <= last < b)
            )
            blank_end = 0 if not lines[candidate + count - 1].strip() else 1
            return (cuts, blank_end, abs(candidate - start), candidate)

        best = min(candidates, key=score)
        delta = best - start
        if inserted:
            result.append(Hunk(hunk.old_start + delta, 0, hunk.new_start + delta, count))
        else:
            result.append(Hunk(hunk.old_start + delta, count, hunk.new_start + delta, 0))
    return result


def _side_end(hunk: Hunk, new_side: bool) -> int:
    """0-based index just after ``hunk`` on one side."""
    start, count = (hunk.new_start, hunk.new_count) if new_side else (hunk.old_start, hunk.old_count)
    return start - 1 + count if count else start


def _side_start(hunk: Hunk, new_side: bool) -> int:
    """0-based index of the first line of ``hunk`` on one side (its anchor if it has none)."""
    start, count = (hunk.new_start, hunk.new_count) if new_side else (hunk.old_start, hunk.old_count)
    return start - 1 if count else start


def apply_hunks(old: list[bytes], new: list[bytes], hunks: list[Hunk]) -> list[bytes]:
    """Rebuild the new lines from the old lines and the hunks (a self-check for tests)."""
    result: list[bytes] = []
    position = 0  # 0-based index into old
    for hunk in hunks:
        start = hunk.old_start - 1 if hunk.old_count else hunk.old_start
        result.extend(old[position:start])
        if hunk.new_count:
            result.extend(new[hunk.new_start - 1 : hunk.new_start - 1 + hunk.new_count])
        position = start + hunk.old_count
    result.extend(old[position:])
    return result
