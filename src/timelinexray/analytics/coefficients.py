"""Versioned table of value-model action coefficients: public defaults at one upstream commit.

Every value here is a **public default** at commit
``77d431aabf409ca1c1eed9bec7e2183f7c914e23`` of ``xai-org/x-algorithm``, copied from
``home-mixer/params/param.rs``. The upstream README at that commit says that many tunable
values are read from a configuration system, that cron scripts set the defaults in the code
to the primary production values, and that experiments run on a share of traffic; a public
default is therefore not the value that any particular request used. In production the
coefficients multiply predicted probabilities (README lines 349-351), not exported counts.
TimelineXray uses them only as a fixed, versioned reference convention for the descriptive
M1 index (P6 §4).

Each entry records the chain from the summed head to its default:
``compute_weighted_score`` (``xai-value-model/scoring.rs``) applies ``weight_expr`` to the
head; the adapter (``home-mixer/scorers/value_model.rs``) fills ``field`` from ``param``;
``param`` is declared with ``switch`` and ``literal`` at ``lines`` of the parameter file.
``tests/test_analytics_coefficients.py`` re-reads all of it from the pinned upstream through
the snapshot span reader.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any

TABLE_ID = "x-algorithm-value-model-public-defaults"
TABLE_VERSION = 1
UPSTREAM_REPOSITORY = "https://github.com/xai-org/x-algorithm"
UPSTREAM_COMMIT = "77d431aabf409ca1c1eed9bec7e2183f7c914e23"
VALUE_LABEL = "public default"

PARAM_SOURCE = "home-mixer/params/param.rs"
SYNC_ANNOTATION_LINE = 1
SYNC_ANNOTATION = "// mirrored from config feature-switch defaults; last sync 2026-09-29T17:02:52Z"


@dataclass(frozen=True)
class SourceSpan:
    path: str
    start: int
    end: int

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "lines": f"{self.start}-{self.end}", "commit": UPSTREAM_COMMIT}


#: The 25 ``apply(...)`` terms that ``compute_weighted_score`` sums.
SUM_SOURCE = SourceSpan("xai-value-model/scoring.rs", 87, 116)
#: Eligibility-dependent weights (vqv, quoted vqv, post unexplored).
CONDITION_SOURCE = SourceSpan("xai-value-model/scoring.rs", 71, 85)
#: ``ValueModelWeights`` fields filled from parameters.
ADAPTER_SOURCE = SourceSpan("home-mixer/scorers/value_model.rs", 10, 39)
#: ``reply_weight_for`` and ``dwell_weight_for``.
BOOST_FUNCTION_SOURCE = SourceSpan("xai-value-model/weights.rs", 78, 94)
#: ``bidirectional_boost_eligible``: mutual-follow author, candidate neither reply nor retweet.
BOOST_ELIGIBILITY_SOURCE = SourceSpan("xai-value-model/inputs.rs", 18, 20)
#: Upstream README statements this module relies on, with an exact anchor in each span.
README_STATEMENTS: tuple[tuple[SourceSpan, str], ...] = (
    (SourceSpan("README.md", 351, 351),
     "they scale the predicted probabilities (or predicted continuous values, e.g. dwell time)"),
    (SourceSpan("README.md", 412, 412),
     "set the defaults in this repository's code to be the primary production values"),
)


@dataclass(frozen=True)
class Coefficient:
    """One weighted term of the value-model sum and its public default."""

    head: str
    weight_expr: str
    field: str
    param: str
    switch: str
    literal: str
    lines: tuple[int, int]
    condition: str | None = None

    @property
    def value(self) -> Fraction:
        """Exact value of the source literal."""
        return Fraction(Decimal(self.literal))

    @property
    def negative(self) -> bool:
        return self.value < 0

    def source(self) -> SourceSpan:
        return SourceSpan(PARAM_SOURCE, *self.lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "head": self.head,
            "value": float(self.value),
            "literal": self.literal,
            "label": VALUE_LABEL,
            "param": self.param,
            "switch": self.switch,
            "source": self.source().to_dict(),
            "condition": self.condition,
        }


def _c(head: str, expr: str, field: str, param: str, literal: str, lines: tuple[int, int],
       condition: str | None = None) -> Coefficient:
    switch = "rust_home_mixer_" + "".join(
        f"_{ch.lower()}" if ch.isupper() else ch for ch in param
    ).lstrip("_")
    return Coefficient(head, expr, field, param, switch, literal, lines, condition)


_REPLY_BOOST = ("reply_weight_for adds BidirectionalFollowReplyWeightBoost when the author is a "
                "mutual follow of the viewer and the candidate is neither a reply nor a retweet; "
                "the base value is used here")
_DWELL_BOOST = ("dwell_weight_for adds BidirectionalFollowDwellWeightBoost under the same "
                "mutual-follow condition")

#: The 25 terms, in the order ``compute_weighted_score`` sums them.
COEFFICIENTS: tuple[Coefficient, ...] = (
    _c("favorite_score", "weights.favorite", "favorite", "FavoriteWeight", "0.5", (302, 302)),
    _c("reply_score", "weights.reply_weight_for(candidate)", "reply", "ReplyWeight", "5.0",
       (303, 303), _REPLY_BOOST),
    _c("retweet_score", "weights.retweet", "retweet", "RetweetWeight", "1.0", (316, 316)),
    _c("photo_expand_score", "weights.photo_expand", "photo_expand", "PhotoExpandWeight",
       "0.05", (317, 322)),
    _c("video_open_score", "weights.video_open", "video_open", "VideoOpenWeight", "0.07",
       (323, 328)),
    _c("click_score", "weights.click", "click", "ClickWeight", "0.3", (329, 329)),
    _c("open_link_score", "weights.open_link", "open_link", "OpenLinkWeight", "0.2", (330, 330)),
    _c("profile_click_score", "weights.profile_click", "profile_click", "ProfileClickWeight",
       "0.0", (331, 336)),
    _c("vqv_score", "vqv_weight", "vqv", "VqvWeight", "0.0", (337, 337),
       "applied only when the candidate is vqv_eligible"),
    _c("share_score", "weights.share", "share", "ShareWeight", "2.0", (338, 338)),
    _c("share_via_dm_score", "weights.share_via_dm", "share_via_dm", "ShareViaDmWeight", "5.0",
       (339, 344)),
    _c("share_via_copy_link_score", "weights.share_via_copy_link", "share_via_copy_link",
       "ShareViaCopyLinkWeight", "20.0", (345, 350)),
    _c("dwell_score", "weights.dwell_weight_for(candidate)", "dwell", "DwellWeight", "0.05",
       (351, 351), _DWELL_BOOST),
    _c("quote_score", "weights.quote", "quote", "QuoteWeight", "5.0", (352, 352)),
    _c("quoted_click_score", "weights.quoted_click", "quoted_click", "QuotedClickWeight", "0.05",
       (353, 358)),
    _c("quoted_vqv_score", "quoted_vqv_weight", "quoted_vqv", "QuotedVqvWeight", "0.0",
       (359, 364), "applied only when the candidate is quoted_vqv_eligible"),
    _c("dwell_time", "weights.cont_dwell_time", "cont_dwell_time", "ContDwellTimeWeight",
       "0.004", (377, 382)),
    _c("click_dwell_time", "weights.cont_click_dwell_time", "cont_click_dwell_time",
       "ContClickDwellTimeWeight", "0.4", (383, 388)),
    _c("follow_author_score", "weights.follow_author", "follow_author", "FollowAuthorWeight",
       "4.0", (365, 370)),
    _c("not_interested_score", "weights.not_interested", "not_interested", "NotInterestedWeight",
       "-47.52", (390, 395)),
    _c("block_author_score", "weights.block_author", "block_author", "BlockAuthorWeight",
       "-31.2", (396, 401)),
    _c("mute_author_score", "weights.mute_author", "mute_author", "MuteAuthorWeight", "-58.8",
       (402, 407)),
    _c("report_score", "weights.report", "report", "ReportWeight", "-234.0", (408, 408)),
    _c("not_dwelled_score", "weights.not_dwelled", "not_dwelled", "NotDwelledWeight", "-0.02",
       (409, 414)),
    _c("post_unexplored_score", "post_unexplored_weight", "post_unexplored",
       "PostUnexploredWeight", "0.02", (371, 376), "applied only to in-network candidates"),
)

#: Conditional additive boosts; recorded for their caveats, never applied to exported counts.
BOOSTS: tuple[Coefficient, ...] = (
    _c("reply_score", "weights.reply_weight_for(candidate)",
       "bidirectional_follow_reply_weight_boost", "BidirectionalFollowReplyWeightBoost", "15.0",
       (304, 309), _REPLY_BOOST),
    _c("dwell_score", "weights.dwell_weight_for(candidate)",
       "bidirectional_follow_dwell_weight_boost", "BidirectionalFollowDwellWeightBoost", "0.0",
       (310, 315), _DWELL_BOOST),
)

BY_HEAD: dict[str, Coefficient] = {c.head: c for c in COEFFICIENTS}


def table_info() -> dict[str, Any]:
    """Identity of the table, for every metric output that uses it."""
    return {
        "id": TABLE_ID,
        "version": TABLE_VERSION,
        "repository": UPSTREAM_REPOSITORY,
        "commit": UPSTREAM_COMMIT,
        "label": VALUE_LABEL,
        "source": PARAM_SOURCE,
        "sync_annotation": SYNC_ANNOTATION,
        "note": ("public defaults at the named commit; not production values, which come from "
                 "a configuration system and experiments; in production they weight predicted "
                 "probabilities, not exported counts"),
    }
