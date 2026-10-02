"""The code index on the real upstream history, using a local clone via file:// (no network).

Needs a local clone of xai-org/x-algorithm containing 77d431a and its parent a707cc2 (found
through ``$TXRAY_TEST_UPSTREAM`` or at ``../x-algorithm-upstream``); skipped without one.
Build times are measured on the machine running the tests and printed; they are reported,
never asserted.
"""

from __future__ import annotations

import random
import sys
import tempfile
import time
import unittest
from pathlib import Path

from timelinexray.index import CodeIndex
from timelinexray.netguard import Allowlist
from timelinexray.snapshot import SnapshotStore
from tests.index_support import grep, logical_dump, oracle_corpus, query_battery, read_blobs_oracle
from tests.support import UPSTREAM_COMMIT, file_url, upstream_git_dir

UPSTREAM = upstream_git_dir()
PARENT_COMMIT = "a707cc27ba36d3fa79450c9cffcc48a82d080b02"  # 77d431a's parent

FIXED_QUERIES = [
    ("PhoenixScorer", {}),
    ("ReplyWeight", {"limit": 1000}),
    ("param!(", {"limit": 1000}),
    ("fn apply", {"literal": True, "limit": 1000}),
    ("weight", {"path_glob": "home-mixer/*", "limit": 1000}),
    ("import scala", {"literal": True, "limit": 1000}),
    ("def __init__", {"literal": True, "path_glob": "*.py", "limit": 1000}),
    ("ClickWeight", {"limit": 1000}),
    ("public static final", {"literal": True, "limit": 1000}),
]


def _sample_queries(store: SnapshotStore, git_dir: Path, commit: str, count: int, seed: int) -> list[str]:
    """Random substrings of real lines, so most queries have hits."""
    _, manifest = store.load_manifest(commit)
    entries = [e for e in manifest.entries
               if e.classification in ("parsed-candidate", "text") and e.utf8]
    rng = random.Random(seed)
    picked = rng.sample(entries, min(count, len(entries)))
    blobs = read_blobs_oracle(git_dir, [e.oid for e in picked])
    queries = []
    for entry in picked:
        lines = [line.strip() for line in blobs[entry.oid].decode().split("\n") if len(line.strip()) >= 8]
        if not lines:
            continue
        line = rng.choice(lines)
        start = rng.randrange(len(line) - 5)
        queries.append(line[start : start + rng.randrange(4, min(24, len(line) - start) + 1)])
    return queries


@unittest.skipIf(UPSTREAM is None, "no local x-algorithm clone with 77d431a "
                 "(set TXRAY_TEST_UPSTREAM to a local clone)")
class UpstreamIndexTest(unittest.TestCase):
    timings: dict[str, float]

    @classmethod
    def setUpClass(cls) -> None:
        assert UPSTREAM is not None
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-index-upstream-")
        root = Path(cls._tmp.name)
        url = file_url(UPSTREAM.parent if UPSTREAM.name == ".git" else UPSTREAM)
        cls.store = SnapshotStore(root / "store")
        for commit in (PARENT_COMMIT, UPSTREAM_COMMIT):
            cls.store.pin(commit, url, allowlist=Allowlist([url]))
        cls.incremental = CodeIndex(cls.store, root / "incremental")
        cls.clean = CodeIndex(cls.store, root / "clean")
        clock = time.perf_counter
        timings: dict[str, float] = {}
        reports = {}
        start = clock()
        reports["parent_clean"] = cls.incremental.build(PARENT_COMMIT)
        timings["a707cc2 clean (empty index)"] = clock() - start
        start = clock()
        reports["head_incremental"] = cls.incremental.build(UPSTREAM_COMMIT)
        timings["77d431a incremental (after a707cc2)"] = clock() - start
        start = clock()
        reports["head_noop"] = cls.incremental.build(UPSTREAM_COMMIT)
        timings["77d431a again (up to date)"] = clock() - start
        start = clock()
        reports["head_clean"] = cls.clean.build(UPSTREAM_COMMIT)
        timings["77d431a clean (empty index)"] = clock() - start
        start = clock()
        reports["parent_incremental"] = cls.clean.build(PARENT_COMMIT)
        timings["a707cc2 incremental (after 77d431a)"] = clock() - start
        cls.timings = timings
        cls.reports = reports

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_sanity_compute_weighted_score(self) -> None:
        result = self.clean.symbols(UPSTREAM_COMMIT, name="compute_weighted_score")
        found = [(s.path, s.kind) for s in result.symbols]
        self.assertIn(("xai-value-model/scoring.rs", "function"), found)
        symbol = next(s for s in result.symbols if s.path == "xai-value-model/scoring.rs")
        span = self.store.read_span(UPSTREAM_COMMIT, symbol.path, symbol.start_line, symbol.end_line)
        self.assertEqual(symbol.span_sha256, span.sha256)
        self.assertTrue(span.data.startswith(b"pub fn compute_weighted_score("))
        self.assertTrue(span.data.rstrip().endswith(b"}"))

    def test_sanity_reply_weight_param(self) -> None:
        result = self.clean.symbols(UPSTREAM_COMMIT, name="ReplyWeight", kind="param",
                                    path_glob="home-mixer/params/param.rs")
        [symbol] = result.symbols
        span = self.store.read_span(UPSTREAM_COMMIT, symbol.path, symbol.start_line, symbol.end_line)
        self.assertEqual(span.data, b'param!(ReplyWeight, f64, "rust_home_mixer_reply_weight", 5.0);\n')
        self.assertEqual(symbol.span_sha256, span.sha256)

    def test_sanity_search_phoenix_scorer(self) -> None:
        result = self.clean.search(UPSTREAM_COMMIT, "PhoenixScorer", limit=1000)
        paths = {hit.path for hit in result.hits}
        self.assertIn("home-mixer/candidate_pipeline/phoenix_candidate_pipeline.rs", paths)
        for hit in result.hits:
            span = self.store.read_span(UPSTREAM_COMMIT, hit.path, hit.start_line, hit.end_line)
            self.assertEqual(hit.span_sha256, span.sha256)
            self.assertIn(b"phoenixscorer", span.data.lower())

    def test_incremental_equals_clean_on_two_commits(self) -> None:
        for commit in (PARENT_COMMIT, UPSTREAM_COMMIT):
            # totals are compared over all matches; hits (with span reads) up to the limit
            queries = FIXED_QUERIES + [
                (query, {"literal": True, "limit": 200})
                for query in _sample_queries(self.store, UPSTREAM, commit, 25, seed=len(commit))
            ]
            with self.subTest(commit=commit):
                self.assertEqual(query_battery(self.incremental, commit, queries),
                                 query_battery(self.clean, commit, queries))
                self.assertEqual(logical_dump(self.incremental, commit),
                                 logical_dump(self.clean, commit))

    def test_incremental_builds_reuse_unchanged_blobs(self) -> None:
        clean, incremental = self.reports["head_clean"], self.reports["head_incremental"]
        self.assertEqual(clean.lexical_reused + clean.parses_reused, 0)
        self.assertLess(incremental.blobs_read, clean.blobs_read / 10)
        self.assertGreater(incremental.lexical_reused, 1900)
        self.assertTrue(self.reports["head_noop"].up_to_date)
        self.assertEqual(self.reports["head_noop"].blobs_read, 0)
        self.assertEqual((incremental.symbols, incremental.calls), (clean.symbols, clean.calls))

    def test_search_equals_an_independent_grep(self) -> None:
        corpus = oracle_corpus(self.store, UPSTREAM, UPSTREAM_COMMIT)
        queries = _sample_queries(self.store, UPSTREAM, UPSTREAM_COMMIT, 20, seed=77)
        queries += ["PhoenixScorer", "ReplyWeight", "param!(ClickWeight"]
        for query in queries:
            with self.subTest(query=query):
                result = self.clean.search(UPSTREAM_COMMIT, query, literal=True, limit=1000)
                expected = grep(corpus, (query,))
                self.assertEqual(result.total, len(expected))
                self.assertEqual([(h.path, h.start_line) for h in result.hits], expected[:1000])

    def test_coverage_accounts_for_every_path(self) -> None:
        coverage = self.clean.coverage(UPSTREAM_COMMIT)
        summary = coverage.summary
        self.assertEqual(summary["files"], 2147)
        self.assertEqual(summary["lexical"], {"indexed": 2140, "skipped": 7})
        self.assertEqual(summary["lexical_skipped_by_reason"],
                         {"binary": 1, "generated": 5, "symlink": 1})
        self.assertEqual(summary["syntax"], {"not-applicable": 304, "parsed": 1836, "skipped": 7})
        self.assertEqual(summary["syntax_backends"], {"lexical/1": 1836})
        self.assertEqual(
            {name: data["files"] for name, data in summary["languages"].items()},
            {"java": 313, "python": 450, "rust": 505, "scala": 568},
        )

    def test_build_timings_are_measured_and_reported(self) -> None:
        coverage = self.clean.coverage(UPSTREAM_COMMIT).summary
        lines = ["", "code index timings on this machine (measured, not promised):"]
        lines += [f"  {name:<40} {seconds:7.2f} s" for name, seconds in self.timings.items()]
        head = self.reports["head_clean"]
        lines.append(f"  77d431a: {head.lines_inserted} indexed lines, {coverage['symbols']} symbols, "
                     f"{coverage['calls']} call candidates")
        for language, data in coverage["languages"].items():
            lines.append(f"    {language:<7} files {data['files']:>4}  symbols {data['symbols']:>6}"
                         f"  calls {data['calls']:>6}")
        sys.stderr.write("\n".join(lines) + "\n")
        self.assertEqual(len(self.timings), 5)


if __name__ == "__main__":
    unittest.main()
