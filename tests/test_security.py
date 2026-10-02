"""Security regression suite (Milestone 6). The threat model is in ``SECURITY.md``.

* Every user-facing path input, on the CLI and over MCP: traversal, absolute paths,
  ``..``, NUL, control and direction-override characters, very long names.
* A hostile upstream tree: file names with newlines, leading dashes, shell metacharacters,
  terminal escapes and direction overrides, very long names; symlinks (absolute, escaping,
  to a directory, with a source-file extension); binary and oversized blobs; terminal
  escape sequences in file content; ``..`` and ``.git`` tree entries. Nothing may reach a
  shell or a git argument vector, nothing outside the store may be read, and human output
  shows control characters escaped.
* Drills beyond ``tests/test_update.py``: force-push to an unrelated history, a commit
  deleted and garbage-collected upstream, fetch failure followed by recovery, a corrupt
  upstream, and refused look-alike URLs.
* Ledger tampering: every read path (CLI, MCP, digest) refuses a damaged ledger; a
  consistent rewrite is caught by an independently kept head; forged self-approval.
* MCP requests that are oversized or malformed in ways the protocol tests do not cover.
* Static checks: no shell, no ``eval``/``exec``/``pickle`` in the package.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from timelinexray import gitio, netguard
from timelinexray.digest import DigestBuilder, render_markdown
from timelinexray.digest.findings import LedgerFindingsProvider
from timelinexray.digest.update import load_state, run_update
from timelinexray.errors import NetworkRefused
from timelinexray.findings import Actor, FindingsMemory, Ledger
from timelinexray.findings.ledger import GENESIS, compute_hash
from timelinexray.findings.model import canonical_json
from timelinexray.index import CodeIndex
from timelinexray.mcp.guard import StoreGuard
from timelinexray.mcp.server import MAX_REQUEST_BYTES, Server
from timelinexray.netguard import ENV_ALLOW_FILE_URLS, Allowlist
from timelinexray.snapshot import SnapshotStore
from timelinexray.snapshot import store as store_module
from tests.diff_support import commit as make_commit, delete_branch, init_bare, run_git, set_branch
from tests.index_support import add_commit
from tests.mcp_support import EXPECTED_TOOLS, call, child_env, request
from tests.support import REPO_ROOT, Symlink, build_fixture_repo, file_url, run_cli

SRC = REPO_ROOT / "src" / "timelinexray"
AUTHOR = Actor("agent-a", "author")
LONG_DIR = "d" * 200
LONG_LEAF = "f" * 250 + ".txt"
TERMINAL = b"title \x1b]0;owned\x07 clear \x1b[2J done\n" + "\u202e reversed\n".encode("utf-8")
HOSTILE: dict[str, Any] = {
    "README.md": b"# Hostile fixture\n",
    "hostile/new\nline.rs": b"pub const NEWLINE_NAME: u8 = 1;\n",
    "hostile/-rf.rs": b"pub const DASH_NAME: u8 = 2;\n",
    "hostile/--upload-pack=touch pwned.py": b"UPLOAD_PACK_NAME = 3\n",
    "hostile/$(touch pwned).py": b"SUBSHELL_NAME = 4\n",
    "hostile/`touch pwned`.txt": b"backtick name\n",
    "hostile/a;touch pwned|cat&.txt": b"metacharacter name\n",
    "hostile/quote'\"name.txt": b"quote name\n",
    "hostile/glob*?[x].txt": b"glob name\n",
    "hostile/esc\x1b[2Jname.txt": b"escape name\n",
    "hostile/rtl\u202etxt.exe": b"direction override name\n",
    f"hostile/{LONG_DIR}/{LONG_LEAF}": b"long name\n",
    "-n.txt": b"leading dash at the root\n",
    "content/terminal.txt": TERMINAL,
    "links/abs": Symlink("/etc/passwd"),
    "links/up": Symlink("../../../../../../etc/passwd"),
    "links/dir": Symlink("../hostile"),
    "links/evil.rs": Symlink("/etc/hosts"),
    "big/blob.bin": b"\x00\x01\x02 binary \x00",
    "big/large.txt": b"L" * 63 + b"\n" + b"x" * (2 * 1024 * 1024),
}
READABLE = [path for path, value in HOSTILE.items()
            if not isinstance(value, Symlink) and not path.startswith("big/")]
SHELL_WORDS = ("pwned",)
BAD_REPO_PATHS = [
    "../etc/passwd", "/etc/passwd", "a/../README.md", "./README.md", "README.md/", "a//b",
    "..", ".", "a\x00b", "~/x", "x" * 5000,
]


def _has_raw_controls(data: bytes) -> bool:
    return bool(re.search(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|\xe2\x80\xae", data))


def _fs_bad_values() -> list[str]:
    return ["", "a\x00b", "a\x1bb", "x" * 5000]


class HostileUpstream:
    """The hostile tree as commit 2 on top of a plain commit 1, pinned and indexed."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-security-")
        self.root = Path(self._tmp.name)
        self.git_dir, self.base = build_fixture_repo(
            self.root / "upstream", {"README.md": b"# Hostile fixture\n"}, frozenset())
        self.commit = add_commit(self.git_dir, HOSTILE, self.base)
        self.url = file_url(self.git_dir)
        self.allowlist = Allowlist([self.url])
        self.store = SnapshotStore(self.root / "store")
        self.store.pin(self.base, self.url, allowlist=self.allowlist)
        self.store.pin(self.commit, self.url, allowlist=self.allowlist)
        CodeIndex(self.store).build(self.commit)
        self.env = {ENV_ALLOW_FILE_URLS: self.url}

    def cli(self, *argv: str, env: dict[str, str] | None = None) -> tuple[int, bytes, bytes]:
        return run_cli([*argv, "--store", str(self.store.root)], env or self.env)

    def cleanup(self) -> None:
        self._tmp.cleanup()


FIX: HostileUpstream


def setUpModule() -> None:
    global FIX
    FIX = HostileUpstream()


def tearDownModule() -> None:
    FIX.cleanup()


def _server(ledger: Path | None = None) -> Server:
    import io

    return Server(StoreGuard(FIX.store.root, ledger), log=io.StringIO())


def _call(server: Server, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    line = server.handle_line(json.dumps(call(name, arguments)).encode("utf-8"))
    assert line is not None
    return json.loads(line)["result"]["structuredContent"]


# -- path inputs ------------------------------------------------------------------------------


class CliPathInputTest(unittest.TestCase):
    def test_repository_paths_are_validated_everywhere(self) -> None:
        commit = FIX.commit
        for path in BAD_REPO_PATHS:
            for argv in (["show", commit, path, "--lines", "1"],
                         ["show", commit, path, "--lines", "1", "--json"],
                         ["findings", "add", "--actor", "agent-a", "--title", "t", "--claim",
                          "c", "--component", "x", "--evidence-class", "CODE", "--status",
                          "SUPPORTED", "--cite", commit, path, "1", "x",
                          "--ledger", str(FIX.root / "ledger-paths")]):
                with self.subTest(argv=argv[0], path=path[:20]):
                    code, out, err = FIX.cli(*argv)
                    self.assertIn(code, (1, 2))
                    self.assertNotIn(b"Traceback", err)
                    self.assertNotIn(b"root:", out + err)  # nothing of /etc/passwd
        self.assertFalse((FIX.root / "ledger-paths" / "events.jsonl").exists())

    def test_path_globs_are_validated(self) -> None:
        for glob in ("a\x00b", "a\x1bb", "x" * 600):
            for command in ("search", "symbols"):
                with self.subTest(command=command, glob=glob[:10]):
                    argv = [command, FIX.commit, "--path", glob]
                    if command == "search":
                        argv.insert(2, "NAME")
                    code, _, err = FIX.cli(*argv)
                    self.assertEqual(code, 2)
                    self.assertNotIn(b"Traceback", err)
        code, out, _ = FIX.cli("search", FIX.commit, "NAME", "--path", "../*")
        self.assertEqual((code, b"hits     0" in out), (0, True))

    def test_file_system_arguments_are_validated(self) -> None:
        ledger = str(FIX.root / "ledger-fs")
        cases = {
            "--store": lambda v: ["manifest", FIX.commit, "--store", v],
            "--ledger": lambda v: ["findings", "list", "--ledger", v],
            "digest --out": lambda v: ["digest", FIX.base, FIX.commit, "--out", v],
            "digest --ledger": lambda v: ["digest", FIX.base, FIX.commit, "--ledger", v],
            "update --out": lambda v: ["update", "--out", v, "--upstream", FIX.url],
            "import FILE": lambda v: ["findings", "import", v, "--source-label", "L",
                                      "--actor", "a", "--ledger", ledger],
            "add --file": lambda v: ["findings", "add", "--actor", "a", "--file", v,
                                     "--ledger", ledger],
            "export --out": lambda v: ["findings", "export", "--out", v, "--ledger", ledger],
            "mcp --store": lambda v: ["mcp", "serve", "--store", v],
        }
        for label, build in cases.items():
            for value in _fs_bad_values():
                with self.subTest(argument=label, value=value[:8]):
                    code, _, err = run_cli(build(value), FIX.env)
                    self.assertEqual(code, 2)
                    self.assertIn(b"error: argument", err)
                    self.assertNotIn(b"Traceback", err)

    def test_file_system_errors_are_reported_not_raised(self) -> None:
        code, _, err = FIX.cli("findings", "import", str(FIX.root), "--source-label", "L",
                               "--actor", "a", "--ledger", str(FIX.root / "ledger-dir"))
        self.assertEqual(code, 1)
        self.assertIn(b"txray: error: Is a directory", err)
        code, out, _ = FIX.cli("findings", "import", str(FIX.root), "--source-label", "L",
                               "--actor", "a", "--ledger", str(FIX.root / "ledger-dir"), "--json")
        self.assertEqual((code, json.loads(out)["error"]["code"]), (1, "os_error"))
        deep = FIX.root / ("n" * 250) / ("m" * 250) / "store"
        code, _, err = run_cli(["manifest", FIX.commit, "--store", str(deep)])
        self.assertEqual(code, 1)
        self.assertNotIn(b"Traceback", err)

    def test_metrics_paths_in_a_separate_process(self) -> None:
        env = child_env({})
        for value in ("a\x1bb", "x" * 5000):
            proc = subprocess.run([sys.executable, "-m", "timelinexray", "metrics", "rwe", value],
                                  capture_output=True, env=env, timeout=120)
            self.assertEqual(proc.returncode, 2)
            self.assertNotIn(b"Traceback", proc.stderr)
        proc = subprocess.run([sys.executable, "-m", "timelinexray", "metrics", "rwe",
                               str(FIX.root / "absent")], capture_output=True, env=env, timeout=120)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn(b"Traceback", proc.stderr)


class McpPathInputTest(unittest.TestCase):
    def test_every_path_argument_is_validated(self) -> None:
        server = _server()
        commit = FIX.commit
        bad = BAD_REPO_PATHS[:-1] + ["x" * 1025, "hostile/new\nline.rs", "a\\b", "C:/x"]
        for path in bad:
            calls = [
                ("read_span", {"commit": commit, "path": path, "start_line": 1, "end_line": 1}),
                ("search_code", {"commit": commit, "query": "NAME", "path_prefix": path}),
                ("find_symbols", {"commit": commit, "path_prefix": path}),
                ("index_coverage", {"commit": commit, "path_prefix": path}),
                ("verify_claim", {"citations": [{"commit": commit, "path": path,
                                                 "start_line": 1, "end_line": 1,
                                                 "anchor": "x"}]}),
            ]
            for name, arguments in calls:
                if path == "README.md/" and name not in ("read_span", "verify_claim"):
                    continue  # a directory prefix may end in "/"
                with self.subTest(tool=name, path=path[:20]):
                    envelope = _call(server, name, arguments)
                    self.assertIn(envelope["outcome"], ("ERROR", "NOT_FOUND", "DENIED"))
                    self.assertIsNone(envelope["data"])
                    self.assertNotIn("root:", json.dumps(envelope))
        for glob in ("/etc/*", "../*", "a\x00b"):
            with self.subTest(glob=glob):
                envelope = _call(server, "search_code",
                                 {"commit": commit, "query": "NAME", "path_glob": glob})
                self.assertEqual(envelope["outcome"], "ERROR")

    def test_hostile_names_are_data(self) -> None:
        server = _server()
        for path in ("hostile/-rf.rs", "hostile/$(touch pwned).py", "hostile/quote'\"name.txt",
                     "hostile/glob*?[x].txt", f"hostile/{LONG_DIR}/{LONG_LEAF}"):
            with self.subTest(path=path[:30]):
                envelope = _call(server, "read_span", {"commit": FIX.commit, "path": path,
                                                       "start_line": 1, "end_line": 1})
                self.assertEqual(envelope["outcome"], "OK")
                self.assertEqual(envelope["data"]["text"].encode(), HOSTILE[path])
        hits = _call(server, "search_code", {"commit": FIX.commit, "query": "NEWLINE_NAME"})
        self.assertEqual([h["citation"]["path"] for h in hits["data"]["hits"]],
                         ["hostile/new\nline.rs"])
        for path in ("links/abs", "links/evil.rs", "big/blob.bin"):
            with self.subTest(path=path):
                envelope = _call(server, "read_span", {"commit": FIX.commit, "path": path,
                                                       "start_line": 1, "end_line": 1})
                self.assertIn(envelope["outcome"], ("DENIED", "ERROR"))
                self.assertIsNone(envelope["data"])


# -- hostile upstream --------------------------------------------------------------------------


class HostileTreeTest(unittest.TestCase):
    def test_every_entry_is_in_the_manifest(self) -> None:
        _, manifest = FIX.store.load_manifest(FIX.commit)
        paths = {entry.path: entry for entry in manifest.entries}
        self.assertEqual(set(paths), set(HOSTILE))
        for path in ("links/abs", "links/up", "links/dir", "links/evil.rs"):
            self.assertEqual((paths[path].classification, paths[path].reason),
                             ("excluded", "symlink"))
        self.assertEqual(paths["big/blob.bin"].reason, "binary")
        self.assertEqual(paths["big/large.txt"].reason, "oversize")

    def test_hostile_names_never_reach_a_shell_or_git_arguments(self) -> None:
        recorded: list[tuple[list[str], dict[str, Any]]] = []
        real_run, real_popen = subprocess.run, subprocess.Popen

        def run(argv: Any, *args: Any, **kwargs: Any) -> Any:
            recorded.append((list(argv), kwargs))
            return real_run(argv, *args, **kwargs)

        class Popen(real_popen):  # type: ignore[misc, valid-type]
            def __init__(self, argv: Any, *args: Any, **kwargs: Any) -> None:
                recorded.append((list(argv), kwargs))
                super().__init__(argv, *args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="txray-security-argv-") as tmp:
            store = SnapshotStore(Path(tmp) / "store")
            with mock.patch.object(gitio.subprocess, "run", run), \
                    mock.patch.object(gitio.subprocess, "Popen", Popen), \
                    mock.patch.object(netguard.subprocess, "run", run):
                store.pin(FIX.base, FIX.url, allowlist=FIX.allowlist)
                store.pin(FIX.commit, FIX.url, allowlist=FIX.allowlist)
                CodeIndex(store).build(FIX.commit)
                for path in READABLE:
                    store.read_span(FIX.commit, path, 1, 1)
                doc = DigestBuilder(store, allowlist=FIX.allowlist).build(FIX.base, FIX.commit)
            added = {i["new_path"] for i in doc["items"] if i["new_path"] in READABLE}
            self.assertEqual(added, set(READABLE) - {"README.md"})
        self.assertTrue(recorded)
        names = [path for path in HOSTILE if path.startswith("hostile/") or path == "-n.txt"]
        for argv, kwargs in recorded:
            self.assertFalse(kwargs.get("shell", False))
            self.assertEqual(argv[0], "git")
            for element in argv:
                for name in names:
                    leaf = name.rsplit("/", 1)[-1]
                    self.assertNotIn(leaf, element)
                self.assertNotIn("pwned", element)
        self.assertEqual(list(FIX.root.rglob("pwned")), [])
        self.assertFalse(Path("pwned").exists())

    def test_show_escapes_names_and_content_for_humans_only(self) -> None:
        code, out, err = FIX.cli("show", FIX.commit, "content/terminal.txt", "--lines", "1-2")
        self.assertEqual((code, err), (0, b""))
        self.assertFalse(_has_raw_controls(out))
        self.assertIn(b"\\x1b]0;owned\\x07", out)
        self.assertIn(b"\\u202e reversed", out)
        self.assertIn(b"shown escaped below; --raw writes the exact bytes", out)
        code, raw, _ = FIX.cli("show", FIX.commit, "content/terminal.txt", "--lines", "1-2", "--raw")
        self.assertEqual((code, raw), (0, TERMINAL))
        code, out, _ = FIX.cli("show", FIX.commit, "content/terminal.txt", "--lines", "1-2", "--json")
        self.assertEqual(json.loads(out)["data"]["text"].encode("utf-8"), TERMINAL)
        self.assertFalse(_has_raw_controls(out))
        for path in ("hostile/esc\x1b[2Jname.txt", "hostile/rtl\u202etxt.exe", "hostile/new\nline.rs"):
            with self.subTest(path=path):
                code, out, _ = FIX.cli("show", FIX.commit, path, "--lines", "1")
                self.assertEqual(code, 0)
                header = out.split(b"\n----\n")[0]
                self.assertFalse(_has_raw_controls(header.replace(b"\n", b"")))
                self.assertEqual(header.count(b"\npath "), 1)
        code, out, _ = run_cli(["show", FIX.commit, "--lines", "1", "--store",
                                str(FIX.store.root), "--", "-n.txt"])
        self.assertEqual((code, out.endswith(b"leading dash at the root\n")), (0, True))

    def test_listings_search_and_digests_escape_names(self) -> None:
        code, listing, _ = FIX.cli("manifest", FIX.commit)
        self.assertEqual(code, 0)
        self.assertFalse(_has_raw_controls(listing.replace(b"\n", b"").replace(b"\t", b"")))
        self.assertEqual(listing.count(b"\n"), len(HOSTILE) + 1)  # header + one line per path
        code, out, _ = FIX.cli("search", FIX.commit, "NAME")
        self.assertEqual(code, 0)
        self.assertIn(b"hostile/new\\x0aline.rs:1", out)
        doc = DigestBuilder(FIX.store, allowlist=FIX.allowlist).build(FIX.base, FIX.commit)
        md = render_markdown(doc).encode("utf-8")
        self.assertFalse(_has_raw_controls(md.replace(b"\n", b"")))
        self.assertIn(b"hostile/esc\\x1b[2Jname.txt", md)

    def test_symlinks_are_never_followed(self) -> None:
        for path in ("links/abs", "links/up", "links/dir", "links/evil.rs"):
            with self.subTest(path=path):
                code, out, err = FIX.cli("show", FIX.commit, path, "--lines", "1")
                self.assertEqual(code, 1)
                self.assertIn(b"symlink", err)
                self.assertNotIn(b"root:", out)
        code, _, err = FIX.cli("show", FIX.commit, "links/dir/-rf.rs", "--lines", "1")
        self.assertEqual(code, 1)
        self.assertIn(b"is not in the manifest", err)
        rows = {row.path: row for row in CodeIndex(FIX.store).coverage(FIX.commit).rows}
        self.assertEqual(rows["links/evil.rs"].lexical_status, "skipped")

    def test_binary_and_oversized_blobs(self) -> None:
        code, _, err = FIX.cli("show", FIX.commit, "big/blob.bin", "--lines", "1")
        self.assertEqual(code, 1)
        self.assertIn(b"binary", err)
        code, out, _ = FIX.cli("show", FIX.commit, "big/large.txt", "--lines", "1")
        self.assertEqual(code, 0)
        self.assertIn(b"oversize", out)
        fresh = SnapshotStore(FIX.store.root)
        with mock.patch.object(store_module, "MAX_READ_BYTES", 1024 * 1024):
            code, _, err = FIX.cli("show", FIX.commit, "big/large.txt", "--lines", "1")
            self.assertEqual(code, 1)
            self.assertIn(b"spans are read only from blobs of at most 1048576 bytes", err)
            envelope = _call(_server(), "read_span", {"commit": FIX.commit, "path": "big/large.txt",
                                                      "start_line": 1, "end_line": 1})
            self.assertEqual((envelope["outcome"], envelope["data"]), ("DENIED", None))
            with mock.patch.object(gitio.GitRepo, "read_blobs",
                                   side_effect=AssertionError("the blob was read")):
                with self.assertRaises(Exception) as caught:
                    fresh.read_span(FIX.commit, "big/large.txt", 1, 1)
                self.assertNotIsInstance(caught.exception, AssertionError)

    def test_dotdot_and_dotgit_tree_entries_are_refused_at_fetch(self) -> None:
        for name in ("..", ".git", "a/.git/config", ".GIT", "a/../b"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                git_dir, commit = build_fixture_repo(Path(tmp) / "up",
                                                     {name: b"x\n", "ok.txt": b"ok\n"}, frozenset())
                url = file_url(git_dir)
                store = Path(tmp) / "store"
                code, _, err = run_cli(["pin", commit, "--upstream", url, "--store", str(store)],
                                       {ENV_ALLOW_FILE_URLS: url})
                self.assertEqual(code, 1)
                self.assertRegex(err, rb"hasDot(dot|git)")
                self.assertFalse((store / "pins").exists() and any((store / "pins").iterdir()))


# -- drills -------------------------------------------------------------------------------------


def _params(value: str, note: str = "") -> dict[str, bytes]:
    return {"params/param.rs": f"param!(ClickWeight, f64, \"click_weight\", {value});\n".encode(),
            "notes.md": f"# Notes{note}\n".encode()}


class DrillTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-drill-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.git_dir = init_bare(self.root / "upstream.git")
        self.url = file_url(self.git_dir)
        self.allowlist = Allowlist([self.url])
        self.store = SnapshotStore(self.root / "store")
        self.out = self.root / "out"
        self.c0 = make_commit(self.git_dir, _params("0.4"), [], 1)
        self.c1 = make_commit(self.git_dir, _params("0.4", " one"), [self.c0], 2)
        set_branch(self.git_dir, "main", self.c1)

    def update(self, **kwargs: Any) -> Any:
        return run_update(self.store, out_dir=self.out, allowlist=self.allowlist,
                          upstream=self.url, clock=lambda: "2026-03-01T06:17:00Z", **kwargs)

    def finding(self) -> FindingsMemory:
        memory = FindingsMemory(Ledger(self.root / "ledger"), self.store)
        memory.add({"finding_id": "F-click", "title": "Click weight", "claim": "ClickWeight is 0.4.",
                    "component": "params", "evidence_class": "PARAM_DEFAULT", "status": "SUPPORTED",
                    "citations": [{"commit": self.c1, "path": "params/param.rs", "lines": "1",
                                   "anchor": "ClickWeight"}]}, AUTHOR)
        return memory

    def test_force_push_to_an_unrelated_history(self) -> None:
        self.assertEqual(self.update().accepted, self.c1)
        memory = self.finding()
        orphan = make_commit(self.git_dir, _params("9.9", " orphan"), [], 5)
        set_branch(self.git_dir, "main", orphan)
        result = self.update()
        self.assertEqual((result.outcome, result.exit_code, result.accepted),
                         ("attention", 1, self.c1))
        self.assertIn("history-rewritten", {e.kind for e in result.events})
        self.assertEqual(load_state(self.store)["quarantined"], [orphan])
        [verified] = memory.verify(["F-click"])  # old evidence stays checkable
        self.assertEqual(verified["freshness"], "CURRENT")
        doc = DigestBuilder(self.store, findings=LedgerFindingsProvider(
            self.store, self.root / "ledger")).build(self.c1, orphan)
        self.assertEqual(doc["range"]["relationship"], "diverged")
        self.assertEqual([r["finding_id"] for r in doc["affected_findings"]["findings"]],
                         ["F-click"])

    def test_commit_deleted_and_garbage_collected_upstream(self) -> None:
        feature = make_commit(self.git_dir, _params("0.9", " feature"), [self.c1], 3)
        set_branch(self.git_dir, "feature", feature)
        self.update()
        self.store.pin(feature, self.url, allowlist=self.allowlist)
        delete_branch(self.git_dir, "feature")
        run_git(self.git_dir, "reflog", "expire", "--expire=now", "--all")
        run_git(self.git_dir, "gc", "--prune=now", "--quiet")
        proc = subprocess.run(["git", f"--git-dir={self.git_dir}", "cat-file", "-e", feature],
                              capture_output=True, env=gitio.git_env())
        self.assertNotEqual(proc.returncode, 0)  # really gone upstream
        c2 = make_commit(self.git_dir, _params("0.4", " two"), [self.c1], 4)
        set_branch(self.git_dir, "main", c2)
        result = self.update()
        self.assertIn(("commit-unreachable-upstream", feature),
                      {(e.kind, e.commit) for e in result.events})
        self.assertEqual(self.store.read_span(feature, "notes.md", 1, 1).data, b"# Notes feature\n")
        code, out, _ = run_cli(["show", feature[:12], "params/param.rs", "--lines", "1", "--raw",
                                "--store", str(self.store.root)])
        self.assertEqual((code, out), (0, _params("0.9")["params/param.rs"]))

    def test_fetch_failure_then_recovery(self) -> None:
        self.update()
        moved = self.root / "moved.git"
        self.git_dir.rename(moved)
        failed = self.update()
        self.assertEqual((failed.outcome, failed.exit_code, failed.written), ("failed", 1, []))
        self.assertEqual(failed.accepted, self.c1)
        moved.rename(self.git_dir)
        c2 = make_commit(self.git_dir, _params("0.3", " two"), [self.c1], 4)
        set_branch(self.git_dir, "main", c2)
        recovered = self.update()
        self.assertEqual((recovered.outcome, recovered.previous, recovered.head),
                         ("updated", self.c1, c2))

    def test_corrupt_upstream_is_a_fetch_failure(self) -> None:
        self.update()
        shutil.rmtree(self.git_dir)
        self.git_dir.mkdir()
        (self.git_dir / "HEAD").write_text("not a repository\n")
        result = self.update()
        self.assertEqual((result.outcome, result.exit_code), ("failed", 1))
        self.assertEqual([e.kind for e in result.events], ["fetch-failure"])
        self.assertEqual(load_state(self.store)["accepted"], self.c1)

    def test_branch_names_are_validated(self) -> None:
        for branch in ("../main", "main\n", "a b", "x:y", "a*", "~1", "main^"):
            with self.subTest(branch=branch):
                code, _, err = run_cli(["update", "--out", str(self.out), "--upstream", self.url,
                                        "--branch", branch, "--store", str(self.store.root)],
                                       {ENV_ALLOW_FILE_URLS: self.url})
                self.assertEqual(code, 1)
                self.assertIn(b"invalid branch name", err)
        self.assertFalse(self.out.exists())

    def test_look_alike_urls_are_refused_before_any_process(self) -> None:
        urls = ["https://github.com/xai-org/x-algorithm", "http://github.com/xai-org/x-algorithm.git",
                "https://github.com.evil.example/xai-org/x-algorithm.git",
                "https://github.com/xai-org/x-algorithm.git/../../evil/repo.git",
                "https://user@github.com/xai-org/x-algorithm.git",
                "ssh://git@github.com/xai-org/x-algorithm.git", "ext::sh -c touch% pwned",
                "file://" + str(self.root / "other.git"), "file:///etc"]
        with mock.patch.object(netguard.subprocess, "run") as run:
            for url in urls:
                with self.subTest(url=url):
                    with self.assertRaises(NetworkRefused):
                        run_update(self.store, out_dir=self.out, allowlist=self.allowlist,
                                   upstream=url)
                    code, _, _ = run_cli(["pin", "a" * 40, "--upstream", url, "--store",
                                          str(self.store.root)], {ENV_ALLOW_FILE_URLS: self.url})
                    self.assertEqual(code, 3)
        run.assert_not_called()
        self.assertFalse(self.store.root.exists())


# -- ledger tampering ----------------------------------------------------------------------------


def _events(ledger: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (ledger / "events.jsonl").read_bytes().splitlines()]


def _write_chain(ledger: Path, events: list[dict[str, Any]]) -> None:
    """Rewrite a ledger consistently (every hash and link recomputed, HEAD updated)."""
    prev = GENESIS
    lines = []
    for seq, event in enumerate(events, 1):
        body = {key: value for key, value in event.items() if key != "hash"}
        body.update(seq=seq, prev=prev)
        prev = compute_hash(body)
        lines.append(canonical_json({**body, "hash": prev}) + b"\n")
    (ledger / "events.jsonl").write_bytes(b"".join(lines))
    (ledger / "HEAD").write_text(json.dumps({"hash": prev, "seq": len(events)}, sort_keys=True) + "\n")


class LedgerTamperTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-tamper-")
        cls.root = Path(cls._tmp.name)
        cls.clean = cls.root / "clean"
        memory = FindingsMemory(Ledger(cls.clean), FIX.store)
        for finding_id, path in (("F-dash", "hostile/-rf.rs"), ("F-sub", "hostile/$(touch pwned).py")):
            memory.add({"finding_id": finding_id, "title": "Hostile name", "claim": "A constant.",
                        "component": "hostile", "evidence_class": "CODE", "status": "SUPPORTED",
                        "citations": [{"commit": FIX.commit, "path": path, "lines": "1",
                                       "anchor": "NAME"}]}, AUTHOR)
        memory.verify()
        cls.head = Ledger(cls.clean).verify().last_hash

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def copy(self, name: str) -> Path:
        target = self.root / name
        shutil.copytree(self.clean, target)
        return target

    def assert_refused_everywhere(self, ledger: Path) -> None:
        code, _, err = FIX.cli("findings", "list", "--ledger", str(ledger))
        self.assertEqual(code, 1)
        self.assertIn(b"fails verification", err)
        envelope = _call(_server(ledger), "find_findings", {})
        self.assertEqual((envelope["outcome"], envelope["data"]), ("ERROR", None))
        doc = DigestBuilder(FIX.store, findings=LedgerFindingsProvider(FIX.store, ledger)).build(
            FIX.base, FIX.commit)
        self.assertFalse(doc["affected_findings"]["available"])

    def test_changed_byte(self) -> None:
        ledger = self.copy("byte")
        data = (ledger / "events.jsonl").read_bytes()
        (ledger / "events.jsonl").write_bytes(data.replace(b"A constant.", b"A constanT.", 1))
        self.assert_refused_everywhere(ledger)

    def test_removed_and_reordered_events(self) -> None:
        ledger = self.copy("order")
        lines = (ledger / "events.jsonl").read_bytes().splitlines(keepends=True)
        (ledger / "events.jsonl").write_bytes(lines[1] + lines[0] + b"".join(lines[2:]))
        self.assert_refused_everywhere(ledger)
        ledger = self.copy("truncated")
        (ledger / "events.jsonl").write_bytes(b"".join(lines[:-1]))
        self.assert_refused_everywhere(ledger)

    def test_appended_event_without_head(self) -> None:
        ledger = self.copy("appended")
        events = _events(ledger)
        forged = {**events[-1], "seq": len(events) + 1, "prev": events[-1]["hash"]}
        body = {k: v for k, v in forged.items() if k != "hash"}
        with open(ledger / "events.jsonl", "ab") as handle:
            handle.write(canonical_json({**body, "hash": compute_hash(body)}) + b"\n")
        self.assert_refused_everywhere(ledger)

    def test_consistent_rewrite_is_caught_by_a_kept_head(self) -> None:
        ledger = self.copy("rewrite")
        events = _events(ledger)
        events[0]["payload"]["record"]["claim"] = "A rewritten claim."
        _write_chain(ledger, events)
        code, _, _ = FIX.cli("findings", "verify-log", "--ledger", str(ledger))
        self.assertEqual(code, 0)  # a hash chain is not a signature (documented limit)
        code, out, _ = FIX.cli("findings", "verify-log", "--ledger", str(ledger),
                               "--expect-head", self.head)
        self.assertEqual(code, 1)
        self.assertIn(b"unexpected_head", out)

    def test_forged_self_approval_is_refused(self) -> None:
        ledger = self.copy("self-approval")
        events = _events(ledger)
        events.append({**events[0], "type": "review", "actor": {"name": "agent-a", "role": "reviewer"},
                       "payload": {"status": "SUPPORTED", "rationale": "mine", "objections": []}})
        _write_chain(ledger, events)
        code, _, err = FIX.cli("findings", "list", "--ledger", str(ledger))
        self.assertEqual(code, 1)
        self.assertIn(b"self-approval", err)

    def test_forged_review_without_a_current_integrity_check_is_refused(self) -> None:
        """The merge condition the write path enforces is re-enforced on replay: a
        consistently rewritten log cannot confirm a code claim that was never verified."""
        reviewer = {"name": "reviewer-b", "role": "reviewer"}
        for name, integrity_event in (("no-check", None), ("other-check", "0" * 64)):
            with self.subTest(name=name):
                ledger = self.copy(f"review-{name}")
                events = _events(ledger)
                # drop the verify events, so no CURRENT integrity check precedes the review
                events = [event for event in events if event["type"] != "verify"]
                events.append({**events[0], "type": "review", "actor": reviewer,
                               "payload": {"status": "SUPPORTED", "rationale": "forged",
                                           "objections": [],
                                           "integrity_event": integrity_event}})
                _write_chain(ledger, events)
                code, _, err = FIX.cli("findings", "list", "--ledger", str(ledger))
                self.assertEqual(code, 1)
                self.assertIn(b"without naming a CURRENT integrity check", err)
        # a review that names the real, current check replays (what the write path records)
        ledger = self.copy("review-valid")
        events = _events(ledger)
        check = [event for event in events if event["type"] == "verify"
                 and event["finding_id"] == "F-dash"][-1]
        events.append({**events[0], "type": "review", "actor": reviewer,
                       "payload": {"status": "SUPPORTED", "rationale": "read", "objections": [],
                                   "integrity_event": check["hash"]}})
        _write_chain(ledger, events)
        code, out, _ = FIX.cli("findings", "list", "--ledger", str(ledger))
        self.assertEqual(code, 0)
        self.assertIn(b"SUPPORTED (reviewed)", out)

    def test_symlinked_and_fifo_log_files(self) -> None:
        ledger = self.copy("fifo")
        (ledger / "events.jsonl").unlink()
        os.mkfifo(ledger / "events.jsonl")
        doc = DigestBuilder(FIX.store, findings=LedgerFindingsProvider(FIX.store, ledger)).build(
            FIX.base, FIX.commit)
        self.assertFalse(doc["affected_findings"]["available"])
        envelope = _call(_server(ledger), "find_findings", {})
        self.assertEqual(envelope["outcome"], "DENIED")
        ledger = self.copy("symlink")
        (ledger / "events.jsonl").unlink()
        os.symlink(self.clean / "events.jsonl", ledger / "events.jsonl")
        envelope = _call(_server(ledger), "find_findings", {})
        self.assertEqual(envelope["outcome"], "DENIED")


# -- analytics egress (P7 section 7.3 gate: zero canary leakage) ---------------------------------


class AnalyticsEgressTest(unittest.TestCase):
    """A canary in the post text and link of a private export must reach no output channel."""

    def test_canary_reaches_no_channel(self) -> None:
        from tests.analytics_support import post, run_txray, write_export

        canary_text = "CANARY" + "-TEXT-" + "7f3a9c"
        canary_link = "https://example.invalid/canary-" + "link-7f3a9c"
        with tempfile.TemporaryDirectory(prefix="txray-egress-") as tmp:
            root = Path(tmp)
            export = write_export(root / "export.csv", [
                post("1000000000000000001", text=canary_text, url=canary_link, impressions=1000,
                     likes=20, replies=2, reposts=1)])
            dataset = root / "private" / "dataset"
            dataset.parent.mkdir()
            outputs = [run_txray(["metrics", "import", str(export), "--schema", "x-post-v1",
                                  "--lang", "en", "--out", str(dataset), "--captured-at",
                                  "2026-09-30T12:00:00Z", "--store", str(FIX.store.root)])]
            outputs.append(run_txray(["metrics", "rwe", str(dataset), "--json",
                                      "--store", str(FIX.store.root)]))
            outputs.append(run_txray(["metrics", "reach", str(dataset), "--horizon", "24h",
                                      "--store", str(FIX.store.root)]))
            self.assertEqual([proc.returncode for proc in outputs], [0, 0, 0])
            channels = {"stdout/stderr of import, rwe and reach": b"".join(
                proc.stdout + proc.stderr for proc in outputs)}
            channels["the dataset directory"] = b"".join(
                path.read_bytes() for path in sorted(dataset.rglob("*")) if path.is_file())
            channels["the snapshot store"] = b"".join(
                path.read_bytes() for path in sorted(FIX.store.root.rglob("*"))
                if path.is_file() and path.stat().st_size < 50_000_000)
            channels["tools/list"] = json.dumps(_server().registry.definitions()).encode()
            leaks = {name for name, data in channels.items()
                     if canary_text.encode() in data or canary_link.encode() in data}
            self.assertEqual(leaks, set(), f"canary found in {sorted(leaks)} of {len(channels)}")
            # a store or ledger that contains the dataset is never served over MCP
            code, _, err = run_cli(["mcp", "serve", "--store", str(root)])
            self.assertEqual(code, 1)
            self.assertIn(b"never served over MCP", err)


# -- MCP requests -------------------------------------------------------------------------------


class McpRequestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _server()

    def rpc(self, raw: bytes) -> dict[str, Any]:
        line = self.server.handle_line(raw)
        assert line is not None
        self.assertLessEqual(len(line), 64 * 1024)
        return json.loads(line)

    def alive(self) -> None:
        response = self.rpc(json.dumps(request("tools/list", id="alive")).encode())
        self.assertEqual(len(response["result"]["tools"]), len(EXPECTED_TOOLS))

    def test_size_limit_boundary(self) -> None:
        base = call("search_code", {"commit": FIX.commit, "query": "NAME"})
        text = json.dumps(base, separators=(",", ":"))
        padded = text[:-1] + "," + '"pad":"' + "x" * (MAX_REQUEST_BYTES - len(text) - 9) + '"}'
        self.assertEqual(len(padded.encode()), MAX_REQUEST_BYTES)
        self.assertIn("result", self.rpc(padded.encode()))  # at the limit: processed
        over = self.rpc(padded.encode() + b" ")
        self.assertEqual(over["error"]["code"], -32600)
        self.alive()

    def test_hostile_values(self) -> None:
        cases = [
            call("read_span", {"commit": FIX.commit, "path": "\ud800", "start_line": 1,
                               "end_line": 1}),
            call("read_span", {"commit": FIX.commit, "path": "README.md", "start_line": 10 ** 30,
                               "end_line": 10 ** 30}),
            call("search_code", {"commit": FIX.commit, "query": "NAME", "extra": {"k" * 10: 1}}),
            call("search_code", {"commit": FIX.commit, "query": "NAME", "limit": 1e308}),
            call("verify_claim", {"citations": [{"commit": FIX.commit, "path": "README.md",
                                                 "start_line": 1, "end_line": 1,
                                                 "anchor": "x"}] * 9}),
            call("find_findings", {"query": "\x00\x1b[2J"}),
            {**call("list_commits", {}), "id": 10 ** 40},
        ]
        for message in cases:
            with self.subTest(message=str(message)[:80]):
                response = self.rpc(json.dumps(message).encode())
                if "result" in response:
                    envelope = response["result"]["structuredContent"]
                    self.assertIn(envelope["outcome"], ("OK", "ERROR", "NOT_FOUND", "DENIED"))
                else:
                    self.assertIn(response["error"]["code"], (-32600, -32602))
        self.alive()

    def test_garbage_stream_over_stdio(self) -> None:
        stream = b"".join([
            b"\x00\x01\x02\n", b"{" * 5000 + b"\n", b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
            b"x" * (MAX_REQUEST_BYTES + 100) + b"\n", "\ud7ff\n".encode("utf-8"), b"\r\n", b"\n",
            json.dumps(request("tools/list", id="last")).encode() + b"\n",
        ])
        proc = subprocess.run(
            [sys.executable, "-m", "timelinexray", "mcp", "serve", "--store", str(FIX.store.root)],
            input=stream, capture_output=True, env=child_env({}), timeout=120)
        self.assertEqual(proc.returncode, 0)
        lines = proc.stdout.splitlines()
        messages = [json.loads(line) for line in lines]
        self.assertEqual(messages[-1]["id"], "last")
        self.assertEqual(len(messages[-1]["result"]["tools"]), len(EXPECTED_TOOLS))
        self.assertTrue(all("error" in message for message in messages[:-1]))
        self.assertNotIn(b"Traceback", proc.stderr)


# -- static checks -------------------------------------------------------------------------------


class StaticTest(unittest.TestCase):
    FORBIDDEN_CALLS = {("os", "system"), ("os", "popen"), ("subprocess", "getoutput"),
                       ("subprocess", "getstatusoutput"), ("pickle", "load"), ("pickle", "loads"),
                       ("marshal", "loads"), ("shelve", "open")}

    def test_no_shell_and_no_code_evaluation(self) -> None:
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text("utf-8"), str(path))
            rel = path.relative_to(SRC.parent)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name):
                    self.assertNotIn(func.id, ("eval", "exec", "compile", "__import__"),
                                     f"{rel}:{node.lineno}")
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    self.assertNotIn((func.value.id, func.attr), self.FORBIDDEN_CALLS,
                                     f"{rel}:{node.lineno}")
                for keyword in node.keywords:
                    if keyword.arg == "shell":
                        self.assertTrue(isinstance(keyword.value, ast.Constant)
                                        and keyword.value.value is False,
                                        f"{rel}:{node.lineno} passes shell=")

    def test_git_argument_vectors_end_options_before_revisions(self) -> None:
        text = (SRC / "gitio.py").read_text("utf-8")
        self.assertIn('"--end-of-options"', text)
        self.assertNotIn("shell=True", text + (SRC / "netguard.py").read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
