"""The snapshot store: bare mirrors, pins and manifests on local disk.

Layout under the store root (default ``$TXRAY_STORE``, else
``$XDG_CACHE_HOME/timelinexray``, else ``~/.cache/timelinexray``)::

    mirrors/<name>-<urlhash>.git/     bare mirror of one upstream URL (heads and tags)
    pins/<commit>.json                pin record: upstream URL, mirror, manifest hash
    manifests/<commit>.json           canonical manifest bytes
    manifests/<commit>.json.sha256    "<sha256>  <commit>.json" (shasum -c compatible)

Keep the store outside any agent's project working tree: upstream files must never enter
an instruction-loading hierarchy. Pinned commits are protected in their mirror by a ref
``refs/txray/pins/<commit>`` so later fetches (which prune upstream-deleted branches) can
never make a pinned commit unreachable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__
from ..errors import IntegrityError, InvalidInput, NotFound, Refused, TxrayError
from ..fsutil import atomic_write
from ..gitio import GitRepo, is_hex
from ..netguard import DEFAULT_UPSTREAM_URL, Allowlist, fetch
from ..span import SpanRead, read_span
from .classify import ClassifierConfig
from .manifest import Manifest, ManifestEntry, build_manifest, manifest_digest

ENV_STORE = "TXRAY_STORE"
PIN_SCHEMA = "timelinexray/pin/v1"
MIRROR_UPSTREAM_KEY = "txray.upstream"
PIN_REF_PREFIX = "refs/txray/pins/"

_MIN_COMMIT_PREFIX = 7
#: Longest repository path accepted as input (characters); longer names are refused.
MAX_REPO_PATH = 4096
#: Largest blob read into memory for a span; larger blobs are refused before reading.
MAX_READ_BYTES = 64 * 1024 * 1024


def default_store_root(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    if environ.get(ENV_STORE):
        return Path(environ[ENV_STORE]).expanduser()
    base = environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(base).expanduser() / "timelinexray"


def validate_commit_input(commit: str) -> str:
    """Accept 7-40 lowercase hex digits (or 64 for SHA-256 repositories)."""
    text = commit.strip().lower() if isinstance(commit, str) else ""
    if not is_hex(text) or not (_MIN_COMMIT_PREFIX <= len(text) <= 40 or len(text) == 64):
        raise InvalidInput(
            f"invalid commit {commit!r}: give a commit id of 7 to 40 hexadecimal digits "
            "(branch and tag names are not accepted, so a pin never follows a moving ref)"
        )
    return text


def validate_repo_path(path: str) -> str:
    """Reject paths that are not normalised and repository-relative."""
    if not isinstance(path, str) or not path:
        raise InvalidInput("path must be a non-empty repository-relative path")
    if len(path) > MAX_REPO_PATH:
        raise InvalidInput(f"path is longer than {MAX_REPO_PATH} characters")
    if "\0" in path:
        raise InvalidInput("path must not contain NUL bytes")
    if path.startswith("/"):
        raise InvalidInput(
            f"absolute paths are not allowed: {path!r}; give a repository-relative path"
        )
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise InvalidInput(
            f"path {path!r} is not normalised: empty, '.' and '..' segments are not allowed"
        )
    return path


@dataclass(frozen=True, slots=True)
class PinRecord:
    commit: str
    tree: str
    committer_time: str
    upstream_url: str
    mirror: str  # relative to the store root
    manifest: str  # relative to the store root
    manifest_sha256: str
    entry_count: int
    pinned_at: str
    tool_version: str

    def to_dict(self) -> dict[str, Any]:
        return {"schema": PIN_SCHEMA, **asdict(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PinRecord":
        if data.get("schema") != PIN_SCHEMA:
            raise IntegrityError(f"pin record schema is not {PIN_SCHEMA}")
        fields = {key: value for key, value in data.items() if key != "schema"}
        try:
            return cls(**fields)
        except TypeError as exc:
            raise IntegrityError(f"malformed pin record: {exc}") from exc


@dataclass(frozen=True, slots=True)
class PinResult:
    pin: PinRecord
    manifest: Manifest
    fetched: bool
    created: bool


class SnapshotStore:
    """Local store of upstream mirrors, commit pins and manifests."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else default_store_root()
        self._manifests: dict[str, Manifest] = {}
        self._repos: dict[str, GitRepo] = {}

    # -- paths ---------------------------------------------------------------------

    def mirror_path(self, url: str) -> Path:
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        if tail.endswith(".git"):
            tail = tail[:-4]
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", tail).strip(".-")[:40] or "repo"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        return self.root / "mirrors" / f"{name}-{digest}.git"

    def _pin_path(self, commit: str) -> Path:
        return self.root / "pins" / f"{commit}.json"

    def _manifest_path(self, commit: str) -> Path:
        return self.root / "manifests" / f"{commit}.json"

    # -- mirrors -------------------------------------------------------------------

    def open_mirror(self, url: str) -> GitRepo:
        """Open (creating if needed) the bare mirror for an already-checked ``url``."""
        path = self.mirror_path(url)
        if not path.exists():
            repo = GitRepo.init_bare(path)
            for key, value in (
                (MIRROR_UPSTREAM_KEY, url),
                ("gc.auto", "0"),
                ("maintenance.auto", "false"),
                ("core.logAllRefUpdates", "always"),
                ("fetch.fsckObjects", "true"),
            ):
                repo.config_set(key, value)
            return repo
        repo = GitRepo(path)
        recorded = repo.config_get(MIRROR_UPSTREAM_KEY)
        if recorded != url:
            raise IntegrityError(f"mirror {path} records upstream {recorded!r}, expected {url!r}")
        return repo

    # -- pinning -------------------------------------------------------------------

    def pin(
        self,
        commit: str,
        upstream: str = DEFAULT_UPSTREAM_URL,
        *,
        allowlist: Allowlist,
        classifier: ClassifierConfig = ClassifierConfig(),
    ) -> PinResult:
        """Make ``commit`` available locally, protect it, and record its manifest.

        The mirror is fetched (through :func:`timelinexray.netguard.fetch`) only when the
        commit is not already present. Re-pinning rebuilds the manifest and requires it to
        match the stored bytes exactly.
        """
        url = allowlist.check(upstream)
        wanted = validate_commit_input(commit)
        for record in self.list_pins():
            if record.commit.startswith(wanted) and record.upstream_url != url:
                raise Refused(
                    f"commit {record.commit} is already pinned from {record.upstream_url}; "
                    "use a separate store for another upstream"
                )
        repo = self.open_mirror(url)
        full = repo.resolve_commit(wanted)
        fetched = False
        if full is None:
            fetch(url, repo.git_dir, allowlist)
            fetched = True
            full = repo.resolve_commit(wanted)
            if full is None:
                raise NotFound(
                    f"commit {wanted} was not found in {url} (branches and tags were fetched), "
                    "or the abbreviation is ambiguous"
                )
        existing = self._read_pin(full)
        if existing is not None and existing.upstream_url != url:
            raise Refused(
                f"commit {full} is already pinned from {existing.upstream_url}; "
                "use a separate store for another upstream"
            )
        repo.update_ref(PIN_REF_PREFIX + full, full)

        manifest = build_manifest(repo, full, classifier)
        data = manifest.to_json_bytes()
        digest = manifest_digest(data)
        manifest_path = self._manifest_path(full)
        if manifest_path.exists():
            stored = manifest_path.read_bytes()
            if stored != data:
                raise IntegrityError(
                    f"stored manifest {manifest_path} differs from the manifest rebuilt from the "
                    f"mirror (stored sha256 {manifest_digest(stored)}, rebuilt {digest})"
                )
        else:
            atomic_write(manifest_path, data)
        atomic_write(
            manifest_path.with_name(manifest_path.name + ".sha256"),
            f"{digest}  {manifest_path.name}\n".encode("ascii"),
        )
        record = PinRecord(
            commit=full,
            tree=manifest.tree,
            committer_time=manifest.committer_time,
            upstream_url=url,
            mirror=repo.git_dir.relative_to(self.root).as_posix(),
            manifest=manifest_path.relative_to(self.root).as_posix(),
            manifest_sha256=digest,
            entry_count=len(manifest.entries),
            pinned_at=existing.pinned_at if existing else _utc_now(),
            tool_version=existing.tool_version if existing else __version__,
        )
        if record != existing:
            atomic_write(
                self._pin_path(full),
                (json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n").encode("ascii"),
            )
        self._manifests[full] = manifest
        return PinResult(record, manifest, fetched=fetched, created=existing is None)

    def _read_pin(self, commit: str) -> PinRecord | None:
        path = self._pin_path(commit)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrityError(f"pin record {path} is not valid JSON: {exc}") from exc
        record = PinRecord.from_dict(data)
        if record.commit != commit:
            raise IntegrityError(f"pin record {path} names commit {record.commit}")
        return record

    def list_pins(self) -> list[PinRecord]:
        directory = self.root / "pins"
        if not directory.is_dir():
            return []
        records = []
        for path in sorted(directory.glob("*.json")):
            record = self._read_pin(path.stem)
            if record is not None:
                records.append(record)
        return records

    def get_pin(self, commit: str) -> PinRecord:
        """The pin whose commit id starts with ``commit`` (7+ hex digits)."""
        prefix = validate_commit_input(commit)
        directory = self.root / "pins"
        names = sorted(p.stem for p in directory.glob("*.json")) if directory.is_dir() else []
        matches = [name for name in names if name.startswith(prefix)]
        if not matches:
            raise NotFound(
                f"commit {prefix} is not pinned in store {self.root}; run: txray pin {prefix}"
            )
        if len(matches) > 1:
            raise InvalidInput(
                f"commit prefix {prefix} matches {len(matches)} pins; give more digits"
            )
        record = self._read_pin(matches[0])
        assert record is not None
        return record

    # -- manifests and blobs -------------------------------------------------------

    def load_manifest(self, commit: str) -> tuple[PinRecord, Manifest]:
        """Load a pinned commit's manifest after checking its bytes against both hashes."""
        pin = self.get_pin(commit)
        cached = self._manifests.get(pin.commit)
        if cached is not None:
            return pin, cached
        path = self.root / pin.manifest
        try:
            data = path.read_bytes()
            sidecar = path.with_name(path.name + ".sha256").read_text("ascii")
        except FileNotFoundError as exc:
            raise IntegrityError(
                f"manifest file missing for pinned commit {pin.commit}: {exc}"
            ) from exc
        digest = manifest_digest(data)
        recorded = sidecar.split()[0] if sidecar.split() else ""
        if digest != recorded or digest != pin.manifest_sha256:
            raise IntegrityError(
                f"manifest {path} has sha256 {digest}, but its sidecar records {recorded} "
                f"and the pin records {pin.manifest_sha256}"
            )
        manifest = Manifest.from_json_bytes(data)
        if manifest.commit != pin.commit:
            raise IntegrityError(f"manifest {path} describes commit {manifest.commit}")
        self._manifests[pin.commit] = manifest
        return pin, manifest

    def repo_for(self, pin: PinRecord) -> GitRepo:
        """The pin's mirror, after checking (once per store object) that it is the right one."""
        repo = self._repos.get(pin.commit)
        if repo is None:
            repo = GitRepo(self.root / pin.mirror)
            recorded = repo.config_get(MIRROR_UPSTREAM_KEY)
            if recorded != pin.upstream_url:
                raise IntegrityError(f"mirror {pin.mirror} does not belong to {pin.upstream_url}")
            self._repos[pin.commit] = repo
        return repo

    def lookup(self, commit: str, path: str) -> tuple[PinRecord, ManifestEntry]:
        """Manifest entry for an exact repository-relative path at a pinned commit."""
        validate_repo_path(path)
        pin, manifest = self.load_manifest(commit)
        entry = manifest.entry(path)
        if entry is None:
            raise NotFound(
                f"path {path!r} is not in the manifest of {pin.commit} "
                "(paths are exact, case-sensitive and repository-relative)"
            )
        return pin, entry

    def read_blob(self, commit: str, path: str) -> tuple[PinRecord, ManifestEntry, bytes]:
        """Bytes of a regular text blob, verified against the manifest's size and SHA-256."""
        pin, entry = self.lookup(commit, path)
        self._readable(entry, path)
        data = self.repo_for(pin).read_blob(entry.oid)
        return pin, entry, self._checked(entry, path, data)

    def read_blobs(
        self, commit: str, paths: Iterable[str]
    ) -> tuple[PinRecord, dict[str, tuple[ManifestEntry, bytes] | TxrayError]]:
        """:meth:`read_blob` for several paths of one commit through one git process.

        The result maps each distinct path to ``(entry, bytes)`` or to the error
        :meth:`read_blob` would raise for it (a missing path, a blob that is not text, a
        manifest mismatch), so one unreadable path never hides the others.
        """
        pin, manifest = self.load_manifest(commit)
        results: dict[str, tuple[ManifestEntry, bytes] | TxrayError] = {}
        wanted: list[tuple[str, ManifestEntry]] = []
        for path in dict.fromkeys(paths):
            try:
                validate_repo_path(path)
                entry = manifest.entry(path)
                if entry is None:
                    raise NotFound(
                        f"path {path!r} is not in the manifest of {pin.commit} "
                        "(paths are exact, case-sensitive and repository-relative)"
                    )
                self._readable(entry, path)
            except TxrayError as exc:
                results[path] = exc
                continue
            wanted.append((path, entry))
        if wanted:
            oids = list(dict.fromkeys(entry.oid for _, entry in wanted))
            data_by_oid = dict(self.repo_for(pin).read_blobs(oids))
            for path, entry in wanted:
                try:
                    results[path] = (entry, self._checked(entry, path, data_by_oid[entry.oid]))
                except IntegrityError as exc:
                    results[path] = exc
        return pin, results

    @staticmethod
    def _readable(entry: ManifestEntry, path: str) -> None:
        if entry.type != "blob":
            raise Refused(f"{path} is a submodule (gitlink) entry and has no blob to read")
        if entry.is_symlink:
            raise Refused(f"{path} is a symlink; symlinks are recorded but never followed or read")
        if entry.reason == "binary":
            raise Refused(f"{path} is classified binary; spans can only be read from text blobs")
        if entry.size is not None and entry.size > MAX_READ_BYTES:
            raise Refused(f"{path} is {entry.size} bytes; spans are read only from blobs of at "
                          f"most {MAX_READ_BYTES} bytes")

    @staticmethod
    def _checked(entry: ManifestEntry, path: str, data: bytes) -> bytes:
        if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise IntegrityError(f"blob {entry.oid} for {path} does not match its manifest entry")
        return data

    def read_span(
        self, commit: str, path: str, start: int, end: int, *, anchor: str | None = None
    ) -> SpanRead:
        """Exact bytes of lines ``start-end`` of ``path`` at a pinned commit."""
        pin, entry, data = self.read_blob(commit, path)
        return read_span(
            data, start, end, commit=pin.commit, path=entry.path, blob_oid=entry.oid, anchor=anchor
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

