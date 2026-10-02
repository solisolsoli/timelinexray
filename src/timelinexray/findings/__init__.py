"""Findings memory (Milestone 3): reviewed claims whose every code citation is a
commit-pinned byte span, kept in an append-only, SHA-256-chained event log.

* :mod:`.model` - vocabulary: evidence status, evidence class, freshness and workflow are
  separate fields; actors and record validation.
* :mod:`.ledger` - the event log (``create``, ``verify``, ``review``, ``supersede``,
  ``retract``, ``import``), its hash chain, lock and ``verify-log``.
* :mod:`.state` - the active view, a pure projection of the events, and the review queue.
* :mod:`.service` - operations and their rules (write gate, no self-approval, imports never
  promote, the verifier never assesses claims).
* :mod:`.research` / :mod:`.yamlsub` - the research-import format.
* :mod:`.goldens` - the reference citations in ``goldens/`` and their runner.

Span integrity and re-anchoring live in :mod:`timelinexray.verify`. See
``docs/findings-memory.md`` for the full contract.
"""

from .ledger import Event, Ledger, LogReport, verify_log
from .model import (
    ACTIVE_WORKFLOWS,
    EVENT_TYPES,
    EVIDENCE_CLASSES,
    SCOPES,
    STATUSES,
    WORKFLOWS,
    Actor,
)
from .service import ENV_FINDINGS, VERIFIER_ACTOR, FindingsMemory, ledger_directory
from .state import FindingState, View, project

__all__ = [
    "ACTIVE_WORKFLOWS",
    "ENV_FINDINGS",
    "EVENT_TYPES",
    "EVIDENCE_CLASSES",
    "SCOPES",
    "STATUSES",
    "VERIFIER_ACTOR",
    "WORKFLOWS",
    "Actor",
    "Event",
    "FindingState",
    "FindingsMemory",
    "Ledger",
    "LogReport",
    "View",
    "ledger_directory",
    "project",
    "verify_log",
]
