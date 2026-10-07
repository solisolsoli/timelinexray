"""``txray diff | digest | update``: the Milestone 5b commands.

::

    txray diff   <old> <new> [--class C]                     classified two-commit diff
    txray digest <old> <new> [--out DIR] [--format md|json] [--ledger DIR]
                                                             digest for a commit range
    txray update --out DIR [--upstream URL] [--branch B] [--since COMMIT] [--ledger DIR]
                 [--reanchor] [--export DIR]                  guarded fetch + pin + digest
                                                             (+ ledger refresh, notes)

All accept ``--store DIR`` and ``--json`` and use the Milestone 1 exit codes (0 ok, 1
failed or attention needed, 2 invalid arguments, 3 network refused). Registered from
:func:`timelinexray.cli.build_parser` via :func:`register`. Nothing is ever posted.
"""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path
from typing import Any

from . import cli as _cli
from .diff import CLASSES, ChangeItem, CommitDiff, DiffEngine, SymbolSource
from .diff.model import Citation
from .digest import (
    AffectedFindingsProvider,
    FORMATS,
    DigestBuilder,
    default_findings_provider,
    render_digest,
    write_digests,
)
from .digest.build import digest_summary
from .digest.update import FAILED, NO_CHANGE, run_update
from .findings import ledger_directory
from .fsutil import check_output_directory
from .index import CodeIndex
from .netguard import DEFAULT_UPSTREAM_URL, ENV_ALLOW_FILE_URLS, Allowlist
from .snapshot.store import SnapshotStore


def _symbols(args: argparse.Namespace) -> SymbolSource:
    return SymbolSource(CodeIndex(_cli._store(args)))


def _findings(args: argparse.Namespace, store: SnapshotStore) -> AffectedFindingsProvider:
    """The read-only ledger provider: --ledger, else $TXRAY_FINDINGS, else <store>/findings."""
    return default_findings_provider(store, args.ledger, index=CodeIndex(store))


def _cite(citation: Citation) -> str:
    commit = citation.commit[:12]
    if citation.start_line is None:
        return f"{commit} {citation.role}" + (f" ({citation.note})" if citation.note else "")
    lines = (f"L{citation.start_line}" if citation.start_line == citation.end_line
             else f"L{citation.start_line}-L{citation.end_line}")
    text = f"{commit} {lines} sha256 {citation.span_sha256}"
    if citation.role == "context":
        text += " (context)"
    return text


# -- diff ----------------------------------------------------------------------------------


def _cmd_diff(args: argparse.Namespace) -> int:
    store = _cli._store(args)
    diff = DiffEngine(store, _symbols(args)).diff(args.old, args.new)
    items = [item for item in diff.items if args.change_class in (None, item.change_class)]
    if args.json:
        data = diff.to_dict()
        data["items"] = [item.to_dict() for item in items]
        data["filter"] = {"class": args.change_class}
        _cli._emit_json("diff", {"outcome": "ok", "data": data, "warnings": _warnings(diff)})
        return 0
    _cli._write(_format_diff(diff, items))
    return 0


def _warnings(diff: CommitDiff) -> list[str]:
    warnings = []
    if diff.rename_detection != "complete":
        warnings.append("rename detection was limited to files with the same base name "
                        "(too many added and removed files to compare every pair)")
    return warnings


def _format_diff(diff: CommitDiff, items: list[ChangeItem]) -> str:
    counts = diff.counts()
    status = counts["files_by_status"]
    classes = ", ".join(f"{name} {value}" for name, value in counts["items_by_class"].items() if value)
    out = [
        f"old        {diff.old.commit}  ({diff.old.committer_time})",
        f"new        {diff.new.commit}  ({diff.new.committer_time})",
        f"files      {counts['files']} changed: {status['added']} added, {status['removed']} "
        f"removed, {status['modified']} modified, {status['renamed']} renamed; "
        f"+{counts['lines_added']} -{counts['lines_removed']} lines",
        f"items      {counts['items']}: {classes or 'none'}",
        f"symbols    {', '.join(diff.symbol_backends) or 'none'}; renames {diff.rename_detection}; "
        f"classifier v{diff.classifier_version}",
        "note       values are public defaults at the cited commit, not production values; "
        "commit messages are not read",
        "----",
    ]
    for item in items:
        out.append(f"[{item.change_class}] {_cli._display(item.path)}  {item.summary}")
        out.append(f"    old {_cite(item.old)}")
        out.append(f"    new {_cite(item.new)}")
        symbols = ", ".join(dict.fromkeys(item.new_symbols + item.old_symbols))
        if symbols:
            out.append(f"    in  {symbols}")
    if not items:
        out.append("(no items)")
    return "\n".join(out) + "\n"


# -- digest --------------------------------------------------------------------------------


def _cmd_digest(args: argparse.Namespace) -> int:
    store = _cli._store(args)
    findings = _findings(args, store)
    out_dir = None
    if args.out is not None:  # refused before anything is built
        out_dir = check_output_directory(Path(args.out), store_root=store.root,
                                         ledger=getattr(findings, "directory", None))
    builder = DigestBuilder(store, allowlist=Allowlist.from_env(), findings=findings,
                            symbols=_symbols(args))
    document = builder.build(args.old, args.new)
    if out_dir is None:
        if args.json:
            _cli._emit_json("digest", {"outcome": "ok", "data": document, "warnings": []})
        else:
            _cli._write(render_digest(document, args.format))
        return 0
    written = write_digests(out_dir, document, (args.format,))  # md brings its appendix
    summary = digest_summary(document)
    if args.json:
        _cli._emit_json("digest", {"outcome": "ok", "data": {"written": written, **summary},
                                   "warnings": []})
    else:
        _cli._write("".join(f"wrote      {out_dir / name}\n" for name in written)
                    + _format_summary(summary))
    return 0


def _format_summary(summary: dict[str, Any], *, events: bool = True) -> str:
    classes = ", ".join(f"{k} {v}" for k, v in summary["items_by_class"].items() if v) or "none"
    reasons = ", ".join(f"{k} {v}" for k, v in summary.get("unknown_reasons", {}).items())
    lines = [
        f"range      {summary['old'][:12]}..{summary['new'][:12]} "
        f"({summary['range']['relationship']}, {summary['range']['commits_in_range']} commits)",
        f"items      {classes}",
        f"parameters {summary['parameter_changes']} public-default changes; "
        f"{summary.get('registration_changes', 0)} registration changes",
    ]
    if reasons:
        lines.append(f"unknown    {reasons} (listed with citations in the appendix)")
    if events:
        for event in summary["events"]:
            lines.append(f"event      {event['kind']} ({event['severity']}): {event['message']}")
    return "\n".join(lines) + "\n"


# -- update --------------------------------------------------------------------------------


ADDED_SHOWN = 10


def _stale_lines(args: argparse.Namespace, refresh: Any) -> list[str]:
    """The stale-review counts and next commands after ``update --reanchor``."""
    summary = refresh.stale_review
    if not summary:
        return []
    counts = summary["counts"]
    where = [*(["--store", str(args.store)] if args.store else []),
             *(["--ledger", str(args.ledger)] if args.ledger else [])]
    lines = []
    if counts["not_current"]:
        areas = ", ".join(f"{k} {v}" for k, v in counts["by_area"].items())
        lines.append(f"review     {counts['not_current']} finding(s) not current at the head "
                     f"({areas}): "
                     + shlex.join(["txray", "findings", "stale", "--target",
                                   refresh.target[:12], *where]))
    for step in summary["batch"]:
        if "commits" in step:
            lines.append(f"unpinned   {len(step['findings'])} finding(s) cite "
                         f"{len(step['commits'])} unpinned commit(s): "
                         + shlex.join(["txray", "findings", "verify", "--pin-cited", *where]))
    return lines


def _cmd_update(args: argparse.Namespace) -> int:
    store = _cli._store(args)
    ledger = None
    if args.reanchor or args.export is not None:
        ledger = ledger_directory(args.ledger, store.root)  # refused inside a git working tree
    result = run_update(
        store,
        out_dir=Path(args.out),
        allowlist=Allowlist.from_env(),
        upstream=args.upstream,
        branch=args.branch,
        since=args.since,
        findings=_findings(args, store),
        symbols=_symbols(args),
        ledger=ledger,
        reanchor=args.reanchor,
        export_dir=Path(args.export) if args.export is not None else None,
    )
    refresh = result.refresh
    if args.json:
        warnings = [e.message for e in result.events if e.severity == "attention"]
        if refresh is not None and refresh.added:
            warnings.append(f"{len(refresh.added)} new review item(s) appeared: run txray "
                            "findings queue")
        if result.export is not None and result.export.get("failed"):
            warnings.append(f"the export failed: {result.export['message']}")
        _cli._emit_json("update", {
            "outcome": "ok" if result.exit_code == 0 else "attention",
            "data": result.status,
            "warnings": warnings,
        })
        return result.exit_code
    lines = [
        f"outcome    {result.outcome}",
        f"head       {result.head or '-'}",
        f"previous   {result.previous or '-'}",
        f"accepted   {result.accepted or '-'}",
    ]
    for name in result.written:
        lines.append(f"wrote      {Path(args.out) / name}")
    lines.append(f"status     {Path(args.out) / 'update-status.json'}")
    if result.digest is not None:
        lines += _format_summary(result.digest, events=False).splitlines()[1:]
    for event in result.events:
        lines.append(f"event      {event.kind} ({event.severity}): {event.message}")
    if refresh is not None:
        counts = ", ".join(f"{k} {v}" for k, v in refresh.freshness.items()) or "none"
        lines.append(f"reanchor   {refresh.target}: {refresh.findings} finding(s) re-anchored "
                     f"({refresh.selection}): {counts}")
        lines.append(f"queue      {refresh.queue_before} -> {refresh.queue_after} open item(s); "
                     f"{len(refresh.added)} new, {len(refresh.removed)} closed")
        # the new items in the stale-review order (parameter, scoring, other), first ten only
        for item in refresh.added[:ADDED_SHOWN]:
            scope = f" @ {str(item['scope'])[:12]}" if item.get("scope") else ""
            lines.append(f"           P{item['priority']} {item['trigger']:<22} "
                         f"{_cli._display(str(item['finding_id']))}{scope}")
        if len(refresh.added) > ADDED_SHOWN:
            lines.append(f"           ... and {len(refresh.added) - ADDED_SHOWN} more "
                         "(update-status.json lists every one)")
        lines.extend(_stale_lines(args, refresh))
    if result.export is not None:
        if result.export.get("failed"):
            lines.append(f"export     failed: {result.export['message']}")
        else:
            found = result.export["findings"]
            files = result.export["files"]
            lines.append(f"export     {found['exported']} finding(s) exported ({found['current']} "
                         f"current, {found['not_current']} not current): {files['written']} "
                         f"written, {files['unchanged']} unchanged, {files['removed']} removed")
    if refresh is not None and refresh.added:
        lines.append(f"next       txray findings stale   ({len(refresh.added)} new review "
                     "item(s) appeared in this run; the review line above has the full command, "
                     "with the old and aligned spans and the re-review commands; txray findings "
                     "queue lists the items by trigger)")
    elif refresh is None and result.head and result.outcome not in (NO_CHANGE, FAILED):
        # a new pin makes every finding checked only at older pins "not current" until
        # the ledger is re-anchored on it (docs/findings-memory.md, "Current")
        lines.append("next       txray findings reanchor --latest   (re-anchor the findings "
                     "ledger on the new pin; findings checked only at older pins are not "
                     "current until then; or run txray update --reanchor)")
    _cli._write("\n".join(lines) + "\n")
    return result.exit_code


# -- registration ----------------------------------------------------------------------------


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    diff = commands.add_parser(
        "diff",
        parents=[common],
        help="classified diff of two pinned commits, with citations on both sides",
        description=(
            "Compare two pinned commits from git objects: added, removed, modified and "
            "renamed paths with blob ids, line hunks on both sides, Milestone 2 symbols, and "
            "classified items (" + ", ".join(CLASSES) + "). Commit messages are never read."
        ),
    )
    diff.add_argument("old", help="pinned commit id or unique prefix (7+ digits)")
    diff.add_argument("new", help="pinned commit id or unique prefix (7+ digits)")
    diff.add_argument("--class", dest="change_class", choices=CLASSES, metavar="C",
                      help="only items of this class: " + ", ".join(CLASSES))
    diff.set_defaults(handler=_cmd_diff)

    digest = commands.add_parser(
        "digest",
        parents=[common],
        help="Markdown or JSON digest of a commit range, generated from diffs",
        description=(
            "Summarise old..new: counts by class, public-default parameter changes with "
            "citations and intermediate values, registration changes, affected findings, "
            "unknown changes and intermediate history. Commits of the first-parent chain are "
            "pinned locally when needed (objects must already be in the mirror; nothing is "
            "fetched). Nothing is posted."
        ),
    )
    digest.add_argument("old", help="pinned commit id or unique prefix (7+ digits)")
    digest.add_argument("new", help="pinned commit id or unique prefix (7+ digits)")
    digest.add_argument("--out", metavar="DIR", type=_cli.fs_path,
                        help="write digest-<old>-<new>.md and its -appendix.md (or .json) into DIR")
    digest.add_argument("--format", choices=FORMATS, default="md",
                        help="md (default: the bounded main digest; with --out also its "
                             "appendix), appendix (every item the main digest only counts) or "
                             "json (the complete document)")
    digest.add_argument("--ledger", metavar="DIR", default=None, type=_cli.fs_path,
                        help="findings ledger read (never written) for the affected-findings "
                             "section (default: $TXRAY_FINDINGS or <store>/findings)")
    digest.set_defaults(handler=_cmd_digest)

    update = commands.add_parser(
        "update",
        parents=[common],
        help="guarded fetch, pin the new head, digest against the previous pin",
        description=(
            "One observation of the upstream: fetch through the network guard (only "
            f"{DEFAULT_UPSTREAM_URL} or file:// URLs listed in ${ENV_ALLOW_FILE_URLS}), pin "
            "the branch head, write a digest against the previous accepted pin into --out, and "
            "report exceptional events (fetch failure, rewritten history, commits deleted "
            "upstream). Pins are never removed. Nothing is posted. Schedule it yourself (see "
            "docs/updates.md)."
        ),
        epilog="exit codes: 0 ok, 1 failed or attention needed, 2 invalid arguments, "
               "3 network refused",
    )
    update.add_argument("--out", required=True, metavar="DIR", type=_cli.fs_path,
                        help="directory for digests and update-status.json")
    update.add_argument("--upstream", default=DEFAULT_UPSTREAM_URL, metavar="URL",
                        help="allowlisted repository URL")
    update.add_argument("--branch", default="main", metavar="NAME",
                        help="upstream branch to follow (default main)")
    update.add_argument("--since", metavar="COMMIT",
                        help="compare against this pinned commit instead of the accepted one")
    update.add_argument("--ledger", metavar="DIR", default=None, type=_cli.fs_path,
                        help="findings ledger read for the digest's affected findings and, "
                             "with --reanchor, written (default: $TXRAY_FINDINGS or "
                             "<store>/findings)")
    update.add_argument("--reanchor", action="store_true",
                        help="after pinning the head, re-anchor every active finding of the "
                             "ledger on it (verify events, as txray findings reanchor <head>; "
                             "when the head did not change, only findings without a check at "
                             "it), print the review-queue delta, and exit 1 when new review "
                             "items appeared")
    update.add_argument("--export", metavar="DIR", default=None, type=_cli.fs_path,
                        help="then refresh the Context Layer notes in DIR (txray export "
                             "context-layer --out DIR: CURRENT findings only, same "
                             "destination rules, checked before the fetch)")
    update.set_defaults(handler=_cmd_update)
