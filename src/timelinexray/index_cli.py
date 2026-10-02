"""``txray index | search | symbols``: the Milestone 2 commands.

::

    txray index <commit> [--rebuild] [--coverage]
    txray search <commit> <query> [--path GLOB] [--limit N] [--literal]
    txray symbols <commit> [--path GLOB] [--kind K] [--name N] [--limit N] [--calls]

All three accept ``--store DIR`` and ``--json`` like the Milestone 1 commands and use the
same exit codes. Registered from :func:`timelinexray.cli.build_parser` via :func:`register`.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from . import cli as _cli
from .index import (
    SEARCH_DEFAULT_LIMIT,
    SEARCH_MAX_LIMIT,
    SYMBOLS_DEFAULT_LIMIT,
    SYMBOLS_MAX_LIMIT,
    BuildReport,
    CodeIndex,
    Coverage,
)
from .syntax import CALL_RELATION, SYMBOL_KINDS


def _limit(maximum: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text, 10)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}") from None
        if not 1 <= value <= maximum:
            raise argparse.ArgumentTypeError(f"must be from 1 to {maximum}, got {value}")
        return value

    return parse


def _index(args: argparse.Namespace) -> CodeIndex:
    return CodeIndex(_cli._store(args))


def _counts(counter: dict[str, int]) -> str:
    return ", ".join(f"{name} {value}" for name, value in sorted(counter.items())) or "none"


# -- index ---------------------------------------------------------------------------------


def _format_build(report: BuildReport, index: CodeIndex) -> str:
    work = report.to_dict()["work"]
    if report.up_to_date:
        state = "up to date (manifest and syntax backends unchanged; nothing was read)"
    else:
        state = (
            f"{'rebuilt' if report.rebuild else 'built'} in {report.seconds.get('total', 0):.2f} s"
            f" - read {work['blobs_read']} blobs; lexical {work['lexical_new']} new,"
            f" {work['lexical_reused']} reused; syntax {work['parses_new']} parsed,"
            f" {work['parses_reused']} reused"
        )
    indexed = report.lexical.get("indexed", 0)
    skipped = {k: v for k, v in report.lexical.items() if k != "indexed"}
    lines = [
        f"commit      {report.commit}",
        f"manifest    sha256:{report.manifest_sha256}",
        f"index       {index.path}",
        f"generation  {state}",
        f"paths       {report.files}: lexical {indexed} indexed, "
        f"{sum(skipped.values())} skipped ({_counts(skipped)})",
        f"syntax      {_counts(dict(report.syntax))}",
        "backends    " + (
            ", ".join(f"{lang} {name}" for lang, name in sorted(report.backends.items()))
            or "none"
        ),
        f"symbols     {report.symbols} ({_counts(report.symbols_by_language)})",
        f"calls       {report.calls} unresolved call candidates (syntactic, never resolved)",
    ]
    for item in index.registry.report():
        if item["unavailable_reason"]:
            lines.append(f"unavailable {item['backend']}: {item['unavailable_reason']}")
    return "\n".join(lines) + "\n"


def _format_coverage(coverage: Coverage) -> str:
    out = ["lexical\treason\tsyntax\treason\tbackend\tsymbols\tcalls\tpath"]
    for row in coverage.rows:
        out.append(
            "\t".join(
                (
                    row.lexical_status,
                    row.lexical_reason or "-",
                    row.syntax_status,
                    row.syntax_reason or "-",
                    row.backend or "-",
                    "-" if row.symbols is None else str(row.symbols),
                    "-" if row.calls is None else str(row.calls),
                    _cli._display(row.path),
                )
            )
        )
    return "\n".join(out) + "\n"


def _cmd_index(args: argparse.Namespace) -> int:
    index = _index(args)
    report = index.build(args.commit, rebuild=args.rebuild)
    coverage = index.coverage(report.commit) if args.coverage else None
    if args.json:
        data: dict[str, Any] = {
            "index": str(index.path),
            "build": report.to_dict(),
            "syntax_backends": index.registry.report(),
        }
        if coverage is not None:
            data["coverage"] = coverage.to_dict()
        _cli._emit_json("index", {"outcome": "ok", "data": data, "warnings": []})
        return 0
    _cli._write(_format_build(report, index))
    if coverage is not None:
        _cli._write(_format_coverage(coverage))
    return 0


# -- search --------------------------------------------------------------------------------


def _cmd_search(args: argparse.Namespace) -> int:
    result = _index(args).search(
        args.commit, args.query, path_glob=args.path, limit=args.limit, literal=args.literal
    )
    note = (_cli.default_note(result.commit)
            if _cli.has_digit(*(hit.snippet for hit in result.hits)) else None)
    if args.json:
        _cli._emit_json("search", {"outcome": "ok", "data": result.to_dict(), "warnings": [],
                                   "note": note})
        return 0
    mode = "literal phrase" if result.literal else "all terms"
    lines = [
        f"commit   {result.commit}",
        f"query    {result.query!r} ({mode}, case-insensitive substring, one line)",
        f"hits     {result.total}" + (f" (showing {len(result.hits)})" if result.truncated else ""),
    ]
    for hit in result.hits:
        lines.append(
            f"{_cli._display(hit.path)}:{hit.start_line}  sha256:{hit.span_sha256[:16]}  "
            f"{_cli._display(hit.snippet)}"
        )
    if note:
        lines.append(f"note     {note}")
    _cli._write("\n".join(lines) + "\n")
    return 0


# -- symbols -------------------------------------------------------------------------------


def _cmd_symbols(args: argparse.Namespace) -> int:
    result = _index(args).symbols(
        args.commit,
        path_glob=args.path,
        kind=args.kind,
        name=args.name,
        limit=args.limit,
        with_calls=args.calls,
    )
    note = (_cli.default_note(result.commit)
            if _cli.has_digit(*(symbol.signature for symbol in result.symbols)) else None)
    if args.json:
        _cli._emit_json("symbols", {"outcome": "ok", "data": result.to_dict(), "warnings": [],
                                    "note": note})
        return 0
    lines = [
        f"commit   {result.commit}",
        f"symbols  {result.total}" + (
            f" (showing {len(result.symbols)})" if result.truncated else ""
        ),
    ]
    for symbol in result.symbols:
        span = f"{symbol.start_line}-{symbol.end_line}"
        lines.append(
            f"{symbol.kind:<11} {_cli._display(symbol.qualname)}  {_cli._display(symbol.path)}:{span}"
            f"  [{symbol.backend}]"
        )
        if symbol.calls:
            for call in symbol.calls:
                separator = "::" if call.form == "path" else "."
                target = f"{call.qualifier}{separator}{call.callee}" if call.qualifier else call.callee
                lines.append(f"    {CALL_RELATION} {_cli._display(target)} ({call.form}) line "
                             f"{call.line}, unresolved")
    if note:
        lines.append(f"note     {note}")
    _cli._write("\n".join(lines) + "\n")
    return 0


# -- registration ----------------------------------------------------------------------------


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    """Add the index, search and symbols subcommands to the ``txray`` parser."""
    index = commands.add_parser(
        "index",
        parents=[common],
        help="build the code index of a pinned commit (incremental by blob)",
        description=(
            "Index the parsed-candidate and text paths of a pinned commit from its git blobs: "
            "an FTS5 trigram index of lines, plus symbols and unresolved call candidates for "
            "Rust, Scala, Python and Java. Blobs already indexed for another commit are "
            "reused. Every manifest path gets a coverage row."
        ),
    )
    index.add_argument("commit", help="pinned commit id or unique prefix (7+ digits)")
    index.add_argument(
        "--rebuild", action="store_true",
        help="re-read and re-derive every blob of this commit instead of reusing cached rows",
    )
    index.add_argument(
        "--coverage", action="store_true",
        help="also list every path with its lexical and syntax status, reason and backend",
    )
    index.set_defaults(handler=_cmd_index)

    search = commands.add_parser(
        "search",
        parents=[common],
        help="find lines containing every query term at an indexed commit",
        description=(
            "A hit is one line containing every whitespace-separated term as a "
            "case-insensitive substring (--literal: the whole query as one substring). The "
            "query is never interpreted as FTS5 or SQL syntax. Each hit reports the SHA-256 "
            "of its exact line span, as txray show would."
        ),
        epilog="write the query after -- if it starts with '-': txray search C -- -term",
    )
    search.add_argument("commit", help="indexed commit id or unique prefix (7+ digits)")
    search.add_argument("query", help="search terms; at least one of 3+ characters")
    search.add_argument(
        "--path", metavar="GLOB",
        help="only paths matching this SQLite GLOB (case-sensitive; * also matches /)",
    )
    search.add_argument(
        "--limit", type=_limit(SEARCH_MAX_LIMIT), default=SEARCH_DEFAULT_LIMIT, metavar="N",
        help=f"maximum hits to print (default {SEARCH_DEFAULT_LIMIT}, at most {SEARCH_MAX_LIMIT})",
    )
    search.add_argument(
        "--literal", action="store_true", help="match the whole query as one substring"
    )
    search.set_defaults(handler=_cmd_search)

    symbols = commands.add_parser(
        "symbols",
        parents=[common],
        help="list declarations found by the syntax backends at an indexed commit",
        description=(
            "Symbols with 1-based line ranges, the SHA-256 of their exact span and the "
            "backend that produced them. --calls adds each symbol's call candidates; these "
            "are syntactic (a name followed by an argument list) and never resolved."
        ),
    )
    symbols.add_argument("commit", help="indexed commit id or unique prefix (7+ digits)")
    symbols.add_argument("--path", metavar="GLOB", help="only paths matching this SQLite GLOB")
    symbols.add_argument("--kind", choices=SYMBOL_KINDS, metavar="K",
                         help="only this kind: " + ", ".join(SYMBOL_KINDS))
    symbols.add_argument(
        "--name", metavar="N",
        help="exact name or qualified name; *, ? and [...] make it a GLOB",
    )
    symbols.add_argument(
        "--limit", type=_limit(SYMBOLS_MAX_LIMIT), default=SYMBOLS_DEFAULT_LIMIT, metavar="N",
        help=f"maximum symbols to print (default {SYMBOLS_DEFAULT_LIMIT})",
    )
    symbols.add_argument(
        "--calls", action="store_true", help="include each symbol's unresolved call candidates"
    )
    symbols.set_defaults(handler=_cmd_symbols)
