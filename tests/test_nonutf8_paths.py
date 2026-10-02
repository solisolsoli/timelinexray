"""Upstream paths that are not UTF-8 (git bytes decoded with ``surrogateescape``) must not
crash any query that binds a path pattern, and a lone surrogate typed by a user is a usage
error, never an internal error."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from timelinexray.errors import InvalidInput
from timelinexray.findings import FindingsMemory, Ledger
from timelinexray.index import CodeIndex, glob_literal
from timelinexray.mcp.guard import StoreGuard
from timelinexray.mcp.server import Server
from timelinexray.netguard import Allowlist
from timelinexray.snapshot import SnapshotStore
from tests.diff_support import commit, init_bare, set_branch
from tests.mcp_support import call
from tests.support import file_url, run_cli

#: Latin-1 e-acute: the file name is the bytes b"caf\xe9.rs", which is not UTF-8.
BAD_PATH = "src/caf\udce9.rs"


def _weights(value: str) -> bytes:
    return f"pub const LATIN_WEIGHT: f64 = {value};\n".encode()


def _files(value: str) -> dict[str, bytes]:
    return {BAD_PATH: _weights(value), "src/plain.rs": b"pub const PLAIN_WEIGHT: f64 = 1.0;\n"}


class NonUtf8Fixture:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-nonutf8-")
        self.root = Path(self._tmp.name)
        self.git_dir = init_bare(self.root / "upstream.git")
        self.first = commit(self.git_dir, _files("1.0"), [], 1)
        self.second = commit(self.git_dir, _files("2.0"), [self.first], 2)
        set_branch(self.git_dir, "main", self.second)
        url = file_url(self.git_dir)
        self.store_dir = self.root / "store"
        self.store = SnapshotStore(self.store_dir)
        for item in (self.first, self.second):
            self.store.pin(item, url, allowlist=Allowlist([url]))
        self.index = CodeIndex(self.store)
        for item in (self.first, self.second):
            self.index.build(item)
        self.args = ["--store", str(self.store_dir)]

    def cleanup(self) -> None:
        self._tmp.cleanup()


FIXTURE: NonUtf8Fixture


def setUpModule() -> None:
    global FIXTURE
    FIXTURE = NonUtf8Fixture()


def tearDownModule() -> None:
    FIXTURE.cleanup()


class NonUtf8PathTest(unittest.TestCase):
    def test_the_fixture_path_is_not_utf8(self) -> None:
        paths = [entry.path for entry in FIXTURE.store.load_manifest(FIXTURE.first)[1].entries]
        self.assertIn(BAD_PATH, paths)
        self.assertRaises(UnicodeEncodeError, BAD_PATH.encode, "utf-8")

    def test_index_queries_match_the_path_by_its_bytes(self) -> None:
        index, commit_id = FIXTURE.index, FIXTURE.first
        self.assertEqual([s.path for s in index.symbols(
            commit_id, path_glob=glob_literal(BAD_PATH)).symbols], [BAD_PATH])
        self.assertEqual([s.path for s in index.symbols(commit_id, path_glob="src/caf*").symbols],
                         [BAD_PATH])
        self.assertEqual([h.path for h in index.search(
            commit_id, "LATIN_WEIGHT", path_glob="src/caf*").hits], [BAD_PATH])
        self.assertEqual(index.calls(commit_id, path_glob="src/caf*")[0], 0)

    def test_diff_digest_and_param_history_succeed(self) -> None:
        both = [FIXTURE.first, FIXTURE.second]
        for argv in (["diff", *both], ["diff", *both, "--json"], ["digest", *both],
                     ["param-history", "LATIN_WEIGHT", "--base", FIXTURE.first,
                      "--head", FIXTURE.second],
                     ["param-history", "LATIN_WEIGHT", "--json"]):
            with self.subTest(argv=argv[:2]):
                code, out, err = run_cli([*argv, *FIXTURE.args])
                self.assertEqual((code, err), (0, b""), err[-300:])
        code, out, _ = run_cli(["diff", *both, "--json", *FIXTURE.args])
        self.assertIn(b"LATIN_WEIGHT", out)

    def test_negative_search_coverage_counts_the_path(self) -> None:
        memory = FindingsMemory(Ledger(FIXTURE.root / "findings"), FIXTURE.store,
                                FIXTURE.index)
        for glob, expected in (("src/*", 2), ("src/caf*", 1), ("nothing/*", 0)):
            with self.subTest(glob=glob):
                negative = memory.negative_search(FIXTURE.first, "ABSENT_TERM", path_glob=glob)
                self.assertEqual(negative["coverage"]["paths_in_scope"], expected)

    def test_mcp_path_prefix_with_the_escaped_byte_and_with_a_lone_surrogate(self) -> None:
        server = Server(StoreGuard(FIXTURE.store_dir), log=io.StringIO())

        def ask(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            line = server.handle_line(json.dumps(call(name, arguments)).encode("utf-8"))
            assert line is not None
            return json.loads(line.decode("utf-8"))["result"]["structuredContent"]

        ok = ask("search_code", {"commit": FIXTURE.first, "query": "LATIN_WEIGHT",
                                 "path_prefix": "src/caf\udce9"})
        self.assertEqual(ok["outcome"], "OK", ok)
        self.assertEqual(ok["data"]["total"], 1)
        for tool, arguments in (
            ("search_code", {"query": "LATIN_WEIGHT"}),
            ("find_symbols", {}),
            ("index_coverage", {}),
        ):
            for key in ("path_prefix", "path_glob"):
                if tool == "index_coverage" and key == "path_glob":
                    continue
                with self.subTest(tool=tool, key=key):
                    bad = ask(tool, {"commit": FIXTURE.first, key: "src/\ud800", **arguments})
                    self.assertEqual((bad["outcome"], bad["error"]["code"]),
                                     ("ERROR", "invalid_input"), bad)


class VisibleTest(unittest.TestCase):
    def test_a_non_utf8_name_is_shown_as_its_byte(self) -> None:
        from timelinexray.textsafe import visible

        self.assertEqual(visible(BAD_PATH), "src/caf\\xe9.rs")
        self.assertEqual(visible("a\ud800b"), "a\\ud800b")
        visible(BAD_PATH).encode("utf-8")


class LoneSurrogateInputTest(unittest.TestCase):
    """Text columns (names, search terms) can never hold a lone surrogate: a usage error."""

    def test_names_and_terms_are_invalid_input(self) -> None:
        index, commit_id = FIXTURE.index, FIXTURE.first
        calls = (
            lambda: index.symbols(commit_id, name="caf\udce9"),
            lambda: index.symbols(commit_id, name="caf\udce9*"),
            lambda: index.calls(commit_id, callee="caf\udce9"),
            lambda: index.calls(commit_id, caller="caf\udce9"),
            lambda: index.search(commit_id, "caf\udce9 weight"),
            lambda: index.search(commit_id, "caf\udce9 weight", literal=True),
            lambda: index.search(commit_id, "weight", path_glob="src/\ud800*"),
        )
        for number, action in enumerate(calls):
            with self.subTest(call=number), self.assertRaises(InvalidInput):
                action()

    def test_cli_exits_2_not_1(self) -> None:
        for argv in (["symbols", FIXTURE.first, "--name", "caf\udce9"],
                     ["symbols", FIXTURE.first, "--path", "src/\ud800"],
                     ["search", FIXTURE.first, "caf\udce9 weight"]):
            with self.subTest(argv=argv[0]):
                code, _, err = run_cli([*argv, *FIXTURE.args])
                self.assertEqual(code, 2, err[-300:])
                self.assertNotIn(b"Traceback", err)


if __name__ == "__main__":
    unittest.main()
