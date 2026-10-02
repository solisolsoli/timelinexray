"""Implementation of ``txray metrics import | rwe | reach`` behind the offline guard.

:func:`run` refuses to work unless :func:`timelinexray.analytics.offline.enter` has already
made the process offline; ``timelinexray.metrics_cli`` does that before importing this
module. Each command returns ``(command, data, text, warnings)``; the caller prints either
the text or a JSON envelope.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import InvalidInput
from . import offline, reach, rwe
from .common import NOTICE, default_tolerance, text_num
from . import schema as sch
from .dataset import ImportSettings, import_file, load_dataset, read_header_row
from .parsing import load_zone, parse_aware, parse_duration

Result = tuple[str, dict[str, Any], str, list[str]]


def run(args: argparse.Namespace) -> Result:
    if not offline.is_active():
        raise RuntimeError("analytics commands run only after offline.enter()")
    handlers = {"import": _import, "rwe": _rwe, "reach": _reach}
    return handlers[args.metrics_command](args)


# -- import ---------------------------------------------------------------------------------


def _import(args: argparse.Namespace) -> Result:
    if getattr(args, "dump_header", False):
        return _dump_header(args)
    missing = [flag for flag, value in (("--out", args.out), ("--captured-at", args.captured_at))
               if not value]
    if missing:
        raise InvalidInput(f"the following arguments are required: {', '.join(missing)} "
                           "(only --dump-header reads an export without them)")
    load_zone(args.source_timezone)
    settings = ImportSettings(
        schema=args.schema,
        language=args.lang,
        captured_at=parse_aware(args.captured_at, "--captured-at"),
        number_locale=args.number_locale or args.lang,
        number_locale_source="declared" if args.number_locale else "header-language",
        source_timezone=args.source_timezone,
        date_format=args.date_format,
        scope=args.scope,
        counts=args.counts,
    )
    summary = import_file(args.csv, args.out, settings)
    record = summary["import"]
    warnings = _import_warnings(record)
    data = {**summary, "dataset_path": offline.resolve(args.out), "offline": offline.status()}
    return "metrics import", data, _import_text(data, warnings), warnings


def _dump_header(args: argparse.Namespace) -> Result:
    """The header row as exported, normalised, and its mapping; no data row is read and
    nothing is written, so a user can confirm an alias table without sharing data."""
    file_name, header = read_header_row(args.csv)
    table = sch.aliases_for(args.schema, args.lang)
    resolution = sch.resolve_headers(header, args.schema, args.lang)
    columns = [{"index": number, "header": column.header,
                "normalized": sch.normalize_header(column.header, args.lang),
                "field": column.field}
               for number, column in enumerate(resolution.columns, 1)]
    warnings: list[str] = []
    if table.status != "SUPPORTED":
        warnings.append(f"header alias table {table.key()} has status {table.status}: its "
                        "names were not checked against a real export; this dump is how to "
                        "check them")
    if resolution.duplicates:
        warnings.append("several columns map to one field: " + "; ".join(
            f"{f}: {', '.join(repr(h) for h in hs)}" for f, hs in resolution.duplicates))
    if resolution.missing_fields:
        warnings.append("missing columns (unknown, never zero): "
                        + ", ".join(resolution.missing_fields))
    if resolution.unmapped_headers:
        warnings.append("unmapped columns: "
                        + ", ".join(repr(h) for h in resolution.unmapped_headers))
    languages = sch.matching_languages(header, args.schema)
    best = max(languages, key=lambda lang: (languages[lang], lang == args.lang), default=None)
    if best is not None and best != args.lang and languages[best] > languages.get(args.lang, 0):
        warnings.append(f"the header matches the {best!r} alias table better: try --lang {best}")
    data = {
        "source": {"file_name": file_name},
        "schema": args.schema,
        "language": args.lang,
        "header": {
            "alias_table": table.key(),
            "alias_status": table.status,
            "confirmed_by": table.confirmed_by,
            "original": list(header),
            "normalized": [column["normalized"] for column in columns],
            "columns": columns,
            "missing_fields": list(resolution.missing_fields),
            "unmapped_headers": list(resolution.unmapped_headers),
            "matching_languages": dict(sorted(languages.items())),
        },
        "data_rows_read": 0,
        "written": [],
        "offline": offline.status(),
    }
    mapped = sum(1 for column in columns if column["field"])
    lines = [
        f"file       {file_name}",
        f"schema     {args.schema} (post grain)  language {args.lang}  header table "
        f"{table.key()}  status {table.status}"
        + (f" (confirmed by {table.confirmed_by})" if table.confirmed_by else ""),
        f"columns    {len(columns)} in the header row: {mapped} mapped, "
        f"{len(resolution.unmapped_headers)} unmapped; {len(resolution.missing_fields)} "
        "missing field(s)",
    ]
    for column in columns:
        target = column["field"] or "(unmapped)"
        lines.append(f"column {column['index']:>3}  {column['header']!r}  normalised "
                     f"{column['normalized']!r}  ->  {target}")
    lines.append("missing    " + (", ".join(resolution.missing_fields) or "none"))
    lines.append("languages  " + ", ".join(f"{lang} {n}" for lang, n in sorted(languages.items()))
                 + "  (columns matching each alias table)")
    lines += [f"warning    {text}" for text in warnings]
    lines.append("note       no data row was read and nothing was written. To confirm the "
                 "table, record a finding (OFFICIAL from X's documentation, EMPIRICAL from "
                 "this export's header) and cite its id as the table's confirmed_by; the "
                 "status changes only through such a record")
    return "metrics import", data, "\n".join(lines) + "\n", warnings


def _import_warnings(record: Mapping[str, Any]) -> list[str]:
    header = record["header"]
    out = []
    if header["alias_status"] != "SUPPORTED":
        out.append(f"header alias table {header['alias_table']} has status "
                   f"{header['alias_status']}: its names were not checked against a real export")
    if header["missing_fields"]:
        out.append("missing columns (unknown, never zero): " + ", ".join(header["missing_fields"]))
    if header["unmapped_headers"]:
        out.append("unmapped columns kept only in the header record: "
                   + ", ".join(repr(h) for h in header["unmapped_headers"]))
    for name, count in record["blank_cells"].items():
        out.append(f"{count} blank {name} cell(s) (unknown, never zero)")
    for status, count in record["created_at_issues"].items():
        out.append(f"{count} publication time(s) {status}")
    if record["links"].get("inconsistent"):
        out.append(f"{record['links']['inconsistent']} post link(s) do not point at the row's "
                   "post id")
    if record["settings"]["counts"] != "cumulative":
        out.append("counts are not declared cumulative (--counts cumulative): horizon-based "
                   "metrics (reach, rwe --horizon) will not use these snapshots")
    return out


def _import_text(data: Mapping[str, Any], warnings: Sequence[str]) -> str:
    record = data["import"]
    settings = record["settings"]
    header = record["header"]
    mapped = sum(1 for column in header["columns"] if column["field"])
    lines = [
        f"imported   {record['source']['file_name']}  sha256:{record['source']['sha256']}",
        f"import id  {record['import_id']}"
        + ("  (already imported: nothing changed)" if data["already_imported"] else ""),
        f"schema     {settings['schema']} (post grain), header table {header['alias_table']}: "
        f"{mapped} of {len(header['columns'])} columns mapped",
        f"captured   {settings['captured_at']}  counts {settings['counts']}  "
        f"scope {settings['scope']}",
        f"numbers    {settings['number_locale']} ({settings['number_locale_source']})  "
        f"times {settings['source_timezone']}"
        + (f" format {settings['date_format']!r}" if settings["date_format"] else " ISO 8601"),
        f"rows       {record['rows']}  snapshots added {data['snapshots_added']}  "
        f"already present {data['snapshots_already_present']}  "
        f"identical duplicate rows {record['identical_duplicate_rows']}",
        f"dataset    {data['dataset_path']}  ({data['dataset']['posts']} posts, "
        f"{data['dataset']['snapshots']} snapshots, {data['dataset']['imports']} imports)",
    ]
    lines += [f"warning    {text}" for text in warnings]
    lines.append("note       private data: keep the dataset out of version control and out of "
                 "anything TimelineXray or an MCP client serves")
    return "\n".join(lines) + "\n"


# -- rwe ------------------------------------------------------------------------------------


def _horizon(args: argparse.Namespace) -> tuple[Any, Any]:
    if not args.horizon:
        if args.horizon_tolerance:
            raise InvalidInput("--horizon-tolerance needs --horizon")
        return None, None
    horizon = parse_duration(args.horizon, "--horizon")
    tolerance = (parse_duration(args.horizon_tolerance, "--horizon-tolerance", allow_zero=True)
                 if args.horizon_tolerance else default_tolerance(horizon))
    return horizon, tolerance


def _rwe(args: argparse.Namespace) -> Result:
    dataset = load_dataset(args.dataset)
    chosen = rwe.variant(args.variant, attribution_validated=args.attribution_validated)
    horizon, tolerance = _horizon(args)
    data = rwe.compute(dataset, chosen, horizon=horizon, tolerance=tolerance, scope=args.scope,
                       posts=args.post or ())
    data["offline"] = offline.status()
    warnings = [f"{n} post(s) without a defined RWE: {reason}"
                for reason, n in data["aggregate"]["undefined"].items()]
    return "metrics rwe", data, _rwe_text(data), warnings


def _rwe_text(data: Mapping[str, Any]) -> str:
    metric = data["metric"]
    table = metric["coefficient_table"]
    lines = [NOTICE, "",
             f"metric     {metric['name']}, variant {metric['variant']} ({metric['definition']})",
             f"unit       {metric['unit']}",
             f"weights    {table['label']} at {table['commit']} ({table['source']})"]
    for term in metric["action_set"]:
        lines.append(f"           {term['field']} x {term['coefficient']['literal']} "
                     f"({term['head']}, {term['mapping']} mapping: {term['caveat']})")
    if metric["attribution"]:
        lines.append(f"attrib.    {metric['attribution']}")
    lines.append(f"scope      {data['scope']}")
    lines.append(f"snapshots  {_horizon_text(data['horizon'])}")
    lines.append("")
    for row in data["posts"]:
        lines.extend(_rwe_post_lines(row))
    agg = data["aggregate"]
    lines += [
        "",
        f"aggregate  posts {agg['posts']}, defined {agg['defined']}; pooled "
        f"{text_num(agg['pooled_rwe'])}, mean {text_num(agg['mean_post_rwe'])}, median "
        f"{text_num(agg['median_post_rwe'])} (pooled and mean answer different questions)",
    ]
    for reason, n in agg["undefined"].items():
        lines.append(f"undefined  {n} x {reason}")
    lines.append(f"negative feedback terms: {data['negative_feedback']} (not exported; never zero)")
    lines.append(f"omitted terms ({table['label']}s at {table['commit'][:7]}):")
    for term in data["omitted_terms"]:
        lines.append(f"  {term['head']:<26} {text_num(term['public_default']):>7}  "
                     f"{term['contribution']:<12}  {term['reason']}")
    lines += _quality_lines(data["data_quality"])
    lines.append("caveats:")
    lines += [f"  - {text}" for text in data["caveats"]]
    return "\n".join(lines) + "\n"


def _rwe_post_lines(row: Mapping[str, Any]) -> list[str]:
    snapshot = row.get("snapshot")
    if snapshot is None:
        return [f"post {row['post_id']}  RWE undefined ({row['reason']})"]
    age = snapshot["observation_age_hours"]
    impressions = row["impressions"]
    shown = impressions["value"] if impressions["status"] == "observed" else impressions["status"]
    value = (text_num(row["rwe"]) if row["status"] == "defined"
             else f"undefined ({row['reason']})")
    head = (f"post {row['post_id']}  captured {snapshot['captured_at']}  age "
            f"{text_num(age) + ' h' if age is not None else 'unknown'}  impressions {shown}  "
            f"RWE {value}")
    parts = []
    for comp in row["components"]:
        count = comp["count"]
        counted = count["value"] if count["status"] == "observed" else count["status"]
        contribution = (text_num(comp["contribution"]) if comp["contribution"] is not None
                        else "unknown" if comp["status"] == "unknown" else "-")
        parts.append(f"{comp['field']} {counted} x {text_num(comp['coefficient'])} "
                     f"-> {contribution}")
    return [head, "  " + " | ".join(parts)]


def _horizon_text(info: Mapping[str, Any]) -> str:
    if "hours" not in info:
        return info["rule"]
    return (f"horizon {text_num(info['hours'])} h, tolerance {text_num(info['tolerance_hours'])}"
            f" h: {info['rule']}")


def _quality_lines(quality: Mapping[str, Any]) -> list[str]:
    lines = ["data quality:"]
    for item in quality["imports"]:
        missing = ", ".join(item["missing_fields"]) or "none"
        unmapped = ", ".join(repr(h) for h in item["unmapped_headers"]) or "none"
        blank = ", ".join(f"{k} {v}" for k, v in item["blank_cells"].items()) or "none"
        lines.append(f"  import {item['import_id']} ({item['alias_table']}, "
                     f"{item['alias_status']}): missing {missing}; unmapped {unmapped}; "
                     f"blank cells {blank}; counts {item['counts']}; scope {item['scope']}")
    decreases = quality["count_decreases"]
    lines.append(f"  cumulative counts that decreased between captures: {len(decreases)}"
                 + (" (reported, not clipped)" if decreases else ""))
    return lines


# -- reach ----------------------------------------------------------------------------------


def _reach(args: argparse.Namespace) -> Result:
    dataset = load_dataset(args.dataset)
    horizon, tolerance = _horizon(args)
    settings = reach.Settings(
        horizon=horizon,
        tolerance=tolerance,
        timezone=args.timezone,
        band_hours=args.band_hours,
        kappa=args.kappa,
        min_parent=args.min_parent,
    )
    data = reach.compute(dataset, settings, scope=args.scope)
    data["offline"] = offline.status()
    warnings = [f"{n} post(s) without RR: {reason}"
                for reason, n in data["summary"]["rr_undefined"].items()]
    return "metrics reach", data, _reach_text(data), warnings


def _reach_text(data: Mapping[str, Any]) -> str:
    metric = data["metric"]
    lines = [NOTICE, "",
             f"metric     {metric['name']} ({metric['definition']}); action coefficients: "
             "not applicable",
             f"snapshots  {_horizon_text(data['horizon'])}",
             f"baseline   {metric['baseline_rule']}; bucket {metric['bucket']}; parent "
             f"{metric['parent']}; kappa {text_num(metric['kappa'])}; min parent "
             f"{metric['min_parent']}",
             f"scope      {data['scope']}", ""]
    header = ("post_id", "published", "bucket", "I(h)", "B", "n_b", "n_p", "lambda", "RR",
              "RR+")
    table = [header]
    for row in data["posts"]:
        baseline = row.get("baseline") or {}
        bucket = row.get("bucket")
        table.append((
            row["post_id"],
            row["published_at"] or "unknown",
            f"{bucket['weekday']} {bucket['band']}" if bucket else "-",
            str(row["impressions_at_horizon"]) if row.get("impressions_at_horizon") is not None
            else "-",
            text_num(baseline.get("value")),
            str(baseline.get("n_bucket", "-")),
            str(baseline.get("n_parent", "-")),
            text_num(baseline.get("lambda")),
            text_num(row["rr"]) if row["rr"] is not None else f"undefined ({row['rr_reason']})",
            (text_num(row["rr_plus"]) if row["rr_plus"] is not None
             else f"undefined ({row['rr_plus_reason']})"),
        ))
    widths = [max(len(r[i]) for r in table) for i in range(len(header) - 1)]
    for r in table:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(r[:-1]))
                     + "  " + r[-1])
    summary = data["summary"]
    lines += ["",
              f"summary    posts {summary['posts']}, RR defined {summary['rr_defined']}, "
              f"RR+ defined {summary['rr_plus_defined']}"]
    for reason, n in summary["rr_undefined"].items():
        lines.append(f"undefined  RR: {n} x {reason}")
    lines += _quality_lines(data["data_quality"])
    lines.append("caveats:")
    lines += [f"  - {text}" for text in data["caveats"]]
    return "\n".join(lines) + "\n"

