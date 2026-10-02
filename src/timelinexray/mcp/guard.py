"""What the MCP server may serve: one snapshot store, never an analytics dataset.

* :class:`StoreGuard` pins the server to one snapshot store directory (resolved once, at
  start). Every file the tools open (pin records, manifests, mirrors, the index database)
  must resolve to a location inside it, so a tampered pin record or a symlink cannot make
  the server read elsewhere.
* The server refuses to start, and refuses every later call, when the store or any
  directory above it contains an analytics dataset: a ``dataset.json`` declaring
  ``"format": "timelinexray/analytics-dataset/v1"`` (or any ``dataset.json`` that cannot be
  read safely). The values are restated here so that this package never imports the
  analytics package; a test keeps them equal to the analytics module's constants.
* :func:`check_repo_path` normalises tool path arguments: repository-relative, no absolute
  or drive paths, no ``.``/``..``/empty segments, no backslashes, NUL or control characters.
* The findings ledger the server may read (Milestone 4b) is chosen by the operator, never
  by a client: :class:`StoreGuard` records its directory, applies the same analytics-dataset
  refusal to it (in it or above it), and keeps the files the findings tools open inside it.
* :func:`redact` removes local filesystem locations (store, ledger, home directory) from any
  text sent to a client.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from ..errors import InvalidInput, Refused

#: Same values as ``analytics.dataset.DATASET_FORMAT`` / ``DATASET_FILE`` (a test checks).
ANALYTICS_DATASET_FORMAT = "timelinexray/analytics-dataset/v1"
ANALYTICS_DATASET_FILE = "dataset.json"

MAX_PATH_CHARS = 1024
_DATASET_READ_LIMIT = 1 << 20
_DRIVE = re.compile(r"^[A-Za-z]:")


def _has_control(text: str) -> bool:
    return any(ord(char) < 0x20 or 0x7F <= ord(char) < 0xA0 for char in text)


def check_repo_path(value: str, what: str = "path", *, prefix: bool = False) -> str:
    """Return ``value`` if it is a normalised repository-relative path, else raise.

    With ``prefix=True`` a single trailing ``/`` is allowed (a directory prefix).
    """
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"{what} must be a non-empty repository-relative path")
    if len(value) > MAX_PATH_CHARS:
        raise InvalidInput(f"{what} is longer than {MAX_PATH_CHARS} characters")
    if "\0" in value:
        raise InvalidInput(f"{what} must not contain NUL")
    if _has_control(value):
        raise InvalidInput(f"{what} must not contain control characters")
    try:  # U+DC80..U+DCFF stand for the bytes of a non-UTF-8 upstream name; no other lone
        value.encode("utf-8", "surrogateescape")  # surrogate can name a path
    except UnicodeEncodeError:
        raise InvalidInput(f"{what} contains a character that is not valid text (a lone surrogate)") from None
    if value.startswith(("/", "~")) or _DRIVE.match(value):
        raise InvalidInput(f"{what} must be repository-relative, not absolute: {value[:80]!r}")
    if "\\" in value:
        raise InvalidInput(f"{what} must use '/' separators and contain no backslash")
    body = value[:-1] if prefix and value.endswith("/") else value
    if any(part in ("", ".", "..") for part in body.split("/")):
        raise InvalidInput(
            f"{what} {value[:80]!r} is not normalised: empty, '.' and '..' segments are not allowed"
        )
    return value


def _dataset_marker(path: Path) -> str | None:
    """Why ``path`` (a file named dataset.json) must be refused, or None if it is harmless."""
    try:
        info = os.lstat(path)
    except OSError as exc:
        return f"cannot inspect {ANALYTICS_DATASET_FILE}: {exc.strerror}"
    if not stat.S_ISREG(info.st_mode):
        return f"{ANALYTICS_DATASET_FILE} is not a regular file"
    try:
        with open(path, "rb") as handle:
            data = handle.read(_DATASET_READ_LIMIT + 1)
    except OSError as exc:
        return f"cannot read {ANALYTICS_DATASET_FILE}: {exc.strerror}"
    if ANALYTICS_DATASET_FORMAT.encode("ascii") in data:
        return f"{ANALYTICS_DATASET_FILE} declares {ANALYTICS_DATASET_FORMAT}"
    try:
        meta = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None  # not JSON at all, and it does not name the analytics format
    if isinstance(meta, dict) and meta.get("format") == ANALYTICS_DATASET_FORMAT:
        return f"{ANALYTICS_DATASET_FILE} declares {ANALYTICS_DATASET_FORMAT}"  # pragma: no cover
    return None


def find_analytics_dataset(root: Path, *, walk: bool = True) -> tuple[Path, str] | None:
    """The first analytics dataset marker in ``root``'s tree or above it, if any.

    With ``walk=False`` only ``root`` itself and the directories above it are checked (for a
    directory that does not exist yet, whose parent's whole tree is not of interest).
    """
    for ancestor in (root, *root.parents):
        candidate = ancestor / ANALYTICS_DATASET_FILE
        if os.path.lexists(candidate):
            reason = _dataset_marker(candidate)
            if reason:
                return candidate, reason
    if not walk:
        return None
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        if ANALYTICS_DATASET_FILE in filenames or ANALYTICS_DATASET_FILE in dirnames:
            candidate = Path(directory) / ANALYTICS_DATASET_FILE
            reason = _dataset_marker(candidate)
            if reason:
                return candidate, reason
    return None


class StoreGuard:
    """The one snapshot store (and findings ledger) a server instance may read, and the
    checks that keep it so.

    ``ledger`` is the findings ledger directory chosen by the operator (``--ledger``,
    ``$TXRAY_FINDINGS`` or ``<store>/findings``); it may not exist yet. ``ledger_problem``
    says why no ledger is available when ``ledger`` is ``None`` (for example, the default
    location lies inside a git working tree).
    """

    def __init__(
        self,
        root: Path | str,
        ledger: Path | str | None = None,
        *,
        ledger_problem: str | None = None,
    ) -> None:
        given = Path(root).expanduser()
        if not given.is_dir():
            raise Refused(f"snapshot store {given} does not exist or is not a directory")
        self.given = given
        self.root = given.resolve(strict=True)
        self.home = Path.home()
        self.ledger_given = (Path(os.path.abspath(Path(ledger).expanduser()))
                             if ledger is not None else None)
        self.ledger_root = (self.ledger_given.resolve(strict=False)
                            if self.ledger_given is not None else None)
        self.ledger_problem = ledger_problem
        self.check_datasets()

    def check_datasets(self) -> None:
        """Refuse the whole store if an analytics dataset lives in or above it (or in or
        above the findings ledger)."""
        for what, root in (("snapshot store", self.root), ("findings ledger", self.ledger_root)):
            if root is None:
                continue
            found = find_analytics_dataset(root)
            if found is not None:
                path, reason = found
                where = "inside" if root in (path.parent, *path.parent.parents) else "above"
                raise Refused(
                    f"refused to serve the {what}: an analytics dataset lies {where} it "
                    f"({reason}). Analytics data is private and never served over MCP; keep "
                    f"datasets outside the {what} and its parent directories."
                )

    def inside(self, path: Path | str, what: str) -> Path:
        """``path`` resolved, provided it lies inside the store; otherwise :class:`Refused`."""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve(strict=False)
        if resolved != self.root and self.root not in resolved.parents:
            raise Refused(f"refused: {what} resolves outside the snapshot store")
        return resolved

    def inside_ledger(self, path: Path | str, what: str) -> Path:
        """``path`` resolved, provided it lies inside the findings ledger directory."""
        if self.ledger_root is None:  # pragma: no cover - callers check first
            raise Refused("refused: no findings ledger is configured")
        resolved = Path(path).resolve(strict=False)
        if resolved != self.ledger_root and self.ledger_root not in resolved.parents:
            raise Refused(f"refused: {what} resolves outside the findings ledger directory")
        return resolved

    def redact(self, text: str) -> str:
        """``text`` with ledger, store and home directory locations replaced by placeholders
        (longest location first, so a ledger inside the store is shown as ``<ledger>``)."""
        pairs = [(str(self.given), "<store>"), (str(self.root), "<store>"), (str(self.home), "~")]
        for ledger in (self.ledger_given, self.ledger_root):
            if ledger is not None:
                pairs.append((str(ledger), "<ledger>"))
        for location, label in sorted(pairs, key=lambda pair: len(pair[0]), reverse=True):
            if location and location not in ("/", "."):
                text = text.replace(location, label)
        return text
