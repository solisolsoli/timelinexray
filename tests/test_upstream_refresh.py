"""Additive acceptance checks for the 2026-10-15 upstream snapshot.

The references are commit-specific and citation-only; byte expectations are checked against
raw ``git show`` output from a local clone, never against extracted or copied upstream files.
"""

from __future__ import annotations

import hashlib
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.run_tests import apply_skip_rule
from timelinexray.findings import goldens
from timelinexray.index import CodeIndex
from timelinexray.netguard import Allowlist
from timelinexray.params import ParamResolver
from timelinexray.snapshot import SnapshotStore
from timelinexray.verify import Verifier
from tests.support import (REPO_ROOT, file_url, git, git_show, ls_tree_paths, oracle_lines,
                           upstream_git_dir)

BASELINE = "78460ca8b65c57ddd3a05f9217c8aaeba214b628"
TARGET = "92f0918f6cb52837cb53a8c790c68f906b2c0db1"
GOLDENS = REPO_ROOT / "goldens" / "upstream-2026-10-15.json"


def local_upstream_git_dir() -> Path | None:
    """Find a caller-provided local clone without imposing the historical release pin."""
    known = upstream_git_dir()
    if known is not None:
        return known
    candidate = Path(os.environ.get("TXRAY_TEST_UPSTREAM") or
                     REPO_ROOT.parent / "x-algorithm-upstream")
    git_dir = candidate / ".git" if (candidate / ".git").is_dir() else candidate
    return git_dir if git_dir.is_dir() else None


UPSTREAM = local_upstream_git_dir()


def has_target(git_dir: Path | None) -> bool:
    if git_dir is None:
        return False
    try:
        git(git_dir, "cat-file", "-e", f"{TARGET}^{{commit}}")
    except AssertionError:
        return False
    return True


TARGET_AVAILABLE = has_target(UPSTREAM)
SKIP_REASON = "local upstream clone does not contain the 2026-10-15 target pin"


class RefreshedGoldenStructureTest(unittest.TestCase):
    def test_file_is_citation_only_and_targets_the_named_snapshots(self) -> None:
        data = json.loads(GOLDENS.read_text("utf-8"))
        self.assertEqual(set(data), {"schema", "upstream", "description", "goldens"})
        self.assertEqual(data["schema"], "timelinexray/goldens/v1")
        refs = goldens.load(GOLDENS)
        self.assertEqual(len(refs), 19)
        self.assertEqual({g.citation.commit for g in refs}, {BASELINE, TARGET})
        self.assertEqual(len({g.id for g in refs}), len(refs))

    def test_missing_target_skip_fails_when_upstream_is_required(self) -> None:
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"TXRAY_TEST_UPSTREAM": "/local/clone"}), \
                contextlib.redirect_stderr(stderr):
            result = apply_skip_rule(True, [("refreshed-upstream", SKIP_REASON)])
        self.assertEqual(result, 1)
        self.assertIn("no test may be skipped", stderr.getvalue())


@unittest.skipUnless(TARGET_AVAILABLE, SKIP_REASON)
class RefreshedUpstreamAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert UPSTREAM is not None
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-upstream-refresh-")
        cls.root = Path(cls._tmp.name)
        cls.url = file_url(UPSTREAM.parent if UPSTREAM.name == ".git" else UPSTREAM)
        cls.store = SnapshotStore(cls.root / "store")
        cls.allowlist = Allowlist([cls.url])
        for commit in (BASELINE, TARGET):
            cls.store.pin(commit, cls.url, allowlist=cls.allowlist)
        cls.index = CodeIndex(cls.store, cls.root / "index")
        cls.index.build(TARGET)
        cls.refs = goldens.load(GOLDENS)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_target_manifest_accounts_for_every_path(self) -> None:
        _, manifest = self.store.load_manifest(TARGET)
        paths = ls_tree_paths(UPSTREAM, TARGET)
        self.assertEqual(len(paths), 2297)
        self.assertEqual(sorted(entry.path for entry in manifest.entries), sorted(paths))
        counts = manifest.counts()
        self.assertEqual(counts["total"], 2297)
        self.assertEqual(counts["classification"],
                         {"parsed-candidate": 1967, "text": 323, "excluded": 7})
        self.assertEqual(counts["excluded_reason"],
                         {"binary": 1, "generated": 5, "oversize": 0,
                          "submodule": 0, "symlink": 1, "vendored": 0})

    def test_golden_bytes_and_anchors_match_independent_git_oracle(self) -> None:
        expected_values = {
            "U15-01": b"0.5", "U15-02": b"0.66",
            "U15-03": b"0.07", "U15-04": b"0.02",
            "U15-05": b"0.004", "U15-06": b"0.006",
            "U15-07": b"-47.52", "U15-08": b"-52.272",
            "U15-12": b"0.5", "U15-13": b"0.5",
            "U15-14": b"0.07", "U15-15": b"0.07",
            "U15-16": b"0.004", "U15-17": b"0.004",
            "U15-18": b"-47.52", "U15-19": b"-47.52",
        }
        for ref in self.refs:
            citation = ref.citation
            with self.subTest(golden=ref.id):
                raw = git_show(UPSTREAM, citation.commit, citation.path)
                lines = oracle_lines(raw)
                span = b"".join(lines[citation.start_line - 1:citation.end_line])
                self.assertEqual(hashlib.sha256(span).hexdigest(), citation.span_sha256)
                self.assertIn(citation.anchor.encode("utf-8"), span)
                self.assertEqual(span.count(citation.anchor.encode("utf-8")), 1)
                if ref.id in expected_values:
                    self.assertIn(expected_values[ref.id], span)
        results = goldens.run(Verifier(self.store), self.refs)
        self.assertEqual([item["id"] for item in results if not item["passed"]], [])

    def test_parameter_lookup_keeps_home_mixer_and_vm_ranker_values_separate(self) -> None:
        resolver = ParamResolver(self.store, self.index)
        expected = {
            "FavoriteWeight": {
                "home-mixer/params/param.rs": "0.66",
                "vm-ranker/params.rs": "0.5",
            },
            "VideoOpenWeight": {
                "home-mixer/params/param.rs": "0.02",
                "vm-ranker/params.rs": "0.07",
            },
            "ContDwellTimeWeight": {
                "home-mixer/params/param.rs": "0.006",
                "vm-ranker/params.rs": "0.004",
            },
            "NotInterestedWeight": {
                "home-mixer/params/param.rs": "-52.272",
                "vm-ranker/params.rs": "-47.52",
            },
        }
        for name, expected_values in expected.items():
            with self.subTest(parameter=name):
                snapshot = resolver.declarations(TARGET, name)
                actual = {declaration.path: declaration.value
                          for declaration in snapshot.declarations}
                self.assertEqual(actual, expected_values)

    def test_new_paths_are_parsed_and_symbols_are_indexed(self) -> None:
        index = self.index
        rows = {row.path: row for row in index.coverage(TARGET).rows}
        expected_paths = {
            "home-mixer/filters/video_carousel_filter.rs": "rust",
            "phoenix/xrex/models/page_rl.py": "python",
            "phoenix/xrex/models/recsys_reply_two_tower_v2_model.py": "python",
        }
        for path, language in expected_paths.items():
            with self.subTest(path=path):
                self.assertEqual(rows[path].language, language)
                self.assertEqual((rows[path].lexical_status, rows[path].syntax_status),
                                 ("indexed", "parsed"))
                self.assertEqual(rows[path].backend, "lexical/1")
        for name, path in (
            ("VideoCarouselFilter", "home-mixer/filters/video_carousel_filter.rs"),
            ("PageRlConfig", "phoenix/xrex/models/page_rl.py"),
            ("REPLY_NEGFB_HEAD_INDICES",
             "phoenix/xrex/models/recsys_reply_two_tower_v2_model.py"),
        ):
            with self.subTest(symbol=name):
                found = index.symbols(TARGET, name=name, limit=100).symbols
                self.assertTrue(any(symbol.path == path for symbol in found),
                                f"{name} was not indexed in {path}")


if __name__ == "__main__":
    unittest.main()
