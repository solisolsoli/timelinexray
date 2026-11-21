"""Additive acceptance checks for the 2026-11-21 upstream snapshot (92f0918..ed78e06).

The references are commit-specific and citation-only; byte expectations are checked against
raw ``git show`` output from a local clone, never against extracted or copied upstream files.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.run_tests import apply_skip_rule
from timelinexray.diff.analysis import make_analysis
from timelinexray.findings import goldens
from timelinexray.index import CodeIndex
from timelinexray.netguard import Allowlist
from timelinexray.params import ParamResolver
from timelinexray.snapshot import SnapshotStore
from timelinexray.verify import Verifier
from tests.support import file_url, git, git_show, ls_tree_paths, oracle_lines, REPO_ROOT
from tests.test_upstream_refresh import local_upstream_git_dir

BASELINE = "92f0918f6cb52837cb53a8c790c68f906b2c0db1"
TARGET = "ed78e0645cfafcd026cfc1fd0da964f630533334"
GOLDENS = REPO_ROOT / "goldens" / "upstream-2026-11-21.json"
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
SKIP_REASON = "local upstream clone does not contain the 2026-11-21 target pin"

# value text expected inside each golden span (public defaults at the cited commit)
EXPECTED_VALUES = {
    "U21-01": b"0.66", "U21-02": b"0.708", "U21-03": b"0.02", "U21-04": b"0.0",
    "U21-05": b"0.3", "U21-06": b"0.274", "U21-07": b"2.0", "U21-08": b"2.118",
    "U21-09": b"0.006", "U21-10": b"0.0075", "U21-11": b"0.4", "U21-12": b"0.385",
    "U21-13": b"-52.272", "U21-14": b"-53.07",
    "U21-15": b"0.5", "U21-16": b"0.07", "U21-17": b"0.3", "U21-18": b"2.0",
    "U21-19": b"0.004", "U21-20": b"0.4", "U21-21": b"-47.52",
    "U21-22": b"multi_risk", "U21-23": b"slot_spread",
    "U21-24": b"true", "U21-25": b"false", "U21-26": b"0.0", "U21-27": b"0.004",
    "U21-28": b"0", "U21-29": b"3", "U21-30": b'"weighted"', "U21-31": b'"weighted"',
    "U21-33": b"-1.0", "U21-34": b"2592000000", "U21-35": b"604800000",
    "U21-36": b"Brazil2026ElectionFilter",
}


class RefreshedGoldenStructureTest(unittest.TestCase):
    def test_file_is_citation_only_and_targets_the_named_snapshots(self) -> None:
        data = json.loads(GOLDENS.read_text("utf-8"))
        self.assertEqual(set(data), {"schema", "upstream", "description", "goldens"})
        self.assertEqual(data["schema"], "timelinexray/goldens/v1")
        refs = goldens.load(GOLDENS)
        self.assertEqual(len(refs), 38)
        self.assertEqual({g.citation.commit for g in refs}, {BASELINE, TARGET})
        self.assertEqual(len({g.id for g in refs}), len(refs))
        self.assertLessEqual(set(EXPECTED_VALUES), {g.id for g in refs})

    def test_missing_target_skip_fails_when_upstream_is_required(self) -> None:
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"TXRAY_TEST_UPSTREAM": "/local/clone"}), \
                contextlib.redirect_stderr(stderr):
            result = apply_skip_rule(True, [("refreshed-upstream-2026-11", SKIP_REASON)])
        self.assertEqual(result, 1)
        self.assertIn("no test may be skipped", stderr.getvalue())


@unittest.skipUnless(TARGET_AVAILABLE, SKIP_REASON)
class RefreshedUpstreamAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert UPSTREAM is not None
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-upstream-refresh-2026-11-")
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
        self.assertEqual(len(paths), 2409)
        self.assertEqual(sorted(entry.path for entry in manifest.entries), sorted(paths))
        counts = manifest.counts()
        self.assertEqual(counts["total"], 2409)
        self.assertEqual(counts["classification"],
                         {"parsed-candidate": 2075, "text": 327, "excluded": 7})
        self.assertEqual(counts["excluded_reason"],
                         {"binary": 1, "generated": 5, "oversize": 0,
                          "submodule": 0, "symlink": 1, "vendored": 0})

    def test_golden_bytes_and_anchors_match_independent_git_oracle(self) -> None:
        for ref in self.refs:
            citation = ref.citation
            with self.subTest(golden=ref.id):
                raw = git_show(UPSTREAM, citation.commit, citation.path)
                lines = oracle_lines(raw)
                span = b"".join(lines[citation.start_line - 1:citation.end_line])
                self.assertEqual(hashlib.sha256(span).hexdigest(), citation.span_sha256)
                self.assertIn(citation.anchor.encode("utf-8"), span)
                if ref.id in EXPECTED_VALUES:
                    self.assertIn(EXPECTED_VALUES[ref.id], span)
        results = goldens.run(Verifier(self.store), self.refs)
        self.assertEqual([item["id"] for item in results if not item["passed"]], [])

    def test_parameter_lookup_keeps_home_mixer_and_vm_ranker_values_separate(self) -> None:
        resolver = ParamResolver(self.store, self.index)
        expected = {
            "FavoriteWeight": ("0.708", "0.5"),
            "VideoOpenWeight": ("0.0", "0.07"),
            "ClickWeight": ("0.274", "0.3"),
            "ShareWeight": ("2.118", "2.0"),
            "ContDwellTimeWeight": ("0.0075", "0.004"),
            "ContClickDwellTimeWeight": ("0.385", "0.4"),
            "NotInterestedWeight": ("-53.07", "-47.52"),
            "PostVideoWeight": ("0.0", "0.0"),
            "GrokAnalyzeWeight": ("0.0", "0.0"),
            "ValueModelMode": ('"weighted"', '"weighted"'),
        }
        for name, (home_mixer, vm_ranker) in expected.items():
            with self.subTest(parameter=name):
                snapshot = resolver.declarations(TARGET, name)
                actual = {declaration.path: declaration.value
                          for declaration in snapshot.declarations}
                self.assertEqual(actual, {"home-mixer/params/param.rs": home_mixer,
                                          "vm-ranker/params.rs": vm_ranker})
        self.assertFalse(resolver.declarations(TARGET, "ReactWithVideoWeight").declarations)

    def test_new_paths_are_parsed_and_symbols_are_indexed(self) -> None:
        rows = {row.path: row for row in self.index.coverage(TARGET).rows}
        for path, name in (
            ("home-mixer/scorers/page_decode_rerank_scorer.rs", "PageDecodeRerankScorer"),
            ("home-mixer/filters/fair_share_ranking_filter.rs", "FairShareRankingFilter"),
            ("home-mixer/filters/zero_fav_codivert_filter.rs", "ZeroFavCodivertFilter"),
            ("xai-value-model/scoring.rs", "multiplicative_score"),
        ):
            with self.subTest(path=path):
                self.assertEqual(rows[path].language, "rust")
                self.assertEqual((rows[path].lexical_status, rows[path].syntax_status),
                                 ("indexed", "parsed"))
                found = self.index.symbols(TARGET, name=name, limit=100).symbols
                self.assertTrue(any(symbol.path == path for symbol in found),
                                f"{name} was not indexed in {path}")
        self.assertNotIn("home-mixer/filters/brazil_2026_election_filter.rs", rows)


@unittest.skipUnless(TARGET_AVAILABLE, SKIP_REASON)
class EnforcementRuleIdentityTest(unittest.TestCase):
    def test_enforcement_rules_pair_by_identity_not_position(self) -> None:
        # Classifier v5 on the real rule file: v4 paired 73 values by list position; the one
        # value that changed for the same rule id is the label time-to-live.
        path = "abuse-enforcement-service/service-lib/rules/enforcement_user.yaml"

        def values(commit: str) -> dict:
            data = git_show(UPSTREAM, commit, path)
            analysis = make_analysis("0" * 40, path, "yaml", data, lambda: None)
            return {decl.key: decl.value for decl in analysis.values}

        old, new = values(BASELINE), values(TARGET)
        changed = sorted((key[1], old[key], new[key]) for key in set(old) & set(new)
                         if old[key] != new[key])
        self.assertEqual(changed, [("rules[id=act_add_llm_slop_label].then.ttl_msec",
                                    "2592000000", "604800000")])


if __name__ == "__main__":
    unittest.main()
