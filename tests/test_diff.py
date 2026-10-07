"""Two-commit diffs: tree changes, hunks, symbols, classification and citations (fixtures)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from timelinexray.diff import CLASSES, DiffEngine, SymbolSource, content, rules
from timelinexray.diff.analysis import BlobAnalysis
from timelinexray.diff.hunks import align_hunks, apply_hunks, line_hunks, split_lines
from timelinexray.diff.model import Hunk
from timelinexray.diff.symbols import FileSymbols
from timelinexray.index import CodeIndex
from timelinexray.netguard import ENV_ALLOW_FILE_URLS, Allowlist
from timelinexray.snapshot import SnapshotStore
from timelinexray.syntax import default_registry
from tests.diff_support import (
    CLASSES_NEW,
    CLASSES_OLD,
    MISLEADING_MESSAGE,
    linear_history,
    run_git,
)
from tests.support import file_url, run_cli


class _Fixture:
    def __init__(self, snapshots: list[dict[str, bytes]]) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-diff-")
        self.root = Path(self._tmp.name)
        self.git_dir, self.commits = linear_history(self.root, snapshots)
        self.url = file_url(self.git_dir)
        self.allowlist = Allowlist([self.url])
        self.store_dir = self.root / "store"
        self.store = SnapshotStore(self.store_dir)
        for commit in self.commits:
            self.store.pin(commit, self.url, allowlist=self.allowlist)

    def cleanup(self) -> None:
        self._tmp.cleanup()


FIXTURE: _Fixture
DIFF = None


def setUpModule() -> None:
    global FIXTURE, DIFF
    FIXTURE = _Fixture([CLASSES_OLD, CLASSES_NEW])
    DIFF = DiffEngine(FIXTURE.store).diff(*FIXTURE.commits)


def tearDownModule() -> None:
    FIXTURE.cleanup()


def _items(change_class: str | None = None, path: str | None = None) -> list:
    return [item for item in DIFF.items
            if (change_class is None or item.change_class == change_class)
            and (path is None or item.path == path)]


class TreeDiffTest(unittest.TestCase):
    def test_statuses_blob_ids_and_renames(self) -> None:
        old, new = FIXTURE.commits
        by_path = {change.path: change for change in DIFF.files}
        self.assertEqual(by_path["added/fresh.rs"].status, "added")
        self.assertEqual(by_path["gone/removed.rs"].status, "removed")
        self.assertEqual(by_path["README.md"].status, "modified")
        moved = by_path["new/moved.rs"]
        self.assertEqual((moved.status, moved.old_path, moved.similarity),
                         ("renamed", "old/moved.rs", 100))
        handler = by_path["svc/request_handler.rs"]
        self.assertEqual((handler.status, handler.old_path), ("renamed", "svc/handler.rs"))
        self.assertGreaterEqual(handler.similarity, 50)
        self.assertLess(handler.similarity, 100)
        for change in DIFF.files:
            with self.subTest(path=change.path):
                if change.old_path:
                    self.assertEqual(change.old_oid, run_git(FIXTURE.git_dir, "rev-parse",
                                                             f"{old}:{change.old_path}"))
                if change.new_path:
                    self.assertEqual(change.new_oid, run_git(FIXTURE.git_dir, "rev-parse",
                                                             f"{new}:{change.new_path}"))
        changed = set(CLASSES_OLD) ^ set(CLASSES_NEW) | {
            p for p in CLASSES_OLD if p in CLASSES_NEW and CLASSES_OLD[p] != CLASSES_NEW[p]}
        seen = {p for change in DIFF.files for p in (change.old_path, change.new_path) if p}
        self.assertEqual(seen, changed)

    def test_hunks_rebuild_the_new_blob(self) -> None:
        for change in DIFF.files:
            if change.analysis != "text":
                continue
            with self.subTest(path=change.path):
                old = split_lines(CLASSES_OLD[change.old_path]) if change.old_path else []
                new = split_lines(CLASSES_NEW[change.new_path]) if change.new_path else []
                self.assertEqual(apply_hunks(old, new, list(change.hunks)), new)
                self.assertEqual(change.lines_added, sum(h.new_count for h in change.hunks))
                for hunk in change.hunks:
                    self.assertLessEqual(hunk.old_start + max(hunk.old_count, 1) - 1, max(len(old), 1))
                    self.assertLessEqual(hunk.new_start + max(hunk.new_count, 1) - 1, max(len(new), 1))

    def test_not_text_entries_are_reported(self) -> None:
        change = next(c for c in DIFF.files if c.path == "assets/blob.bin")
        self.assertEqual((change.analysis, change.hunks), ("not-text:binary", ()))
        [item] = _items(path="assets/blob.bin")
        self.assertEqual((item.change_class, item.old.role, item.new.role),
                         ("unknown", "not-text", "not-text"))


class ClassificationTest(unittest.TestCase):
    def test_every_class_occurs(self) -> None:
        self.assertEqual({item.change_class for item in DIFF.items}, set(CLASSES))

    def test_parameter_defaults(self) -> None:
        rows = {(i.path, i.detail["name"], i.kind): i for i in _items("parameter-default")}
        click = rows[("params/param.rs", "ClickWeight", "value-changed")]
        self.assertEqual((click.detail["old_value"], click.detail["new_value"]), ("0.4", "0.3"))
        self.assertEqual((click.detail["type"], click.detail["flag"]), ("f64", "click_weight"))
        self.assertIn("public default", click.summary)
        self.assertEqual((click.old.start_line, click.new.start_line), (4, 4))
        self.assertEqual(rows[("params/param.rs", "EnableLegacy", "removed")].detail["old_value"], "true")
        added = rows[("params/param.rs", "ExcludeSeen", "added")]
        self.assertEqual((added.new.start_line, added.new.end_line), (18, 23))
        self.assertEqual(added.old.role, "context")
        config = rows[("config/app.yaml", "server.timeout_ms", "value-changed")]
        self.assertEqual((config.detail["old_value"], config.detail["new_value"]), ("250", "300"))
        const = rows[("util/constants.py", "MAX_ITEMS", "value-changed")]
        self.assertEqual((const.detail["old_value"], const.detail["new_value"]), ("10", "20"))
        # ReplyWeight and MaxResults did not change; a computed constant is not a default.
        names = {name for _, name, _ in rows}
        self.assertNotIn("ReplyWeight", names)
        self.assertNotIn("MaxResults", names)
        self.assertNotIn("NAMES", names)
        self.assertEqual(len(rows), 5)

    def test_registration_added_removed_reordered(self) -> None:
        [item] = _items("registration")
        self.assertEqual(item.path, "pipeline/pipeline.rs")
        self.assertEqual(item.detail["list"], "Pipeline::new: filters")
        self.assertEqual(item.detail["added"], ["SpamFilter"])
        self.assertEqual(item.detail["removed"], ["MutedKeywordFilter"])
        self.assertEqual(item.detail["reordered"], ["DedupFilter"])
        self.assertEqual(item.detail["new_entries"], ["DedupFilter", "AgeFilter", "SpamFilter"])
        self.assertEqual(item.new_symbols, ("Pipeline::new",))
        self.assertEqual((item.old.start_line, item.old.end_line), (5, 9))
        # the unchanged sources list produced no item
        self.assertNotIn("sources", json.dumps([i.to_dict() for i in DIFF.items]))

    def test_logic_classes_and_symbols(self) -> None:
        [scoring] = _items("scoring-logic")
        self.assertEqual((scoring.path, scoring.new_symbols), ("scorers/weighted_scorer.rs", ("combine",)))
        [model] = _items("model-config")
        self.assertEqual((model.path, model.new_symbols), ("phoenix/models/features.py", ("build",)))
        unknown = {item.path: item for item in _items("unknown")}
        self.assertEqual(unknown["util/strings.rs"].new_symbols, ("trim",))
        self.assertEqual(set(unknown), {"util/strings.rs", "util/constants.py", "added/fresh.rs",
                                        "gone/removed.rs", "svc/request_handler.rs",
                                        "assets/blob.bin", "visibility/rules.rs"})
        self.assertEqual(unknown["svc/request_handler.rs"].new_symbols, ("lookup",))
        self.assertEqual(unknown["svc/request_handler.rs"].status, "renamed")

    def test_path_and_region_classes(self) -> None:
        by_class: dict[str, set[str]] = {}
        for item in DIFF.items:
            by_class.setdefault(item.change_class, set()).add(item.path)
        self.assertEqual(by_class["license"], {"LICENSE"})
        self.assertEqual(by_class["docs-only"], {"README.md", "docs/guide.md"})
        self.assertEqual(by_class["test-only"], {"tests/test_pipeline.py", "util/strings.rs"})
        self.assertEqual(by_class["generated-vendored"], {"vendor/lib/thing.py"})
        self.assertEqual(by_class["cosmetic"], {"fmt/pretty.rs", "params/param.rs", "new/moved.rs"})
        [test_region] = [i for i in _items("test-only") if i.path == "util/strings.rs"]
        self.assertEqual(test_region.new_symbols, ("tests::trims",))
        [sync] = [i for i in _items("cosmetic") if i.path == "params/param.rs"]
        self.assertEqual((sync.old.start_line, sync.new.start_line), (1, 1))
        [moved] = [i for i in _items("cosmetic") if i.path == "new/moved.rs"]
        self.assertEqual((moved.kind, moved.old.path, moved.new.path),
                         ("moved", "old/moved.rs", "new/moved.rs"))

    def test_citations_are_exact_spans_on_both_commits(self) -> None:
        store = FIXTURE.store
        checked = 0
        for item in DIFF.items:
            for citation in (item.old, item.new):
                with self.subTest(item=item.summary, commit=citation.commit[:7]):
                    self.assertIn(citation.commit, FIXTURE.commits)
                    if citation.role in ("span", "context"):
                        span = store.read_span(citation.commit, citation.path,
                                               citation.start_line, citation.end_line)
                        self.assertEqual(span.sha256, citation.span_sha256)
                        _, entry = store.lookup(citation.commit, citation.path)
                        self.assertEqual(citation.blob_oid, entry.oid)
                        checked += 1
                    else:
                        self.assertIsNone(citation.span_sha256)
                        self.assertTrue(citation.note)
        self.assertGreater(checked, 30)
        [click] = [i for i in _items("parameter-default") if i.detail["name"] == "ClickWeight"]
        old = store.read_span(click.old.commit, "params/param.rs", 4, 4, anchor="0.4")
        new = store.read_span(click.new.commit, "params/param.rs", 4, 4, anchor="0.3")
        self.assertEqual((old.anchor.verdict, new.anchor.verdict), ("FOUND", "FOUND"))

    def test_commit_messages_are_never_used(self) -> None:
        text = json.dumps(DIFF.to_dict())
        self.assertNotIn(MISLEADING_MESSAGE, text)
        self.assertNotIn("12345.678", text)

    def test_index_and_extraction_give_the_same_diff(self) -> None:
        index = CodeIndex(FIXTURE.store, FIXTURE.root / "index")
        for commit in FIXTURE.commits:
            index.build(commit)
        indexed = DiffEngine(FIXTURE.store, SymbolSource(index)).diff(*FIXTURE.commits)
        self.assertEqual(indexed.to_dict(), DIFF.to_dict())
        self.assertEqual(DIFF.symbol_backends, ("lexical/1",))


class StepFixtureTest(unittest.TestCase):
    """Single-purpose steps: reorder only, cosmetic only, line endings."""

    def _diff(self, old: dict[str, bytes], new: dict[str, bytes]):
        fixture = _Fixture([old, new])
        self.addCleanup(fixture.cleanup)
        return DiffEngine(fixture.store).diff(*fixture.commits)

    def test_reorder_only(self) -> None:
        old = {"p.rs": b"fn scorers() -> Vec<Box<dyn Scorer>> {\n    vec![Box::new(AScorer), "
                       b"Box::new(BScorer), Box::new(CScorer)]\n}\n"}
        new = {"p.rs": b"fn scorers() -> Vec<Box<dyn Scorer>> {\n    vec![Box::new(CScorer), "
                       b"Box::new(AScorer), Box::new(BScorer)]\n}\n"}
        diff = self._diff(old, new)
        [item] = diff.items
        self.assertEqual((item.change_class, item.kind), ("registration", "entries-changed"))
        self.assertEqual((item.detail["added"], item.detail["removed"], item.detail["reordered"]),
                         ([], [], ["CScorer"]))
        self.assertEqual(item.detail["list"], "scorers")

    def test_cosmetic_only_commit(self) -> None:
        old = {
            "a.rs": b"// one\nfn f(a: i32,b: i32) -> i32 { a+b }\n",
            "b.py": b"def g(x):\n    return x  # note\n",
            "c.java": b"class C {\n  /* doc */\n  int f() { return 1; }\n}\n",
            "d.sh": b"echo alpha beta\n",
        }
        new = {
            "a.rs": b"// one, reworded\nfn f(a: i32, b: i32) -> i32 {\n    a + b\n}\n",
            "b.py": b"def g(x):\n    return x  # a longer note\n",
            "c.java": b"class C {\r\n  /* documentation */\r\n  int f() { return 1; }\r\n}\r\n",
            "d.sh": b"echo alpha   beta\n",
        }
        diff = self._diff(old, new)
        self.assertEqual({item.change_class for item in diff.items}, {"cosmetic"})
        self.assertEqual({item.path for item in diff.items}, set(old))

    def test_python_indentation_is_not_cosmetic(self) -> None:
        old = {"m.py": b"def g(x):\n    if x:\n        y = 1\n    return y\n"}
        new = {"m.py": b"def g(x):\n    if x:\n        y = 1\n        return y\n"}
        [item] = self._diff(old, new).items
        self.assertEqual(item.change_class, "unknown")

    def test_string_literal_change_is_not_cosmetic(self) -> None:
        old = {"s.rs": b"fn name() -> &'static str { \"a // b\" }\n"}
        new = {"s.rs": b"fn name() -> &'static str { \"a // c\" }\n"}
        [item] = self._diff(old, new).items
        self.assertEqual(item.change_class, "unknown")

    def test_empty_and_identical_commits(self) -> None:
        same = {"x.rs": b"fn x() {}\n"}
        self.assertEqual(self._diff(same, dict(same)).items, ())


class ValueParserTest(unittest.TestCase):
    def _values(self, path: str, language: str | None, text: bytes) -> dict[str, tuple[str, str]]:
        backend = default_registry().select(language) if language else None

        def load() -> FileSymbols:
            from timelinexray.diff.symbols import SymbolSource as Source
            return Source().for_blob("0" * 40, path, language, text, text.decode())

        from timelinexray.diff.analysis import make_analysis
        analysis: BlobAnalysis = make_analysis("0" * 40, path, language, text, load)
        self.assertTrue(backend is None or backend.name == "lexical")
        return {d.name: (d.kind, d.value) for d in analysis.values}

    def test_rust(self) -> None:
        values = self._values("p.rs", "rust", (
            b'param!(A, String, "flag_a", "x, y"); // trailing, comment\n'
            b"param!(\n    B, // comment, with comma\n    f64,\n    \"flag_b\",\n    -0.5\n);\n"
            b"const C: [&str; 2] = [\"a\", \"b\"];\n"
            b"const D: Duration = Duration::from_secs(compute());\n"
            b"pub static E: u32 = 1_000;\n"
        ))
        self.assertEqual(values["A"], ("param!", '"x, y"'))
        self.assertEqual(values["B"], ("param!", "-0.5"))
        self.assertEqual(values["C"], ("const", '["a", "b"]'))
        self.assertNotIn("D", values)
        self.assertEqual(values["E"], ("static", "1_000"))

    def test_java_scala_python(self) -> None:
        java = self._values("C.java", "java", b"class C {\n  public static final double W = 0.25;\n"
                                               b"  private int n = 5;\n  private Foo f = new Foo();\n}\n")
        self.assertEqual((java["C.W"], java["C.n"]), (("const", "0.25"), ("field", "5")))
        self.assertNotIn("C.f", java)
        scala = self._values("P.scala", "scala", b"object P {\n  val Weight = 0.4\n"
                                                 b"  val Timeout = 30.seconds\n  val Other = compute()\n}\n")
        self.assertEqual((scala["P.Weight"], scala["P.Timeout"]),
                         (("field", "0.4"), ("field", "30.seconds")))
        self.assertNotIn("P.Other", scala)
        python = self._values("c.py", "python", b"LIMIT: int = 3\nNAMES = [\"a\"]\nOTHER = f(1)\n")
        self.assertEqual((python["LIMIT"], python["NAMES"]), (("const", "3"), ("const", '["a"]')))
        self.assertNotIn("OTHER", python)

    def test_config_files(self) -> None:
        yaml = self._values("conf/app.yaml", "yaml",
                            b"# comment\nserver:\n  timeout_ms: 250 # ms\n  list:\n    - a\n"
                            b"name: \"x\"\n")
        self.assertEqual(yaml, {"server.timeout_ms": ("config-key", "250"),
                                "name": ("config-key", '"x"')})
        toml = self._values("conf/app.toml", "toml", b"top = 1\n[section]\nkey = \"v\"\n")
        self.assertEqual(toml, {"top": ("config-key", "1"), "section.key": ("config-key", '"v"')})
        self.assertEqual(self._values("Cargo.toml", "toml", b"[package]\nversion = \"1\"\n"), {})
        json_values = self._values("conf/a.json", "json", b'{\n  "a": {\n    "b": 2,\n    "c": true\n  }\n}\n')
        self.assertEqual(json_values, {"a.b": ("config-key", "2"), "a.c": ("config-key", "true")})


class ClassifierV2Test(unittest.TestCase):
    """Classifier v2 rules (build-dependency, train/, test names, unknown reasons, the
    deciding name rule), each on synthetic two-commit fixtures, including the cases they must not take."""

    def _diff(self, old: dict[str, bytes], new: dict[str, bytes]):
        fixture = _Fixture([old, new])
        self.addCleanup(fixture.cleanup)
        return DiffEngine(fixture.store).diff(*fixture.commits)

    def _classes(self, old: dict[str, bytes], new: dict[str, bytes]) -> dict[str, set[str]]:
        found: dict[str, set[str]] = {}
        for item in self._diff(old, new).items:
            found.setdefault(item.path, set()).add(item.change_class)
        return found

    def test_fixture_items_of_the_new_classes(self) -> None:
        [build] = _items("build-dependency", "svc/BUILD.bazel")
        self.assertEqual(build.kind, "file")
        [imports] = _items("build-dependency", "svc/wiring.rs")
        self.assertEqual((imports.kind, imports.new.start_line, imports.new.end_line), ("region", 1, 2))
        self.assertIn("import or module declarations", imports.summary)
        # no filtering/visibility name rule: such code stays unknown (see docs/updates.md)
        [filtering] = _items(None, "visibility/rules.rs")
        self.assertEqual((filtering.change_class, filtering.detail),
                         ("unknown", {"unknown_reason": "no-rule"}))
        [scoring] = _items("scoring-logic")
        self.assertEqual(scoring.detail["matched_by"],
                         [{"rule": "path", "name": "scorers/weighted_scorer.rs"}])
        self.assertNotIn("matched_by", imports.detail)
        for item in _items("unknown"):
            self.assertIn(item.detail["unknown_reason"], ("no-rule", "not-parsed", "not-text", "mode-only"))
        self.assertEqual(_items("unknown", "assets/blob.bin")[0].detail["unknown_reason"], "not-text")
        self.assertEqual(_items("unknown", "util/constants.py")[0].detail["unknown_reason"], "no-rule")

    def test_build_paths(self) -> None:
        for path in ("BUILD", "a/BUILD.bazel", "WORKSPACE", "x/defs.bzl", "crate/Cargo.toml",
                     "crate/build.rs", "requirements.txt", "requirements-dev.txt", "Makefile",
                     "svc/Dockerfile", "pom.xml", "build.gradle.kts", "build.sbt", "go.mod",
                     "cmake/deps.cmake", "pyproject.toml", "setup.py"):
            with self.subTest(path):
                self.assertTrue(rules.is_build_path(path))
                self.assertEqual(rules.path_class(path, None), "build-dependency")
        for path in ("builder.rs", "src/build_index.py", "rebuild.sh", "config/app.yaml",
                     "docs/requirements.md"):
            with self.subTest(path):
                self.assertFalse(rules.is_build_path(path))
        # earlier path rules keep their files
        self.assertEqual(rules.path_class("tests/BUILD", None), "test-only")
        self.assertEqual(rules.path_class("docs/Makefile", None), "docs-only")
        self.assertEqual(rules.path_class("vendor/x/BUILD", "vendored"), "generated-vendored")

    def test_build_manifest_change_is_one_file_item(self) -> None:
        old = {"crate/Cargo.toml": b'[package]\nname = "a"\nversion = "1.0.0"\n'}
        new = {"crate/Cargo.toml": b'[package]\nname = "a"\nversion = "1.1.0"\n'}
        [item] = self._diff(old, new).items
        self.assertEqual((item.change_class, item.kind), ("build-dependency", "file"))

    def test_import_only_hunks(self) -> None:
        body = b"\npub fn run() -> u8 {\n    1\n}\n"
        cases = {
            "r.rs": (b"use a::b;\n" + body, b"use a::b;\nuse a::c;\n" + body),
            "m.rs": (b"mod one;\n" + body, b"mod one;\npub mod two;\n" + body),
            "p.py": (b"import os\n\ndef run():\n    return 1\n",
                     b"import os\nfrom sys import argv\n\ndef run():\n    return 1\n"),
            "J.java": (b"import a.B;\n\nclass J {\n  int f() { return 1; }\n}\n",
                       b"import a.B;\nimport a.C;\n\nclass J {\n  int f() { return 1; }\n}\n"),
            "multi.rs": (b"use a::{\n    b,\n};\n" + body, b"use a::{\n    b,\n    c,\n};\n" + body),
        }
        old = {path: pair[0] for path, pair in cases.items()}
        new = {path: pair[1] for path, pair in cases.items()}
        found = self._classes(old, new)
        self.assertEqual(found, {path: {"build-dependency"} for path in cases})

    def test_import_rule_never_takes_code(self) -> None:
        body = b"\npub fn run() -> u8 {\n    1\n}\n"
        old = {
            # an import and a code line change in one hunk
            "mixed.rs": b"use a::b;\nconst LIMIT: usize = compute();\n" + body,
            # a module with a body is code, not a declaration
            "inline.rs": b"mod inner {\n    pub fn x() -> u8 { 1 }\n}\n" + body,
            # a comment-only change stays cosmetic
            "note.rs": b"use a::b; // first\n" + body,
            # a language without symbols is never an import change
            "lib.cc": b"#include <a>\nint f() { return 1; }\n",
        }
        new = {
            "mixed.rs": b"use a::c;\nconst LIMIT: usize = compute_more();\n" + body,
            "inline.rs": b"mod inner {\n    pub fn x() -> u8 { 2 }\n}\n" + body,
            "note.rs": b"use a::b; // second\n" + body,
            "lib.cc": b"#include <a>\n#include <b>\nint f() { return 1; }\n",
        }
        found = self._classes(old, new)
        self.assertNotIn("build-dependency", found["mixed.rs"])
        self.assertNotIn("build-dependency", found["inline.rs"])
        self.assertEqual(found["note.rs"], {"cosmetic"})
        self.assertEqual(found["lib.cc"], {"unknown"})

    def test_filtering_names_are_not_a_class(self) -> None:
        """A filtering/visibility name rule was measured and rejected (its items were mostly
        caches, telemetry and tooling): such paths and symbols stay unknown."""
        fn_old, fn_new = b"pub fn f() -> u8 {\n    1\n}\n", b"pub fn f() -> u8 {\n    2\n}\n"
        paths = {
            "visibility-filtering/hydration/store.rs": "unknown",
            "home-mixer/filters/age_filter.rs": "unknown",
            "brand_safety/check.rs": "unknown",
            "home-mixer/scorers/filter_scorer.rs": "scoring-logic",  # scoring path wins
            "visibility-filtering/models/label.rs": "model-config",  # model path wins
            "visibility-filtering/tests/rules.rs": "test-only",  # test path wins
            "util/strings.rs": "unknown",
        }
        found = self._classes({p: fn_old for p in paths}, {p: fn_new for p in paths})
        self.assertEqual(found, {path: {cls} for path, cls in paths.items()})
        # an enclosing symbol name decides the scoring class when no path rule does
        old = {"svc/pipeline.rs": b"pub fn apply_filters() -> u8 {\n    1\n}\n"
                                  b"pub fn rank_score() -> u8 {\n    1\n}\n"}
        new = {"svc/pipeline.rs": b"pub fn apply_filters() -> u8 {\n    2\n}\n"
                                  b"pub fn rank_score() -> u8 {\n    2\n}\n"}
        items = sorted((i.new_symbols, i.change_class, i.detail.get("matched_by"))
                       for i in self._diff(old, new).items)
        self.assertEqual(items, [
            (("apply_filters",), "unknown", None),
            (("rank_score",), "scoring-logic", [{"rule": "symbol", "name": "rank_score"}]),
        ])

    def test_train_directory_and_test_names(self) -> None:
        self.assertTrue(rules.is_model_config_path("phoenix/xrex/train/trainer.py"))
        self.assertFalse(rules.is_model_config_path("phoenix/xrex/trainer/run.py"))
        for path in ("client/test_support.rs", "svc/test_helpers.py", "lib/ledger_contract_fixtures.rs",
                     "lib/fixture.rs"):
            with self.subTest(path):
                self.assertTrue(rules.is_test_path(path))
        for path in ("svc/test_user.rs", "svc/contest.rs", "svc/fixtures_loader.rs"):
            with self.subTest(path):
                self.assertFalse(rules.is_test_path(path))

    def test_unknown_reasons(self) -> None:
        old = {"svc/run.rs": b"pub fn run() -> u8 {\n    1\n}\n", "kern/k.cu": b"int k() { return 1; }\n",
               "tool.sh": b"echo a\n"}
        new = {"svc/run.rs": b"pub fn run() -> u8 {\n    2\n}\n", "kern/k.cu": b"int k() { return 2; }\n",
               "tool.sh": b"echo b\n"}
        reasons = {i.path: (i.change_class, i.detail["unknown_reason"]) for i in self._diff(old, new).items}
        self.assertEqual(reasons, {"svc/run.rs": ("unknown", "no-rule"),
                                   "kern/k.cu": ("unknown", "not-parsed"),
                                   "tool.sh": ("unknown", "not-parsed")})


class ScoringNameRuleTest(unittest.TestCase):
    """Classifier v3 scoring-name rule: whole words, two excluded system names, and only
    symbols that enclose a changed production line (hand check in docs/updates.md)."""

    def _diff(self, old: dict[str, bytes], new: dict[str, bytes]):
        fixture = _Fixture([old, new])
        self.addCleanup(fixture.cleanup)
        return DiffEngine(fixture.store).diff(*fixture.commits)

    def test_whole_words_name_scoring(self) -> None:
        for name in ("home-mixer/scorers/author_cold_start.rs", "vm-ranker/scoring/value_model.rs",
                     "PhoenixScorer::score", "PhoenixScores::from", "CandidateScoringInputs",
                     "util/rescore.rs", "side_effects/reranking_effect.rs", "vqv_weight",
                     "ScoredTweet", "HTTPScorer", "ads/safe_gap_blender.rs", "POST_ANN_MIN_SCORE",
                     "time_decay", "calibration.py", "ranking"):
            with self.subTest(name):
                self.assertTrue(rules.is_scoring_name(name))
        for name in ("phoenix-rankall/src/consumer/kafka.rs", "UNSCORED_AUTHOR_FALLBACK_METRIC",
                     "util/lightweight.rs", "outrank_check", "scorecard.rs", "", None):
            with self.subTest(name):
                self.assertFalse(rules.is_scoring_name(name))
        self.assertEqual(rules.name_words("PhoenixRankAll/vm_ranker-fa4.rs"),
                         ["phoenix", "rank", "all", "vm", "ranker", "fa", "4", "rs"])

    def test_page_rank_and_rank_all_are_not_scoring_names(self) -> None:
        for name in ("botmaker-rules/HighPageRankThreshold.df", "fetch_high_page_rank_v2",
                     "RISKY_HIGH_VIZ_REPLY_EXEMPT_MIN_PAGE_RANK_SCORE", "pagerank_score.py",
                     "phoenix_rank_all/phoenixRankAllCandidateProcessor.strato",
                     "phoenix-rankall/src/processor/gen/scored_candidate.rs"):
            with self.subTest(name):
                self.assertFalse(rules.is_scoring_name(name))

    def test_excluded_phrases_are_adjacent_words_and_exclude_the_whole_name(self) -> None:
        self.assertFalse(rules.is_scoring_name("rank_all_candidates_by_score_later"))
        self.assertTrue(rules.is_scoring_name("rank_candidates_all"))
        self.assertTrue(rules.is_scoring_name("page_size_rank"))

    def test_an_insertion_does_not_take_its_neighbours_name(self) -> None:
        body = b"pub fn run() -> u8 {\n    1\n}\n"
        old = {"svc/source.rs": b"const MIN_SCORE: f64 = 0.5;\n" + body,
               "svc/combine.rs": b"pub fn combine_scores(a: u8) -> u8 {\n    a\n}\n"}
        new = {"svc/source.rs": b"const MIN_SCORE: f64 = 0.5;\nconst MAX_AGE: u64 = 9;\n" + body,
               "svc/combine.rs": b"pub fn combine_scores(a: u8) -> u8 {\n    let b = a;\n    b\n}\n"}
        found = {(i.path, i.change_class, json.dumps(i.detail.get("matched_by")))
                 for i in self._diff(old, new).items}
        self.assertEqual(found, {
            # the added constant's only scoring-named neighbour is not part of the change
            ("svc/source.rs", "unknown", "null"),
            # a line added inside a scoring-named function still takes its name
            ("svc/combine.rs", "scoring-logic",
             json.dumps([{"rule": "symbol", "name": "combine_scores"}])),
        })

    def test_a_test_name_does_not_decide_a_production_line(self) -> None:
        head = b"pub fn dedup() -> u8 {\n    1\n}\n"
        tests_module = (b"#[cfg(test)]\nmod tests {\n    #[test]\n    fn keeps_highest_score() {\n"
                        b"        assert_eq!(super::dedup(), 1);\n    }\n}\n")
        old = {"svc/dedup.rs": head}
        new = {"svc/dedup.rs": head + b"const LIMIT: u8 = 2;\n" + tests_module}
        [item] = self._diff(old, new).items
        self.assertEqual((item.change_class, item.detail), ("unknown", {"unknown_reason": "no-rule"}))
        # a hunk entirely inside the test module is still test-only
        changed = new["svc/dedup.rs"].replace(b"1);", b"2);")
        [test_item] = self._diff(new, {"svc/dedup.rs": changed}).items
        self.assertEqual(test_item.change_class, "test-only")


class ContentRulesTest(unittest.TestCase):
    """Classifier v3 content rules (what the changed lines do): observability, data-type,
    visibility-rule, access-modifier; each with the cases it must not take."""

    def _classes(self, old: dict[str, bytes], new: dict[str, bytes]) -> dict[str, set[str]]:
        fixture = _Fixture([old, new])
        self.addCleanup(fixture.cleanup)
        found: dict[str, set[str]] = {}
        for item in DiffEngine(fixture.store).diff(*fixture.commits).items:
            found.setdefault(item.path, set()).add(item.change_class)
        return found

    def _check(self, cases: dict[str, tuple[bytes, bytes, str]]) -> None:
        found = self._classes({p: c[0] for p, c in cases.items()}, {p: c[1] for p, c in cases.items()})
        self.assertEqual(found, {path: {case[2]} for path, case in cases.items()})

    def test_observability_takes_log_and_metric_statements(self) -> None:
        fn = b"pub fn run(x: u8) -> u8 {\n%s    x\n}\n"
        self._check({
            "svc/a.rs": (fn % b'    info!(\n        "x={}",\n        x\n    );\n',
                         fn % b'    info!(\n        "x={} y",\n        x + 1\n    );\n', "observability"),
            "svc/b.rs": (fn % b"", fn % b'    tracing::warn!(x, "slow");\n    REQUESTS.with_label_values(&["a"]).inc();\n',
                         "observability"),
            "svc/c.rs": (b"lazy_static! {\n}\n",
                         b'lazy_static! {\n    pub static ref HITS: IntCounter = register_int_counter!(\n'
                         b'        "hits", "help"\n    )\n    .unwrap();\n}\n', "observability"),
            "svc/d.py": (b"def run(x):\n    logger.info('x %s', x)\n    return x\n",
                         b"def run(x):\n    logger.info('x=%s', x)\n    Metrics.histogram('a').record(x)\n    return x\n",
                         "observability"),
            "svc/E.java": (b"class E {\n  int f(int x) {\n    log.info(\"a\");\n    return x;\n  }\n}\n",
                           b"class E {\n  int f(int x) {\n    log.info(\"b {}\", x);\n    return x;\n  }\n}\n",
                           "observability"),
            "svc/f.scala": (b"object F {\n  def f(x: Int): Int = {\n    x\n  }\n}\n",
                            b'object F {\n  def f(x: Int): Int = {\n    stats.counter("calls").incr()\n    x\n  }\n}\n',
                            "observability"),
        })

    def test_observability_never_takes_other_code(self) -> None:
        fn = b"pub fn run(x: u8) -> u8 {\n%s    x\n}\n"
        self._check({
            # a log line and a code line in one hunk
            "svc/a.rs": (fn % b'    info!("a");\n', fn % b'    info!("b");\n    let y = x;\n', "unknown"),
            # a log call in the same line as a block header
            "svc/b.rs": (fn % b"", fn % b'    if x > 1 { warn!("big") }\n', "unknown"),
            # a macro name inside a string is not a call
            "svc/c.rs": (fn % b'    let s = "a";\n', fn % b'    let s = "info!(x)";\n', "unknown"),
            # a binding of a log call is not a bare telemetry statement
            "svc/d.rs": (fn % b"", fn % b'    let _ = info!("a");\n', "unknown"),
            # "metric" and "stats" alone name ranking data in this upstream: only call shapes
            # of logging and metrics libraries count
            "svc/m.rs": (fn % b"    let v = metric_value(c, Metric::Likes, true);\n",
                         fn % b"    let v = metric_value(c, Metric::Replies, false);\n", "unknown"),
            "svc/n.rs": (fn % b"", fn % b"    STATE.set(x);\n    METRIC_CACHE.insert(x);\n", "unknown"),
            "svc/o.py": (b"def run(x):\n    return x\n",
                         b"def run(x):\n    stats.update(x)\n    metrics.append(x)\n    return x\n", "unknown"),
            "svc/p.scala": (b"object P {\n  def f(x: Int): Int = {\n    x\n  }\n}\n",
                            b"object P {\n  def f(x: Int): Int = {\n    stats.add(x)\n    x\n  }\n}\n",
                            "unknown"),
            # print() is not telemetry
            "svc/e.py": (b"def run(x):\n    return x\n", b"def run(x):\n    print(x)\n    return x\n", "unknown"),
            # a comment-only change stays cosmetic; a scoring path keeps its name rule
            "svc/f.rs": (fn % b"    // info!(a)\n", fn % b"    // info!(b)\n", "cosmetic"),
            "scorers/g.rs": (fn % b'    info!("a");\n', fn % b'    info!("b");\n', "scoring-logic"),
        })

    def test_data_type_takes_fields_variants_and_attributes(self) -> None:
        self._check({
            "svc/a.rs": (b"pub struct A {\n    x: u8,\n}\n", b"pub struct A {\n    x: u8,\n    y: Option<u64>,\n}\n",
                         "data-type"),
            "svc/b.rs": (b"enum B {\n    One,\n}\n", b"enum B {\n    One,\n    Two(u8),\n}\n", "data-type"),
            "svc/c.rs": (b"#[derive(Clone)]\nstruct C;\n", b"#[derive(Clone, Copy, Debug)]\nstruct C;\n", "data-type"),
            "svc/d.rs": (b"struct D<'a>(&'a [String]);\n", b"struct D<'a>(&'a [&'a str]);\n", "data-type"),
        })

    def test_data_type_never_takes_values_code_or_other_languages(self) -> None:
        self._check({
            # an enum discriminant and an attribute default assign values
            "svc/a.rs": (b"enum A {\n    One = 1,\n}\n", b"enum A {\n    One = 2,\n}\n", "unknown"),
            "svc/b.rs": (b"struct B {\n    #[arg(long, default_value_t = 1)]\n    n: u8,\n}\n",
                         b"struct B {\n    #[arg(long, default_value_t = 2)]\n    n: u8,\n}\n", "unknown"),
            # a field and a function body in one hunk
            "svc/c.rs": (b"struct A { x: u8 }\nfn f() -> u8 { 1 }\n", b"struct A { x: u16 }\nfn f() -> u8 { 2 }\n",
                         "unknown"),
            # Java enums and Python classes hold code: not this rule
            "svc/D.java": (b"enum D {\n  ONE;\n}\n", b"enum D {\n  ONE, TWO;\n}\n", "unknown"),
            "svc/e.py": (b"class E:\n    x: int\n", b"class E:\n    x: int\n    y: str\n", "unknown"),
        })

    def test_visibility_rule_takes_rule_declarations(self) -> None:
        self._check({
            "rules/a.rs": (b"const LOGGED_OUT: Condition = viewer(ViewerPredicate::LoggedOut);\n",
                           b"const LOGGED_OUT: Condition = not(viewer(ViewerPredicate::LoggedIn));\n",
                           "visibility-rule"),
            "rules/b.rs": (b"pub(super) fn drops() -> Vec<RuleClause> {\n    rule(RuleId::A, drop_post(R::A))\n}\n",
                           b"pub(super) fn drops() -> Vec<RuleClause> {\n    rule(RuleId::B, drop_post(R::A))\n}\n",
                           "visibility-rule"),
            "rules/c.rs": (b"fn blurs(r: Reason) -> [Clause; 2] {\n    [blur(r), age(r)]\n}\n",
                           b"fn blurs(r: Reason) -> [Clause; 2] {\n    [blur(r), prompt(r)]\n}\n",
                           "visibility-rule"),
            "rules/d.rs": (b"const ALL: [Predicate; 1] = [Predicate::A];\n",
                           b"const ALL: [Predicate; 2] = [Predicate::A, Predicate::B];\n", "visibility-rule"),
        })

    def test_visibility_rule_needs_the_declared_type(self) -> None:
        self._check({
            # returns a verdict, takes a clause, or declares another type: not a rule definition
            "rules/a.rs": (b"fn decide(c: &RuleClause) -> Verdict {\n    Verdict::Keep\n}\n",
                           b"fn decide(c: &RuleClause) -> Verdict {\n    Verdict::Drop\n}\n", "unknown"),
            "rules/b.rs": (b"fn new(clauses: Vec<RuleClause>) -> Self {\n    Self { clauses }\n}\n",
                           b"fn new(clauses: Vec<RuleClause>) -> Self {\n    Self { clauses: vec![] }\n}\n",
                           "unknown"),
            "rules/c.rs": (b"const LEVELS: [(RuleId, Label); 1] = [(RuleId::A, Label::B)];\n",
                           b"const LEVELS: [(RuleId, Label); 1] = [(RuleId::A, Label::C)];\n", "unknown"),
            # a model/config location keeps its path rule
            "models/d.rs": (b"const X: Condition = a();\n", b"const X: Condition = b();\n", "model-config"),
        })
        self.assertFalse(content.is_rule_declaration("function", "fn name(x: &RuleClause) -> &'static str"))
        self.assertFalse(content.is_rule_declaration("struct", "pub struct Condition"))

    def test_access_modifier_only(self) -> None:
        fn = b"fn run(x: u8) -> u8 {\n    x\n}\n"
        self._check({
            "svc/a.rs": (fn, b"pub(crate) " + fn, "access-modifier"),
            "svc/b.rs": (b"pub(in crate::x) " + fn, fn, "access-modifier"),
            "svc/C.java": (b"class C {\n  private int f() { return 1; }\n}\n",
                           b"class C {\n  public int f() { return 1; }\n}\n", "access-modifier"),
            "svc/d.scala": (b"object D {\n  private[svc] def f: Int = 1\n}\n",
                            b"object D {\n  def f: Int = 1\n}\n", "access-modifier"),
            # a modifier and another token, a pure insertion, Python: not this rule
            "svc/e.rs": (fn, b"pub " + fn.replace(b"    x", b"    x + 1"), "unknown"),
            "svc/f.rs": (fn, fn + b"\npub fn other() -> u8 {\n    1\n}\n", "unknown"),
            "svc/g.py": (b"def run(x):\n    return x\n", b"def run(x):\n    return x + 1\n", "unknown"),
        })
        self.assertEqual(content.without_access_modifiers(("pub", "(", "crate", ")", "fn"), "rust"), ("fn",))
        self.assertIsNone(content.without_access_modifiers(("def",), "python"))

    def test_statement_splitting_on_masked_code(self) -> None:
        code = 'fn f() {\n    info!("a;b");\n    if x { warn!("c") }\n}\n'
        from timelinexray.syntax.source import mask
        masked = mask(code, "rust").code
        texts = [(" ".join(masked[a:b].split()), simple) for a, b, simple in content.statements(masked, "rust")]
        self.assertEqual(texts, [("fn f() {", False), ("info!( );", True), ("if x {", False),
                                 ("warn!( )", True)])


class HunkTest(unittest.TestCase):
    def test_line_hunks_and_apply(self) -> None:
        old = split_lines(b"a\nb\nc\nd\n")
        new = split_lines(b"a\nB\nc\nd\ne\n")
        hunks = line_hunks(old, new)
        self.assertEqual(hunks, [Hunk(2, 1, 2, 1), Hunk(4, 0, 5, 1)])
        self.assertEqual(apply_hunks(old, new, hunks), new)
        self.assertEqual(line_hunks(old, old), [])
        self.assertEqual(line_hunks([], new), [Hunk(0, 0, 1, 5)])
        self.assertEqual(line_hunks(old, []), [Hunk(1, 4, 0, 0)])
        crlf = split_lines(b"a\r\nb\r\n")
        self.assertEqual(line_hunks(split_lines(b"a\nb\n"), crlf), [Hunk(1, 2, 1, 2)])

    def test_alignment_prefers_whole_declarations(self) -> None:
        old = split_lines(b"p(\n  A\n);\n\np(\n  C\n);\n")
        new = split_lines(b"p(\n  A\n);\n\np(\n  B\n);\n\np(\n  C\n);\n")
        raw = line_hunks(old, new)
        aligned = align_hunks(raw, old, new, [(1, 3), (5, 7)], [(1, 3), (5, 7), (9, 11)])
        self.assertEqual(aligned, [Hunk(4, 0, 5, 4)])
        self.assertEqual(apply_hunks(old, new, aligned), new)


class DiffCommandTest(unittest.TestCase):
    def test_human_json_and_errors(self) -> None:
        old, new = FIXTURE.commits
        store = ["--store", str(FIXTURE.store_dir)]
        code, out, err = run_cli(["diff", old[:10], new[:10], "--class", "parameter-default", *store])
        self.assertEqual((code, err), (0, b""))
        text = out.decode()
        self.assertIn("ClickWeight: public default 0.4 -> 0.3", text)
        self.assertNotIn("[registration]", text)
        code, out, _ = run_cli(["diff", old, new, "--json", *store])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual((doc["command"], doc["outcome"]), ("diff", "ok"))
        self.assertEqual(len(doc["data"]["items"]), len(DIFF.items))
        self.assertTrue(all(item["id"].startswith("i-") for item in doc["data"]["items"]))
        code, _, _ = run_cli(["diff", old, new, "--class", "nonsense", *store])
        self.assertEqual(code, 2)
        code, _, err = run_cli(["diff", old, "f" * 40, *store])
        self.assertEqual(code, 1)
        self.assertIn(b"not pinned", err)
        code, _, _ = run_cli(["diff", old, "main", *store], {ENV_ALLOW_FILE_URLS: ""})
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
