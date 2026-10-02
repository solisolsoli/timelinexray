"""Syntax extraction: masking, the four lexical extractors, the backend interface."""

from __future__ import annotations

import random
import unittest

from timelinexray.syntax import (
    CALLER_KINDS,
    FAILED,
    PARSED,
    PARTIAL,
    SYMBOL_KINDS,
    Extraction,
    Registry,
    Symbol,
    backend_key,
    default_registry,
    select_backend,
)
from timelinexray.syntax.lexical import LexicalBackend, mask
from timelinexray.syntax.treesitter import TreeSitterHook
from tests import syntax_fixtures as F

LEXICAL = LexicalBackend()


def symbols_of(text: str, language: str) -> list[tuple[str, str, int, int]]:
    extraction = LEXICAL.extract(text, language)
    return [(s.kind, s.qualname, s.start_line, s.end_line) for s in extraction.symbols]


def calls_of(text: str, language: str) -> list[tuple[str, str, str | None, str, int]]:
    extraction = LEXICAL.extract(text, language)
    return [
        (extraction.symbols[c.caller].qualname, c.callee, c.qualifier, c.form, c.line)
        for c in extraction.calls
    ]


RUST_SYMBOLS = [
    ("import", "crate::inputs::{CandidateScoringInputs, QueryScoringContext}", 2, 2),
    ("import", "std::collections::HashMap", 3, 3),
    ("struct", "ValueScores", 6, 10),
    ("enum", "Mode", 12, 15),
    ("trait", "Scorer", 17, 22),
    ("method", "Scorer::score", 18, 18),
    ("method", "Scorer::name", 19, 21),
    ("impl", "Weighted", 24, 34),
    ("method", "Weighted::score", 28, 33),
    ("impl", "Mode", 36, 40),
    ("method", "Mode::is_fast", 37, 39),
    ("const", "NEGATIVE_SCORES_OFFSET", 42, 42),
    ("static", "COUNTER", 43, 43),
    ("param", "ReplyWeight", 45, 45),
    ("param", "ClickWeight", 46, 51),
    ("macro", "weighted", 53, 57),
    ("function", "compute_weighted_score", 59, 65),
    ("function", "compute_weighted_score::inner_apply", 60, 62),
    ("module", "tests", 67, 73),
    ("function", "tests::it_scores", 69, 72),
]
RUST_CALLS = [
    ("Scorer::name", "from", "String", "path", 20),
    ("Weighted::score", "reply_weight_for", "self.weights", "method", 32),
    ("Weighted::score", "helper", None, "call", 32),
    ("Weighted::score", "lookup", None, "call", 32),
    ("Mode::is_fast", "matches!", None, "macro", 38),
    ("compute_weighted_score::inner_apply", "weighted!", None, "macro", 61),
    ("compute_weighted_score", "iter", "scores", "method", 63),
    ("compute_weighted_score", "map", None, "method", 63),
    ("compute_weighted_score", "inner_apply", None, "call", 63),
    ("compute_weighted_score", "sum", None, "method", 63),
    ("compute_weighted_score", "new", "Box", "path", 64),
    ("compute_weighted_score", "clamp", None, "method", 64),
    ("tests::it_scores", "assert_eq!", None, "macro", 71),
    ("tests::it_scores", "compute_weighted_score", "super", "path", 71),
    ("tests::it_scores", "default", "Default", "path", 71),
]

SCALA_SYMBOLS = [
    ("module", "com.example.ranking", 1, 1),
    ("import", "com.example.util.{Helper => H, _}", 3, 3),
    ("import", "scala.concurrent.{ExecutionContext, Future}", 4, 4),
    ("class", "Candidate", 7, 23),
    ("field", "Candidate.name", 15, 15),
    ("method", "Candidate.total", 17, 17),
    ("method", "Candidate.weighted", 19, 22),
    ("trait", "Shape", 25, 25),
    ("object", "Empty", 26, 26),
    ("class", "Circle", 27, 27),
    ("object", "Registry", 29, 58),
    ("field", "Registry.MaxResults", 30, 30),
    ("field", "Registry.cache", 31, 31),
    ("field", "Registry.counter", 32, 32),
    ("type", "Registry.Key", 33, 33),
    ("method", "Registry.build", 35, 44),
    ("method", "Registry.chained", 46, 49),
    ("method", "Registry.describe", 51, 51),
    ("object", "Registry.Nested", 53, 57),
    ("method", "Registry.Nested.+", 54, 54),
    ("field", "Registry.Nested.query", 55, 56),
    ("class", "Plain", 60, 62),
    ("constructor", "Plain.this", 61, 61),
]
SCALA_CALLS = [
    ("Candidate.weighted", "getOrElse", "weights", "method", 20),
    ("Registry.cache", "LruCache", None, "new", 31),
    ("Registry.build", "keySerializer", None, "call", 39),
    ("Registry.build", "lookup", None, "call", 40),
    ("Registry.build", "fallback", None, "call", 42),
    ("Registry.chained", "compute", None, "call", 47),
    ("Registry.chained", "map", None, "method", 48),
    ("Registry.chained", "getOrElse", None, "method", 49),
]

JAVA_SYMBOLS = [
    ("module", "com.example.ranking", 1, 1),
    ("import", "java.util.List", 3, 3),
    ("import", "java.util.Map", 4, 4),
    ("import", "java.util.Map.Entry", 5, 5),
    ("class", "RankingService", 8, 55),
    ("const", "RankingService.TABLE", 11, 11),
    ("const", "RankingService.CLICK_WEIGHT", 12, 12),
    ("field", "RankingService.limit", 13, 13),
    ("constructor", "RankingService.RankingService", 15, 18),
    ("method", "RankingService.run", 20, 33),
    ("method", "RankingService.group", 35, 37),
    ("class", "RankingService.Inner", 39, 41),
    ("method", "RankingService.Inner.values", 40, 40),
    ("interface", "RankingService.Shape", 43, 47),
    ("const", "RankingService.Shape.SIDES", 44, 44),
    ("method", "RankingService.Shape.area", 45, 45),
    ("method", "RankingService.Shape.label", 46, 46),
    ("enum", "RankingService.Color", 49, 54),
    ("field", "RankingService.Color.code", 51, 51),
    ("constructor", "RankingService.Color.Color", 52, 52),
    ("method", "RankingService.Color.code", 53, 53),
    ("record", "Point", 57, 57),
    ("annotation", "Marker", 59, 61),
    ("method", "Marker.value", 60, 60),
]
JAVA_CALLS = [
    ("RankingService.TABLE", "HashMap", None, "new", 11),
    ("RankingService.run", "Runnable", None, "new", 22),
    ("RankingService.run", "refresh", None, "call", 25),
    ("RankingService.run", "fetchIds", None, "call", 28),
    ("RankingService.run", "get", "TABLE", "method", 28),
    ("RankingService.group", "transform", None, "call", 36),
    ("RankingService.group", "size", "value", "method", 36),
]

PYTHON_SYMBOLS = [
    ("import", "__future__ import annotations", 2, 2),
    ("import", "os, sys as system", 4, 4),
    ("import", ".models import ( Candidate, Score, )", 5, 8),
    ("const", "MAX_RESULTS", 10, 10),
    ("const", "_PATTERN", 11, 11),
    ("class", "Weights", 15, 38),
    ("const", "Weights.DEFAULT_WEIGHT", 19, 19),
    ("method", "Weights.__init__", 21, 22),
    ("method", "Weights.total", 24, 26),
    ("method", "Weights.fetch", 28, 35),
    ("class", "Weights.Inner", 37, 38),
    ("method", "Weights.Inner.method", 38, 38),
    ("function", "outer", 41, 51),
    ("function", "outer.inner", 42, 43),
    ("function", "outer.decorated", 45, 47),
    ("function", "main", 54, 56),
]
PYTHON_CALLS = [
    ("_PATTERN", "compile", "re", "method", 11),
    ("Weights.__init__", "normalize", None, "call", 22),
    ("Weights.total", "sum", None, "call", 26),
    ("Weights.total", "values", "self.table", "method", 26),
    ("Weights.fetch", "get", "client", "method", 33),
    ("Weights.Inner.method", "helper", None, "call", 38),
    ("outer", "register", None, "call", 45),
    ("outer", "inner", None, "call", 49),
    ("outer", "extra", None, "call", 50),
    ("main", "session", None, "call", 55),
    ("main", "close", "active", "method", 56),
]

CASES = {
    "rust": (F.RUST, RUST_SYMBOLS, RUST_CALLS),
    "scala": (F.SCALA, SCALA_SYMBOLS, SCALA_CALLS),
    "java": (F.JAVA, JAVA_SYMBOLS, JAVA_CALLS),
    "python": (F.PYTHON, PYTHON_SYMBOLS, PYTHON_CALLS),
}

DECOYS = {
    "rust": ("not_a_function", "Hidden", "fake_inside_string", "fake_raw"),
    "scala": ("notADef", "NotAClass", "fake", "format"),
    "java": ("notAMethod", "NotAClass", "fakeMethod", "FakeClass", "fake"),
    "python": ("not_a_function", "NotAClass", "fake", "not_a_def", "call"),
}


class LanguageFixtureTest(unittest.TestCase):
    def test_symbols_and_line_ranges(self) -> None:
        for language, (text, expected, _) in CASES.items():
            with self.subTest(language=language):
                extraction = LEXICAL.extract(text, language)
                self.assertEqual((extraction.status, extraction.reason), (PARSED, None))
                self.assertEqual(symbols_of(text, language), expected)

    def test_call_candidates(self) -> None:
        for language, (text, _, expected) in CASES.items():
            with self.subTest(language=language):
                self.assertEqual(calls_of(text, language), expected)

    def test_nothing_is_extracted_from_comments_or_strings(self) -> None:
        for language, (text, _, _) in CASES.items():
            extraction = LEXICAL.extract(text, language)
            names = {s.name for s in extraction.symbols} | {c.callee for c in extraction.calls}
            for decoy in DECOYS[language]:
                with self.subTest(language=language, decoy=decoy):
                    self.assertNotIn(decoy, names)

    def test_every_record_is_well_formed(self) -> None:
        for language, (text, _, _) in CASES.items():
            extraction = LEXICAL.extract(text, language)
            line_count = text.count("\n") + (0 if text.endswith("\n") else 1)
            for symbol in extraction.symbols:
                with self.subTest(language=language, symbol=symbol.qualname):
                    self.assertIn(symbol.kind, SYMBOL_KINDS)
                    self.assertTrue(
                        1 <= symbol.start_line <= symbol.name_line <= symbol.end_line <= line_count
                    )
            for call in extraction.calls:
                self.assertIn(extraction.symbols[call.caller].kind, CALLER_KINDS)

    def test_modifiers_and_signatures(self) -> None:
        def find(language: str, qualname: str) -> Symbol:
            text = CASES[language][0]
            return next(s for s in LEXICAL.extract(text, language).symbols if s.qualname == qualname)

        self.assertEqual(find("rust", "ValueScores").modifiers, ("pub",))
        self.assertEqual(find("rust", "Mode").modifiers, ("pub(crate)",))
        self.assertEqual(find("rust", "Mode::is_fast").modifiers, ("pub", "const"))
        self.assertEqual(find("rust", "COUNTER").modifiers, ("mut",))
        self.assertEqual(find("rust", "ValueScores").name_line, 7)  # attribute on line 6
        self.assertEqual(
            find("rust", "ReplyWeight").signature,
            'param!(ReplyWeight, f64, "fixture_reply_weight", 5.0)',
        )
        self.assertEqual(
            find("rust", "ClickWeight").signature,
            'param!( ClickWeight, f64, "fixture_click_weight", 0.3 )',
        )
        self.assertEqual(
            find("rust", "Weighted").signature,
            "impl<'a, T: Clone + Iterator<Item = u32>> Scorer for "
            "crate::weights::Weighted<'a, T> where T: Send + Sync,",
        )
        self.assertEqual(find("scala", "Candidate").modifiers, ("@SerialVersionUID", "final", "case"))
        self.assertEqual(find("scala", "Empty").modifiers, ("case",))
        self.assertEqual(find("scala", "Registry.counter").modifiers, ("private[this]", "var"))
        self.assertEqual(find("scala", "Registry.MaxResults").signature, "final val MaxResults = 100")
        self.assertEqual(
            find("scala", "Candidate.weighted").signature,
            "def weighted(weights: Map[String, Double])(implicit ec: ExecutionContext): Double",
        )
        self.assertEqual(
            find("java", "RankingService").modifiers,
            ("@Deprecated", "@SuppressWarnings", "public", "final"),
        )
        self.assertEqual(find("java", "RankingService.Shape.label").modifiers, ("default",))
        self.assertEqual(
            find("java", "RankingService.CLICK_WEIGHT").signature,
            "public static final double CLICK_WEIGHT = 0.3",
        )
        self.assertEqual(
            find("java", "RankingService.group").signature,
            "public <K, V extends List<K>> Map<K, List<V>> group(Map<K, V> input) throws Exception",
        )
        self.assertEqual(find("python", "Weights.fetch").modifiers, ("@staticmethod", "async"))
        self.assertEqual(find("python", "outer.decorated").modifiers, ("@register", "async"))
        self.assertEqual(find("python", "Weights").name_line, 16)
        self.assertEqual(
            find("python", "Weights.fetch").signature,
            'async def fetch( client, key: str = "def not_a_def():", ) -> dict[str, list[int]]',
        )

    def test_crlf_endings_give_the_same_records(self) -> None:
        for language, (text, expected, calls) in CASES.items():
            crlf = text.replace("\n", "\r\n")
            with self.subTest(language=language):
                self.assertEqual(symbols_of(crlf, language), expected)
                self.assertEqual(calls_of(crlf, language), calls)

    def test_byte_order_mark_is_ignored(self) -> None:
        for language, (text, expected, _) in CASES.items():
            with self.subTest(language=language):
                self.assertEqual(symbols_of("﻿" + text, language), expected)

    def test_malformed_input_never_raises(self) -> None:
        rng = random.Random(20260930)
        for language, (text, _, _) in CASES.items():
            for round_ in range(60):
                cut = rng.randrange(len(text))
                if round_ % 2:
                    mutated = text[:cut]
                else:
                    width = rng.randrange(1, 40)
                    mutated = text[:cut] + text[cut + width :]
                with self.subTest(language=language, round=round_):
                    extraction = LEXICAL.extract(mutated, language)
                    self.assertIn(extraction.status, (PARSED, PARTIAL))
                    count = mutated.count("\n") + 1
                    for symbol in extraction.symbols:
                        self.assertTrue(1 <= symbol.start_line <= symbol.end_line <= count)

    def test_unbalanced_input_is_partial_with_a_reason(self) -> None:
        cases = {
            "rust": "fn open() {\n    let x = 1;\n",
            "scala": 'object O {\n  val s = "unterminated\n',
            "java": "class A {\n  void f() {\n",
            "python": "def f(:\n    pass\n",
        }
        for language, text in cases.items():
            with self.subTest(language=language):
                extraction = LEXICAL.extract(text, language)
                self.assertEqual(extraction.status, PARTIAL)
                self.assertTrue(extraction.reason)
        self.assertEqual(LEXICAL.extract("", "rust"), Extraction(PARSED, None, (), ()))


def gone(segment: str) -> str:
    """What masking leaves of a comment or literal: spaces, newlines kept."""
    return "".join("\n" if char == "\n" else " " for char in segment)


class MaskTest(unittest.TestCase):
    def assertMasked(self, language: str, *parts: tuple[str, bool]) -> None:
        """``parts`` are ``(text, kept)`` segments; blanked segments become spaces."""
        text = "".join(part for part, _ in parts)
        expected = "".join(part if kept else gone(part) for part, kept in parts)
        masked = mask(text, language)
        self.assertEqual(len(masked.code), len(text))
        self.assertEqual(masked.code, expected)

    def test_rust_lifetimes_chars_raw_strings_and_nested_comments(self) -> None:
        self.assertMasked("rust", ("fn f<'a>(x: &'a str) {}", True))
        self.assertMasked("rust", ("let c = ", True), ("'{'", False), ("; let d = ", True),
                          ("b'\\''", False), (";", True))
        self.assertMasked("rust", ('r#"a "b" }"#', False), (" x", True))
        self.assertMasked("rust", ("/* a /* b */ c */", False), (" d", True))
        self.assertMasked("rust", ('"line\nbreak"', False), (" e", True))

    def test_scala_interpolation_and_triple_quotes(self) -> None:
        self.assertMasked("scala", ("s", True), ('"a ${f("}")} b"', False), (" + g", True))
        self.assertMasked("scala", ('"""x "y" """"', False), (" + h", True))
        self.assertMasked("scala", ("'c'", False), (" 'sym", True))

    def test_java_text_blocks_and_comments(self) -> None:
        self.assertMasked("java", ("a = ", True), ('"""\n  x \\""" y\n  """', False), ("; b", True))
        self.assertMasked("java", ("x ", True), ("/* { */", False), (" y ", True), ("// }", False))

    def test_python_prefixes_and_triple_quotes(self) -> None:
        self.assertMasked("python", ("x = ", True), ("rb'a\\'b'", False), (" ", True), ("# c", False))
        self.assertMasked("python", ("f'''a\\''' b'''", False), ("\ny", True))
        masked = mask('x = "open\ny = 1', "python")
        self.assertEqual(masked.problems, ("unterminated-string",))
        self.assertEqual(masked.code.split("\n")[1], "y = 1")

    def test_signature_view_keeps_strings_but_not_comments(self) -> None:
        masked = mask('param!(A, "x") // note', "rust")
        self.assertEqual(masked.nocomment, 'param!(A, "x")        ')
        self.assertEqual(masked.string_spans, ((10, 13),))


class FakeBackend:
    name = "fake"
    version = "9"
    languages = frozenset({"python"})

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason

    def probe(self) -> str | None:
        return self.reason

    def extract(self, text: str, language: str) -> Extraction:
        symbol = Symbol("function", "fake", None, ".", 1, 1, 1, "fake")
        return Extraction(PARSED, None, (symbol,), ())


class BackendInterfaceTest(unittest.TestCase):
    def test_default_registry_uses_the_lexical_fallback(self) -> None:
        for language in ("java", "python", "rust", "scala"):
            with self.subTest(language=language):
                backend = select_backend(language)
                self.assertIsNotNone(backend)
                self.assertEqual(backend_key(backend), "lexical/1")
        self.assertIsNone(select_backend("markdown"))

    def test_tree_sitter_hook_is_registered_but_never_selected(self) -> None:
        registry = default_registry()
        self.assertEqual([b.name for b in registry.backends], ["tree-sitter", "lexical"])
        report = {item["backend"]: item for item in registry.report()}
        self.assertIn("not bundled", report["tree-sitter"]["unavailable_reason"])
        self.assertIsNone(report["lexical"]["unavailable_reason"])
        with self.assertRaises(RuntimeError):
            TreeSitterHook().extract("fn f() {}", "rust")

    def test_an_available_backend_takes_priority_for_its_languages(self) -> None:
        registry = default_registry()
        registry.register(FakeBackend())
        self.assertEqual(registry.select("python").name, "fake")
        self.assertEqual(registry.select("rust").name, "lexical")
        unavailable = default_registry()
        unavailable.register(FakeBackend(reason="grammar missing"))
        self.assertEqual(unavailable.select("python").name, "lexical")

    def test_registry_rejects_objects_that_are_not_backends(self) -> None:
        with self.assertRaises(TypeError):
            Registry().register(object())  # type: ignore[arg-type]

    def test_unknown_language_is_a_programming_error(self) -> None:
        with self.assertRaises(ValueError):
            LEXICAL.extract("x", "cobol")

    def test_status_constants(self) -> None:
        self.assertEqual({PARSED, PARTIAL, FAILED}, {"parsed", "partial", "failed"})


if __name__ == "__main__":
    unittest.main()
