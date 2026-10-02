"""Optional exports of recorded findings for other tools (``txray export ...``).

Nothing here runs unless a user asks for it with ``txray export``; no other command, the
MCP server or any default imports it. The one target is a folder of Markdown notes that a
Context Layer vault can index (``txray export context-layer``): TimelineXray only writes
Markdown files; it does not import, depend on, configure or start Context Layer.

* :mod:`.notes`  - the notes, rendered as a pure function of the findings ledger's view;
* :mod:`.writer` - the output directory checks, the manifest and the atomic writes.

The ledger is read with :meth:`timelinexray.findings.Ledger.read_events` (hash chain
verified, shared lock of an existing lock file, nothing written). No source bytes and no
analytics data are read; of the snapshot store only the pin records are read (to resolve
``commit`` and to know the newest pinned commit, which decides what is current). See
``docs/context-layer.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..findings import Ledger, project
from ..snapshot.store import SnapshotStore
from .notes import NoteSet, build_notes
from .writer import MANIFEST_NAME, WriteResult, check_destination, check_ledger, write_files


@dataclass
class ExportResult:
    out: Path
    notes: NoteSet
    write: WriteResult

    def to_dict(self) -> dict[str, Any]:
        return {"out": str(self.out), **self.notes.summary(), "files": self.write.to_dict()}


def export_context_layer(
    out: Path | str,
    *,
    store_root: Path,
    ledger: Path,
    commit: str | None = None,
    include_stale: bool = False,
) -> ExportResult:
    """Write the findings of ``ledger`` as Context Layer vault notes into ``out``.

    ``commit`` (7-40 hex digits of a commit pinned in the store at ``store_root``) asks for
    freshness at that commit instead of each finding's latest check. Every refusal happens
    before anything is written.
    """
    check_ledger(Path(ledger))
    destination = check_destination(Path(out), store_root=Path(store_root), ledger=Path(ledger))
    store = SnapshotStore(store_root)
    full = store.get_pin(commit).commit if commit is not None else None
    view = project(Ledger(ledger).read_events())
    notes = build_notes(view, pins=store.list_pins(), commit=full, include_stale=include_stale)
    result = write_files(destination, notes.files, head=view.head)
    return ExportResult(destination, notes, result)


__all__ = ["MANIFEST_NAME", "ExportResult", "export_context_layer"]
