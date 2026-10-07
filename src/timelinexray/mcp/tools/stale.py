"""``stale_worklist``: the stale-review worklist of the configured ledger, read only.

The same data as ``txray findings stale --json`` (:mod:`timelinexray.findings.stale`): for a
target commit (default: the newest pin), every active finding with checkable evidence that is
not ``CURRENT`` there, in the worklist order (parameter findings, then findings on scoring
paths, then the rest; then the review-trigger priority), each with its old spans, the spans
the tool located or line-diff-aligned at the target, and the commands a person runs to
re-read and decide; plus the counts and the batch steps.

Read only, like the other findings tools: the event log is read through the same guarded,
chain-verified path (:func:`.findings.open_events`), under a shared lock when the ledger's
lock file exists; nothing is written, nothing is approved, and no draft file is produced
(``--spec-dir`` exists only in the CLI). Output is bounded: at most 20 entries per page
(``limit``, ``cursor``), 10 citations, 5 occurrences, 10 dependencies and 20 read commands
per entry, 50 finding ids per batch step, free text clipped. The printed commands carry no
``--store``/``--ledger`` (no local location is ever shown): they are meant for the operator's
environment, which serves this store and ledger.
"""

from __future__ import annotations

from typing import Any

from ...errors import InvalidInput
from ...findings.freshness import PinIndex
from ...findings.stale import AREAS, ORDER_NOTE, newest_target, worklist_from_events
from .. import schema as S
from ..context import ToolContext
from .base import Registry, Tool, ToolResult
from .findings import LEDGER_NOTE, _ledger_info, open_events

PAGE_MAX = 20
CITATIONS_MAX = 10
OCCURRENCES_MAX = 5
DEPENDENCIES_MAX = 10
READ_MAX = 20
BATCH_IDS_MAX = 50
COMMITS_MAX = 20
TITLE_MAX = 300
TEXT_MAX = 1000

STALE_NOTE = (
    "A worklist, not a verdict: freshness is not truth, nothing here approves a finding or "
    "changes its evidence, and a candidate span is a line-diff proposal to read, not a check. "
    "The commands omit --store/--ledger: run them, outside this server, where txray reaches "
    "the store and ledger this server serves; AUTHOR, REVIEWER, STATUS, RATIONALE, REASON and "
    "SPEC_DIR are placeholders for a person."
)

STR = {"type": "string"}
NSTR = {"type": ["string", "null"]}
INT = {"type": "integer"}
NINT = {"type": ["integer", "null"]}
COUNT = {"type": "integer", "minimum": 0}
BOOL = {"type": "boolean"}
NBOOL = {"type": ["boolean", "null"]}
SPAN = {"type": ["object", "null"]}
STRS = {"type": "array", "items": STR}

ENTRY = {
    "type": "object",
    "properties": {
        "rank": COUNT, "finding_id": STR, "title": NSTR, "component": NSTR,
        "evidence_class": NSTR, "status": NSTR, "status_basis": NSTR, "workflow": NSTR,
        "freshness": STR, "area": STR, "priority": INT, "check_event": NSTR,
        "citations": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "index": NINT, "outcome": NSTR, "freshness": NSTR, "reason": NSTR,
                "old": SPAN, "candidate": SPAN, "candidates": {"type": "array"},
                "anchor_at_target": SPAN, "search_complete": NBOOL,
            },
        }},
        "citations_total": COUNT, "current_citations": COUNT,
        "dependencies": {"type": "array"}, "negative": SPAN, "unpinned_commits": STRS,
        "draft": {"type": "object"}, "commands": {"type": "object"},
    },
}


def _clip(text: Any, limit: int) -> str | None:
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _anchor(found: Any) -> dict[str, Any] | None:
    """Where the anchor occurs in the target blob: path, count and the first lines."""
    if not isinstance(found, dict):
        return None
    return {"path": found.get("path"), "count": found.get("count"),
            "lines": list(found.get("lines") or [])[:OCCURRENCES_MAX * 4]}


def _citation(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": item.get("index"),
        "outcome": item.get("outcome"),
        "freshness": item.get("freshness"),
        "reason": _clip(item.get("reason"), TEXT_MAX),
        "old": item.get("old"),
        "candidate": item.get("candidate"),
        "candidates": [{key: occurrence.get(key) for key in ("path", "start_line", "end_line")}
                       for occurrence in (item.get("candidates") or [])[:OCCURRENCES_MAX]],
        "anchor_at_target": _anchor(item.get("anchor_at_target")),
        "search_complete": item.get("search_complete"),
    }


def _entry(entry: dict[str, Any]) -> dict[str, Any]:
    negative = entry.get("negative")
    commands = entry["commands"]
    return {
        "rank": entry["rank"],
        "finding_id": entry["finding_id"],
        "title": _clip(entry.get("title"), TITLE_MAX),
        "component": _clip(entry.get("component"), TITLE_MAX),
        "evidence_class": entry.get("evidence_class"),
        "status": entry.get("status"),
        "status_basis": entry.get("status_basis"),
        "workflow": entry.get("workflow"),
        "freshness": entry["freshness"],
        "area": entry["area"],
        "priority": entry["priority"],
        "check_event": entry.get("check_event"),
        "citations": [_citation(item) for item in entry["citations"][:CITATIONS_MAX]],
        "citations_total": len(entry["citations"]),
        "current_citations": entry.get("current_citations") or 0,
        "dependencies": [{"kind": dep.get("kind"), "finding_id": dep.get("finding_id"),
                          "freshness": dep.get("freshness"),
                          "reason": _clip(dep.get("reason"), TEXT_MAX)}
                         for dep in (entry.get("dependencies") or [])[:DEPENDENCIES_MAX]],
        "negative": (None if not negative else
                     {"freshness": negative.get("freshness"), "total": negative.get("total")}),
        "unpinned_commits": list(entry.get("unpinned_commits") or [])[:COMMITS_MAX],
        "draft": entry["draft"],
        "commands": {"read": list(commands["read"])[:READ_MAX], "decide": commands["decide"]},
    }


def _batch(step: dict[str, Any]) -> dict[str, Any]:
    findings = list(step.get("findings") or [])
    return {
        "why": step.get("why"),
        "findings": findings[:BATCH_IDS_MAX],
        "findings_total": len(findings),
        "commits": list(step.get("commits") or [])[:COMMITS_MAX],
        "command": step.get("command"),
        "effect": step.get("effect"),
    }


def _stale_worklist(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pins = PinIndex.of(ctx.store)
    if args.get("target") is not None:
        target = ctx.pin(args["target"]).commit
    else:
        target = newest_target(pins)
    area = args.get("area")
    limit = args.get("limit", 5)
    events = open_events(ctx)
    ctx.check_deadline()
    work = worklist_from_events(events, pins, target)  # no --store/--ledger in the commands
    ctx.check_deadline()
    pager = ctx.pager("stale_worklist", args, target, work.view.head)
    selected = [entry for entry in work.entries if area is None or entry["area"] == area]
    pager.total = len(selected)
    offset = pager.offset
    if offset > len(selected):
        raise InvalidInput("cursor is past the end of the worklist; repeat without a cursor")
    entries = [_entry(entry) for entry in selected[offset: offset + limit]]
    newest = pins.latest()
    data = {
        "target": target,
        "newest_pin": newest.commit if newest else None,
        "counts": work.counts(),
        "batch": [_batch(step) for step in work.batch()],
        "total": len(selected),
        "offset": offset,
        "returned": len(entries),
        "entries": entries,
        "ledger": _ledger_info(work.view),
    }
    warnings = []
    if work.not_checked:
        warnings.append(f"{len(work.not_checked)} finding(s) have no check at the target and are "
                        "not listed one by one: run the batch reanchor command (outside this "
                        "server)")
    return ToolResult(data, warnings=warnings, pager=pager,
                      notes=[LEDGER_NOTE, STALE_NOTE, f"Order: {ORDER_NOTE}."])


STALE_WORKLIST = Tool(
    name="stale_worklist",
    title="Stale-review worklist",
    description=(
        "Read-only stale-review worklist of the findings ledger at a pinned commit (default: "
        "the newest pin), as txray findings stale --json: old and aligned spans, commands."
    ),
    input_schema=S.obj(
        {
            "target": S.COMMIT_FULL,
            "area": {"type": "string", "enum": list(AREAS)},
            "limit": {"type": "integer", "minimum": 1, "maximum": PAGE_MAX, "default": 5},
            "cursor": S.string(2048),
        },
        required=[],
    ),
    data_schema=S.obj(
        {
            "target": S.COMMIT_FULL,
            "newest_pin": S.nullable(dict(S.COMMIT_FULL)),
            "counts": {"type": "object"},
            "batch": S.array({"type": "object"}, 2),
            "total": COUNT,
            "offset": COUNT,
            "returned": COUNT,
            "entries": S.array(ENTRY, PAGE_MAX),
            "ledger": S.obj({"head": STR, "events": COUNT}),
        }
    ),
    handler=_stale_worklist,
    list_key="entries",
    output_budget=900,
)


def register(registry: Registry) -> None:
    registry.add(STALE_WORKLIST)


__all__ = ["STALE_WORKLIST", "register"]
