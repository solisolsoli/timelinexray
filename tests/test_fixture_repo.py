"""Snapshot store end to end on a synthetic repository with CRLF, edge-case and excluded files."""

from __future__ import annotations

import hashlib
import json
import shutil
import unittest

from timelinexray.errors import IntegrityError, InvalidInput, NetworkRefused, NotFound, Refused
from timelinexray.netguard import Allowlist
from timelinexray.snapshot import (
    CLASSIFICATIONS,
    MANIFEST_SCHEMA,
    REASONS,
    ClassifierConfig,
    Manifest,
    SnapshotStore,
    build_manifest,
)
from timelinexray.gitio import GitRepo
from tests.support import (
    FIXTURE_EXPECTED,
    FIXTURE_FILES,
    REPO_ROOT,
    FixtureRepo,
    Gitlink,
    Symlink,
    build_fixture_repo,
    file_url,
    git,
    git_show,
    ls_tree_paths,
    oracle_lines,
)

FIXTURE: FixtureRepo


def setUpModule() -> None:
    global FIXTURE
    FIXTURE = FixtureRepo()


def tearDownModule() -> None:
    FIXTURE.cleanup()


def _readable_paths() -> list[str]:
    return [
        path for path, (cls, reason, _) in FIXTURE_EXPECTED.items()
        if reason not in ("binary", "symlink", "submodule")
    ]


class ManifestAccountingTest(unittest.TestCase):
    def test_every_path_is_accounted_for(self) -> None:
        manifest = FIXTURE.pin_result.manifest
        tree_paths = ls_tree_paths(FIXTURE.git_dir, FIXTURE.commit)
        self.assertEqual(len(manifest.entries), len(tree_paths))
        self.assertEqual(sorted(e.path for e in manifest.entries), sorted(tree_paths))
        self.assertEqual(set(tree_paths), set(FIXTURE_FILES))

    def test_classification_of_each_fixture_path(self) -> None:
        manifest = FIXTURE.pin_result.manifest
        for path, expected in FIXTURE_EXPECTED.items():
            with self.subTest(path=path):
                entry = manifest.entry(path)
                self.assertIsNotNone(entry)
                self.assertEqual((entry.classification, entry.reason, entry.language), expected)
                self.assertEqual(entry.rule is None, entry.reason is None)

    def test_blob_identity_size_and_sha256(self) -> None:
        manifest = FIXTURE.pin_result.manifest
        for entry in manifest.entries:
            with self.subTest(path=entry.path):
                content = FIXTURE_FILES[entry.path]
                if isinstance(content, Gitlink):
                    self.assertEqual((entry.type, entry.mode, entry.oid), ("commit", "160000", content))
                    self.assertIsNone(entry.sha256)
                    continue
                data = content.encode("utf-8") if isinstance(content, Symlink) else content
                self.assertEqual(entry.size, len(data))
                self.assertEqual(entry.sha256, hashlib.sha256(data).hexdigest())
                raw = git(FIXTURE.git_dir, "cat-file", "blob", entry.oid)
                self.assertEqual(raw, data)

    def test_modes_and_utf8_flags(self) -> None:
        manifest = FIXTURE.pin_result.manifest
        self.assertEqual(manifest.entry("scripts/run.sh").mode, "100755")
        self.assertEqual(manifest.entry("link/escape").mode, "120000")
        self.assertFalse(manifest.entry("edge/latin1.txt").utf8)
        self.assertTrue(manifest.entry("edge/bom.py").utf8)
        self.assertTrue(manifest.entry("unicode/naïve.txt").utf8)

    def test_counts(self) -> None:
        counts = FIXTURE.pin_result.manifest.counts()
        self.assertEqual(counts["total"], len(FIXTURE_FILES))
        self.assertEqual(sum(counts["classification"].values()), counts["total"])
        excluded = counts["classification"]["excluded"]
        self.assertEqual(sum(counts["excluded_reason"].values()), excluded)
        self.assertEqual(
            counts["excluded_reason"],
            {"binary": 2, "generated": 3, "oversize": 1, "submodule": 1, "symlink": 2, "vendored": 2},
        )
        self.assertEqual(list(counts["classification"]), list(CLASSIFICATIONS))
        self.assertEqual(list(counts["excluded_reason"]), list(REASONS))

    def test_license_inventory(self) -> None:
        licenses = {item.path: item for item in FIXTURE.pin_result.manifest.licenses}
        self.assertEqual(set(licenses), {"LICENSE", "pkg/NOTICE"})
        self.assertEqual(licenses["LICENSE"].kind, "license")
        self.assertEqual(licenses["LICENSE"].hints, ("Apache-2.0",))
        self.assertEqual(licenses["pkg/NOTICE"].hints, ("MIT",))

    def test_manifest_is_deterministic_and_round_trips(self) -> None:
        repo = FIXTURE.store.repo_for(FIXTURE.pin_result.pin)
        first = build_manifest(repo, FIXTURE.commit).to_json_bytes()
        second = build_manifest(repo, FIXTURE.commit).to_json_bytes()
        self.assertEqual(first, second)
        stored = (FIXTURE.store_dir / FIXTURE.pin_result.pin.manifest).read_bytes()
        self.assertEqual(stored, first)
        self.assertEqual(Manifest.from_json_bytes(stored), FIXTURE.pin_result.manifest)
        self.assertTrue(stored.endswith(b"\n") and stored.isascii())

    def test_sidecar_is_shasum_compatible(self) -> None:
        pin = FIXTURE.pin_result.pin
        path = FIXTURE.store_dir / pin.manifest
        sidecar = path.with_name(path.name + ".sha256").read_text("ascii")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(sidecar, f"{digest}  {path.name}\n")
        self.assertEqual(pin.manifest_sha256, digest)

    def test_schema_file_agrees_with_the_code(self) -> None:
        schema = json.loads((REPO_ROOT / "schemas" / "manifest.schema.json").read_text("utf-8"))
        data = FIXTURE.pin_result.manifest.to_dict()
        self.assertEqual(schema["properties"]["schema"]["const"], MANIFEST_SCHEMA)
        self.assertEqual(set(schema["required"]), set(data))
        entry_schema = schema["$defs"]["entry"]
        self.assertEqual(set(entry_schema["required"]), set(data["entries"][0]))
        self.assertEqual(entry_schema["properties"]["classification"]["enum"], list(CLASSIFICATIONS))
        self.assertEqual(entry_schema["properties"]["reason"]["enum"], [*REASONS, None])
        self.assertEqual(set(schema["$defs"]["license"]["required"]), set(data["licenses"][0]))


class SpanTest(unittest.TestCase):
    def test_spans_reproduce_git_show_bytes(self) -> None:
        for path in _readable_paths():
            original = git_show(FIXTURE.git_dir, FIXTURE.commit, path)
            lines = oracle_lines(original)
            count = len(lines)
            if count == 0:
                continue
            with self.subTest(path=path, lines=f"1-{count}"):
                span = FIXTURE.store.read_span(FIXTURE.commit, path, 1, count)
                self.assertEqual(span.data, original)
                self.assertEqual(span.blob_sha256, hashlib.sha256(original).hexdigest())
            if count > 50:
                ranges = [(1, 1), (2, 7), (count - 3, count), (count // 2, count // 2 + 1)]
            else:
                ranges = [(a, b) for a in range(1, count + 1) for b in range(a, count + 1)]
            for start, end in ranges:
                with self.subTest(path=path, lines=f"{start}-{end}"):
                    span = FIXTURE.store.read_span(FIXTURE.commit, path, start, end)
                    self.assertEqual(span.data, b"".join(lines[start - 1 : end]))

    def test_crlf_file(self) -> None:
        span = FIXTURE.store.read_span(FIXTURE.commit, "crlf/windows.txt", 2, 3, anchor="beta\r\n")
        self.assertEqual(span.data, b"beta\r\ngamma\r\n")
        self.assertEqual((span.start_byte, span.end_byte), (7, 20))
        self.assertEqual(span.crlf_lines, 2)
        self.assertEqual(span.sha256, hashlib.sha256(b"beta\r\ngamma\r\n").hexdigest())
        self.assertEqual((span.anchor.verdict, span.anchor.lines), ("FOUND", (2,)))

    def test_crlf_without_final_newline(self) -> None:
        span = FIXTURE.store.read_span(FIXTURE.commit, "crlf/no_final_newline.scala", 1, 2)
        self.assertEqual(span.data, b"object A\r\nobject B")
        self.assertEqual(span.unterminated_lines, 1)

    def test_past_end_of_file_is_a_clear_error(self) -> None:
        with self.assertRaises(Exception) as ctx:
            FIXTURE.store.read_span(FIXTURE.commit, "crlf/windows.txt", 3, 4)
        self.assertEqual(
            str(ctx.exception),
            "line range 3-4 is past end of file: crlf/windows.txt has 3 lines",
        )

    def test_empty_file(self) -> None:
        with self.assertRaisesRegex(Exception, r"edge/empty\.py is empty \(0 lines\)"):
            FIXTURE.store.read_span(FIXTURE.commit, "edge/empty.py", 1, 1)

    def test_refused_entries(self) -> None:
        for path, word in (("link/to_readme", "symlink"), ("link/escape", "symlink"),
                           ("bin/data.bin", "binary"), ("sub/module", "submodule")):
            with self.subTest(path=path):
                with self.assertRaisesRegex(Refused, word):
                    FIXTURE.store.read_span(FIXTURE.commit, path, 1, 1)

    def test_invalid_and_missing_paths(self) -> None:
        for path in ("/etc/passwd", "../README.md", "src/../README.md", "./README.md",
                     "src//lib.rs", "src/", ""):
            with self.subTest(path=path):
                with self.assertRaises(InvalidInput):
                    FIXTURE.store.read_span(FIXTURE.commit, path, 1, 1)
        with self.assertRaisesRegex(NotFound, "not in the manifest"):
            FIXTURE.store.read_span(FIXTURE.commit, "readme.md", 1, 1)

    def test_excluded_text_blobs_remain_readable(self) -> None:
        span = FIXTURE.store.read_span(FIXTURE.commit, "big/huge.log", 1, 1)
        self.assertEqual(span.data, b"x" * 63 + b"\n")
        self.assertEqual(FIXTURE.store.read_span(FIXTURE.commit, "Cargo.lock", 2, 2).data,
                         b"version = 4\n")

    def test_commit_prefix_lookup(self) -> None:
        span = FIXTURE.store.read_span(FIXTURE.commit[:7], "src/lib.rs", 1, 1)
        self.assertEqual(span.commit, FIXTURE.commit)
        with self.assertRaisesRegex(NotFound, "txray pin"):
            FIXTURE.store.read_span("0000000", "src/lib.rs", 1, 1)


class PinTest(unittest.TestCase):
    def test_pin_protects_the_commit_with_a_ref(self) -> None:
        pin = FIXTURE.pin_result.pin
        refs = git(FIXTURE.store_dir / pin.mirror, "for-each-ref", "--format=%(refname) %(objectname)")
        self.assertIn(f"refs/txray/pins/{FIXTURE.commit} {FIXTURE.commit}".encode(), refs)
        self.assertTrue(FIXTURE.pin_result.fetched)
        self.assertTrue(FIXTURE.pin_result.created)

    def test_repin_is_idempotent_and_offline(self) -> None:
        pin_file = FIXTURE.store_dir / "pins" / f"{FIXTURE.commit}.json"
        before = pin_file.read_bytes()
        again = FIXTURE.store.pin(FIXTURE.commit[:10], FIXTURE.url, allowlist=FIXTURE.allowlist)
        self.assertFalse(again.fetched)
        self.assertFalse(again.created)
        self.assertEqual(again.pin, FIXTURE.pin_result.pin)
        self.assertEqual(pin_file.read_bytes(), before)

    def test_unknown_commit_and_branch_names(self) -> None:
        store = FIXTURE.new_store("store-unknown")
        with self.assertRaisesRegex(NotFound, "was not found"):
            store.pin("deadbeef" * 5, FIXTURE.url, allowlist=FIXTURE.allowlist)
        for bad in ("main", "HEAD", "abc", "77d431a^", "--all"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidInput):
                    store.pin(bad, FIXTURE.url, allowlist=FIXTURE.allowlist)

    def test_non_allowlisted_url_touches_nothing(self) -> None:
        store = FIXTURE.new_store("store-refused")
        with self.assertRaises(NetworkRefused):
            store.pin(FIXTURE.commit, FIXTURE.url, allowlist=Allowlist())
        self.assertFalse(store.root.exists())

    def test_same_commit_from_another_upstream_is_refused(self) -> None:
        other_dir, other_commit = build_fixture_repo(
            FIXTURE.root / "copy", FIXTURE_FILES, frozenset({"scripts/run.sh"})
        )
        self.assertEqual(other_commit, FIXTURE.commit)  # the fixture is fully deterministic
        other_url = file_url(other_dir)
        allowlist = Allowlist([FIXTURE.url, other_url])
        with self.assertRaisesRegex(Refused, "already pinned"):
            FIXTURE.store.pin(FIXTURE.commit, other_url, allowlist=allowlist)
        self.assertFalse(FIXTURE.store.mirror_path(other_url).exists())

    def test_small_classifier_limits(self) -> None:
        store = FIXTURE.new_store("store-small")
        result = store.pin(FIXTURE.commit, FIXTURE.url, allowlist=FIXTURE.allowlist,
                           classifier=ClassifierConfig(max_file_bytes=40))
        self.assertEqual(result.manifest.entry("src/app.py").reason, "oversize")
        self.assertEqual(result.manifest.entry("src/lib.rs").reason, "oversize")
        self.assertEqual(result.manifest.entry("README.md").reason, "oversize")
        self.assertEqual(result.manifest.classifier["max_file_bytes"], 40)


class TamperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = FIXTURE.root / f"tamper-{self._testMethodName}"
        shutil.copytree(FIXTURE.store_dir, self.dir)
        self.store = SnapshotStore(self.dir)
        self.pin = FIXTURE.pin_result.pin
        self.manifest_path = self.dir / self.pin.manifest

    def test_edited_manifest_is_detected(self) -> None:
        data = self.manifest_path.read_bytes().replace(b'"rust"', b'"java"', 1)
        self.manifest_path.write_bytes(data)
        with self.assertRaisesRegex(IntegrityError, "sha256"):
            self.store.load_manifest(FIXTURE.commit)

    def test_edited_sidecar_is_detected(self) -> None:
        sidecar = self.manifest_path.with_name(self.manifest_path.name + ".sha256")
        sidecar.write_text("0" * 64 + "  x.json\n", "ascii")
        with self.assertRaises(IntegrityError):
            self.store.load_manifest(FIXTURE.commit)

    def test_blob_that_disagrees_with_its_manifest_entry_is_refused(self) -> None:
        raw = json.loads(self.manifest_path.read_bytes())
        for entry in raw["entries"]:
            if entry["path"] == "src/lib.rs":
                entry["sha256"] = "0" * 64
        data = (json.dumps(raw, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode()
        self.manifest_path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        self.manifest_path.with_name(self.manifest_path.name + ".sha256").write_text(
            f"{digest}  {self.manifest_path.name}\n", "ascii")
        pin_path = self.dir / "pins" / f"{FIXTURE.commit}.json"
        record = json.loads(pin_path.read_text("ascii"))
        record["manifest_sha256"] = digest
        pin_path.write_text(json.dumps(record), "ascii")
        with self.assertRaisesRegex(IntegrityError, "does not match its manifest entry"):
            self.store.read_span(FIXTURE.commit, "src/lib.rs", 1, 1)

    def test_repin_detects_a_stored_manifest_that_differs(self) -> None:
        self.manifest_path.write_bytes(self.manifest_path.read_bytes() + b" ")
        with self.assertRaisesRegex(IntegrityError, "differs from the manifest rebuilt"):
            self.store.pin(FIXTURE.commit, FIXTURE.url, allowlist=FIXTURE.allowlist)

    def test_mirror_belonging_to_another_upstream_is_refused(self) -> None:
        mirror = GitRepo(self.dir / self.pin.mirror)
        mirror.config_set("txray.upstream", "file:///elsewhere")
        with self.assertRaises(IntegrityError):
            self.store.read_span(FIXTURE.commit, "src/lib.rs", 1, 1)


if __name__ == "__main__":
    unittest.main()
