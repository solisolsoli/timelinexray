"""Showing untrusted text on a terminal or in Markdown (public internal API).

Upstream paths, source lines, symbol names and parameter values are untrusted data. When
the CLI prints them for a human (not ``--json`` and not ``--raw``), or a digest writes them
into Markdown, terminal control characters and invisible direction overrides are shown as
visible escapes, so a file name or a line of source can never move the cursor, retitle a
terminal window, hide text or reorder what the reader sees:

* C0 controls (except those a caller keeps, such as tab or newline), DEL and C1 controls
  become ``\\xNN``;
* the Unicode direction marks, embeddings, overrides and isolates (U+200E, U+200F,
  U+202A-U+202E, U+2066-U+2069) and the line and paragraph separators (U+2028, U+2029)
  become ``\\uNNNN``;
* the ``surrogateescape`` form of a byte that is not UTF-8 (a lone surrogate U+DC80-U+DCFF,
  which is how a non-UTF-8 upstream path name reaches Python) becomes ``\\xNN`` of that byte,
  so such a name can be written to a terminal or a file (any other lone surrogate becomes
  ``\\uNNNN``).

JSON output needs none of this: it is always serialised with ASCII escapes. ``--raw``
output is exact bytes by design.
"""

from __future__ import annotations

import re

_INVISIBLE = "‎‏‪‫‬‭‮⁦⁧⁨⁩  "
_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f\ud800-\udfff" + _INVISIBLE + "]")
_UNSAFE_BYTES = re.compile(
    rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"  # C0 controls except tab, LF, CR; DEL
    rb"|\xc2[\x80-\x9f]"  # C1 controls (UTF-8)
    rb"|\xe2\x80[\x8e\x8f\xa8\xa9\xaa-\xae]"  # LRM, RLM, LS, PS, embeddings and overrides
    rb"|\xe2\x81[\xa6-\xa9]"  # isolates
)


def _escape(char: str) -> str:
    code = ord(char)
    if 0xDC80 <= code <= 0xDCFF:  # surrogateescape: the original byte
        return f"\\x{code - 0xDC00:02x}"
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def visible(text: str, keep: str = "") -> str:
    """``text`` with every unsafe character (other than those in ``keep``) escaped."""
    return _UNSAFE.sub(lambda m: m.group() if m.group() in keep else _escape(m.group()), text)


def visible_bytes(data: bytes) -> tuple[bytes, int]:
    """Bytes for a terminal: unsafe sequences escaped (tab, LF and CR kept), and how many."""
    count = 0

    def replace(match: re.Match[bytes]) -> bytes:
        nonlocal count
        count += 1
        return _escape(match.group().decode("utf-8")).encode("ascii") if len(match.group()) > 1 \
            else f"\\x{match.group()[0]:02x}".encode("ascii")

    return _UNSAFE_BYTES.sub(replace, data), count


__all__ = ["visible", "visible_bytes"]
