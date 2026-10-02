"""Helpers for the code index tests: a two-commit fixture repository and canonical dumps.

``logical_dump`` reads everything the index stores about one commit and replaces internal
integer keys by content (blob ids, parse identities), so an incremental build and a clean
build of the same commit can be compared as data.
"""

from __future__ import annotations

import sqlite3
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from timelinexray.gitio import git_env
from timelinexray.index import CodeIndex
from timelinexray.index.schema import LINE_BITS, database_path
from timelinexray.netguard import Allowlist
from timelinexray.snapshot import SnapshotStore
from tests import syntax_fixtures as F
from tests.support import Symlink, build_fixture_repo, file_url, git
from tests.templates import copy_into, metadata, template, write_metadata

NOTES = (
    "# Notes\n"
    "\n"
    'alpha OR beta NEAR(gamma delta) "quoted" col:term ^start -neg *star {text}: x\n'
    "alpha alone here\n"
    "beta alone here\n"
    "plain quoted word without quote marks\n"
)

COMMIT_A: dict[str, bytes | Symlink] = {
    "src/scoring.rs": F.RUST.encode(),
    "src/Registry.scala": F.SCALA.encode(),
    "src/RankingService.java": F.JAVA.encode(),
    "src/weights.py": F.PYTHON.encode(),
    "docs/notes.md": NOTES.encode(),
    "docs/old_only.txt": b"legacy_marker_alpha appears only in the first commit\n",
    "docs/crlf.txt": b"first crlf_line_marker\r\nsecond line\r\n",
    "docs/latin1.txt": b"caf\xe9 latin_marker\n",
    "shared/same.txt": b"shared_content_marker is identical in both commits\n",
    "bin/blob.bin": b"\x00\x01binary_marker\x00",
    "link/to_notes": Symlink("../docs/notes.md"),
}

COMMIT_B: dict[str, bytes | Symlink] = {
    **{key: value for key, value in COMMIT_A.items() if key != "docs/old_only.txt"},
    "src/scoring.rs": F.RUST.replace('"fixture_click_weight",\n    0.3', '"fixture_click_weight",\n    0.25')
    .replace(
        "#[cfg(test)]",
        "pub fn fresh_marker_scorer() -> f64 {\n    compute_weighted_score(&HashMap::new(), &[])\n}\n\n"
        "#[cfg(test)]",
    )
    .encode(),
    "src/extra.py": b"def new_only_function():\n    return helper(1)\n",
    "shared/copy.txt": b"shared_content_marker is identical in both commits\n",
}


def _write_tree(git_dir: Path, files: Mapping[str, bytes | Symlink]) -> str:
    root: dict[str, Any] = {}
    for path, value in files.items():
        node = root
        *parents, name = path.split("/")
        for part in parents:
            node = node.setdefault(part, {})
        node[name] = value

    def write(node: dict[str, Any]) -> str:
        records = []
        for name in sorted(node):
            value = node[name]
            if isinstance(value, dict):
                mode, kind, oid = "040000", "tree", write(value)
            else:
                data = value.encode("utf-8") if isinstance(value, Symlink) else value
                oid = git(git_dir, "hash-object", "-w", "--no-filters", "--stdin", input=data)
                oid = oid.decode("ascii").strip()
                mode = "120000" if isinstance(value, Symlink) else "100644"
                kind = "blob"
            records.append(f"{mode} {kind} {oid}\t".encode("ascii") + name.encode("utf-8"))
        return git(git_dir, "mktree", "-z", input=b"\0".join(records) + b"\0").decode().strip()

    return write(root)


def add_commit(git_dir: Path, files: Mapping[str, bytes | Symlink], parent: str, *,
               committer_date: str | None = None) -> str:
    """Create a commit with exactly ``files`` on top of ``parent`` and move ``main`` to it.
    ``committer_date`` (ISO 8601 with offset) replaces the fixture's fixed date; the
    fixture otherwise gives every commit the same committer time."""
    dates = ({"GIT_AUTHOR_DATE": committer_date, "GIT_COMMITTER_DATE": committer_date}
             if committer_date else None)
    tree = _write_tree(git_dir, files)
    commit = git(git_dir, "commit-tree", tree, "-p", parent, "-m", "second",
                 env_overrides=dates).decode().strip()
    git(git_dir, "update-ref", "refs/heads/main", commit)
    return commit


class TwoCommitRepo:
    """A bare repository with commits A and B (B's parent is A), pinned in one store."""

    #: the shared template this class copies (see :mod:`tests.templates`)
    TEMPLATE = "two-commit"

    def __init__(self) -> None:
        # The commit ids are deterministic (fixed identity and dates), so the repository and
        # its pinned store are built once per run and the store is copied. The upstream
        # repository is shared read-only in place (the store records its URL); no test
        # changes it.
        shared = template(self.TEMPLATE, self._build)
        meta = metadata(shared)
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-index-")
        self.root = Path(self._tmp.name)
        self.git_dir = shared / "upstream" / "fixture.git"
        self.commit_a, self.commit_b = meta["a"], meta["b"]
        self.url = file_url(self.git_dir)
        self.allowlist = Allowlist([self.url])
        self.store_dir = self.root / "store"
        copy_into(shared / "store", self.store_dir)
        self.store = SnapshotStore(self.store_dir)

    @staticmethod
    def _build(directory: Path) -> None:
        git_dir, commit_a = build_fixture_repo(directory / "upstream", COMMIT_A, frozenset())
        commit_b = add_commit(git_dir, COMMIT_B, commit_a)
        url = file_url(git_dir)
        store = SnapshotStore(directory / "store")
        for commit in (commit_a, commit_b):
            store.pin(commit, url, allowlist=Allowlist([url]))
        write_metadata(directory, {"a": commit_a, "b": commit_b})

    def index(self, name: str, **kwargs: Any) -> CodeIndex:
        return CodeIndex(self.store, self.root / name, **kwargs)

    def cleanup(self) -> None:
        self._tmp.cleanup()


def read_blobs_oracle(git_dir: Path, oids: list[str]) -> dict[str, bytes]:
    """Blob bytes straight from ``git cat-file --batch`` (independent of the tool's reader)."""
    unique = list(dict.fromkeys(oids))
    proc = subprocess.run(
        ["git", f"--git-dir={git_dir}", "-c", "protocol.allow=never", "cat-file", "--batch"],
        input="".join(f"{oid}\n" for oid in unique).encode("ascii"),
        capture_output=True,
        env=git_env(),
        check=True,
    )
    out = proc.stdout
    position = 0
    blobs: dict[str, bytes] = {}
    for oid in unique:
        newline = out.index(b"\n", position)
        size = int(out[position:newline].split()[2])
        blobs[oid] = out[newline + 1 : newline + 1 + size]
        position = newline + 2 + size
    return blobs


def oracle_corpus(
    store: SnapshotStore, git_dir: Path, commit: str
) -> list[tuple[str, list[str]]]:
    """(path, lines) of every indexable text file of ``commit``, read directly from git."""
    _, manifest = store.load_manifest(commit)
    entries = [
        e for e in manifest.entries
        if e.classification in ("parsed-candidate", "text") and e.utf8
    ]
    blobs = read_blobs_oracle(git_dir, [e.oid for e in entries])
    corpus = []
    for entry in sorted(entries, key=lambda e: e.path.encode("utf-8", "surrogateescape")):
        text = blobs[entry.oid].decode("utf-8")
        lines = text.split("\n")
        if text.endswith("\n"):
            lines.pop()
        corpus.append((entry.path, [line.lower() for line in lines]))
    return corpus


def grep(corpus: list[tuple[str, list[str]]], terms: tuple[str, ...]) -> list[tuple[str, int]]:
    """Every (path, line) whose line contains all terms, case-insensitive, in path order."""
    folded = [term.lower() for term in terms]
    return [
        (path, number)
        for path, lines in corpus
        for number, line in enumerate(lines, 1)
        if all(term in line for term in folded)
    ]


def grep_oracle(
    store: SnapshotStore, git_dir: Path, commit: str, terms: tuple[str, ...]
) -> list[tuple[str, int]]:
    return grep(oracle_corpus(store, git_dir, commit), terms)


def logical_dump(index: CodeIndex, commit: str) -> dict[str, Any]:
    """Everything the index holds for ``commit``, with internal keys replaced by content."""
    connection = sqlite3.connect(database_path(index.root))
    try:
        generation = connection.execute(
            "SELECT tree, manifest_sha256, backends, files, lexical_indexed, syntax_files, "
            "symbols, calls FROM generations WHERE commit_id = ?",
            (commit,),
        ).fetchone()
        files = []
        for row in connection.execute(
            "SELECT path, blob_oid, language, classification, lexical_status, lexical_reason, "
            "syntax_status, syntax_reason, parse_key FROM files WHERE commit_id = ? ORDER BY path",
            (commit,),
        ):
            path, oid, *rest, parse_key = row
            lexical = None
            if row[4] == "indexed":
                blob = connection.execute(
                    "SELECT blob_key, sha256, size, line_count, indexed_lines FROM blobs "
                    "WHERE blob_oid = ?",
                    (oid,),
                ).fetchone()
                low = blob[0] << LINE_BITS
                lines = connection.execute(
                    "SELECT rowid - ?, text FROM blob_lines WHERE rowid BETWEEN ? AND ? "
                    "ORDER BY rowid",
                    (low, low, low | ((1 << LINE_BITS) - 1)),
                ).fetchall()
                lexical = (blob[1:], tuple(lines))
            parse = None
            if parse_key is not None:
                head = connection.execute(
                    "SELECT blob_oid, language, backend, backend_version, status, reason, "
                    "symbol_count, call_count FROM parses WHERE parse_key = ?",
                    (parse_key,),
                ).fetchone()
                symbols = connection.execute(
                    "SELECT ord, kind, name, container, qualname, start_line, name_line, "
                    "end_line, span_sha256, signature, modifiers FROM symbols "
                    "WHERE parse_key = ? ORDER BY ord",
                    (parse_key,),
                ).fetchall()
                calls = connection.execute(
                    "SELECT ord, caller_ord, callee, qualifier, form, line FROM calls "
                    "WHERE parse_key = ? ORDER BY ord",
                    (parse_key,),
                ).fetchall()
                parse = (head, tuple(symbols), tuple(calls))
            files.append((bytes(path), oid, tuple(rest), lexical, parse))
        return {"generation": generation, "files": files}
    finally:
        connection.close()


def query_battery(index: CodeIndex, commit: str, queries: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Public query results for ``commit``, as plain data."""
    searches = [
        index.search(commit, query, **options).to_dict() for query, options in queries
    ]
    symbols = index.symbols(commit, limit=100_000, with_calls=True).to_dict()
    total, calls = index.calls(commit, limit=100_000)
    coverage = index.coverage(commit).to_dict()
    return {
        "searches": searches,
        "symbols": symbols,
        "calls": [total, [call.to_dict() for call in calls]],
        "coverage": coverage,
        "generation": index.generation(commit).to_dict(),
    }
