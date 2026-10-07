"""Operations on the findings memory: add, import, verify, reanchor, review, supersede,
retract, list, queue and export.

Every write takes the ledger lock, re-verifies the whole event chain, builds the current
view, checks the operation against it and appends its events atomically (see
:mod:`timelinexray.findings.ledger`). The rules enforced here:

* **Write gate** (``add``): every code citation is read from its git blob and must be
  ``INTACT`` with its anchor inside the span (``FOUND`` once or ``FOUND_MULTIPLE``; a
  ``MISSING`` anchor is refused); an expected span SHA-256,
  when given, must match. Code-class claims need a code citation, and a ``NOT_FOUND``
  code-class claim needs a recorded negative search with zero hits. ``PARAM_DEFAULT``
  claims have the scope ``public_default``.
* **The verifier never assesses claims.** ``verify`` and ``reanchor`` append ``verify``
  events that change freshness only. Evidence status changes only through ``review``.
* **No self-approval.** A ``review`` needs an actor with a reviewer role whose name differs
  from the actor that created (``create``, ``import`` or ``supersede``) the finding.
  Confirming ``SUPPORTED`` or ``PARTIAL`` (or a ``NOT_FOUND`` backed by a negative search)
  also needs a current integrity check of the cited evidence.
* **Imports never promote.** Imported findings keep the report's status with basis
  ``reported`` and workflow ``imported`` until a reviewer reviews them.
"""

from __future__ import annotations

import os
import sqlite3
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import __version__
from ..errors import InvalidInput, NotFound, Refused, TxrayError
from ..index import CodeIndex, path_param
from ..snapshot.store import SnapshotStore
from ..span import CONFIRMING, parse_line_range
from ..verify import (
    CHANGED,
    CHANGED_SPAN,
    CURRENT,
    INTACT,
    MISSING,
    NOT_CHECKED,
    STALE,
    UNUSABLE,
    UNVERIFIABLE,
    VERIFIER_VERSION,
    Citation,
    CitationCheck,
    Relocation,
    Verifier,
    worst_freshness,
)
from . import research
from .ledger import Ledger
from .model import (
    AUTHOR_ROLES,
    CODE_CLASSES,
    DEFAULT_SCOPE,
    EVIDENCE_CLASSES,
    MAX_TITLE,
    RECORD_SCHEMA,
    REVIEWER_ROLES,
    SCOPES,
    STATUSES,
    VERIFIER_ROLE,
    Actor,
    canonical_json,
    check_choice,
    check_finding_id,
    check_text,
    code_citations,
    sha256_hex,
    web_source,
)
from .state import OWN_SCOPE, FindingState, View, project

ENV_FINDINGS = "TXRAY_FINDINGS"
FINDINGS_DIRECTORY = "findings"
VERIFIER_ACTOR = Actor("txray-verifier", VERIFIER_ROLE)
NEGATIVE_HIT_SAMPLE = 20
_SPEC_KEYS = frozenset({
    "finding_id", "title", "claim", "component", "evidence_class", "status", "scope",
    "citations", "web_sources", "depends_on", "negative", "limitations",
})


# -- ledger location -------------------------------------------------------------------------


def _worktree_of(path: Path) -> Path | None:
    """The nearest ancestor of ``path`` that contains a ``.git`` entry, if any."""
    current = Path(os.path.abspath(path))
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def ledger_directory(
    explicit: str | Path | None, store_root: Path, environ: Mapping[str, str] | None = None
) -> Path:
    """``--ledger``, else ``$TXRAY_FINDINGS``, else ``<store>/findings`` outside any worktree."""
    environ = os.environ if environ is None else environ
    if explicit:
        return Path(explicit).expanduser()
    if environ.get(ENV_FINDINGS):
        return Path(environ[ENV_FINDINGS]).expanduser()
    default = Path(store_root).expanduser() / FINDINGS_DIRECTORY
    worktree = _worktree_of(default)
    if worktree is not None:
        raise Refused(
            f"the default findings ledger {default} would be inside the git working tree "
            f"{worktree}; pass --ledger DIR or set ${ENV_FINDINGS} to choose a location "
            "explicitly"
        )
    return default


# -- helpers -----------------------------------------------------------------------------------


def _lines(value: object, what: str) -> tuple[int, int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value, value
    if not isinstance(value, str):
        raise InvalidInput(f"{what}: lines must be 'A-B' or 'A'")
    return parse_line_range(value)


def _citation_input(data: object, what: str) -> Citation:
    if not isinstance(data, dict):
        raise InvalidInput(f"{what} must be an object with commit, path, lines and anchor")
    unknown = set(data) - {"commit", "path", "lines", "start_line", "end_line", "anchor",
                           "span_sha256"}
    if unknown:
        raise InvalidInput(f"{what}: unknown keys {sorted(unknown)}")
    if "lines" in data:
        start, end = _lines(data["lines"], what)
    else:
        start, end = data.get("start_line"), data.get("end_line")
    return Citation.from_dict({
        "commit": data.get("commit"), "path": data.get("path"), "start_line": start,
        "end_line": end, "anchor": data.get("anchor"), "span_sha256": data.get("span_sha256"),
    })


def _check_summary(check: CitationCheck) -> dict[str, Any]:
    return {
        "label": check.citation.label(),
        "verdict": check.verdict,
        "reason": check.reason,
        "anchor_verdict": check.anchor_verdict,
        "anchor_lines": list(check.anchor_lines),
        "baseline": check.baseline,
        "observed_span_sha256": check.observed.span_sha256 if check.observed else None,
        "observed_commit": check.observed.commit if check.observed else None,
    }


def _check_freshness(check: CitationCheck) -> str:
    if check.usable:
        return CURRENT
    return STALE if check.verdict == CHANGED else UNVERIFIABLE


def _worst_verdict(checks: list[CitationCheck]) -> str | None:
    if not checks:
        return None
    verdicts = {check.verdict for check in checks}
    for verdict in (MISSING, CHANGED):
        if verdict in verdicts:
            return verdict
    return INTACT


class FindingsMemory:
    """The findings memory of one ledger directory, reading spans from one snapshot store."""

    def __init__(
        self, ledger: Ledger, store: SnapshotStore, index: CodeIndex | None = None
    ) -> None:
        self.ledger = ledger
        self.store = store
        self.index = index if index is not None else CodeIndex(store)
        self.verifier = Verifier(store, self.index)

    # -- reading ---------------------------------------------------------------------------

    def view(self) -> View:
        return project(self.ledger.events())

    def get(self, finding_id: str, view: View | None = None) -> FindingState:
        view = view or self.view()
        state = view.get(check_finding_id(finding_id))
        if state is None:
            raise NotFound(f"finding {finding_id} is not in the ledger {self.ledger.directory}")
        return state

    def full_commit(self, commit: str) -> str:
        return self.verifier.full_commit(commit)

    # -- building records --------------------------------------------------------------------

    def _resolve_citation(self, citation: Citation, what: str) -> Citation:
        check = self.verifier.check(Citation(citation.commit, citation.path, citation.start_line,
                                             citation.end_line, citation.anchor))
        if check.verdict != INTACT or check.observed is None:
            raise Refused(f"{what} {citation.label()} cannot be read: {check.reason}")
        if check.anchor_verdict not in CONFIRMING:
            raise Refused(
                f"{what} {citation.label()}: anchor {citation.anchor!r} is "
                f"{check.anchor_verdict} in the span; it must occur inside the cited lines"
            )
        if citation.span_sha256 and citation.span_sha256 != check.observed.span_sha256:
            raise Refused(
                f"{what} {citation.label()}: span sha256 is {check.observed.span_sha256}, "
                f"not the expected {citation.span_sha256}"
            )
        return check.observed

    def negative_search(
        self, commit: str, query: str, *, literal: bool = False, path_glob: str | None = None
    ) -> dict[str, Any]:
        """Run a recorded search and describe exactly what it covered."""
        full = self.full_commit(commit)
        generation = self.index.generation(full)
        result = self.index.search(full, query, path_glob=path_glob, literal=literal,
                                   limit=NEGATIVE_HIT_SAMPLE)
        return {
            "commit": full,
            "query": query,
            "literal": literal,
            "path_glob": path_glob,
            "match": "every term as a case-insensitive substring of one line",
            "terms": list(result.terms),
            "total": result.total,
            "truncated": result.truncated,
            "hits": [
                {"path": hit.path, "start_line": hit.start_line, "end_line": hit.end_line,
                 "span_sha256": hit.span_sha256}
                for hit in result.hits
            ],
            "generation": generation.to_dict(),
            "coverage": self._coverage(full, path_glob),
        }

    def _coverage(self, commit: str, path_glob: str | None) -> dict[str, Any]:
        rows = self.index.coverage(commit).rows
        if path_glob is not None:  # the same SQLite GLOB the search applied
            db = sqlite3.connect(":memory:")
            try:  # bytes, as the index binds them: a non-UTF-8 path is not bindable as text
                pattern = path_param(path_glob)
                rows = tuple(row for row in rows if db.execute(
                    "SELECT CAST(? AS TEXT) GLOB CAST(? AS TEXT)",
                    (path_param(row.path), pattern)).fetchone()[0])
            finally:
                db.close()
        searched = [row for row in rows if row.lexical_status == "indexed"]
        return {
            "paths_in_scope": len(rows),
            "paths_searched": len(searched),
            "languages_searched": dict(sorted(Counter(row.language or "unknown"
                                                      for row in searched).items())),
            "skipped_by_reason": dict(sorted(Counter(row.lexical_reason or "unknown"
                                                     for row in rows
                                                     if row.lexical_status != "indexed").items())),
        }

    def build_record(self, spec: object, view: View) -> dict[str, Any]:
        """Validate an ``add`` specification and apply the write gate."""
        if not isinstance(spec, dict):
            raise InvalidInput("a finding specification must be a JSON object")
        unknown = set(spec) - _SPEC_KEYS
        if unknown:
            raise InvalidInput(f"unknown keys in the finding specification: {sorted(unknown)}")
        evidence_class = check_choice(spec.get("evidence_class"), EVIDENCE_CLASSES,
                                      "evidence_class")
        status = check_choice(spec.get("status"), STATUSES, "status")
        scope = spec.get("scope") or DEFAULT_SCOPE[evidence_class]
        check_choice(scope, SCOPES, "scope")
        if evidence_class == "PARAM_DEFAULT" and scope != "public_default":
            raise InvalidInput("a PARAM_DEFAULT finding has the scope public_default: public "
                               "defaults are not production values")
        citations = [
            self._resolve_citation(_citation_input(item, f"citation {number}"),
                                   f"citation {number}")
            for number, item in enumerate(_as_list(spec.get("citations"), "citations"), 1)
        ]
        webs = [web_source(item) for item in _as_list(spec.get("web_sources"), "web_sources")]
        dependencies = []
        for number, item in enumerate(_as_list(spec.get("depends_on"), "depends_on"), 1):
            if isinstance(item, dict) and set(item) == {"finding"}:
                target = view.get(check_finding_id(item["finding"]))
                if target is None or not target.active:
                    raise Refused(f"dependency {item['finding']} is not an active finding")
                dependencies.append({"kind": "finding", "finding_id": target.finding_id})
            elif isinstance(item, dict) and set(item) == {"span"}:
                cited = self._resolve_citation(_citation_input(item["span"],
                                                               f"span dependency {number}"),
                                               f"span dependency {number}")
                dependencies.append({"kind": "span", "citation": cited.to_dict()})
            else:
                raise InvalidInput(f"depends_on {number} must be {{\"finding\": ID}} or "
                                   "{\"span\": {commit, path, lines, anchor}}")
        negative = None
        if spec.get("negative") is not None:
            item = spec["negative"]
            if not isinstance(item, dict) or not {"commit", "query"} <= set(item) or \
                    set(item) - {"commit", "query", "literal", "path"}:
                raise InvalidInput("negative must be {commit, query, literal?, path?}")
            if status != "NOT_FOUND":
                raise InvalidInput("a negative search belongs to a NOT_FOUND finding")
            negative = self.negative_search(item["commit"], item["query"],
                                            literal=bool(item.get("literal", False)),
                                            path_glob=item.get("path"))
            if negative["total"]:
                raise Refused(
                    f"the negative search {item['query']!r} has {negative['total']} hit(s) at "
                    f"{negative['commit'][:12]} (first: {negative['hits'][0]['path']}:"
                    f"{negative['hits'][0]['start_line']}); a NOT_FOUND finding needs zero hits"
                )
        if evidence_class in CODE_CLASSES:
            if status == "NOT_FOUND" and negative is None:
                raise InvalidInput(
                    "a NOT_FOUND claim about code needs a negative search "
                    "(negative: {commit, query, literal?, path?}) so it can be re-run"
                )
            if status != "NOT_FOUND" and not citations:
                raise InvalidInput(f"a {evidence_class} finding needs at least one code citation")
        if not citations and not webs and negative is None and not dependencies:
            raise InvalidInput("a finding needs evidence: citations, web_sources, a negative "
                               "search or dependencies")
        limitations = [check_text(item, "limitation", maximum=1000)
                       for item in _as_list(spec.get("limitations"), "limitations")]
        record = {
            "schema": RECORD_SCHEMA,
            "finding_id": None,
            "title": check_text(spec.get("title"), "title", maximum=MAX_TITLE),
            "claim": check_text(spec.get("claim"), "claim"),
            "component": check_text(spec.get("component"), "component", maximum=200),
            "evidence_class": evidence_class,
            "status": status,
            "scope": scope,
            "sources": [citation.to_dict() for citation in citations] + webs,
            "dependencies": dependencies,
            "negative": negative,
            "limitations": limitations,
            "attributes": {},
            "origin": {"kind": "create"},
        }
        finding_id = spec.get("finding_id")
        if finding_id is None:
            finding_id = "F-" + sha256_hex(canonical_json(record))[:12]
        record["finding_id"] = check_finding_id(finding_id)
        return record

    # -- writes --------------------------------------------------------------------------------

    def add(self, spec: object, actor: Actor, *, expect_head: str | None = None) -> FindingState:
        _require_role(actor, AUTHOR_ROLES, "add a finding")
        with self.ledger.writer(expect_head=expect_head) as writer:
            view = project(writer.existing)
            record = self.build_record(spec, view)
            if view.get(record["finding_id"]) is not None:
                raise Refused(f"finding {record['finding_id']} already exists; supersede it "
                              "to change it")
            writer.append("create", actor, record["finding_id"], {"record": record})
            finding_id = record["finding_id"]
        return self.get(finding_id)

    def import_file(
        self, path: Path | str, source_label: str, actor: Actor, *,
        expect_head: str | None = None,
    ) -> dict[str, Any]:
        """Import a research-import file as ``imported`` findings and verify their spans."""
        _require_role(actor, AUTHOR_ROLES, "import findings")
        label = research.check_label(source_label)
        document = research.load(Path(path))
        problems = research.validate(document.data)
        if problems:
            shown = "; ".join(problems[:20])
            more = f" (and {len(problems) - 20} more)" if len(problems) > 20 else ""
            raise InvalidInput(f"{document.name} is not a valid research-import file: "
                               f"{shown}{more}")
        report: dict[str, Any] = {
            "file": document.name, "format": document.format, "file_sha256": document.sha256,
            "source_label": label, "findings": len(document.data["findings"]),
            "imported": 0, "unchanged": 0, "verified": 0,
            "freshness": Counter(), "citations": Counter(), "anchors": Counter(),
            "unpinned_commits": Counter(),
        }

        def resolve(source: dict[str, Any]) -> dict[str, Any]:
            citation = self._import_citation(source)
            if citation.get("resolution") == "commit_not_pinned":
                report["unpinned_commits"][source["commit"]] += 1
            return citation

        with self.ledger.writer(expect_head=expect_head) as writer:
            view = project(writer.existing)
            for finding in document.data["findings"]:
                record = research.to_record(finding, label, document, resolve)
                existing = view.get(record["finding_id"])
                if existing is not None:
                    same = (existing.record.get("origin") or {}).get("finding_sha256")
                    if same == record["origin"]["finding_sha256"]:
                        report["unchanged"] += 1
                        continue
                    raise Refused(
                        f"finding {record['finding_id']} was already imported with different "
                        "content; supersede it instead of re-importing"
                    )
                event = writer.append("import", actor, record["finding_id"], {"record": record})
                report["imported"] += 1
                if code_citations(record):
                    state = project([event]).findings[record["finding_id"]]
                    payload = self._integrity_payload(state, view)
                    writer.append("verify", VERIFIER_ACTOR, record["finding_id"], payload)
                    report["verified"] += 1
                    report["freshness"][payload["freshness"]] += 1
                    for item in payload["citations"]:
                        report["citations"][item["verdict"]] += 1
                        report["anchors"][item["anchor_verdict"] or "not read"] += 1
        for key in ("freshness", "citations", "anchors", "unpinned_commits"):
            report[key] = dict(sorted(report[key].items()))
        report["hint"] = None
        if report["unpinned_commits"]:
            # one command: `txray pin` takes one commit, so a list of pins plus a verify was
            # several steps (and an easy-to-mistype command line)
            report["hint"] = (
                f"{sum(report['unpinned_commits'].values())} citation(s) name "
                f"{len(report['unpinned_commits'])} commit(s) this store has not pinned; to "
                f"resolve them in one step: txray findings verify --pin-cited --label {label}"
            )
        report["head"] = self.ledger.verify().last_hash
        return report

    def _import_citation(self, source: dict[str, Any]) -> dict[str, Any]:
        """Resolve a research-import code source as far as the store allows."""
        start, end = parse_line_range(str(source["lines"]))
        citation = Citation(source["commit"], source["path"], start, end, source["anchor"])
        check = self.verifier.check(citation)
        record = citation.to_dict()
        if check.observed is not None:
            record = check.observed.to_dict()
        record["reported"] = {"commit": source["commit"], "lines": source["lines"]}
        record["resolution"] = None if check.observed is not None else check.reason
        return record

    def _integrity_payload(self, state: FindingState, view: View) -> dict[str, Any]:
        record = state.record
        sources = code_citations(record)
        checks = [self.verifier.check(Citation.from_dict(item)) for item in sources]
        parts: list[str] = [_check_freshness(check) for check in checks]
        reasons: list[str] = []
        triggers: list[dict[str, Any]] = []
        entries = []
        resolved_now = 0
        resolved: list[dict[str, Any]] = []
        for source, check in zip(sources, checks):
            if source.get("resolution") and check.usable and check.observed is not None:
                resolved_now += 1
                resolved.append({**check.observed.to_dict(),
                                 "reported": source.get("reported"), "resolution": None})
            else:
                resolved.append(source)
        for number, check in enumerate(checks, 1):
            entries.append({"index": number, **_check_summary(check)})
            if check.verdict == CHANGED:
                reasons.append(f"citation {number} {check.citation.label()} CHANGED: {check.reason}")
                triggers.append({"trigger": "citation_changed", "summary": reasons[-1],
                                 "detail": entries[-1]})
            elif not check.usable:
                why = check.reason or f"anchor {check.anchor_verdict}"
                reasons.append(f"citation {number} {check.citation.label()} {check.verdict}: {why}")
                triggers.append({"trigger": "citation_unusable", "summary": reasons[-1],
                                 "detail": entries[-1]})
        dependencies = []
        for dependency in record.get("dependencies", []):
            if dependency["kind"] == "span":
                check = self.verifier.check(Citation.from_dict(dependency["citation"]))
                fresh = _check_freshness(check)
                dependencies.append({"kind": "span", **_check_summary(check), "freshness": fresh})
            else:
                other = view.get(dependency["finding_id"])
                fresh = CURRENT if other is not None and other.active else STALE
                dependencies.append({"kind": "finding", "finding_id": dependency["finding_id"],
                                     "workflow": other.workflow if other else None,
                                     "freshness": fresh})
            parts.append(fresh)
            if fresh != CURRENT:
                reasons.append(f"dependency {dependencies[-1].get('label') or dependency.get('finding_id')} "
                               f"is {fresh}")
                triggers.append({"trigger": "dependency_changed", "summary": reasons[-1],
                                 "detail": dependencies[-1]})
        negative = self._negative_check(record, None, parts, reasons, triggers)
        # A citation that could not be read when the record was written (its commit was
        # not pinned) and reads now gets its hashes recorded as a provenance revision at
        # the cited commits; the immutable record keeps the reported form.
        provenance = {"commit": None, "citations": resolved} if resolved_now else None
        return {
            "mode": "integrity",
            "target": None,
            "freshness": worst_freshness(parts),
            "verdict": _worst_verdict(checks),
            "reasons": reasons,
            "citations": entries,
            "dependencies": dependencies,
            "negative": negative,
            "provenance": provenance,
            "resolved_citations": resolved_now,
            "triggers": triggers,
            "verifier": VERIFIER_VERSION,
            "tool_version": __version__,
        }

    def _negative_check(
        self, record: dict[str, Any], target: str | None, parts: list[str],
        reasons: list[str], triggers: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        negative = record.get("negative")
        if not negative:
            return None
        commit = target or negative["commit"]
        try:
            rerun = self.negative_search(commit, negative["query"], literal=negative["literal"],
                                         path_glob=negative["path_glob"])
        except TxrayError as exc:
            parts.append(UNVERIFIABLE)
            reasons.append(f"the negative search could not be re-run at {commit[:12]}: {exc}")
            detail = {"commit": commit, "error": str(exc)}
            triggers.append({"trigger": "negative_unverifiable", "summary": reasons[-1],
                             "detail": detail})
            return {"freshness": UNVERIFIABLE, **detail}
        fresh = STALE if rerun["total"] else CURRENT
        parts.append(fresh)
        if rerun["total"]:
            reasons.append(f"the negative search {negative['query']!r} now has {rerun['total']} "
                           f"hit(s) at {rerun['commit'][:12]}")
            triggers.append({"trigger": "negative_now_found", "summary": reasons[-1], "detail": {
                "query": negative["query"], "commit": rerun["commit"], "total": rerun["total"],
                "hits": rerun["hits"]}})
        return {"freshness": fresh, **rerun}

    def verify(
        self, finding_ids: list[str] | None = None, *, actor: Actor = VERIFIER_ACTOR,
        label: str | None = None, expect_head: str | None = None,
    ) -> list[dict[str, Any]]:
        """Integrity check of each finding's evidence at its own commits (freshness only).

        ``label`` restricts the default selection (every active finding) to the findings
        imported with that source label. A result's ``resolved_citations`` counts citations
        that could not be read when the finding was imported and were resolved now; its
        ``recheck_targets`` lists the re-anchoring targets whose recorded checks were made
        from the unresolved citations and so no longer count (re-anchor them again).
        """
        results = []
        with self.ledger.writer(expect_head=expect_head) as writer:
            view = project(writer.existing)
            for state in self._select(view, finding_ids, label=label):
                if not _checkable(state):
                    results.append({"finding_id": state.finding_id, "skipped":
                                    "no code citations, dependencies or negative search"})
                    continue
                payload = self._integrity_payload(state, view)
                event = writer.append("verify", actor, state.finding_id, payload)
                recheck = sorted(state.targets) if payload["resolved_citations"] else []
                results.append({"finding_id": state.finding_id, "event": event.hash,
                                **{k: payload[k] for k in ("freshness", "verdict", "reasons")},
                                "triggers": [t["trigger"] for t in payload["triggers"]],
                                "resolved_citations": payload["resolved_citations"],
                                "recheck_targets": recheck})
        return results

    def reanchor(
        self, target: str, finding_ids: list[str] | None = None, *,
        actor: Actor = VERIFIER_ACTOR, expect_head: str | None = None,
    ) -> dict[str, Any]:
        """Re-anchor findings on a pinned target commit (P7 section 3.3; freshness only)."""
        full = self.full_commit(target)
        results: list[dict[str, Any]] = []
        with self.ledger.writer(expect_head=expect_head) as writer:
            view = project(writer.existing)
            selected = [state for state in self._select(view, finding_ids) if _checkable(state)]
            ordered = _dependency_order(view, selected)
            computed: dict[str, str] = {}
            for state, cycle in ordered:
                payload = self._reanchor_payload(state, view, full, computed, cycle)
                computed[state.finding_id] = payload["freshness"]
                event = writer.append("verify", actor, state.finding_id, payload)
                outcomes = Counter(item["outcome"] for item in payload["citations"])
                results.append({
                    "finding_id": state.finding_id, "event": event.hash,
                    "freshness": payload["freshness"], "reasons": payload["reasons"],
                    "outcomes": dict(sorted(outcomes.items())),
                    "triggers": [t["trigger"] for t in payload["triggers"]],
                    "provenance": bool(payload["provenance"]),
                })
        totals = Counter(result["freshness"] for result in results)
        return {"target": full, "findings": len(results),
                "freshness": dict(sorted(totals.items())), "results": results}

    def _reanchor_payload(
        self, state: FindingState, view: View, target: str, computed: dict[str, str],
        cycle: bool,
    ) -> dict[str, Any]:
        record = state.record
        base = state.provenance[-1]["citations"] if state.provenance else []
        relocations: list[Relocation] = [self.verifier.relocate(Citation.from_dict(item), target)
                                         for item in base]
        parts = [relocation.freshness for relocation in relocations]
        reasons: list[str] = []
        triggers: list[dict[str, Any]] = []
        entries = []
        for number, relocation in enumerate(relocations, 1):
            entry = {"index": number, **relocation.to_dict()}
            entries.append(entry)
            if relocation.freshness == CURRENT:
                continue
            reasons.append(f"citation {number} {relocation.citation.label()} "
                           f"{relocation.outcome}: {relocation.reason}")
            if relocation.outcome == UNUSABLE and state.integrity is not None \
                    and state.integrity["freshness"] != CURRENT:
                continue  # already queued by the integrity check of the cited commit
            trigger = {CHANGED_SPAN: "changed_span", "ambiguous": "ambiguous_span",
                       "missing": "missing_span"}.get(relocation.outcome, "citation_unusable")
            triggers.append({"trigger": trigger, "summary": reasons[-1], "detail": {
                "old": relocation.citation.to_dict(), "outcome": relocation.outcome,
                "reason": relocation.reason, "proposed": relocation.proposed,
                "candidates": list(relocation.candidates),
                "anchor_at_target": relocation.anchor_at_target, "diff": relocation.diff,
                "claim": record.get("claim")}})
        dependencies = []
        for dependency in record.get("dependencies", []):
            if dependency["kind"] == "span":
                relocation = self.verifier.relocate(Citation.from_dict(dependency["citation"]),
                                                    target)
                fresh = relocation.freshness
                entry = {"kind": "span", "freshness": fresh, **relocation.to_dict()}
                label = relocation.citation.label()
            else:
                other = view.get(dependency["finding_id"])
                label = dependency["finding_id"]
                if cycle:
                    fresh, why = UNVERIFIABLE, "dependency cycle"
                elif other is None or not other.active:
                    fresh = STALE
                    why = f"the dependency is {other.workflow if other else 'missing'}"
                else:
                    fresh = computed.get(other.finding_id) or other.freshness(target)[0]
                    why = f"the dependency is {fresh} at the target"
                    if fresh == NOT_CHECKED:
                        fresh, why = CURRENT, "the dependency has no code evidence to check"
                entry = {"kind": "finding", "finding_id": dependency["finding_id"],
                         "freshness": fresh, "reason": why}
            dependencies.append(entry)
            parts.append(fresh)
            if fresh != CURRENT:
                reasons.append(f"dependency {label} is {fresh}")
                triggers.append({"trigger": "dependency_changed", "summary": reasons[-1],
                                 "detail": entry})
        negative = self._negative_check(record, target, parts, reasons, triggers)
        freshness = worst_freshness(parts)
        provenance = None
        current = None
        if relocations and all(r.freshness == CURRENT for r in relocations):
            current = [r.current.to_dict() for r in relocations if r.current is not None]
            if any(r.moved for r in relocations):
                provenance = {"commit": target, "citations": current}
        return {
            "mode": "reanchor",
            "target": target,
            "freshness": freshness,
            "verdict": None,
            "reasons": reasons,
            "citations": entries,
            "current_citations": current,
            "dependencies": dependencies,
            "negative": negative,
            "provenance": provenance,
            "triggers": triggers,
            "verifier": VERIFIER_VERSION,
            "tool_version": __version__,
        }

    def review(
        self, finding_id: str, actor: Actor, status: str, rationale: str, *,
        objections: list[str] | None = None, target: str | None = None,
        expect_head: str | None = None,
    ) -> FindingState:
        """Record a reviewer's assessed status (the only way a status becomes reviewed).

        The review closes the queue items of the cited commits and, with ``target``, of
        that re-anchoring target (which must have a recorded check); items of other
        targets stay open.
        """
        _require_role(actor, REVIEWER_ROLES, "review a finding")
        check_choice(status, STATUSES, "status")
        rationale = check_text(rationale, "rationale")
        objections = [check_text(item, "objection", maximum=1000) for item in objections or []]
        assessed = self.full_commit(target) if target else None
        with self.ledger.writer(expect_head=expect_head) as writer:
            state = self.get(finding_id, project(writer.existing))
            if assessed is not None and state.target_check(assessed) is None:
                raise Refused(
                    f"finding {state.finding_id} has no recorded re-anchoring check at "
                    f"{assessed[:12]}; run: txray findings reanchor {assessed[:12]} "
                    f"{state.finding_id}, then review with --target"
                )
            if not state.active:
                raise Refused(f"finding {state.finding_id} is {state.workflow}; review its "
                              f"successor instead" if state.superseded_by else
                              f"finding {state.finding_id} is {state.workflow}")
            if actor.key == state.created_by.key:
                raise Refused(
                    f"no self-approval: {actor.name} created finding {state.finding_id} "
                    f"({state.origin}); another reviewer must review it"
                )
            needs_check = (status in ("SUPPORTED", "PARTIAL") and state.has_code) or (
                status == "NOT_FOUND" and state.record.get("negative"))
            if needs_check and (state.integrity is None or state.integrity["freshness"] != CURRENT):
                latest = (f"latest integrity check is {state.integrity['freshness']}"
                          if state.integrity else "it was never verified")
                raise Refused(
                    f"finding {state.finding_id} cannot be reviewed as {status}: its cited "
                    f"evidence must first verify INTACT with anchors inside the spans ({latest}); "
                    f"run: txray findings verify {state.finding_id}"
                )
            scopes = [OWN_SCOPE] + ([assessed] if assessed else [])
            writer.append("review", actor, state.finding_id, {
                "status": status,
                "rationale": rationale,
                "objections": objections,
                "previous_status": state.status,
                "previous_basis": state.status_basis,
                "integrity_event": state.integrity["event"] if state.integrity else None,
                "assessed_target": assessed,
                "closed_scopes": scopes,
            })
        return self.get(finding_id)

    def supersede(
        self, finding_id: str, actor: Actor, *, by: str | None = None,
        spec: object | None = None, rationale: str, expect_head: str | None = None,
    ) -> FindingState:
        """Mark a finding superseded by an existing finding or by a new record."""
        _require_role(actor, AUTHOR_ROLES + REVIEWER_ROLES, "supersede a finding")
        if (by is None) == (spec is None):
            raise InvalidInput("give exactly one of --by ID and --file SPEC")
        rationale = check_text(rationale, "rationale")
        with self.ledger.writer(expect_head=expect_head) as writer:
            view = project(writer.existing)
            state = self.get(finding_id, view)
            if not state.active:
                raise Refused(f"finding {state.finding_id} is already {state.workflow}")
            record = None
            if spec is not None:
                record = self.build_record(spec, view)
                by = record["finding_id"]
                if view.get(by) is not None:
                    raise Refused(f"finding {by} already exists; use --by {by}")
            else:
                successor = self.get(by, view)  # type: ignore[arg-type]
                if not successor.active or successor.finding_id == state.finding_id:
                    raise Refused(f"finding {by} cannot supersede {state.finding_id}")
            writer.append("supersede", actor, state.finding_id,
                          {"by": by, "record": record, "rationale": rationale})
        return self.get(finding_id)

    def retract(
        self, finding_id: str, actor: Actor, reason: str, *, expect_head: str | None = None
    ) -> FindingState:
        _require_role(actor, AUTHOR_ROLES + REVIEWER_ROLES, "retract a finding")
        reason = check_text(reason, "reason")
        with self.ledger.writer(expect_head=expect_head) as writer:
            state = self.get(finding_id, project(writer.existing))
            if not state.active:
                raise Refused(f"finding {state.finding_id} is already {state.workflow}")
            writer.append("retract", actor, state.finding_id, {"reason": reason})
        return self.get(finding_id)

    # -- listing and export ------------------------------------------------------------------

    @staticmethod
    def _select(view: View, finding_ids: list[str] | None,
                label: str | None = None) -> list[FindingState]:
        if not finding_ids:
            states = [state for state in view.states() if state.active]
            if label is not None:
                label = research.check_label(label)
                states = [state for state in states
                          if (state.record.get("origin") or {}).get("source_label") == label]
            return states
        if label is not None:
            raise InvalidInput("give finding ids or --label, not both")
        states = []
        for finding_id in dict.fromkeys(finding_ids):
            state = view.get(check_finding_id(finding_id))
            if state is None:
                raise NotFound(f"finding {finding_id} is not in the ledger")
            states.append(state)
        return states

    def export(self, form: str = "findings", *, label: str | None = None) -> dict[str, Any]:
        view = self.view()
        if form == "findings":
            return {
                "schema": "timelinexray/findings-export/v1",
                "head": view.head,
                "events": view.events,
                "findings": [state.to_dict() for state in view.states()],
            }
        if form == "events":
            return {"schema": "timelinexray/findings-events/v1", "head": view.head,
                    "events": [event.to_dict() for event in self.ledger.events()]}
        if form == "research":
            if label is None:
                raise InvalidInput("--format research needs --label LABEL")
            label = research.check_label(label)
            records = [state.record for state in view.states()
                       if (state.record.get("origin") or {}).get("source_label") == label]
            return research.from_records(records)
        raise InvalidInput(f"unknown export format {form!r}")


def _as_list(value: object, what: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidInput(f"{what} must be a list")
    return value


def _require_role(actor: Actor, roles: tuple[str, ...], action: str) -> None:
    if actor.role not in roles:
        raise Refused(f"role {actor.role!r} may not {action}; allowed roles: {', '.join(roles)}")


def _checkable(state: FindingState) -> bool:
    record = state.record
    return bool(code_citations(record) or record.get("dependencies") or record.get("negative"))


def _dependency_order(
    view: View, selected: list[FindingState]
) -> list[tuple[FindingState, bool]]:
    """Selected findings plus their finding dependencies, dependencies first."""
    order: list[tuple[FindingState, bool]] = []
    done: set[str] = set()
    visiting: set[str] = set()

    def visit(state: FindingState) -> bool:
        if state.finding_id in done:
            return False
        if state.finding_id in visiting:
            return True
        visiting.add(state.finding_id)
        cycle = False
        for dependency in state.record.get("dependencies", []):
            if dependency["kind"] == "finding":
                other = view.get(dependency["finding_id"])
                if other is not None and other.active and _checkable(other):
                    cycle |= visit(other)
        visiting.discard(state.finding_id)
        done.add(state.finding_id)
        order.append((state, cycle))
        return cycle

    for state in selected:
        visit(state)
    return order
