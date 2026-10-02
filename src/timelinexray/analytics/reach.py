"""M2: relative reach against the account's own pre-publication baseline (P6 §5.1).

For post i published at t_i, with impressions I_i(h) at a common observation horizon h:

1. Baseline observations are the other posts j whose horizon-h snapshot was *captured
   before t_i*: only information available before publication enters the baseline.
2. ``z_j = log(1 + I_j(h))``. The weekday/time bucket b of t_i (in the analysis time zone)
   has median ``m_b`` over n_b observations; the parent (all baseline observations) has
   median ``m_p`` over n_p observations.
3. Shrinkage heuristic: ``lambda_b = n_b / (n_b + kappa)`` and
   ``m~_b = lambda_b * m_b + (1 - lambda_b) * m_p``. The effective sample size is the plain
   count n_b; dependence between posts is not modelled.
4. ``B = exp(m~_b) - 1``; ``RR = I_i(h) / B``, undefined when B is zero.
5. ``LR = log(1 + I_i(h)) - m~_b`` and ``RR+ = exp(LR) = (1 + I_i(h)) / (1 + B)``, a
   separately named, always-defined alternative. RR and RR+ are never interchanged.

This median-pooling construction is a heuristic, not a Bayesian posterior, and a same-horizon
RR contains its own outcome: correlating RR with I(h) is not evidence of predictive value.
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import InvalidInput
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
from .dataset import Dataset
from .parsing import format_utc, hours, load_zone

METRIC = "M2 relative reach"
DEFINITION = "m2-relative-reach/v1"
DEFAULT_KAPPA = 5.0
DEFAULT_BAND_HOURS = 6
DEFAULT_MIN_PARENT = 5
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class Settings:
    horizon: dt.timedelta
    tolerance: dt.timedelta
    timezone: str = "UTC"
    band_hours: int = DEFAULT_BAND_HOURS
    kappa: float = DEFAULT_KAPPA
    min_parent: int = DEFAULT_MIN_PARENT

    def check(self) -> None:
        if self.band_hours not in (1, 2, 3, 4, 6, 8, 12, 24):
            raise InvalidInput("--band-hours must divide 24 (1, 2, 3, 4, 6, 8, 12 or 24)")
        if not (self.kappa >= 0 and math.isfinite(self.kappa)):
            raise InvalidInput("--kappa must be a finite number >= 0")
        if self.min_parent < 1:
            raise InvalidInput("--min-parent must be at least 1")


@dataclass(frozen=True)
class _Observation:
    post_id: str
    published: dt.datetime
    captured: dt.datetime
    impressions: int
    bucket: tuple[int, int]

    @property
    def z(self) -> float:
        return math.log1p(self.impressions)


def _bucket(published: dt.datetime, zone: dt.tzinfo, band_hours: int) -> tuple[int, int]:
    local = published.astimezone(zone)
    return (local.weekday(), local.hour // band_hours)


def _bucket_label(bucket: tuple[int, int], band_hours: int) -> dict[str, str]:
    start = bucket[1] * band_hours
    return {"weekday": WEEKDAYS[bucket[0]],
            "band": f"{start:02d}:00-{start + band_hours:02d}:00"}


def _median(ordered: Sequence[float]) -> float:
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def compute(dataset: Dataset, settings: Settings, *, scope: str | None = None) -> dict[str, Any]:
    settings.check()
    zone = load_zone(settings.timezone)
    chosen_scope = select_scope(dataset, scope)
    grouped = by_post(dataset.snapshots, chosen_scope)

    targets: list[dict[str, Any]] = []
    observations: list[_Observation] = []
    for post_id, snapshots in grouped.items():
        published, time_reason = publication_time(snapshots)
        row: dict[str, Any] = {"post_id": post_id,
                               "published_at": format_utc(published) if published else None}
        if published is None:
            row.update(impressions_at_horizon=None, baseline=None)
            _ratios(row, time_reason)
            targets.append(row)
            continue
        bucket = _bucket(published, zone, settings.band_hours)
        row["published_local"] = published.astimezone(zone).isoformat()
        row["bucket"] = _bucket_label(bucket, settings.band_hours)
        snapshot, reason = at_horizon(snapshots, published, settings.horizon, settings.tolerance)
        impressions = None
        if snapshot is not None:
            cell = snapshot.cells["impressions"]
            row["snapshot"] = {
                "captured_at": format_utc(snapshot.captured_at),
                "observation_age_hours": hours(snapshot.captured_at - published),
                "import_id": snapshot.import_id,
            }
            if cell.observed:
                impressions = cell.value
                observations.append(_Observation(post_id, published, snapshot.captured_at,
                                                 impressions, bucket))  # type: ignore[arg-type]
            else:
                reason = f"impressions-{cell.status}"
        row["impressions_at_horizon"] = impressions
        row["_published"], row["_bucket"], row["_reason"] = published, bucket, reason
        targets.append(row)

    observations.sort(key=lambda o: o.captured)
    parent: list[float] = []
    buckets: dict[tuple[int, int], list[float]] = {}
    first_published: dt.datetime | None = None
    last_published: dt.datetime | None = None
    last_captured: dt.datetime | None = None
    added = 0
    ordered = sorted((r for r in targets if "_published" in r), key=lambda r: r["_published"])
    for row in ordered:
        published = row.pop("_published")
        bucket = row.pop("_bucket")
        own_reason = row.pop("_reason")
        while added < len(observations) and observations[added].captured < published:
            obs = observations[added]
            bisect.insort(parent, obs.z)
            bisect.insort(buckets.setdefault(obs.bucket, []), obs.z)
            first_published = min(first_published or obs.published, obs.published)
            last_published = max(last_published or obs.published, obs.published)
            last_captured = obs.captured
            added += 1
        row["baseline"], baseline_reason = _baseline(parent, buckets.get(bucket, []), settings)
        if row["baseline"] is not None:
            row["baseline"]["window"] = {
                "first_published": format_utc(first_published),  # type: ignore[arg-type]
                "last_published": format_utc(last_published),  # type: ignore[arg-type]
                "last_captured": format_utc(last_captured),  # type: ignore[arg-type]
            }
        _ratios(row, own_reason or baseline_reason)

    summary = {
        "posts": len(targets),
        "rr_defined": sum(1 for r in targets if r.get("rr") is not None),
        "rr_plus_defined": sum(1 for r in targets if r.get("rr_plus") is not None),
        "rr_undefined": reasons(targets, "rr_reason"),
        "rr_plus_undefined": reasons(targets, "rr_plus_reason"),
    }
    return {
        "notice": NOTICE,
        "metric": {
            "name": METRIC,
            "definition": DEFINITION,
            "formulas": {
                "z": "log(1 + I_j(h))",
                "shrinkage": "lambda_b = n_b / (n_b + kappa); m~_b = lambda_b m_b + "
                             "(1 - lambda_b) m_p",
                "baseline": "B = exp(m~_b) - 1",
                "rr": "RR = I_i(h) / B, undefined when B = 0",
                "rr_plus": "RR+ = exp(log(1 + I_i(h)) - m~_b) = (1 + I_i(h)) / (1 + B)",
            },
            "baseline_rule": ("other posts whose horizon snapshot was captured before this "
                              "post's publication time"),
            "bucket": f"weekday and {settings.band_hours}-hour band in {settings.timezone}",
            "parent": "all baseline observations of the account",
            "kappa": settings.kappa,
            "min_parent": settings.min_parent,
            "effective_sample_size": "plain count of baseline posts; dependence not modelled",
            "action_coefficients": "not applicable: M2 uses no action weights",
        },
        "scope": chosen_scope,
        "horizon": horizon_info(settings.horizon, settings.tolerance),
        "posts": targets,
        "summary": summary,
        "data_quality": data_quality(dataset, chosen_scope),
        "caveats": CAVEATS,
        "provenance": provenance(dataset, DEFINITION),
    }


def _baseline(parent: Sequence[float], bucket: Sequence[float],
              settings: Settings) -> tuple[dict[str, Any] | None, str | None]:
    n_p, n_b = len(parent), len(bucket)
    if n_p == 0:
        return None, "no-pre-publication-baseline"
    if n_p < settings.min_parent:
        return None, f"baseline-below-min-parent:{n_p}<{settings.min_parent}"
    m_p = _median(parent)
    m_b = _median(bucket) if n_b else None
    lam = n_b / (n_b + settings.kappa) if n_b else 0.0
    shrunk = lam * m_b + (1 - lam) * m_p if m_b is not None else m_p
    return {
        "value": math.expm1(shrunk),
        "log_median_bucket": m_b,
        "n_bucket": n_b,
        "log_median_parent": m_p,
        "n_parent": n_p,
        "lambda": lam,
        "kappa": settings.kappa,
        "log_shrunk": shrunk,
    }, None


def _ratios(row: dict[str, Any], reason: str | None) -> None:
    baseline = row.get("baseline")
    impressions = row.get("impressions_at_horizon")
    row.update(rr=None, rr_plus=None, lr=None, rr_reason=None, rr_plus_reason=None)
    if baseline is None or impressions is None:
        row.update(status="undefined", reason=reason, rr_reason=reason, rr_plus_reason=reason)
    else:
        shrunk = baseline["log_shrunk"]
        lr = math.log1p(impressions) - shrunk
        row.update(lr=num(lr), rr_plus=num(math.exp(lr)))
        if baseline["value"] > 0:
            row.update(rr=num(impressions / baseline["value"]), status="defined")
        else:
            row.update(rr_reason="baseline-zero", status="rr-undefined")
    if baseline is not None:
        for key in ("value", "log_median_bucket", "log_median_parent", "lambda", "log_shrunk"):
            baseline[key] = num(baseline[key])


CAVEATS = (
    "RR compares observed impressions with an explicitly defined historical reference of the "
    "same account; it does not measure quality, causal format effects or suppression.",
    "Same-horizon RR contains its own outcome: correlating RR with I(h) is not evidence of "
    "predictive validity (P6-018).",
    "The shrinkage is a heuristic, not a Bayesian posterior; kappa, the band width and the "
    "minimum parent size are declared tool settings, not validated values.",
    "The count at the chosen snapshot can exceed the count at exactly h, by the impressions "
    "gathered between h and the capture.",
    "Topic, format, account growth, promotion, external events and platform changes remain "
    "confounders; a moving baseline changes the reference over time.",
    "A multiple of one account's baseline is not comparable with the same multiple for another "
    "account.",
    "Uncertainty (baseline estimation and between-post dispersion) is not computed here.",
)
