"""Tools about pinned commits: ``list_commits``, ``resolve_commit``, ``manifest_summary``."""

from __future__ import annotations

from typing import Any

from ...snapshot.store import PinRecord
from .. import schema as S
from ..context import ToolContext, display_path, upstream_label
from .base import Registry, Tool, ToolResult

MAX_COMMITS = 200
MAX_LICENSES = 200

PIN = S.obj(
    {
        "commit": S.COMMIT_FULL,
        "tree": S.OID,
        "committer_time": S.string(32),
        "upstream": S.string(256),
        "manifest_sha256": S.SHA256,
        "entry_count": {"type": "integer", "minimum": 0},
        "indexed": {"type": "boolean"},
        "index_generation": S.nullable(S.string(64)),
    }
)


def pin_summary(ctx: ToolContext, pin: PinRecord) -> dict[str, Any]:
    generation = ctx.generation_or_none(pin.commit)
    return {
        "commit": pin.commit,
        "tree": pin.tree,
        "committer_time": pin.committer_time,
        "upstream": upstream_label(pin),
        "manifest_sha256": pin.manifest_sha256,
        "entry_count": pin.entry_count,
        "indexed": generation is not None,
        "index_generation": generation[1] if generation else None,
    }


# -- list_commits -------------------------------------------------------------------------


def _list_commits(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    records = sorted(
        ctx.store.list_pins(), key=lambda pin: (pin.committer_time, pin.commit), reverse=True
    )
    commits = []
    for pin in records[:MAX_COMMITS]:
        ctx.check_deadline()
        commits.append(pin_summary(ctx, ctx.pin(pin.commit)))
    warnings = []
    if not records:
        warnings.append("the snapshot store has no pinned commits; run: txray pin <commit>")
    return ToolResult(
        {"commits": commits, "total": len(records), "returned": len(commits)},
        warnings=warnings,
        source_text=False,
    )


LIST_COMMITS = Tool(
    name="list_commits",
    title="List pinned commits",
    description=(
        "List the upstream commits pinned in the local snapshot store, newest committer time "
        "first, with manifest hash and whether the code index covers them. Only pinned "
        "commits can be read; this server never fetches."
    ),
    input_schema=S.obj({}),
    data_schema=S.obj(
        {
            "commits": S.array(PIN, MAX_COMMITS),
            "total": {"type": "integer", "minimum": 0},
            "returned": {"type": "integer", "minimum": 0},
        }
    ),
    handler=_list_commits,
    list_key="commits",
)


# -- resolve_commit -----------------------------------------------------------------------


def _resolve_commit(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pin = ctx.pin(args["commit"])
    data = {"requested": args["commit"], **pin_summary(ctx, pin)}
    return ToolResult(data, commit=pin.commit, index_generation=data["index_generation"],
                      source_text=False)


RESOLVE_COMMIT = Tool(
    name="resolve_commit",
    title="Resolve a pinned commit",
    description=(
        "Resolve a commit id or unique prefix (7-40 lowercase hex digits) to the full id of a "
        "pinned commit. Branch and tag names are not accepted. Other tools require the full "
        "40-digit id this returns."
    ),
    input_schema=S.obj(
        {"commit": {"type": "string", "pattern": "^[0-9a-f]{7,40}$",
                    "description": "Commit id or unique prefix, 7-40 lowercase hex digits."}}
    ),
    data_schema=S.obj({"requested": S.string(40), **PIN["properties"]}),
    handler=_resolve_commit,
)


# -- manifest_summary ---------------------------------------------------------------------

LICENSE = S.obj(
    {
        "path": S.string(1024),
        "kind": S.string(64),
        "blob_oid": S.OID,
        "sha256": S.nullable(dict(S.SHA256)),
        "size": S.nullable({"type": "integer", "minimum": 0}),
        "classification": S.string(32),
        "hints": S.array(S.string(128), 32),
    }
)


def _manifest_summary(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pin = ctx.pin(args["commit"])
    _, manifest = ctx.store.load_manifest(pin.commit)
    summary = pin_summary(ctx, pin)
    licenses = [
        {
            "path": display_path(item.path),
            "kind": item.kind,
            "blob_oid": item.oid,
            "sha256": item.sha256,
            "size": item.size,
            "classification": item.classification,
            "hints": list(item.hints),
        }
        for item in manifest.licenses[:MAX_LICENSES]
    ]
    warnings = []
    if len(manifest.licenses) > MAX_LICENSES:
        warnings.append(
            f"TRUNCATED: {len(manifest.licenses)} license files, the first {MAX_LICENSES} are listed"
        )
    data = {
        "pin": summary,
        "parents": list(manifest.parents),
        "object_format": manifest.object_format,
        "classifier": dict(manifest.classifier),
        "counts": manifest.counts(),
        "licenses": licenses,
        "license_hint_note": (
            "Hints are keyword matches in license files, not license determinations."
        ),
    }
    return ToolResult(data, commit=pin.commit, index_generation=summary["index_generation"],
                      warnings=warnings, truncated=len(manifest.licenses) > MAX_LICENSES)


MANIFEST_SUMMARY = Tool(
    name="manifest_summary",
    title="Manifest summary of a pinned commit",
    description=(
        "Summarise the verified manifest of a pinned commit: every path is accounted for as "
        "parsed-candidate, text or excluded (with the exclusion reason); counts by class, "
        "reason and guessed language; classifier settings; license files with keyword hints. "
        "Retrieve this before substantive research on a commit."
    ),
    input_schema=S.obj({"commit": S.COMMIT_FULL}),
    data_schema=S.obj(
        {
            "pin": PIN,
            "parents": S.array(S.OID, 16),
            "object_format": {"type": "string", "enum": ["sha1", "sha256"]},
            "classifier": S.counts(),
            "counts": S.obj(
                {
                    "total": {"type": "integer", "minimum": 0},
                    "classification": S.counts(),
                    "excluded_reason": S.counts(),
                    "language": S.counts(),
                }
            ),
            "licenses": S.array(LICENSE, MAX_LICENSES),
            "license_hint_note": S.string(200),
        }
    ),
    handler=_manifest_summary,
    list_key="licenses",
)


def register(registry: Registry) -> None:
    for tool in (LIST_COMMITS, RESOLVE_COMMIT, MANIFEST_SUMMARY):
        registry.add(tool)
