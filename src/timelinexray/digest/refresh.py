"""Keeping the findings ledger fresh from ``txray update`` (``--reanchor``, ``--export``).

``txray update --reanchor`` re-anchors every active finding on the head it just pinned
(``verify`` events through :class:`timelinexray.findings.FindingsMemory`: exactly what
``txray findings reanchor <head>`` writes), before the digest is built, so the digest's
affected findings carry each finding's freshness *at the head* and no finding stays "not
current" merely because nobody looked. On a head that was already pinned (no new commit,
or a quarantined head observed again), only findings without a recorded check at it are
re-anchored (findings recorded or imported since), so a daily schedule appends nothing to
a ledger that is already fresh.

The review queue is compared before and after: the items that appeared are what a
maintainer must look at (a changed span, a negative search with hits, a dependency that
went stale), and ``txray update`` exits 1 when there are any. ``--export DIR`` then
refreshes the Context Layer notes with the rules of ``txray export context-layer`` (the
export package is imported only then).

The digest's affected-findings provider stays read-only; the ledger is written only
through the findings service, with its write gate, hash chain and declared actor.
Freshness changes here; evidence statuses never do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..findings import ACTIVE_WORKFLOWS, VERIFIER_ACTOR, Actor, FindingsMemory
from ..verify import NOT_CHECKED

EVERY_ACTIVE = "every active finding"
UNCHECKED_ONLY = "findings without a check at the head"


def queue_key(item: dict[str, Any]) -> tuple[str, str, str]:
    """What identifies a review item across two readings of the queue."""
    return (str(item["finding_id"]), str(item["trigger"]), str(item.get("scope") or ""))


def _brief(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item.get(key) for key in ("finding_id", "trigger", "priority", "scope", "title")}


@dataclass
class LedgerRefresh:
    """What one re-anchoring from ``txray update`` did (status file and CLI output)."""

    target: str
    selection: str
    findings: int
    freshness: dict[str, int]
    queue_before: int
    queue_after: int
    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    ledger_head: str = ""
    ledger_events: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "selection": self.selection,
            "findings": self.findings,
            "freshness": dict(self.freshness),
            "review_queue": {
                "before": self.queue_before,
                "after": self.queue_after,
                "added": [_brief(item) for item in self.added],
                "removed": [_brief(item) for item in self.removed],
            },
            "ledger": {"head": self.ledger_head, "events": self.ledger_events},
        }


def refresh_ledger(
    memory: FindingsMemory, target: str, *, only_unchecked: bool = False,
    actor: Actor = VERIFIER_ACTOR,
) -> LedgerRefresh:
    """Re-anchor on ``target`` (a pinned commit) and report the review-queue delta.

    With ``only_unchecked`` the selection is the active findings that have no recorded
    check at ``target``; otherwise every active finding (findings without code evidence
    are skipped by the service either way).
    """
    full = memory.full_commit(target)
    view = memory.view()
    before = view.queue()
    selection = EVERY_ACTIVE
    ids: list[str] | None = None
    if only_unchecked:
        selection = UNCHECKED_ONLY
        ids = [state.finding_id for state in view.states()
               if state.workflow in ACTIVE_WORKFLOWS and state.freshness(full)[0] == NOT_CHECKED]
    if ids == []:  # an empty selection would mean "all" to the service
        report: dict[str, Any] = {"target": full, "findings": 0, "freshness": {}, "results": []}
    else:
        report = memory.reanchor(full, ids, actor=actor)
    after_view = memory.view()
    after = after_view.queue()
    before_keys = {queue_key(item) for item in before}
    after_keys = {queue_key(item) for item in after}
    return LedgerRefresh(
        target=full,
        selection=selection,
        findings=int(report["findings"]),
        freshness={str(key): int(value) for key, value in report["freshness"].items()},
        queue_before=len(before),
        queue_after=len(after),
        added=[item for item in after if queue_key(item) not in before_keys],
        removed=[item for item in before if queue_key(item) not in after_keys],
        ledger_head=after_view.head,
        ledger_events=after_view.events,
    )


def refresh_export(out: Path, *, store_root: Path, ledger: Path) -> dict[str, Any]:
    """``txray export context-layer --out OUT`` after a refresh; counts only, no paths."""
    from ..export import export_context_layer  # loaded only when --export is used

    data = export_context_layer(out, store_root=store_root, ledger=ledger).to_dict()
    files = data["files"]
    return {
        "findings": data["findings"],
        "files": {name: len(files[name]) for name in ("written", "unchanged", "removed")},
    }


__all__ = ["EVERY_ACTIVE", "UNCHECKED_ONLY", "LedgerRefresh", "queue_key", "refresh_export",
           "refresh_ledger"]
