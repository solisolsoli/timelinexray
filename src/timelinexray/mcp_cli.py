"""``txray mcp``: the read-only MCP server (Milestone 4).

::

    txray mcp serve [--store DIR] [--ledger DIR] [--time-budget SECONDS]   stdio server
    txray mcp tools [--json [--strict]]                     list the tools and schemas

The findings tools read one findings ledger chosen here, never by a client: ``--ledger``,
else ``$TXRAY_FINDINGS``, else ``<store>/findings`` (resolved with
:func:`timelinexray.findings.ledger_directory`; when that default would lie inside a git
working tree, the server still starts and the findings tools report why no ledger is
available). An explicit ``--ledger`` must be an existing directory.

Registered from :func:`timelinexray.cli.build_parser` via :func:`register`. The server
module is imported only when a subcommand runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import InvalidInput, Refused
from .snapshot.store import ENV_STORE, default_store_root

_DESCRIPTION = (
    "Serve pinned public code to MCP clients over stdio (JSON-RPC, one message per line): "
    "read-only, public-code profile only, no network, no analytics data. Protocol "
    "revisions 2026-07-28 (per-request metadata) and 2025-11-25 (initialize handshake)."
)


def _budget(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected seconds, got {text!r}") from None
    if not 0.01 <= value <= 300:
        raise argparse.ArgumentTypeError("must be from 0.01 to 300 seconds")
    return value


def _ledger_location(explicit: str | None, store_root: str) -> tuple[str | None, str | None]:
    """``(ledger directory, None)`` or ``(None, why no ledger is available)``."""
    from .findings import ledger_directory

    if explicit is not None and not Path(explicit).expanduser().is_dir():
        raise Refused(f"findings ledger {explicit} does not exist or is not a directory")
    try:
        return str(ledger_directory(explicit, Path(store_root))), None
    except Refused:  # only the default location is refused: it lies inside a git working tree
        return None, ("the default findings ledger <store>/findings lies inside a git working "
                      "tree; start the server with --ledger DIR or set $TXRAY_FINDINGS")


def _cmd_serve(args: argparse.Namespace) -> int:
    from .mcp.server import DEFAULT_TIME_BUDGET, serve_stdio

    root = args.store if args.store is not None else str(default_store_root())
    budget = args.time_budget if args.time_budget is not None else DEFAULT_TIME_BUDGET
    ledger, problem = _ledger_location(args.ledger, root)
    return serve_stdio(root, time_budget=budget, ledger=ledger, ledger_problem=problem)


def _cmd_tools(args: argparse.Namespace) -> int:
    from .mcp.server import SUPPORTED_VERSIONS
    from .mcp.tools import build_registry

    registry = build_registry()
    if args.strict and not args.json:
        raise InvalidInput("--strict needs --json")
    if args.json:
        payload = {"protocolVersions": list(SUPPORTED_VERSIONS),
                   "tools": registry.definitions(strict=args.strict)}
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n")
        return 0
    lines = [f"{tool.name:<18}{tool.title}" for tool in registry]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    """Add ``mcp serve | tools`` to the ``txray`` subcommands."""
    from .cli import fs_path

    mcp = commands.add_parser(
        "mcp", help="read-only MCP server over stdio (public-code profile)",
        description=_DESCRIPTION,
    )
    sub = mcp.add_subparsers(dest="mcp_command", required=True, metavar="SUBCOMMAND")
    serve = sub.add_parser(
        "serve", help="run the stdio server until stdin closes",
        description=_DESCRIPTION + " Logs go to stderr; stdout carries only protocol messages.",
    )
    serve.add_argument(
        "--store", metavar="DIR", default=None, type=fs_path,
        help=f"snapshot store directory (default: ${ENV_STORE} or ~/.cache/timelinexray)",
    )
    serve.add_argument(
        "--ledger", metavar="DIR", default=None, type=fs_path,
        help="findings ledger read by the findings tools (read-only; default: $TXRAY_FINDINGS "
             "or <store>/findings); must exist when given",
    )
    serve.add_argument(
        "--time-budget", type=_budget, default=None, metavar="SECONDS",
        help="wall-clock budget of one tool call (default 10)",
    )
    serve.set_defaults(handler=_cmd_serve, json=False)
    tools = sub.add_parser(
        "tools", help="list the tools this server offers",
        description="Print the tool names, or with --json the full tools/list definitions "
                    "(input and published output JSON Schemas; --strict prints the strict "
                    "output schemas every result is validated against).",
    )
    tools.add_argument("--json", action="store_true", help="print the definitions as JSON")
    tools.add_argument("--strict", action="store_true",
                       help="with --json: the strict output schemas instead of the published ones")
    tools.set_defaults(handler=_cmd_tools)
