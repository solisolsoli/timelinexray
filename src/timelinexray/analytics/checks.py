"""M3 pre-publish checklist and M4 visibility flags: data structures only (P6 §6).

Neither produces a number. Checklist items carry two independent fields, the evidence
status of what the source establishes and the applicability of the check to one proposed
post; there is no score, no percentage, no probability, and there are no points of any kind
(in particular none for asking people to like, reply, repost or share). Visibility flags
record what a user's report says, its scope, time and context, and a conclusion from a
closed vocabulary; they never translate labels into lost impressions.

Evidence statuses and classes below are those of the P6 research report (imported, not
reviewed in TimelineXray). Freshness is ``NOT_CHECKED`` until the findings ledger
(Milestone 3) re-verifies the cited spans.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .coefficients import UPSTREAM_COMMIT

STATUS_ORIGIN = "P6 research report, imported; not reviewed in TimelineXray"


class Applicability(str, Enum):
    """Whether a check holds for one proposed post; decided by the user, never scored."""

    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not-applicable"


@dataclass(frozen=True)
class Citation:
    """A commit-pinned code span or an official web page."""

    path: str | None = None
    lines: str | None = None
    commit: str | None = None
    anchor: str | None = None
    url: str | None = None
    retrieved: str | None = None


@dataclass(frozen=True)
class ChecklistItem:
    check_id: str
    question: str
    source_establishes: str
    evidence_status: str
    evidence_class: str
    freshness: str
    citations: tuple[Citation, ...]
    honest_interpretation: str
    status_origin: str = STATUS_ORIGIN


@dataclass(frozen=True)
class ChecklistAnswer:
    """The user's applicability answer for one check and one proposed post."""

    check_id: str
    applicability: Applicability
    note: str = ""


M3_CHECKLIST: tuple[ChecklistItem, ...] = (
    ChecklistItem(
        "m3-audience-access",
        "Is the intended audience compatible with the post's access setting?",
        "Subscription-tagged candidates are kept only for viewers subscribed to their author.",
        "SUPPORTED", "CODE", "NOT_CHECKED",
        (Citation("home-mixer/filters/ineligible_subscription_filter.rs", "14-29",
                  UPSTREAM_COMMIT, "subscribed_user_ids.contains"),),
        "An access restriction is not a prediction of impression volume.",
    ),
    ChecklistItem(
        "m3-post-type",
        "Has the post type (original, reply, repost) been recorded correctly?",
        "The mutual-follow reply boost excludes reply and retweet candidates.",
        "SUPPORTED", "CODE", "NOT_CHECKED",
        (Citation("xai-value-model/inputs.rs", "17-21", UPSTREAM_COMMIT,
                  "!self.is_reply && !self.is_retweet"),),
        "Do not apply an original-post coefficient convention to other post types.",
    ),
    ChecklistItem(
        "m3-labels",
        "Have available labels been checked with their scope and date?",
        "Home recommendation policies add recommendation-only filtering clauses.",
        "SUPPORTED", "CODE", "NOT_CHECKED",
        (Citation("visibility-filtering/rules/registry.rs", "268-287", UPSTREAM_COMMIT,
                  "timeline_home_recommendation_only"),),
        "A reported label needs contextual interpretation, not a universal deduction.",
    ),
    ChecklistItem(
        "m3-authentic-engagement",
        "Does the publication plan avoid artificial or coordinated engagement?",
        "X's authenticity policy prohibits inauthentic engagement manipulation.",
        "SUPPORTED", "OFFICIAL", "NOT_CHECKED",
        (Citation(url="https://help.x.com/en/rules-and-policies/authenticity",
                  retrieved="2026-09-30"),),
        "A policy-compliance check, not a promised ranking benefit.",
    ),
)


class FlagScope(str, Enum):
    ACCOUNT_LABEL = "account-label"
    POST_LABEL_AGGREGATE = "post-label-aggregate"
    IDENTIFIED_POST = "identified-post"


class Conclusion(str, Enum):
    REPORTED_OBSERVATION = "reported-observation"
    POSSIBLE_EFFECT_UNDER_CONDITIONS = "possible-effect-under-stated-conditions"
    INSUFFICIENT_INFORMATION = "insufficient-information"


class Remediation(str, Enum):
    """Only accurate labeling and appeal; evasion is not a remediation."""

    REVIEW_ACCURATE_LABELING = "review-accurate-labeling"
    APPLICABLE_APPEAL = "applicable-appeal-procedure"
    NONE = "none"


@dataclass(frozen=True)
class FlagTime:
    covered_start: str | None
    covered_end: str | None
    report_generated: str | None
    retrieved: str | None
    completeness: str | None


@dataclass(frozen=True)
class FlagContext:
    surface: str | None
    viewer_relationship: str | None
    geography: str | None
    age_or_settings: str | None
    access_conditions: str | None


@dataclass(frozen=True)
class VisibilityFlag:
    """One time-bounded observation from a user's own report, with its conditional reading.

    ``observation`` is the report's own wording (a label name, or a share of posts or days
    exactly as printed); the tool never sums overlapping labels, converts them into lost
    impressions, or reads "no labels reported" as unrestricted distribution.
    """

    observation: str
    scope: FlagScope
    time: FlagTime
    canonical_label: str | None
    rule: str | None
    rule_commit: str | None
    cited_predicate: Citation | None
    context: FlagContext
    conclusion: Conclusion
    remediation: Remediation


#: What the source says about Under the Hood reports, for M4 conclusions (P6 §6.2).
_UTH = "under-the-hood/strato/columns/underTheHoodReport.User.strato"

M4_EVIDENCE: tuple[tuple[str, str, str, tuple[Citation, ...]], ...] = (
    ("The monthly report starts from a prior calendar month and can fall back further "
     "according to readiness and available data; it is not a live clearance.",
     "SUPPORTED", "CODE",
     (Citation(_UTH, "152-165", UPSTREAM_COMMIT, "calendarMonthBefore(now)"),)),
    ("Post-label percentages use post counts and account-label percentages use days as "
     "denominators; neither measures impressions.",
     "SUPPORTED", "CODE",
     (Citation(_UTH, "261-263", UPSTREAM_COMMIT,
               "percentageOfPosts = formatPercentage(carried, postCount)"),
      Citation(_UTH, "297-299", UPSTREAM_COMMIT,
               "percentageOfDays = formatPercentage(days, periodDays.toLong)"))),
)

#: Field names that must never appear on these records (checked by the tests).
FORBIDDEN_FIELD_WORDS = ("score", "point", "probability", "percent", "weight", "rank", "reach")
