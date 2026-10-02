"""Syntax and symbol extraction for Rust, Scala, Python and Java, behind a backend interface.

Upstream code is only parsed as text here: it is never built, imported or executed.

Records
-------
An extraction turns the decoded text of one blob into:

* :class:`Symbol` records - declarations with a kind from :data:`SYMBOL_KINDS`, a name, the
  chain of enclosing declarations (``container``), and 1-based inclusive line numbers:
  ``start_line`` (first line, including attributes, annotations or decorators directly
  above the declaration), ``name_line`` (the line holding the name) and ``end_line`` (the
  closing brace, terminator or last line of the body).
* :class:`CallCandidate` records - a name followed by an argument list inside the body of
  a function, method, constructor or initializer. They are **syntactic candidates only**:
  the callee is a name as written, never resolved to a declaration, so the index reports
  them with relation ``CANDIDATE_CALL`` and resolution ``unresolved``. Dynamic dispatch,
  macros, implicits, overloading, imports and cross-service calls are not resolved.

Backends
--------
A backend implements :class:`Backend`: a ``name``, a ``version`` (bumped whenever its output
for the same input can change), the ``languages`` it handles, :meth:`Backend.probe` (``None``
when usable, otherwise the reason it is not) and :meth:`Backend.extract`.

* ``lexical`` (:mod:`timelinexray.syntax.lexical`) is always available and is the fallback
  for every language. It masks comments and string literals, matches declarations with
  per-language patterns, and finds extents from delimiter matching (Rust, Java, Scala) or
  indentation (Python). It is heuristic; see that module for its documented limits.
* ``tree-sitter`` (:mod:`timelinexray.syntax.treesitter`) is a documented hook only. No
  tree-sitter package or grammar is a dependency of this release; the probe reports why
  the backend is unavailable, and nothing selects it.

:func:`select_backend` picks, per language, the available backend with the highest
priority in a :class:`Registry`. Parse results are cached by the index under
``(blob_oid, language, backend, version)``, so changing or adding a backend never mixes its
records with another backend's, and every file and symbol reports the backend that
produced it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

LANGUAGES = ("java", "python", "rust", "scala")

#: Every symbol kind any backend may emit.
SYMBOL_KINDS = (
    "annotation",  # Java @interface
    "class",  # Java/Scala/Python class (Scala case classes carry the "case" modifier)
    "const",  # Rust const, Java static final field, Python UPPER_CASE module/class constant
    "constructor",  # Java constructor, Scala auxiliary constructor (def this)
    "enum",
    "field",  # Java field, Scala val/var member
    "function",  # free function, nested function
    "impl",  # Rust impl block (name = the implementing type)
    "import",  # Rust use / extern crate, Java/Scala/Python import
    "interface",
    "macro",  # Rust macro_rules!
    "method",  # function declared directly in a type-like body
    "module",  # Rust mod, Java/Scala package clause
    "object",  # Scala object / case object / package object
    "param",  # Rust param!(Name, ...) declaration macro
    "record",  # Java record
    "static",  # Rust static
    "struct",
    "trait",
    "type",  # Rust/Scala type alias or associated type
    "union",
)

#: Kinds whose bodies can contain call candidates as the "caller".
CALLER_KINDS = frozenset({"function", "method", "constructor", "const", "static", "field"})

CALL_FORMS = ("call", "method", "path", "macro", "new")
CALL_RELATION = "CANDIDATE_CALL"
CALL_RESOLUTION = "unresolved"

# Extraction statuses for one file.
PARSED = "parsed"
PARTIAL = "partial"  # extracted, but with diagnostics (for example unbalanced delimiters)
FAILED = "failed"  # the backend raised; no records were kept


@dataclass(frozen=True, slots=True)
class Symbol:
    kind: str
    name: str
    container: str | None
    separator: str  # joins container and name into the qualified name ("::" or ".")
    start_line: int
    name_line: int
    end_line: int
    signature: str
    modifiers: tuple[str, ...] = ()

    @property
    def qualname(self) -> str:
        return f"{self.container}{self.separator}{self.name}" if self.container else self.name


@dataclass(frozen=True, slots=True)
class CallCandidate:
    caller: int  # index of the calling symbol in :attr:`Extraction.symbols`
    callee: str  # the name as written (a macro keeps its "!")
    qualifier: str | None  # receiver or path text before the name, when it is a plain chain
    form: str  # one of CALL_FORMS
    line: int


@dataclass(frozen=True, slots=True)
class Extraction:
    status: str
    reason: str | None
    symbols: tuple[Symbol, ...]
    calls: tuple[CallCandidate, ...]


@runtime_checkable
class Backend(Protocol):
    """What the index needs from a syntax backend."""

    name: str
    version: str
    languages: frozenset[str]

    def probe(self) -> str | None:
        """``None`` if the backend can run in this process, else why it cannot."""

    def extract(self, text: str, language: str) -> Extraction:
        """Symbols and call candidates of ``text`` (one blob decoded as UTF-8)."""


def extract_checked(backend: Backend, text: str, language: str, line_count: int) -> Extraction:
    """Run ``backend`` on ``text`` and validate its records (public internal API).

    Any exception raised by the backend, a symbol whose line range is not
    ``1 <= start_line <= name_line <= end_line <= line_count``, or a call candidate outside
    the file or without a caller becomes a ``failed`` extraction with a reason, so a backend
    bug is reported instead of stopping an index build or a diff. ``line_count`` is the
    number of lines of the blob under the span contract (:class:`timelinexray.span.LineMap`).
    """
    try:
        extraction = backend.extract(text, language)
    except Exception as exc:  # a backend bug must not stop the caller; it is reported
        return Extraction(FAILED, f"{type(exc).__name__}: {exc}"[:200], (), ())
    for symbol in extraction.symbols:
        if not 1 <= symbol.start_line <= symbol.name_line <= symbol.end_line <= line_count:
            return Extraction(FAILED, f"symbol {symbol.name!r} has an invalid line range", (), ())
    for call in extraction.calls:
        if not 0 <= call.caller < len(extraction.symbols) or not 1 <= call.line <= line_count:
            return Extraction(FAILED, "call candidate outside the file or without a caller", (), ())
    return extraction


def backend_key(backend: Backend) -> str:
    return f"{backend.name}/{backend.version}"


@dataclass
class Registry:
    """Backends in priority order (earlier entries win when available)."""

    backends: list[Backend] = field(default_factory=list)

    def register(self, backend: Backend, *, first: bool = True) -> None:
        if not isinstance(backend, Backend):
            raise TypeError(f"{backend!r} does not implement the Backend protocol")
        self.backends = [b for b in self.backends if b.name != backend.name]
        if first:
            self.backends.insert(0, backend)
        else:
            self.backends.append(backend)

    def select(self, language: str) -> Backend | None:
        for backend in self.backends:
            if language in backend.languages and backend.probe() is None:
                return backend
        return None

    def report(self) -> list[dict[str, object]]:
        """Every registered backend with its availability, for status output."""
        return [
            {
                "backend": backend.name,
                "version": backend.version,
                "languages": sorted(backend.languages),
                "unavailable_reason": backend.probe(),
            }
            for backend in self.backends
        ]


def default_registry() -> Registry:
    """The tree-sitter hook (never available in this release) ahead of the lexical fallback."""
    from .lexical import LexicalBackend
    from .treesitter import TreeSitterHook

    return Registry([TreeSitterHook(), LexicalBackend()])


def select_backend(language: str, registry: Registry | None = None) -> Backend | None:
    return (registry or default_registry()).select(language)


def sorted_kinds(kinds: Iterable[str]) -> list[str]:
    order = {kind: index for index, kind in enumerate(SYMBOL_KINDS)}
    return sorted(set(kinds), key=lambda kind: order.get(kind, len(order)))


__all__ = [
    "CALLER_KINDS",
    "CALL_FORMS",
    "CALL_RELATION",
    "CALL_RESOLUTION",
    "FAILED",
    "LANGUAGES",
    "PARSED",
    "PARTIAL",
    "SYMBOL_KINDS",
    "Backend",
    "CallCandidate",
    "Extraction",
    "Registry",
    "Symbol",
    "backend_key",
    "default_registry",
    "extract_checked",
    "select_backend",
    "sorted_kinds",
]
