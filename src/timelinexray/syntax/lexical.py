"""The lexical syntax backend: always available, standard library only, heuristic.

How it works
------------
1. **Masking.** Comments and string/char literals are replaced by spaces (newlines kept),
   so later patterns never match inside them and every offset still points at the same
   character of the original text. Rust and Scala block comments nest; Rust raw strings
   (``r#"..."#``), byte and C strings, Java text blocks, Scala triple-quoted and
   interpolated strings and Python prefixed/triple-quoted strings are recognised. Rust
   lifetimes and labels (``'a``) are not mistaken for char literals.
2. **Declarations.** Per-language patterns anchored at statement starts (a line start, or
   just after ``{``, ``}`` or ``;``) find declarations together with the attributes,
   annotations or decorators directly above them. A declaration inside parentheses or
   brackets is ignored; Java members and Scala ``val``/``var`` members are only accepted
   directly inside a type body, so local variables are not reported.
3. **Extents.** Rust, Java and Scala use delimiter matching: a declaration ends at the
   brace closing its body, at its terminating ``;``, or (Scala) at the end of its
   expression body, found by indentation and delimiter depth. Python uses logical lines
   and indentation.
4. **Call candidates.** Inside function, method, constructor and initializer bodies, a
   name directly followed by an argument list (Rust macros: ``name!`` followed by any
   delimiter) is a candidate. Keywords, declarations and pattern positions (Scala
   ``case X(...)``, Rust ``dyn Fn(...)``) are skipped.

Documented limits
-----------------
It does not expand macros, resolve names, types, imports, implicits or overloads, or see
calls written without parentheses (Scala infix and parameterless calls, Python decorators
used without arguments). Rust enum variants, struct fields and Java enum constants are not
reported. Unusual formatting can shift an end line; delimiter or literal problems mark the
file ``partial`` with a reason instead of failing it.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable
from dataclasses import dataclass

from . import FAILED, PARSED, PARTIAL, CallCandidate, Extraction, Symbol

NAME = "lexical"
#: Bump whenever the output for an unchanged input may change (it is part of the cache key).
VERSION = "1"

SIGNATURE_MAX = 240


# -- text helpers ------------------------------------------------------------------------


class Lines:
    """1-based line numbers of character offsets; only LF ends a line (as in span.py)."""

    __slots__ = ("starts",)

    def __init__(self, text: str) -> None:
        starts = [0]
        index = text.find("\n")
        while index != -1:
            starts.append(index + 1)
            index = text.find("\n", index + 1)
        self.starts = starts

    def line_of(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)


def blank(segment: str) -> str:
    """Spaces of the same length, newlines kept."""
    if "\n" not in segment:
        return " " * len(segment)
    return "\n".join(" " * len(part) for part in segment.split("\n"))


def normalize(segment: str, limit: int = SIGNATURE_MAX) -> str:
    """Collapse whitespace; cap the length (a cut signature ends in ' ...')."""
    text = " ".join(segment.split())
    if len(text) > limit:
        text = text[: limit - 4].rstrip() + " ..."
    return text


def last_non_space(text: str, stop: int, floor: int) -> int:
    """Offset of the last non-whitespace character in ``text[floor:stop]`` (or ``floor``)."""
    index = min(stop, len(text)) - 1
    while index > floor and text[index].isspace():
        index -= 1
    return max(index, floor)


# -- masking -----------------------------------------------------------------------------

_BLOCK_EDGE = re.compile(r"/\*|\*/")

# Unrolled-loop string bodies: [^"\\]*(?:\\.[^"\\]*)*
_RUST_TOKEN = re.compile(
    r"""
      (?P<line>//[^\n]*)
    | (?P<block>/\*)
    | (?P<raw>(?<![\w])(?:br|cr|r)(?P<hashes>\#*)")
    | (?P<str>(?:(?<![\w])[bc])?"[^"\\]*(?:\\.[^"\\]*)*")
    | (?P<ustr>")
    | (?P<char>(?<![\w])b?'(?:[^'\\\n]|\\(?:x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]{1,6}\}|[^\n]))')
    """,
    re.X | re.S,
)

_SCALA_TOKEN = re.compile(
    r"""
      (?P<line>//[^\n]*)
    | (?P<block>/\*)
    | (?P<triple>\"\"\")
    | (?P<interp>(?<=[\w])")
    | (?P<str>"[^"\\\n]*(?:\\.[^"\\\n]*)*")
    | (?P<ustr>")
    | (?P<char>'(?:[^'\\\n]|\\(?:u+[0-9a-fA-F]{4}|[0-7]{1,3}|[^\n]))')
    """,
    re.X | re.S,
)

_JAVA_TOKEN = re.compile(
    r"""
      (?P<line>//[^\n]*)
    | (?P<cblock>/\*)
    | (?P<textblock>\"\"\")
    | (?P<str>"[^"\\\n]*(?:\\.[^"\\\n]*)*")
    | (?P<ustr>")
    | (?P<char>'(?:[^'\\\n]|\\(?:u+[0-9a-fA-F]{4}|[0-7]{1,3}|[^\n]))')
    """,
    re.X | re.S,
)

_PY_PREFIX = r"(?:(?<![\w])[rRbBuUfF]{1,2})?"
_PYTHON_TOKEN = re.compile(
    rf"""
      (?P<line>\#[^\n]*)
    | (?P<triple>{_PY_PREFIX}(?:'''|\"\"\"))
    | (?P<str>{_PY_PREFIX}(?:'[^'\\\n]*(?:\\.[^'\\\n]*)*'|"[^"\\\n]*(?:\\.[^"\\\n]*)*"))
    | (?P<ustr>{_PY_PREFIX}['"])
    """,
    re.X | re.S,
)

#: The unguarded patterns, by language (the reference the guarded ones must equal).
_PLAIN_TOKENS = {
    "rust": _RUST_TOKEN,
    "scala": _SCALA_TOKEN,
    "java": _JAVA_TOKEN,
    "python": _PYTHON_TOKEN,
}

#: Every character a token of the language can start with, from its alternatives:
#: Rust ``//`` and ``/*`` (``/``), raw strings ``r``/``br``/``cr`` (``r b c``), strings
#: ``"``, ``b"``, ``c"`` (``" b c``), chars ``'`` and ``b'`` (``' b``); Scala and Java
#: ``//``, ``/*``, ``"``, ``'`` (an interpolated Scala string starts at its quote, after
#: the identifier); Python ``#`` and an optional ``r R b B u U f F`` prefix before ``'``
#: or ``"``. A position whose character is outside the set cannot start any alternative.
_TOKEN_FIRST = {
    "rust": "/\"'rbc",
    "scala": "/\"'",
    "java": "/\"'",
    "python": "#\"'rRbBuUfF",
}


def _guarded(pattern: re.Pattern[str], first: str) -> re.Pattern[str]:
    """``pattern`` behind a lookahead of its possible first characters.

    ``search`` then skips the (many) positions that cannot start a token without
    entering the alternation; the match found is the one the plain pattern finds,
    because the lookahead consumes nothing and every alternative implies it.
    """
    escaped = "".join("\\" + char if char in "\\]^-#" else char for char in first)
    return re.compile(f"(?=[{escaped}])(?:{pattern.pattern})", pattern.flags)


_TOKENS = {
    language: _guarded(_PLAIN_TOKENS[language], _TOKEN_FIRST[language])
    for language in _PLAIN_TOKENS
}

_DELIMITER = re.compile(r"[(){}\[\]]")
_OPENER = {")": "(", "]": "[", "}": "{"}


def match_delimiters(code: str) -> tuple[dict[int, int], bool]:
    """Map every opening ``( [ {`` offset to the offset of its closer.

    Mismatches are recovered from (a closer closes the nearest matching opener; stray
    closers are ignored; unclosed openers close at the end) and reported as unbalanced.
    """
    close_of: dict[int, int] = {}
    stack: list[tuple[str, int]] = []
    balanced = True
    for match in _DELIMITER.finditer(code):
        char = match.group()
        position = match.start()
        if char in "({[":
            stack.append((char, position))
            continue
        want = _OPENER[char]
        if stack and stack[-1][0] == want:
            close_of[stack.pop()[1]] = position
            continue
        balanced = False
        for index in range(len(stack) - 1, -1, -1):
            if stack[index][0] == want:
                for _, opened in stack[index:]:
                    close_of[opened] = position
                del stack[index:]
                break
    if stack:
        balanced = False
        last = max(len(code) - 1, 0)
        for _, opened in stack:
            close_of[opened] = max(last, opened)
    return close_of, balanced


COMMENT = "comment"
STRING = "string"


@dataclass(frozen=True, slots=True)
class Masked:
    """Three views of one text, all the same length as the original."""

    text: str  # the original
    code: str  # comments and literals blanked
    nocomment: str  # comments blanked, literals kept (used for signatures)
    string_spans: tuple[tuple[int, int], ...]  # [start, end) of every literal
    problems: tuple[str, ...]

    def in_string(self, offset: int) -> tuple[int, int] | None:
        spans = self.string_spans
        index = bisect.bisect_right(spans, (offset, len(self.text) + 1)) - 1
        if index >= 0 and spans[index][0] <= offset < spans[index][1]:
            return spans[index]
        return None


def _nested_block_end(text: str, start: int, problems: set[str]) -> int:
    depth = 1
    index = start + 2
    while depth:
        edge = _BLOCK_EDGE.search(text, index)
        if edge is None:
            problems.add("unterminated-comment")
            return len(text)
        depth += 1 if edge.group() == "/*" else -1
        index = edge.end()
    return index


def _find_unescaped(text: str, needle: str, start: int) -> int:
    """First occurrence of ``needle`` at or after ``start`` not preceded by an odd run of \\."""
    index = text.find(needle, start)
    while index != -1:
        slashes = 0
        back = index - 1
        while back >= start and text[back] == "\\":
            slashes += 1
            back -= 1
        if slashes % 2 == 0:
            return index
        index = text.find(needle, index + 1)
    return -1


def _interpolated_end(text: str, start: int) -> int:
    """End of a single-line Scala interpolated string at ``start``, or -1 if unterminated.

    ``${ ... }`` blocks may span lines and contain nested (interpolated) strings.
    """
    size = len(text)
    index = start + 1
    while index < size:
        char = text[index]
        if char == "\\":
            index += 2
        elif char == '"':
            return index + 1
        elif char == "\n":
            return -1
        elif char == "$" and index + 1 < size and text[index + 1] == "{":
            depth = 1
            index += 2
            while index < size and depth:
                inner = text[index]
                if inner == "{":
                    depth += 1
                elif inner == "}":
                    depth -= 1
                elif inner == '"':
                    nested = _interpolated_end(text, index)
                    if nested == -1:
                        return -1
                    index = nested
                    continue
                index += 1
            if depth:
                return -1
        else:
            index += 1
    return -1


def mask(text: str, language: str) -> Masked:
    """Blank comments and literals of ``text`` written in ``language``."""
    token = _TOKENS[language]
    code: list[str] = []
    nocomment: list[str] = []
    spans: list[tuple[int, int]] = []
    problems: set[str] = set()
    size = len(text)
    previous = 0
    position = 0
    if text.startswith("﻿"):
        code.append(" ")
        nocomment.append(" ")
        previous = position = 1
    while True:
        match = token.search(text, position)
        if match is None:
            break
        start = match.start()
        kind = match.lastgroup
        end = match.end()
        category = STRING
        if kind == "line":
            category = COMMENT
        elif kind == "block":
            category = COMMENT
            end = _nested_block_end(text, start, problems)
        elif kind == "cblock":
            category = COMMENT
            close = text.find("*/", start + 2)
            if close == -1:
                problems.add("unterminated-comment")
                end = size
            else:
                end = close + 2
        elif kind == "raw":
            closing = '"' + match.group("hashes")
            close = text.find(closing, end)
            if close == -1:
                problems.add("unterminated-string")
                end = size
            else:
                end = close + len(closing)
        elif kind == "triple":
            quote = text[end - 3 : end]
            close = _find_unescaped(text, quote, end) if language == "python" else text.find(
                quote, end
            )
            if close == -1:
                problems.add("unterminated-string")
                end = size
            else:
                end = close + 3
                if language == "scala":
                    while end < size and text[end] == '"':
                        end += 1
        elif kind == "interp":
            close = _interpolated_end(text, start)
            if close == -1:
                problems.add("unterminated-string")
                newline = text.find("\n", end)
                end = size if newline == -1 else newline
            else:
                end = close
        elif kind == "textblock":
            close = _find_unescaped(text, '"""', end)
            if close == -1:
                problems.add("unterminated-string")
                end = size
            else:
                end = close + 3
        elif kind == "ustr":
            problems.add("unterminated-string")
            if language == "rust":
                end = size
            else:
                newline = text.find("\n", end)
                end = size if newline == -1 else newline
        segment = text[start:end]
        blanked = blank(segment)
        code.append(text[previous:start])
        code.append(blanked)
        nocomment.append(text[previous:start])
        if category == COMMENT:
            nocomment.append(blanked)
        else:
            nocomment.append(segment)
            spans.append((start, end))
        previous = position = end
        if end == start:  # pragma: no cover - every alternative consumes input
            position += 1
    code.append(text[previous:])
    nocomment.append(text[previous:])
    return Masked(text, "".join(code), "".join(nocomment), tuple(spans), tuple(sorted(problems)))


# -- the backend ---------------------------------------------------------------------------


class LexicalBackend:
    """Masking plus per-language patterns; see the module documentation."""

    name = NAME
    version = VERSION
    languages = frozenset({"java", "python", "rust", "scala"})

    def probe(self) -> str | None:
        return None

    def extract(self, text: str, language: str) -> Extraction:
        extractor = _extractors().get(language)
        if extractor is None:
            raise ValueError(f"the lexical backend has no extractor for {language!r}")
        try:
            symbols, calls, problems = extractor(text)
        except RecursionError as exc:  # pragma: no cover - defensive; the scanners iterate
            return Extraction(FAILED, f"{type(exc).__name__}", (), ())
        status = PARTIAL if problems else PARSED
        return Extraction(
            status, ",".join(sorted(set(problems))) or None, tuple(symbols), tuple(calls)
        )


ExtractFn = Callable[[str], tuple[list[Symbol], list[CallCandidate], list[str]]]


def _extractors() -> dict[str, ExtractFn]:
    from ._brace import extract_java, extract_rust, extract_scala
    from ._python import extract_python

    return {
        "java": extract_java,
        "python": extract_python,
        "rust": extract_rust,
        "scala": extract_scala,
    }
