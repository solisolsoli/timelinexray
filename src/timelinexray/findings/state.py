"""The active view of the findings memory: a projection of the event log.

Nothing here is stored. :func:`project` replays the verified events in order and derives,
for every finding, its record, workflow state, evidence status (with its basis), latest
integrity check, latest check per target commit, provenance revisions, open review-queue
items and history. The same events always give the same view.

Review-queue items are opened by ``verify`` events (triggers) and by drafts awaiting review;
a later check of the same scope (the cited commits, or one target commit) replaces the
items of that scope. A ``supersede`` or ``retract`` event closes all open items of the
finding. A ``review`` closes only the scopes it assessed (``payload["closed_scopes"]``:
the cited commits, plus the target commit the reviewer named); items of other targets -
a ``changed_span`` found by re-anchoring on a newer commit - stay open, so a finding that
is STALE at a newer pin never leaves the queue because its historical evidence was
confirmed. A review event without ``closed_scopes`` (written by the released 0.7.0 or
earlier) closes every scope, as it did when it was written, so the same events always give
the same view.

Replay re-enforces what the write path refused (defence in depth against a consistently
rewritten log): no self-approval, and a review confirming ``SUPPORTED`` or ``PARTIAL`` for a
finding with code citations (or ``NOT_FOUND`` backed by a negative search) must name the
finding's latest integrity check, which must be ``CURRENT`` at that point of the log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import IntegrityError
from ..verify import CURRENT, NOT_CHECKED
from .ledger import Event
from .model import (
    ACTIVE_WORKFLOWS,
    DRAFT,
    IMPORTED,
    RETRACTED,
    REVIEWED,
    SUPERSEDED,
    Actor,
    code_citations,
)

OWN_SCOPE = "cited"

#: Priority of review-queue triggers (1 is most urgent).
TRIGGER_PRIORITY = {
    "changed_span": 1,
    "negative_now_found": 1,
    "citation_changed": 1,
    "dependency_changed": 2,
    "ambiguous_span": 2,
    "missing_span": 2,
    "citation_unusable": 2,
    "negative_unverifiable": 3,
    "awaiting_review": 3,
    "awaiting_review_imported": 4,
}
ALLOWED_ACTIONS = (
    "review: record an assessed status (only a reviewer who did not author the finding)",
    "supersede: replace it with a revised finding that cites the target commit",
    "retract: withdraw it with a reason",
    "leave unresolved: the item stays open and the finding keeps its freshness",
)


@dataclass
class FindingState:
    finding_id: str
    record: dict[str, Any]
    origin: str
    created_by: Actor
    created_event: str
    created_time: str
    workflow: str
    status: str
    status_basis: str
    status_by: str | None = None
    integrity: dict[str, Any] | None = None
    targets: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_target: str | None = None
    provenance: list[dict[str, Any]] = field(default_factory=list)
    open_items: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    superseded_by: str | None = None
    supersedes: list[str] = field(default_factory=list)
    retraction: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    #: Sequence number of the latest integrity check that resolved citations (a provenance
    #: revision at the cited commit); target checks older than it are not shown as checks.
    resolution_seq: int = 0

    @property
    def active(self) -> bool:
        return self.workflow in ACTIVE_WORKFLOWS

    @property
    def has_code(self) -> bool:
        return bool(code_citations(self.record))

    @property
    def checkable(self) -> bool:
        """Whether any evidence of the finding can be re-checked against code: a code
        citation, a dependency or a recorded negative search. A finding with only external
        (web) sources has nothing to verify or re-anchor."""
        record = self.record
        return bool(code_citations(record) or record.get("dependencies")
                    or record.get("negative"))

    def resolved_citations(self) -> list[dict[str, Any]]:
        """The record's code citations, with every citation that could not be read when the
        record was written (``resolution`` set) replaced by its resolved form from the
        latest integrity check that read it (a provenance revision at the cited commits).
        The record itself is never changed; this is the view consumers display."""
        sources = code_citations(self.record)
        revisions = [rev for rev in self.provenance if rev.get("mode") == "integrity"]
        if not revisions or not any(source.get("resolution") for source in sources):
            return sources
        latest = revisions[-1]["citations"]
        resolved = []
        for index, source in enumerate(sources):
            better = latest[index] if index < len(latest) else None
            if source.get("resolution") and better and better.get("span_sha256") \
                    and not better.get("resolution"):
                resolved.append({**better, "resolved_by": revisions[-1]["event"]})
            else:
                resolved.append(source)
        return resolved

    def target_check(self, target: str) -> dict[str, Any] | None:
        """The recorded check against ``target``, unless the citations were resolved after
        it (then it was made from unresolved citations and no longer counts)."""
        check = self.targets.get(target)
        if check is None or check.get("seq", 0) < self.resolution_seq:
            return None
        return check

    def check_for(self, target: str | None) -> dict[str, Any] | None:
        """The latest check against ``target`` (a full commit), or the display check."""
        if target is not None:
            return self.target_check(target)
        if self.last_target is not None:
            check = self.target_check(self.last_target)
            if check is not None:
                return check
        return self.integrity

    def freshness(self, target: str | None = None) -> tuple[str, str | None]:
        """``(freshness, checked_against)``; ``checked_against`` is a commit or ``cited``."""
        check = self.check_for(target)
        if check is None:
            return NOT_CHECKED, target
        return check["freshness"], check.get("target") or OWN_SCOPE

    def queue_items(self) -> list[dict[str, Any]]:
        items = [item for scope in sorted(self.open_items) for item in self.open_items[scope]]
        if self.active and self.workflow in (DRAFT, IMPORTED) and not self.reviews:
            trigger = "awaiting_review" if self.workflow == DRAFT else "awaiting_review_imported"
            items.append(_item(self, trigger, None, None, {
                "workflow": self.workflow, "status": self.status, "basis": self.status_basis,
            }, f"{self.workflow} finding with status {self.status} ({self.status_basis}) "
               "awaits review"))
        return items

    def summary(self, target: str | None = None) -> dict[str, Any]:
        freshness, against = self.freshness(target)
        record = self.record
        return {
            "finding_id": self.finding_id,
            "title": record.get("title"),
            "component": record.get("component"),
            "evidence_class": record.get("evidence_class"),
            "scope": record.get("scope"),
            "status": self.status,
            "status_basis": self.status_basis,
            "status_by": self.status_by,
            "freshness": freshness,
            "checked_against": against,
            "integrity": self.integrity["verdict"] if self.integrity else NOT_CHECKED,
            "workflow": self.workflow,
            "open_items": sum(len(items) for items in self.open_items.values()),
            "source_label": (record.get("origin") or {}).get("source_label"),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "record": self.record,
            "origin": self.origin,
            "created_by": self.created_by.to_dict(),
            "created_event": self.created_event,
            "created_time": self.created_time,
            "integrity_check": self.integrity,
            "targets": {key: self.targets[key] for key in sorted(self.targets)},
            "last_target": self.last_target,
            "provenance": self.provenance,
            "queue": self.queue_items(),
            "reviews": self.reviews,
            "superseded_by": self.superseded_by,
            "supersedes": self.supersedes,
            "retraction": self.retraction,
            "history": self.history,
        }


def _item(state: FindingState, trigger: str, scope: str | None, event: str | None,
          detail: dict[str, Any] | None, summary: str | None = None) -> dict[str, Any]:
    return {
        "finding_id": state.finding_id,
        "trigger": trigger,
        "summary": summary,
        "priority": TRIGGER_PRIORITY.get(trigger, 3),
        "scope": scope,
        "event": event,
        "title": state.record.get("title"),
        "status": state.status,
        "status_basis": state.status_basis,
        "workflow": state.workflow,
        "detail": detail,
        "allowed_actions": list(ALLOWED_ACTIONS),
    }


@dataclass
class View:
    """All findings in log order plus the ledger head."""

    findings: dict[str, FindingState]
    order: list[str]
    head: str
    events: int

    def get(self, finding_id: str) -> FindingState | None:
        return self.findings.get(finding_id)

    def states(self) -> list[FindingState]:
        return [self.findings[finding_id] for finding_id in self.order]

    def queue(self, *, include_imported: bool = False) -> list[dict[str, Any]]:
        items = []
        for state in self.states():
            for item in state.queue_items():
                if item["trigger"] == "awaiting_review_imported" and not include_imported:
                    continue
                items.append(item)
        items.sort(key=lambda item: (item["priority"], item["finding_id"], item["trigger"],
                                     item["scope"] or ""))
        return items


def _new_state(event: Event, record: dict[str, Any], workflow: str, basis: str) -> FindingState:
    state = FindingState(
        finding_id=record["finding_id"],
        record=record,
        origin=event.type,
        created_by=event.actor,
        created_event=event.hash,
        created_time=event.time,
        workflow=workflow,
        status=record["status"],
        status_basis=basis,
    )
    citations = code_citations(record)
    if citations:
        state.provenance.append({"revision": 1, "event": event.hash, "commit": None,
                                 "citations": citations})
    return state


def _close_items(state: FindingState, event: Event, how: str,
                 scopes: list[str] | None = None) -> list[dict[str, Any]]:
    """Close the open items of ``scopes`` (every scope when ``None``)."""
    chosen = [scope for scope in sorted(state.open_items) if scopes is None or scope in scopes]
    closed = [dict(item, closed_by=event.hash, closed_how=how)
              for scope in chosen for item in state.open_items[scope]]
    for scope in chosen:
        state.open_items.pop(scope, None)
    return closed


def _review_scopes(payload: dict[str, Any]) -> list[str] | None:
    scopes = payload.get("closed_scopes")
    if scopes is None:
        return None  # a review written by the released 0.7.0 or earlier closed every scope
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise IntegrityError("review event: closed_scopes must be a list of scopes")
    return scopes


def project(events: list[Event]) -> View:
    """Replay verified events into the active view (raises on an inconsistent log)."""
    findings: dict[str, FindingState] = {}
    order: list[str] = []

    def fail(event: Event, message: str) -> IntegrityError:
        return IntegrityError(f"event {event.seq} ({event.type} {event.finding_id}): {message}")

    for event in events:
        payload = event.payload
        state = findings.get(event.finding_id)
        if event.type in ("create", "import"):
            record = payload.get("record")
            if state is not None or not isinstance(record, dict) \
                    or record.get("finding_id") != event.finding_id:
                raise fail(event, "duplicate or malformed finding record")
            new = _new_state(event, record, DRAFT if event.type == "create" else IMPORTED,
                             "proposed" if event.type == "create" else "reported")
            findings[event.finding_id] = new
            order.append(event.finding_id)
            state = new
        elif state is None:
            raise fail(event, "the finding does not exist")
        elif event.type == "verify":
            scope = payload.get("target") or OWN_SCOPE
            check = {
                "event": event.hash,
                "seq": event.seq,
                "time": event.time,
                "mode": payload.get("mode"),
                "target": payload.get("target"),
                "freshness": payload.get("freshness"),
                "verdict": payload.get("verdict"),
                "reasons": payload.get("reasons", []),
            }
            if payload.get("mode") == "integrity":
                state.integrity = check
            else:
                state.targets[scope] = check
                state.last_target = scope
            if payload.get("provenance"):
                state.provenance.append({
                    "revision": len(state.provenance) + 1,
                    "event": event.hash,
                    "seq": event.seq,
                    "mode": payload.get("mode"),
                    "commit": payload["provenance"]["commit"],
                    "citations": payload["provenance"]["citations"],
                })
                if payload.get("mode") == "integrity":
                    # the citations were resolved now; checks made from the unresolved
                    # ones no longer describe this finding at their targets
                    state.resolution_seq = event.seq
            items = [_item(state, trigger["trigger"], scope, event.hash, trigger.get("detail"),
                           trigger.get("summary"))
                     for trigger in payload.get("triggers", [])]
            if state.active:
                if items:
                    state.open_items[scope] = items
                else:
                    state.open_items.pop(scope, None)
        elif event.type == "review":
            if not state.active:
                raise fail(event, "review of an inactive finding")
            if event.actor.key == state.created_by.key:
                raise fail(event, "self-approval")
            status = payload.get("status")
            confirms = (status in ("SUPPORTED", "PARTIAL") and state.has_code) or (
                status == "NOT_FOUND" and bool(state.record.get("negative")))
            if confirms:
                integrity = state.integrity
                if integrity is None or integrity["event"] != payload.get("integrity_event") \
                        or integrity["freshness"] != CURRENT:
                    raise fail(event, f"review confirms {status} without naming a CURRENT "
                                      "integrity check of the cited evidence")
            closed = _close_items(state, event, "review", _review_scopes(payload))
            state.status = payload["status"]
            state.status_basis = "reviewed"
            state.status_by = event.actor.name
            state.workflow = REVIEWED
            state.reviews.append({
                "event": event.hash,
                "time": event.time,
                "actor": event.actor.to_dict(),
                "status": payload["status"],
                "rationale": payload.get("rationale"),
                "objections": payload.get("objections", []),
                "integrity_event": payload.get("integrity_event"),
                "assessed_target": payload.get("assessed_target"),
                "closed_scopes": _review_scopes(payload),
                "closed_items": len(closed),
            })
        elif event.type == "supersede":
            if not state.active:
                raise fail(event, "supersede of an inactive finding")
            by = payload.get("by")
            record = payload.get("record")
            if record is not None:
                if by in findings or record.get("finding_id") != by:
                    raise fail(event, "malformed superseding record")
                new = _new_state(event, record, DRAFT, "proposed")
                findings[by] = new
                order.append(by)
            successor = findings.get(by)
            if successor is None or not successor.active or by == state.finding_id:
                raise fail(event, "the superseding finding is missing or inactive")
            _close_items(state, event, "supersede")
            state.workflow = SUPERSEDED
            state.superseded_by = by
            successor.supersedes.append(state.finding_id)
            successor.history.append({"seq": event.seq, "type": "supersede", "time": event.time,
                                      "actor": event.actor.to_dict(), "event": event.hash,
                                      "note": f"supersedes {state.finding_id}"})
        elif event.type == "retract":
            if not state.active:
                raise fail(event, "retraction of an inactive finding")
            _close_items(state, event, "retract")
            state.workflow = RETRACTED
            state.retraction = {"event": event.hash, "time": event.time,
                                "actor": event.actor.to_dict(), "reason": payload.get("reason")}
        state.history.append({"seq": event.seq, "type": event.type, "time": event.time,
                              "actor": event.actor.to_dict(), "event": event.hash})
    head = events[-1].hash if events else "0" * 64
    return View(findings, order, head, len(events))


__all__ = ["FindingState", "View", "project", "OWN_SCOPE", "TRIGGER_PRIORITY", "SUPERSEDED",
           "RETRACTED", "REVIEWED"]
