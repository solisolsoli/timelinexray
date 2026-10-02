"""The code index on synthetic repositories: search, symbols, calls, coverage, scoping,
query-injection safety, the backend hook, and incremental builds equal to clean builds."""

from __future__ import annotations

import json
import random
import sqlite3
import unittest
from unittest import mock

from timelinexray.errors import IntegrityError, InvalidInput, NotFound
from timelinexray.index import CodeIndex
from timelinexray.index.query import SNIPPET_MAX, _snippet, fts_expression, parse_query
from timelinexray.index.schema import database_path
from timelinexray.syntax import CALL_RELATION, Extraction, default_registry
from tests.index_support import (
    TwoCommitRepo,
    grep_oracle,
    logical_dump,
    query_battery,
)
from tests.support import FIXTURE_EXPECTED, FixtureRepo, git_show, run_cli
from tests.test_syntax import FakeBackend

REPO: TwoCommitRepo
EDGE: FixtureRepo

BATTERY = [
    ("weight", {}),
    ("alpha OR beta", {}),
    ('"quoted"', {}),
    ("shared_content_marker", {}),
    ("fn apply", {"literal": True}),
    ("score", {"path_glob": "src/*.rs", "limit": 5}),
    ("def", {"path_glob": "*.py"}),
    ("legacy_marker_alpha", {}),
    ("fresh_marker_scorer", {}),
]


def setUpModule() -> None:
    global REPO, EDGE
    REPO = TwoCommitRepo()
    REPO.index("main").build(REPO.commit_a)
    REPO.index("main").build(REPO.commit_b)
    EDGE = FixtureRepo()
    CodeIndex(EDGE.store).build(EDGE.commit)


def tearDownModule() -> None:
    REPO.cleanup()
    EDGE.cleanup()


def hits(result) -> list[tuple[str, int]]:
    return [(hit.path, hit.start_line) for hit in result.hits]


class SearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.index = REPO.index("main")
        self.b = REPO.commit_b

    def test_sanity_every_hit_is_a_verified_span(self) -> None:
        result = self.index.search(self.b, "weight", limit=1000)
        self.assertGreater(result.total, 10)
        for hit in result.hits:
            with self.subTest(path=hit.path, line=hit.start_line):
                span = REPO.store.read_span(self.b, hit.path, hit.start_line, hit.end_line)
                self.assertEqual(hit.commit, self.b)
                self.assertEqual(hit.span_sha256, span.sha256)
                self.assertEqual((hit.start_byte, hit.end_byte), (span.start_byte, span.end_byte))
                self.assertIn("weight", span.data.decode().lower())
                self.assertEqual(hit.snippet, span.data.decode().strip())

    def test_all_terms_on_one_line_case_insensitive(self) -> None:
        self.assertEqual(hits(self.index.search(self.b, "ALPHA")),
                         [("docs/notes.md", 3), ("docs/notes.md", 4)])
        self.assertEqual(hits(self.index.search(self.b, "alone alpha")), [("docs/notes.md", 4)])
        self.assertEqual(hits(self.index.search(self.b, "alone alpha", literal=True)), [])
        self.assertEqual(hits(self.index.search(self.b, "alpha alone", literal=True)),
                         [("docs/notes.md", 4)])

    def test_short_terms_are_checked_on_candidates(self) -> None:
        self.assertEqual(hits(self.index.search(self.b, "alpha OR")), [("docs/notes.md", 3)])
        with self.assertRaisesRegex(InvalidInput, "3 or more characters"):
            self.index.search(self.b, "ab cd")

    def test_results_equal_an_independent_grep(self) -> None:
        rng = random.Random(4242)
        texts = [line for text in (REPO.store.read_blob(self.b, p)[2].decode() for p in (
            "src/scoring.rs", "src/Registry.scala", "src/RankingService.java", "src/weights.py",
            "docs/notes.md")) for line in text.split("\n") if len(line.strip()) >= 6]
        queries = []
        for _ in range(40):
            line = rng.choice(texts).strip()
            start = rng.randrange(len(line) - 3)
            queries.append(line[start : start + rng.randrange(3, min(12, len(line) - start) + 1)])
        for query in queries:
            if not query.strip() or len(query.strip()) < 3:
                continue
            with self.subTest(query=query):
                result = self.index.search(self.b, query, literal=True, limit=1000)
                expected = grep_oracle(REPO.store, REPO.git_dir, self.b, (query,))
                self.assertEqual(hits(result), expected)
                self.assertEqual(result.total, len(expected))

    def test_duplicate_content_is_reported_at_every_path(self) -> None:
        self.assertEqual(hits(self.index.search(self.b, "shared_content_marker")),
                         [("shared/copy.txt", 1), ("shared/same.txt", 1)])

    def test_path_glob_and_limit(self) -> None:
        result = self.index.search(self.b, "weight", path_glob="*.py")
        self.assertTrue(result.hits)
        self.assertTrue(all(hit.path.endswith(".py") for hit in result.hits))
        limited = self.index.search(self.b, "weight", limit=2)
        self.assertEqual(len(limited.hits), 2)
        self.assertTrue(limited.truncated)
        self.assertEqual(limited.total, self.index.search(self.b, "weight", limit=1000).total)
        self.assertEqual(self.index.search(self.b, "weight", path_glob="nothing/*").total, 0)

    def test_crlf_lines(self) -> None:
        [hit] = self.index.search(self.b, "crlf_line_marker").hits
        self.assertEqual(hit.snippet, "first crlf_line_marker")
        span = REPO.store.read_span(self.b, "docs/crlf.txt", 1, 1)
        self.assertEqual(span.data, b"first crlf_line_marker\r\n")
        self.assertEqual(hit.span_sha256, span.sha256)

    def test_excluded_and_non_utf8_files_are_not_searched(self) -> None:
        for query in ("latin_marker", "binary_marker", "to_notes"):
            with self.subTest(query=query):
                self.assertEqual(self.index.search(self.b, query).total, 0)

    def test_repeated_lines_are_separate_hits(self) -> None:
        result = CodeIndex(EDGE.store).search(EDGE.commit, "WEIGHT")
        self.assertEqual(hits(result), [("src/app.py", 1), ("src/app.py", 2), ("src/app.py", 5)])
        self.assertEqual(result.hits[0].span_sha256, result.hits[1].span_sha256)

    def test_long_lines_get_a_bounded_snippet(self) -> None:
        line = "x" * 500 + " needle " + "y" * 500 + "\r\n"
        snippet = _snippet(line, ["needle"])
        self.assertLessEqual(len(snippet), SNIPPET_MAX)
        self.assertIn("needle", snippet)
        self.assertTrue(snippet.startswith("...") and snippet.endswith("..."))
        self.assertEqual(_snippet("  short line\n", ["short"]), "short line")


class QueryInjectionTest(unittest.TestCase):
    """User text is never FTS5 syntax or SQL: every term is a quoted literal."""

    def setUp(self) -> None:
        self.index = REPO.index("main")
        self.b = REPO.commit_b

    def test_fts_operators_are_literal_text(self) -> None:
        for query in ("NEAR(gamma", "col:term", "^start", "-neg", "*star", "{text}:",
                      "alpha OR beta", "NEAR(gamma delta)", "(gamma"):
            with self.subTest(query=query):
                self.assertEqual(hits(self.index.search(self.b, query)), [("docs/notes.md", 3)])
        # AND and NOT are words to find, not operators: no line contains "and" next to these
        self.assertEqual(self.index.search(self.b, "gamma AND delta").total, 0)
        self.assertEqual(self.index.search(self.b, "alpha NOT beta").total, 0)

    def test_quotes_are_part_of_the_term(self) -> None:
        self.assertEqual(
            hits(self.index.search(self.b, '"quoted"')),
            [("docs/notes.md", 3), ("src/Registry.scala", 55), ("src/scoring.rs", 31)],
        )
        self.assertEqual(fts_expression(('say "hi"',)), '"say ""hi"""')
        self.assertEqual(fts_expression(("ab", "abc")), '"abc"')

    def test_unescaped_input_would_have_meant_something_else(self) -> None:
        connection = sqlite3.connect(database_path(self.index.root))
        try:
            raw = connection.execute(
                "SELECT count(*) FROM blob_lines WHERE blob_lines MATCH 'alpha OR beta'"
            ).fetchone()[0]
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("SELECT * FROM blob_lines WHERE blob_lines MATCH 'a\" OR'")
        finally:
            connection.close()
        # as FTS5 syntax, OR matches every line with either word (the table is shared by
        # all indexed blobs); as literal terms, one line of this commit has all three
        self.assertGreaterEqual(raw, 3)
        self.assertEqual(self.index.search(self.b, "alpha OR beta").total, 1)

    def test_sql_text_changes_nothing(self) -> None:
        before = logical_dump(self.index, self.b)
        for query in ("'); DROP TABLE files; --", "x' OR '1'='1", "a\"; DELETE FROM blobs; --"):
            with self.subTest(query=query):
                self.assertEqual(self.index.search(self.b, query).total, 0)
        self.assertEqual(self.index.search(self.b, "weight", path_glob="' OR 1=1 --").total, 0)
        self.assertEqual(logical_dump(self.index, self.b), before)

    def test_invalid_queries_are_rejected_before_querying(self) -> None:
        too_many = " ".join(f"term{i:02d}" for i in range(17))
        for query in ("", "   ", "ab", "x\0yz", "abc\ndef", "a" * 513, too_many):
            with self.subTest(query=query[:20]):
                with self.assertRaises(InvalidInput):
                    parse_query(query)
        with self.assertRaises(InvalidInput):
            self.index.search(self.b, "weight", limit=0)
        with self.assertRaises(InvalidInput):
            self.index.search(self.b, "weight", path_glob="")


class SymbolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.index = REPO.index("main")
        self.b = REPO.commit_b

    def test_filters(self) -> None:
        result = self.index.symbols(self.b, kind="param")
        self.assertEqual([(s.name, s.path, s.start_line) for s in result.symbols],
                         [("ReplyWeight", "src/scoring.rs", 45), ("ClickWeight", "src/scoring.rs", 46)])
        scala = self.index.symbols(self.b, path_glob="*.scala", kind="object")
        self.assertEqual([s.qualname for s in scala.symbols],
                         ["Empty", "Registry", "Registry.Nested"])
        by_qualname = self.index.symbols(self.b, name="Weights.Inner.method")
        self.assertEqual([s.start_line for s in by_qualname.symbols], [38])
        globbed = self.index.symbols(self.b, name="*Weight")
        self.assertEqual(sorted({s.name for s in globbed.symbols}), ["ClickWeight", "ReplyWeight"])
        limited = self.index.symbols(self.b, limit=3)
        self.assertEqual((len(limited.symbols), limited.truncated), (3, True))

    def test_every_symbol_span_hash_matches_the_span_reader(self) -> None:
        result = self.index.symbols(self.b, limit=100_000)
        self.assertGreater(result.total, 80)
        for symbol in result.symbols:
            with self.subTest(symbol=symbol.qualname, path=symbol.path):
                span = REPO.store.read_span(self.b, symbol.path, symbol.start_line, symbol.end_line)
                self.assertEqual(symbol.span_sha256, span.sha256)
                self.assertEqual(symbol.backend, "lexical/1")
                self.assertEqual(symbol.commit, self.b)

    def test_call_candidates_stay_unresolved(self) -> None:
        [symbol] = self.index.symbols(self.b, name="fresh_marker_scorer", with_calls=True).symbols
        self.assertEqual(
            [(c.callee, c.qualifier, c.form, c.line) for c in symbol.calls],
            [("compute_weighted_score", None, "call", 68), ("new", "HashMap", "path", 68)],
        )
        data = symbol.calls[0].to_dict()
        self.assertEqual((data["relation"], data["resolution"]), (CALL_RELATION, "unresolved"))
        total, callers = self.index.calls(self.b, callee="compute_weighted_score")
        self.assertEqual(total, 2)
        self.assertEqual([c.caller for c in callers], ["fresh_marker_scorer", "tests::it_scores"])
        _, from_python = self.index.calls(self.b, caller="new_only_function")
        self.assertEqual([(c.callee, c.path) for c in from_python], [("helper", "src/extra.py")])

    def test_invalid_filters(self) -> None:
        with self.assertRaisesRegex(InvalidInput, "unknown symbol kind"):
            self.index.symbols(self.b, kind="widget")
        with self.assertRaises(InvalidInput):
            self.index.symbols(self.b, name="")


class BlobPathGlobTest(unittest.TestCase):
    """Paths are BLOBs; SQLite built with SQLITE_LIKE_DOESNT_MATCH_BLOBS makes GLOB false for
    a BLOB operand (the Python builds on GitHub's Ubuntu runners). This emulates that build by
    replacing glob() on every connection the index opens; path filters must still match."""

    def test_path_filters_match_on_a_build_where_glob_ignores_blobs(self) -> None:
        connect = sqlite3.connect
        plain = connect(":memory:")

        operands: list[tuple[type, type]] = []

        def strict_glob(pattern: object, value: object) -> int:
            operands.append((type(pattern), type(value)))
            if isinstance(pattern, bytes) or isinstance(value, bytes):
                return 0
            return plain.execute("SELECT ? GLOB ?", (value, pattern)).fetchone()[0]

        def connect_strict(*args: object, **kwargs: object) -> sqlite3.Connection:
            connection = connect(*args, **kwargs)
            connection.create_function("glob", 2, strict_glob, deterministic=True)
            return connection

        b = REPO.commit_b
        with mock.patch("sqlite3.connect", connect_strict):
            index = REPO.index("main")
            self.assertEqual([s.name for s in index.symbols(b, path_glob="src/scoring.rs",
                                                            kind="param").symbols],
                             ["ReplyWeight", "ClickWeight"])
            self.assertGreater(index.search(b, "weight", path_glob="*.py").total, 0)
            self.assertGreater(index.calls(b, path_glob="src/*")[0], 0)
        plain.close()
        # path patterns are bound as bytes (a non-UTF-8 path name is not bindable as text) and
        # cast to TEXT on both sides, so the glob() function only ever sees text operands
        self.assertIn((str, str), operands)
        self.assertEqual(set(operands), {(str, str)})


class CommitScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.index = REPO.index("main")
        self.a, self.b = REPO.commit_a, REPO.commit_b

    def test_text_only_in_one_commit(self) -> None:
        self.assertEqual([(h.commit, h.path) for h in self.index.search(self.a, "legacy_marker_alpha").hits],
                         [(self.a, "docs/old_only.txt")])
        self.assertEqual(self.index.search(self.b, "legacy_marker_alpha").total, 0)
        self.assertEqual(self.index.search(self.a, "fresh_marker_scorer").total, 0)
        self.assertEqual({h.commit for h in self.index.search(self.b, "fresh_marker_scorer").hits}, {self.b})

    def test_shared_blobs_answer_with_the_asking_commit(self) -> None:
        for commit, expected in ((self.a, ["shared/same.txt"]),
                                 (self.b, ["shared/copy.txt", "shared/same.txt"])):
            with self.subTest(commit=commit):
                result = self.index.search(commit, "shared_content_marker")
                self.assertEqual([h.path for h in result.hits], expected)
                self.assertEqual({h.commit for h in result.hits}, {commit})

    def test_exact_name_lookup_uses_the_name_index(self) -> None:
        """An exact ``name`` lookup restricts ``s.name`` to the suffixes of the requested
        name (``IN (...)``), which lets SQLite answer it through ``symbols_by_name``: the
        plain ``name = ? OR qualname = ?`` form made it scan every symbol of the commit
        (0.6 s per lookup on the upstream index, which get_param and param_history repeat
        per pinned commit; 1 ms with the restriction). The planner's choice depends on
        table sizes, so the test checks the statement, not the plan of the tiny fixture."""
        from timelinexray.index import query as index_query
        from timelinexray.index.schema import open_for_read

        statements: list[str] = []
        db = open_for_read(self.index.root)
        try:
            db.set_trace_callback(statements.append)
            found = index_query.symbols(db, REPO.store, self.index.root, self.b, name="ClickWeight")
            qualified = index_query.symbols(db, REPO.store, self.index.root, self.b,
                                            name="Weights.Inner.method")
            db.set_trace_callback(None)
            self.assertEqual([s.qualname for s in found.symbols], ["ClickWeight"])
            self.assertEqual([s.start_line for s in qualified.symbols], [38])
            selects = [text for text in statements if text.lstrip().startswith("SELECT f.path")]
            self.assertEqual(len(selects), 2)
            for sql in selects:
                self.assertIn("s.name IN (", sql)
                self.assertIn(" OR s.qualname = ", sql)
        finally:
            db.close()

    def test_symbols_follow_each_commit(self) -> None:
        def click(commit: str) -> str:
            [symbol] = self.index.symbols(commit, name="ClickWeight").symbols
            return symbol.signature

        self.assertEqual(click(self.a), 'param!( ClickWeight, f64, "fixture_click_weight", 0.3 )')
        self.assertEqual(click(self.b), 'param!( ClickWeight, f64, "fixture_click_weight", 0.25 )')
        self.assertEqual(self.index.symbols(self.a, name="new_only_function").total, 0)
        self.assertEqual(self.index.symbols(self.b, name="new_only_function").total, 1)

    def test_every_commit_scoped_row_carries_its_commit(self) -> None:
        connection = sqlite3.connect(database_path(self.index.root))
        try:
            commits = {row[0] for row in connection.execute("SELECT commit_id FROM files")}
            generations = {row[0] for row in connection.execute("SELECT commit_id FROM generations")}
            null_rows = connection.execute(
                "SELECT count(*) FROM files WHERE commit_id IS NULL OR commit_id = ''"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(commits, {self.a, self.b})
        self.assertEqual(generations, {self.a, self.b})
        self.assertEqual(null_rows, 0)

    def test_unindexed_and_unpinned_commits(self) -> None:
        only_a = REPO.index("only-a")
        only_a.build(self.a)
        with self.assertRaisesRegex(NotFound, "is not indexed.*run: txray index"):
            only_a.search(self.b, "weight")
        with self.assertRaisesRegex(NotFound, "not pinned"):
            only_a.search("abcdef1", "weight")
        with self.assertRaisesRegex(NotFound, "no code index"):
            REPO.index("never-built").symbols(self.a)


class IncrementalBuildTest(unittest.TestCase):
    def test_incremental_equals_clean_for_both_commits(self) -> None:
        incremental = REPO.index("main")  # built A, then B reusing A's blobs
        clean_b = REPO.index("clean-b")
        clean_b.build(REPO.commit_b)
        clean_a = REPO.index("clean-a")
        clean_a.build(REPO.commit_a)
        for commit, clean in ((REPO.commit_b, clean_b), (REPO.commit_a, clean_a)):
            with self.subTest(commit=commit):
                self.assertEqual(query_battery(incremental, commit, BATTERY),
                                 query_battery(clean, commit, BATTERY))
                self.assertEqual(logical_dump(incremental, commit), logical_dump(clean, commit))

    def test_incremental_build_reads_only_new_blobs(self) -> None:
        index = REPO.index("counting")
        first = index.build(REPO.commit_a)
        second = index.build(REPO.commit_b)
        _, manifest_a = REPO.store.load_manifest(REPO.commit_a)
        _, manifest_b = REPO.store.load_manifest(REPO.commit_b)

        def indexable(manifest) -> set[str]:
            return {e.oid for e in manifest.entries
                    if e.classification in ("parsed-candidate", "text") and e.utf8}

        new_blobs = indexable(manifest_b) - indexable(manifest_a)
        self.assertEqual(first.blobs_read, len(indexable(manifest_a)))
        self.assertEqual(second.blobs_read, len(new_blobs))
        self.assertEqual(second.lexical_new, len(new_blobs))
        self.assertEqual(second.lexical_reused, len(indexable(manifest_b)) - len(new_blobs))
        self.assertEqual(second.parses_new, 2)  # src/scoring.rs changed, src/extra.py is new
        self.assertEqual(second.parses_reused, 3)
        again = index.build(REPO.commit_b)
        self.assertTrue(again.up_to_date)
        self.assertEqual((again.blobs_read, again.symbols), (0, second.symbols))

    def test_rebuild_in_place_changes_nothing(self) -> None:
        index = REPO.index("rebuild")
        index.build(REPO.commit_a)
        index.build(REPO.commit_b)
        before = {c: logical_dump(index, c) for c in (REPO.commit_a, REPO.commit_b)}
        report = index.build(REPO.commit_b, rebuild=True)
        self.assertTrue(report.rebuild)
        self.assertEqual(report.parses_reused, 0)
        after = {c: logical_dump(index, c) for c in (REPO.commit_a, REPO.commit_b)}
        self.assertEqual(after, before)

    def test_changed_manifest_is_detected(self) -> None:
        index = REPO.index("stale")
        index.build(REPO.commit_a)
        connection = sqlite3.connect(database_path(index.root))
        connection.execute("UPDATE generations SET manifest_sha256 = ?", ("0" * 64,))
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(IntegrityError, "--rebuild"):
            index.search(REPO.commit_a, "weight")
        self.assertFalse(index.build(REPO.commit_a).up_to_date)
        self.assertEqual(index.search(REPO.commit_a, "weight").commit, REPO.commit_a)


class CoverageTest(unittest.TestCase):
    def test_every_manifest_path_is_accounted_for(self) -> None:
        coverage = CodeIndex(EDGE.store).coverage(EDGE.commit)
        manifest = EDGE.pin_result.manifest
        self.assertEqual([row.path for row in coverage.rows], [e.path for e in manifest.entries])
        for row in coverage.rows:
            classification, reason, language = FIXTURE_EXPECTED[row.path]
            with self.subTest(path=row.path):
                self.assertEqual((row.classification, row.language), (classification, language))
                if classification == "excluded":
                    self.assertEqual((row.lexical_status, row.lexical_reason), ("skipped", reason))
                    self.assertEqual((row.syntax_status, row.syntax_reason), ("skipped", reason))
                elif row.path == "edge/latin1.txt":
                    self.assertEqual((row.lexical_status, row.lexical_reason), ("skipped", "not-utf8"))
                elif classification == "text":
                    self.assertEqual((row.lexical_status, row.syntax_status), ("indexed", "not-applicable"))
                else:
                    self.assertEqual((row.lexical_status, row.syntax_status), ("indexed", "parsed"))
                    self.assertEqual(row.backend, "lexical/1")
        summary = coverage.summary
        self.assertEqual(summary["files"], len(manifest.entries))
        self.assertEqual(summary["lexical_skipped_by_reason"],
                         {"binary": 2, "generated": 3, "not-utf8": 1, "oversize": 1,
                          "submodule": 1, "symlink": 2, "vendored": 2})
        self.assertEqual(summary["syntax_backends"], {"lexical/1": 12})

    def test_edge_files(self) -> None:
        coverage = {row.path: row for row in CodeIndex(EDGE.store).coverage(EDGE.commit).rows}
        self.assertEqual((coverage["edge/empty.py"].line_count, coverage["edge/empty.py"].symbols), (0, 0))
        self.assertEqual(coverage["edge/blank_lines.py"].indexed_lines, 1)
        index = CodeIndex(EDGE.store)
        names = {(s.path, s.kind, s.name, s.start_line, s.end_line)
                 for s in index.symbols(EDGE.commit, limit=1000).symbols}
        self.assertLessEqual({
            ("crlf/mixed.rs", "function", "a", 1, 1),
            ("crlf/mixed.rs", "function", "c", 3, 3),
            ("crlf/no_final_newline.scala", "object", "B", 2, 2),
            ("edge/no_newline.java", "class", "A", 1, 1),
            ("src/app.py", "function", "score", 4, 5),
            ("src/lib.rs", "const", "CLICK", 1, 1),
            ("src/types.pyi", "function", "score", 1, 1),
        }, names)
        for symbol in index.symbols(EDGE.commit, limit=1000).symbols:
            span = EDGE.store.read_span(EDGE.commit, symbol.path, symbol.start_line, symbol.end_line)
            self.assertEqual(symbol.span_sha256, span.sha256)
        [bom] = index.search(EDGE.commit, "print('bom')").hits
        self.assertEqual(bom.span_sha256, EDGE.store.read_span(EDGE.commit, "edge/bom.py", 1, 1).sha256)
        self.assertEqual(git_show(EDGE.git_dir, EDGE.commit, "edge/bom.py")[:3], b"\xef\xbb\xbf")


class BackendHookTest(unittest.TestCase):
    def test_a_registered_backend_is_used_and_reported(self) -> None:
        registry = default_registry()
        registry.register(FakeBackend())
        index = REPO.index("fake", registry=registry)
        index.build(REPO.commit_b)
        backends = {row.path: row.backend for row in index.coverage(REPO.commit_b).rows if row.backend}
        self.assertEqual(backends["src/weights.py"], "fake/9")
        self.assertEqual(backends["src/scoring.rs"], "lexical/1")
        python = index.symbols(REPO.commit_b, path_glob="*.py").symbols
        self.assertEqual({(s.name, s.backend) for s in python}, {("fake", "fake/9")})
        # switching back re-parses only the Python blobs; lexical rows are reused
        lexical = CodeIndex(REPO.store, index.root)
        report = lexical.build(REPO.commit_b)
        self.assertFalse(report.up_to_date)
        self.assertEqual((report.parses_new, report.lexical_new), (2, 0))
        self.assertEqual(logical_dump(lexical, REPO.commit_b), logical_dump(REPO.index("main"), REPO.commit_b))

    def test_a_failing_backend_is_reported_not_fatal(self) -> None:
        class Broken(FakeBackend):
            name = "broken"

            def extract(self, text: str, language: str) -> Extraction:
                raise RuntimeError("synthetic failure")

        registry = default_registry()
        registry.register(Broken())
        index = REPO.index("broken", registry=registry)
        index.build(REPO.commit_b)
        row = next(r for r in index.coverage(REPO.commit_b).rows if r.path == "src/weights.py")
        self.assertEqual((row.syntax_status, row.backend, row.symbols), ("failed", "broken/9", 0))
        self.assertIn("RuntimeError: synthetic failure", row.syntax_reason)
        self.assertEqual(index.search(REPO.commit_b, "DEFAULT_WEIGHT").total, 1)


class DatabaseTest(unittest.TestCase):
    def test_an_interrupted_build_leaves_no_partial_generation(self) -> None:
        class Interrupting(FakeBackend):
            name = "interrupting"

            def extract(self, text: str, language: str) -> Extraction:
                raise KeyboardInterrupt  # not an Exception: it must abort the build

        index = REPO.index("interrupted")
        index.build(REPO.commit_a)
        before = logical_dump(index, REPO.commit_a)
        registry = default_registry()
        registry.register(Interrupting())
        with self.assertRaises(KeyboardInterrupt):
            CodeIndex(REPO.store, index.root, registry=registry).build(REPO.commit_b)
        with self.assertRaisesRegex(NotFound, "is not indexed"):
            index.generation(REPO.commit_b)
        connection = sqlite3.connect(database_path(index.root))
        try:
            parses = connection.execute(
                "SELECT count(*) FROM parses WHERE backend = 'interrupting'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(parses, 0)
        self.assertEqual(logical_dump(index, REPO.commit_a), before)
        self.assertEqual(index.build(REPO.commit_b).commit, REPO.commit_b)

    def test_unknown_schema_version_is_refused(self) -> None:
        index = REPO.index("versioned")
        index.build(REPO.commit_a)
        connection = sqlite3.connect(database_path(index.root))
        connection.execute("PRAGMA user_version = 99")
        connection.close()
        with self.assertRaisesRegex(IntegrityError, "schema version 99"):
            index.search(REPO.commit_a, "weight")
        with self.assertRaisesRegex(IntegrityError, "schema version 99"):
            index.build(REPO.commit_a)


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        CodeIndex(REPO.store).build(REPO.commit_b)  # the store's default index location

    def store_args(self) -> list[str]:
        return ["--store", str(REPO.store_dir)]

    def test_index_search_symbols_json(self) -> None:
        code, out, err = run_cli(["index", REPO.commit_b[:10], "--coverage", "--json", *self.store_args()])
        self.assertEqual((code, err), (0, b""))
        doc = json.loads(out)
        self.assertEqual((doc["command"], doc["outcome"]), ("index", "ok"))
        self.assertEqual(len(doc["data"]["coverage"]["files"]), len(REPO.store.load_manifest(REPO.commit_b)[1].entries))
        code, out, _ = run_cli(["search", REPO.commit_b, "PhoenixScorer", "--json", *self.store_args()])
        self.assertEqual(json.loads(out)["data"]["total"], 0)
        code, out, _ = run_cli(["search", REPO.commit_b, "reply_weight_for", "--json", *self.store_args()])
        [hit] = json.loads(out)["data"]["hits"]
        self.assertEqual((hit["path"], hit["start_line"], hit["end_line"], hit["commit"]),
                         ("src/scoring.rs", 32, 32, REPO.commit_b))
        self.assertEqual(hit["span_sha256"], REPO.store.read_span(REPO.commit_b, "src/scoring.rs", 32, 32).sha256)
        code, out, _ = run_cli(["symbols", REPO.commit_b, "--kind", "param", "--name", "Click*", "--json",
                                *self.store_args()])
        [symbol] = json.loads(out)["data"]["symbols"]
        self.assertEqual((symbol["name"], symbol["backend"], symbol["start_line"], symbol["end_line"]),
                         ("ClickWeight", "lexical/1", 46, 51))

    def test_human_output(self) -> None:
        code, out, _ = run_cli(["index", REPO.commit_b, *self.store_args()])
        text = out.decode()
        self.assertEqual(code, 0)
        self.assertIn("generation  up to date", text)
        self.assertIn("unresolved call candidates", text)
        self.assertIn("unavailable tree-sitter", text)
        code, out, _ = run_cli(["search", *self.store_args(), REPO.commit_b, "--", "-neg"])
        self.assertEqual(code, 0)
        self.assertRegex(out.decode(), r"docs/notes\.md:3  sha256:[0-9a-f]{16}  alpha OR beta")
        code, out, _ = run_cli(["symbols", REPO.commit_b, "--name", "compute_weighted_score", "--calls",
                                *self.store_args()])
        self.assertIn("function    compute_weighted_score  src/scoring.rs:59-65  [lexical/1]", out.decode())
        self.assertIn(f"{CALL_RELATION} inner_apply (call) line 63, unresolved", out.decode())

    def test_errors_and_exit_codes(self) -> None:
        cases = [
            (["search", REPO.commit_b, "ab"], 2, b"3 or more characters"),
            (["search", REPO.commit_b, "weight", "--limit", "0"], 2, b"--limit"),
            (["symbols", REPO.commit_b, "--kind", "widget"], 2, b"--kind"),
            (["search", "abcdef1", "weight"], 1, b"is not pinned"),
            (["index", "nothex"], 2, b"invalid commit"),
        ]
        for args, expected, message in cases:
            with self.subTest(args=args):
                code, out, err = run_cli([*args, *self.store_args()])
                self.assertEqual(code, expected)
                self.assertIn(message, out + err)
        code, out, err = run_cli(["search", REPO.commit_a, "weight", "--json",
                                  "--store", str(REPO.root / "empty-store")])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["error"]["code"], "not_found")


class PublicDefaultFooterTest(unittest.TestCase):
    """FA-013: search, symbols and show say "public default" next to the numbers they show."""

    @classmethod
    def setUpClass(cls) -> None:
        CodeIndex(REPO.store).build(REPO.commit_b)  # the store's default index location

    def store_args(self) -> list[str]:
        return ["--store", str(REPO.store_dir)]

    def test_search_footer_when_numbers_appear(self) -> None:
        commit = REPO.commit_b
        code, out, _ = run_cli(["search", commit, "fixture_reply_weight", *self.store_args()])
        self.assertEqual(code, 0)
        self.assertIn(b"5.0", out)
        self.assertTrue(out.decode().rstrip().endswith(
            f"note     numbers in the output above are public defaults at commit {commit}, "
            "not production values"), out)
        code, out, _ = run_cli(["search", commit, "fixture_reply_weight", "--json", *self.store_args()])
        self.assertIn("public defaults at commit " + commit, json.loads(out)["note"])
        code, out, _ = run_cli(["search", commit, "alpha alone", "--literal", *self.store_args()])
        self.assertEqual(code, 0)
        self.assertNotIn(b"public default", out)
        code, out, _ = run_cli(["search", commit, "alpha alone", "--literal", "--json",
                                *self.store_args()])
        self.assertIsNone(json.loads(out)["note"])

    def test_symbols_footer_when_numbers_appear(self) -> None:
        commit = REPO.commit_b
        code, out, _ = run_cli(["symbols", commit, "--name", "ClickWeight", *self.store_args()])
        self.assertEqual(code, 0)
        self.assertIn(f"note     numbers in the output above are public defaults at commit {commit}",
                      out.decode())
        code, out, _ = run_cli(["symbols", commit, "--name", "ClickWeight", "--json", *self.store_args()])
        self.assertIn("public defaults at commit " + commit, json.loads(out)["note"])
        code, out, _ = run_cli(["symbols", commit, "--name", "new_only_function", *self.store_args()])
        self.assertEqual(code, 0)
        self.assertNotIn(b"public default", out)

    def test_show_header_note_when_numbers_appear(self) -> None:
        commit = REPO.commit_b
        code, out, _ = run_cli(["show", commit, "src/scoring.rs", "--lines", "46-51", *self.store_args()])
        self.assertEqual(code, 0)
        header, body = out.split(b"\n----\n", 1)
        self.assertIn(f"note       numbers in the output above are public defaults at commit {commit}, "
                      "not production values".encode(), header)
        self.assertNotIn(b"public default", body)
        code, out, _ = run_cli(["show", commit, "src/scoring.rs", "--lines", "46-51", "--json",
                                *self.store_args()])
        self.assertIn("public defaults at commit " + commit, json.loads(out)["note"])
        code, out, _ = run_cli(["show", commit, "docs/notes.md", "--lines", "4", *self.store_args()])
        self.assertEqual(code, 0)
        self.assertNotIn(b"public default", out)
        code, out, _ = run_cli(["show", commit, "src/scoring.rs", "--lines", "46-51", "--raw",
                                *self.store_args()])
        self.assertNotIn(b"public default", out)


if __name__ == "__main__":
    unittest.main()
