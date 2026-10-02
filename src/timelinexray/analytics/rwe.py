"""M1: realized weighted engagement (RWE), exactly as defined in P6 §4.1.

For post i, observation horizon h, accepted action set O and coefficient version v::

    RWE(v, O)_i(h) = 1000 * sum over a in O of  w_a(v) * C_ia(h) / I_i(h)

The unit is *reference-weighted observed events per 1,000 recorded impressions*. It is a
fixed weighted summary of exported action rates in the audience that actually received the
post, not ranking points, not the value-model score and not a prediction.

Variants (the action set O):

``core`` (default)
    likes x favorite, replies x reply (base coefficient), reposts x retweet.
``extended``
    core plus URL clicks x open_link and new follows x follow_author. P6 allows it only
    after the attribution of those columns to the post has been validated; the command
    therefore requires ``--attribution-validated`` and records that the claim is the user's.
``sensitivity``
    core plus detail expands x click, an approximate mapping kept apart from the default.

Generic shares, engagements, bookmarks, account-level follows and inferred dwell are never
weighted. The five negative terms have no export column: they are reported as unknown,
never as zero, and the partial sum is not a bound on the full score.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from ..errors import InvalidInput
from . import coefficients as co
from . import schema as sch
from .common import (
    NOTICE,
    at_horizon,
    by_post,
    data_quality,
    horizon_info,
    num,
    provenance,
    publication_time,
    reasons,
    select_scope,
)
from .dataset import Dataset, Snapshot
from .parsing import format_utc, hours

METRIC = "M1 realized weighted engagement (RWE)"
DEFINITION = "m1-rwe/v1"
UNIT = "reference-weighted observed events per 1,000 recorded impressions"
PER = 1000


@dataclass(frozen=True)
class Term:
    """An exported count weighted by the public default of one value-model head."""

    field: str
    head: str
    mapping: str
    caveat: str

    @property
    def coefficient(self) -> co.Coefficient:
        return co.BY_HEAD[self.head]


@dataclass(frozen=True)
class Variant:
    name: str
    terms: tuple[Term, ...]
    description: str
    requires_attribution: bool


CORE_TERMS = (
    Term("likes", "favorite_score", "exact",
         "exported likes are counts, not viewer-specific predicted like probabilities"),
    Term("replies", "reply_score", "exact",
         "base coefficient only: the mutual-follow reply boost depends on the viewer and is "
         "not applied"),
    Term("reposts", "retweet_score", "exact",
         "whether the export keeps quotes separate from reposts is not established"),
)
URL_CLICKS = Term("url_clicks", "open_link_score", "exact",
                  "valid only if URL clicks are attributed to this post within a compatible "
                  "observation window")
NEW_FOLLOWS = Term("new_follows", "follow_author_score", "approximate",
                   "valid only if new follows are attributed to this post, not daily account "
                   "follows")
DETAIL_EXPANDS = Term("detail_expands", "click_score", "approximate",
                      "X defines detail expands as opening post detail; equivalence with the "
                      "model's click label is not established")

VARIANTS: Mapping[str, Variant] = {
    "core": Variant("core", CORE_TERMS,
                    "P6 conservative default: likes, replies, reposts", False),
    "extended": Variant("extended", CORE_TERMS + (URL_CLICKS, NEW_FOLLOWS),
                        "P6 extended variant: core plus URL clicks and new follows, only after "
                        "their attribution to the post has been validated", True),
    "sensitivity": Variant("sensitivity", CORE_TERMS + (DETAIL_EXPANDS,),
                           "sensitivity analysis: core plus detail expands as an approximate "
                           "stand-in for click_score; never a default", False),
}

#: P6 §3: every summed head, its closest export column and why it is or is not used.
EXPORT_MAPPING: Mapping[str, tuple[str | None, str, str]] = {
    "favorite_score": ("likes", "exact", "core term"),
    "reply_score": ("replies", "exact", "core term, base coefficient"),
    "retweet_score": ("reposts", "exact", "core term"),
    "photo_expand_score": (None, "none", "media views are not photo-expansion clicks"),
    "video_open_score": (None, "none", "video views are not post-attributed video opens"),
    "click_score": ("detail_expands", "approximate", "sensitivity variant only"),
    "open_link_score": ("url_clicks", "exact", "extended variant only (attribution)"),
    "profile_click_score": ("profile_visits", "approximate",
                           "a profile visit need not be the model's post-attributed profile "
                           "click; not weighted"),
    "vqv_score": (None, "none", "a video-view total does not establish quality-view "
                                "eligibility"),
    "share_score": ("shares", "approximate",
                    "generic Shares is insufficiently defined for this head; excluded from "
                    "every variant"),
    "share_via_dm_score": (None, "none", "generic Shares does not identify DM shares"),
    "share_via_copy_link_score": (None, "none",
                                  "copying a link is not a URL click; generic Shares cannot "
                                  "substitute"),
    "dwell_score": (None, "none", "no dwell event is exported"),
    "quote_score": (None, "none", "this schema has no verified quote count"),
    "quoted_click_score": (None, "none", "detail expands do not isolate quoted-content clicks"),
    "quoted_vqv_score": (None, "none", "requires quoted-content quality-view information"),
    "dwell_time": (None, "none", "continuous quantity, not exported"),
    "click_dwell_time": (None, "none", "no click-conditioned dwell duration is exported"),
    "follow_author_score": ("new_follows", "approximate", "extended variant only (attribution)"),
    "not_interested_score": (None, "none", "no negative-feedback column"),
    "block_author_score": (None, "none", "unfollows are not blocks"),
    "mute_author_score": (None, "none", "unfollows are not mutes"),
    "report_score": (None, "none", "no report count is exported"),
    "not_dwelled_score": (None, "none", "impressions minus engagements does not recover it"),
    "post_unexplored_score": (None, "none",
                              "applied only to in-network candidates; low impressions do not "
                              "recover it"),
}

NEGATIVE_FEEDBACK = "unknown"


def variant(name: str, *, attribution_validated: bool = False) -> Variant:
    try:
        chosen = VARIANTS[name]
    except KeyError:
        raise InvalidInput(f"unknown RWE variant {name!r}; choose one of "
                           f"{', '.join(VARIANTS)}") from None
    if chosen.requires_attribution and not attribution_validated:
        raise InvalidInput(
            f"the {name} variant weights URL clicks and new follows, which count only if they "
            "are attributed to the post; after validating that for your export, repeat with "
            "--attribution-validated (the tool cannot check it)"
        )
    return chosen


def omitted_terms(chosen: Variant) -> list[dict[str, Any]]:
    used = {term.head for term in chosen.terms}
    rows = []
    for coefficient in co.COEFFICIENTS:
        if coefficient.head in used:
            continue
        column, mapping, reason = EXPORT_MAPPING[coefficient.head]
        rows.append({
            "head": coefficient.head,
            "public_default": float(coefficient.value),
            "closest_field": column,
            "mapping": mapping,
            "contribution": NEGATIVE_FEEDBACK if coefficient.negative else "not included",
            "reason": (reason + "; missing negative feedback is unknown, never zero"
                       if coefficient.negative else reason),
        })
    return rows


def post_rwe(snapshot: Snapshot, chosen: Variant) -> dict[str, Any]:
    """RWE of one snapshot, with every component's count, rate and contribution."""
    impressions = snapshot.cells["impressions"]
    total: Fraction | None = Fraction(0)
    reason: str | None = None
    if not impressions.observed:
        total, reason = None, f"impressions-{impressions.status}"
    elif impressions.value == 0:
        total, reason = None, "zero-impressions"
    denominator = impressions.value if total is not None else None
    components = []
    for term in chosen.terms:
        weight = term.coefficient.value
        cell = snapshot.cells[term.field]
        row: dict[str, Any] = {
            "field": term.field,
            "head": term.head,
            "coefficient": float(weight),
            "mapping": term.mapping,
            "count": cell.to_dict(),
            "rate_per_1000": None,
            "contribution": None,
            "one_event_changes_rwe_by": None,
        }
        if not cell.observed:
            row["status"] = "unknown"
            if total is not None:
                total, reason = None, f"component-{cell.status}:{term.field}"
        elif denominator:
            rate = Fraction(PER * cell.value, denominator)  # type: ignore[operator]
            contribution = weight * rate
            row.update(status="observed", rate_per_1000=num(rate),
                       contribution=num(contribution),
                       one_event_changes_rwe_by=num(Fraction(PER) * weight / denominator))
            if total is not None:
                total += contribution
        else:
            row["status"] = "observed"
        components.append(row)
    return {
        "rwe": num(total) if total is not None else None,
        "rwe_exact": total,
        "status": "defined" if total is not None else "undefined",
        "reason": reason,
        "impressions": impressions.to_dict(),
        "components": components,
        "negative_feedback": NEGATIVE_FEEDBACK,
        "outcomes": outcome_rates(snapshot),
    }


def outcome_rates(snapshot: Snapshot) -> dict[str, Any]:
    """Unweighted count and rate per 1,000 impressions for every exported action."""
    impressions = snapshot.cells["impressions"]
    usable = impressions.observed and impressions.value
    out = {}
    for name in sch.COUNT_FIELDS:
        if name == "impressions":
            continue
        cell = snapshot.cells[name]
        rate = (num(Fraction(PER * cell.value, impressions.value))  # type: ignore[operator]
                if cell.observed and usable else None)
        out[name] = {"count": cell.to_dict(), "rate_per_1000": rate}
    return out


def compute(
    dataset: Dataset,
    chosen: Variant,
    *,
    horizon: dt.timedelta | None = None,
    tolerance: dt.timedelta | None = None,
    scope: str | None = None,
    posts: Sequence[str] = (),
) -> dict[str, Any]:
    """M1 for every post of a dataset (or the listed posts), with aggregate and caveats."""
    chosen_scope = select_scope(dataset, scope)
    grouped = by_post(dataset.snapshots, chosen_scope)
    if posts:
        unknown = sorted(set(posts) - set(grouped))
        if unknown:
            raise InvalidInput(f"post id(s) not in the dataset for scope {chosen_scope}: "
                               f"{', '.join(unknown)}")
        grouped = {p: grouped[p] for p in grouped if p in set(posts)}
    rows = []
    for post_id, snapshots in grouped.items():
        published, time_reason = publication_time(snapshots)
        if horizon is None:
            snapshot, reason = snapshots[-1], None
        else:
            assert tolerance is not None
            snapshot, reason = at_horizon(snapshots, published, horizon, tolerance)
            reason = reason if reason != "publication-time-unknown" else time_reason
        row: dict[str, Any] = {
            "post_id": post_id,
            "published_at": format_utc(published) if published else None,
            "publication_time_issue": time_reason,
        }
        if snapshot is None:
            row.update(status="undefined", reason=reason, rwe=None, snapshot=None)
            rows.append(row)
            continue
        observed_age = snapshot.captured_at - published if published else None
        row["snapshot"] = {
            "captured_at": format_utc(snapshot.captured_at),
            "import_id": snapshot.import_id,
            "counts_kind": snapshot.counts_kind,
            "observation_age_hours": hours(observed_age) if observed_age is not None else None,
        }
        row.update(post_rwe(snapshot, chosen))
        rows.append(row)
    aggregate = _aggregate(rows)
    for row in rows:
        row.pop("rwe_exact", None)
    return {
        "notice": NOTICE,
        "metric": {
            "name": METRIC,
            "definition": DEFINITION,
            "variant": chosen.name,
            "description": chosen.description,
            "unit": UNIT,
            "formula": "RWE = 1000 * sum over the action set of w_a * C_a / I",
            "action_set": [
                {"field": t.field, "head": t.head, "mapping": t.mapping, "caveat": t.caveat,
                 "coefficient": t.coefficient.to_dict()}
                for t in chosen.terms
            ],
            "coefficient_table": co.table_info(),
            "attribution": ("asserted by the user with --attribution-validated; not verified "
                            "by the tool") if chosen.requires_attribution else None,
        },
        "scope": chosen_scope,
        "horizon": horizon_info(horizon, tolerance),
        "posts": rows,
        "aggregate": aggregate,
        "negative_feedback": NEGATIVE_FEEDBACK,
        "omitted_terms": omitted_terms(chosen),
        "data_quality": data_quality(dataset, chosen_scope),
        "caveats": CAVEATS,
        "provenance": provenance(dataset, DEFINITION),
    }


def _aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    defined = [row for row in rows if row.get("status") == "defined"]
    values = [row["rwe_exact"] for row in defined]
    weighted = sum((row["rwe_exact"] * row["impressions"]["value"] for row in defined),
                   Fraction(0))
    total_impressions = sum(row["impressions"]["value"] for row in defined)
    return {
        "posts": len(rows),
        "defined": len(defined),
        "undefined": reasons(rows),
        "pooled_rwe": num(weighted / total_impressions) if total_impressions else None,
        "mean_post_rwe": num(sum(values, Fraction(0)) / len(values)) if values else None,
        "median_post_rwe": num(statistics.median(values)) if values else None,
        "note": ("pooled = 1000 * sum of weighted counts / sum of impressions over posts with a "
                 "defined RWE; mean and median are taken over per-post values; they answer "
                 "different questions"),
    }


CAVEATS = (
    "RWE describes observed actions in the audience that actually received each post; it is "
    "not the value-model score, whose weights multiply predicted probabilities per viewer.",
    "The coefficients are public defaults at one commit, not production values.",
    "Negative feedback (not interested, block, mute, report, not dwelled) is not exported and "
    "is unknown, so the partial sum is not a bound on any full score.",
    "A large coefficient does not make a large contribution; contribution also depends on "
    "event frequency and measurement validity.",
    "Broader distribution can lower engagement rates while raising total impressions; "
    "comparisons across posts, periods or accounts need the same weights, head mask, field "
    "meanings, observation horizon and scope.",
    "One additional event changes RWE by 1000 * w / I (see one_event_changes_rwe_by); with few "
    "impressions single events move the index a lot, and impressions are not unique viewers.",
    "Do not optimize for RWE: requests for interaction or controversy can raise observed "
    "counts without showing value.",
)
