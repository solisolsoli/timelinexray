"""Parameter tools over the index and the pinned commits (P7 ``get_param``, ``param_history``).

``get_param``      every declaration of a named parameter at one indexed commit: literal
                   public default, declared type, flag string, citation; lexical mentions
``param_history``  the value at every pinned commit of one line of history, with changes
                   cited on both sides, reversions and gaps; pinned commits only, no fetch

Both are thin shapes over :class:`timelinexray.params.ParamResolver`, which reuses the
Milestone 2 index and the Milestone 5b value extraction. Every value shown is a public
default at its commit, never a production value.
"""

from __future__ import annotations

from typing import Any

from ...params import (
    DECLARED,
    LOOKUP_INDEX,
    LOOKUP_KNOWN_PATHS,
    MAX_HISTORY_COMMITS,
    MAX_NAME_CHARS,
    REMOVED,
    VALUE_CHANGED,
    ParamDeclaration,
    ParamResolver,
)
from ...snapshot.store import PinRecord
from .. import schema as S
from ..context import ToolContext, display_path
from .base import Registry, Tool, ToolResult
from .code import CITATION

DECLARATIONS_MAX = 50
MENTIONS_MAX = 10
TIMELINES_MAX = 50
CHANGES_MAX = 200
REVERSIONS_MAX = 100
VALUE_MAX = 1000

DECLARATION_KINDS = ("param!", "const", "static", "field")
SYMBOL_KINDS = ("const", "field", "param", "static")
EVENTS = (DECLARED, VALUE_CHANGED, REMOVED)
LOOKUPS = (LOOKUP_INDEX, LOOKUP_KNOWN_PATHS)

HISTORY_NOTE = (
    "Values are public defaults at the cited commits, not production values. The history "
    "is computed from pinned commits only (nothing is fetched; commit messages are never "
    "read): a change is first seen at the pinned commit named, which may be later than the "
    "upstream commit that made it when commits in between are unpinned (see gaps)."
)
MENTIONS_NOTE = (
    "Lines containing the name as a case-insensitive substring outside the declarations "
    "(lexical, unresolved); not every mention is a use of this parameter."
)
LOOKUP_NOTE = (
    "Declarations found through the code index by exact name or qualified name (kinds "
    "const, static, field, param!). Config-file keys are not reachable by name: use "
    "search_code."
)

NAME = S.string(MAX_NAME_CHARS, description="Exact name or qualified name (no GLOB).")


def _clip(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[: limit - 24] + f" [... {len(text) - limit + 24} more]"


def _declaration(ctx: ToolContext, pins: dict[str, PinRecord], item: ParamDeclaration) -> dict[str, Any]:
    pin = pins.get(item.commit)
    if pin is None:
        pin = pins[item.commit] = ctx.pin(item.commit)
    return {
        "citation": ctx.citation(pin, item.path, item.start_line, item.end_line,
                                 item.span_sha256, item.blob_oid),
        "declaration": item.declaration,
        "symbol_kind": item.symbol_kind,
        "name": item.name,
        "qualname": item.qualname,
        "container": item.container,
        "ordinal": item.ordinal,
        "language": item.language,
        "value": _clip(item.value, VALUE_MAX),
        "type": _clip(item.value_type, 200),
        "flag": _clip(item.flag, VALUE_MAX),
        "literal": item.literal,
        "signature": item.signature[:VALUE_MAX],
        "value_note": item.to_dict()["value_note"],
    }


DECLARATION = S.obj(
    {
        "citation": CITATION,
        "declaration": {"type": "string", "enum": list(DECLARATION_KINDS)},
        "symbol_kind": {"type": "string", "enum": list(SYMBOL_KINDS)},
        "name": S.string(1024, min_length=0),
        "qualname": S.string(4096, min_length=0),
        "container": S.nullable(S.string(4096, min_length=0)),
        "ordinal": {"type": "integer", "minimum": 0},
        "language": S.nullable(S.string(64)),
        "value": S.nullable(S.string(VALUE_MAX, min_length=0)),
        "type": S.nullable(S.string(200, min_length=0)),
        "flag": S.nullable(S.string(VALUE_MAX, min_length=0)),
        "literal": {"type": "boolean"},
        "signature": S.string(VALUE_MAX, min_length=0),
        "value_note": S.string(200),
    }
)


# -- get_param ----------------------------------------------------------------------------


def _get_param(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pin = ctx.pin(args["commit"])
    _, gid = ctx.generation(pin.commit)
    resolver = ParamResolver(ctx.store, ctx.index, check=ctx.check_deadline)
    snapshot = resolver.declarations(pin.commit, args["name"])
    ctx.check_deadline()
    pins = {pin.commit: pin}
    declarations = [_declaration(ctx, pins, item)
                    for item in snapshot.declarations[:DECLARATIONS_MAX]]
    total, hits = resolver.mentions(pin.commit, args["name"], snapshot.declarations, MENTIONS_MAX)
    warnings = []
    if len(snapshot.declarations) > DECLARATIONS_MAX:
        warnings.append(f"TRUNCATED: {len(snapshot.declarations)} declarations; the first "
                        f"{DECLARATIONS_MAX} in path order are listed")
    if not snapshot.declarations:
        warnings.append(f"no const, static, field or param! declaration named {args['name']!r} "
                        f"at commit {pin.commit}; {total} line(s) contain the text")
    data = {
        "name": args["name"],
        "total": len(snapshot.declarations),
        "returned": len(declarations),
        "declarations": declarations,
        "mentions_total": total,
        "mentions": [
            {
                "citation": ctx.citation(pin, hit.path, hit.start_line, hit.end_line,
                                         hit.span_sha256, hit.blob_oid),
                "snippet": hit.snippet,
            }
            for hit in hits
        ],
    }
    return ToolResult(data, commit=pin.commit, index_generation=gid, warnings=warnings,
                      notes=[LOOKUP_NOTE, MENTIONS_NOTE],
                      truncated=bool(warnings and warnings[0].startswith("TRUNCATED")))


GET_PARAM = Tool(
    name="get_param",
    title="Parameter declarations and public default at a commit",
    description=(
        "Every declaration of a named parameter (param!, const, static or field; exact or "
        "qualified name) at a pinned, indexed commit: literal public default, declared type, "
        "flag, cited declaration span, and up to 10 lexical mentions elsewhere. Public "
        "defaults at that commit, never production values."
    ),
    input_schema=S.obj({"commit": S.COMMIT_FULL, "name": NAME}, required=["commit", "name"]),
    data_schema=S.obj(
        {
            "name": S.string(MAX_NAME_CHARS),
            "total": {"type": "integer", "minimum": 0},
            "returned": {"type": "integer", "minimum": 0},
            "declarations": S.array(DECLARATION, DECLARATIONS_MAX),
            "mentions_total": {"type": "integer", "minimum": 0},
            "mentions": S.array(
                S.obj({"citation": CITATION, "snippet": S.string(1000, min_length=0)}),
                MENTIONS_MAX,
            ),
        }
    ),
    handler=_get_param,
    list_key="declarations",
)


# -- param_history ------------------------------------------------------------------------


def _param_history(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pins: dict[str, PinRecord] = {}
    for record in ctx.store.list_pins():
        ctx.check_deadline()
        pins[record.commit] = ctx.pin(record.commit)
    resolver = ParamResolver(ctx.store, ctx.index, check=ctx.check_deadline)
    history = resolver.history(args["name"], base=args.get("base"), head=args.get("head"))
    ctx.check_deadline()
    timelines = []
    for timeline in history.timelines[:TIMELINES_MAX]:
        changes = [
            {
                "event": change.event,
                "commit": change.commit,
                "committer_time": change.committer_time,
                "previous_commit": change.previous_commit,
                "old_value": _clip(change.old.value, VALUE_MAX) if change.old else None,
                "new_value": _clip(change.new.value, VALUE_MAX) if change.new else None,
                "old": _declaration(ctx, pins, change.old) if change.old else None,
                "new": _declaration(ctx, pins, change.new) if change.new else None,
            }
            for change in timeline.changes[:CHANGES_MAX]
        ]
        timelines.append({
            "path": display_path(timeline.path),
            "declaration": timeline.declaration,
            "qualname": timeline.qualname,
            "ordinal": timeline.ordinal,
            "points": [
                {"commit": commit, "committer_time": time, "value": _clip(value, VALUE_MAX)}
                for commit, time, value in timeline.points[:MAX_HISTORY_COMMITS]
            ],
            "changes": changes,
            "reversions": [
                {"value": _clip(item["value"], VALUE_MAX), "left_at": item["left_at"],
                 "back_at": item["back_at"]}
                for item in timeline.reversions[:REVERSIONS_MAX]
            ],
            "current": _declaration(ctx, pins, timeline.current) if timeline.current else None,
        })
    warnings = list(history.warnings)
    if len(history.timelines) > TIMELINES_MAX:
        warnings.append(f"TRUNCATED: {len(history.timelines)} declarations change over this "
                        f"line; the first {TIMELINES_MAX} in path order are listed")
    data = {
        "name": history.name,
        "base": history.base,
        "head": history.head,
        "order": history.order,
        "commits": [
            {
                "commit": snapshot.commit,
                "committer_time": snapshot.committer_time,
                "indexed": snapshot.indexed,
                "lookup": snapshot.lookup,
                "declared": len(snapshot.declarations),
            }
            for snapshot in history.commits
        ],
        "timelines": timelines,
        "history_complete": history.history_complete,
        "gaps": list(history.gaps[:MAX_HISTORY_COMMITS]),
        "not_on_line": list(history.not_on_line[:MAX_HISTORY_COMMITS]),
    }
    return ToolResult(data, warnings=warnings, notes=[HISTORY_NOTE],
                      truncated=len(history.timelines) > TIMELINES_MAX)


POINT = S.obj(
    {
        "commit": S.COMMIT_FULL,
        "committer_time": S.string(32),
        "value": S.nullable(S.string(VALUE_MAX, min_length=0)),
    }
)
CHANGE = S.obj(
    {
        "event": {"type": "string", "enum": list(EVENTS)},
        "commit": S.COMMIT_FULL,
        "committer_time": S.string(32),
        "previous_commit": S.COMMIT_FULL,
        "old_value": S.nullable(S.string(VALUE_MAX, min_length=0)),
        "new_value": S.nullable(S.string(VALUE_MAX, min_length=0)),
        "old": S.nullable(DECLARATION),
        "new": S.nullable(DECLARATION),
    }
)
REVERSION = S.obj(
    {
        "value": S.nullable(S.string(VALUE_MAX, min_length=0)),
        "left_at": S.COMMIT_FULL,
        "back_at": S.COMMIT_FULL,
    }
)
TIMELINE = S.obj(
    {
        "path": S.string(4096),
        "declaration": {"type": "string", "enum": list(DECLARATION_KINDS)},
        "qualname": S.string(4096, min_length=0),
        "ordinal": {"type": "integer", "minimum": 0},
        "points": S.array(POINT, MAX_HISTORY_COMMITS),
        "changes": S.array(CHANGE, CHANGES_MAX),
        "reversions": S.array(REVERSION, REVERSIONS_MAX),
        "current": S.nullable(DECLARATION),
    }
)

PARAM_HISTORY = Tool(
    name="param_history",
    title="Parameter value over the pinned commits",
    description=(
        "A named parameter's value at every pinned commit of one first-parent chain (base to "
        "head; default: oldest to newest pin; nothing is fetched). Per declaration: value and "
        "committer time per commit, changes (declared, value-changed, removed) cited on both "
        "sides, reversions, the current declaration; plus gaps where commits are unpinned. "
        "Public defaults only, never production values."
    ),
    input_schema=S.obj(
        {
            "name": NAME,
            "base": S.COMMIT_FULL,
            "head": S.COMMIT_FULL,
        },
        required=["name"],
    ),
    data_schema=S.obj(
        {
            "name": S.string(MAX_NAME_CHARS),
            "base": S.COMMIT_FULL,
            "head": S.COMMIT_FULL,
            "order": S.string(200),
            "commits": S.array(
                S.obj(
                    {
                        "commit": S.COMMIT_FULL,
                        "committer_time": S.string(32),
                        "indexed": {"type": "boolean"},
                        "lookup": {"type": "string", "enum": list(LOOKUPS)},
                        "declared": {"type": "integer", "minimum": 0},
                    }
                ),
                MAX_HISTORY_COMMITS,
            ),
            "timelines": S.array(TIMELINE, TIMELINES_MAX),
            "history_complete": {"type": "boolean"},
            "gaps": S.array(S.string(400), MAX_HISTORY_COMMITS),
            "not_on_line": S.array(S.COMMIT_FULL, MAX_HISTORY_COMMITS),
        }
    ),
    handler=_param_history,
    list_key="timelines",
)


def register(registry: Registry) -> None:
    for tool in (GET_PARAM, PARAM_HISTORY):
        registry.add(tool)
