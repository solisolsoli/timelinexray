"""Write gate, review rules (no self-approval), supersession, retraction and the queue."""

from __future__ import annotations

import unittest

from timelinexray.errors import InvalidInput, NotFound, Refused
from timelinexray.findings import Actor
from timelinexray.verify import CURRENT, STALE
from tests.findings_support import AUTHOR, REVIEWER, HistoryRepo, spec

REPO: HistoryRepo


def setUpModule() -> None:
    global REPO
    REPO = HistoryRepo()
    REPO.indexed("base")


def tearDownModule() -> None:
    REPO.cleanup()


class WriteGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = REPO.memory()

    def refused(self, error: type[Exception], text: str, **overrides) -> None:
        with self.assertRaises(error) as raised:
            self.memory.add(spec(REPO, **overrides), AUTHOR)
        self.assertIn(text, str(raised.exception))
        self.assertEqual(self.memory.view().events, 0)

    def test_a_valid_finding_records_the_full_span(self) -> None:
        state = self.memory.add(spec(REPO), AUTHOR)
        cited = state.record["sources"][0]
        self.assertEqual(cited["commit"], REPO.commits["base"])
        self.assertEqual((cited["start_line"], cited["end_line"]), (4, 4))
        self.assertEqual(len(cited["span_sha256"]), 64)
        self.assertEqual(len(cited["blob_oid"]), 40)
        self.assertEqual(state.record["scope"], "public_default")
        self.assertTrue(state.finding_id.startswith("F-"))
        self.assertEqual((state.workflow, state.status, state.status_basis),
                         ("draft", "SUPPORTED", "proposed"))

    def test_anchor_must_occur_inside_the_span(self) -> None:
        self.refused(Refused, "is MISSING", citations=[REPO.cite("base", "src/weights.rs", "2",
                                                                  "CLICK_WEIGHT")])
        # an anchor occurring several times inside the cited lines confirms the span
        state = self.memory.add(spec(REPO, finding_id="F-multi", citations=[
            REPO.cite("base", "src/weights.rs", "2-7", "WEIGHT")]), AUTHOR)
        self.assertEqual(state.record["sources"][0]["end_line"], 7)
        [result] = self.memory.verify(["F-multi"])
        self.assertEqual((result["freshness"], result["verdict"]), ("CURRENT", "INTACT"))

    def test_expected_span_hash_must_match(self) -> None:
        cite = {**REPO.cite("base", "src/weights.rs", "4", "CLICK_WEIGHT"), "span_sha256": "0" * 64}
        self.refused(Refused, "not the expected", citations=[cite])

    def test_unreadable_citations_are_refused(self) -> None:
        self.refused(Refused, "cannot be read", citations=[REPO.cite("base", "src/none.rs", "1", "x")])
        self.refused(Refused, "cannot be read",
                     citations=[{"commit": "abcdef0", "path": "src/weights.rs", "lines": "4",
                                 "anchor": "CLICK_WEIGHT"}])

    def test_class_specific_rules(self) -> None:
        self.refused(InvalidInput, "public_default", scope="public_code")
        self.refused(InvalidInput, "needs at least one code citation", citations=[],
                     evidence_class="CODE")
        self.refused(InvalidInput, "needs a negative search", status="NOT_FOUND", citations=[],
                     evidence_class="CODE")
        self.refused(Refused, "has 1 hit(s)", status="NOT_FOUND", citations=[],
                     evidence_class="CODE",
                     negative={"commit": REPO.commits["base"], "query": "REPLY_WEIGHT: f64"})
        self.refused(InvalidInput, "belongs to a NOT_FOUND finding",
                     negative={"commit": REPO.commits["base"], "query": "BOOST"})
        self.refused(InvalidInput, "unknown keys", extra=1)

    def test_web_only_findings_and_date_precision(self) -> None:
        state = self.memory.add(spec(REPO, evidence_class="OFFICIAL", status="EXTERNAL_RECHECK",
                                     citations=[], web_sources=[{
                                         "url": "https://example.com/synthetic",
                                         "publisher": "Example", "published": None,
                                         "retrieved": "2026-01-01", "quote": ""}]), AUTHOR)
        source = state.record["sources"][0]
        self.assertEqual((source["kind"], source["published_precision"]), ("web", "unknown"))
        self.assertEqual(state.record["scope"], "external")
        self.assertEqual(self.memory.verify(),
                         [{"finding_id": state.finding_id,
                           "skipped": "no code citations, dependencies or negative search"}])

    def test_duplicate_ids_and_missing_dependencies(self) -> None:
        self.memory.add(spec(REPO, finding_id="F-one"), AUTHOR)
        with self.assertRaises(Refused):
            self.memory.add(spec(REPO, finding_id="F-one"), AUTHOR)
        with self.assertRaises(Refused):
            self.memory.add(spec(REPO, finding_id="F-two", depends_on=[{"finding": "F-none"}]),
                            AUTHOR)

    def test_roles(self) -> None:
        with self.assertRaises(Refused):
            self.memory.add(spec(REPO), Actor("tool", "verifier"))
        with self.assertRaises(InvalidInput):
            Actor("has space", "author")
        with self.assertRaises(InvalidInput):
            Actor("agent", "superuser")


class ReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = REPO.memory()
        self.memory.add(spec(REPO, finding_id="F-click"), AUTHOR)

    def test_no_self_approval(self) -> None:
        self.memory.verify(["F-click"])
        for actor in (Actor("agent-a", "reviewer"), Actor("AGENT-A", "maintainer")):
            with self.subTest(actor=actor):
                with self.assertRaises(Refused) as raised:
                    self.memory.review("F-click", actor, "SUPPORTED", "Looks right.")
                self.assertIn("no self-approval", str(raised.exception))
        for actor in (Actor("reviewer-b", "author"), Actor("reviewer-b", "verifier")):
            with self.subTest(role=actor.role), self.assertRaises(Refused):
                self.memory.review("F-click", actor, "SUPPORTED", "Looks right.")
        state = self.memory.get("F-click")
        self.assertEqual((state.status_basis, state.reviews), ("proposed", []))

    def test_review_needs_a_current_integrity_check(self) -> None:
        with self.assertRaises(Refused) as raised:
            self.memory.review("F-click", REVIEWER, "SUPPORTED", "Looks right.")
        self.assertIn("never verified", str(raised.exception))
        self.memory.review("F-click", REVIEWER, "CONTRADICTED", "The value differs elsewhere.")
        state = self.memory.get("F-click")
        self.assertEqual((state.status, state.status_basis, state.status_by, state.workflow),
                         ("CONTRADICTED", "reviewed", "reviewer-b", "reviewed"))

    def test_supported_and_stale_coexist(self) -> None:
        self.memory.verify(["F-click"])
        self.memory.reanchor(REPO.commits["literal"])
        state = self.memory.review("F-click", REVIEWER, "SUPPORTED",
                                   "Supported at its cited commit; stale at the target.")
        self.assertEqual((state.status, state.freshness()),
                         ("SUPPORTED", (STALE, REPO.commits["literal"])))
        self.assertEqual(state.integrity["freshness"], CURRENT)
        self.assertEqual(state.reviews[0]["integrity_event"], state.integrity["event"])
        # the review assessed the cited commit only: the STALE target's item stays open
        self.assertEqual((state.reviews[0]["closed_items"], state.reviews[0]["closed_scopes"]),
                         (0, ["cited"]))
        self.assertEqual([(i["priority"], i["trigger"], i["scope"]) for i in state.queue_items()],
                         [(1, "changed_span", REPO.commits["literal"])])

    def test_a_review_closes_only_the_scopes_it_assessed(self) -> None:
        self.memory.verify(["F-click"])
        self.memory.reanchor(REPO.commits["literal"])
        self.memory.reanchor(REPO.commits["duplicate"])
        before = [(i["trigger"], i["scope"]) for i in self.memory.view().queue()]
        self.assertEqual(sorted(before), sorted([
            ("changed_span", REPO.commits["literal"]),
            ("ambiguous_span", REPO.commits["duplicate"]),
            ("awaiting_review", None)]))
        with self.assertRaises(Refused) as raised:  # a target without a recorded check
            self.memory.review("F-click", REVIEWER, "SUPPORTED", "Read it.",
                               target=REPO.commits["shift"])
        self.assertIn("no recorded re-anchoring check", str(raised.exception))
        state = self.memory.review("F-click", REVIEWER, "SUPPORTED", "Read at the cited commit.")
        self.assertEqual(sorted((i["trigger"], i["scope"]) for i in state.queue_items()),
                         sorted([("changed_span", REPO.commits["literal"]),
                                 ("ambiguous_span", REPO.commits["duplicate"])]))
        state = self.memory.review("F-click", REVIEWER, "SUPPORTED",
                                   "Checked the duplicate too.", target=REPO.commits["duplicate"])
        self.assertEqual(state.reviews[-1]["assessed_target"], REPO.commits["duplicate"])
        self.assertEqual(state.reviews[-1]["closed_scopes"], ["cited", REPO.commits["duplicate"]])
        self.assertEqual([(i["trigger"], i["scope"]) for i in state.queue_items()],
                         [("changed_span", REPO.commits["literal"])])
        # the STALE finding is still in the ledger-wide queue after both reviews
        self.assertEqual([i["finding_id"] for i in self.memory.view().queue()], ["F-click"])

    def test_supersede_and_retract(self) -> None:
        new = spec(REPO, finding_id="F-click-2", claim="The synthetic fixture sets CLICK_WEIGHT "
                   "to 0.3 as a public default.",
                   citations=[REPO.cite("literal", "src/weights.rs", "4", "CLICK_WEIGHT")])
        editor = Actor("editor-c", "author")
        old = self.memory.supersede("F-click", editor, spec=new, rationale="Literal changed.")
        self.assertEqual((old.workflow, old.superseded_by), ("superseded", "F-click-2"))
        successor = self.memory.get("F-click-2")
        self.assertEqual((successor.supersedes, successor.created_by.name),
                         (["F-click"], "editor-c"))
        with self.assertRaises(Refused):
            self.memory.review("F-click", REVIEWER, "CONTRADICTED", "Old value.")
        self.memory.verify(["F-click-2"])
        with self.assertRaises(Refused):  # the superseding author cannot approve it either
            self.memory.review("F-click-2", Actor("editor-c", "reviewer"), "SUPPORTED", "Mine.")
        self.memory.review("F-click-2", REVIEWER, "SUPPORTED", "Span read at the new commit.")
        self.memory.add(spec(REPO, finding_id="F-other"), AUTHOR)
        self.memory.supersede("F-click-2", editor, by="F-other", rationale="Merged.")
        with self.assertRaises(Refused):
            self.memory.supersede("F-other", editor, by="F-click", rationale="Back.")
        retracted = self.memory.retract("F-other", AUTHOR, "Synthetic retraction.")
        self.assertEqual(retracted.workflow, "retracted")
        with self.assertRaises(Refused):
            self.memory.retract("F-other", AUTHOR, "Again.")
        view = self.memory.view()
        self.assertEqual([s.finding_id for s in view.states() if s.active], [])
        with self.assertRaises(NotFound):
            self.memory.get("F-none")

    def test_queue_orders_by_priority(self) -> None:
        self.memory.add(spec(REPO, finding_id="F-fav", citations=[
            REPO.cite("base", "src/weights.rs", "2", "FAVORITE_WEIGHT")]), AUTHOR)
        self.memory.reanchor(REPO.commits["literal"])
        items = self.memory.view().queue()
        self.assertEqual([(i["priority"], i["trigger"], i["finding_id"]) for i in items],
                         [(1, "changed_span", "F-click"), (3, "awaiting_review", "F-click"),
                          (3, "awaiting_review", "F-fav")])
        self.assertIn("review", items[0]["allowed_actions"][0])


if __name__ == "__main__":
    unittest.main()
