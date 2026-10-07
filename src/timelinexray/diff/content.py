"""Content rules of the change classifier: what the changed lines *do*, read from masked code.

Content rules (classifier version 3), each a line set computed once per blob:

* **telemetry lines** - lines that belong only to simple statements that log, trace or record
  a metric: Rust ``info!``/``warn!``/... and ``tracing::``/``log::`` macros, ``metrics``-crate
  macros, Prometheus ``register_*!`` statics and ``NAME.inc()``/``observe()`` updates;
  Python ``logger.``/``logging.`` calls and ``metrics.``/``stats.`` calls; Java and Scala
  ``log.``/``logger.`` calls and ``stats``/``statsReceiver`` counters.
* **type body lines** - lines inside a Rust ``struct``, ``enum`` or ``union`` definition
  (Milestone 2 symbols): fields, variants and their attributes, never executable code, and
  no line that assigns a value (``=``: a discriminant or an attribute default).
* **access modifiers** - the token sequences of both sides of a hunk are equal once access
  modifiers are removed (Rust ``pub`` and ``pub(crate|super|self|in path)``, Java ``public``,
  ``private``, ``protected``, Scala ``private``/``protected`` with an optional ``[scope]``).
* **rule declaration lines** - lines inside a Rust ``const``/``static`` whose declared type,
  or a function whose return type, is built only from the visibility rule types
  ``Condition``, ``Predicate``, ``Clause`` and ``RuleClause`` (``Vec<RuleClause>``,
  ``[Clause; 3]``, ...): the declarative rule definitions of visibility filtering.

Each pattern is a call shape: a logging/metrics macro, a logger method, or a metrics
object's *metric-creating* method followed by an update (``counter(...).incr()``); the
words ``metric`` or ``stats`` alone never qualify, because in this upstream they often name
ranking data (a ``Metric`` enum, ``metric_value``, a ``stats`` map).

Statements are split on masked code (comments and literals blanked), so a ``;``, bracket or
macro name inside a string or comment is never read. A statement that is not recognised in
full is not telemetry, and a line shared with any other statement is not a telemetry line.
"""

from __future__ import annotations

import bisect
import re

#: Statement patterns per language, matched against the whole statement (masked code,
#: whitespace collapsed, a trailing ``;`` removed).
_RUST_TELEMETRY = (
    re.compile(r"(?:(?:tracing|log)\s*::\s*)?(?:trace|debug|info|warn|error)\s*!\s*\(.*\)"),
    re.compile(r"(?:tracing\s*::\s*)?event\s*!\s*\(.*\)"),
    re.compile(r"(?:metrics\s*::\s*)?(?:counter|gauge|histogram|increment_counter|increment_gauge"
               r"|decrement_gauge)\s*!\s*\(.*\)"
               r"(?:\s*\.\s*(?:increment|decrement|absolute|set|record)\s*\(.*\))?"),
    re.compile(r"(?:pub(?:\s*\(\s*[\w:]+\s*\))?\s+)?static\s+ref\s+[A-Z][A-Z0-9_]*\s*:\s*[\w:<>, ]+"
               r"=\s*register_\w+\s*!\s*\(.*\)(?:\s*\.\s*(?:unwrap|expect)\s*\(.*\))?"),
    re.compile(r"[A-Z][A-Z0-9_]*(?:\s*\.\s*with_label_values\s*\(.*\))?"
               r"\s*\.\s*(?:inc|inc_by|dec|dec_by|observe)\s*\(.*\)"),
    re.compile(r"[A-Z][A-Z0-9_]*\s*\.\s*with_label_values\s*\(.*\)\s*\.\s*set\s*\(.*\)"),
)
_PYTHON_TELEMETRY = (
    re.compile(r"(?:(?:self|cls)\s*\.\s*)?_?(?:logger|log|logging|LOGGER|LOG)\s*\.\s*"
               r"(?:debug|info|warning|warn|error|exception|critical|log)\s*\(.*\)"),
    re.compile(r"(?:(?:self|cls)\s*\.\s*)?_?(?:metrics|Metrics|stats|statsd)\s*\.\s*"
               r"(?:counter|histogram|gauge|timer|timing|increment|incr|decrement|decr)\s*\(.*\)"
               r"(?:\s*\.\s*(?:record|add|inc|incr|observe|set)\s*\(.*\))?"),
)
_JVM_TELEMETRY = (
    re.compile(r"(?:this\s*\.\s*)?(?:log|logger|LOG|LOGGER|Log|Logger)\s*\.\s*"
               r"(?:trace|debug|info|warn|warning|error)\s*\(.*\)"),
    re.compile(r"(?:this\s*\.\s*)?\w*[sS]tats\w*\s*\.\s*(?:counter|stat)\s*\(.*\)"
               r"\s*\.\s*(?:incr|add)\s*\(.*\)"),
)
TELEMETRY_PATTERNS = {
    "rust": _RUST_TELEMETRY,
    "python": _PYTHON_TELEMETRY,
    "java": _JVM_TELEMETRY,
    "scala": _JVM_TELEMETRY,
}
_BRACE_LANGUAGES = frozenset({"rust", "java", "scala"})
_NEWLINE_LANGUAGES = frozenset({"python", "scala"})
TYPE_KINDS = frozenset({"struct", "enum", "union"})
#: The visibility rule types, and the containers a declared type may wrap them in.
RULE_TYPES = frozenset({"Condition", "Predicate", "Clause", "RuleClause"})
_TYPE_WRAPPERS = frozenset({"Vec", "Option", "Box", "static"})
_DECLARED_TYPE = re.compile(r"\b(?:const|static)\s+(?:mut\s+)?[A-Za-z_]\w*\s*:\s*([^=]+)=")
_TYPE_IDENT = re.compile(r"[A-Za-z_]\w*")


def statements(code: str, language: str) -> list[tuple[int, int, bool]]:
    """``(start, end, simple)`` for the statements of masked ``code`` (``[start, end)``
    offsets; ``simple`` is false for a block header).

    A statement starts at its first code character and ends at a ``;`` outside brackets,
    at a line end outside brackets in Python and Scala (unless the next line continues a
    method chain with ``.``), or just before a ``}`` that closes the enclosing block. In the
    brace languages a ``{`` outside brackets opens a block: what came before it (a header
    such as ``fn f()`` or ``if x``) is a header, not a simple statement.
    """
    brace = language in _BRACE_LANGUAGES
    newline_ends = language in _NEWLINE_LANGUAGES
    out: list[tuple[int, int, bool]] = []
    start: int | None = None
    depth = 0
    size = len(code)
    index = 0
    while index < size:
        char = code[index]
        if start is None:
            if char.isspace() or (brace and char in "{}") or char == ";":
                index += 1
                continue
            start, depth = index, 0
        if char in "([{":
            if brace and char == "{" and depth == 0:
                out.append((start, index + 1, False))
                start = None
            else:
                depth += 1
        elif char in ")]}":
            if depth == 0:
                out.append((start, index, True))
                start = None
            else:
                depth -= 1
        elif char == ";" and depth == 0:
            out.append((start, index + 1, True))
            start = None
        elif char == "\n" and depth == 0 and newline_ends:
            rest = index + 1
            while rest < size and code[rest] in " \t\r\n":
                rest += 1
            back = index - 1
            while back >= start and code[back] in " \t\r":
                back -= 1
            continued = (rest < size and code[rest] == ".") or (
                language == "python" and back >= start and code[back] == "\\")
            if not continued:
                out.append((start, index, True))
                start = None
        index += 1
    if start is not None:
        out.append((start, size, True))
    return out


def is_telemetry_statement(text: str, language: str) -> bool:
    """Whether one statement's masked text (any whitespace) is a telemetry call in full."""
    compact = " ".join(text.split()).rstrip(";").strip()
    return any(pattern.fullmatch(compact) for pattern in TELEMETRY_PATTERNS.get(language, ()))


def telemetry_lines(code: str, language: str, line_starts: list[int]) -> frozenset[int]:
    """1-based lines whose every statement is a telemetry statement (see the module
    documentation); ``line_starts`` holds the offset of every line's first character."""
    verdicts: dict[int, bool] = {}
    for start, end, simple in statements(code, language):
        telemetry = simple and is_telemetry_statement(code[start:end], language)
        first = bisect.bisect_right(line_starts, start)
        last = bisect.bisect_right(line_starts, max(start, end - 1))
        for line in range(first, last + 1):
            verdicts[line] = verdicts.get(line, True) and telemetry
    return frozenset(line for line, telemetry in verdicts.items() if telemetry)


def _rule_type(text: str) -> bool:
    names = set(_TYPE_IDENT.findall(text)) - _TYPE_WRAPPERS
    return bool(names) and names <= RULE_TYPES


def is_rule_declaration(kind: str, signature: str) -> bool:
    """A Rust ``const``/``static`` whose declared type, or a function or method whose return
    type, is built only from :data:`RULE_TYPES` (see the module documentation)."""
    if kind in ("const", "static"):
        match = _DECLARED_TYPE.search(signature)
        return match is not None and _rule_type(match.group(1))
    if kind in ("function", "method") and "->" in signature:
        return _rule_type(signature.rsplit("->", 1)[1])
    return False


_MODIFIER_WORDS = {
    "rust": frozenset({"pub"}),
    "java": frozenset({"public", "private", "protected"}),
    "scala": frozenset({"private", "protected"}),
}


def without_access_modifiers(tokens: tuple[str, ...], language: str) -> tuple[str, ...] | None:
    """``tokens`` with every access modifier removed (see the module documentation), or
    ``None`` for a language without such modifiers."""
    words = _MODIFIER_WORDS.get(language)
    if words is None:
        return None
    out: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in words:
            index += 1
            opener, closer = ("(", ")") if language == "rust" else ("[", "]")
            if index < len(tokens) and tokens[index] == opener and closer in tokens[index:]:
                index = tokens.index(closer, index) + 1
            continue
        out.append(token)
        index += 1
    return tuple(out)
