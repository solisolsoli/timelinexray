"""The stale-review worklist: what to re-read, in which order, after re-anchoring.

``txray findings reanchor`` records, for every finding, where each cited span is at a target
commit (identical, moved, changed, ambiguous, missing or unusable) and, for a changed span,
the region a line diff aligns it with. This module turns those recorded checks into a
prioritised worklist for a human reviewer. It reads the ledger and the store's pins only:
it never writes a ledger event, never approves a finding and never moves a finding's
evidence. Freshness is not truth; only ``txray findings review`` by a different reviewer
sets a status, and only ``supersede`` (by a named author, then reviewed) gives a finding new
citations.

For every active finding with checkable evidence that is not ``CURRENT`` at the target the
worklist has one entry with:

* the finding's freshness at the target, its evidence class, status and workflow;
* every non-current citation: the old span (commit, path, lines, span SHA-256, anchor), the
  outcome, and the candidate at the target when the tool knows one (for ``changed``: the
  line-diff-aligned span with its SHA-256 and anchor verdict; for ``ambiguous``: every
  occurrence; for ``missing``: the searched scope; for ``unusable``: the reason);
* non-current dependencies and a negative search that now has hits;
* the exact commands: read both spans with ``txray show``, then one of the three decisions
  (supersede with the aligned spans and have a different reviewer review the successor;
  review the finding at the target as it is; retract it).

Entries are ordered by *area* (``parameter``: a ``PARAM_DEFAULT`` finding; ``scoring``: a
cited path or the component matches the digest's scoring-name rule
:func:`timelinexray.diff.rules.is_scoring_name`; ``other``), then by the priority of the
review trigger, then by id. The order is a mechanical heuristic for where to look first,
not a judgement of importance.

Two causes are batched instead of listed per finding: findings never re-anchored at the
target (one ``txray findings reanchor`` command) and findings whose citations name commits
the store has not pinned (one ``txray findings verify --pin-cited`` command, which pins them
through the guarded fetch, re-verifies and re-anchors).

:func:`draft_successor` builds the specification of a successor finding whose citations are
the current or aligned spans at the target, each with its expected ``span_sha256`` so the
write gate re-reads and re-checks it; the claim is copied unchanged with a limitation saying
so. A draft is a file for an author to read and edit; it becomes a finding only through
``txray findings supersede --file`` and a review by somebody else.
"""

from __future__ import annotations

import os
import re
import shlex
from collections import Counter
from pathlib import Path
from typing import Any

from ..diff.rules import is_scoring_name
from ..errors import InvalidInput
from ..span import CONFIRMING
from ..verify import CURRENT, NOT_CHECKED, STALE, UNVERIFIABLE
from .freshness import PinIndex, evaluate
from .model import MAX_ID, code_citations
from .state import FindingState, View, project

SCHEMA = "timelinexray/stale-review/v1"
AREAS = ("parameter", "scoring", "other")
AREA_RANK = {area: rank for rank, area in enumerate(AREAS)}
#: Priority of a non-current citation outcome or part (1 is most urgent), as the queue.
OUTCOME_PRIORITY = {"changed": 1, "negative_now_found": 1, "dependency": 2, "ambiguous": 2,
                    "missing": 2, "unusable": 2, "negative_unverifiable": 3}
UNPINNED = "commit_not_pinned"
SHORT = 12
ORDER_NOTE = ("parameter findings first, then findings on scoring paths, then the rest; "
              "within each, the review-trigger priority (a mechanical order, not a judgement)")
NO_APPROVAL_NOTE = ("nothing here approves a finding or changes its evidence: freshness is "
                    "not truth, only a review by a different reviewer sets a status, and only "
                    "a reviewed successor gives a finding new citations")
DRAFT_NOTE = ("Drafted by txray findings stale from {old} at {target}: the citations were "
              "moved to the spans the tool located or aligned at the target, and the claim "
              "was copied unchanged. Read every span and edit the claim before superseding.")
#: A member that is not a specification key, so ``supersede --file`` refuses an unread draft.
DRAFT_KEY = "remove_after_reading"
DRAFT_INSTRUCTION = ("Read every cited span at the target (txray show ...), correct the claim, "
                     "title and status to what the new spans say, then delete this member; "
                     "txray findings supersede refuses the file while it is present.")


def _cmd(*parts: str) -> str:
    return " ".join(shlex.quote(str(part)) for part in ("txray", *parts))


def area_of(state: FindingState) -> str:
    """``parameter``, ``scoring`` or ``other`` (see the module documentation)."""
    record = state.record
    if record.get("evidence_class") == "PARAM_DEFAULT":
        return "parameter"
    paths = [source.get("path") for source in state.resolved_citations()]
    paths += [dependency["citation"].get("path") for dependency in record.get("dependencies") or []
              if dependency.get("kind") == "span"]
    if any(is_scoring_name(path) for path in paths) or is_scoring_name(record.get("component")):
        return "scoring"
    return "other"


def unpinned_commits(state: FindingState, pins: PinIndex) -> list[str]:
    """Commits the finding's citations name that could not be read because the store had not
    pinned them (an imported citation that is still unresolved) and that ``pins`` still does
    not hold. A citation of a commit pinned since then stays unresolved only when it is not
    usable there (for example its anchor is missing); that is not a pinning problem."""
    return sorted({source["commit"] for source in state.resolved_citations()
                   if source.get("resolution") == UNPINNED and isinstance(source.get("commit"), str)
                   and pins.resolve(source["commit"]) is None})


def _span(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not data:
        return None
    return {key: data.get(key) for key in ("commit", "path", "start_line", "end_line",
                                           "span_sha256", "anchor")}


def _citation_entry(item: dict[str, Any]) -> dict[str, Any]:
    """One non-current citation from a re-anchoring check (a ``Relocation`` dict)."""
    proposed = item.get("proposed")
    candidate = None
    if proposed:
        candidate = {"commit": item.get("target"), "path": proposed.get("path"),
                     "start_line": proposed.get("start_line"), "end_line": proposed.get("end_line"),
                     "span_sha256": proposed.get("span_sha256"),
                     "anchor_verdict": proposed.get("anchor_verdict"),
                     "method": proposed.get("method")}
    search = item.get("search") or {}
    return {
        "index": item.get("index"),
        "outcome": item.get("outcome"),
        "freshness": item.get("freshness"),
        "reason": item.get("reason"),
        "old": _span(item.get("citation")),
        "candidate": candidate,
        "candidates": list(item.get("candidates") or [])[:20],
        "anchor_at_target": item.get("anchor_at_target"),
        "search_complete": search.get("complete") if search else None,
    }


def _integrity_entry(item: dict[str, Any], source: dict[str, Any] | None) -> dict[str, Any]:
    """One unusable citation from an integrity check at the finding's own commit."""
    return {
        "index": item.get("index"),
        "outcome": "unusable",
        "freshness": UNVERIFIABLE if item.get("verdict") != "CHANGED" else STALE,
        "reason": (f"{item.get('verdict')}: {item.get('reason') or 'anchor ' + str(item.get('anchor_verdict'))}"),
        "old": _span(source),
        "candidate": None,
        "candidates": [],
        "anchor_at_target": None,
        "search_complete": None,
    }


def _usable(item: dict[str, Any]) -> bool:
    return item.get("verdict") == "INTACT" and item.get("anchor_verdict") in CONFIRMING


class Worklist:
    """The stale-review worklist of one ledger at one target commit (read-only)."""

    def __init__(self, view: View, payloads: dict[str, dict[str, Any]], pins: PinIndex,
                 target: str, *, store_args: list[str] | None = None,
                 ledger_args: list[str] | None = None) -> None:
        self.view = view
        self.payloads = payloads
        self.pins = pins
        self.target = target
        self.store_args = list(store_args or [])
        self.ledger_args = list(ledger_args or [])
        self.entries: list[dict[str, Any]] = []
        self.not_checked: list[str] = []
        self.unpinned: dict[str, list[str]] = {}
        self.totals: Counter[str] = Counter()
        self.checkable = 0
        self._build()

    # -- commands ---------------------------------------------------------------------------

    def _findings(self, *parts: str) -> str:
        return _cmd("findings", *parts, *self.store_args, *self.ledger_args)

    def _show(self, span: dict[str, Any]) -> str:
        return _cmd("show", str(span["commit"])[:SHORT], span["path"],
                    "--lines", f"{span['start_line']}-{span['end_line']}",
                    "--anchor", span["anchor"], *self.store_args)

    def successor_id(self, finding_id: str) -> str:
        """The id the drafted successor gets: ``<id>.<target 7>`` (or the plain hash id when
        that would be too long)."""
        candidate = f"{finding_id}.{self.target[:7]}"
        return candidate if len(candidate) <= MAX_ID else f"successor.{self.target[:7]}"

    @staticmethod
    def spec_name(successor: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", successor) + ".json"

    # -- building -----------------------------------------------------------------------------

    def _build(self) -> None:
        target = self.target
        for state in self.view.states():
            if not state.active or not state.checkable:
                continue
            self.checkable += 1
            fresh = evaluate(state, self.pins, target)
            self.totals[fresh.value] += 1
            missing = unpinned_commits(state, self.pins)
            if missing:
                self.unpinned[state.finding_id] = missing
            if fresh.value == CURRENT:
                continue
            if fresh.value == NOT_CHECKED:
                self.not_checked.append(state.finding_id)
                continue
            entry = self._entry(state, fresh.value, fresh.event, missing)
            if missing and not entry["dependencies"] and not entry["negative"] and all(
                    c["outcome"] == "unusable" and UNPINNED in (c["reason"] or "")
                    for c in entry["citations"]):
                continue  # only unpinned commits: the batch step resolves it first
            self.entries.append(entry)
        self.entries.sort(key=lambda entry: (AREA_RANK[entry["area"]], entry["priority"],
                                             entry["finding_id"]))
        for rank, entry in enumerate(self.entries, 1):
            entry["rank"] = rank

    def _entry(self, state: FindingState, freshness: str, event: str | None,
               missing: list[str]) -> dict[str, Any]:
        payload = self.payloads.get(event or "", {})
        sources = state.resolved_citations()
        citations: list[dict[str, Any]] = []
        current = 0
        located: list[dict[str, Any] | None] = []
        if payload.get("mode") == "reanchor":
            for item in payload.get("citations") or []:
                if item.get("freshness") == CURRENT:
                    current += 1
                    located.append(_span(item.get("current")))
                    continue
                entry = _citation_entry(item)
                citations.append(entry)
                cand = entry["candidate"]
                located.append({**cand, "anchor": (entry["old"] or {}).get("anchor")}
                               if cand and cand.get("anchor_verdict") in CONFIRMING
                               and cand.get("span_sha256") and (entry["old"] or {}).get("anchor")
                               else None)
        else:  # the target is the finding's own commit: its integrity check
            for item, source in zip(payload.get("citations") or [], sources):
                if _usable(item):
                    current += 1
                    located.append(_span(source))
                else:
                    citations.append(_integrity_entry(item, source))
                    located.append(None)
        dependencies = [dep for dep in payload.get("dependencies") or []
                        if dep.get("freshness") != CURRENT]
        negative = payload.get("negative")
        if negative and negative.get("freshness") == CURRENT:
            negative = None
        parts = [OUTCOME_PRIORITY.get(c["outcome"], 2) for c in citations]
        parts += [OUTCOME_PRIORITY["dependency"]] * len(dependencies)
        if negative:
            parts.append(OUTCOME_PRIORITY["negative_now_found" if negative.get("total")
                                          else "negative_unverifiable"])
        priority = min(parts) if parts else 3
        record = state.record
        entry: dict[str, Any] = {
            "finding_id": state.finding_id,
            "title": record.get("title"),
            "component": record.get("component"),
            "evidence_class": record.get("evidence_class"),
            "status": state.status,
            "status_basis": state.status_basis,
            "workflow": state.workflow,
            "freshness": freshness,
            "area": area_of(state),
            "priority": priority,
            "check_event": event,
            "citations": citations,
            "current_citations": current,
            "dependencies": dependencies,
            "negative": negative,
            "unpinned_commits": missing,
        }
        blocker = self._draft_blocker(state, citations, located, dependencies, negative, missing)
        entry["draft"] = {"possible": blocker is None, "why_not": blocker,
                          "successor_id": self.successor_id(state.finding_id) if blocker is None
                          else None}
        entry["_located"] = located
        entry["commands"] = self._commands(state, entry)
        return entry

    @staticmethod
    def _draft_blocker(state: FindingState, citations: list[dict[str, Any]],
                       located: list[dict[str, Any] | None], dependencies: list[dict[str, Any]],
                       negative: dict[str, Any] | None, missing: list[str]) -> str | None:
        if missing:
            return "a cited commit is not pinned; run txray findings verify --pin-cited first"
        if not code_citations(state.record):
            return "the finding has no code citation to move"
        if len(located) != len(code_citations(state.record)):
            return "the recorded check does not list every citation; re-anchor again"
        if any(span is None for span in located):
            outcomes = sorted({c["outcome"] for c in citations
                               if c["outcome"] != "changed" or not c.get("candidate")})
            return ("no single span at the target for every citation ("
                    + ", ".join(outcomes or ["aligned span without a confirming anchor"]) + ")")
        if any(dep.get("kind") == "span" for dep in dependencies) or any(
                dep.get("kind") == "span" for dep in state.record.get("dependencies") or []):
            return "a span dependency must be re-cited by hand"
        if negative is not None or state.record.get("negative"):
            return "a negative search must be re-run by hand"
        return None

    def _commands(self, state: FindingState, entry: dict[str, Any]) -> dict[str, Any]:
        finding_id = state.finding_id
        target = self.target[:SHORT]
        read: list[str] = []
        for citation in entry["citations"]:
            if citation["old"] and citation["old"].get("anchor"):
                read.append(self._show(citation["old"]))
            if citation["candidate"]:
                read.append(self._show({**citation["candidate"],
                                        "anchor": citation["old"]["anchor"]}))
            for occurrence in citation["candidates"][:5]:
                read.append(self._show({"commit": self.target, "anchor": citation["old"]["anchor"],
                                        **occurrence}))
        decide: dict[str, Any] = {
            "show": self._findings("show", finding_id, "--target", target),
            "assess_at_target": self._findings(
                "review", finding_id, "--actor", "REVIEWER", "--role", "reviewer",
                "--status", "STATUS", "--target", target, "--rationale", "RATIONALE"),
            "retract": self._findings("retract", finding_id, "--actor", "AUTHOR",
                                      "--reason", "REASON"),
        }
        if entry["draft"]["possible"]:
            successor = entry["draft"]["successor_id"]
            decide["supersede"] = self._findings(
                "supersede", finding_id, "--actor", "AUTHOR", "--file",
                os.path.join("SPEC_DIR", self.spec_name(successor)), "--rationale", "RATIONALE")
            decide["verify_successor"] = self._findings("verify", successor)
            decide["review_successor"] = self._findings(
                "review", successor, "--actor", "REVIEWER", "--role", "reviewer",
                "--status", "STATUS", "--rationale", "RATIONALE")
        else:
            decide["supersede"] = None
            decide["verify_successor"] = None
            decide["review_successor"] = None
        return {"read": read, "decide": decide}

    # -- output -------------------------------------------------------------------------------

    def batch(self) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        if self.unpinned:
            commits = sorted({c for values in self.unpinned.values() for c in values})
            steps.append({
                "why": "citations name commits this store has not pinned",
                "findings": sorted(self.unpinned),
                "commits": commits,
                "command": _cmd("findings", "verify", "--pin-cited", *self.store_args,
                                *self.ledger_args),
                "effect": ("pins them (fetching through the guarded upstream fetch only when the "
                           "mirror lacks a commit), re-verifies those findings and re-anchors them "
                           "where earlier checks were made from the unresolved citations; "
                           "freshness only"),
            })
        if self.not_checked:
            steps.append({
                "why": "no recorded check at the target",
                "findings": sorted(self.not_checked),
                "command": self._findings("reanchor", self.target[:SHORT]),
                "effect": "re-anchors them on the target; freshness only",
            })
        return steps

    def counts(self) -> dict[str, Any]:
        return {
            "checkable": self.checkable,
            "freshness": dict(sorted(self.totals.items())),
            "not_current": len(self.entries),
            "by_area": dict(sorted(Counter(e["area"] for e in self.entries).items())),
            "by_outcome": dict(sorted(Counter(c["outcome"] for e in self.entries
                                              for c in e["citations"]).items())),
            "unpinned_findings": len(self.unpinned),
            "unpinned_commits": len({c for values in self.unpinned.values() for c in values}),
            "not_checked": len(self.not_checked),
            "drafts_possible": sum(1 for e in self.entries if e["draft"]["possible"]),
        }

    def to_dict(self, limit: int | None = None) -> dict[str, Any]:
        entries = self.entries if not limit else self.entries[:limit]
        newest = self.pins.latest()
        return {
            "schema": SCHEMA,
            "target": self.target,
            "newest_pin": newest.commit if newest else None,
            "ledger_head": self.view.head,
            "order": ORDER_NOTE,
            "note": NO_APPROVAL_NOTE,
            "counts": self.counts(),
            "batch": self.batch(),
            "shown": len(entries),
            "entries": [{k: v for k, v in entry.items() if not k.startswith("_")}
                        for entry in entries],
        }

    def summary(self, top: int = 10) -> dict[str, Any]:
        """Counts, batch steps, the first entries in one line each and the next command."""
        return {
            "target": self.target,
            "counts": self.counts(),
            "batch": self.batch(),
            "first": [{key: entry[key] for key in ("rank", "finding_id", "area", "freshness",
                                                   "priority")}
                      | {"outcomes": sorted({c["outcome"] for c in entry["citations"]})}
                      for entry in self.entries[:top]],
            "next": (_cmd("findings", "stale", "--target", self.target[:SHORT], *self.store_args,
                          *self.ledger_args) if self.entries else None),
        }

    # -- drafts -------------------------------------------------------------------------------

    def draft_successor(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """The successor specification of an entry (``None`` when no draft is possible)."""
        if not entry["draft"]["possible"]:
            return None
        state = self.view.get(entry["finding_id"])
        assert state is not None
        record = state.record
        citations = []
        for span in entry["_located"]:
            citations.append({"commit": self.target, "path": span["path"],
                              "lines": f"{span['start_line']}-{span['end_line']}",
                              "anchor": span["anchor"], "span_sha256": span["span_sha256"]})
        webs = [{key: source.get(key) for key in ("url", "publisher", "published", "retrieved",
                                                   "quote")}
                for source in record.get("sources") or [] if source.get("kind") == "web"]
        depends = [{"finding": dep["finding_id"]} for dep in record.get("dependencies") or []
                   if dep.get("kind") == "finding"]
        limitations = list(record.get("limitations") or [])
        limitations.append(DRAFT_NOTE.format(old=state.finding_id, target=self.target[:SHORT]))
        spec: dict[str, Any] = {
            # not a specification key: the write gate refuses the file until an author has
            # read the spans, edited the claim and removed this member
            DRAFT_KEY: DRAFT_INSTRUCTION,
            "finding_id": entry["draft"]["successor_id"],
            "title": record.get("title"),
            "claim": record.get("claim"),
            "component": record.get("component"),
            "evidence_class": record.get("evidence_class"),
            "status": record.get("status"),
            "scope": record.get("scope"),
            "citations": citations,
            "limitations": limitations,
        }
        if webs:
            spec["web_sources"] = webs
        if depends:
            spec["depends_on"] = depends
        return spec


def worklist(memory: Any, target: str | None = None, *, store_args: list[str] | None = None,
             ledger_args: list[str] | None = None) -> Worklist:
    """The worklist of ``memory`` (a :class:`FindingsMemory`) at ``target`` (a pinned commit,
    default the newest pin of the store's upstream). Reads only."""
    pins = PinIndex.of(memory.store)
    if target is None:
        newest = pins.latest()
        if newest is None:
            raise InvalidInput("the store has no single newest pin; give the target commit")
        full = newest.commit
    else:
        full = memory.full_commit(target)
    events = memory.ledger.read_events()
    view = project(events)
    payloads = {event.hash: event.payload for event in events if event.type == "verify"}
    return Worklist(view, payloads, pins, full, store_args=store_args, ledger_args=ledger_args)


def worktree_of(path: Path) -> Path | None:
    """The nearest ancestor of ``path`` with a ``.git`` entry (a git working tree), if any."""
    current = Path(os.path.abspath(path))
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


__all__ = ["AREAS", "DRAFT_KEY", "SCHEMA", "Worklist", "area_of", "unpinned_commits",
           "worklist", "worktree_of"]
