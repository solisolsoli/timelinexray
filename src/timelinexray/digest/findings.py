"""The read-only interface through which a digest asks which findings a change touches.

A digest passes the changed regions of its net diff to an :class:`AffectedFindingsProvider`
and prints what it returns. Two providers exist:

* :class:`LedgerFindingsProvider` (Milestone 6; what the CLI uses) reads the findings
  ledger of Milestone 3 and reports every *active* finding (workflow draft, imported or
  reviewed) whose code citation or span dependency overlaps a changed region, with its
  evidence status and its recorded freshness as separate fields. A citation made at
  another commit is first placed on the digest's commits by the Milestone 3 re-anchoring
  rules (exact span bytes, found exactly once); a citation that cannot be placed on either
  commit is counted and listed in ``coverage``, never reported as unaffected. Only spans
  that can be affected are placed: the changed regions are indexed by path and side, and
  a span whose path exists at a commit untouched by any region is skipped there (while
  its path exists, re-anchoring looks nowhere else); a span whose path is absent at a
  commit is placed (its bytes may have moved into a changed file). A file that moved with
  byte-identical content (or changed only its mode) is never "changed lines": a span
  cited in it is reported as a *relocation candidate* instead.
* :class:`NullFindingsProvider` is not connected to any ledger and says so.

A provider that is not available says why, and the digest states that affected findings
were *not computed* - an empty section is never presented as "no finding is affected".

Providers are read-only: they read the ledger (after verifying its hash chain, under a
shared lock of an existing lock file, never creating one) and re-read cited spans, but never
append events, change statuses or freshness, or mark anything reviewed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..errors import IntegrityError, InvalidInput, NotFound, Refused, TxrayError
from ..findings import ACTIVE_WORKFLOWS, Ledger, ledger_directory, project
from ..findings.model import code_citations
from ..snapshot.store import SnapshotStore
from ..verify import IDENTICAL, RELOCATED, UNCHANGED, Citation, Verifier


@dataclass(frozen=True, slots=True)
class ChangedRegion:
    """Lines that changed between two commits, on each side (1-based, inclusive).

    ``old_lines``/``new_lines`` are ``None`` when the path does not exist on that side or
    nothing changed there (a pure insertion or deletion); ``item_id`` names the digest
    item the region belongs to. ``old_hunks``/``new_hunks`` are the item's hunks on each
    side as ``(start, count)``: ``count`` lines from ``start`` changed, or, with ``count``
    0, lines of the other side were inserted after line ``start`` (0 = before line 1).
    ``content_unchanged`` marks an item whose blob is byte-identical on both sides (a
    file moved, or only its mode changed): its lines cite the whole file for reference,
    but no byte of them changed, so a span placed there is never "affected".
    """

    item_id: str
    change_class: str
    old_commit: str
    new_commit: str
    old_path: str | None
    new_path: str | None
    old_lines: tuple[int, int] | None
    new_lines: tuple[int, int] | None
    old_hunks: tuple[tuple[int, int], ...] = ()
    new_hunks: tuple[tuple[int, int], ...] = ()
    content_unchanged: bool = False

    def side(self, name: str) -> tuple[str | None, tuple[int, int] | None,
                                       tuple[tuple[int, int], ...]]:
        """``(path, lines, hunks)`` of the ``"old"`` or ``"new"`` side."""
        if name == "old":
            return self.old_path, self.old_lines, self.old_hunks
        return self.new_path, self.new_lines, self.new_hunks

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "class": self.change_class,
            "old_commit": self.old_commit,
            "new_commit": self.new_commit,
            "old_path": self.old_path,
            "new_path": self.new_path,
            "old_lines": list(self.old_lines) if self.old_lines else None,
            "new_lines": list(self.new_lines) if self.new_lines else None,
            "content_unchanged": self.content_unchanged,
        }


@dataclass(frozen=True, slots=True)
class AffectedFinding:
    """A finding whose citation (or dependency) overlaps one or more changed regions."""

    finding_id: str
    commit: str
    path: str
    start_line: int
    end_line: int
    span_sha256: str | None
    reason: str
    item_ids: tuple[str, ...]
    status: str | None = None
    freshness: str | None = None
    status_basis: str | None = None
    workflow: str | None = None
    freshness_checked_against: str | None = None
    via: str = "citation"

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "citation": {
                "commit": self.commit,
                "path": self.path,
                "start_line": self.start_line,
                "end_line": self.end_line,
                "span_sha256": self.span_sha256,
            },
            "via": self.via,
            "reason": self.reason,
            "item_ids": list(self.item_ids),
            "status": self.status,
            "status_basis": self.status_basis,
            "workflow": self.workflow,
            "freshness": self.freshness,
            "freshness_checked_against": self.freshness_checked_against,
        }


@runtime_checkable
class AffectedFindingsProvider(Protocol):
    """Read-only lookup of findings touched by changed regions."""

    name: str

    def available(self) -> bool:
        """Whether a findings source is connected; ``False`` means "not computed"."""

    def affected_findings(self, regions: Sequence[ChangedRegion]) -> Sequence[AffectedFinding]:
        """Findings whose citations touch any of ``regions`` (never mutates anything)."""


class NullFindingsProvider:
    """No findings ledger connected: nothing is looked up, and that is stated."""

    name = "null"
    note = (
        "No findings ledger is connected (null provider), so affected findings were not "
        "computed. This is not evidence that no finding is affected."
    )

    def available(self) -> bool:
        return False

    def affected_findings(self, regions: Sequence[ChangedRegion]) -> Sequence[AffectedFinding]:
        return ()


_PLACED = frozenset({IDENTICAL, UNCHANGED, RELOCATED})
_UNPLACED_SHOWN = 20


def _touches(span: tuple[int, int], lines: tuple[int, int] | None,
             hunks: tuple[tuple[int, int], ...]) -> list[str]:
    """How the changed lines of one side touch the cited lines ``span`` (empty: not at all).

    A hunk with changed lines touches the span when the ranges overlap. A pure insertion or
    deletion (count 0) after line ``p`` changes the span's bytes when it falls strictly
    inside it (``A <= p < B``); one before or after the span leaves them intact.
    """
    first, last = span
    found = []
    if hunks:
        for start, count in hunks:
            if count:
                end = start + count - 1
                if start <= last and first <= end:
                    found.append(f"changed lines {start}-{end}")
            elif first <= start < last:
                found.append(f"lines inserted or removed after line {start}")
    elif lines is not None and lines[0] <= last and first <= lines[1]:
        found.append(f"changed lines {lines[0]}-{lines[1]}")
    return found


class LedgerFindingsProvider:
    """Affected findings from a findings ledger (read-only; see the module documentation)."""

    name = "ledger"

    def __init__(
        self,
        store: SnapshotStore,
        directory: Path | None,
        *,
        index: Any | None = None,
        problem: str | None = None,
    ) -> None:
        self.store = store
        self.directory = directory
        self.index = index
        self.problem = problem
        self._view: Any | None = None
        self._loaded = False
        self._coverage: dict[str, Any] | None = None
        self._candidates: list[AffectedFinding] = []

    # -- loading -----------------------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.problem is not None:
            return
        if self.directory is None or not self.directory.is_dir():
            self.problem = ("no findings ledger exists at the configured location (--ledger, "
                            "$TXRAY_FINDINGS or <store>/findings)")
            return
        try:
            self._view = project(Ledger(self.directory).read_events())
        except (IntegrityError, Refused) as exc:
            reason = "is not readable" if isinstance(exc, Refused) else "fails verification"
            self.problem = (f"the findings ledger {reason} (run: txray findings verify-log)")
        except OSError as exc:
            self.problem = f"the findings ledger could not be read ({type(exc).__name__})"

    @property
    def note(self) -> str | None:
        self._load()
        if self.problem is None:
            return None
        return (f"Affected findings were not computed: {self.problem}. This is not evidence "
                "that no finding is affected.")

    def available(self) -> bool:
        self._load()
        return self._view is not None

    def fingerprint(self) -> dict[str, Any]:
        """What the digest's ``inputs_sha256`` covers: the ledger head and event count."""
        self._load()
        if self._view is None:
            return {"available": False}
        return {"available": True, "head": self._view.head, "events": self._view.events}

    def coverage(self) -> dict[str, Any] | None:
        return self._coverage

    def relocation_candidates(self) -> Sequence[AffectedFinding]:
        """Findings cited in a file that moved without a byte changing (never affected)."""
        return tuple(self._candidates)

    # -- lookup ------------------------------------------------------------------------

    def affected_findings(self, regions: Sequence[ChangedRegion]) -> Sequence[AffectedFinding]:
        self._load()
        self._candidates = []
        if self._view is None or not regions:
            if self._view is not None:
                self._coverage = self._empty_coverage()
            return ()
        commits = {"old": regions[0].old_commit, "new": regions[0].new_commit}
        # the regions indexed by (side, path): changed content, and byte-identical moves
        changed: dict[tuple[str, str], list[ChangedRegion]] = {}
        moved: dict[tuple[str, str], list[ChangedRegion]] = {}
        for region in regions:
            for side in ("old", "new"):
                path = region.side(side)[0]
                if path is not None:
                    table = moved if region.content_unchanged else changed
                    table.setdefault((side, path), []).append(region)
        touched = set(changed) | set(moved)
        manifests = {side: self.store.load_manifest(commit)[1] for side, commit in commits.items()}
        verifier = Verifier(self.store, self.index)
        placements: dict[tuple[Any, ...], tuple[tuple[str, int, int] | None, str]] = {}
        results: list[AffectedFinding] = []
        candidates: list[AffectedFinding] = []
        coverage = self._empty_coverage()
        # first pass: which spans can be affected, and on which sides; their blobs (at the
        # cited commit and at each side where the path exists) are read in one git
        # process per commit before anything is placed
        work: list[tuple[Any, str, Citation, list[str]]] = []
        needed: dict[str, list[str]] = {}
        for state in self._view.states():
            if state.workflow not in ACTIVE_WORKFLOWS:
                continue
            coverage["active_findings"] += 1
            for via, cited in self._spans(state):
                coverage["spans"] += 1
                sides = [side for side in ("old", "new")
                         if _may_touch(cited.path, side, manifests[side], touched)]
                if not sides:
                    coverage["skipped"] += 1
                    continue
                work.append((state, via, cited, sides))
                needed.setdefault(cited.commit, []).append(cited.path)
                for side in sides:
                    if manifests[side].entry(cited.path) is not None:
                        needed.setdefault(commits[side], []).append(cited.path)
        for commit, paths in needed.items():
            try:
                verifier.prefetch(commit, paths)
            except TxrayError:
                pass  # an unpinned cited commit: each placement reports it on its own
        readings: dict[str, tuple[str, str | None]] = {}
        for state, via, cited, sides in work:
            if state.finding_id not in readings:
                freshness, against = state.freshness(commits["new"])
                if freshness == "NOT_CHECKED":
                    freshness, against = state.freshness()
                readings[state.finding_id] = (freshness, against)
            freshness, against = readings[state.finding_id]
            placed = {}
            outcomes = {"old": "untouched", "new": "untouched"}
            for side in sides:
                key = (cited.commit, cited.path, cited.start_line, cited.end_line,
                       cited.anchor, cited.span_sha256, commits[side])
                if key not in placements:
                    placements[key] = self._place(verifier, cited, commits[side])
                where, outcomes[side] = placements[key]
                if where is not None:
                    placed[side] = where
            if not placed:
                coverage["unplaced"] += 1
                if len(coverage["unplaced_spans"]) < _UNPLACED_SHOWN:
                    coverage["unplaced_spans"].append({
                        "finding_id": state.finding_id, "via": via,
                        "citation": _citation_label(cited),
                        "old": outcomes["old"], "new": outcomes["new"]})
                continue
            coverage["placed"] += 1
            reasons: list[str] = []
            items: list[str] = []
            moves: list[str] = []
            move_items: list[str] = []
            for side, (path, first, last) in placed.items():
                commit = commits[side]
                how = "" if outcomes[side] == "cited" else (
                    f" (cited at {cited.commit[:12]}; placed here by its exact span "
                    f"bytes: {outcomes[side]})")
                for region in changed.get((side, path), ()):
                    _, lines, hunks = region.side(side)
                    hit = _touches((first, last), lines, hunks)
                    if not hit:
                        continue
                    if region.item_id not in items:
                        items.append(region.item_id)
                    reasons.append(f"{side} side {commit[:12]}: {', '.join(hit)} touch "
                                   f"cited lines {first}-{last} of {path}{how}")
                for region in moved.get((side, path), ()):
                    other = region.new_path if side == "old" else region.old_path
                    if other == path:
                        continue  # mode-only change: neither the bytes nor the path moved
                    if region.item_id not in move_items:
                        move_items.append(region.item_id)
                    direction = "moved to" if side == "old" else "moved from"
                    moves.append(f"{side} side {commit[:12]}: {path} {direction} {other} "
                                 f"with identical bytes; cited lines {first}-{last} are "
                                 f"intact{how}")
            if items:
                results.append(self._row(state, cited, via, reasons, items, freshness, against))
            elif move_items:
                candidates.append(self._row(state, cited, via, moves, move_items, freshness,
                                            against))
        coverage["affected_findings"] = len({row.finding_id for row in results})
        coverage["relocation_candidates"] = len({row.finding_id for row in candidates})
        self._coverage = coverage
        self._candidates = candidates
        return results

    @staticmethod
    def _row(state: Any, cited: Citation, via: str, reasons: list[str], items: list[str],
             freshness: str, against: str | None) -> AffectedFinding:
        return AffectedFinding(
            finding_id=state.finding_id,
            commit=cited.commit,
            path=cited.path,
            start_line=cited.start_line,
            end_line=cited.end_line,
            span_sha256=cited.span_sha256,
            reason="; ".join(dict.fromkeys(reasons))[:1000],
            item_ids=tuple(items),
            status=state.status,
            freshness=freshness,
            status_basis=state.status_basis,
            workflow=state.workflow,
            freshness_checked_against=against,
            via=via,
        )

    @staticmethod
    def _empty_coverage() -> dict[str, Any]:
        return {"active_findings": 0, "spans": 0, "placed": 0, "unplaced": 0, "skipped": 0,
                "affected_findings": 0, "relocation_candidates": 0, "unplaced_spans": []}

    @staticmethod
    def _spans(state: Any) -> list[tuple[str, Citation]]:
        """The finding's code citations (latest provenance revision) and span dependencies."""
        spans: list[tuple[str, Citation]] = []
        revision = state.provenance[-1]["citations"] if state.provenance else code_citations(
            state.record)
        for item in revision:
            try:
                spans.append(("citation", Citation.from_dict(item)))
            except InvalidInput:
                continue
        for dependency in state.record.get("dependencies") or []:
            if dependency.get("kind") == "span":
                try:
                    spans.append(("span dependency", Citation.from_dict(dependency["citation"])))
                except InvalidInput:
                    continue
        return spans

    def _place(self, verifier: Verifier, cited: Citation,
               commit: str) -> tuple[tuple[str, int, int] | None, str]:
        """Where ``cited`` is at ``commit``: its own lines, or its exact bytes found once."""
        try:
            own = verifier.full_commit(cited.commit)
        except (NotFound, InvalidInput):
            return None, "commit_not_pinned"
        if own == commit:
            return (cited.path, cited.start_line, cited.end_line), "cited"
        try:
            relocation = verifier.relocate(cited, commit, propose=False)
        except TxrayError as exc:
            return None, f"error: {exc.code}"
        if relocation.outcome in _PLACED and relocation.current is not None:
            current = relocation.current
            return (current.path, current.start_line, current.end_line), relocation.outcome
        return None, relocation.outcome


def _may_touch(path: str, side: str, manifest: Any, touched: set[tuple[str, str]]) -> bool:
    """Whether a span cited in ``path`` can overlap a region on ``side`` (FA-009).

    While its path exists at a commit, re-anchoring looks for a span only in that path, so
    the span can be affected there only when that path carries a region. A path absent at
    the commit may have moved into a changed file, so such a span is placed.
    """
    if manifest.entry(path) is None:
        return True
    return (side, path) in touched


def _citation_label(citation: Citation) -> str:
    return f"{citation.path}:{citation.start_line}-{citation.end_line}@{citation.commit[:12]}"


def default_findings_provider(
    store: SnapshotStore,
    ledger: str | Path | None = None,
    *,
    index: Any | None = None,
    environ: Mapping[str, str] | None = None,
) -> AffectedFindingsProvider:
    """The provider the CLI uses: the findings ledger, read-only.

    The ledger is ``ledger``, else ``$TXRAY_FINDINGS``, else ``<store>/findings`` (the same
    resolution as ``txray findings``). When it cannot be used - the default location lies
    inside a git working tree, no ledger exists there, or it fails verification - the
    provider reports itself unavailable with the reason, and the digest says "not computed".
    """
    try:
        directory = ledger_directory(ledger, store.root, environ)
    except Refused:
        return LedgerFindingsProvider(
            store, None, index=index,
            problem="the default findings ledger would lie inside a git working tree; pass "
                    "--ledger DIR or set $TXRAY_FINDINGS")
    return LedgerFindingsProvider(store, directory, index=index)
