"""Fixture templates: expensive stores built once per test run, copied into each class.

A template is a directory built by a function the first time any test asks for it and then
only copied (``shutil.copytree``). With ``TXRAY_TEST_SHARED`` set (``scripts/run_tests.py
--jobs`` does that) every worker process uses the same directory and an exclusive file lock
plus a ``.done`` marker (holding a fingerprint of the code, so a template from an earlier
run of different code is rebuilt) make exactly one of them build; otherwise the template lives in a
temporary directory of this process that is removed at exit.

A template directory is never modified after it is built, and it stays where it was built:
the snapshot store records the ``file://`` URL of its upstream, so a synthetic upstream
repository is shared in place, read-only, while the store is copied. Use a template only
where the test does not look at how the store was built (a fresh-build report, a "not
indexed" state).
"""

from __future__ import annotations

import atexit
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

SHARED_ENV = "TXRAY_TEST_SHARED"

_local_lock = threading.RLock()  # a template may build on another one
_process_dir: Path | None = None
_fingerprint: str | None = None
_ROOT = Path(__file__).resolve().parent.parent


def _code_fingerprint() -> str:
    """SHA-256 of the code a template depends on (the package and the test fixtures), so a
    template left in a shared directory by an earlier run of different code is rebuilt."""
    global _fingerprint
    if _fingerprint is None:
        digest = hashlib.sha256()
        files = sorted((_ROOT / "src" / "timelinexray").rglob("*.py"))
        files += [_ROOT / "tests" / name for name in ("templates.py", "support.py",
                                                      "index_support.py")]
        for path in files:
            if path.is_file():
                digest.update(path.relative_to(_ROOT).as_posix().encode() + b"\0")
                digest.update(path.read_bytes() + b"\0")
        _fingerprint = digest.hexdigest()
    return _fingerprint


def _base() -> Path:
    global _process_dir
    shared = os.environ.get(SHARED_ENV)
    if shared:
        Path(shared).mkdir(parents=True, exist_ok=True)
        return Path(shared)
    with _local_lock:
        if _process_dir is None:
            _process_dir = Path(tempfile.mkdtemp(prefix="txray-templates-")).resolve()
            atexit.register(shutil.rmtree, _process_dir, ignore_errors=True)
        return _process_dir


def template(name: str, build: Callable[[Path], None]) -> Path:
    """The directory of template ``name``, built by ``build(directory)`` on first use."""
    directory = _base() / name
    marker = _base() / (name + ".done")
    with _local_lock, open(_base() / (name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            stamp = _code_fingerprint() + "\n"
            if not marker.exists() or marker.read_text() != stamp:
                marker.unlink(missing_ok=True)  # interrupted, or built by different code
                shutil.rmtree(directory, ignore_errors=True)
                directory.mkdir(parents=True)
                build(directory)
                marker.write_text(stamp)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return directory


def copy_into(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True)


def metadata(directory: Path) -> dict:
    """The JSON a template builder wrote beside its content (ids that its build produced)."""
    return json.loads((directory / "meta.json").read_text("utf-8"))


def write_metadata(directory: Path, data: dict) -> None:
    (directory / "meta.json").write_text(json.dumps(data, sort_keys=True), "utf-8")


# -- the real upstream -------------------------------------------------------------------

OLD = "4c5cfe8f07f1c76d4f04277e803f20e6039f5191"
NEW = "a707cc27ba36d3fa79450c9cffcc48a82d080b02"
LAST = "77d431aabf409ca1c1eed9bec7e2183f7c914e23"


def _upstream_url() -> str:
    from tests.support import file_url, upstream_git_dir

    git_dir = upstream_git_dir()
    assert git_dir is not None, "no local upstream clone"
    return file_url(git_dir.parent if git_dir.name == ".git" else git_dir)


def _pin(store_dir: Path, commits: tuple[str, ...]) -> None:
    from timelinexray.netguard import Allowlist
    from timelinexray.snapshot import SnapshotStore

    url = _upstream_url()
    store = SnapshotStore(store_dir)
    for commit in commits:
        store.pin(commit, url, allowlist=Allowlist([url]))


def _index(store_dir: Path, commits: tuple[str, ...]) -> None:
    from timelinexray.index import CodeIndex
    from timelinexray.snapshot import SnapshotStore

    index = CodeIndex(SnapshotStore(store_dir))
    for commit in commits:
        index.build(commit)


def _built_on(base: str, then: Callable[[Path], None]) -> Callable[[Path], None]:
    def build(directory: Path) -> None:
        copy_into(upstream_template(base) / "store", directory / "store")
        then(directory / "store")

    return build


def upstream_template(kind: str) -> Path:
    """A directory holding ``store/``: ``single`` has 77d431a pinned and indexed; ``pins``
    adds 4c5cfe8 and a707cc2 pinned (only 77d431a indexed); ``indexed`` indexes all three."""
    if kind == "single":
        def build(directory: Path) -> None:
            _pin(directory / "store", (LAST,))
            _index(directory / "store", (LAST,))
        return template("upstream-single", build)
    if kind == "pins":
        return template("upstream-pins", _built_on("single", lambda s: _pin(s, (OLD, NEW))))
    if kind == "indexed":
        return template("upstream-indexed",
                        _built_on("pins", lambda s: _index(s, (OLD, NEW))))
    raise ValueError(kind)


def copy_upstream_store(kind: str, destination: Path) -> Path:
    """Copy the upstream template ``kind`` to ``destination`` (which must not exist)."""
    copy_into(upstream_template(kind) / "store", destination)
    return destination
