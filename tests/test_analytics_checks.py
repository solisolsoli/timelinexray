"""M3 and M4 exist only as records: no scores, points, percentages or probabilities."""

from __future__ import annotations

import dataclasses
import typing
import unittest

from timelinexray.analytics import checks

RECORDS = (checks.ChecklistItem, checks.ChecklistAnswer, checks.Citation, checks.VisibilityFlag,
           checks.FlagTime, checks.FlagContext)


class ChecklistTest(unittest.TestCase):
    def test_records_carry_no_numbers(self) -> None:
        for record in RECORDS:
            hints = typing.get_type_hints(record)
            for field in dataclasses.fields(record):
                with self.subTest(record=record.__name__, field=field.name):
                    self.assertFalse(any(word in field.name
                                         for word in checks.FORBIDDEN_FIELD_WORDS))
                    kinds = typing.get_args(hints[field.name]) or (hints[field.name],)
                    self.assertFalse(set(kinds) & {int, float, complex, bool})

    def test_checklist_items_separate_evidence_from_applicability(self) -> None:
        self.assertEqual([item.check_id for item in checks.M3_CHECKLIST],
                         ["m3-audience-access", "m3-post-type", "m3-labels",
                          "m3-authentic-engagement"])
        for item in checks.M3_CHECKLIST:
            with self.subTest(check=item.check_id):
                self.assertIn(item.evidence_status, ("SUPPORTED", "PARTIAL", "NOT_FOUND",
                                                     "CONTRADICTED", "EXTERNAL_RECHECK"))
                self.assertIn(item.evidence_class, ("CODE", "OFFICIAL"))
                self.assertEqual(item.freshness, "NOT_CHECKED")
                self.assertIn("not reviewed", item.status_origin)
                self.assertNotIn("applicability", dataclasses.asdict(item))
        self.assertEqual({a.value for a in checks.Applicability},
                         {"pass", "fail", "unknown", "not-applicable"})
        answer = checks.ChecklistAnswer("m3-post-type", checks.Applicability.UNKNOWN)
        self.assertEqual(answer.applicability, "unknown")

    def test_no_check_rewards_engagement_requests(self) -> None:
        for item in checks.M3_CHECKLIST:
            text = " ".join((item.question, item.source_establishes,
                             item.honest_interpretation)).lower()
            for phrase in ("ask for likes", "ask for replies", "request likes", "points"):
                self.assertNotIn(phrase, text)

    def test_visibility_flag_vocabulary(self) -> None:
        self.assertEqual({c.value for c in checks.Conclusion},
                         {"reported-observation", "possible-effect-under-stated-conditions",
                          "insufficient-information"})
        self.assertEqual({r.value for r in checks.Remediation},
                         {"review-accurate-labeling", "applicable-appeal-procedure", "none"})
        self.assertEqual({s.value for s in checks.FlagScope},
                         {"account-label", "post-label-aggregate", "identified-post"})
        flag = checks.VisibilityFlag(
            observation="label shown in the report, as printed",
            scope=checks.FlagScope.ACCOUNT_LABEL,
            time=checks.FlagTime("2026-08-01", "2026-08-31", None, "2026-09-30", None),
            canonical_label=None, rule=None, rule_commit=None, cited_predicate=None,
            context=checks.FlagContext(None, None, None, None, None),
            conclusion=checks.Conclusion.INSUFFICIENT_INFORMATION,
            remediation=checks.Remediation.NONE,
        )
        self.assertEqual(flag.conclusion, "insufficient-information")
        for text, status, klass, citations in checks.M4_EVIDENCE:
            self.assertEqual((status, klass), ("SUPPORTED", "CODE"))
            self.assertTrue(citations)


if __name__ == "__main__":
    unittest.main()
