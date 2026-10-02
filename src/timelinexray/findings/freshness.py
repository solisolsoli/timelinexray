"""Freshness of a finding relative to what the snapshot store knows now.

The ledger records checks (``verify`` events): integrity at the cited commits and
re-anchoring at target commits. A consumer, however, asks "may I present this finding as
current?", and that question is relative to the *newest pinned commit of the upstream*,
not to the commit of the finding's latest check. This module combines a finding's recorded
checks with the store's pins into one reading:

* ``value`` is one of the four ledger freshness values (``CURRENT``, ``STALE``,
  ``UNVERIFIABLE``, ``NOT_CHECKED``) at the ``commit`` named beside it, or the derived
  reading ``NOT_APPLICABLE`` for a finding whose evidence cannot be re-checked against the
  repository at all (only external web sources): nothing of it can go stale, the reading is
  never written to the ledger, and the recorded retrieval date is the only date it has.
* Without a commit of the caller's choice, ``commit`` is the newest pinned commit the
  finding was re-anchored on (by committer time), else its cited commit (integrity check).
  ``newer_pins`` lists the pins of the same upstream with a later committer time that have
  no recorded check. A finding with unchecked newer pins is **not** current: its evidence
  may have changed there and nobody looked (``txray findings reanchor --latest``).
* With a commit of the caller's choice (a historical answer), the reading is the recorded
  check at that commit and ``newer_pins`` is empty: the answer names its commit.

``current`` (:func:`is_current`) is therefore: the finding is active and either
``CURRENT`` with no unchecked newer pin, or ``NOT_APPLICABLE``. This is the rule every
consumer (MCP tools, export, CLI listings) applies; the agent contract rests on it.

Time-bounded external sources: when a finding's text (note, limitations, claim,
implication or a quote) says "recheck after", "effective", "valid until" or "expires" with
an ISO date, ``recheck_after`` carries the latest such date so a reader can tell whether
the recorded statement may have been superseded. It is a hint parsed from recorded text,
not a judgement.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..snapshot.store import PinRecord
from ..verify import CURRENT, FRESHNESS, NOT_CHECKED, UNVERIFIABLE
from .model import code_citations
from .state import OWN_SCOPE, FindingState

NOT_APPLICABLE = "NOT_APPLICABLE"
#: Every value a consumer may show: the ledger's four plus the derived reading.
READINGS = (*FRESHNESS, NOT_APPLICABLE)
#: Reason key under which listings count a CURRENT finding left out for unchecked pins.
NEWER_PIN_UNCHECKED = "NEWER_PIN_UNCHECKED"
NEWER_PINS_SHOWN = 20

EXTERNAL_NOTE = (
    "external evidence only (no code citation, dependency or negative search): nothing can "
    "be re-verified against the repository, so freshness does not apply; the recorded "
    "retrieval date is the only date this finding has"
)

_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
_RECHECK = re.compile(
    r"(?i)\b(?:re-?check(?:ed)?\s+(?:after|on|from)|effective(?:\s+(?:from|on|date))?|"
    r"valid\s+until|expires?(?:\s+on)?|in\s+force\s+(?:from|until))\s*:?\s*"
    r"(\d{4}-\d{2}-\d{2})"
)


def _sort_key(pin: PinRecord) -> tuple[str, str]:
    return (pin.committer_time, pin.commit)


class PinIndex:
    """The store's pins, by commit and by upstream (newest committer time first)."""

    def __init__(self, pins: Iterable[PinRecord]) -> None:
        self.by_commit: dict[str, PinRecord] = {}
        self.by_upstream: dict[str, list[PinRecord]] = {}
        for pin in pins:
            self.by_commit[pin.commit] = pin
            self.by_upstream.setdefault(pin.upstream_url, []).append(pin)
        for records in self.by_upstream.values():
            records.sort(key=_sort_key, reverse=True)

    @classmethod
    def of(cls, store: Any) -> "PinIndex":
        return cls(store.list_pins())

    def resolve(self, commit: object) -> PinRecord | None:
        """The pin of a full commit id or a unique prefix of one."""
        if not isinstance(commit, str) or len(commit) < 7:
            return None
        if commit in self.by_commit:
            return self.by_commit[commit]
        matches = [pin for full, pin in self.by_commit.items() if full.startswith(commit)]
        return matches[0] if len(matches) == 1 else None

    def upstream_of(self, commits: Iterable[object]) -> str | None:
        for commit in commits:
            pin = self.resolve(commit)
            if pin is not None:
                return pin.upstream_url
        if len(self.by_upstream) == 1:
            return next(iter(self.by_upstream))
        return None

    def newest(self, upstream: str | None) -> PinRecord | None:
        if upstream is None or upstream not in self.by_upstream:
            return None
        return self.by_upstream[upstream][0]

    def newer_than(self, commit: str | None, upstream: str | None) -> list[PinRecord]:
        """Pins of ``upstream`` with a committer time strictly later than ``commit``'s
        (newest first). Equal committer times give no order, so neither counts as newer."""
        if upstream is None or upstream not in self.by_upstream:
            return []
        reference = self.resolve(commit) if commit else None
        if reference is None:
            return []
        return [pin for pin in self.by_upstream[upstream]
                if pin.committer_time > reference.committer_time]

    def latest(self, upstream: str | None = None) -> PinRecord | None:
        """The newest pin of ``upstream``, or of the store when it holds one upstream."""
        if upstream is None:
            if len(self.by_upstream) != 1:
                return None
            upstream = next(iter(self.by_upstream))
        return self.newest(upstream)


@dataclass
class Freshness:
    """One reading (see the module documentation)."""

    value: str
    commit: str | None
    check: str | None
    event: str | None
    checkable: bool
    newest_pin: str | None = None
    newer_pins: list[str] = field(default_factory=list)
    note: str | None = None
    retrieved: str | None = None
    recheck_after: str | None = None

    @property
    def current(self) -> bool:
        """Whether the evidence is current relative to the newest pin (workflow aside)."""
        if self.value == NOT_APPLICABLE:
            return True
        return self.value == CURRENT and not self.newer_pins

    @property
    def reason(self) -> str:
        """Why the reading is not current: the freshness value, or the unchecked pins."""
        if self.value == CURRENT and self.newer_pins:
            return NEWER_PIN_UNCHECKED
        return self.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "commit": self.commit,
            "check": self.check,
            "event": self.event,
            "checkable": self.checkable,
            "newest_pin": self.newest_pin,
            "newer_pins": list(self.newer_pins[:NEWER_PINS_SHOWN]),
            "note": self.note,
            "retrieved": self.retrieved,
            "recheck_after": self.recheck_after,
        }


def cited_commits(record: dict[str, Any],
                  citations: list[dict[str, Any]] | None = None) -> list[str]:
    """Every commit the finding's code evidence names (citations, span dependencies and
    the negative search), sorted. ``citations`` replaces the record's own code citations
    (for example :meth:`FindingState.resolved_citations`, whose commits are full ids once
    an imported citation was resolved)."""
    sources = code_citations(record) if citations is None else citations
    commits = {source["commit"] for source in sources
               if isinstance(source.get("commit"), str)}
    for dependency in record.get("dependencies") or []:
        if dependency.get("kind") == "span":
            commits.add(dependency["citation"]["commit"])
    negative = record.get("negative")
    if negative and isinstance(negative.get("commit"), str):
        commits.add(negative["commit"])
    return sorted(commits)


def recheck_after(record: dict[str, Any]) -> str | None:
    """The latest ISO date a recorded text names as the moment to recheck (or ``None``)."""
    texts = [record.get("claim"), *(record.get("limitations") or [])]
    attributes = record.get("attributes") or {}
    texts += [attributes.get("note"), attributes.get("implication")]
    texts += [source.get("quote") for source in record.get("sources") or []
              if source.get("kind") == "web"]
    dates = [match.group(1) for text in texts if isinstance(text, str)
             for match in _RECHECK.finditer(text)]
    return max(dates) if dates else None


def latest_retrieval(record: dict[str, Any]) -> str | None:
    dates = [source.get("retrieved") for source in record.get("sources") or []
             if source.get("kind") == "web" and isinstance(source.get("retrieved"), str)]
    return max(dates) if dates else None


def _cited(state: FindingState) -> list[str]:
    return cited_commits(state.record, state.resolved_citations())


def _integrity_commit(state: FindingState) -> str | None:
    cited = [c for c in _cited(state) if _FULL_COMMIT.fullmatch(c)]
    return cited[0] if len(cited) == 1 else None


def evaluate(state: FindingState, pins: PinIndex, commit: str | None = None) -> Freshness:
    """The reading of ``state`` at ``commit`` (a full pinned commit), or relative to the
    newest pin when ``commit`` is ``None``."""
    record = state.record
    if not state.checkable:
        upstream = pins.upstream_of(())
        newest = pins.newest(upstream)
        return Freshness(NOT_APPLICABLE, None, None, None, False,
                         newest_pin=newest.commit if newest else None, note=EXTERNAL_NOTE,
                         retrieved=latest_retrieval(record), recheck_after=recheck_after(record))
    cited = _cited(state)
    if commit is not None:
        check = state.target_check(commit)
        if check is None and state.integrity is not None and cited == [commit]:
            check = state.integrity
        upstream = pins.upstream_of([commit, *cited])
        newest = pins.newest(upstream)
        if check is None:
            return Freshness(NOT_CHECKED, commit, None, None, True,
                             newest_pin=newest.commit if newest else None)
        kind = "integrity" if check is state.integrity else "reanchor"
        value = check.get("freshness")
        return Freshness(value if value in FRESHNESS else UNVERIFIABLE, commit, kind,
                         check.get("event"), True, newest_pin=newest.commit if newest else None)
    checked = {target: state.target_check(target) for target in state.targets}
    checked = {target: check for target, check in checked.items() if check is not None}
    display: str | None
    if checked:
        def order(target: str) -> tuple[str, int]:
            # newest committer time first; among equal times (no order) the latest check
            pin = pins.resolve(target)
            return (pin.committer_time if pin else "", checked[target].get("seq", 0))

        display = max(checked, key=order)
        check = checked[display]
        kind = "reanchor"
    elif state.integrity is not None:
        display = _integrity_commit(state)
        check = state.integrity
        kind = "integrity"
    else:
        display, check, kind = None, None, None
    reference = display or (max(cited, key=lambda c: (
        pins.resolve(c).committer_time if pins.resolve(c) else "", c)) if cited else None)
    upstream = pins.upstream_of([reference, *cited] if reference else cited)
    newest = pins.newest(upstream)
    newer = [pin.commit for pin in pins.newer_than(reference, upstream)]
    newer = [c for c in newer if c not in checked]
    if check is None:
        return Freshness(NOT_CHECKED, display, None, None, True,
                         newest_pin=newest.commit if newest else None, newer_pins=newer)
    value = check.get("freshness")
    return Freshness(value if value in FRESHNESS else UNVERIFIABLE, display, kind,
                     check.get("event"), True, newest_pin=newest.commit if newest else None,
                     newer_pins=newer)


def is_current(state: FindingState, fresh: Freshness) -> bool:
    """The one rule: active, and current relative to the newest pin (or not applicable)."""
    return state.active and fresh.current


def describe(fresh: Freshness) -> str:
    """One line for humans: value, commit and what keeps it from being current."""
    if fresh.value == NOT_APPLICABLE:
        parts = ["NOT_APPLICABLE (external evidence, not re-verifiable)"]
        if fresh.retrieved:
            parts.append(f"retrieved {fresh.retrieved}")
        if fresh.recheck_after:
            parts.append(f"recheck after {fresh.recheck_after}")
        return "; ".join(parts)
    where = fresh.commit[:12] if fresh.commit else OWN_SCOPE
    text = f"{fresh.value}@{where}"
    if fresh.newer_pins:
        text += (f"; {len(fresh.newer_pins)} newer pin(s) unchecked (newest "
                 f"{fresh.newer_pins[0][:12]}): not current until re-anchored")
    return text


__all__ = [
    "EXTERNAL_NOTE",
    "NEWER_PINS_SHOWN",
    "NEWER_PIN_UNCHECKED",
    "NOT_APPLICABLE",
    "READINGS",
    "Freshness",
    "PinIndex",
    "cited_commits",
    "describe",
    "evaluate",
    "is_current",
    "latest_retrieval",
    "recheck_after",
]
