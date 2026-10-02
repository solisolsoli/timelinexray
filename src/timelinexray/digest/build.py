"""Build the digest document for a commit range (JSON-ready, deterministic).

The digest of ``old..new`` is computed from diffs only:

1. both endpoints must be pinned; their lineage is checked in the local mirror;
2. when ``old`` is an ancestor of ``new``, every commit of the first-parent chain between
   them is pinned locally (no fetch: the objects are already in the mirror) and each
   consecutive pair is diffed, so intermediate changes and reversions are visible, not
   only the net endpoint diff;
3. the net diff ``old -> new`` is classified; parameter-default and registration changes
   get their own tables, with timelines across the intermediate commits;
4. the changed regions of the net diff are passed to an affected-findings provider.

The document contains no timestamps other than committer times from git objects and no
local paths, so the same inputs give byte-identical output.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from .. import __version__
from ..diff.engine import DiffEngine
from ..errors import NotFound
from ..diff.history import (
    ATTENTION,
    HISTORY_REWRITTEN,
    NOT_ANCESTOR,
    Event,
    Lineage,
    is_ancestor,
)
from ..diff.model import ChangeItem, CommitDiff
from ..diff.rules import (
    CLASS_EVIDENCE,
    CLASS_TITLES,
    CLASSES,
    CLASSIFIER_VERSION,
    PARAMETER_DEFAULT,
    REGISTRATION,
)
from ..diff.symbols import SymbolSource
from ..netguard import DEFAULT_UPSTREAM_URL, Allowlist
from ..snapshot.store import PinRecord, SnapshotStore
from .findings import AffectedFindingsProvider, ChangedRegion, NullFindingsProvider

DIGEST_SCHEMA = "timelinexray/digest/v1"
UPSTREAM_REPOSITORY = "xai-org/x-algorithm"

STATEMENTS = (
    "Generated mechanically from diffs of git objects between pinned commits. Commit "
    "messages are never read or used as evidence.",
    "Every value shown is a public default at the cited commit: a default in the public "
    "repository, not a production value. Live configuration, experiments and request-time "
    "values are unknown.",
    "Nothing has been posted anywhere. This digest is a local file; TimelineXray has no "
    "posting, X API access or engagement automation.",
    "Classification is heuristic and no item has been reviewed. Unknown changes are listed, "
    "not interpreted; reviewed conclusions belong in the findings ledger.",
    "Every citation is a commit, a path, a 1-based line span and the SHA-256 of the exact "
    "span bytes; check it with: txray show <commit> <path> --lines A-B",
    "Independent community analysis of publicly available source code. Not affiliated with "
    "or endorsed by X or xAI.",
)


def upstream_info(url: str) -> dict[str, Any]:
    """How a digest names its upstream: the public URL, never a local mirror location."""
    if url == DEFAULT_UPSTREAM_URL:
        return {"repository": UPSTREAM_REPOSITORY, "url": DEFAULT_UPSTREAM_URL, "note": None}
    return {
        "repository": None,
        "url": None,
        "note": "pinned from a local file:// mirror; its location is not shown",
    }


def _canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")


class DigestBuilder:
    """Builds digests from one snapshot store (see the module documentation)."""

    def __init__(
        self,
        store: SnapshotStore,
        *,
        allowlist: Allowlist | None = None,
        findings: AffectedFindingsProvider | None = None,
        symbols: SymbolSource | None = None,
    ) -> None:
        self.store = store
        self.allowlist = allowlist
        self.findings = findings if findings is not None else NullFindingsProvider()
        self.engine = DiffEngine(store, symbols)

    def _pin_local(self, commit: str, like: PinRecord) -> None:
        """Pin a commit that is already in ``like``'s mirror (never fetches)."""
        repo = self.store.repo_for(like)
        if repo.resolve_commit(commit) != commit:
            raise NotFound(
                f"commit {commit} is not in the local mirror; it cannot be pinned without a fetch"
            )
        allowlist = self.allowlist if self.allowlist is not None else Allowlist.from_env()
        self.store.pin(commit, like.upstream_url, allowlist=allowlist)

    def build(self, old: str, new: str, *, events: Sequence[Event] = ()) -> dict[str, Any]:
        old_pin = self.store.get_pin(old)
        new_pin = self.store.get_pin(new)
        all_events = list(events)
        chain: list[str] | None = None
        side: list[str] = []
        relationship = "same" if old_pin.commit == new_pin.commit else "diverged"
        if old_pin.mirror != new_pin.mirror:
            relationship = "unrelated-mirrors"
            all_events.append(Event(NOT_ANCESTOR, ATTENTION, old_pin.commit,
                                    "the two commits were pinned from different upstreams; "
                                    "only their trees are compared"))
        elif relationship != "same":
            repo = self.store.repo_for(new_pin)
            lineage = Lineage(repo)
            if is_ancestor(repo, old_pin.commit, new_pin.commit):
                relationship = "ancestor"
                chain = lineage.chain(old_pin.commit, new_pin.commit)
                if chain is not None:
                    side = lineage.side_commits(old_pin.commit, chain)
            elif is_ancestor(repo, new_pin.commit, old_pin.commit):
                relationship = "reversed"
                all_events.append(Event(NOT_ANCESTOR, ATTENTION, old_pin.commit,
                                        "the old commit is a descendant of the new commit "
                                        "(arguments reversed?); only the trees are compared"))
            elif not any(event.kind == HISTORY_REWRITTEN for event in all_events):
                all_events.append(Event(NOT_ANCESTOR, ATTENTION, old_pin.commit,
                                        "the old commit is not an ancestor of the new commit "
                                        "(rewritten history or another branch); only the trees "
                                        "are compared and intermediate history is unavailable"))
        if chain is None:
            chain = [old_pin.commit] if relationship == "same" else [old_pin.commit, new_pin.commit]
            history_complete = relationship == "same"
        else:
            history_complete = True
        for commit in chain[1:-1]:
            try:
                self.store.get_pin(commit)
            except NotFound:
                self._pin_local(commit, new_pin)

        net = self.engine.diff(old_pin.commit, new_pin.commit)
        steps: list[CommitDiff] = []
        if relationship == "ancestor":
            if len(chain) == 2:
                steps = [net]
            else:
                steps = [self.engine.diff(a, b) for a, b in zip(chain, chain[1:])]
        return self._document(old_pin, new_pin, net, steps, chain, side, relationship,
                              history_complete, all_events)

    # -- document ----------------------------------------------------------------------

    def _document(
        self,
        old_pin: PinRecord,
        new_pin: PinRecord,
        net: CommitDiff,
        steps: list[CommitDiff],
        chain: list[str],
        side: list[str],
        relationship: str,
        history_complete: bool,
        events: list[Event],
    ) -> dict[str, Any]:
        items = [item.to_dict() for item in net.items]
        across = {name: 0 for name in CLASSES}
        for step in steps:
            for item in step.items:
                across[item.change_class] += 1
        backends = sorted({b for d in [net, *steps] for b in d.symbol_backends})
        parameters = {
            "net": [_parameter_row(item) for item in net.items if item.change_class == PARAMETER_DEFAULT],
            **_parameter_history(steps),
        }
        registrations = {
            "net": [_registration_row(item) for item in net.items if item.change_class == REGISTRATION],
            **_registration_history(steps),
        }
        regions = [
            ChangedRegion(
                item_id=data["id"],
                change_class=item.change_class,
                old_commit=net.old.commit,
                new_commit=net.new.commit,
                old_path=item.old_path,
                new_path=item.new_path,
                old_lines=_lines(item.old.start_line, item.old.end_line, item.old.role),
                new_lines=_lines(item.new.start_line, item.new.end_line, item.new.role),
                old_hunks=_hunks(item, "old"),
                new_hunks=_hunks(item, "new"),
                content_unchanged=_content_unchanged(item),
            )
            for item, data in zip(net.items, items)
        ]
        available = self.findings.available()
        affected = sorted(
            (finding.to_dict() for finding in self.findings.affected_findings(regions)),
            key=_finding_order,
        ) if available else []
        candidates_of = getattr(self.findings, "relocation_candidates", None)
        candidates = sorted(
            (finding.to_dict() for finding in candidates_of()), key=_finding_order,
        ) if available and callable(candidates_of) else []
        coverage_of = getattr(self.findings, "coverage", None)
        findings_coverage = coverage_of() if available and callable(coverage_of) else None
        fingerprint_of = getattr(self.findings, "fingerprint", None)
        findings_inputs = fingerprint_of() if callable(fingerprint_of) else None
        coverage: dict[str, int] = {}
        for diff in [net, *steps]:
            for status, count in diff.parse_coverage().items():
                coverage[status] = coverage.get(status, 0) + count
        inputs = {
            "tool_version": __version__,
            "classifier_version": CLASSIFIER_VERSION,
            "symbol_backends": backends,
            "commits": [
                {"commit": commit, "manifest_sha256": self.store.get_pin(commit).manifest_sha256}
                for commit in sorted(set(chain) | {old_pin.commit, new_pin.commit})
            ],
            "findings_provider": getattr(self.findings, "name", "unknown"),
        }
        if findings_inputs is not None:
            inputs["findings_inputs"] = findings_inputs
        document: dict[str, Any] = {
            "schema": DIGEST_SCHEMA,
            "tool": {
                "name": "timelinexray",
                "version": __version__,
                "classifier_version": CLASSIFIER_VERSION,
                "symbol_backends": backends,
            },
            "statements": list(STATEMENTS),
            "upstream": upstream_info(new_pin.upstream_url),
            "range": {
                "old": net.old.to_dict(),
                "new": net.new.to_dict(),
                "relationship": relationship,
                "first_parent_chain": chain,
                "reached_through_merges": sorted(side),
                "commits_in_range": max(len(chain) - 1, 0) + len(side),
                "history_complete": history_complete,
            },
            "events": [event.to_dict() for event in events],
            "classes": [
                {"class": name, "title": CLASS_TITLES[name], "evidence": CLASS_EVIDENCE[name]}
                for name in CLASSES
            ],
            "summary": {
                "net": net.counts(),
                "items_by_class_across_steps": across,
                "steps": len(steps),
            },
            "parameters": parameters,
            "registrations": registrations,
            "affected_findings": {
                "provider": getattr(self.findings, "name", "unknown"),
                "available": available,
                "note": None if available else getattr(
                    self.findings, "note", NullFindingsProvider.note),
                "regions": len(regions),
                "coverage": findings_coverage,
                "findings": affected,
                "relocation_candidates": candidates,
            },
            "items": items,
            "files": [change.to_dict() for change in net.files],
            "steps": [
                {
                    "old": step.old.commit,
                    "new": step.new.commit,
                    "committer_time": step.new.committer_time,
                    "files": len(step.files),
                    "items_by_class": {k: v for k, v in step.counts()["items_by_class"].items() if v},
                    "lines_added": step.counts()["lines_added"],
                    "lines_removed": step.counts()["lines_removed"],
                }
                for step in steps
            ],
            "health": {
                "history_complete": history_complete,
                "rename_detection": sorted({d.rename_detection for d in [net, *steps]}),
                "parse_coverage": dict(sorted(coverage.items())),
            },
            "inputs_sha256": hashlib.sha256(_canonical(inputs)).hexdigest(),
        }
        return document


def _hunks(item: ChangeItem, side: str) -> tuple[tuple[int, int], ...]:
    """``(start, count)`` of every hunk of ``item`` on one side (see ``ChangedRegion``)."""
    if side == "old":
        return tuple((hunk.old_start, hunk.old_count) for hunk in item.hunks)
    return tuple((hunk.new_start, hunk.new_count) for hunk in item.hunks)


def _lines(start: int | None, end: int | None, role: str) -> tuple[int, int] | None:
    if start is None or end is None or role != "span":
        return None
    return (start, end)


def _content_unchanged(item: ChangeItem) -> bool:
    """Whether the item's blob is byte-identical on both sides (a move or a mode change)."""
    return (not item.hunks and item.old.blob_oid is not None
            and item.old.blob_oid == item.new.blob_oid)


def _finding_order(row: dict[str, Any]) -> tuple[Any, ...]:
    return (row["finding_id"], row["via"],
            row["citation"]["path"].encode("utf-8", "surrogateescape"),
            row["citation"]["start_line"], row["citation"]["end_line"])


def _parameter_row(item: ChangeItem) -> dict[str, Any]:
    detail = item.detail
    return {
        "name": detail.get("name"),
        "path": item.path,
        "old_path": item.old_path,
        "declaration": detail.get("declaration"),
        "change": item.kind,
        "type": detail.get("type"),
        "flag": detail.get("flag"),
        "old_value": detail.get("old_value"),
        "new_value": detail.get("new_value"),
        "old": item.old.to_dict(),
        "new": item.new.to_dict(),
    }


def _registration_row(item: ChangeItem) -> dict[str, Any]:
    detail = item.detail
    return {
        "list": detail.get("list"),
        "path": item.path,
        "change": item.kind,
        "added": detail.get("added", []),
        "removed": detail.get("removed", []),
        "reordered": detail.get("reordered", []),
        "old": item.old.to_dict(),
        "new": item.new.to_dict(),
    }


def _rename_map(step: CommitDiff) -> dict[str, str]:
    return {c.old_path: c.new_path for c in step.files
            if c.status == "renamed" and c.old_path and c.new_path}


def _parameter_history(steps: list[CommitDiff]) -> dict[str, Any]:
    """Per-parameter value timelines across the steps, and reversions within the range."""
    timelines: dict[tuple[str, str, str, int], list[dict[str, Any]]] = {}
    for step in steps:
        renames = _rename_map(step)
        if renames:
            timelines = {
                (renames.get(path, path), name, declaration, ordinal): points
                for (path, name, declaration, ordinal), points in timelines.items()
            }
        for item in step.items:
            if item.change_class != PARAMETER_DEFAULT:
                continue
            detail = item.detail
            key = (item.path, detail["name"], detail["declaration"], detail["ordinal"])
            points = timelines.setdefault(key, [])
            if not points:
                points.append({
                    "commit": step.old.commit,
                    "committer_time": step.old.committer_time,
                    "event": "start",
                    "value": detail["old_value"],
                    "citation": item.old.to_dict(),
                })
            points.append({
                "commit": step.new.commit,
                "committer_time": step.new.committer_time,
                "event": item.kind,
                "value": detail["new_value"],
                "citation": item.new.to_dict(),
            })
    history = []
    reversions = []
    for (path, name, declaration, ordinal), points in sorted(timelines.items(), key=lambda kv: (
            kv[0][0].encode("utf-8", "surrogateescape"), kv[0][1], kv[0][2], kv[0][3])):
        row = {"path": path, "name": name, "declaration": declaration, "ordinal": ordinal,
               "points": points}
        changed = sum(1 for point in points if point["event"] == "value-changed")
        declared_and_removed = any(p["event"] == "added" for p in points) and any(
            p["event"] == "removed" for p in points)
        if changed or declared_and_removed or len(points) > 2:
            history.append(row)
        values = [point["value"] for point in points]
        returns = _returns(values)
        if returns:
            reversions.append({**row, "returns": returns})
    return {"history": history, "reversions": reversions}


def _returns(values: list[Any]) -> list[dict[str, int]]:
    """Positions where a value comes back to an earlier value after a different one."""
    found = []
    for later in range(2, len(values)):
        for earlier in range(later - 1):
            if values[earlier] == values[later] and any(
                values[between] != values[earlier] for between in range(earlier + 1, later)
            ):
                found.append({"from_point": earlier, "back_at_point": later})
                break
    return found


def _registration_history(steps: list[CommitDiff]) -> dict[str, Any]:
    """Registration changes per step, and entries added then removed (or the reverse)."""
    history = []
    entries: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
    for step in steps:
        renames = _rename_map(step)
        if renames:
            entries = {(renames.get(path, path), label, key): events
                       for (path, label, key), events in entries.items()}
        for item in step.items:
            if item.change_class != REGISTRATION:
                continue
            history.append({"old_commit": step.old.commit, "new_commit": step.new.commit,
                            **_registration_row(item)})
            label = item.detail.get("list") or ""
            for key in item.detail.get("added", []):
                entries.setdefault((item.path, label, key), []).append((step.new.commit, "added"))
            for key in item.detail.get("removed", []):
                entries.setdefault((item.path, label, key), []).append((step.new.commit, "removed"))
    reversions = []
    for (path, label, key), events in sorted(entries.items(), key=lambda kv: (
            kv[0][0].encode("utf-8", "surrogateescape"), kv[0][1], kv[0][2])):
        kinds = [kind for _, kind in events]
        if any(a != b for a, b in zip(kinds, kinds[1:])):
            reversions.append({"path": path, "list": label, "entry": key,
                               "events": [{"commit": c, "event": k} for c, k in events]})
    return {"history": history if len(steps) > 1 else [], "reversions": reversions}
