"""``txray metrics``: offline creator metrics from the user's own analytics export.

``txray metrics import EXPORT --schema S --lang L --dump-header`` prints the header row as
exported, each name normalised, and the field it maps to, reading no data row and writing
nothing, so a user can confirm an alias table (the English one is ``EXTERNAL_RECHECK``)
without sharing data.

This module only declares the command line and imports no analytics code. Its handler
first makes the process offline (:func:`timelinexray.analytics.offline.enter`: sockets,
process creation and ``ctypes`` disabled; writes restricted to the declared output
directory, never the snapshot store) and only then imports the modules that read exports
and compute metrics. The guard stays active until the process exits.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

#: Same values as ``analytics.dataset.SCOPES`` / ``COUNT_KINDS`` (a test keeps them equal);
#: restated so that building the parser imports no analytics module.
SCOPES = ("organic", "promoted", "combined", "unknown")
COUNT_KINDS = ("cumulative", "window", "unknown")
SCHEMAS = ("x-post-v1",)
LANGUAGES = ("en", "tr")
VARIANTS = ("core", "extended", "sensitivity")

_DESCRIPTION = (
    "Descriptive metrics from your own post-level analytics export, computed in a process "
    "with networking, process creation and native code loading disabled. Not a reach "
    "prediction and not an algorithm score; missing values are unknown, never zero."
)


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    """Add ``metrics import | rwe | reach`` to the ``txray`` subcommands."""
    from .cli import fs_path

    metrics = commands.add_parser(
        "metrics", help="offline creator metrics from your own analytics export (no network)",
        description=_DESCRIPTION,
    )
    sub = metrics.add_subparsers(dest="metrics_command", required=True, metavar="SUBCOMMAND")

    imp = sub.add_parser(
        "import", parents=[common], help="import an export CSV into a private dataset",
        description=(
            "Import one export file into a private dataset directory. Every declaration is "
            "recorded with the import. The directory must lie outside the snapshot store "
            "(--store); nothing else is written."
        ),
    )
    imp.add_argument("csv", type=fs_path, help="export file (UTF-8 CSV, header row first)")
    imp.add_argument("--schema", required=True, choices=SCHEMAS, help="documented export schema")
    imp.add_argument("--lang", required=True, choices=LANGUAGES,
                     help="language of the header row")
    imp.add_argument("--out", metavar="DIR", type=fs_path,
                     help="dataset directory: new, empty, or an existing dataset (required "
                          "unless --dump-header)")
    imp.add_argument("--captured-at", metavar="TIME",
                     help="when the export was downloaded, ISO 8601 with offset "
                          "(e.g. 2026-09-30T18:00:00+09:00; required unless --dump-header)")
    imp.add_argument("--dump-header", action="store_true",
                     help="print the header row as exported, normalised, and its mapping to "
                          "the canonical fields; reads no data row and writes nothing")
    imp.add_argument("--counts", choices=COUNT_KINDS, default="unknown",
                     help="cumulative: lifetime totals at capture time; window: totals for a "
                          "date range; default unknown (horizon metrics then refuse them)")
    imp.add_argument("--scope", choices=SCOPES, default="unknown",
                     help="organic, promoted, or combined counts (default unknown)")
    imp.add_argument("--number-locale", choices=LANGUAGES,
                     help="separators of the numbers (default: the header language)")
    imp.add_argument("--source-timezone", default="UTC", metavar="ZONE",
                     help="IANA zone of date values without an offset (default UTC)")
    imp.add_argument("--date-format", metavar="FORMAT",
                     help="strptime format of the date column (default ISO 8601)")
    imp.set_defaults(handler=_run)

    rwe = sub.add_parser(
        "rwe", parents=[common], help="M1 realized weighted engagement per post",
        description=(
            "M1 (P6 section 4.1): reference-weighted observed events per 1,000 recorded "
            "impressions, with public default coefficients at a named upstream commit."
        ),
    )
    rwe.add_argument("dataset", type=fs_path, help="dataset directory made by 'txray metrics import'")
    rwe.add_argument("--variant", choices=VARIANTS, default="core",
                     help="core (default): likes, replies, reposts; extended adds URL clicks "
                          "and new follows (needs --attribution-validated); sensitivity adds "
                          "detail expands")
    rwe.add_argument("--attribution-validated", action="store_true",
                     help="state that URL clicks and new follows in this export are attributed "
                          "to the post (required by --variant extended; not verifiable here)")
    rwe.add_argument("--horizon", metavar="DURATION",
                     help="use the snapshot at a common observation age (e.g. 24h, 7d)")
    rwe.add_argument("--horizon-tolerance", metavar="DURATION",
                     help="accepted age above the horizon (default max(1h, horizon/10))")
    rwe.add_argument("--scope", choices=SCOPES, help="metric scope when the dataset has several")
    rwe.add_argument("--post", action="append", metavar="ID", help="only this post (repeatable)")
    rwe.set_defaults(handler=_run)

    reach = sub.add_parser(
        "reach", parents=[common], help="M2 relative reach against the account's own baseline",
        description=(
            "M2 (P6 section 5.1): impressions at a common horizon relative to a pre-publication "
            "baseline of the same account, as RR and the separately named RR+."
        ),
    )
    reach.add_argument("dataset", type=fs_path, help="dataset directory made by 'txray metrics import'")
    reach.add_argument("--horizon", required=True, metavar="DURATION",
                       help="common observation age (e.g. 24h, 72h, 7d)")
    reach.add_argument("--horizon-tolerance", metavar="DURATION",
                       help="accepted age above the horizon (default max(1h, horizon/10))")
    reach.add_argument("--timezone", default="UTC", metavar="ZONE",
                       help="IANA zone for weekday/time buckets (default UTC)")
    reach.add_argument("--band-hours", type=int, default=6, metavar="N",
                       help="width of the time-of-day band, a divisor of 24 (default 6)")
    reach.add_argument("--kappa", type=float, default=5.0, metavar="K",
                       help="shrinkage constant of the bucket median (default 5)")
    reach.add_argument("--min-parent", type=int, default=5, metavar="N",
                       help="fewest baseline posts for a defined baseline (default 5)")
    reach.add_argument("--scope", choices=SCOPES, help="metric scope when the dataset has several")
    reach.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    from .analytics import offline

    store_root = offline.snapshot_store_root(args.store)
    allowed: tuple[str, ...] = ()
    if args.metrics_command == "import" and args.out and not args.dump_header:
        allowed = (offline.check_output_location(args.out, [store_root]),)
    offline.enter(allowed_write_roots=allowed, forbidden_roots=[store_root])

    from .analytics import commands  # imported only once the process is offline

    command, data, text, warnings = commands.run(args)
    if args.json:
        from .cli import _emit_json

        _emit_json(command, {"outcome": "ok", "data": data, "warnings": warnings})
    else:
        sys.stdout.write(text)
    return 0
