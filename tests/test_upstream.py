"""Acceptance checks at the pinned upstream commit 77d431a, using a local clone via file://.

These tests never contact the network. They need a local clone of xai-org/x-algorithm that
contains commit 77d431aabf409ca1c1eed9bec7e2183f7c914e23, found through
``$TXRAY_TEST_UPSTREAM`` or at ``../x-algorithm-upstream`` next to this repository; without
one they are skipped.
"""

from __future__ import annotations

import hashlib
import random
import tempfile
import unittest
from pathlib import Path

from timelinexray.netguard import Allowlist
from timelinexray.snapshot import SnapshotStore
from tests.support import (
    UPSTREAM_COMMIT,
    file_url,
    git,
    git_show,
    ls_tree_paths,
    oracle_lines,
    run_cli,
    upstream_git_dir,
)

UPSTREAM = upstream_git_dir()

# Golden numbers for the immutable commit 77d431a under classifier version 1.
EXPECTED_TOTAL = 2147
EXPECTED_CLASSES = {"parsed-candidate": 1836, "text": 304, "excluded": 7}
EXPECTED_REASONS = {"binary": 1, "generated": 5, "oversize": 0, "submodule": 0, "symlink": 1,
                    "vendored": 0}
EXPECTED_EXCLUDED = {
    "media-model-proxy/src/main/resources/sample-image.jpg": "binary",
    "media-model-proxy/src/test/resources/test_decider_base.yml": "symlink",
    "bdsm/runtime/proto_gen/abuse_inference_pb2.py": "generated",
    "phoenix/reference/_sid_proto/sid_lookup_pb2.py": "generated",
    "phoenix/reference/_sid_proto/sid_lookup_pb2_grpc.py": "generated",
    "phoenix/Cargo.lock": "generated",
    "bdsm/rust/accumulator/Cargo.lock": "generated",
}
EXPECTED_LICENSES = {
    "LICENSE": "license",
    "abuse-ledger-service/NOTICE": "notice",
    "phoenix/NOTICE": "notice",
    "phoenix/THIRD_PARTY_NOTICES.md": "third-party-notices",
}


@unittest.skipIf(UPSTREAM is None, "no local x-algorithm clone with 77d431a "
                 "(set TXRAY_TEST_UPSTREAM to a local clone)")
class UpstreamAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert UPSTREAM is not None
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-upstream-")
        cls.store_dir = Path(cls._tmp.name) / "store"
        cls.url = file_url(UPSTREAM.parent if UPSTREAM.name == ".git" else UPSTREAM)
        cls.store = SnapshotStore(cls.store_dir)
        cls.result = cls.store.pin(UPSTREAM_COMMIT[:7], cls.url, allowlist=Allowlist([cls.url]))
        cls.manifest = cls.result.manifest

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_every_path_is_accounted_for(self) -> None:
        tree_paths = ls_tree_paths(UPSTREAM, UPSTREAM_COMMIT)
        self.assertEqual(len(tree_paths), EXPECTED_TOTAL)
        self.assertEqual(len(self.manifest.entries), len(tree_paths))
        self.assertEqual(sorted(e.path for e in self.manifest.entries), sorted(tree_paths))

    def test_classification_counts(self) -> None:
        counts = self.manifest.counts()
        self.assertEqual(counts["total"], EXPECTED_TOTAL)
        self.assertEqual(counts["classification"], EXPECTED_CLASSES)
        self.assertEqual(counts["excluded_reason"], EXPECTED_REASONS)
        excluded = {e.path: e.reason for e in self.manifest.entries if e.reason}
        self.assertEqual(excluded, EXPECTED_EXCLUDED)
        parsed = {e.language for e in self.manifest.entries
                  if e.classification == "parsed-candidate"}
        self.assertEqual(parsed, {"java", "python", "rust", "scala"})

    def test_license_inventory(self) -> None:
        licenses = {item.path: item for item in self.manifest.licenses}
        self.assertEqual({path: item.kind for path, item in licenses.items()}, EXPECTED_LICENSES)
        self.assertIn("Apache-2.0", licenses["LICENSE"].hints)
        self.assertIn("MIT", licenses["abuse-ledger-service/NOTICE"].hints)

    def test_manifest_hashes_match_the_independent_clone(self) -> None:
        for entry in self.manifest.entries[::97]:
            with self.subTest(path=entry.path):
                raw = git(UPSTREAM, "cat-file", "blob", entry.oid)
                self.assertEqual(entry.sha256, hashlib.sha256(raw).hexdigest())
                self.assertEqual(entry.size, len(raw))

    def test_sampled_spans_equal_git_show_bytes(self) -> None:
        readable = [e for e in self.manifest.entries
                    if e.reason not in ("binary", "symlink", "submodule")]
        sample = readable[::23] + [
            self.manifest.entry("bdsm/runtime/action_names.json"),  # no final newline
            self.manifest.entry("botmaker/src/scala/com/twitter/botmaker/FunctionUnitGen.scala"),
            self.manifest.entry("xai-value-model/scoring.rs"),
        ]
        rng = random.Random(77431)
        checked = 0
        for entry in sample:
            original = git_show(UPSTREAM, UPSTREAM_COMMIT, entry.path)
            lines = oracle_lines(original)
            if not lines:
                continue
            count = len(lines)
            ranges = [(1, count)] + [
                tuple(sorted((rng.randint(1, count), rng.randint(1, count)))) for _ in range(3)
            ]
            for start, end in ranges:
                with self.subTest(path=entry.path, lines=f"{start}-{end}"):
                    span = self.store.read_span(UPSTREAM_COMMIT, entry.path, start, end)
                    self.assertEqual(span.data, b"".join(lines[start - 1 : end]))
                    if (start, end) == (1, count):
                        self.assertEqual(span.data, original)
                    checked += 1
        self.assertGreater(checked, 300)

    def test_edge_files(self) -> None:
        with self.assertRaisesRegex(Exception, r"is empty \(0 lines\)"):
            self.store.read_span(UPSTREAM_COMMIT, "bdsm/runtime/__init__.py", 1, 1)
        with self.assertRaisesRegex(Exception, "symlink"):
            self.store.read_span(
                UPSTREAM_COMMIT, "media-model-proxy/src/test/resources/test_decider_base.yml", 1, 1
            )
        last = self.store.read_span(UPSTREAM_COMMIT, "bdsm/runtime/action_names.json", 198, 198)
        self.assertEqual(last.unterminated_lines, 1)

    def test_cli_show_with_anchor(self) -> None:
        code, out, _ = run_cli(["show", UPSTREAM_COMMIT[:7], "xai-value-model/scoring.rs",
                                "--lines", "57-60", "--anchor", "fn apply",
                                "--store", str(self.store_dir)])
        self.assertEqual(code, 0)
        self.assertIn(b"anchor     FOUND at line 57", out)
        body = out.split(b"\n----\n", 1)[1]
        original = git_show(UPSTREAM, UPSTREAM_COMMIT, "xai-value-model/scoring.rs")
        self.assertEqual(body, b"".join(oracle_lines(original)[56:60]))


if __name__ == "__main__":
    unittest.main()
