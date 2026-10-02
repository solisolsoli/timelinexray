"""A strict reader for the small YAML subset research-import files use.

The standard library has no YAML parser, and the research reports deliver their findings
as YAML. This reader accepts exactly the block-style subset those files use and rejects
everything else with an error naming the line, so a file is either read as its author
wrote it or not at all:

* block mappings (``key: value``; keys are plain identifiers or quoted strings) and block
  sequences (``- item``, including ``- key: value`` items and sequences written at the
  same indentation as their parent key);
* scalars: plain (possibly continued on more-indented lines, folded with single spaces),
  single-quoted (``''`` escapes a quote) and double-quoted (backslash escapes), both of
  which may also span lines; ``null``, ``~`` and an empty value are null; the empty flow
  collections ``[]`` and ``{}``;
* comments (``#`` at a line start, or after whitespace outside quotes) and the document
  markers ``---`` and ``...``.

Unlike YAML 1.1 readers, plain scalars other than null stay strings (``no`` is ``"no"``,
``1`` is ``"1"``). Anchors, aliases, tags, block scalars (``|``, ``>``), non-empty flow
collections, complex keys, tabs in indentation and duplicate keys are errors.
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import InvalidInput

_KEY = re.compile(r"""(?P<key>[A-Za-z_][A-Za-z0-9_.-]*|"(?:[^"\\]|\\.)*"|'(?:[^']|'')*')[ ]*:(?:[ ]|$)""")
_NULLS = frozenset({"", "null", "Null", "NULL", "~"})
_INDICATORS = frozenset("&*!|>%@`{}[],")
_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f",
    "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85",
    "_": "\xa0", "L": " ", "P": " ",
}


class YamlError(InvalidInput):
    pass


class _Line:
    __slots__ = ("number", "indent", "text")

    def __init__(self, number: int, indent: int, text: str) -> None:
        self.number = number
        self.indent = indent
        self.text = text


def _strip_comment(text: str) -> str:
    """Remove a trailing comment from a plain-scalar line."""
    index = text.find(" #")
    return (text[:index] if index >= 0 else text).rstrip()


class _Reader:
    def __init__(self, source: str) -> None:
        self.raw = source.split("\n")
        self.lines: list[_Line] = []
        for number, raw in enumerate(self.raw, 1):
            line = raw[:-1] if raw.endswith("\r") else raw
            stripped = line.lstrip(" ")
            if stripped.startswith("\t") or (stripped and "\t" in line[: len(line) - len(stripped)]):
                raise YamlError(f"line {number}: tabs are not allowed in indentation")
            self.lines.append(_Line(number, len(line) - len(stripped), stripped))
        self.pos = 0

    # -- navigation --------------------------------------------------------------------

    def _is_blank(self, line: _Line) -> bool:
        return not line.text or line.text.startswith("#")

    def peek(self) -> _Line | None:
        while self.pos < len(self.lines) and self._is_blank(self.lines[self.pos]):
            self.pos += 1
        return self.lines[self.pos] if self.pos < len(self.lines) else None

    @staticmethod
    def _marker(line: _Line) -> bool:
        return line.indent == 0 and line.text.rstrip() in ("---", "...")

    def error(self, line: _Line | int, message: str) -> YamlError:
        number = line.number if isinstance(line, _Line) else line
        return YamlError(f"line {number}: {message}")

    # -- document ------------------------------------------------------------------------

    def document(self) -> Any:
        line = self.peek()
        if line is not None and line.indent == 0 and line.text.rstrip() == "---":
            self.pos += 1
            line = self.peek()
        if line is None:
            return None
        value = self.block(line.indent)
        line = self.peek()
        if line is not None and line.indent == 0 and line.text.rstrip() == "...":
            self.pos += 1
            line = self.peek()
        if line is not None:
            raise self.error(line, "unexpected content (more than one document, or bad "
                             "indentation)")
        return value

    def block(self, indent: int) -> Any:
        line = self.peek()
        assert line is not None
        if line.indent != indent:
            raise self.error(line, "bad indentation")
        if line.text == "-" or line.text.startswith("- "):
            return self.sequence(indent)
        return self.mapping(indent)

    def mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while True:
            line = self.peek()
            if line is None or line.indent < indent or self._marker(line):
                return result
            if line.indent > indent:
                raise self.error(line, "bad indentation")
            if line.text == "-" or line.text.startswith("- "):
                return result if result else self._fail_seq(line)
            match = _KEY.match(line.text)
            if not match:
                raise self.error(line, "expected 'key: value'")
            key = self._key(match.group("key"), line)
            if key in result:
                raise self.error(line, f"duplicate key {key!r}")
            rest = line.text[match.end():].lstrip(" ")
            self.pos += 1
            result[key] = self.value_after_key(rest, indent, line)

    def _fail_seq(self, line: _Line) -> dict[str, Any]:
        raise self.error(line, "a sequence item where a mapping key was expected")

    def _key(self, raw: str, line: _Line) -> str:
        if raw.startswith('"'):
            return self._double(raw[1:-1], line)
        if raw.startswith("'"):
            return raw[1:-1].replace("''", "'")
        return raw

    def value_after_key(self, rest: str, indent: int, line: _Line) -> Any:
        if rest and not rest.startswith("#"):
            return self.scalar(rest, indent, line)
        following = self.peek()
        if following is None:
            return None
        if following.indent > indent:
            return self.block(following.indent)
        if following.indent == indent and (following.text == "-" or
                                           following.text.startswith("- ")):
            return self.sequence(indent)
        return None

    def sequence(self, indent: int) -> list[Any]:
        result: list[Any] = []
        while True:
            line = self.peek()
            if line is None or line.indent < indent or self._marker(line):
                return result
            if line.indent > indent:
                raise self.error(line, "bad indentation")
            if not (line.text == "-" or line.text.startswith("- ")):
                return result
            rest = line.text[1:]
            spaces = len(rest) - len(rest.lstrip(" "))
            rest = rest.lstrip(" ")
            if not rest or rest.startswith("#"):
                self.pos += 1
                following = self.peek()
                if following is not None and following.indent > indent:
                    result.append(self.block(following.indent))
                else:
                    result.append(None)
                continue
            if rest.startswith("- ") or rest == "-":
                raise self.error(line, "nested sequences on one line are not supported")
            if _KEY.match(rest):
                # A compact mapping: re-read this line as a mapping at the item's column.
                self.lines[self.pos] = _Line(line.number, indent + 1 + spaces, rest)
                result.append(self.mapping(indent + 1 + spaces))
                continue
            self.pos += 1
            result.append(self.scalar(rest, indent, line))

    # -- scalars -----------------------------------------------------------------------

    def _continuation(self, parent: int) -> list[_Line | None]:
        """Following lines indented deeper than ``parent`` (``None`` marks a blank line)."""
        out: list[_Line | None] = []
        index = self.pos
        blank = 0
        while index < len(self.lines):
            line = self.lines[index]
            if not line.text:
                blank += 1
                index += 1
                continue
            if line.indent <= parent or line.text.startswith("#"):
                break
            out.extend([None] * blank)
            blank = 0
            out.append(line)
            index += 1
            self.pos = index
        return out

    def scalar(self, text: str, parent: int, line: _Line) -> Any:
        first = text[0]
        if first in "\"'":
            return self.quoted(text, parent, line)
        if text.rstrip() in ("[]", "{}") or _strip_comment(text) in ("[]", "{}"):
            return [] if _strip_comment(text).startswith("[") else {}
        if first in _INDICATORS or (first in "-?:" and text[1:2] in ("", " ")):
            raise self.error(line, f"unsupported YAML construct starting with {first!r}")
        parts = [_strip_comment(text)]
        for more in self._continuation(parent):
            if more is None:
                parts.append("")
                continue
            if " #" in more.text:
                raise self.error(more, "a comment inside a multi-line plain scalar")
            parts.append(more.text.rstrip())
        value = _fold(parts)
        if ": " in value or value.endswith(":"):
            raise self.error(line, "a plain scalar may not contain ': '; quote the value")
        return None if value in _NULLS else value

    def quoted(self, text: str, parent: int, line: _Line) -> str:
        quote = text[0]
        pieces: list[str] = []
        current = text[1:]
        number = line.number
        while True:
            end = _closing(current, quote)
            if end is not None:
                pieces.append(current[:end])
                tail = current[end + 1:].strip()
                if tail and not tail.startswith("#"):
                    raise YamlError(f"line {number}: unexpected text after a quoted scalar")
                break
            pieces.append(current)
            following = self.lines[self.pos] if self.pos < len(self.lines) else None
            if following is None or (following.text and following.indent <= parent):
                raise YamlError(f"line {line.number}: unterminated quoted scalar")
            self.pos += 1
            current = following.text
            number = following.number
        return self._join_quoted(pieces, quote, line)

    def _join_quoted(self, pieces: list[str], quote: str, line: _Line) -> str:
        """Fold the lines of a quoted scalar: a break is a space, each blank line a newline."""
        out: list[str] = []
        blank = 0
        joined = False
        for index, piece in enumerate(pieces):
            first, last = index == 0, index == len(pieces) - 1
            text = piece if first else piece.lstrip(" ")
            if not first and not last and text == "":
                blank += 1
                continue
            if not first and not joined:
                out.append("\n" * blank if blank else " ")
            blank = 0
            joined = False
            if not last:
                if quote == '"' and _escaped_break(text):
                    text = text[:-1]
                    joined = True
                else:
                    text = text.rstrip(" ")
            out.append(text)
        raw = "".join(out)
        if quote == "'":
            return raw.replace("''", "'")
        return self._double(raw, line)

    def _double(self, raw: str, line: _Line) -> str:
        out: list[str] = []
        index = 0
        while index < len(raw):
            char = raw[index]
            if char != "\\":
                out.append(char)
                index += 1
                continue
            code = raw[index + 1 : index + 2]
            if code in _ESCAPES:
                out.append(_ESCAPES[code])
                index += 2
            elif code in ("x", "u", "U"):
                width = {"x": 2, "u": 4, "U": 8}[code]
                digits = raw[index + 2 : index + 2 + width]
                if len(digits) != width or not all(c in "0123456789abcdefABCDEF" for c in digits):
                    raise self.error(line, f"bad escape \\{code}{digits}")
                out.append(chr(int(digits, 16)))
                index += 2 + width
            else:
                raise self.error(line, f"unknown escape \\{code}")
        return "".join(out)


def _closing(text: str, quote: str) -> int | None:
    index = 0
    while index < len(text):
        char = text[index]
        if quote == '"' and char == "\\":
            index += 2
            continue
        if char == quote:
            if quote == "'" and text[index + 1 : index + 2] == "'":
                index += 2
                continue
            return index
        index += 1
    return None


def _escaped_break(text: str) -> bool:
    count = len(text) - len(text.rstrip("\\"))
    return count % 2 == 1


def _fold(parts: list[str]) -> str:
    out: list[str] = []
    blank = 0
    for index, part in enumerate(parts):
        if index and part == "":
            blank += 1
            continue
        if index:
            out.append("\n" * blank if blank else " ")
        blank = 0
        out.append(part)
    return "".join(out)


def load(text: str) -> Any:
    """Parse ``text`` (the YAML subset above) into dicts, lists, strings and ``None``."""
    return _Reader(text).document()
