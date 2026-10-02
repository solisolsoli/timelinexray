"""``txray findings ...``: the Milestone 3 commands.

::

    txray findings add        --actor NAME (--file SPEC.json | --title ... --cite ...)
    txray findings import     FILE --source-label LABEL --actor NAME
    txray findings list       [--workflow W] [--status S] [--freshness F] [--target C] [--current]
    txray findings show       ID [--target C]
    txray findings verify     [ID ...]
    txray findings reanchor   (TARGET | --latest) [ID ...] [--strict]
    txray findings review     ID --actor NAME --role reviewer --status S --rationale TEXT [--target C]
    txray findings supersede  ID --actor NAME (--by ID | --file SPEC.json) --rationale TEXT
    txray findings retract    ID --actor NAME --reason TEXT
    txray findings queue      [--include-imported]
    txray findings export     [--format findings|research|events] [--label L] [--out FILE]
    txray findings verify-log [--expect-head HASH] [--repair-head]

Every command accepts ``--store DIR``, ``--ledger DIR`` and ``--json``. The ledger defaults
to ``$TXRAY_FINDINGS``, else ``<store>/findings`` (refused when that would lie inside a git
working tree). Write commands accept ``--expect-head HASH`` (compare-and-swap on the ledger
head). Exit codes follow the other commands: 0 ok, 1 failed (including ``verify-log``
problems and ``reanchor --strict`` with a finding that is not ``CURRENT``), 2 invalid
arguments. Registered from :func:`timelinexray.cli.build_parser` via :func:`register`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from . import cli as _cli
from .errors import InvalidInput
from .findings import (
    STATUSES,
    VERIFIER_ACTOR,
    WORKFLOWS,
    Actor,
    FindingsMemory,
    FindingState,
    Ledger,
    ledger_directory,
)
from .findings.freshness import READINGS, PinIndex, describe, evaluate, is_current
from .findings.model import EVIDENCE_CLASSES, ROLES, SCOPES
from .snapshot.store import default_store_root


def _memory(args: argparse.Namespace) -> FindingsMemory:
    store = _cli._store(args)
    root = Path(args.store) if args.store else default_store_root()
    return FindingsMemory(Ledger(ledger_directory(args.ledger, root)), store)


def _ledger(args: argparse.Namespace) -> Ledger:
    root = Path(args.store) if args.store else default_store_root()
    return Ledger(ledger_directory(args.ledger, root))


def _actor(args: argparse.Namespace, default_role: str) -> Actor:
    return Actor(args.actor, args.role or default_role)


def _emit(args: argparse.Namespace, data: Any, warnings: list[str] | None = None) -> None:
    _cli._emit_json(args.command, {"outcome": "ok", "data": data, "warnings": warnings or []})


def _read_spec(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except FileNotFoundError:
        raise InvalidInput(f"specification file {path} does not exist") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidInput(f"specification file {path} is not valid UTF-8 JSON: {exc}") from None


def _status_text(state: FindingState) -> str:
    return f"{state.status} ({state.status_basis})"


def _fresh_text(state: FindingState, target: str | None = None) -> str:
    freshness, against = state.freshness(target)
    if against is None:
        return freshness
    return f"{freshness}@{against[:12] if against != 'cited' else 'cited'}"


def _public_default(state: FindingState) -> str:
    record = state.record
    if record.get("evidence_class") != "PARAM_DEFAULT":
        return ""
    commits = sorted({c["commit"][:12] for c in record["sources"] if c.get("kind") == "code"})
    return f"  [public default at {', '.join(commits)}]" if commits else "  [public default]"


# -- add / import --------------------------------------------------------------------------------


def _cmd_add(args: argparse.Namespace) -> int:
    if args.file:
        flags = [args.title, args.claim, args.component, args.evidence_class, args.status,
                 args.cite, args.depends_on, args.id, args.scope]
        if any(flag for flag in flags):
            raise InvalidInput("give either --file or the individual flags, not both")
        spec = _read_spec(args.file)
    else:
        spec = {
            "title": args.title, "claim": args.claim, "component": args.component,
            "evidence_class": args.evidence_class, "status": args.status,
            "citations": [{"commit": c, "path": p, "lines": lines, "anchor": a}
                          for c, p, lines, a in args.cite or []],
            "depends_on": [{"finding": item} for item in args.depends_on or []],
        }
        if args.id:
            spec["finding_id"] = args.id
        if args.scope:
            spec["scope"] = args.scope
    state = _memory(args).add(spec, _actor(args, "author"), expect_head=args.expect_head)
    if args.json:
        _emit(args, state.to_dict())
        return 0
    _cli._write(f"created    {state.finding_id}  ({state.workflow}, {_status_text(state)})\n"
                f"title      {state.record['title']}{_public_default(state)}\n"
                f"citations  {len([s for s in state.record['sources'] if s['kind'] == 'code'])}"
                " code span(s) read from their git blobs, anchors inside the spans\n"
                "next       txray findings verify, then a different reviewer may review it\n")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    report = _memory(args).import_file(args.file, args.source_label, _actor(args, "importer"),
                                       expect_head=args.expect_head)
    if args.json:
        _emit(args, report)
        return 0
    lines = [
        f"file       {report['file']} ({report['format']}, sha256 {report['file_sha256']})",
        f"label      {report['source_label']}",
        f"findings   {report['findings']}: {report['imported']} imported, "
        f"{report['unchanged']} already present",
        "workflow   imported (report status kept as 'reported'; nothing promoted)",
        f"verified   {report['verified']} with code sources; freshness "
        f"{_counts(report['freshness'])}",
        f"spans      {_counts(report['citations'])}; anchors {_counts(report['anchors'])}",
    ]
    if report["unpinned_commits"]:
        lines.append(f"unpinned   {_counts(report['unpinned_commits'])} (citations per commit)")
        lines.append(f"next       {report['hint']}")
    lines.append(f"head       {report['head']}")
    _cli._write("\n".join(lines) + "\n")
    return 0


def _counts(counter: dict[str, int]) -> str:
    return ", ".join(f"{key} {value}" for key, value in sorted(counter.items())) or "none"


# -- list / show / queue ----------------------------------------------------------------------------


def _target(memory: FindingsMemory, target: str | None) -> str | None:
    return memory.full_commit(target) if target else None


def _cmd_list(args: argparse.Namespace) -> int:
    memory = _memory(args)
    view = memory.view()
    target = _target(memory, args.target)
    pins = PinIndex.of(memory.store)
    states = []
    for state in view.states():
        fresh = evaluate(state, pins, target)
        if not args.all and not state.active and args.workflow is None:
            continue
        if args.workflow and state.workflow != args.workflow:
            continue
        if args.status and state.status != args.status:
            continue
        if args.freshness and fresh.value != args.freshness:
            continue
        if args.current and not is_current(state, fresh):
            continue
        if args.label and (state.record.get("origin") or {}).get("source_label") != args.label:
            continue
        states.append((state, fresh))
    newest = pins.latest()
    if args.json:
        _emit(args, {"head": view.head, "target": target,
                     "newest_pin": newest.commit if newest else None, "total": len(states),
                     "findings": [{**state.summary(target), "reading": fresh.to_dict(),
                                   "current": is_current(state, fresh)}
                                  for state, fresh in states]})
        return 0
    lines = [f"findings {len(states)}  (ledger head {view.head[:16]}"
             + (f"; newest pin {newest.commit[:12]}" if newest else "") + ")"]
    for state, fresh in states:
        title = state.record.get("title") or ""
        marker = "current" if is_current(state, fresh) else "not current"
        lines.append(f"{state.finding_id:<24} {state.workflow:<10} {_status_text(state):<30} "
                     f"{marker:<12} {title[:60]}\n    {describe(fresh)}")
    _cli._write("\n".join(lines) + "\n")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    memory = _memory(args)
    state = memory.get(args.finding_id)
    target = _target(memory, args.target)
    fresh = evaluate(state, PinIndex.of(memory.store), target)
    if args.json:
        data = state.to_dict()
        data["freshness"], data["checked_against"] = state.freshness(target)
        data["reading"] = fresh.to_dict()
        data["current"] = is_current(state, fresh)
        _emit(args, data)
        return 0
    record = state.record
    out = [
        f"finding    {state.finding_id}",
        f"title      {record['title']}{_public_default(state)}",
        f"claim      {record['claim']}",
        f"class      {record['evidence_class']}  scope {record['scope']}"
        f"  component {record['component']}",
        f"status     {_status_text(state)}" + (f" by {state.status_by}" if state.status_by else ""),
        f"freshness  {describe(fresh)}",
        f"current    {'yes' if is_current(state, fresh) else 'no'}"
        + (f"  (newest pin {fresh.newest_pin[:12]})" if fresh.newest_pin else ""),
        f"integrity  {state.integrity['verdict'] if state.integrity else 'NOT_CHECKED'}",
        f"workflow   {state.workflow}",
        f"created    {state.created_time} by {state.created_by.name} ({state.origin})",
    ]
    resolved = iter(state.resolved_citations())
    for number, source in enumerate(record["sources"], 1):
        if source["kind"] == "code":
            shown = next(resolved)
            sha = (shown.get("span_sha256") or "not resolved")[:16]
            later = "  (resolved by a later verify)" if shown.get("resolved_by") else ""
            out.append(f"source {number}   {shown['path']}:{shown['start_line']}-"
                       f"{shown['end_line']} @ {shown['commit'][:12]}  anchor "
                       f"{shown['anchor']!r}  span {sha}{later}")
        else:
            out.append(f"source {number}   web {source['url']} ({source.get('published') or 'date unknown'})")
    for dependency in record.get("dependencies", []):
        if dependency["kind"] == "finding":
            out.append(f"depends    finding {dependency['finding_id']}")
        else:
            cited = dependency["citation"]
            out.append(f"depends    span {cited['path']}:{cited['start_line']}-{cited['end_line']}"
                       f" @ {cited['commit'][:12]}")
    if record.get("negative"):
        negative = record["negative"]
        out.append(f"negative   {negative['query']!r} in {negative['path_glob'] or 'all paths'} "
                   f"@ {negative['commit'][:12]}: {negative['total']} hits over "
                   f"{negative['coverage']['paths_searched']} searched paths")
    for revision in state.provenance[1:]:
        where = (revision["commit"][:12] if revision.get("commit")
                 else "the cited commits (resolved)")
        out.append(f"provenance r{revision['revision']} @ {where}: " + ", ".join(
            f"{c['path']}:{c['start_line']}-{c['end_line']}" for c in revision["citations"]))
    for commit, check in sorted(state.targets.items()):
        out.append(f"checked    {commit[:12]}: {check['freshness']}"
                   + (f" - {check['reasons'][0]}" if check["reasons"] else ""))
    for item in state.queue_items():
        out.append(f"queue      P{item['priority']} {item['trigger']}"
                   + (f" @ {item['scope'][:12]}" if item["scope"] else ""))
    if state.superseded_by:
        out.append(f"superseded by {state.superseded_by}")
    if state.retraction:
        out.append(f"retracted  {state.retraction['reason']}")
    for entry in state.history:
        out.append(f"event {entry['seq']:>5} {entry['type']:<9} {entry['time']} "
                   f"{entry['actor']['name']} ({entry['actor']['role']})  {entry['event'][:16]}")
    _cli._write("\n".join(out) + "\n")
    return 0


def _cmd_queue(args: argparse.Namespace) -> int:
    view = _memory(args).view()
    items = view.queue(include_imported=args.include_imported)
    if args.json:
        _emit(args, {"head": view.head, "total": len(items), "items": items})
        return 0
    lines = [f"review queue {len(items)}"]
    for item in items:
        where = f" @ {item['scope'][:12]}" if item["scope"] and item["scope"] != "cited" else ""
        lines.append(f"P{item['priority']} {item['trigger']:<24} {item['finding_id']}{where}"
                     + (f"\n     {item['summary']}" if item.get("summary") else ""))
    _cli._write("\n".join(lines) + "\n")
    return 0


# -- verify / reanchor ----------------------------------------------------------------------------


def _checker(args: argparse.Namespace) -> Actor:
    return Actor(args.actor, args.role or "verifier") if args.actor else VERIFIER_ACTOR


def _cmd_verify(args: argparse.Namespace) -> int:
    results = _memory(args).verify(args.finding_ids or None, actor=_checker(args),
                                   label=args.label, expect_head=args.expect_head)
    if args.json:
        _emit(args, {"results": results})
        return 0
    lines = [f"verified {sum(1 for r in results if 'skipped' not in r)} finding(s) at their "
             "cited commits (integrity only; statuses unchanged)"]
    recheck: set[str] = set()
    for result in results:
        if "skipped" in result:
            lines.append(f"{result['finding_id']:<24} skipped: {result['skipped']}")
            continue
        resolved = (f"  ({result['resolved_citations']} citation(s) resolved now)"
                    if result.get("resolved_citations") else "")
        lines.append(f"{result['finding_id']:<24} {result['freshness']:<13} spans "
                     f"{result['verdict'] or '-'}{resolved}"
                     + (f"  {result['reasons'][0]}" if result["reasons"] else ""))
        recheck.update(result.get("recheck_targets") or ())
    if recheck:
        lines.append("next       citations were resolved after re-anchoring checks at "
                     + ", ".join(sorted(c[:12] for c in recheck))
                     + "; those checks no longer count: run txray findings reanchor "
                     "<target> (or --latest) again")
    _cli._write("\n".join(lines) + "\n")
    return 0


def _latest_target(memory: FindingsMemory) -> str:
    """The newest pinned commit of the store's upstream (``reanchor --latest``)."""
    pins = PinIndex.of(memory.store)
    newest = pins.latest()
    if newest is None:
        if not pins.by_commit:
            raise InvalidInput("the store has no pinned commits; run: txray pin <commit>")
        raise InvalidInput("the store holds pins of several upstreams; give the target commit")
    return newest.commit


def _cmd_reanchor(args: argparse.Namespace) -> int:
    memory = _memory(args)
    if (args.target is None) == (not args.latest):
        raise InvalidInput("give a TARGET commit or --latest, not both")
    target = args.target if args.target is not None else _latest_target(memory)
    report = memory.reanchor(target, args.finding_ids or None, actor=_checker(args),
                             expect_head=args.expect_head)
    stale = [r for r in report["results"] if r["freshness"] != "CURRENT"]
    if args.json:
        _emit(args, report)
    else:
        lines = [f"target     {report['target']}",
                 f"findings   {report['findings']}: {_counts(report['freshness'])}"]
        for result in report["results"]:
            moved = "  (new provenance revision)" if result["provenance"] else ""
            lines.append(f"{result['finding_id']:<24} {result['freshness']:<13} "
                         f"{_counts(result['outcomes'])}{moved}"
                         + (f"  {result['reasons'][0]}" if result["reasons"] else ""))
        _cli._write("\n".join(lines) + "\n")
    return 1 if args.strict and stale else 0


# -- review / supersede / retract -------------------------------------------------------------------


def _cmd_review(args: argparse.Namespace) -> int:
    state = _memory(args).review(args.finding_id, _actor(args, "reviewer"), args.status,
                                 args.rationale, objections=args.objection,
                                 target=args.target, expect_head=args.expect_head)
    if args.json:
        _emit(args, state.to_dict())
        return 0
    open_items = state.queue_items()
    lines = [f"reviewed   {state.finding_id}: {_status_text(state)} by {state.status_by}",
             f"freshness  {_fresh_text(state)} (unchanged by review)"]
    if open_items:
        scopes = sorted({item["scope"][:12] for item in open_items if item["scope"]})
        lines.append(f"queue      {len(open_items)} item(s) stay open at "
                     f"{', '.join(scopes) or 'other scopes'} (not assessed by this review; "
                     "review --target C, supersede or retract to close them)")
    _cli._write("\n".join(lines) + "\n")
    return 0


def _cmd_supersede(args: argparse.Namespace) -> int:
    spec = _read_spec(args.file) if args.file else None
    state = _memory(args).supersede(args.finding_id, _actor(args, "author"), by=args.by,
                                    spec=spec, rationale=args.rationale,
                                    expect_head=args.expect_head)
    if args.json:
        _emit(args, state.to_dict())
        return 0
    _cli._write(f"superseded {state.finding_id} by {state.superseded_by}\n")
    return 0


def _cmd_retract(args: argparse.Namespace) -> int:
    state = _memory(args).retract(args.finding_id, _actor(args, "author"), args.reason,
                                  expect_head=args.expect_head)
    if args.json:
        _emit(args, state.to_dict())
        return 0
    _cli._write(f"retracted  {state.finding_id}\n")
    return 0


# -- export / verify-log -------------------------------------------------------------------------------


def _cmd_export(args: argparse.Namespace) -> int:
    data = _memory(args).export(args.format, label=args.label)
    if args.out:
        text = json.dumps(data, indent=2, sort_keys=args.format != "research",
                          ensure_ascii=True) + "\n"
        Path(args.out).write_text(text, "utf-8")
        summary = {"format": args.format, "out": args.out,
                   "findings": len(data.get("findings", data.get("events", [])))}
        if args.json:
            _emit(args, summary)
        else:
            _cli._write(f"exported   {summary['findings']} {args.format} record(s) to {args.out}\n")
        return 0
    if args.json:
        _emit(args, data)
    else:
        _cli._write(json.dumps(data, indent=2, sort_keys=args.format != "research",
                               ensure_ascii=True) + "\n")
    return 0


def _cmd_verify_log(args: argparse.Namespace) -> int:
    ledger = _ledger(args)
    repair = ledger.repair_head() if args.repair_head else None
    report = ledger.verify(expect_head=args.expect_head)
    if args.json:
        _cli._emit_json(args.command, {
            "outcome": "ok" if report.ok else "failed",
            "data": {"ledger": str(ledger.directory), "repair": repair, **report.to_dict()},
            "warnings": [],
        })
    else:
        lines = [f"ledger     {ledger.directory}",
                 f"events     {len(report.events)}",
                 f"head       {report.last_hash}"]
        if repair is not None:
            previous = repair["previous"]
            was = (f"seq {previous.get('seq')}" if isinstance(previous, dict)
                   else "missing or malformed")
            lines.append(f"repair     {'HEAD rewritten' if repair['repaired'] else 'nothing to do'}"
                         f": {repair['reason']} (HEAD was {was}, now seq {repair['head']['seq']})")
        if report.ok:
            lines.append("chain      ok: every hash, link and the HEAD record verify")
        for problem in report.problems:
            where = f"line {problem['line']}: " if problem["line"] else ""
            lines.append(f"problem    {problem['code']}: {where}{problem['message']}")
        _cli._write("\n".join(lines) + "\n")
    return 0 if report.ok else 1


# -- registration ----------------------------------------------------------------------------------


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    """Add the ``findings`` command group to the ``txray`` parser."""
    base = argparse.ArgumentParser(add_help=False, parents=[common])
    base.add_argument(
        "--ledger", metavar="DIR", default=None, type=_cli.fs_path,
        help="findings ledger directory (default: $TXRAY_FINDINGS or <store>/findings)",
    )
    writing = argparse.ArgumentParser(add_help=False)
    writing.add_argument("--expect-head", metavar="HASH",
                         help="refuse to write unless the ledger head is this event hash")
    who = argparse.ArgumentParser(add_help=False)
    who.add_argument("--actor", metavar="NAME", required=True,
                     help="declared actor name (a role or handle, not a personal name)")
    who.add_argument("--role", choices=ROLES, default=None,
                     help="actor role (default: author; importer for import; reviewer for review)")
    checker = argparse.ArgumentParser(add_help=False)
    checker.add_argument("--actor", metavar="NAME", default=None,
                         help=f"actor recorded on verify events (default {VERIFIER_ACTOR.name})")
    checker.add_argument("--role", choices=ROLES, default=None, help=argparse.SUPPRESS)

    group = commands.add_parser(
        "findings",
        help="findings memory: add, import, verify, reanchor and review cited claims",
        description=(
            "Reviewed claims whose code citations are commit-pinned byte spans, kept in an "
            "append-only SHA-256-chained event log. Evidence status, freshness and workflow "
            "are separate fields; verification changes freshness only, and only a reviewer "
            "who did not author a finding can set its status."
        ),
    )
    sub = group.add_subparsers(dest="findings_command", required=True, metavar="SUBCOMMAND")

    def parser(name: str, parents: list[argparse.ArgumentParser], **kwargs: Any) -> Any:
        created = sub.add_parser(name, parents=[base, *parents], **kwargs)
        created.set_defaults(command=f"findings {name}")
        return created

    add = parser(
        "add", [who, writing],
        help="add a draft finding (the write gate reads every cited span)",
        description=(
            "Add a draft finding, from --file or from the individual flags (not both). The "
            "write gate reads every cited span from its git blob and refuses an anchor that "
            "is MISSING from the cited lines. The finding starts as a draft; only a different "
            "reviewer can set a reviewed status."
        ),
    )
    add.add_argument("--file", metavar="SPEC.json", type=_cli.fs_path, help="finding specification as JSON (see docs/findings-memory.md); replaces the individual flags")
    add.add_argument("--id", help="finding id (default: F- plus a content hash)")
    add.add_argument("--title", help="short title of the finding")
    add.add_argument("--claim", help="the claim, one falsifiable sentence")
    add.add_argument("--component", help="the part of the algorithm the claim concerns")
    add.add_argument("--evidence-class", choices=EVIDENCE_CLASSES, help="evidence class of the claim")
    add.add_argument("--status", choices=STATUSES, help="proposed status (basis 'proposed')")
    add.add_argument("--scope", choices=SCOPES,
                     help="what the claim covers (PARAM_DEFAULT findings always use public_default)")
    add.add_argument("--cite", nargs=4, action="append", metavar=("COMMIT", "PATH", "LINES", "ANCHOR"),
                     help="code citation: pinned commit, repository path, line range A-B and an anchor text inside it; repeatable")
    add.add_argument("--depends-on", action="append", metavar="ID", help="finding dependency; repeatable")
    add.set_defaults(handler=_cmd_add)

    imp = parser(
        "import", [who, writing],
        help="import a research-import file as imported findings",
        description=(
            "Import the findings of a research-import file (schemas/research-import.schema.json). "
            "Each finding keeps the report's status as 'reported', enters the workflow "
            "'imported' and has its citations read and verified; nothing is promoted to "
            "reviewed."
        ),
    )
    imp.add_argument("file", type=_cli.fs_path, help="JSON, YAML (strict subset) or Markdown with one findings block")
    imp.add_argument("--source-label", required=True, metavar="LABEL",
                     help="namespace of the imported ids, e.g. P1b (ids become LABEL:ID)")
    imp.set_defaults(handler=_cmd_import)

    lst = parser(
        "list", [],
        help="list findings with status, freshness and workflow",
        description=(
            "List the active findings with status, freshness relative to the newest pin and "
            "workflow. Filters combine; --workflow also shows findings in that workflow when "
            "they are superseded or retracted."
        ),
    )
    lst.add_argument("--workflow", choices=WORKFLOWS, help="only findings in this workflow state")
    lst.add_argument("--status", choices=STATUSES, help="only findings with this evidence status")
    lst.add_argument("--freshness", choices=READINGS,
                     help="freshness reading (NOT_APPLICABLE: external evidence only)")
    lst.add_argument("--target", metavar="COMMIT", help="freshness against this pinned commit")
    lst.add_argument("--current", action="store_true",
                     help="only findings that are current relative to the newest pin (what "
                          "the MCP tools and the export list by default)")
    lst.add_argument("--label", metavar="LABEL", help="only findings imported with this label")
    lst.add_argument("--all", action="store_true", help="include superseded and retracted findings")
    lst.set_defaults(handler=_cmd_list)

    show = parser(
        "show", [],
        help="show one finding with its evidence, checks and history",
        description=(
            "Show one finding: claim, status, freshness, integrity, citations, dependencies, "
            "negative search, provenance, recorded checks, queue items and the event history."
        ),
    )
    show.add_argument("finding_id", metavar="ID", help="finding id (an imported one is LABEL:ID)")
    show.add_argument("--target", metavar="COMMIT", help="freshness against this pinned commit")
    show.set_defaults(handler=_cmd_show)

    verify = parser(
        "verify", [checker, writing],
        help="check cited spans at their own commits (INTACT / CHANGED / MISSING)",
        description=(
            "Re-read every cited span at its own commit and record the integrity verdict. "
            "This changes freshness only, never a finding's status, and writes one verify "
            "event per finding."
        ),
    )
    verify.add_argument("finding_ids", nargs="*", metavar="ID", help="default: all active findings")
    verify.add_argument("--label", metavar="LABEL",
                        help="only the active findings imported with this source label")
    verify.set_defaults(handler=_cmd_verify)

    reanchor = parser(
        "reanchor", [checker, writing],
        help="re-anchor findings on a newer pinned commit (freshness only)",
        description=(
            "Locate every cited span of the findings at a newer pinned commit (identical, "
            "unchanged, relocated, changed, ambiguous, missing or unusable) and record the "
            "resulting freshness. Statuses stay unchanged. Give a TARGET commit or --latest."
        ),
    )
    reanchor.add_argument("target", metavar="TARGET", nargs="?", default=None,
                          help="pinned target commit (or --latest)")
    reanchor.add_argument("finding_ids", nargs="*", metavar="ID", help="default: all active findings")
    reanchor.add_argument("--latest", action="store_true",
                          help="re-anchor on the newest pinned commit of the upstream "
                               "(by committer time)")
    reanchor.add_argument("--strict", action="store_true",
                          help="exit 1 when any finding is not CURRENT at the target")
    reanchor.set_defaults(handler=_cmd_reanchor)

    review = parser(
        "review", [who, writing],
        help="record a reviewer's assessed status (no self-approval)",
        description=(
            "Record the status a reviewer assessed after reading the cited code. Needs the "
            "role reviewer or maintainer, and an actor other than the finding's author. It "
            "is the only event that gives a finding a reviewed status; freshness is unchanged."
        ),
    )
    review.add_argument("finding_id", metavar="ID", help="finding id (an imported one is LABEL:ID)")
    review.add_argument("--status", choices=STATUSES, required=True, help="the status the reviewer assessed")
    review.add_argument("--rationale", required=True, help="why the source entails (or fails to entail) the claim")
    review.add_argument("--objection", action="append", metavar="TEXT", help="an objection or caveat to record; repeatable")
    review.add_argument("--target", metavar="COMMIT",
                        help="the review also assessed the finding at this re-anchoring "
                             "target (its queue items close too; default: cited commits only)")
    review.set_defaults(handler=_cmd_review)

    supersede = parser(
        "supersede", [who, writing],
        help="supersede a finding",
        description=(
            "Mark an active finding as superseded, either by another active finding (--by) "
            "or by a new finding created from a specification (--file); give exactly one."
        ),
    )
    supersede.add_argument("finding_id", metavar="ID", help="the active finding to supersede (an imported one is LABEL:ID)")
    supersede.add_argument("--by", metavar="ID", help="id of an existing active finding that replaces it")
    supersede.add_argument("--file", metavar="SPEC.json", type=_cli.fs_path, help="specification (JSON) of a new finding that replaces it")
    supersede.add_argument("--rationale", required=True, help="why the finding is superseded")
    supersede.set_defaults(handler=_cmd_supersede)

    retract = parser(
        "retract", [who, writing],
        help="retract a finding with a reason",
        description=(
            "Retract an active finding. The event stays in the log; the finding leaves the "
            "active view and is listed only with --all."
        ),
    )
    retract.add_argument("finding_id", metavar="ID", help="the active finding to retract (an imported one is LABEL:ID)")
    retract.add_argument("--reason", required=True, help="why the finding is withdrawn")
    retract.set_defaults(handler=_cmd_retract)

    queue = parser(
        "queue", [],
        help="list open review-queue items by priority",
        description=(
            "List the open review-queue items, most urgent first: findings awaiting review "
            "and findings whose cited evidence changed, moved, went missing or became "
            "ambiguous at a checked commit."
        ),
    )
    queue.add_argument("--include-imported", action="store_true",
                       help="also list imported findings awaiting review")
    queue.set_defaults(handler=_cmd_queue)

    export = parser(
        "export", [],
        help="export the active view, the raw events or research data",
        description=(
            "Export JSON: the active findings view (findings), the findings in the "
            "research-import shape (research) or the raw event log (events)."
        ),
    )
    export.add_argument("--format", choices=("findings", "research", "events"), default="findings",
                        help="what to export (default: findings)")
    export.add_argument("--label", metavar="LABEL", help="source label for --format research")
    export.add_argument("--out", metavar="FILE", type=_cli.fs_path, help="write to FILE instead of stdout")
    export.set_defaults(handler=_cmd_export)

    log = parser(
        "verify-log", [],
        help="verify the event log's hash chain and HEAD",
        description=(
            "Verify every event hash and link of the append-only log and the HEAD record. "
            "Exit status 1 when any problem is found."
        ),
    )
    log.add_argument("--expect-head", metavar="HASH",
                     help="also require this last-event hash (an independently kept copy)")
    log.add_argument("--repair-head", action="store_true",
                     help="rewrite HEAD from the log when the log verifies and HEAD fell "
                          "behind it (recovery after an interrupted write); refused for any "
                          "other problem")
    log.set_defaults(handler=_cmd_verify_log)
