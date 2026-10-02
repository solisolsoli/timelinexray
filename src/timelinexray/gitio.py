"""Local, read-mostly access to git object stores.

This module never contacts a network. Network transfers live only in
:mod:`timelinexray.netguard`. Every git process started here:

* runs with a clean environment: inherited ``GIT_*`` variables are dropped, system and
  global git configuration are ignored (so user ``url.*.insteadOf`` rewrites, credential
  helpers, or ``GIT_ALLOW_PROTOCOL`` overrides cannot apply), lazy fetching of missing
  objects is disabled, and messages are in the C locale;
* sets ``protocol.allow=never`` so no transport can be used even by accident;
* disables hooks and fsmonitor;
* may only run a subcommand from :data:`LOCAL_SUBCOMMANDS`.

Blob bytes are always read from the object store (``git cat-file``), never from a working
tree, and every blob read is checked against its object id.
"""

from __future__ import annotations

import codecs
import hashlib
import os
import subprocess
import threading
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import GitError, IntegrityError

#: Subcommands :func:`run_local` accepts. None of them transfers objects over a network.
LOCAL_SUBCOMMANDS = frozenset(
    {"cat-file", "config", "for-each-ref", "init", "ls-tree", "rev-list", "rev-parse",
     "update-ref"}
)

#: Configuration applied to every local git invocation.
LOCAL_HARDENING: tuple[str, ...] = (
    "-c", "protocol.allow=never",
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "core.fsmonitor=false",
)

_CHUNK = 1 << 16


def git_env() -> dict[str, str]:
    """Environment for git subprocesses: inherited ``GIT_*`` removed, user config ignored."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    return env


def _stderr_text(data: bytes) -> str:
    return data.decode("utf-8", "replace").strip()


def run_local(
    git_dir: Path | None,
    args: Sequence[str],
    *,
    input: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run one local git subcommand against ``git_dir`` and return the completed process."""
    if not args or args[0] not in LOCAL_SUBCOMMANDS:
        raise ValueError(f"git subcommand not permitted by gitio: {list(args[:1])!r}")
    argv = ["git"]
    if git_dir is not None:
        argv.append(f"--git-dir={git_dir}")
    argv.extend(LOCAL_HARDENING)
    argv.extend(args)
    try:
        proc = subprocess.run(argv, input=input, capture_output=True, env=git_env())
    except FileNotFoundError as exc:  # pragma: no cover - depends on the host
        raise GitError("git executable not found on PATH") from exc
    if check and proc.returncode != 0:
        detail = _stderr_text(proc.stderr)
        raise GitError(f"git {args[0]} failed (exit {proc.returncode}): {detail}")
    return proc


def object_hasher(oid: str) -> "hashlib._Hash":
    """Return a fresh hash object of the algorithm git used to name ``oid``."""
    if len(oid) == 40:
        return hashlib.sha1(usedforsecurity=False)
    if len(oid) == 64:
        return hashlib.sha256()
    raise GitError(f"unrecognised object id length {len(oid)} for {oid!r}")


def is_hex(text: str) -> bool:
    return bool(text) and all(ch in "0123456789abcdef" for ch in text)


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One entry of ``git ls-tree -r``: a blob or a gitlink (submodule commit)."""

    mode: str
    type: str
    oid: str
    size: int | None
    path: bytes


@dataclass(frozen=True, slots=True)
class CommitInfo:
    commit: str
    tree: str
    parents: tuple[str, ...]
    committer_time: str  # ISO 8601, UTC, second precision


@dataclass(frozen=True, slots=True)
class BlobScan:
    """Streaming digest of one blob; the full content is not retained."""

    oid: str
    size: int
    sha256: str
    head: bytes
    utf8: bool


class _CatFileBatch:
    """A ``git cat-file --batch`` process fed from a background thread."""

    def __init__(self, git_dir: Path, oids: Sequence[str]) -> None:
        argv = ["git", f"--git-dir={git_dir}", *LOCAL_HARDENING, "cat-file", "--batch"]
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=git_env(),
            )
        except FileNotFoundError as exc:  # pragma: no cover - depends on the host
            raise GitError("git executable not found on PATH") from exc
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._out = self._proc.stdout
        self._writer = threading.Thread(target=self._feed, args=(list(oids),), daemon=True)
        self._writer.start()

    def _feed(self, oids: list[str]) -> None:
        stdin = self._proc.stdin
        assert stdin is not None
        try:
            for oid in oids:
                stdin.write(oid.encode("ascii") + b"\n")
        except (BrokenPipeError, ValueError):
            pass
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    def header(self, expected_oid: str) -> tuple[str, int]:
        line = self._out.readline()
        parts = line.split()
        if len(parts) == 2 and parts[1] == b"missing":
            raise GitError(f"object {expected_oid} is missing from the object store")
        if len(parts) != 3 or parts[0].decode("ascii", "replace") != expected_oid:
            raise GitError(f"unexpected git cat-file header for {expected_oid}: {line[:120]!r}")
        return parts[1].decode("ascii"), int(parts[2])

    def read(self, size: int) -> bytes:
        data = self._out.read(size)
        if len(data) != size:
            raise GitError("git cat-file output ended early")
        return data

    def end_object(self) -> None:
        if self._out.read(1) != b"\n":
            raise GitError("git cat-file output is not framed as expected")

    def close(self, *, ok: bool) -> None:
        if not ok and self._proc.poll() is None:
            self._proc.kill()
        self._out.close()
        stderr = self._proc.stderr.read() if self._proc.stderr else b""
        if self._proc.stderr:
            self._proc.stderr.close()
        code = self._proc.wait()
        self._writer.join(timeout=5)
        if ok and code != 0:
            raise GitError(f"git cat-file failed (exit {code}): {_stderr_text(stderr)}")


class GitRepo:
    """A git object store (bare repository or ``.git`` directory) accessed read-mostly."""

    def __init__(self, git_dir: Path | str) -> None:
        self.git_dir = Path(git_dir)

    def __repr__(self) -> str:
        return f"GitRepo({str(self.git_dir)!r})"

    # -- creation and configuration -------------------------------------------------

    @classmethod
    def init_bare(cls, git_dir: Path) -> "GitRepo":
        """Create an empty bare repository with no hook or template files."""
        git_dir.parent.mkdir(parents=True, exist_ok=True)
        run_local(
            None,
            ["init", "--bare", "--quiet", "--template=", "--initial-branch=main", str(git_dir)],
        )
        return cls(git_dir)

    def config_get(self, key: str) -> str | None:
        proc = run_local(self.git_dir, ["config", "--local", "--get", key], check=False)
        if proc.returncode == 1:
            return None
        if proc.returncode != 0:
            raise GitError(f"git config --get {key} failed: {_stderr_text(proc.stderr)}")
        return proc.stdout.decode("utf-8").rstrip("\n")

    def config_set(self, key: str, value: str) -> None:
        run_local(self.git_dir, ["config", "--local", key, value])

    def update_ref(self, ref: str, oid: str) -> None:
        run_local(self.git_dir, ["update-ref", ref, oid])

    # -- commits and trees -----------------------------------------------------------

    def resolve_commit(self, rev: str) -> str | None:
        """Return the full id of the commit named by hex ``rev``, or None if absent/ambiguous."""
        if not is_hex(rev):
            raise ValueError(f"commit must be lowercase hexadecimal, got {rev!r}")
        proc = run_local(
            self.git_dir,
            ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}"],
            check=False,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("ascii").strip()

    def commit_info(self, commit: str) -> CommitInfo:
        raw = run_local(self.git_dir, ["cat-file", "commit", commit]).stdout
        header = raw.split(b"\n\n", 1)[0].decode("utf-8", "replace")
        tree = ""
        parents: list[str] = []
        committer_time = ""
        for line in header.split("\n"):
            key, _, value = line.partition(" ")
            if key == "tree":
                tree = value
            elif key == "parent":
                parents.append(value)
            elif key == "committer":
                timestamp = int(value.rsplit(" ", 2)[-2])
                when = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                committer_time = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        if not tree or not committer_time:
            raise GitError(f"could not parse commit object {commit}")
        return CommitInfo(commit, tree, tuple(parents), committer_time)

    def ls_tree(self, commit: str) -> list[TreeEntry]:
        """Every path in the commit's tree (blobs, symlinks and gitlinks), recursively."""
        raw = run_local(
            self.git_dir, ["ls-tree", "-r", "-z", "--long", "--full-tree", commit]
        ).stdout
        entries: list[TreeEntry] = []
        for record in raw.split(b"\0"):
            if not record:
                continue
            meta, sep, path = record.partition(b"\t")
            fields = meta.split()
            if not sep or len(fields) != 4:
                raise GitError(f"unexpected git ls-tree record: {record[:120]!r}")
            mode, obj_type, oid, size = (field.decode("ascii") for field in fields)
            entries.append(TreeEntry(mode, obj_type, oid, None if size == "-" else int(size), path))
        return entries

    # -- blobs -----------------------------------------------------------------------

    def scan_blobs(self, oids: Iterable[str], *, head_bytes: int) -> dict[str, BlobScan]:
        """Stream every blob once: size, SHA-256, first ``head_bytes`` bytes, UTF-8 validity.

        Each blob's git object id is recomputed from the streamed bytes; a mismatch raises
        :class:`IntegrityError`.
        """
        unique = list(dict.fromkeys(oids))
        results: dict[str, BlobScan] = {}
        batch = _CatFileBatch(self.git_dir, unique)
        ok = False
        try:
            for oid in unique:
                obj_type, size = batch.header(oid)
                if obj_type != "blob":
                    raise GitError(f"object {oid} is a {obj_type}, not a blob")
                sha256 = hashlib.sha256()
                ident = object_hasher(oid)
                ident.update(b"blob %d\0" % size)
                decoder = codecs.getincrementaldecoder("utf-8")()
                utf8 = True
                head = bytearray()
                remaining = size
                while remaining:
                    chunk = batch.read(min(_CHUNK, remaining))
                    remaining -= len(chunk)
                    sha256.update(chunk)
                    ident.update(chunk)
                    if len(head) < head_bytes:
                        head += chunk[: head_bytes - len(head)]
                    if utf8:
                        try:
                            decoder.decode(chunk)
                        except UnicodeDecodeError:
                            utf8 = False
                if utf8:
                    try:
                        decoder.decode(b"", final=True)
                    except UnicodeDecodeError:
                        utf8 = False
                batch.end_object()
                if ident.hexdigest() != oid:
                    raise IntegrityError(f"blob {oid} content does not hash to its object id")
                results[oid] = BlobScan(oid, size, sha256.hexdigest(), bytes(head), utf8)
            ok = True
        finally:
            batch.close(ok=ok)
        return results

    def read_blobs(self, oids: Iterable[str]) -> Iterator[tuple[str, bytes]]:
        """Yield ``(oid, content)`` for each requested blob, in order, verified by object id."""
        wanted = list(oids)
        batch = _CatFileBatch(self.git_dir, wanted)
        ok = False
        try:
            for oid in wanted:
                obj_type, size = batch.header(oid)
                if obj_type != "blob":
                    raise GitError(f"object {oid} is a {obj_type}, not a blob")
                data = batch.read(size)
                batch.end_object()
                ident = object_hasher(oid)
                ident.update(b"blob %d\0" % size)
                ident.update(data)
                if ident.hexdigest() != oid:
                    raise IntegrityError(f"blob {oid} content does not hash to its object id")
                yield oid, data
            ok = True
        finally:
            batch.close(ok=ok)

    def read_blob(self, oid: str) -> bytes:
        [(_, data)] = list(self.read_blobs([oid]))
        return data
