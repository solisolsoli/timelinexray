"""Lexical extraction for Python: logical lines and indentation.

A logical line joins physical lines while brackets are open or a line ends with a
backslash (after masking, so brackets and backslashes inside strings and comments do not
count). A ``def``/``class`` block ends at the last logical line indented deeper than its
header; decorators directly above a header belong to it. Module-level and class-level
``UPPER_CASE`` assignments are reported as constants.
"""

from __future__ import annotations

import re

from . import CallCandidate, Symbol
from .lexical import Lines, mask, normalize

_HEADER = re.compile(r"[ \t]*(?:(?P<async>async)\s+)?(?P<kw>def|class)\s+(?P<name>[A-Za-z_]\w*)")
_DECORATOR = re.compile(r"[ \t]*@\s*(?P<name>[A-Za-z_][\w.]*)")
_IMPORT = re.compile(r"[ \t]*(?P<kw>import|from)\s+(?=[\w.])")
_CONST = re.compile(r"[ \t]*(?P<name>_*[A-Z][A-Z0-9_]*)\s*(?::[^=\n]*)?=(?!=)")
_CALL = re.compile(r"(?<![\w])(?P<name>[A-Za-z_]\w*)\s*\(")
_OPENERS = "([{"
_CLOSERS = ")]}"
_KEYWORDS = frozenset(
    "False None True and as assert async await break case class continue def del elif else "
    "except finally for from global if import in is lambda match nonlocal not or pass raise "
    "return try while with yield".split()
)
_IDENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_SPACE = frozenset(" \t\r\n\\")


def _indent(line: str) -> int:
    stripped = line.lstrip(" \t\f")
    return len(line[: len(line) - len(stripped)].expandtabs(8))


def _header_colon(logical: str, start: int) -> int:
    """Offset (in ``logical``) of the ``:`` closing a def/class header, or -1."""
    depth = 0
    for index in range(start, len(logical)):
        char = logical[index]
        if char in _OPENERS:
            depth += 1
        elif char in _CLOSERS:
            depth -= 1
        elif char == ":" and depth <= 0:
            return index
    return -1


def _preceding(code: str, position: int) -> tuple[bool, str | None, str]:
    """``(is_method, qualifier, word)`` for the text just before a name."""
    index = position - 1
    while index >= 0 and code[index] in _SPACE:
        index -= 1
    if index >= 0 and code[index] == ".":
        parts: list[str] = []
        index -= 1
        while len(parts) < 32:
            while index >= 0 and code[index] in _SPACE:
                index -= 1
            end = index
            while index >= 0 and code[index] in _IDENT_CHARS:
                index -= 1
            ident = code[index + 1 : end + 1]
            if not ident or ident[0].isdigit():
                return True, None, ""
            parts.append(ident)
            probe = index
            while probe >= 0 and code[probe] in _SPACE:
                probe -= 1
            if probe >= 0 and code[probe] == ".":
                parts.append(".")
                index = probe - 1
            else:
                break
        return True, "".join(reversed(parts)), ""
    end = index
    while index >= 0 and code[index] in _IDENT_CHARS:
        index -= 1
    return False, None, code[index + 1 : end + 1]


def extract_python(text: str) -> tuple[list[Symbol], list[CallCandidate], list[str]]:
    masked = mask(text, "python")
    code = masked.code
    lines = Lines(text)
    problems = list(masked.problems)
    physical = code.split("\n")

    # 1. logical lines as (first, last) 0-based physical line indexes
    logical: list[tuple[int, int]] = []
    depth = 0
    first: int | None = None
    for number, line in enumerate(physical):
        if first is None:
            if not line.strip():
                continue
            first = number
        depth += sum(line.count(c) for c in _OPENERS) - sum(line.count(c) for c in _CLOSERS)
        if depth < 0:
            problems.append("unbalanced-delimiters")
            depth = 0
        if depth == 0 and not line.rstrip().endswith("\\"):
            logical.append((first, number))
            first = None
    if first is not None:
        problems.append("unbalanced-delimiters")
        logical.append((first, len(physical) - 1))

    def offset_of(line_index: int) -> int:
        return lines.starts[line_index] if line_index < len(lines.starts) else len(text)

    def line_end_offset(line_index: int) -> int:
        return offset_of(line_index) + len(physical[line_index])

    # 2. declarations
    symbols: list[Symbol] = []
    parents: list[int | None] = []
    regions: list[tuple[int, int, int]] = []  # (start offset, end offset, symbol index)
    name_positions: set[int] = set()
    # open blocks: [indent, symbol index, header last line, is_class, last seen line]
    stack: list[list[int]] = []
    ends: dict[int, int] = {}  # symbol index -> 0-based last line
    decorators: list[tuple[int, str]] = []  # (0-based line, name)
    previous_last = -1

    def close_until(indent: int) -> None:
        while stack and indent <= stack[-1][0]:
            block = stack.pop()
            ends[block[1]] = max(previous_last, block[2])

    for first_line, last_line in logical:
        line = physical[first_line]
        indent = _indent(line)
        close_until(indent)
        parent = stack[-1][1] if stack else None
        parent_is_class = bool(stack) and bool(stack[-1][3])
        container = symbols[parent].qualname if parent is not None else None
        decorator = _DECORATOR.match(line)
        header = _HEADER.match(line)
        if decorator:
            decorators.append((first_line, decorator.group("name")))
        elif header:
            kw = header.group("kw")
            name = header.group("name")
            name_offset = offset_of(first_line) + header.start("name")
            name_positions.add(name_offset)
            logical_text = "\n".join(physical[first_line : last_line + 1])
            colon = _header_colon(logical_text, header.end())
            one_liner = colon != -1 and bool(logical_text[colon + 1 :].strip())
            header_end = colon if colon != -1 else len(logical_text)
            signature_text = "\n".join(
                masked.nocomment[offset_of(i) : line_end_offset(i)]
                for i in range(first_line, last_line + 1)
            )
            leading = len(line) - len(line.lstrip(" \t\f"))
            modifiers = [f"@{d}" for _, d in decorators]
            if header.group("async"):
                modifiers.append("async")
            kind = "class" if kw == "class" else ("method" if parent_is_class else "function")
            start_line = decorators[0][0] if decorators else first_line
            index = len(symbols)
            symbols.append(
                Symbol(
                    kind=kind,
                    name=name,
                    container=container,
                    separator=".",
                    start_line=start_line + 1,
                    name_line=first_line + 1,
                    end_line=last_line + 1,  # replaced below for block headers
                    signature=normalize(signature_text[leading:header_end]),
                    modifiers=tuple(modifiers),
                )
            )
            parents.append(parent)
            if one_liner:
                ends[index] = last_line
                if kind != "class":
                    start = offset_of(first_line) + colon + 1
                    regions.append((start, line_end_offset(last_line), index))
            else:
                stack.append([indent, index, last_line, int(kind == "class")])
            decorators = []
        else:
            decorators = []
            imported = _IMPORT.match(line)
            constant = _CONST.match(line)
            if imported:
                raw = "\n".join(
                    masked.nocomment[offset_of(i) : line_end_offset(i)]
                    for i in range(first_line, last_line + 1)
                )
                statement = raw[imported.end() :]
                symbols.append(
                    Symbol(
                        kind="import",
                        name=normalize(statement.replace("\\\n", " ")),
                        container=container,
                        separator=".",
                        start_line=first_line + 1,
                        name_line=first_line + 1,
                        end_line=last_line + 1,
                        signature=normalize(raw.replace("\\\n", " ")),
                        modifiers=(),
                    )
                )
                parents.append(parent)
                ends[len(symbols) - 1] = last_line
            elif constant and (parent is None or parent_is_class):
                raw = "\n".join(
                    masked.nocomment[offset_of(i) : line_end_offset(i)]
                    for i in range(first_line, last_line + 1)
                )
                index = len(symbols)
                name_offset = offset_of(first_line) + constant.start("name")
                symbols.append(
                    Symbol(
                        kind="const",
                        name=constant.group("name"),
                        container=container,
                        separator=".",
                        start_line=first_line + 1,
                        name_line=first_line + 1,
                        end_line=last_line + 1,
                        signature=normalize(raw),
                        modifiers=(),
                    )
                )
                parents.append(parent)
                ends[index] = last_line
                regions.append(
                    (offset_of(first_line) + constant.end(), line_end_offset(last_line), index)
                )
                name_positions.add(name_offset)
        previous_last = last_line
    close_until(-1)

    # 3. block ends and function body regions
    final: list[Symbol] = []
    for index, symbol in enumerate(symbols):
        last = ends.get(index, symbol.end_line - 1)
        if last + 1 != symbol.end_line:
            symbol = Symbol(
                symbol.kind, symbol.name, symbol.container, symbol.separator,
                symbol.start_line, symbol.name_line, last + 1, symbol.signature,
                symbol.modifiers,
            )
        final.append(symbol)
    with_region = {region[2] for region in regions}
    for index, symbol in enumerate(final):
        if symbol.kind in ("function", "method") and index not in with_region:
            header_last = _header_last_line(symbol, physical)
            if header_last + 1 < symbol.end_line:
                regions.append(
                    (offset_of(header_last + 1), line_end_offset(symbol.end_line - 1), index)
                )

    # 4. call candidates
    calls: list[CallCandidate] = []
    if regions:
        regions.sort()
        active: list[tuple[int, int, int]] = []
        cursor = 0
        for match in _CALL.finditer(code):
            position = match.start("name")
            if position in name_positions:
                continue
            while cursor < len(regions) and regions[cursor][0] <= position:
                region = regions[cursor]
                cursor += 1
                while active and active[-1][1] < region[0]:
                    active.pop()
                active.append(region)
            while active and active[-1][1] < position:
                active.pop()
            if not active:
                continue
            name = match.group("name")
            if name in _KEYWORDS:
                continue
            is_method, qualifier, word = _preceding(code, position)
            if not is_method and word in ("def", "class", "case"):
                continue
            calls.append(
                CallCandidate(
                    caller=active[-1][2],
                    callee=name,
                    qualifier=qualifier,
                    form="method" if is_method else "call",
                    line=lines.line_of(position),
                )
            )
    return final, calls, problems


def _header_last_line(symbol: Symbol, physical: list[str]) -> int:
    """0-based last physical line of a def header (its logical line may span lines)."""
    depth = 0
    for number in range(symbol.name_line - 1, len(physical)):
        line = physical[number]
        depth += sum(line.count(c) for c in _OPENERS) - sum(line.count(c) for c in _CLOSERS)
        if depth <= 0 and not line.rstrip().endswith("\\"):
            return number
    return symbol.name_line - 1
