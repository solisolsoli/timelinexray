"""``txray param | param-history``: parameter declarations and their history.

::

    txray param <name> [--commit C]                 declarations and public default at a commit
    txray param-history <name> [--base C] [--head C]   value at every pinned commit of one line

Both accept ``--store DIR`` and ``--json`` like the other commands and use the same exit
codes. ``param`` needs an indexed commit (default: the newest pin); ``param-history`` reads
pinned commits only and never fetches. Registered from
:func:`timelinexray.cli.build_parser` via :func:`register`.
"""

from __future__ import annotations

import argparse
from typing import Any

from . import cli as _cli
from .errors import NotFound
from .params import ParamDeclaration, ParamHistory, ParamResolver

MENTIONS_SHOWN = 10


def _resolver(args: argparse.Namespace) -> ParamResolver:
    return ParamResolver(_cli._store(args))


def _format_declaration(item: ParamDeclaration) -> list[str]:
    span = f"{item.start_line}-{item.end_line}" if item.start_line != item.end_line else str(item.start_line)
    head = (f"{item.declaration:<11} {_cli._display(item.qualname)}  "
            f"{_cli._display(item.path)}:{span}  sha256:{item.span_sha256[:16]}")
    lines = [head]
    if item.value is None:
        lines.append("            value: none extracted (computed, or test code)")
    else:
        detail = f"            public default {_cli._display(item.value)}"
        if item.value_type:
            detail += f"  type {_cli._display(item.value_type)}"
        if item.flag is not None:
            detail += f'  flag "{_cli._display(item.flag)}"'
        lines.append(detail)
    return lines


# -- param ---------------------------------------------------------------------------------


def _cmd_param(args: argparse.Namespace) -> int:
    store = _cli._store(args)
    if args.commit is None:
        pins = store.list_pins()
        if not pins:
            raise NotFound("the snapshot store has no pinned commits; run: txray pin <commit>")
        commit = max(pins, key=lambda p: (p.committer_time, p.commit)).commit
    else:
        commit = store.get_pin(args.commit).commit
    resolver = _resolver(args)
    snapshot = resolver.declarations(commit, args.name)
    total, hits = resolver.mentions(commit, args.name, snapshot.declarations, MENTIONS_SHOWN)
    note = _cli.default_note(commit) if snapshot.declarations else None
    if args.json:
        data: dict[str, Any] = snapshot.to_dict()
        data["mentions_total"] = total
        data["mentions"] = [hit.to_dict() for hit in hits]
        _cli._emit_json("param", {"outcome": "ok", "data": data, "warnings": [], "note": note})
        return 0
    lines = [
        f"commit      {snapshot.commit}",
        f"name        {_cli._display(args.name)}",
        f"declared    {len(snapshot.declarations)} declaration(s) (const, static, field or param!; "
        f"found through the index)",
    ]
    for item in snapshot.declarations:
        lines.extend(_format_declaration(item))
    lines.append(f"mentions    {total} line(s) contain the text; "
                 f"{len(hits)} outside the declarations shown (lexical, unresolved)")
    for hit in hits:
        lines.append(f"            {_cli._display(hit.path)}:{hit.start_line}  "
                     f"{_cli._display(hit.snippet)}")
    text = "\n".join(lines) + "\n"
    if note:
        text += f"note        {_cli.default_note(commit)}\n"
    _cli._write(text)
    return 0


# -- param-history -------------------------------------------------------------------------


def _format_history(history: ParamHistory) -> str:
    lines = [
        f"name        {_cli._display(history.name)}",
        f"line        {history.base[:12]}..{history.head[:12]}: {len(history.commits)} pinned "
        f"commit(s), {history.order}",
        "history     " + ("complete (no unpinned commits between the pins)"
                          if history.history_complete else
                          "incomplete: " + "; ".join(history.gaps)),
    ]
    unindexed = [s.commit for s in history.commits if not s.indexed]
    if unindexed:
        lines.append(f"unindexed   {len(unindexed)} pinned commit(s) checked at known paths only: "
                     + ", ".join(c[:12] for c in unindexed))
    if history.not_on_line:
        lines.append("off line    " + ", ".join(c[:12] for c in history.not_on_line))
    for warning in history.warnings:
        lines.append(f"warning     {warning}")
    if not history.timelines:
        lines.append("declared    never, at any pinned commit of this line")
    for timeline in history.timelines:
        lines.append("")
        lines.append(f"{timeline.declaration:<11} {_cli._display(timeline.qualname)}  "
                     f"{_cli._display(timeline.path)}")
        first = timeline.points[0] if timeline.points else None
        if first is not None and not any(c.event == "declared" and c.commit == first[0]
                                         for c in timeline.changes):
            lines.append(f"  {first[1]}  {first[0][:12]}  present at the start of the line, "
                         f"public default {_cli._display(first[2] or 'none')}")
        for change in timeline.changes:
            if change.event == "value-changed":
                assert change.old is not None and change.new is not None
                lines.append(
                    f"  {change.committer_time}  {change.commit[:12]}  value changed: public "
                    f"default {_cli._display(change.old.value or 'none')} -> "
                    f"{_cli._display(change.new.value or 'none')}  (old L{change.old.start_line} "
                    f"sha256:{change.old.span_sha256[:16]} at {change.previous_commit[:12]}; "
                    f"new L{change.new.start_line} sha256:{change.new.span_sha256[:16]})"
                )
            elif change.event == "declared":
                assert change.new is not None
                lines.append(
                    f"  {change.committer_time}  {change.commit[:12]}  declared with public "
                    f"default {_cli._display(change.new.value or 'none')}  (L{change.new.start_line} "
                    f"sha256:{change.new.span_sha256[:16]})"
                )
            else:
                assert change.old is not None
                lines.append(
                    f"  {change.committer_time}  {change.commit[:12]}  removed (public default "
                    f"was {_cli._display(change.old.value or 'none')} at "
                    f"{change.previous_commit[:12]} L{change.old.start_line} "
                    f"sha256:{change.old.span_sha256[:16]})"
                )
        for item in timeline.reversions:
            lines.append(f"  reversion: {_cli._display(str(item['value']))} left at "
                         f"{item['left_at'][:12]}, back at {item['back_at'][:12]}")
        if timeline.current is not None:
            lines.append(f"  current: public default {_cli._display(timeline.current.value or 'none')} "
                         f"at {history.head[:12]} ({len(timeline.points)} pinned commit(s) with a value)")
        else:
            lines.append(f"  current: not declared at {history.head[:12]}")
    text = "\n".join(lines) + "\n"
    if history.timelines:
        text += ("note        values are public defaults at the cited commits, not production "
                 "values; computed from pinned commits only (nothing fetched)\n")
    return text


def _cmd_param_history(args: argparse.Namespace) -> int:
    history = _resolver(args).history(args.name, base=args.base, head=args.head)
    if args.json:
        note = ("values are public defaults at the cited commits, not production values"
                if history.timelines else None)
        _cli._emit_json("param-history", {"outcome": "ok", "data": history.to_dict(),
                                          "warnings": list(history.warnings), "note": note})
        return 0
    _cli._write(_format_history(history))
    return 0


# -- registration ----------------------------------------------------------------------------


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    """Add the param and param-history subcommands to the ``txray`` parser."""
    param = commands.add_parser(
        "param",
        parents=[common],
        help="declarations and public default of a named parameter at an indexed commit",
        description=(
            "Every const, static, field or param! declaration with the given name or "
            "qualified name at a pinned, indexed commit (default: the newest pin): its "
            "literal public default, declared type and flag string, the exact span with its "
            "SHA-256, and up to ten lines mentioning the name elsewhere (lexical, unresolved). "
            "Values are public defaults at that commit, never production values."
        ),
    )
    param.add_argument("name", help="exact name or qualified name (no GLOB)")
    param.add_argument("--commit", metavar="C", default=None,
                       help="indexed commit id or unique prefix (default: the newest pin)")
    param.set_defaults(handler=_cmd_param)

    history = commands.add_parser(
        "param-history",
        parents=[common],
        help="the value of a named parameter at every pinned commit of one line of history",
        description=(
            "Follow a parameter over the pinned commits on the first-parent chain from "
            "--base to --head (default: the oldest to the newest pin of the same mirror); "
            "nothing is fetched and commit messages are never read. Lists the value at each "
            "pinned commit, every change with the citation on both sides, reversions, and "
            "gaps where commits between two pins are not pinned. Values are public defaults "
            "at the cited commits, never production values."
        ),
    )
    history.add_argument("name", help="exact name or qualified name (no GLOB)")
    history.add_argument("--base", metavar="C", default=None,
                         help="oldest pinned commit of the line (default: the oldest pin)")
    history.add_argument("--head", metavar="C", default=None,
                         help="newest pinned commit of the line (default: the newest pin)")
    history.set_defaults(handler=_cmd_param_history)
