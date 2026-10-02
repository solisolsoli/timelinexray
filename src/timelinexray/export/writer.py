"""Where an export may be written, and how it is written there.

:func:`check_destination` refuses an output directory that is a symbolic link, whose parent
does not exist, that lies inside (or is) a TimelineXray source tree or the installed
package, that lies inside or contains the snapshot store or the findings ledger, or that
lies inside, above or around an analytics dataset. Paths are compared after resolving the
symbolic links of existing parent directories.

:func:`write_files` writes the rendered notes inside that directory only:

* every file is replaced atomically (:func:`timelinexray.fsutil.atomic_write`), and a file
  whose bytes are already right is not rewritten;
* ``.txray-export.json`` (the manifest) lists every file an export created with its
  SHA-256. A re-export replaces or removes only files listed there, only while their bytes
  are still the recorded ones, and never touches anything else: a file that exists but was
  not created by an export, or an exported file that was edited afterwards, is refused;
* the manifest is written before any note (listing the files about to be written as
  ``pending``) and again after the last one, so an interrupted export can always be
  re-run;
* symbolic links are never followed: a link where the manifest, the notes folder or a
  note would be is refused.

This is path validation, not a sandbox against another process that swaps directories
while an export runs.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import __version__
from ..errors import Refused
from ..fsutil import atomic_write, check_output_directory, is_within, resolved_path
from ..mcp.guard import find_analytics_dataset
from .notes import FINDINGS_DIR, INDEX_NAME, NOTE_PREFIX, README_NAME

MANIFEST_NAME = ".txray-export.json"
MANIFEST_SCHEMA = "timelinexray/context-layer-export-manifest/v1"
MANIFEST_LIMIT = 4 << 20
_SHA256 = re.compile(r"[0-9a-f]{64}")
_OWNED = re.compile(
    re.escape(README_NAME) + "|" + re.escape(INDEX_NAME) + "|"
    + re.escape(FINDINGS_DIR) + "/" + re.escape(NOTE_PREFIX) + r"[A-Za-z0-9._-]{1,200}\.md"
)


def owned_name(name: object) -> bool:
    """Whether ``name`` is a relative path an export can create (and so may replace)."""
    return isinstance(name, str) and _OWNED.fullmatch(name) is not None


# -- destination -----------------------------------------------------------------------------


def check_destination(out: Path, *, store_root: Path, ledger: Path,
                      option: str = "--out") -> Path:
    """The absolute output directory, after every refusal of the module documentation.

    The shared rules are :func:`timelinexray.fsutil.check_output_directory`; an export
    additionally refuses a directory *above* the store or the ledger, because a vault
    indexed there would index the store or ledger files. ``option`` names the argument
    in messages (``--export`` when ``txray update`` refreshes the notes).
    """
    absolute = check_output_directory(out, store_root=store_root, ledger=ledger, option=option)
    real = absolute.resolve(strict=False)
    for what, root in (("snapshot store", resolved_path(store_root)),
                       ("findings ledger", resolved_path(ledger))):
        if real != root and is_within(root, real):
            raise Refused(f"refused: {option} {absolute} contains the {what} {root}; a vault "
                          "indexed there would index the store or ledger files")
    return absolute


def check_ledger(ledger: Path) -> None:
    """The ledger must be a directory that lies in no analytics dataset (as for MCP)."""
    if not ledger.is_dir():
        raise Refused(f"no findings ledger exists at {ledger}; record findings with "
                      "txray findings add / import, or pass --ledger DIR")
    found = find_analytics_dataset(resolved_path(ledger))
    if found is not None:
        raise Refused(f"refused: an analytics dataset lies in or above the findings ledger "
                      f"{ledger} ({found[1]}); analytics data is private and never exported")


# -- manifest --------------------------------------------------------------------------------


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular(path: Path, what: str, limit: int | None = None) -> bytes:
    """The bytes of a regular file, never through a symbolic link."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise Refused(f"refused: cannot read {what} ({exc.strerror})") from None
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise Refused(f"refused: {what} is not a regular file")
        data = handle.read() if limit is None else handle.read(limit + 1)
    if limit is not None and len(data) > limit:
        raise Refused(f"refused: {what} is larger than {limit} bytes")
    return data


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def read_manifest(out: Path) -> dict[str, dict[str, str]] | None:
    """``{"files": ..., "pending": ...}`` of an earlier export in ``out``, or ``None``."""
    path = out / MANIFEST_NAME
    if not os.path.lexists(path):
        return None
    if os.path.islink(path):
        raise Refused(f"refused: the export manifest {MANIFEST_NAME} is a symbolic link")
    data = _read_regular(path, f"the export manifest {MANIFEST_NAME}", MANIFEST_LIMIT)
    problem = f"refused: {MANIFEST_NAME} in {out} is not a valid TimelineXray export manifest"
    try:
        manifest = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise Refused(problem + " (not JSON); nothing was changed") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise Refused(problem + " (unknown schema); nothing was changed")
    parts: dict[str, dict[str, str]] = {}
    for key in ("files", "pending"):
        entries = manifest.get(key, {})
        if not isinstance(entries, dict) or not all(
                owned_name(name) and isinstance(digest, str) and _SHA256.fullmatch(digest)
                for name, digest in entries.items()):
            raise Refused(problem + f" (its {key!r} list names a file an export cannot "
                          "create); nothing was changed")
        parts[key] = dict(entries)
    return parts


def _manifest_bytes(files: dict[str, str], pending: dict[str, str] | None, head: str) -> bytes:
    body: dict[str, Any] = {"schema": MANIFEST_SCHEMA, "files": dict(sorted(files.items())),
                            "timelinexray": __version__, "ledger_head": head}
    if pending:
        body["pending"] = dict(sorted(pending.items()))
    return (json.dumps(body, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


# -- writing ---------------------------------------------------------------------------------


@dataclass
class WriteResult:
    written: list[str]
    unchanged: list[str]
    removed: list[str]
    manifest: str = MANIFEST_NAME

    def to_dict(self) -> dict[str, Any]:
        return {"written": self.written, "unchanged": self.unchanged, "removed": self.removed,
                "manifest": self.manifest}


def _check_folder(out: Path, name: str) -> None:
    """The folders between ``out`` and a file of the export are real directories."""
    parts = name.split("/")[:-1]
    current = out
    for part in parts:
        current = current / part
        if os.path.islink(current):
            raise Refused(f"refused: {current.relative_to(out)} in {out} is a symbolic link")
        if os.path.lexists(current) and not current.is_dir():
            raise Refused(f"refused: {current.relative_to(out)} in {out} is not a directory")


def write_files(out: Path, files: dict[str, bytes], *, head: str) -> WriteResult:
    """Write ``files`` (relative path -> bytes) into ``out`` as the module describes."""
    if not all(owned_name(name) for name in files):  # pragma: no cover - build_notes names
        raise Refused("refused: an export may only write its own txray- files")
    previous = read_manifest(out) if out.is_dir() else None
    old_files = previous["files"] if previous else {}
    old_pending = previous["pending"] if previous else {}
    owned = set(old_files) | set(old_pending)
    new_hashes = {name: _hash(data) for name, data in sorted(files.items())}
    on_disk: dict[str, str] = {}
    for name in sorted(set(files) | owned):
        _check_folder(out, name)
        target = out / name
        if not os.path.lexists(target):
            continue
        if os.path.islink(target):
            raise Refused(f"refused: {name} in {out} is a symbolic link; nothing was changed")
        if name not in owned:
            raise Refused(f"refused: {name} already exists in {out} and was not created by a "
                          "TimelineXray export; move it away and export again. Nothing was "
                          "changed")
        digest = _hash(_read_regular(target, name))
        if digest not in (old_files.get(name), old_pending.get(name)):
            raise Refused(f"refused: {name} in {out} was changed after the last export (its "
                          f"SHA-256 differs from {MANIFEST_NAME}); move your changes to a note "
                          "of your own, delete it and export again. Nothing was changed")
        on_disk[name] = digest
    to_write = [name for name in new_hashes if on_disk.get(name) != new_hashes[name]]
    to_remove = sorted(name for name in owned - set(files) if name in on_disk)
    unchanged = [name for name in new_hashes if name not in to_write]
    final = _manifest_bytes(new_hashes, None, head)
    manifest_path = out / MANIFEST_NAME
    current_manifest = (_read_regular(manifest_path, MANIFEST_NAME, MANIFEST_LIMIT)
                        if os.path.lexists(manifest_path) else None)
    if not to_write and not to_remove and current_manifest == final:
        return WriteResult([], unchanged, [])
    with contextlib.suppress(FileExistsError):
        out.mkdir()
    for folder in sorted({name.rsplit("/", 1)[0] for name in to_write if "/" in name}):
        with contextlib.suppress(FileExistsError):  # checked above: not a link, not a file
            (out / folder).mkdir()
    atomic_write(manifest_path, _manifest_bytes(on_disk, new_hashes, head))
    for name in to_write:
        atomic_write(out / name, files[name])
    for name in to_remove:
        target = out / name
        if os.path.islink(target) or _hash(_read_regular(target, name)) != on_disk[name]:
            raise Refused(f"refused: {name} in {out} changed while the export ran; it was "
                          "left in place")  # pragma: no cover - a concurrent edit
        os.unlink(target)
    atomic_write(manifest_path, final)
    return WriteResult(to_write, unchanged, to_remove)


__all__ = [
    "MANIFEST_NAME",
    "MANIFEST_SCHEMA",
    "WriteResult",
    "check_destination",
    "check_ledger",
    "owned_name",
    "read_manifest",
    "write_files",
]
