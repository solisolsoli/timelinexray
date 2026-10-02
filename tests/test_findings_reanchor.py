"""Integrity checks and re-anchoring on a synthetic history (P7 sections 3.3 and 7.4).

Controlled mutations: line shift, rename, rename with an unrelated edit, literal change,
duplicated line (duplicate anchor), changed span dependency, changed finding dependency,
new files matching a negative search, and a deleted file.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timelinexray.errors import NotFound
from timelinexray.findings import Actor, FindingsMemory, Ledger
from timelinexray.index import CodeIndex
from timelinexray.netguard import Allowlist
from timelinexray.snapshot import SnapshotStore
from timelinexray.verify import (
    AMBIGUOUS_SPAN,
    CHANGED,
    CHANGED_SPAN,
    CURRENT,
    IDENTICAL,
    INTACT,
    MISSING,
    MISSING_SPAN,
    RELOCATED,
    STALE,
    UNCHANGED,
    UNUSABLE,
    UNVERIFIABLE,
    Citation,
    Verifier,
    _unified,
)
from tests.findings_support import AUTHOR, NOTES, WEIGHTS, HistoryRepo, spec
from tests.index_support import add_commit
from tests.support import build_fixture_repo, file_url

REPO: HistoryRepo


def setUpModule() -> None:
    global REPO
    REPO = HistoryRepo()
    REPO.indexed("base", "negative", "shift")


def tearDownModule() -> None:
    REPO.cleanup()


def click(name: str = "base") -> Citation:
    lines = {"shift": 7}.get(name, 4)
    return Citation(REPO.commits[name], "src/weights.rs", lines, lines, "CLICK_WEIGHT")


class IntegrityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = Verifier(REPO.store, REPO.index)

    def test_intact_citation(self) -> None:
        check = self.verifier.check(click())
        self.assertEqual((check.verdict, check.anchor_verdict, check.baseline), (INTACT, "FOUND", True))
        recorded = self.verifier.check(check.observed)
        self.assertTrue(recorded.usable)
        self.assertFalse(recorded.baseline)
        self.assertEqual(recorded.observed, check.observed)
        self.assertEqual(check.observed.start_byte, 118)

    def test_changed_recorded_bytes(self) -> None:
        observed = self.verifier.check(click()).observed
        for field, value, reason in (
            ("span_sha256", "0" * 64, "span_sha256_differs"),
            ("blob_sha256", "1" * 64, "blob_sha256_differs"),
            ("start_byte", 0, "byte_range_differs"),
        ):
            with self.subTest(field):
                forged = Citation.from_dict({**observed.to_dict(), field: value})
                check = self.verifier.check(forged)
                self.assertEqual((check.verdict, check.reason), (CHANGED, reason))
                self.assertFalse(check.usable)

    def test_missing_spans(self) -> None:
        cases = [
            (Citation("abcdef0", "src/weights.rs", 4, 4, "CLICK_WEIGHT"), "commit_not_pinned"),
            (Citation("xyz", "src/weights.rs", 4, 4, "CLICK_WEIGHT"), "invalid_commit"),
            (Citation(REPO.commits["base"], "src/none.rs", 1, 1, "x"), "path_missing"),
            (Citation(REPO.commits["base"], "src/weights.rs", 90, 91, "x"), "invalid_range"),
            (Citation(REPO.commits["base"], "src/weights.rs", 5, 4, "x"), "invalid_range"),
        ]
        for citation, reason in cases:
            with self.subTest(reason):
                check = self.verifier.check(citation)
                self.assertEqual((check.verdict, check.reason), (MISSING, reason))

    def test_anchor_verdicts_are_reported_beside_integrity(self) -> None:
        base = REPO.commits["base"]
        multiple = self.verifier.check(Citation(base, "src/weights.rs", 2, 7, "WEIGHT"))
        missing = self.verifier.check(Citation(base, "src/weights.rs", 2, 2, "CLICK_WEIGHT"))
        self.assertEqual((multiple.verdict, multiple.anchor_verdict), (INTACT, "FOUND_MULTIPLE"))
        self.assertEqual(multiple.anchor_lines, (2, 3, 4, 7, 7, 7))
        self.assertEqual((missing.verdict, missing.anchor_verdict), (INTACT, "MISSING"))
        # an anchor inside the span (once or several times) confirms the cited content
        self.assertTrue(multiple.usable)
        self.assertFalse(missing.usable)


class UnifiedDiffLinesTest(unittest.TestCase):
    """The proposal's diff counts lines the way every citation does: LF only."""

    def test_hunk_header_ignores_form_feed_and_other_separators(self) -> None:
        for name, separator in (("form feed", b"\x0c"), ("vertical tab", b"\x0b"),
                                ("next line", "\u0085".encode()), ("line separator",
                                                                    "\u2028".encode()),
                                ("lone CR", b"\r")):
            with self.subTest(name):
                old = b"a\n" + separator + b"\nb\nTARGET = 1\n"
                new = b"a\n" + separator + b"\nb\nTARGET = 2\n"
                diff = _unified(old, new, "old", "new")
                self.assertIn("@@ -3,2 +3,2 @@", diff)
                self.assertIn("-TARGET = 1\n+TARGET = 2\n", diff)


class RelocationTest(unittest.TestCase):
    """The citation-level outcomes of P7 section 3.3."""

    def setUp(self) -> None:
        self.verifier = Verifier(REPO.store, REPO.index)

    def relocate(self, name: str, citation: Citation | None = None):
        return self.verifier.relocate(citation or click(), REPO.commits[name])

    def test_same_blob_is_identical(self) -> None:
        result = self.relocate("negative")
        self.assertEqual((result.outcome, result.freshness), (IDENTICAL, CURRENT))
        self.assertEqual(result.current.commit, REPO.commits["negative"])

    def test_line_shift_relocates_unchanged_bytes(self) -> None:
        result = self.relocate("shift")
        self.assertEqual((result.outcome, result.freshness), (RELOCATED, CURRENT))
        self.assertEqual((result.current.start_line, result.current.end_line), (7, 7))
        self.assertEqual(result.current.span_sha256, self.verifier.check(click()).observed.span_sha256)

    def test_unrelated_edit_keeps_the_span_unchanged(self) -> None:
        favorite = Citation(REPO.commits["base"], "src/weights.rs", 2, 2, "FAVORITE_WEIGHT")
        result = self.relocate("literal", favorite)
        self.assertEqual((result.outcome, result.freshness), (UNCHANGED, CURRENT))

    def test_pure_rename_relocates_by_blob_id_without_an_index(self) -> None:
        result = Verifier(REPO.store).relocate(click(), REPO.commits["rename"])
        self.assertEqual((result.outcome, result.freshness), (RELOCATED, CURRENT))
        self.assertEqual(result.current.path, "src/params/weights.rs")
        self.assertEqual(result.search["by_blob_id"], ["src/params/weights.rs"])

    def test_prefetch_gives_the_same_results_through_one_git_process(self) -> None:
        base = REPO.commits["base"]
        paths = ["src/weights.rs", "README.md", "missing/file.rs", "src/weights.rs"]
        single = {}
        for path in dict.fromkeys(paths):
            try:
                single[path] = REPO.store.read_blob(base, path)[2]
            except NotFound as exc:
                single[path] = type(exc)
        verifier = Verifier(REPO.store)
        with mock.patch.object(REPO.store, "read_blob", side_effect=AssertionError("one by one")):
            verifier.prefetch(base, paths)
            verifier.prefetch(base, paths)  # already cached: nothing is read
            verifier.prefetch(REPO.commits["shift"], ["src/weights.rs"])
            self.assertEqual(verifier.check(click()).verdict, INTACT)
            self.assertEqual(verifier.relocate(click(), REPO.commits["shift"]).outcome, RELOCATED)
            for path, expected in single.items():
                if isinstance(expected, bytes):
                    self.assertEqual(verifier._blob(base, path).data, expected)
                else:
                    with self.assertRaises(expected):
                        verifier._blob(base, path)
        _, results = REPO.store.read_blobs(base, paths)
        self.assertEqual(sorted(results), sorted(set(paths)))
        self.assertEqual(results["src/weights.rs"][1], single["src/weights.rs"])
        self.assertIsInstance(results["missing/file.rs"], NotFound)
        # a changed span placed without a proposal: same outcome, no diff computed
        bare = Verifier(REPO.store).relocate(click(), REPO.commits["literal"], propose=False)
        full = Verifier(REPO.store).relocate(click(), REPO.commits["literal"])
        self.assertEqual((bare.outcome, bare.freshness), (full.outcome, full.freshness))
        self.assertEqual((bare.proposed, bare.diff), (None, None))
        self.assertIn("no line-diff proposal was requested", bare.reason)
        self.assertIsNotNone(full.proposed)

    def test_rename_with_edit_needs_the_index(self) -> None:
        without = Verifier(REPO.store).relocate(click(), REPO.commits["rename_edit"])
        self.assertEqual((without.outcome, without.freshness), (MISSING_SPAN, UNVERIFIABLE))
        self.assertIn("same blob id only", without.reason)
        REPO.indexed("rename_edit")
        found = Verifier(REPO.store, REPO.index).relocate(click(), REPO.commits["rename_edit"])
        self.assertEqual((found.outcome, found.freshness), (RELOCATED, CURRENT))
        self.assertEqual((found.current.path, found.current.start_line),
                         ("src/params/weights.rs", 4))
        self.assertTrue(found.search["complete"])

    def test_changed_literal_is_stale_with_a_proposal(self) -> None:
        result = self.relocate("literal")
        self.assertEqual((result.outcome, result.freshness), (CHANGED_SPAN, STALE))
        self.assertIsNone(result.current)
        self.assertEqual((result.proposed["start_line"], result.proposed["end_line"]), (4, 4))
        self.assertEqual(result.proposed["anchor_verdict"], "FOUND")
        self.assertIn("-pub const CLICK_WEIGHT: f64 = 0.4;", result.diff)
        self.assertIn("+pub const CLICK_WEIGHT: f64 = 0.3;", result.diff)

    def test_duplicated_span_is_ambiguous_and_nothing_is_chosen(self) -> None:
        result = self.relocate("duplicate")
        self.assertEqual((result.outcome, result.freshness), (AMBIGUOUS_SPAN, UNVERIFIABLE))
        self.assertIsNone(result.current)
        self.assertEqual([(c["start_line"], c["end_line"]) for c in result.candidates],
                         [(4, 4), (10, 10)])
        self.assertIn("no occurrence is chosen", result.reason)

    def test_deleted_file_is_missing(self) -> None:
        result = self.relocate("delete")
        self.assertEqual((result.outcome, result.freshness), (MISSING_SPAN, UNVERIFIABLE))

    def test_a_missing_span_names_the_files_the_index_did_not_search(self) -> None:
        """FA-025: the relocation search covers the lexically indexed files only; a span
        that moved into a binary file is reported missing, and the reason says which files
        were searched and how many were skipped, by reason, instead of "the code index"."""
        with tempfile.TemporaryDirectory(prefix="txray-coverage-") as tmp:
            root = Path(tmp)
            git_dir, base = build_fixture_repo(root / "upstream", {"src/weights.rs": WEIGHTS,
                                                                  "docs/notes.md": NOTES},
                                               frozenset())
            moved = add_commit(git_dir, {"docs/notes.md": NOTES,
                                         "assets/weights.bin": WEIGHTS + b"\x00\x01"}, base)
            url = file_url(git_dir)
            store = SnapshotStore(root / "store")
            for commit in (base, moved):
                store.pin(commit, url, allowlist=Allowlist([url]))
            index = CodeIndex(store)
            index.build(moved)
            citation = Citation(base, "src/weights.rs", 4, 4, "CLICK_WEIGHT")
            result = Verifier(store, index).relocate(citation, moved)
        self.assertEqual((result.outcome, result.freshness), (MISSING_SPAN, UNVERIFIABLE))
        self.assertTrue(result.search["complete"])
        coverage = result.search["lexical_coverage"]
        self.assertEqual((coverage["files"], coverage["lexically_indexed"]), (2, 1))
        self.assertEqual(sum(coverage["not_lexically_indexed"].values()), 1)
        self.assertIn("searched: the lexically indexed files of the target (1 of 2 files; "
                      "1 not lexically indexed: ", result.reason)
        self.assertNotIn("the code index of the target", result.reason)

    def test_an_unusable_citation_is_unverifiable(self) -> None:
        bad = Citation(REPO.commits["base"], "src/weights.rs", 2, 2, "CLICK_WEIGHT")
        result = self.relocate("shift", bad)
        self.assertEqual((result.outcome, result.freshness), (UNUSABLE, UNVERIFIABLE))
        self.assertIn("anchor MISSING", result.reason)

    def test_matches_must_be_whole_lines(self) -> None:
        # The cited line survives only as the tail of "// was: ...": not a whole-line match.
        result = self.relocate("embedded")
        self.assertEqual((result.outcome, result.freshness), (CHANGED_SPAN, STALE))
        self.assertEqual(result.anchor_at_target["count"], 2)


class MultipleAnchorTest(unittest.TestCase):
    """An anchor found several times inside its span confirms it; relocation needs uniqueness.

    At the finding's own commit the anchor ``BOOST`` occurs three times inside the cited
    line (``FOUND_MULTIPLE``), which confirms the span. Relocating the span to another
    commit matches its exact bytes: once is ``relocated`` (``CURRENT``), twice is
    ``ambiguous`` (``UNVERIFIABLE``) with a reason and both candidates, never the first.
    """

    LINE = b"pub const BOOST: f64 = BOOST_BASE * BOOST_SCALE;\n"
    BASE = b"// rules\n" + LINE + b"pub fn f() {}\n"

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-multi-anchor-")
        root = Path(cls._tmp.name)
        git_dir, base = build_fixture_repo(root / "upstream", {"src/rules.rs": cls.BASE},
                                           frozenset())
        moved = add_commit(git_dir, {"src/rules.rs": b"// a\n// b\n" + cls.BASE}, base)
        twice = add_commit(git_dir, {"src/rules.rs": cls.BASE + b"\n" + cls.LINE}, moved)
        cls.commits = {"base": base, "moved": moved, "twice": twice}
        url = file_url(git_dir)
        cls.store = SnapshotStore(root / "store")
        for commit in cls.commits.values():
            cls.store.pin(commit, url, allowlist=Allowlist([url]))
        cls.root = root
        cls.citation = Citation(base, "src/rules.rs", 2, 2, "BOOST")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_own_commit_confirms_with_every_occurrence_reported(self) -> None:
        check = Verifier(self.store).check(self.citation)
        self.assertEqual((check.verdict, check.anchor_verdict, check.usable),
                         (INTACT, "FOUND_MULTIPLE", True))
        self.assertEqual(check.anchor_lines, (2, 2, 2))

    def test_unique_relocation_is_current(self) -> None:
        result = Verifier(self.store).relocate(self.citation, self.commits["moved"])
        self.assertEqual((result.outcome, result.freshness), (RELOCATED, CURRENT))
        self.assertEqual((result.current.start_line, result.current.end_line), (4, 4))

    def test_ambiguous_relocation_is_unverifiable_and_never_first_match(self) -> None:
        result = Verifier(self.store).relocate(self.citation, self.commits["twice"])
        self.assertEqual((result.outcome, result.freshness), (AMBIGUOUS_SPAN, UNVERIFIABLE))
        self.assertIsNone(result.current)
        self.assertEqual([(c["start_line"], c["end_line"]) for c in result.candidates],
                         [(2, 2), (5, 5)])
        self.assertIn("occur 2 times", result.reason)
        self.assertIn("no occurrence is chosen", result.reason)

    def test_findings_memory_applies_the_rule(self) -> None:
        memory = FindingsMemory(Ledger(self.root / "ledger"), self.store)
        memory.add({"finding_id": "F-boost", "title": "Boost constant",
                    "claim": "BOOST is the product of BOOST_BASE and BOOST_SCALE.",
                    "component": "src", "evidence_class": "CODE", "status": "SUPPORTED",
                    "citations": [{"commit": self.commits["base"], "path": "src/rules.rs",
                                   "lines": "2", "anchor": "BOOST"}]}, AUTHOR)
        [verified] = memory.verify(["F-boost"])
        self.assertEqual(verified["freshness"], CURRENT)
        moved = memory.reanchor(self.commits["moved"], ["F-boost"])["results"][0]
        self.assertEqual((moved["freshness"], moved["provenance"]), (CURRENT, True))
        twice = memory.reanchor(self.commits["twice"], ["F-boost"])["results"][0]
        self.assertEqual((twice["freshness"], twice["triggers"]), (UNVERIFIABLE, ["ambiguous_span"]))


class ReanchorFindingsTest(unittest.TestCase):
    """Finding-level re-anchoring: provenance, review items, dependencies, negatives."""

    def setUp(self) -> None:
        self.memory = REPO.memory()

    def add(self, **overrides):
        return self.memory.add(spec(REPO, **overrides), AUTHOR)

    def reanchor(self, name: str, *ids: str) -> dict:
        report = self.memory.reanchor(REPO.commits[name], list(ids) or None)
        return {result["finding_id"]: result for result in report["results"]}

    def test_line_shift_appends_a_provenance_revision(self) -> None:
        self.add(finding_id="F-click")
        result = self.reanchor("shift")["F-click"]
        self.assertEqual((result["freshness"], result["provenance"]), (CURRENT, True))
        state = self.memory.get("F-click")
        self.assertEqual(len(state.provenance), 2)
        self.assertEqual(state.provenance[0]["citations"][0]["start_line"], 4)
        self.assertEqual(state.provenance[1]["citations"][0]["start_line"], 7)
        self.assertEqual(state.provenance[1]["commit"], REPO.commits["shift"])
        self.assertEqual(state.record["sources"][0]["start_line"], 4)  # the old citation is kept
        self.assertEqual(state.freshness(), (CURRENT, REPO.commits["shift"]))
        self.assertEqual(state.queue_items()[0]["trigger"], "awaiting_review")

    def test_literal_change_is_stale_and_queued(self) -> None:
        self.add(finding_id="F-click")
        result = self.reanchor("literal")["F-click"]
        self.assertEqual((result["freshness"], result["triggers"]), (STALE, ["changed_span"]))
        state = self.memory.get("F-click")
        self.assertEqual((state.status, state.status_basis), ("SUPPORTED", "proposed"))
        item = self.memory.view().queue()[0]
        self.assertEqual((item["trigger"], item["priority"]), ("changed_span", 1))
        self.assertEqual(item["detail"]["proposed"]["start_line"], 4)
        self.assertIn("0.3", item["detail"]["diff"])
        self.assertEqual(item["detail"]["claim"], state.record["claim"])

    def test_duplicate_anchor_is_unverifiable(self) -> None:
        self.add(finding_id="F-click")
        result = self.reanchor("duplicate")["F-click"]
        self.assertEqual((result["freshness"], result["triggers"]),
                         (UNVERIFIABLE, ["ambiguous_span"]))

    def test_rename(self) -> None:
        self.add(finding_id="F-click")
        result = self.reanchor("rename")["F-click"]
        self.assertEqual((result["freshness"], result["outcomes"]), (CURRENT, {RELOCATED: 1}))
        state = self.memory.get("F-click")
        self.assertEqual(state.provenance[-1]["citations"][0]["path"], "src/params/weights.rs")

    def test_span_dependency_change(self) -> None:
        self.add(finding_id="F-filters", title="Synthetic filter list",
                 claim="The synthetic filter list names two filters.", evidence_class="CODE",
                 citations=[REPO.cite("base", "src/filters.py", "1-4", '"AgeFilter"')],
                 depends_on=[{"span": REPO.cite("base", "src/filters.py", "6-7", "def apply")}])
        result = self.reanchor("dependency")["F-filters"]
        self.assertEqual(result["outcomes"], {UNCHANGED: 1})  # its own span did not change
        self.assertEqual((result["freshness"], result["triggers"]), (STALE, ["dependency_changed"]))
        self.assertEqual(self.reanchor("negative")["F-filters"]["freshness"], CURRENT)

    def test_finding_dependency_change(self) -> None:
        self.add(finding_id="F-click")
        self.add(finding_id="F-fav", citations=[REPO.cite("base", "src/weights.rs", "2",
                                                          "FAVORITE_WEIGHT")],
                 depends_on=[{"finding": "F-click"}])
        results = self.reanchor("literal", "F-fav")
        self.assertEqual(list(results), ["F-click", "F-fav"])  # dependencies first
        self.assertEqual(results["F-fav"]["outcomes"], {UNCHANGED: 1})
        self.assertEqual((results["F-fav"]["freshness"], results["F-fav"]["triggers"]),
                         (STALE, ["dependency_changed"]))
        shifted = self.reanchor("shift")
        self.assertEqual({k: v["freshness"] for k, v in shifted.items()},
                         {"F-click": CURRENT, "F-fav": CURRENT})
        self.memory.retract("F-click", AUTHOR, "synthetic retraction")
        again = self.reanchor("shift", "F-fav")
        self.assertEqual(again["F-fav"]["freshness"], STALE)

    def test_negative_search_is_rerun(self) -> None:
        state = self.add(finding_id="F-neg", title="No boost weight",
                         claim="The synthetic fixture defines no boost weight.",
                         evidence_class="CODE", status="NOT_FOUND", citations=[],
                         negative={"commit": REPO.commits["base"], "query": "BOOST_WEIGHT",
                                   "path": "src/*"})
        negative = state.record["negative"]
        self.assertEqual((negative["total"], negative["truncated"]), (0, False))
        self.assertEqual(negative["coverage"]["paths_searched"], 2)
        self.assertEqual(negative["coverage"]["languages_searched"], {"python": 1, "rust": 1})
        self.assertEqual(negative["generation"]["commit"], REPO.commits["base"])
        self.assertEqual(self.reanchor("shift")["F-neg"]["freshness"], CURRENT)
        found = self.reanchor("negative")["F-neg"]
        self.assertEqual((found["freshness"], found["triggers"]), (STALE, ["negative_now_found"]))
        item = [i for i in self.memory.view().queue() if i["trigger"] == "negative_now_found"][0]
        self.assertEqual(item["detail"]["hits"][0]["path"], "src/boost.py")
        missing_index = self.reanchor("literal")["F-neg"]
        self.assertEqual((missing_index["freshness"], missing_index["triggers"]),
                         (UNVERIFIABLE, ["negative_unverifiable"]))

    def test_checks_never_change_the_status(self) -> None:
        self.add(finding_id="F-click")
        for name in ("literal", "duplicate", "delete", "shift"):
            self.reanchor(name)
        self.memory.verify()
        state = self.memory.get("F-click")
        self.assertEqual((state.status, state.status_basis, state.workflow),
                         ("SUPPORTED", "proposed", "draft"))
        self.assertEqual(sorted(state.targets), sorted(REPO.commits[n] for n in
                                                       ("literal", "duplicate", "delete", "shift")))
        self.assertEqual(state.freshness(REPO.commits["literal"])[0], STALE)
        self.assertEqual(state.freshness(REPO.commits["shift"])[0], CURRENT)

    def test_resolving_citations_retires_checks_made_from_unresolved_ones(self) -> None:
        """A re-anchoring check made while the cited commit was not pinned (the citation
        was unusable) no longer counts once a later verify resolves the citation; the
        finding must be re-anchored again, and verify says so."""
        import json
        import tempfile
        from pathlib import Path

        from timelinexray.snapshot import SnapshotStore
        from timelinexray.findings import FindingsMemory, Ledger

        with tempfile.TemporaryDirectory(prefix="txray-partial-") as tmp:
            root = Path(tmp)
            store = SnapshotStore(root / "store")
            store.pin(REPO.commits["shift"], REPO.url, allowlist=REPO.allowlist)
            memory = FindingsMemory(Ledger(root / "ledger"), store)
            finding = {
                "id": "R-1", "title": "Click weight", "claim": "Synthetic.", "component": "src",
                "evidence_class": "PARAM_DEFAULT", "status": "SUPPORTED",
                "sources": [{"kind": "code", "path": "src/weights.rs", "lines": "4",
                             "commit": REPO.commits["base"][:12], "anchor": "CLICK_WEIGHT"}],
                "creator_relevance": "low", "creator_controllable": "no", "implication": "",
                "volatility": "param", "misuse_risk": "low", "note": "",
            }
            source = root / "r.json"
            source.write_text(json.dumps({"findings": [finding]}), "utf-8")
            report = memory.import_file(source, "R", Actor("importer-1", "importer"))
            self.assertEqual(report["unpinned_commits"], {REPO.commits["base"][:12]: 1})
            shift = REPO.commits["shift"]
            outcome = memory.reanchor(shift)["results"][0]
            self.assertEqual((outcome["freshness"], outcome["outcomes"]),
                             (UNVERIFIABLE, {"unusable": 1}))
            self.assertEqual(memory.get("R:R-1").freshness(), (UNVERIFIABLE, shift))

            store.pin(REPO.commits["base"], REPO.url, allowlist=REPO.allowlist)
            [result] = memory.verify(["R:R-1"])
            self.assertEqual((result["freshness"], result["resolved_citations"],
                              result["recheck_targets"]), (CURRENT, 1, [shift]))
            state = memory.get("R:R-1")
            self.assertEqual(state.freshness(), (CURRENT, "cited"))
            self.assertEqual(state.freshness(shift), ("NOT_CHECKED", shift))
            self.assertIsNone(state.target_check(shift))
            self.assertEqual(state.resolved_citations()[0]["commit"], REPO.commits["base"])
            again = memory.reanchor(shift)["results"][0]
            self.assertEqual((again["freshness"], again["outcomes"]), (CURRENT, {"relocated": 1}))
            self.assertEqual(memory.get("R:R-1").freshness(), (CURRENT, shift))

    def test_a_new_check_of_a_target_replaces_its_items(self) -> None:
        self.add(finding_id="F-click")
        self.reanchor("literal")
        self.reanchor("duplicate")
        self.assertEqual(sorted(i["trigger"] for i in self.memory.view().queue()
                                if i["scope"] is not None), ["ambiguous_span", "changed_span"])
        reviewer = Actor("reviewer-b", "reviewer")
        self.memory.verify(["F-click"])
        self.memory.review("F-click", reviewer, "SUPPORTED", "Span read at its own commit.")
        # the review assessed the cited commit: the targets' items are not closed by it
        self.assertEqual(sorted(i["trigger"] for i in self.memory.view().queue()),
                         ["ambiguous_span", "changed_span"])
        self.reanchor("duplicate")  # a new check of a target replaces that target's items
        self.assertEqual(sorted(i["trigger"] for i in self.memory.view().queue()),
                         ["ambiguous_span", "changed_span"])
        self.memory.review("F-click", reviewer, "SUPPORTED", "Assessed at the duplicate target.",
                           target=REPO.commits["duplicate"])
        self.assertEqual([i["trigger"] for i in self.memory.view().queue()], ["changed_span"])
        state = self.memory.get("F-click")
        self.assertEqual(state.freshness(), (UNVERIFIABLE, REPO.commits["duplicate"]))


if __name__ == "__main__":
    unittest.main()
