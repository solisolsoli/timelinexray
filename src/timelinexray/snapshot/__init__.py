"""Snapshot store (Milestone 1): bare mirrors, commit pins, manifests and license inventory.

Later milestones consume three things from here:

* :class:`SnapshotStore` - resolves a pinned commit to its verified :class:`Manifest` and
  its mirror (:class:`timelinexray.gitio.GitRepo`), and reads exact spans;
* :class:`Manifest` - every path of the commit with classification, blob id, size and
  SHA-256; ``parsed-candidate`` and ``text`` entries are what the index covers;
* :mod:`timelinexray.span` - the byte-to-line contract behind every citation.
"""

from .classify import (
    CLASSIFICATIONS,
    CLASSIFIER_VERSION,
    EXCLUDED,
    PARSED_CANDIDATE,
    REASONS,
    TEXT,
    ClassifierConfig,
    classify,
    guess_language,
    license_hints,
    license_kind,
)
from .manifest import MANIFEST_SCHEMA, LicenseFile, Manifest, ManifestEntry, build_manifest
from .store import (
    ENV_STORE,
    PinRecord,
    PinResult,
    SnapshotStore,
    default_store_root,
    validate_commit_input,
    validate_repo_path,
)

__all__ = [
    "CLASSIFICATIONS",
    "CLASSIFIER_VERSION",
    "ENV_STORE",
    "EXCLUDED",
    "MANIFEST_SCHEMA",
    "PARSED_CANDIDATE",
    "REASONS",
    "TEXT",
    "ClassifierConfig",
    "LicenseFile",
    "Manifest",
    "ManifestEntry",
    "PinRecord",
    "PinResult",
    "SnapshotStore",
    "build_manifest",
    "classify",
    "default_store_root",
    "guess_language",
    "license_hints",
    "license_kind",
    "validate_commit_input",
    "validate_repo_path",
]
