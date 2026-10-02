"""``txray export``: optional exports of recorded findings for other tools.

::

    txray export context-layer --out DIR [--ledger DIR] [--commit C] [--include-stale]

Writes the findings of a findings ledger as Markdown notes that a Context Layer vault can
index (one note per finding, an index note with ``[[wikilinks]]`` and a README note with
the export's provenance). Only current findings by default. Nothing is written outside
``--out``; see ``docs/context-layer.md``. TimelineXray never imports, installs or starts
Context Layer. Accepts ``--store DIR`` and ``--json``; exit codes as for every command (0
ok, 1 refused or failed, 2 invalid arguments). Registered from
:func:`timelinexray.cli.build_parser` via :func:`register`; the export package is
imported only when the command runs.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from . import cli as _cli
from .snapshot.store import default_store_root


def _cmd_context_layer(args: argparse.Namespace) -> int:
    from .export import export_context_layer
    from .findings import ledger_directory

    store_root = Path(args.store).expanduser() if args.store else default_store_root()
    ledger = ledger_directory(args.ledger, store_root)
    result = export_context_layer(Path(args.out), store_root=store_root, ledger=ledger,
                                  commit=args.commit, include_stale=args.include_stale)
    data = result.to_dict()
    if args.json:
        _cli._emit_json(args.command, {"outcome": "ok", "data": data, "warnings": []})
        return 0
    counts = data["findings"]
    files = data["files"]
    left = ", ".join(f"{value} {key.replace('_', ' ')}"
                     for key, value in counts["left_out"].items()) or "none"
    lines = [
        f"exported   {counts['exported']} finding(s) ({counts['current']} current, "
        f"{counts['not_current']} not current) to {_cli._display(str(result.out))}",
        f"ledger     head {data['ledger']['head']} ({data['ledger']['events']} events)",
        f"freshness  {'at ' + data['commit'] if data['commit'] else 'at each finding latest check'}",
        f"files      {len(files['written'])} written, {len(files['unchanged'])} unchanged, "
        f"{len(files['removed'])} removed (listed in {files['manifest']})",
        f"left out   {left}",
        "next       context-layer index <vault> (optional; TimelineXray does not run it)",
    ]
    _cli._write("\n".join(lines) + "\n")
    return 0


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    group = commands.add_parser(
        "export",
        help="optional: write recorded findings for another tool (context-layer)",
        description=(
            "Optional integrations. Nothing is exported unless you run one of these "
            "commands; TimelineXray does not depend on, import or start the other tool."
        ),
    )
    targets = group.add_subparsers(dest="export_target", required=True, metavar="TARGET")
    target = targets.add_parser(
        "context-layer",
        parents=[common],
        help="findings as Markdown notes that a Context Layer vault can index",
        description=(
            "Write one Markdown note per finding (YAML frontmatter with id, title, evidence "
            "status and basis, freshness and its commit, evidence class, component; the claim; "
            "commit-pinned GitHub permalinks with span SHA-256; limitations), an index note "
            "linking them with [[wikilinks]] and a README note with the provenance. Only "
            "CURRENT findings unless --include-stale. Deterministic; written atomically and "
            "only inside --out; files of an earlier export are replaced or removed only as "
            "listed in its manifest. No analytics data is ever exported."
        ),
        epilog="exit codes: 0 ok, 1 refused or failed, 2 invalid arguments",
    )
    target.add_argument("--out", required=True, metavar="DIR", type=_cli.fs_path,
                        help="folder inside your notes vault (created if its parent exists)")
    target.add_argument("--ledger", metavar="DIR", default=None, type=_cli.fs_path,
                        help="findings ledger to read, never written (default: "
                             "$TXRAY_FINDINGS or <store>/findings)")
    target.add_argument("--commit", metavar="C", default=None,
                        help="freshness at this pinned commit instead of each finding's "
                             "latest check")
    target.add_argument("--include-stale", action="store_true",
                        help="also export findings that are not CURRENT, labelled NOT CURRENT")
    target.set_defaults(handler=_cmd_context_layer, command="export context-layer")
