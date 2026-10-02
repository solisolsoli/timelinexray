"""Shared test helpers: network blocking, a deterministic fixture repository, oracles.

Tests never use the network. Python sockets are blocked for the whole run (see
``tests/__init__.py``) and every git fetch in the suite uses a ``file://`` URL.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from unittest import mock

from timelinexray.gitio import git_env

REPO_ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_COMMIT = "77d431aabf409ca1c1eed9bec7e2183f7c914e23"
ENV_TEST_UPSTREAM = "TXRAY_TEST_UPSTREAM"

# -- network ---------------------------------------------------------------------------


class NetworkBlocked(RuntimeError):
    pass


def _refuse(*args: object, **kwargs: object) -> None:
    raise NetworkBlocked("network access is disabled in the TimelineXray test suite")


_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex


def _guarded_connect(self: socket.socket, address: object) -> None:
    if self.family in (socket.AF_INET, socket.AF_INET6):
        _refuse()
    return _original_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: object) -> int:
    if self.family in (socket.AF_INET, socket.AF_INET6):
        _refuse()
    return _original_connect_ex(self, address)


def block_network() -> None:
    """Make every Python-level IP connection attempt raise :class:`NetworkBlocked`."""
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]
    socket.create_connection = _refuse  # type: ignore[assignment]
    socket.getaddrinfo = _refuse  # type: ignore[assignment]


# -- git helpers -----------------------------------------------------------------------

FIXTURE_IDENTITY = {
    "GIT_AUTHOR_NAME": "Fixture Author",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
    "GIT_COMMITTER_NAME": "Fixture Author",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
}


def git(git_dir: Path, *args: str, input: bytes | None = None,
        env_overrides: Mapping[str, str] | None = None) -> bytes:
    """Run git directly (tests only) with the same isolated environment as the tool.
    ``env_overrides`` replace single variables of the fixture identity (for example a
    committer date, when a fixture needs commits in a known time order)."""
    env = {**git_env(), **FIXTURE_IDENTITY, **(env_overrides or {})}
    proc = subprocess.run(
        ["git", f"--git-dir={git_dir}", "-c", "protocol.allow=never", *args],
        input=input,
        capture_output=True,
        env=env,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr.decode('utf-8', 'replace')}")
    return proc.stdout


def git_show(git_dir: Path, commit: str, path: str) -> bytes:
    """Independent oracle: the raw blob bytes as ``git show <commit>:<path>`` prints them."""
    return git(git_dir, "show", f"{commit}:{path}")


def ls_tree_paths(git_dir: Path, commit: str) -> list[str]:
    raw = git(git_dir, "ls-tree", "-r", "-z", "--name-only", commit)
    return [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]


_ORACLE_LINE = re.compile(rb"[^\n]*\n|[^\n]+\Z")


def oracle_lines(data: bytes) -> list[bytes]:
    """Lines split by an independent regular expression (LF-terminated, last may lack LF)."""
    return _ORACLE_LINE.findall(data)


class Symlink(str):
    """A fixture tree entry that is a symbolic link to the given target."""


class Gitlink(str):
    """A fixture tree entry that is a submodule commit (gitlink)."""


def build_fixture_repo(
    directory: Path, files: Mapping[str, bytes | Symlink | Gitlink], executables: frozenset[str]
) -> tuple[Path, str]:
    """Create a bare repository with exactly ``files`` as its single commit.

    Blobs are written with ``git hash-object --no-filters`` and trees with ``git mktree``,
    so no working tree, line-ending conversion or attribute can alter the bytes. The
    commit identity and date are fixed, so the commit id is the same on every run.
    """
    git_dir = directory / "fixture.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", "--template=", "--initial-branch=main", str(git_dir)],
        check=True,
        env=git_env(),
    )
    root: dict[str, object] = {}
    for path, value in files.items():
        node = root
        *parents, name = path.split("/")
        for part in parents:
            node = node.setdefault(part, {})  # type: ignore[assignment]
        node[name] = (path, value)

    def write_tree(node: dict[str, object]) -> str:
        records = []
        for name in sorted(node):
            value = node[name]
            if isinstance(value, dict):
                mode, kind, oid = "040000", "tree", write_tree(value)
            else:
                path, content = value  # type: ignore[misc]
                if isinstance(content, Gitlink):
                    mode, kind, oid = "160000", "commit", str(content)
                else:
                    data = content.encode("utf-8") if isinstance(content, Symlink) else content
                    oid = git(git_dir, "hash-object", "-w", "--no-filters", "--stdin", input=data)
                    oid = oid.decode("ascii").strip()
                    kind = "blob"
                    if isinstance(content, Symlink):
                        mode = "120000"
                    else:
                        mode = "100755" if path in executables else "100644"
            records.append(f"{mode} {kind} {oid}\t".encode("ascii") + name.encode("utf-8"))
        tree = git(git_dir, "mktree", "-z", "--missing", input=b"\0".join(records) + b"\0")
        return tree.decode("ascii").strip()

    tree = write_tree(root)
    commit = git(git_dir, "commit-tree", tree, "-m", "fixture").decode("ascii").strip()
    git(git_dir, "update-ref", "refs/heads/main", commit)
    return git_dir, commit


def file_url(path: Path) -> str:
    return "file://" + os.path.realpath(path)


# -- the fixture ------------------------------------------------------------------------

HUGE_SIZE = 1_048_576 + 1  # one byte over the default max_file_bytes

FIXTURE_FILES: dict[str, bytes | Symlink | Gitlink] = {
    "README.md": b"# Fixture\n\nSynthetic repository for TimelineXray tests.\n",
    "LICENSE": b"                                 Apache License\n"
    b"                           Version 2.0, January 2004\n(synthetic excerpt)\n",
    "pkg/NOTICE": b"Portions: Permission is hereby granted, free of charge, to any person.\n",
    "src/license.rs": b"// not a license file\npub fn license() {}\n",
    "crlf/windows.txt": b"alpha\r\nbeta\r\ngamma\r\n",
    "crlf/mixed.rs": b"fn a() {}\r\nfn b() {}\nfn c() {}\r\n",
    "crlf/no_final_newline.scala": b"object A\r\nobject B",
    "edge/empty.py": b"",
    "edge/lone_cr.txt": b"one\rtwo\nthree\n",
    "edge/only_newline.txt": b"\n",
    "edge/no_newline.java": b"class A {}",
    "edge/blank_lines.py": b"\n\n\nx = 1\n\n",
    "edge/bom.py": b"\xef\xbb\xbfprint('bom')\r\n",
    "edge/latin1.txt": b"caf\xe9\n",
    "unicode/naïve.txt": "naïve — café\n".encode("utf-8"),
    "src/Main.java": b"public class Main {\n  public static void main(String[] a) {}\n}\n",
    "src/app.py": b"WEIGHT = 0.5\nWEIGHT = 0.5\n\ndef score(x):\n    return x * WEIGHT\n",
    "src/lib.rs": b"pub const CLICK: f64 = 0.3;\npub fn f() -> f64 { CLICK }\n",
    "src/App.scala": b"object App {\n  val x = 1\n}\n",
    "src/types.pyi": b"def score(x: float) -> float: ...\n",
    "bin/data.bin": b"\x00\x01\x02binary\x00",
    "bin/image.jpg": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01",
    "big/huge.log": (b"x" * 63 + b"\n") * (HUGE_SIZE // 64) + b"y" * (HUGE_SIZE % 64),
    "vendor/lib/thing.py": b"VENDORED = True\n",
    "web/node_modules/pkg/index.js": b"module.exports = 1;\n",
    "proto/api_pb2.py": b"# protobuf output\nDESCRIPTOR = None\n",
    "Cargo.lock": b"# This file is automatically @generated by Cargo.\nversion = 4\n",
    "gen/output.rs": b"// @generated by a fixture tool\npub fn g() {}\n",
    "link/to_readme": Symlink("../README.md"),
    "link/escape": Symlink("../../../../../etc/passwd"),
    "scripts/run.sh": b"#!/bin/sh\necho fixture\n",
    "sub/module": Gitlink("1" * 40),
}

FIXTURE_EXECUTABLES = frozenset({"scripts/run.sh"})

#: path -> (classification, reason, language)
FIXTURE_EXPECTED: dict[str, tuple[str, str | None, str | None]] = {
    "README.md": ("text", None, "markdown"),
    "LICENSE": ("text", None, None),
    "pkg/NOTICE": ("text", None, None),
    "src/license.rs": ("parsed-candidate", None, "rust"),
    "crlf/windows.txt": ("text", None, "plain-text"),
    "crlf/mixed.rs": ("parsed-candidate", None, "rust"),
    "crlf/no_final_newline.scala": ("parsed-candidate", None, "scala"),
    "edge/empty.py": ("parsed-candidate", None, "python"),
    "edge/lone_cr.txt": ("text", None, "plain-text"),
    "edge/only_newline.txt": ("text", None, "plain-text"),
    "edge/no_newline.java": ("parsed-candidate", None, "java"),
    "edge/blank_lines.py": ("parsed-candidate", None, "python"),
    "edge/bom.py": ("parsed-candidate", None, "python"),
    "edge/latin1.txt": ("text", None, "plain-text"),
    "unicode/naïve.txt": ("text", None, "plain-text"),
    "src/Main.java": ("parsed-candidate", None, "java"),
    "src/app.py": ("parsed-candidate", None, "python"),
    "src/lib.rs": ("parsed-candidate", None, "rust"),
    "src/App.scala": ("parsed-candidate", None, "scala"),
    "src/types.pyi": ("parsed-candidate", None, "python"),
    "bin/data.bin": ("excluded", "binary", None),
    "bin/image.jpg": ("excluded", "binary", None),
    "big/huge.log": ("excluded", "oversize", None),
    "vendor/lib/thing.py": ("excluded", "vendored", "python"),
    "web/node_modules/pkg/index.js": ("excluded", "vendored", "javascript"),
    "proto/api_pb2.py": ("excluded", "generated", "python"),
    "Cargo.lock": ("excluded", "generated", "toml"),
    "gen/output.rs": ("excluded", "generated", "rust"),
    "link/to_readme": ("excluded", "symlink", None),
    "link/escape": ("excluded", "symlink", None),
    "scripts/run.sh": ("text", None, "shell"),
    "sub/module": ("excluded", "submodule", None),
}


class FixtureRepo:
    """The fixture repository plus a snapshot store that has pinned its commit."""

    def __init__(self) -> None:
        from timelinexray.netguard import Allowlist
        from timelinexray.snapshot import SnapshotStore

        self._tmp = tempfile.TemporaryDirectory(prefix="txray-fixture-")
        self.root = Path(self._tmp.name)
        self.git_dir, self.commit = build_fixture_repo(
            self.root / "upstream", FIXTURE_FILES, FIXTURE_EXECUTABLES
        )
        self.url = file_url(self.git_dir)
        self.allowlist = Allowlist([self.url])
        self.store_dir = self.root / "store"
        self.store = SnapshotStore(self.store_dir)
        self.pin_result = self.store.pin(self.commit, self.url, allowlist=self.allowlist)

    def new_store(self, name: str) -> "object":
        from timelinexray.snapshot import SnapshotStore

        return SnapshotStore(self.root / name)

    def cleanup(self) -> None:
        self._tmp.cleanup()


# -- CLI --------------------------------------------------------------------------------


class _Captured(io.TextIOWrapper):
    def __init__(self) -> None:
        super().__init__(io.BytesIO(), encoding="utf-8", newline="")

    def value(self) -> bytes:
        self.flush()
        return self.buffer.getvalue()  # type: ignore[attr-defined]


@contextlib.contextmanager
def _swap_streams() -> Iterator[tuple[_Captured, _Captured]]:
    out, err = _Captured(), _Captured()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        yield out, err
    finally:
        sys.stdout, sys.stderr = saved


def run_cli(
    argv: list[str], env: Mapping[str, str] | None = None
) -> tuple[int, bytes, bytes]:
    """Run ``txray`` in-process; return ``(exit_code, stdout_bytes, stderr_bytes)``."""
    from timelinexray.cli import main

    with mock.patch.dict(os.environ, dict(env or {})), _swap_streams() as (out, err):
        try:
            code = main(argv)
        except SystemExit as exc:  # argparse usage errors
            code = int(exc.code or 0)
        return code, out.value(), err.value()


# -- upstream ----------------------------------------------------------------------------


def upstream_git_dir() -> Path | None:
    """The local upstream clone holding 77d431a, if available (never fetched from the web).

    Looked up in ``$TXRAY_TEST_UPSTREAM`` or, by default, ``../x-algorithm-upstream`` next
    to this repository.
    """
    candidate = Path(os.environ.get(ENV_TEST_UPSTREAM) or REPO_ROOT.parent / "x-algorithm-upstream")
    git_dir = candidate / ".git" if (candidate / ".git").is_dir() else candidate
    if not git_dir.is_dir():
        return None
    proc = subprocess.run(
        ["git", f"--git-dir={git_dir}", "-c", "protocol.allow=never", "cat-file", "-e",
         f"{UPSTREAM_COMMIT}^{{commit}}"],
        capture_output=True,
        env=git_env(),
    )
    return git_dir if proc.returncode == 0 else None
