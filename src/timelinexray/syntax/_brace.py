"""Lexical extraction for the brace languages: Rust, Java and Scala.

See :mod:`timelinexray.syntax.lexical` for the approach and its limits. Offsets are
character offsets into the decoded text; the masked views have the same length.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from . import CALLER_KINDS, CallCandidate, Symbol
from .lexical import Lines, Masked, last_non_space, mask, match_delimiters, normalize

_STOP = re.compile(r"[(){}\[\];=\n]")
_EXPR_STOP = re.compile(r"[(){}\[\];\n]")
_IDENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")
_SPACE = frozenset(" \t\r\n")


@dataclass(eq=False, slots=True)
class Decl:
    kind: str
    name: str
    start: int  # first character, attributes/annotations included
    kw_pos: int  # first modifier or keyword
    name_pos: int
    name_end: int
    modifiers: tuple[str, ...] = ()
    keyword: str = ""
    end: int = -1  # offset of the last character
    sig_end: int = -1  # exclusive end of the signature text
    body: tuple[int, int] | None = None  # (open, close) of a brace body
    region: tuple[int, int] | None = None  # where call candidates belong to this declaration
    parent: "Decl | None" = None
    index: int = -1
    call_like: bool = False  # a Java ``Name(`` candidate: a call unless it is a constructor

    def qualname(self, separator: str) -> str:
        parts = []
        node: Decl | None = self
        while node is not None:
            parts.append(node.name)
            node = node.parent
        return separator.join(reversed(parts))


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    language: str
    separator: str
    find: Callable[["Engine"], list[Decl]]
    accept: Callable[["Engine", Decl, str, "Decl | None"], bool]
    extent: Callable[["Engine", Decl], None]
    call_pattern: re.Pattern[str]
    keywords: frozenset[str]  # never callee names
    skip_after: frozenset[str]  # a name preceded by one of these words is not a call
    new_word: str | None  # "new" for Java and Scala


class Engine:
    def __init__(self, text: str, spec: LanguageSpec) -> None:
        self.spec = spec
        self.masked: Masked = mask(text, spec.language)
        self.text = text
        self.code = self.masked.code
        self.lines = Lines(text)
        self.close_of, balanced = match_delimiters(self.code)
        self.problems = list(self.masked.problems)
        if not balanced:
            self.problems.append("unbalanced-delimiters")
        self.all_name_positions: set[int] = set()
        self.skip_regions: list[tuple[int, int]] = []

    # -- small helpers -------------------------------------------------------------------

    def indent_of(self, offset: int) -> int:
        start = self.code.rfind("\n", 0, offset) + 1
        index = start
        while index < len(self.code) and self.code[index] in " \t":
            index += 1
        return index - start

    def next_code_line(self, newline: int) -> tuple[int, str] | None:
        """Indentation and stripped text of the first non-blank line after ``newline``."""
        code = self.code
        start = newline + 1
        size = len(code)
        while start < size:
            end = code.find("\n", start)
            if end == -1:
                end = size
            line = code[start:end]
            stripped = line.strip()
            if stripped:
                return len(line) - len(line.lstrip(" \t")), stripped
            start = end + 1
        return None

    def is_assign(self, offset: int) -> bool:
        code = self.code
        after = code[offset + 1] if offset + 1 < len(code) else ""
        before = code[offset - 1] if offset else ""
        return after not in ("=", ">") and before not in ("=", "!", "<", ">")

    _CONTINUED_LINE = re.compile(r"(?:[(,=.:\[]|=>|\bwith|\bextends|\bnew)$")
    _CONTINUING_LINE = re.compile(r"(?:extends\b|with\b|derives\b|[{.=:])")

    def continues(self, newline: int) -> bool:
        """Scala: does the statement go on after this newline (at delimiter depth 0)?"""
        start = self.code.rfind("\n", 0, newline) + 1
        current = self.code[start:newline].rstrip()
        if self._CONTINUED_LINE.search(current):
            return True
        following = self.next_code_line(newline)
        return following is not None and bool(self._CONTINUING_LINE.match(following[1]))

    def scan(
        self, offset: int, *, body_ok: bool, eq_ok: bool = False, newline_end: bool = False
    ) -> tuple[str, int]:
        """Walk a declaration header from ``offset`` at delimiter depth 0.

        Returns ``(what, position)`` where ``what`` is ``body`` (an opening brace),
        ``term`` (a ``;``), ``eq`` (Scala ``=``), ``newline`` (Scala statement end),
        ``closed`` (an enclosing closer) or ``eof``.
        """
        code = self.code
        size = len(code)
        close_of = self.close_of
        index = offset
        while True:
            found = _STOP.search(code, index)
            if found is None:
                return "eof", size
            position = found.start()
            char = code[position]
            if char in "([":
                index = close_of.get(position, size - 1) + 1
            elif char == "{":
                if body_ok:
                    return "body", position
                index = close_of.get(position, size - 1) + 1
            elif char in ")]}":
                return "closed", position
            elif char == ";":
                return "term", position
            elif char == "=":
                if eq_ok and self.is_assign(position):
                    return "eq", position
                index = position + 1
            else:  # newline
                inside = self.masked.in_string(position)
                if inside is not None:
                    index = inside[1]
                elif newline_end and not self.continues(position):
                    return "newline", position
                else:
                    index = position + 1

    def expression(self, equals: int, indent: int) -> tuple[str, int]:
        """Scala: the body after ``=`` - a brace block, or an expression ending by indentation."""
        text = self.text
        size = len(text)
        first = equals + 1
        while first < size and text[first] in _SPACE:
            first += 1
        if first < size and self.code[first] == "{":
            return "body", first
        code = self.code
        close_of = self.close_of
        index = equals + 1
        while True:
            found = _EXPR_STOP.search(code, index)
            if found is None:
                return "expr", size
            position = found.start()
            char = code[position]
            if char in "([{":
                index = close_of.get(position, size - 1) + 1
            elif char in ")]};":
                return "expr", position
            else:  # newline
                inside = self.masked.in_string(position)
                if inside is not None:
                    index = inside[1]
                    continue
                following = self.next_code_line(position)
                if following is None:
                    return "expr", position
                if following[0] > indent or position < first:
                    index = position + 1
                    continue
                return "expr", position

    def finish(self, decl: Decl, what: str, position: int) -> None:
        """Set ``end``, ``sig_end`` and ``body`` from a :meth:`scan` result."""
        if what == "body":
            close = self.close_of.get(position, len(self.code) - 1)
            decl.body = (position, close)
            decl.end = close
            decl.sig_end = position
        elif what == "term":
            decl.end = position
            decl.sig_end = position
        else:
            decl.end = last_non_space(self.masked.nocomment, position, decl.name_end - 1)
            decl.sig_end = decl.end + 1

    # -- the passes -----------------------------------------------------------------------

    def run(self) -> tuple[list[Symbol], list[CallCandidate], list[str]]:
        spec = self.spec
        candidates = sorted(spec.find(self), key=lambda d: (d.start, d.name_pos))
        self.all_name_positions.update(d.name_pos for d in candidates if not d.call_like)
        pairs = sorted(self.close_of.items())
        stack: list[tuple[int, int]] = []
        owners: dict[int, Decl] = {}
        kept: list[Decl] = []
        expressions: list[Decl] = []  # kept declarations whose region is not a brace body
        cursor = 0
        for decl in candidates:
            position = decl.start
            while cursor < len(pairs) and pairs[cursor][0] < position:
                opened = pairs[cursor]
                cursor += 1
                while stack and stack[-1][1] < opened[0]:
                    stack.pop()
                stack.append(opened)
            while stack and stack[-1][1] < position:
                stack.pop()
            if stack and self.code[stack[-1][0]] != "{":
                continue  # inside parentheses or brackets: a parameter, not a declaration
            direct = owners.get(stack[-1][0]) if stack else None
            scope = "top" if not stack else ("block" if direct is None else direct.kind)
            parent = None
            parent_start = -1
            for opened, _ in reversed(stack):
                if opened in owners:
                    parent, parent_start = owners[opened], opened
                    break
            while expressions and expressions[-1].region[1] < position:  # type: ignore[index]
                expressions.pop()
            if expressions and expressions[-1].region[0] > parent_start:  # type: ignore[index]
                parent, direct, scope = expressions[-1], None, "block"
            decl.parent = parent
            if not spec.accept(self, decl, scope, direct):
                continue
            spec.extent(self, decl)
            if decl.body is not None:
                owners[decl.body[0]] = decl
            elif decl.region is not None:
                expressions.append(decl)
            decl.index = len(kept)
            kept.append(decl)
            self.all_name_positions.add(decl.name_pos)
        symbols = [self.symbol(decl) for decl in kept]
        calls = self.calls(kept)
        return symbols, calls, self.problems

    def symbol(self, decl: Decl) -> Symbol:
        lines = self.lines
        container = decl.parent.qualname(self.spec.separator) if decl.parent else None
        signature = normalize(self.masked.nocomment[decl.kw_pos : max(decl.sig_end, decl.kw_pos)])
        return Symbol(
            kind=decl.kind,
            name=decl.name,
            container=container,
            separator=self.spec.separator,
            start_line=lines.line_of(decl.start),
            name_line=lines.line_of(decl.name_pos),
            end_line=max(lines.line_of(decl.end), lines.line_of(decl.name_pos)),
            signature=signature,
            modifiers=decl.modifiers,
        )

    # -- call candidates ---------------------------------------------------------------------

    def preceding(self, position: int) -> tuple[str | None, str | None, str]:
        """``(separator, qualifier, word)`` just before a name at ``position``."""
        code = self.code
        index = position - 1
        while index >= 0 and code[index] in _SPACE:
            index -= 1
        separator = None
        if index >= 0 and code[index] == ".":
            separator = "."
            index -= 1
        elif index >= 1 and code[index] == ":" and code[index - 1] == ":":
            separator = "::"
            index -= 2
        if separator is None:
            end = index
            while index >= 0 and code[index] in _IDENT_CHARS:
                index -= 1
            return None, None, code[index + 1 : end + 1]
        parts: list[str] = []
        while len(parts) < 32:
            while index >= 0 and code[index] in _SPACE:
                index -= 1
            end = index
            while index >= 0 and code[index] in _IDENT_CHARS:
                index -= 1
            ident = code[index + 1 : end + 1]
            if not ident or ident[0].isdigit():
                return separator, None, ""
            parts.append(ident)
            probe = index
            while probe >= 0 and code[probe] in _SPACE:
                probe -= 1
            if probe >= 0 and code[probe] == ".":
                parts.append(".")
                index = probe - 1
            elif probe >= 1 and code[probe] == ":" and code[probe - 1] == ":":
                parts.append("::")
                index = probe - 2
            else:
                break
        return separator, "".join(reversed(parts)), ""

    def calls(self, kept: list[Decl]) -> list[CallCandidate]:
        spec = self.spec
        regions = sorted(
            (decl.region[0], decl.region[1], decl.index)
            for decl in kept
            if decl.kind in CALLER_KINDS and decl.region is not None
        )
        if not regions:
            return []
        has_bang = "bang" in spec.call_pattern.groupindex
        skip = sorted(self.skip_regions)
        skip_cursor = 0
        results: list[CallCandidate] = []
        stack: list[tuple[int, int, int]] = []
        cursor = 0
        line_of = self.lines.line_of
        for match in spec.call_pattern.finditer(self.code):
            position = match.start("name")
            if position in self.all_name_positions:
                continue
            while cursor < len(regions) and regions[cursor][0] < position:
                region = regions[cursor]
                cursor += 1
                while stack and stack[-1][1] < region[0]:
                    stack.pop()
                stack.append(region)
            while stack and stack[-1][1] < position:
                stack.pop()
            if not stack:
                continue
            while skip_cursor < len(skip) and skip[skip_cursor][1] < position:
                skip_cursor += 1
            if skip_cursor < len(skip) and skip[skip_cursor][0] <= position:
                continue
            name = match.group("name")
            macro = has_bang and match.group("bang")
            if not macro and name in spec.keywords:
                continue
            if position and self.code[position - 1] == "@":
                continue  # an annotation
            separator, qualifier, word = self.preceding(position)
            if macro:
                form, callee = "macro", name + "!"
            elif separator == ".":
                form, callee = "method", name
            elif separator == "::":
                form, callee = "path", name
            elif spec.new_word is not None and word == spec.new_word:
                form, callee = "new", name
            elif word in spec.skip_after:
                continue
            else:
                form, callee = "call", name
            results.append(
                CallCandidate(
                    caller=stack[-1][2],
                    callee=callee,
                    qualifier=qualifier,
                    form=form,
                    line=line_of(position),
                )
            )
        return results


# -- shared pattern pieces ------------------------------------------------------------------

_STATEMENT = r"(?:^|(?<=[{};]))[ \t]*"


def _words(text: str) -> tuple[str, ...]:
    return tuple(text.split())


# -- Rust ------------------------------------------------------------------------------------

_RUST_DECL = re.compile(
    _STATEMENT
    + r"""
    (?P<attrs>(?:\#!?\[[^\[\]]*(?:\[[^\[\]]*\][^\[\]]*)*\]\s*)*)
    (?P<vis>pub(?:\s*\([^()]*\))?\s+)?
    (?P<quals>(?:(?:default|const|async|unsafe|extern)\s+)*)
    (?:
        (?P<kw>fn|struct|enum|union|trait|mod|type|const|static|use)(?=\s)
      | (?P<impl>impl)(?=[\s<])
      | (?P<crate>extern\s+crate)(?=\s)
      | (?P<macro>macro_rules!)
      | (?P<param>param)!\s*\(
    )
    """,
    re.M | re.X,
)
_RUST_NAME = re.compile(r"\s*(?:r\#)?([A-Za-z_][\w]*)")
_RUST_STATIC_NAME = re.compile(r"\s*(?:mut\s+)?(?:r\#)?([A-Za-z_][\w]*)")
_RUST_ATTRIBUTE = re.compile(r"\#!?\[")
_RUST_KIND = {"fn": "function", "mod": "module", "use": "import"}
_RUST_BODY_KINDS = frozenset({"function", "struct", "enum", "union", "trait", "impl", "module", "macro"})
_RUST_WORD = re.compile(r"'?[A-Za-z_]\w*")
_RUST_PATH_SEGMENT = re.compile(r"\s*::\s*([A-Za-z_]\w*)")
_RUST_TYPE_SKIP = frozenset({"mut", "dyn", "const", "unsafe", "for", "impl", "crate", "self", "super"})


def _skip_angles(text: str, index: int, size: int) -> int:
    depth = 0
    while index < size:
        char = text[index]
        if char == "<":
            depth += 1
        elif char == ">" and text[index - 1] != "-":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return size


def _rust_impl_target(code: str, start: int, end: int) -> tuple[str, int] | None:
    """Name and offset of the implementing type in an ``impl`` header ``code[start:end]``."""
    index = start
    while index < end and code[index] in _SPACE:
        index += 1
    if index < end and code[index] == "<":
        index = _skip_angles(code, index, end)
    header = code[index:end]
    depth = 0
    words: list[tuple[str, int]] = []
    position = 0
    while position < len(header):
        char = header[position]
        if char == "<":
            depth += 1
        elif char == ">" and position and header[position - 1] != "-":
            depth -= 1
        elif depth == 0:
            word = _RUST_WORD.match(header, position)
            if word:
                words.append((word.group(), index + position))
                position = word.end()
                continue
        position += 1
    names = words
    for number, (word, _) in enumerate(names):
        if word == "where":
            names = names[:number]
            break
    for number, (word, _) in enumerate(names):
        if word == "for":
            names = names[number + 1 :]
            break
    candidates = [(w, p) for w, p in names if not w.startswith("'") and w not in _RUST_TYPE_SKIP]
    if not candidates:
        return None
    # the last segment of the first path: crate::a::Foo<T> -> Foo
    first_word, first_pos = candidates[0]
    name, name_pos = first_word, first_pos
    probe = first_pos + len(first_word)
    while True:
        following = _RUST_PATH_SEGMENT.match(code, probe, end)
        if not following:
            break
        name, name_pos = following.group(1), following.start(1)
        probe = following.end()
    return name, name_pos


def _rust_find(engine: Engine) -> list[Decl]:
    code = engine.code
    found: list[Decl] = []
    for attribute in _RUST_ATTRIBUTE.finditer(code):
        bracket = attribute.end() - 1
        engine.skip_regions.append((attribute.start(), engine.close_of.get(bracket, bracket)))
    for match in _RUST_DECL.finditer(code):
        start = match.start("attrs")
        kw_pos = match.start("vis") if match.group("vis") else match.start("quals")
        modifiers: list[str] = []
        if match.group("vis"):
            modifiers.append(" ".join(match.group("vis").split()))
        modifiers.extend(_words(match.group("quals")))
        end = match.end()
        if match.group("impl"):
            kind = "impl"
            header_end = engine.scan(end, body_ok=True)[1]
            target = _rust_impl_target(code, end, header_end)
            if target is None:
                continue
            name, name_pos = target
            name_end = end
            keyword = "impl"
        elif match.group("crate"):
            name_match = _RUST_NAME.match(code, end)
            if not name_match:
                continue
            kind, keyword = "import", "extern crate"
            name, name_pos, name_end = name_match.group(1), name_match.start(1), name_match.end(1)
        elif match.group("macro"):
            name_match = _RUST_NAME.match(code, end)
            if not name_match:
                continue
            kind, keyword = "macro", "macro_rules!"
            name, name_pos, name_end = name_match.group(1), name_match.start(1), name_match.end(1)
        elif match.group("param"):
            name_match = _RUST_NAME.match(code, end)
            if not name_match:
                continue
            kind, keyword = "param", "param!"
            kw_pos = match.start("param")
            name, name_pos, name_end = name_match.group(1), name_match.start(1), name_match.end(1)
        else:
            keyword = match.group("kw")
            kind = _RUST_KIND.get(keyword, keyword)
            if keyword == "use":
                name_pos = end + len(code[end : end + 200]) - len(code[end : end + 200].lstrip())
                name, name_end = "", name_pos
            else:
                pattern = _RUST_STATIC_NAME if keyword == "static" else _RUST_NAME
                name_match = pattern.match(code, end)
                if not name_match:
                    continue
                name, name_pos = name_match.group(1), name_match.start(1)
                name_end = name_match.end(1)
                if keyword == "static" and re.match(r"\s*mut\b", code[end : name_pos]):
                    modifiers.append("mut")
        found.append(
            Decl(kind, name, start, kw_pos, name_pos, name_end, tuple(modifiers), keyword)
        )
    return found


def _rust_accept(engine: Engine, decl: Decl, scope: str, direct: Decl | None) -> bool:
    if decl.kind == "function" and direct is not None and direct.kind in ("impl", "trait"):
        decl.kind = "method"
    return True


def _rust_extent(engine: Engine, decl: Decl) -> None:
    if decl.kind in _RUST_BODY_KINDS or decl.kind == "method":
        what, position = engine.scan(decl.name_end, body_ok=True)
        engine.finish(decl, what, position)
        if decl.kind in ("function", "method") and decl.body is not None:
            decl.region = decl.body
        return
    start = decl.name_end
    if decl.keyword == "param!":
        start = engine.code.find("(", decl.kw_pos)  # scan the whole macro argument list
    what, position = engine.scan(start, body_ok=False)
    engine.finish(decl, what, position)
    if decl.keyword == "use":
        decl.name = normalize(engine.masked.nocomment[decl.name_pos : position if what == "term" else decl.end + 1])
    if decl.kind in ("const", "static"):
        decl.region = (decl.name_end, decl.end)


_RUST_KEYWORDS = frozenset(
    "as async await break const continue crate dyn else enum extern false fn for if impl in "
    "let loop match mod move mut pub ref return self Self static struct super trait true type "
    "unsafe use where while".split()
)

RUST = LanguageSpec(
    language="rust",
    separator="::",
    find=_rust_find,
    accept=_rust_accept,
    extent=_rust_extent,
    call_pattern=re.compile(
        r"(?<![\w$])(?P<name>[A-Za-z_]\w*)(?:(?P<bang>!)\s*[(\[{]|\s*(?:::\s*<[^<>;{}]*(?:<[^<>;{}]*>[^<>;{}]*)*>\s*)?\()"
    ),
    keywords=_RUST_KEYWORDS,
    skip_after=frozenset({"fn", "dyn", "impl", "struct", "enum", "union", "trait", "type"}),
    new_word=None,
)


# -- Java ------------------------------------------------------------------------------------

_JAVA_MODIFIERS = (
    "public|protected|private|static|final|abstract|synchronized|native|default|strictfp|"
    "transient|volatile|sealed|non-sealed"
)
_JAVA_GENERIC = r"<[^;{}()=]*>"
_JAVA_DECL = re.compile(
    _STATEMENT
    + r"""
    (?P<annos>(?:@(?!interface\b)[A-Za-z_$][\w$.]*\s*(?:\((?:[^()]|\([^()]*\))*\))?\s*)*)
    (?P<mods>(?:(?:"""
    + _JAVA_MODIFIERS
    + r""")\s+)*)
    (?:
        (?P<tkw>class|interface|enum|record|@interface)\s+(?P<tname>[A-Za-z_$][\w$]*)
      | (?P<pkw>package|import)\s+(?P<pstatic>static\s+)?(?P<pname>[\w$.]+(?:\.\*)?)\s*;
      | (?:(?P<tparams>"""
    + _JAVA_GENERIC
    + r""")\s*)?
        (?P<rtype>[A-Za-z_$][\w$]*(?:\s*"""
    + _JAVA_GENERIC
    + r""")?(?:\s*\.\s*[A-Za-z_$][\w$]*(?:\s*"""
    + _JAVA_GENERIC
    + r""")?)*(?:\s*\[\s*\])*)
        \s+(?P<mname>[A-Za-z_$][\w$]*)
      | (?P<cname>[A-Za-z_$][\w$]*)(?=\s*\()
    )
    """,
    re.M | re.X,
)
_JAVA_KEYWORDS = frozenset(
    "abstract assert boolean break byte case catch char class const continue default do double "
    "else enum extends final finally float for goto if implements import instanceof int "
    "interface long native new package private protected public return short static strictfp "
    "super switch synchronized this throw throws transient try void volatile while var yield "
    "record sealed permits".split()
) - {"boolean", "byte", "char", "double", "float", "int", "long", "short", "void", "var"}
_JAVA_TYPE_KINDS = frozenset({"class", "interface", "enum", "record", "annotation"})
_JAVA_TKW = {"@interface": "annotation"}


def _java_find(engine: Engine) -> list[Decl]:
    code = engine.code
    found: list[Decl] = []
    for match in _JAVA_DECL.finditer(code):
        annos = match.group("annos")
        start = match.start("annos")
        kw_pos = match.start("mods")
        modifiers = [f"@{a}" for a in re.findall(r"@([A-Za-z_$][\w$.]*)", annos)] if annos else []
        modifiers.extend(_words(match.group("mods")))
        if match.group("tkw"):
            keyword = match.group("tkw")
            kind = _JAVA_TKW.get(keyword, keyword)
            name, name_pos, name_end = match.group("tname"), match.start("tname"), match.end("tname")
        elif match.group("pkw"):
            keyword = match.group("pkw")
            kind = "module" if keyword == "package" else "import"
            if match.group("pstatic"):
                modifiers.append("static")
            name, name_pos, name_end = match.group("pname"), match.start("pname"), match.end("pname")
        elif match.group("mname"):
            rtype = match.group("rtype")
            name = match.group("mname")
            if rtype in _JAVA_KEYWORDS or name in _JAVA_KEYWORDS:
                continue
            name_pos, name_end = match.start("mname"), match.end("mname")
            following = engine.code[name_end : name_end + 200].lstrip()[:1]
            if following == "(":
                kind, keyword = "method", "method"
            elif following in ("=", ";", ",", "["):
                kind, keyword = "field", "field"
            else:
                continue
        else:
            name = match.group("cname")
            if name in _JAVA_KEYWORDS:
                continue
            kind, keyword = "constructor", "constructor"
            name_pos, name_end = match.start("cname"), match.end("cname")
        decl = Decl(kind, name, start, kw_pos, name_pos, name_end, tuple(modifiers), keyword)
        decl.call_like = keyword == "constructor"
        found.append(decl)
    return found


def _java_accept(engine: Engine, decl: Decl, scope: str, direct: Decl | None) -> bool:
    if decl.kind in _JAVA_TYPE_KINDS:
        return True
    if decl.kind in ("module", "import"):
        return scope == "top"
    if direct is None or direct.kind not in _JAVA_TYPE_KINDS:
        return False
    if decl.kind == "constructor":
        return decl.name == direct.name
    if decl.kind == "field":
        mods = set(decl.modifiers)
        if direct.kind in ("interface", "annotation") or {"static", "final"} <= mods:
            decl.kind = "const"
    return True


def _java_extent(engine: Engine, decl: Decl) -> None:
    if decl.kind in ("module", "import"):
        what, position = engine.scan(decl.name_end, body_ok=False)
        engine.finish(decl, what, position)
        return
    if decl.kind in _JAVA_TYPE_KINDS or decl.kind in ("method", "constructor"):
        what, position = engine.scan(decl.name_end, body_ok=True)
        engine.finish(decl, what, position)
        if decl.kind in ("method", "constructor") and decl.body is not None:
            decl.region = decl.body
        return
    what, position = engine.scan(decl.name_end, body_ok=False)
    engine.finish(decl, what, position)
    decl.region = (decl.name_end, decl.end)


JAVA = LanguageSpec(
    language="java",
    separator=".",
    find=_java_find,
    accept=_java_accept,
    extent=_java_extent,
    call_pattern=re.compile(
        r"(?<![\w$])(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^<>;{}()]*(?:<[^<>;{}()]*(?:<[^<>;{}()]*>[^<>;{}()]*)*>[^<>;{}()]*)*>\s*)?\("
    ),
    keywords=_JAVA_KEYWORDS | {"this", "super"},
    skip_after=frozenset(),
    new_word="new",
)


# -- Scala -----------------------------------------------------------------------------------

_SCALA_NAME = r"`[^`\n]+`|[A-Za-z_$][\w$]*(?:_[!#%&*+\-/:<=>?@\\^|~]+)?|[!#%&*+\-/:<=>?@\\^|~]+"
_SCALA_DECL = re.compile(
    _STATEMENT
    + r"""
    (?P<annos>(?:@[A-Za-z_][\w.]*(?:\[[^\]\n]*\])?(?:\((?:[^()]|\([^()]*\))*\))*\s+)*)
    (?P<mods>(?:(?:(?:private|protected)(?:\[[\w.]*\])?|override|final|sealed|abstract|implicit|lazy|case|inline|opaque|transparent|open)\s+)*)
    (?:
        (?P<pobj>package\s+object)\s+(?P<poname>[A-Za-z_$][\w$]*)
      | (?P<kw>class|trait|object|def|val|var|type)\s+(?P<name>"""
    + _SCALA_NAME
    + r""")
      | (?P<pkw>package|import)\s+(?=[\w`_{])
    )
    """,
    re.M | re.X,
)
_SCALA_KIND = {"def": "function", "val": "field", "var": "field", "import": "import", "package": "module"}
_SCALA_TYPE_KINDS = frozenset({"class", "trait", "object"})
_SCALA_KEYWORDS = frozenset(
    "abstract case catch class def do else extends false final finally for forSome if "
    "implicit import lazy match new null object override package private protected return "
    "sealed super this throw trait try true type val var while with yield".split()
)


def _scala_find(engine: Engine) -> list[Decl]:
    code = engine.code
    found: list[Decl] = []
    for match in _SCALA_DECL.finditer(code):
        annos = match.group("annos")
        start = match.start("annos")
        kw_pos = match.start("mods")
        modifiers = [f"@{a}" for a in re.findall(r"@([A-Za-z_][\w.]*)", annos)] if annos else []
        modifiers.extend(_words(match.group("mods")))
        if match.group("pobj"):
            kind, keyword = "object", "package object"
            modifiers.append("package")
            name, name_pos, name_end = match.group("poname"), match.start("poname"), match.end("poname")
        elif match.group("kw"):
            keyword = match.group("kw")
            kind = _SCALA_KIND.get(keyword, keyword)
            name, name_pos, name_end = match.group("name"), match.start("name"), match.end("name")
            if keyword in ("val", "var"):
                modifiers.append(keyword)
            if keyword == "def" and name == "this":
                kind = "constructor"
        else:
            keyword = match.group("pkw")
            kind = _SCALA_KIND[keyword]
            name, name_pos, name_end = "", match.end(), match.end()
        found.append(Decl(kind, name, start, kw_pos, name_pos, name_end, tuple(modifiers), keyword))
    return found


def _scala_accept(engine: Engine, decl: Decl, scope: str, direct: Decl | None) -> bool:
    template = scope == "top" or (direct is not None and direct.kind in _SCALA_TYPE_KINDS)
    if decl.kind == "module":
        return scope == "top"
    if decl.kind in ("field", "type"):
        return template
    if decl.kind == "function" and direct is not None and direct.kind in _SCALA_TYPE_KINDS:
        decl.kind = "method"
    return True


def _scala_extent(engine: Engine, decl: Decl) -> None:
    indent = engine.indent_of(decl.kw_pos)
    if decl.kind in ("import", "module"):
        what, position = engine.scan(decl.name_end, body_ok=False, newline_end=True)
        engine.finish(decl, what, position)
        decl.name = normalize(engine.masked.nocomment[decl.name_pos : decl.end + (what != "term")])
        return
    if decl.kind in _SCALA_TYPE_KINDS:
        what, position = engine.scan(decl.name_end, body_ok=True, newline_end=True)
        engine.finish(decl, what, position)
        return
    what, position = engine.scan(decl.name_end, body_ok=True, eq_ok=True, newline_end=True)
    if what != "eq":
        engine.finish(decl, what, position)
        if decl.body is not None:
            decl.region = decl.body
        return
    equals = position
    what, position = engine.expression(equals, indent)
    if what == "body":
        engine.finish(decl, "body", position)
        decl.region = decl.body
        decl.sig_end = equals
    else:
        decl.end = last_non_space(engine.masked.nocomment, position, equals)
        decl.region = (equals, decl.end)
        decl.sig_end = equals
    if decl.kind in ("field", "type") and decl.end - decl.kw_pos < 240:
        decl.sig_end = decl.end + 1


SCALA = LanguageSpec(
    language="scala",
    separator=".",
    find=_scala_find,
    accept=_scala_accept,
    extent=_scala_extent,
    call_pattern=re.compile(
        r"(?<![\w$])(?P<name>[A-Za-z_$][\w$]*)\s*(?:\[[^\[\];{}()]*(?:\[[^\[\];{}()]*\][^\[\];{}()]*)*\]\s*)?\("
    ),
    keywords=_SCALA_KEYWORDS,
    skip_after=frozenset({"def", "class", "object", "trait", "val", "var", "case", "type", "extends", "with"}),
    new_word="new",
)


def extract_rust(text: str) -> tuple[list[Symbol], list[CallCandidate], list[str]]:
    return Engine(text, RUST).run()


def extract_java(text: str) -> tuple[list[Symbol], list[CallCandidate], list[str]]:
    return Engine(text, JAVA).run()


def extract_scala(text: str) -> tuple[list[Symbol], list[CallCandidate], list[str]]:
    return Engine(text, SCALA).run()
