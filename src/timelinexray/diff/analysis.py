"""What the classifier reads from one blob: lines, masked text, symbols, declarations.

A :class:`BlobAnalysis` depends only on the blob bytes, its language and the syntax
backend, so it is computed once per blob and shared by every diff that touches the blob.
A :class:`BlobView` adds the commit and path, which every citation needs.

Masking and delimiter matching come from :mod:`timelinexray.syntax.source` (the Milestone
2 machinery: comments and literals blanked with offsets preserved, brackets matched on the
masked text), so a comma, bracket or ``=`` inside a string or comment is never read as
code.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property

from ..span import LineMap, SpanRead, read_span
from ..syntax.source import Lines, Masked, mask, match_delimiters
from . import content
from .hunks import split_lines
from .model import Citation, SPAN
from .rules import NATIVE_LANGUAGES, is_key_value_config
from .symbols import FileSymbols, SymbolInfo

VALUE_KINDS = frozenset({"param", "const", "static", "field"})
PARAM_MACRO = "param!"
CONFIG_KEY = "config-key"

#: Symbol kinds that can enclose a changed line for attribution (imports never do).
#: A Rust ``#[cfg(test)]`` attribute: the item is compiled only for tests. Not
#: ``cfg(any(test, ...))`` or ``cfg(not(test))``, which also compile outside tests.
_CFG_TEST = re.compile(rb"#\[\s*cfg\s*\(\s*test\s*\)\s*\]")
_ATTRIBUTION_SKIP = frozenset({"import"})

_TOKEN = re.compile(r"[A-Za-z0-9_$]+|\S")
_NUMBER = re.compile(r"(?<![A-Za-z_$])[0-9][0-9_]*(?:\.[0-9_]+)?(?:[eE][+-]?[0-9]+)?[A-Za-z0-9_]*")
_WORD = re.compile(r"[A-Za-z_$][\w$]*")
_LITERAL_WORDS = frozenset(
    {
        "true", "false", "True", "False", "None", "null", "nil", "Some", "Duration",
        "second", "seconds", "millis", "milliseconds", "minute", "minutes", "hour", "hours",
        "day", "days",
    }
)

# -- registration lists --------------------------------------------------------------------

_LIST_OPENERS = {
    "rust": re.compile(r"\bvec!\s*([\[(])|(?:=|=>|\breturn|\(|,)\s*&?\s*(\[)"),
    "scala": re.compile(
        r"\b(?:Seq|List|Vector|Set|IndexedSeq|Array)\s*(?:\[[^\]\n]*\])?\s*(\()"
    ),
    "java": re.compile(
        r"\b(?:List\.of|Set\.of|Arrays\.asList|ImmutableList\.of|ImmutableSet\.of|Stream\.of|"
        r"Lists\.newArrayList)\s*(?:<[^>\n]*>)?\s*(\()"
    ),
    "python": re.compile(r"(?:=|\breturn|\(|,)\s*(\[)"),
}
_WRAPPER = re.compile(r"(?:(?:Box|Arc|Rc)\s*::\s*(?:new|pin|from)\s*\(|Some\s*\(|new\s+|&\s*|mut\s+)")
_ENTRY_HEAD = re.compile(r"[A-Za-z_]\w*(?:\s*(?:::|\.)\s*[A-Za-z_]\w*)*")
_COMPONENT = re.compile(
    r"^[A-Z]\w*(Filter|Source|Hydrator|Scorer|Selector|SideEffect|Rule|Stage|Ranker|Mixer|"
    r"Processor|Transformer|Blender|Decorator|Gate|Predicate|Policy|Handler|Pipeline|"
    r"Extractor|Injector|Sampler|Retriever|Booster|Enricher)s?$"
)
_FIRST_STRING_ARG = re.compile(r'\s*\(\s*("(?:[^"\\\n]|\\.)*")')
_COMPONENT_BINDING = re.compile(
    r"(?i)(^|_)(filters|sources|hydrators|scorers|selectors|side_?effects|rules|stages|"
    r"processors|pipelines|decorators|handlers|gates|rankers|mixers|transformers|enrichers|"
    r"extractors|retrievers)$"
)
#: A cheap pre-check on raw text: no list can be a registration list without one of these.
_COMPONENT_WORD = re.compile(
    r"[A-Z]\w*(?:Filter|Source|Hydrator|Scorer|Selector|SideEffect|Rule|Stage|Ranker|Mixer|"
    r"Processor|Transformer|Blender|Decorator|Gate|Predicate|Policy|Handler|Pipeline|"
    r"Extractor|Injector|Sampler|Retriever|Booster|Enricher)|(?i:filters|sources|hydrators|"
    r"scorers|selectors|side_?effects|rules|stages|processors|pipelines|decorators|handlers|"
    r"gates|rankers|mixers|transformers|enrichers|extractors|retrievers)"
)
#: The literal words of ``_COMPONENT_WORD``: its first alternative needs one of the
#: capitalised suffixes verbatim; its second one of the plurals, ignoring case (``side_?effects``
#: is spelled out twice).
_COMPONENT_SUFFIXES = (
    "Filter", "Source", "Hydrator", "Scorer", "Selector", "SideEffect", "Rule", "Stage",
    "Ranker", "Mixer", "Processor", "Transformer", "Blender", "Decorator", "Gate", "Predicate",
    "Policy", "Handler", "Pipeline", "Extractor", "Injector", "Sampler", "Retriever", "Booster",
    "Enricher",
)
_COMPONENT_PLURALS = (
    "filters", "sources", "hydrators", "scorers", "selectors", "sideeffects", "side_effects",
    "rules", "stages", "processors", "pipelines", "decorators", "handlers", "gates", "rankers",
    "mixers", "transformers", "enrichers", "extractors", "retrievers",
)


def _may_name_component(text: str) -> bool:
    """A necessary condition for ``_COMPONENT_WORD.search(text)``: substring tests only.

    Never False when the regex matches. A match needs a suffix verbatim, or a plural up to
    case; ``str.lower`` agrees with the regex's case folding on ASCII text, but a few
    non-ASCII characters fold to ASCII letters in the regex only (for example U+017F and
    U+0130), so non-ASCII text skips the pre-check and goes to the regex.
    """
    if not text.isascii():
        return True
    if any(word in text for word in _COMPONENT_SUFFIXES):
        return True
    lowered = text.lower()
    return any(word in lowered for word in _COMPONENT_PLURALS)


_COMMENT_START = {
    "rust": ("//", "/*", "*"),
    "java": ("//", "/*", "*"),
    "scala": ("//", "/*", "*"),
    "python": ("#",),
}
_DECLARED = re.compile(r"\b(?:let|val|var|const|static|final)\s+(?:mut\s+)?([A-Za-z_]\w*)")
_TRAILING_CALL = re.compile(r"[\w:.<>]+\s*\(\s*$")
_ANNOTATED = re.compile(r"([A-Za-z_]\w*)\s*:")
_IDENT = re.compile(r"[A-Za-z_]\w*")

# -- config keys ---------------------------------------------------------------------------

_YAML_KEY = re.compile(r"^(\s*)(?:-\s+)?(['\"]?)([A-Za-z0-9_.\-/]+)\2\s*:(?:\s+(.*?))?\s*$")
_INI_SECTION = re.compile(r"^\s*\[\[?([^\]]+)\]\]?\s*$")
_INI_KEY = re.compile(r"^(\s*)(['\"]?)([A-Za-z0-9_.\-/]+)\2\s*[=:]\s*(.*?)\s*$")
_JSON_KEY = re.compile(r"^(\s*)\"([^\"\\]+)\"\s*:\s*(.*?)\s*,?\s*$")


@dataclass(frozen=True, slots=True)
class ValueDecl:
    """A declaration with a default value: ``param!``, const/static, field or config key."""

    kind: str  # "param!", "const", "static", "field", "config-key"
    name: str  # qualified name (or dotted config key)
    ordinal: int  # among declarations with the same kind group and name, in file order
    start_line: int
    end_line: int
    value: str  # whitespace-normalised value text, comments removed
    value_type: str | None
    flag: str | None
    literal: bool  # the value is a literal (number, bool, string, simple list/duration)

    @property
    def key(self) -> tuple[str, str, int]:
        group = "param" if self.kind == PARAM_MACRO else (
            CONFIG_KEY if self.kind == CONFIG_KEY else "constant"
        )
        return (group, self.name, self.ordinal)


@dataclass(frozen=True, slots=True)
class ListEntry:
    key: str
    text: str
    line: int


@dataclass(frozen=True, slots=True)
class RegList:
    """A list literal that registers components (see :func:`BlobAnalysis.lists`)."""

    container: str | None
    binding: str | None
    ordinal: int
    start_line: int
    end_line: int
    entries: tuple[ListEntry, ...]

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.container or "", self.binding or "", self.ordinal)

    @property
    def label(self) -> str:
        base = self.container or "(module level)"
        if self.binding and self.binding != base.rsplit("::", 1)[-1].rsplit(".", 1)[-1]:
            base = f"{base}: {self.binding}"
        return base + (f" #{self.ordinal + 1}" if self.ordinal else "")


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _is_literal(code_value: str) -> bool:
    """``code_value`` is a value with literals blanked; true when no identifier remains."""
    stripped = _NUMBER.sub(" ", code_value)
    return all(word in _LITERAL_WORDS for word in _WORD.findall(stripped))


@dataclass
class BlobAnalysis:
    """Per-blob facts; see the module documentation."""

    oid: str
    language: str | None
    data: bytes
    symbol_loader: Callable[[], FileSymbols]
    config_keys: bool = False
    line_map: LineMap = field(init=False)
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        self.line_map = LineMap.of(self.data)
        self.sha256 = hashlib.sha256(self.data).hexdigest()

    @cached_property
    def symbols(self) -> FileSymbols:
        """Milestone 2 symbols, extracted (or read from the index) on first use."""
        return self.symbol_loader()

    @property
    def symbols_loaded(self) -> bool:
        return "symbols" in self.__dict__

    # -- text views --------------------------------------------------------------------

    @cached_property
    def lines(self) -> list[bytes]:
        return split_lines(self.data, self.line_map)

    @property
    def line_count(self) -> int:
        return self.line_map.line_count

    @cached_property
    def text(self) -> str | None:
        try:
            return self.data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    @cached_property
    def masked(self) -> Masked | None:
        if self.text is None or self.language not in NATIVE_LANGUAGES:
            return None
        return mask(self.text, self.language)

    @cached_property
    def char_lines(self) -> Lines | None:
        return Lines(self.text) if self.text is not None else None

    def char_range(self, start_line: int, end_line: int) -> tuple[int, int]:
        """Character offsets ``[a, b)`` of lines ``start_line..end_line`` of the text."""
        assert self.text is not None and self.char_lines is not None
        starts = self.char_lines.starts
        a = starts[start_line - 1]
        b = starts[end_line] if end_line < len(starts) else len(self.text)
        return a, b

    @property
    def indent_sensitive(self) -> bool:
        return self.language in ("python", "yaml")

    def tokens(self, lines: list[int]) -> tuple[str, ...]:
        """Comment- and whitespace-insensitive tokens of the given lines (in order).

        Comments are removed for the native languages; string literals stay whole tokens;
        for indentation-sensitive languages (Python, YAML) each non-blank line also
        contributes its indentation width.
        """
        if not lines:
            return ()
        out: list[str] = []
        if self.text is None:
            for line in lines:
                raw = self.lines[line - 1]
                if self.indent_sensitive and raw.strip():
                    width = len(raw) - len(raw.lstrip(b" \t"))
                    out.append("\n" + str(width))
                out.extend(part.decode("latin-1") for part in raw.split())
            return tuple(out)
        view = self.masked.nocomment if self.masked is not None else self.text
        spans = self.masked.string_spans if self.masked is not None else ()
        for run_start, run_end in _runs(lines):
            a, b = self.char_range(run_start, run_end)
            if self.indent_sensitive:
                for line in range(run_start, run_end + 1):
                    la, lb = self.char_range(line, line)
                    segment = view[la:lb]
                    if segment.strip():
                        width = len(segment) - len(segment.lstrip(" \t"))
                        out.append("\n" + str(width))
                        out.extend(_tokens_of(view, la, lb, spans))
            else:
                out.extend(_tokens_of(view, a, b, spans))
        return tuple(out)

    # -- symbols -----------------------------------------------------------------------

    @cached_property
    def _innermost_by_line(self) -> list[SymbolInfo | None]:
        """For every line, the smallest enclosing symbol (ties: the later one); imports
        never enclose. Painted from the largest span to the smallest."""
        table: list[SymbolInfo | None] = [None] * (self.line_count + 1)
        ordered = sorted(
            (s for s in self.symbols.symbols if s.kind not in _ATTRIBUTION_SKIP),
            key=lambda symbol: (-symbol.span, symbol.start_line),
        )
        for symbol in ordered:
            for line in range(symbol.start_line, min(symbol.end_line, self.line_count) + 1):
                table[line] = symbol
        return table

    def innermost(self, line: int) -> SymbolInfo | None:
        table = self._innermost_by_line
        return table[line] if 0 < line < len(table) else None

    def attribute(self, lines: list[int], limit: int = 8) -> tuple[str, ...]:
        """Qualified names of the innermost symbols enclosing ``lines``, in line order."""
        names: list[str] = []
        for line in lines:
            symbol = self.innermost(line)
            if symbol is not None and symbol.qualname not in names:
                names.append(symbol.qualname)
        if len(names) > limit:
            names = names[:limit] + [f"... {len(names) - limit} more"]
        return tuple(names)

    @cached_property
    def test_ranges(self) -> tuple[tuple[int, int], ...]:
        """Line ranges of test code inside a source file (Rust test modules and functions,
        Java ``@Test`` methods, Python ``test_*`` functions and ``Test*`` classes)."""
        ranges: list[tuple[int, int]] = []
        for symbol in self.symbols.symbols:
            if self._is_test_symbol(symbol):
                ranges.append((symbol.start_line, symbol.end_line))
        return tuple(sorted(ranges))

    def _is_test_symbol(self, symbol: SymbolInfo) -> bool:
        if self.language == "rust":
            header = b"".join(self.lines[symbol.start_line - 1 : symbol.name_line])
            if _CFG_TEST.search(header):
                return True  # any item compiled only for tests (classifier version 4)
            if symbol.kind == "module":
                return symbol.name in ("tests", "test")
            if symbol.kind in ("function", "method"):
                return bool(re.search(rb"#\[\s*(?:[\w:]+::)?(?:test|rstest)\b", header))
            return False
        if self.language == "java":
            return symbol.kind == "method" and "Test" in symbol.modifiers
        if self.language == "python":
            return (symbol.kind in ("function", "method") and symbol.name.startswith("test_")) or (
                symbol.kind == "class" and symbol.name.startswith("Test")
            )
        return False

    def in_test_code(self, lines: list[int]) -> bool | None:
        """Whether every code line of ``lines`` lies in test code; blank and comment-only
        lines may accompany them (``None``: no code line)."""
        code_lines = [line for line in lines if not self.code_free(line)]
        if not code_lines:
            return None
        return self.in_test_ranges(code_lines)

    def in_test_ranges(self, lines: list[int]) -> bool:
        return bool(lines) and all(
            any(start <= line <= end for start, end in self.test_ranges) for line in lines
        )

    # -- import and module declarations --------------------------------------------------

    @cached_property
    def declaration_lines(self) -> frozenset[int]:
        """Lines inside an ``import`` symbol (Rust ``use`` / ``extern crate``, Java, Scala and
        Python imports) or a bodyless ``module`` symbol (Rust ``mod name;``, a Java or Scala
        ``package`` clause). Empty for files whose language is not parsed."""
        if self.masked is None:
            return frozenset()
        code = self.masked.code
        lines: set[int] = set()
        for symbol in self.symbols.symbols:
            if symbol.kind == "module":
                a, b = self.char_range(symbol.start_line, min(symbol.end_line, self.line_count))
                if "{" in code[a:b]:
                    continue  # a module with a body holds code
            elif symbol.kind != "import":
                continue
            lines.update(range(symbol.start_line, min(symbol.end_line, self.line_count) + 1))
        return frozenset(lines)

    def code_free(self, line: int) -> bool:
        """The line holds no code: blank, or only a comment (native languages only)."""
        if self.masked is None:
            return not self.lines[line - 1].strip()
        a, b = self.char_range(line, line)
        return not self.masked.nocomment[a:b].strip()

    def only_declarations(self, lines: list[int]) -> bool | None:
        """Whether every code line of ``lines`` is an import or bodyless module declaration.

        ``None`` when there is no code line at all (nothing to decide on this side)."""
        return self.only_within(lines, self.declaration_lines)

    def only_within(self, lines: list[int], allowed: frozenset[int]) -> bool | None:
        """Whether every code line of ``lines`` is in ``allowed`` (``None``: no code line)."""
        code_lines = [line for line in lines if not self.code_free(line)]
        if not code_lines:
            return None
        return all(line in allowed for line in code_lines)

    # -- content rules (timelinexray.diff.content) ---------------------------------------

    @cached_property
    def telemetry_lines(self) -> frozenset[int]:
        """Lines whose every statement logs, traces or records a metric (native languages)."""
        if self.masked is None or self.char_lines is None:
            return frozenset()
        return content.telemetry_lines(self.masked.code, self.language or "", self.char_lines.starts)

    @cached_property
    def type_body_lines(self) -> frozenset[int]:
        """Lines inside a Rust ``struct``, ``enum`` or ``union`` definition (Milestone 2
        symbols, attributes included) that assign no value: a line with ``=`` in its code
        (an enum discriminant, an attribute argument such as ``default_value_t = 8080``) is
        excluded. Empty for other languages."""
        if self.masked is None or self.language != "rust":
            return frozenset()
        lines: set[int] = set()
        for symbol in self.symbols.symbols:
            if symbol.kind in content.TYPE_KINDS:
                lines.update(range(symbol.start_line, min(symbol.end_line, self.line_count) + 1))
        code = self.masked.code
        return frozenset(line for line in lines if "=" not in code[slice(*self.char_range(line, line))])

    @cached_property
    def rule_declaration_lines(self) -> frozenset[int]:
        """Lines inside a Rust visibility rule declaration (:func:`content.is_rule_declaration`)."""
        if self.masked is None or self.language != "rust":
            return frozenset()
        lines: set[int] = set()
        for symbol in self.symbols.symbols:
            if content.is_rule_declaration(symbol.kind, symbol.signature or ""):
                lines.update(range(symbol.start_line, min(symbol.end_line, self.line_count) + 1))
        return frozenset(lines)

    # -- declarations with values --------------------------------------------------------

    @cached_property
    def values(self) -> tuple[ValueDecl, ...]:
        if self.config_keys and self.text is not None:
            return tuple(self._config_values())
        if self.masked is None:
            return ()
        found: list[ValueDecl] = []
        seen: Counter[tuple[str, str]] = Counter()
        for symbol in self.symbols.symbols:
            if symbol.kind not in VALUE_KINDS or self.in_test_ranges([symbol.name_line]):
                continue
            decl = self._value_of(symbol)
            if decl is None:
                continue
            group = decl.key[0]
            ordinal = seen[(group, decl.name)]
            seen[(group, decl.name)] += 1
            found.append(
                ValueDecl(decl.kind, decl.name, ordinal, decl.start_line, decl.end_line,
                          decl.value, decl.value_type, decl.flag, decl.literal)
            )
        return tuple(found)

    def _value_of(self, symbol: SymbolInfo) -> ValueDecl | None:
        assert self.masked is not None
        a, b = self.char_range(symbol.start_line, symbol.end_line)
        code = self.masked.code
        keep = self.masked.nocomment
        if symbol.kind == "param":
            position = code.find("param!", a, b)
            opener = code.find("(", position, b) if position != -1 else -1
            if opener == -1:
                return None
            args = _split_args(code, keep, opener, b)
            return self._param_decl(symbol, args) if args else None
        equals = _assignment(code, a, b)
        if equals is None:
            return None
        end = _statement_end(code, equals + 1, b)
        value_keep = keep[equals + 1 : end]
        value_code = code[equals + 1 : end]
        literal = _is_literal(value_code)
        if not literal:
            return None  # a computed value is logic, not a default
        return ValueDecl(symbol.kind, symbol.qualname, 0, symbol.start_line, symbol.end_line,
                         _normalize(value_keep), None, None, literal)

    def _param_decl(self, symbol: SymbolInfo, args: list[tuple[str, str]]) -> ValueDecl:
        value_keep, value_code = args[3] if len(args) >= 4 else args[-1]
        flag = None
        if len(args) >= 4:
            text = _normalize(args[2][0])
            flag = text[1:-1] if len(text) >= 2 and text[0] == text[-1] == '"' else text
        value_type = _normalize(args[1][0]) if len(args) >= 2 else None
        return ValueDecl(PARAM_MACRO, symbol.qualname, 0, symbol.start_line, symbol.end_line,
                         _normalize(value_keep), value_type, flag, _is_literal(value_code))

    def _config_values(self) -> list[ValueDecl]:
        assert self.text is not None
        found: list[ValueDecl] = []
        seen: Counter[str] = Counter()
        stack: list[tuple[int, str]] = []  # (indent, key) of open parents
        section: str | None = None
        for number, raw in enumerate(self.text.split("\n")[: self.line_count], start=1):
            line = raw.rstrip("\r")
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";", "//")):
                continue
            section_match = _INI_SECTION.match(line)
            if section_match and not _JSON_KEY.match(line):
                section = section_match.group(1).strip()
                stack = []
                continue
            match = _JSON_KEY.match(line) or _YAML_KEY.match(line) or _INI_KEY.match(line)
            if match is None:
                continue
            groups = match.groups()
            indent = len(groups[0])
            key = groups[-2] if len(groups) == 4 else groups[1]
            value = groups[-1] if len(groups) == 4 else groups[2]
            value = _strip_config_comment(value or "")
            while stack and stack[-1][0] >= indent:
                stack.pop()
            if not value or value in ("{", "[", "|", ">", "|-", ">-"):
                stack.append((indent, key))
                continue
            parts = ([section] if section else []) + [name for _, name in stack] + [key]
            name = ".".join(parts)
            ordinal = seen[name]
            seen[name] += 1
            found.append(ValueDecl(CONFIG_KEY, name, ordinal, number, number,
                                   _normalize(value.rstrip(",")), None, None, True))
        return found

    # -- registration lists --------------------------------------------------------------

    def may_lack_code(self, lines: list[int]) -> bool:
        """``False`` when some line visibly holds code (not blank, not starting a comment);
        a cheap pre-check before comparing tokens against an empty side."""
        markers = _COMMENT_START.get(self.language or "")
        if markers is None:
            return not any(self.lines[line - 1].strip() for line in lines)
        for line in lines:
            text = self.lines[line - 1].strip()
            if text and not text.decode("latin-1").startswith(markers):
                return False
        return True

    @cached_property
    def lists(self) -> tuple[RegList, ...]:
        if self.language not in _LIST_OPENERS or self.text is None:
            return ()
        if not _may_name_component(self.text) or not _COMPONENT_WORD.search(self.text):
            return ()
        if self.masked is None:
            return ()
        code, keep = self.masked.code, self.masked.nocomment
        close_of, _ = match_delimiters(code)
        found: list[tuple[str | None, str | None, int, int, tuple[ListEntry, ...]]] = []
        for match in _LIST_OPENERS[self.language].finditer(code):
            opener = next(match.start(g) for g in range(1, (match.lastindex or 0) + 1)
                          if match.group(g) is not None)
            closer = close_of.get(opener)
            if closer is None or closer <= opener:
                continue
            entries = self._entries(code, keep, close_of, opener, closer)
            if not entries:
                continue
            binding = self._binding(code, opener)
            if not any(_COMPONENT.match(entry.key) for entry in entries) and (
                binding is None
                or not _COMPONENT_BINDING.search(binding)
                or not any(entry.key[:1].isalpha() or entry.key[:1] == "_" for entry in entries)
            ):
                continue  # neither component names nor a component list of named entries
            start_line = self.char_lines.line_of(opener) if self.char_lines else 1
            end_line = self.char_lines.line_of(closer) if self.char_lines else 1
            if self.in_test_ranges([start_line]):
                continue
            enclosing = self.innermost(start_line)
            found.append((enclosing.qualname if enclosing else None, binding, start_line,
                          end_line, entries))
        result: list[RegList] = []
        seen: Counter[tuple[str | None, str | None]] = Counter()
        for container, binding, start_line, end_line, entries in found:
            ordinal = seen[(container, binding)]
            seen[(container, binding)] += 1
            result.append(RegList(container, binding, ordinal, start_line, end_line, entries))
        return tuple(result)

    def _entries(
        self, code: str, keep: str, close_of: dict[int, int], opener: int, closer: int
    ) -> tuple[ListEntry, ...]:
        assert self.char_lines is not None
        entries: list[ListEntry] = []
        start = opener + 1
        position = start
        while position <= closer:
            char = code[position] if position < closer else ","
            if char in "([{" and position < closer:
                position = max(close_of.get(position, position), position) + 1
                continue
            if char == ",":
                text = keep[start:position]
                if text.strip():
                    offset = start + (len(text) - len(text.lstrip()))
                    entries.append(ListEntry(_entry_key(code[start:position], text),
                                             _normalize(text)[:200], self.char_lines.line_of(offset)))
                start = position + 1
            position += 1
        return tuple(entries)

    def _binding(self, code: str, opener: int) -> str | None:
        floor = max(0, opener - 400)
        boundary = max(code.rfind(char, floor, opener) for char in ";{}")
        if self.language == "python":
            boundary = max(boundary, code.rfind("\n", floor, opener))
        prefix = code[max(boundary + 1, floor) : opener]
        prefix = re.sub(r"\bvec!\s*$", "", prefix)
        prefix = re.sub(r"(?:\b(?:Seq|List|Vector|Set|IndexedSeq|Array|List\.of|Set\.of|"
                        r"Arrays\.asList|ImmutableList\.of|ImmutableSet\.of|Stream\.of|"
                        r"Lists\.newArrayList)\s*(?:[\[<][^\]>\n]*[\]>])?\s*)$", "", prefix)
        prefix = prefix.rstrip().rstrip("&").rstrip()
        for _ in range(8):  # wrapper calls such as Arc::new( before the list
            match = _TRAILING_CALL.search(prefix[-120:])
            if match is None:
                break
            prefix = prefix[: len(prefix) - (len(prefix[-120:]) - match.start())].rstrip()
        if not prefix.endswith("=") or prefix[-2:-1] in ("=", "!", "<", ">"):
            return None
        left = prefix[:-1].rstrip()
        declared = _DECLARED.findall(left)
        if declared:
            return declared[-1]
        annotated = _ANNOTATED.search(left)
        if annotated:
            return annotated.group(1)
        names = _IDENT.findall(left)
        return names[-1] if names else None

    # -- citations -----------------------------------------------------------------------

    def span(self, commit: str, path: str, start: int, end: int) -> SpanRead:
        return read_span(self.data, start, end, commit=commit, path=path, blob_oid=self.oid,
                         line_map=self.line_map, blob_sha256=self.sha256)


def _runs(lines: list[int]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    for line in sorted(set(lines)):
        if runs and line == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], line)
        else:
            runs.append((line, line))
    return runs


def _tokens_of(view: str, a: int, b: int, spans: tuple[tuple[int, int], ...]) -> list[str]:
    out: list[str] = []
    position = a
    for start, end in spans:
        if end <= a or start >= b:
            continue
        if start > position:
            out.extend(_TOKEN.findall(view[position:start]))
        out.append(view[max(start, a) : min(end, b)])
        position = min(end, b)
    if position < b:
        out.extend(_TOKEN.findall(view[position:b]))
    return out


def _split_args(code: str, keep: str, opener: int, limit: int) -> list[tuple[str, str]]:
    """Top-level comma-separated arguments of the group opened at ``opener``.

    Returns ``(text with literals, text with literals blanked)`` per argument; comments are
    removed from both.
    """
    close_of, _ = match_delimiters(code[opener:limit])
    closer = opener + close_of.get(0, len(code[opener:limit]) - 1)
    args: list[tuple[str, str]] = []
    start = opener + 1
    position = start
    while position <= closer:
        char = code[position] if position < closer else ","
        if char in "([{" and position < closer:
            local = close_of.get(position - opener)
            position = (opener + local if local is not None else position) + 1
            continue
        if char == ",":
            if keep[start:position].strip():
                args.append((keep[start:position].strip(), code[start:position]))
            start = position + 1
        position += 1
    return args


def _assignment(code: str, a: int, b: int) -> int | None:
    depth = 0
    for position in range(a, b):
        char = code[position]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "=" and depth == 0:
            before = code[position - 1] if position > a else " "
            after = code[position + 1] if position + 1 < b else " "
            if after in "=>" or before in "=!<>+-*/%&|^:":
                continue
            return position
    return None


def _statement_end(code: str, start: int, b: int) -> int:
    depth = 0
    for position in range(start, b):
        char = code[position]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                return position
            depth -= 1
        elif char == ";" and depth == 0:
            return position
    return b


def _entry_key(code_text: str, keep_text: str) -> str:
    """The registered component's name: the last capitalised path segment of the entry
    after wrappers (``Box::new(``, ``Arc::new(``, ``new``, ``&``); for a lower-case call
    whose first argument is a string literal, ``name("literal")``."""
    offset = len(code_text) - len(code_text.lstrip())
    while True:
        match = _WRAPPER.match(code_text, offset)
        if match is None or match.end() == offset:
            break
        offset = match.end()
        offset += len(code_text[offset:]) - len(code_text[offset:].lstrip())
    match = _ENTRY_HEAD.match(code_text, offset)
    if not match:
        return _normalize(keep_text)[:80]
    segments = re.split(r"\s*(?:::|\.)\s*", match.group())
    upper = [segment for segment in segments if segment[:1].isupper()]
    if upper:
        return upper[-1]
    literal = _FIRST_STRING_ARG.match(keep_text, match.end())
    if literal:
        return f"{segments[-1]}({literal.group(1)[:80]})"
    return segments[0]


def _strip_config_comment(value: str) -> str:
    if value.startswith(("'", '"')):
        return value
    for marker in (" #", " ;", " //"):
        index = value.find(marker)
        if index != -1:
            value = value[:index]
    return value.strip()


@dataclass(frozen=True, slots=True)
class BlobView:
    """A blob at a commit and path: what citations are cut from."""

    commit: str
    path: str
    analysis: BlobAnalysis

    def cite(self, start: int, end: int, *, role: str = SPAN, note: str | None = None) -> Citation:
        span = self.analysis.span(self.commit, self.path, start, end)
        return Citation(self.commit, self.path, role, span.start_line, span.end_line,
                        span.sha256, self.analysis.oid, note)


def make_analysis(oid: str, path: str, language: str | None, data: bytes,
                  symbols: Callable[[], FileSymbols]) -> BlobAnalysis:
    return BlobAnalysis(oid, language, data, symbols, config_keys=is_key_value_config(path))
