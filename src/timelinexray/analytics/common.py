"""Shared pieces of the metric computations: notice, snapshot selection, data quality."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Context, Decimal
from fractions import Fraction
from typing import Any

from .. import __version__
from ..errors import InvalidInput
from . import schema as sch
from .dataset import Dataset, Snapshot, post_sort_key
from .parsing import PARSED, format_utc, hours

#: P6 §10, used verbatim in every metric output.
NOTICE = (
    "This is not a reach prediction. These measurements summarize your exported analytics "
    "using a documented historical baseline and, where specified, public reference "
    "coefficients. They do not reconstruct X\u2019s personalized ranking score, observe missing "
    "negative feedback, or estimate impressions lost to visibility restrictions."
)

DECIMALS = 6


_CONTEXT = Context(prec=60, rounding=ROUND_HALF_EVEN)


def num(value: Fraction | float | int | None) -> float | None:
    """Round for output (half-even, six decimals); exact inputs stay exact until here."""
    if value is None:
        return None
    if isinstance(value, Fraction):
        exact = _CONTEXT.divide(Decimal(value.numerator), Decimal(value.denominator))
    else:
        exact = Decimal(str(value))
    return float(exact.quantize(Decimal(1).scaleb(-DECIMALS), context=_CONTEXT))


def text_num(value: float | None) -> str:
    if value is None:
        return "-"
    text = f"{value:.{DECIMALS}f}".rstrip("0").rstrip(".")
    return text or "0"


def select_scope(dataset: Dataset, scope: str | None) -> str:
    scopes = sorted({s.scope for s in dataset.snapshots})
    if scope is not None:
        if scope not in scopes:
            raise InvalidInput(f"scope {scope!r} is not in this dataset (it has: "
                               f"{', '.join(scopes) or 'no snapshots'})")
        return scope
    if len(scopes) != 1:
        raise InvalidInput(f"the dataset mixes metric scopes ({', '.join(scopes)}); choose one "
                           "with --scope, they are never combined")
    return scopes[0]


def by_post(snapshots: Iterable[Snapshot], scope: str) -> dict[str, list[Snapshot]]:
    grouped: dict[str, list[Snapshot]] = defaultdict(list)
    for snapshot in snapshots:
        if snapshot.scope == scope:
            grouped[snapshot.post_id].append(snapshot)
    for items in grouped.values():
        items.sort(key=lambda s: s.captured_at)
    return dict(sorted(grouped.items(), key=lambda item: post_sort_key(item[0])))


def publication_time(snapshots: Sequence[Snapshot]) -> tuple[dt.datetime | None, str | None]:
    """The post's publication instant, or ``(None, reason)``."""
    times = {s.created_at.utc for s in snapshots if s.created_at.status == PARSED
             and s.created_at.utc is not None}
    if len(times) > 1:
        return None, "publication-time-differs-between-snapshots"
    if times:
        return times.pop(), None
    statuses = sorted({s.created_at.status for s in snapshots})
    if any(s.created_at.precision == "date" and s.created_at.status == PARSED
           for s in snapshots):
        return None, "publication-time-date-only"
    return None, "publication-time-" + "+".join(statuses)


def age(snapshot: Snapshot, published: dt.datetime | None) -> dt.timedelta | None:
    return snapshot.captured_at - published if published is not None else None


def at_horizon(
    snapshots: Sequence[Snapshot], published: dt.datetime | None, horizon: dt.timedelta,
    tolerance: dt.timedelta,
) -> tuple[Snapshot | None, str | None]:
    """The earliest cumulative snapshot whose observation age lies in [h, h + tolerance]."""
    if published is None:
        return None, "publication-time-unknown"
    cumulative = [s for s in snapshots if s.counts_kind == "cumulative"]
    if not cumulative:
        return None, "counts-not-declared-cumulative"
    for snapshot in cumulative:
        observed = snapshot.captured_at - published
        if horizon <= observed <= horizon + tolerance:
            return snapshot, None
    return None, "no-snapshot-in-horizon-window"


def default_tolerance(horizon: dt.timedelta) -> dt.timedelta:
    """Tool default: a tenth of the horizon, at least one hour."""
    return max(dt.timedelta(hours=1), horizon / 10)


def horizon_info(horizon: dt.timedelta | None, tolerance: dt.timedelta | None) -> dict[str, Any]:
    if horizon is None:
        return {"rule": "latest snapshot of each post; observation ages differ between posts"}
    assert tolerance is not None
    return {
        "hours": hours(horizon),
        "tolerance_hours": hours(tolerance),
        "rule": ("earliest snapshot with counts declared cumulative and observation age in "
                 "[h, h + tolerance]; its counts can exceed the counts at exactly h"),
    }


def data_quality(dataset: Dataset, scope: str) -> dict[str, Any]:
    """Missing fields, unmapped headers, blank cells, time issues and count revisions."""
    imports = []
    for item in dataset.imports:
        header = item["header"]
        imports.append({
            "import_id": item["import_id"],
            "alias_table": header["alias_table"],
            "alias_status": header["alias_status"],
            "missing_fields": header["missing_fields"],
            "unmapped_headers": header["unmapped_headers"],
            "blank_cells": item["blank_cells"],
            "created_at_issues": item["created_at_issues"],
            "links": item["links"],
            "scope": item["settings"]["scope"],
            "counts": item["settings"]["counts"],
        })
    return {
        "scope": scope,
        "imports": imports,
        "count_decreases": count_decreases(dataset.snapshots, scope),
        "note": "missing and blank values are unknown, never zero",
    }


def count_decreases(snapshots: Iterable[Snapshot], scope: str) -> list[dict[str, Any]]:
    """Cumulative counts that went down between two captures (reported, never clipped)."""
    found = []
    for post_id, items in by_post(snapshots, scope).items():
        cumulative = [s for s in items if s.counts_kind == "cumulative"]
        for earlier, later in zip(cumulative, cumulative[1:]):
            for name in sch.COUNT_FIELDS:
                a, b = earlier.cells[name], later.cells[name]
                if a.observed and b.observed and b.value < a.value:  # type: ignore[operator]
                    found.append({
                        "post_id": post_id,
                        "field": name,
                        "earlier": {"captured_at": format_utc(earlier.captured_at),
                                    "value": a.value},
                        "later": {"captured_at": format_utc(later.captured_at),
                                  "value": b.value},
                    })
    return found


def provenance(dataset: Dataset, definition: str) -> dict[str, Any]:
    return {
        "metric_definition": definition,
        "schema": dataset.meta["schema"]["id"],
        "imports": [
            {
                "import_id": item["import_id"],
                "source_file": item["source"]["file_name"],
                "source_sha256": item["source"]["sha256"],
                "alias_table": item["header"]["alias_table"],
                "settings": item["settings"],
            }
            for item in dataset.imports
        ],
        "tool_version": __version__,
        "calculated_at": format_utc(dt.datetime.now(dt.timezone.utc)),
    }


def reasons(rows: Iterable[Mapping[str, Any]], key: str = "reason") -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        if row.get(key):
            counts[row[key]] += 1
    return dict(sorted(counts.items()))
