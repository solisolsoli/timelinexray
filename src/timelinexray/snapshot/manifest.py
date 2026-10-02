"""Per-commit manifest: every path of a commit's tree, classified, with blob identities.

The manifest is a pure function of the commit's objects and the classifier settings. It
contains no timestamps of its own, so building it twice yields identical bytes. It is
serialised as JSON (sorted keys, one-space indent, ASCII with ``\\u`` escapes, trailing
newline); its identity is the SHA-256 of those exact bytes, stored beside it in
``<commit>.json.sha256`` in ``shasum`` format.

Paths are git's raw path bytes decoded as UTF-8 with ``surrogateescape``, so a path that is
not valid UTF-8 still round-trips to its exact bytes.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from ..errors import GitError, IntegrityError
from ..gitio import GitRepo
from .classify import (
    CLASSIFICATIONS,
    REASONS,
    ClassifierConfig,
    classify,
    license_hints,
    license_kind,
)

MANIFEST_SCHEMA = "timelinexray/manifest/v1"

_LICENSE_HINT_MAX_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    path: str
    mode: str
    type: str  # "blob" or "commit" (a gitlink)
    oid: str
    size: int | None
    sha256: str | None
    classification: str
    reason: str | None
    rule: str | None
    language: str | None
    utf8: bool | None

    @property
    def is_symlink(self) -> bool:
        return self.mode == "120000"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LicenseFile:
    path: str
    kind: str
    oid: str
    sha256: str | None
    size: int | None
    classification: str
    hints: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["hints"] = list(self.hints)
        return data


def manifest_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _path_key(path: str) -> bytes:
    return path.encode("utf-8", "surrogateescape")


@dataclass(frozen=True)
class Manifest:
    commit: str
    tree: str
    parents: tuple[str, ...]
    committer_time: str
    object_format: str
    classifier: dict[str, int]
    entries: tuple[ManifestEntry, ...]
    licenses: tuple[LicenseFile, ...]
    _index: dict[str, ManifestEntry] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        index = {entry.path: entry for entry in self.entries}
        if len(index) != len(self.entries):
            raise IntegrityError(f"manifest for {self.commit} lists a path more than once")
        object.__setattr__(self, "_index", index)

    def entry(self, path: str) -> ManifestEntry | None:
        return self._index.get(path)

    def counts(self) -> dict[str, Any]:
        by_class = Counter(entry.classification for entry in self.entries)
        by_reason = Counter(entry.reason for entry in self.entries if entry.reason)
        by_language = Counter(entry.language for entry in self.entries if entry.language)
        return {
            "total": len(self.entries),
            "classification": {name: by_class.get(name, 0) for name in CLASSIFICATIONS},
            "excluded_reason": {name: by_reason.get(name, 0) for name in REASONS},
            "language": dict(sorted(by_language.items())),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": MANIFEST_SCHEMA,
            "commit": self.commit,
            "tree": self.tree,
            "parents": list(self.parents),
            "committer_time": self.committer_time,
            "object_format": self.object_format,
            "classifier": dict(self.classifier),
            "counts": self.counts(),
            "licenses": [item.to_dict() for item in self.licenses],
            "entries": [entry.to_dict() for entry in self.entries],
        }

    def to_json_bytes(self) -> bytes:
        text = json.dumps(self.to_dict(), indent=1, sort_keys=True, ensure_ascii=True)
        return text.encode("ascii") + b"\n"

    @classmethod
    def from_json_bytes(cls, data: bytes) -> "Manifest":
        try:
            raw = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrityError(f"manifest is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("schema") != MANIFEST_SCHEMA:
            raise IntegrityError(f"manifest schema is not {MANIFEST_SCHEMA}")
        try:
            manifest = cls(
                commit=raw["commit"],
                tree=raw["tree"],
                parents=tuple(raw["parents"]),
                committer_time=raw["committer_time"],
                object_format=raw["object_format"],
                classifier=dict(raw["classifier"]),
                entries=tuple(ManifestEntry(**item) for item in raw["entries"]),
                licenses=tuple(
                    LicenseFile(**{**item, "hints": tuple(item["hints"])})
                    for item in raw["licenses"]
                ),
            )
        except (KeyError, TypeError) as exc:
            raise IntegrityError(f"manifest is missing or has malformed fields: {exc}") from exc
        if raw.get("counts") != manifest.counts():
            raise IntegrityError("manifest counts do not match its entries")
        return manifest


def build_manifest(
    repo: GitRepo, commit: str, config: ClassifierConfig = ClassifierConfig()
) -> Manifest:
    """Classify every path of ``commit`` (a full commit id present in ``repo``)."""
    info = repo.commit_info(commit)
    tree_entries = repo.ls_tree(commit)
    scans = repo.scan_blobs(
        (item.oid for item in tree_entries if item.type == "blob"), head_bytes=config.head_bytes
    )
    entries: list[ManifestEntry] = []
    for item in tree_entries:
        path = item.path.decode("utf-8", "surrogateescape")
        if item.type == "blob":
            scan = scans[item.oid]
            if item.size is not None and scan.size != item.size:
                raise IntegrityError(f"{path}: tree lists {item.size} bytes, blob has {scan.size}")
            verdict = classify(path, item.mode, item.type, scan.size, scan.head, config)
            entries.append(
                ManifestEntry(
                    path, item.mode, item.type, item.oid, scan.size, scan.sha256,
                    verdict.classification, verdict.reason, verdict.rule, verdict.language,
                    scan.utf8,
                )
            )
        elif item.type == "commit":
            verdict = classify(path, item.mode, item.type, None, b"", config)
            entries.append(
                ManifestEntry(
                    path, item.mode, item.type, item.oid, None, None,
                    verdict.classification, verdict.reason, verdict.rule, verdict.language,
                    None,
                )
            )
        else:
            raise GitError(f"unexpected tree entry type {item.type!r} at {path}")
    entries.sort(key=lambda entry: _path_key(entry.path))

    license_entries = [(entry, license_kind(entry.path)) for entry in entries]
    license_entries = [(entry, kind) for entry, kind in license_entries if kind]
    readable = sorted(
        {
            entry.oid
            for entry, _ in license_entries
            if entry.type == "blob"
            and not entry.is_symlink
            and (entry.size or 0) <= _LICENSE_HINT_MAX_BYTES
        }
    )
    contents = dict(repo.read_blobs(readable)) if readable else {}
    licenses = tuple(
        LicenseFile(
            path=entry.path,
            kind=kind,
            oid=entry.oid,
            sha256=entry.sha256,
            size=entry.size,
            classification=entry.classification,
            hints=license_hints(contents[entry.oid]) if entry.oid in contents else (),
        )
        for entry, kind in license_entries
    )
    return Manifest(
        commit=info.commit,
        tree=info.tree,
        parents=info.parents,
        committer_time=info.committer_time,
        object_format="sha1" if len(info.commit) == 40 else "sha256",
        classifier=config.to_dict(),
        entries=tuple(entries),
        licenses=licenses,
    )
