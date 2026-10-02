"""The MCP server as a client sees it: ``txray mcp serve`` in a child process over stdio.

``FixtureStdioTest`` always runs (synthetic fixture store). ``UpstreamStdioTest`` pins and
indexes 77d431a of a local x-algorithm clone into a temporary store through the ``txray``
command line (a ``file://`` URL allowed with ``TXRAY_ALLOW_FILE_URLS``; no network) and
exercises every tool against it; it is skipped without a local clone (set
``TXRAY_TEST_UPSTREAM``).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from timelinexray.mcp import schema as S
from timelinexray.mcp.tools import build_registry
from tests.mcp_support import (
    CODE_TOOLS,
    EXPECTED_TOOLS,
    FINDINGS_TOOLS,
    LEGACY,
    MODERN,
    SNAPSHOT,
    IndexedStore,
    StdioClient,
    call,
    child_env,
    initialize,
    request,
)
from tests.support import UPSTREAM_COMMIT, git_show, upstream_git_dir
from tests.templates import copy_upstream_store

REGISTRY = build_registry()
UPSTREAM = upstream_git_dir()


def envelope_of(response: dict[str, Any], name: str) -> dict[str, Any]:
    """The tool result of a tools/call response, validated against the published schema."""
    result = response["result"]
    envelope = result["structuredContent"]
    S.validate(envelope, REGISTRY.get(name).strict_output_schema)
    S.validate(envelope, REGISTRY.get(name).output_schema)
    if json.loads(result["content"][0]["text"]) != envelope:
        raise AssertionError("text content is not the serialized structured content")
    return envelope


class FixtureStdioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = IndexedStore()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.cleanup()

    def setUp(self) -> None:
        self.client = StdioClient(self.fixture.store_dir)

    def tearDown(self) -> None:
        if self.client.proc.poll() is None:
            self.client.proc.kill()
            self.client.proc.wait()
        for stream in (self.client.proc.stdin, self.client.proc.stdout, self.client.proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def test_legacy_session_round_trip(self) -> None:
        client, commit = self.client, self.fixture.commit_a
        init = client.ask(initialize(LEGACY))
        self.assertEqual(init["result"]["protocolVersion"], LEGACY)
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = client.ask(request("tools/list", id=2, modern=False))["result"]["tools"]
        self.assertEqual([tool["name"] for tool in listed], EXPECTED_TOOLS)
        self.assertEqual(listed, json.loads(SNAPSHOT.read_text("utf-8"))["tools"])
        calls = {
            "list_commits": {},
            "resolve_commit": {"commit": commit[:10]},
            "manifest_summary": {"commit": commit},
            "read_span": {"commit": commit, "path": "src/weights.py", "start_line": 1,
                          "end_line": 3},
            "search_code": {"commit": commit, "query": "weight", "limit": 2},
            "find_symbols": {"commit": commit, "kind": "param", "with_calls": True},
            "index_coverage": {"commit": commit, "include_files": True, "limit": 5},
            "get_param": {"commit": commit, "name": "ClickWeight"},
            "param_history": {"name": "ClickWeight"},
        }
        self.assertEqual(list(calls), CODE_TOOLS)
        for number, (name, arguments) in enumerate(calls.items(), 10):
            with self.subTest(tool=name):
                response = client.ask(call(name, arguments, id=number, modern=False))
                self.assertEqual(response["id"], number)
                self.assertNotIn("resultType", response["result"])
                envelope = envelope_of(response, name)
                self.assertIn(envelope["outcome"], ("OK", "INCOMPLETE"))
                self.assertFalse(response["result"]["isError"])
                if name not in ("list_commits", "param_history"):
                    self.assertEqual(envelope["commit"], commit)
                elif name == "param_history":  # citations name their own commits
                    self.assertIsNone(envelope["commit"])
                    self.assertEqual(envelope["data"]["head"], self.fixture.commit_b)
        # without a findings ledger (<store>/findings does not exist) the findings tools
        # answer NOT_FOUND and the server keeps serving
        findings_calls = {"find_findings": {}, "get_finding": {"finding_id": "F-000000000000"},
                          "verify_claim": {"finding_id": "F-000000000000"}}
        self.assertEqual(list(findings_calls), FINDINGS_TOOLS)
        for number, (name, arguments) in enumerate(findings_calls.items(), 20):
            with self.subTest(tool=name):
                response = client.ask(call(name, arguments, id=number, modern=False))
                envelope = envelope_of(response, name)
                self.assertEqual(envelope["outcome"], "NOT_FOUND")
                self.assertTrue(response["result"]["isError"])
                self.assertIn("no findings ledger exists at <ledger>", envelope["error"]["message"])
        span = envelope_of(client.ask(call("read_span", calls["read_span"], id=30,
                                           modern=False)), "read_span")
        oracle = b"".join(git_show(self.fixture.git_dir, commit, "src/weights.py")
                          .splitlines(keepends=True)[:3])
        self.assertEqual(span["data"]["text"].encode("utf-8"), oracle)
        self.assertEqual(span["data"]["citation"]["span_sha256"], hashlib.sha256(oracle).hexdigest())
        code, stderr = client.close()
        self.assertEqual(code, 0)
        self.assertNotIn(str(self.fixture.root).encode(), stderr)

    def test_modern_requests_and_errors_in_one_stream(self) -> None:
        client, commit = self.client, self.fixture.commit_a
        discover = client.ask(request("server/discover", id="d"))["result"]
        self.assertEqual(discover["supportedVersions"], [MODERN, LEGACY])
        client.send_raw(b'{"jsonrpc": "2.0", "id": 1, "method": \n')
        self.assertEqual(client.receive()["error"]["code"], -32700)
        client.send_raw(b"\xff\xfe\n")
        self.assertEqual(client.receive()["error"]["code"], -32700)
        self.assertEqual(client.ask(request("resources/read", id=2))["error"]["code"], -32601)
        self.assertEqual(client.ask(call("no_such_tool", {}, id=3))["error"]["code"], -32602)
        bad = request("tools/call", {"name": "read_span", "arguments": "x"}, id=4)
        self.assertEqual(client.ask(bad)["error"]["code"], -32602)
        oversized = call("search_code", {"commit": commit, "query": "q" * 20000}, id=5)
        reply = client.ask(oversized)
        self.assertEqual((reply["id"], reply["error"]["code"]), (None, -32600))
        traversal = client.ask(call("read_span", {
            "commit": commit, "path": "../../../etc/passwd", "start_line": 1, "end_line": 1},
            id=6))
        envelope = envelope_of(traversal, "read_span")
        self.assertTrue(traversal["result"]["isError"])
        self.assertEqual(envelope["error"]["code"], "invalid_input")
        invalid = envelope_of(client.ask(call("read_span", {
            "commit": commit, "path": "src/weights.py", "start_line": "1", "end_line": 1},
            id=7)), "read_span")
        self.assertEqual(invalid["error"]["code"], "invalid_arguments")
        page = envelope_of(client.ask(call("search_code", {
            "commit": commit, "query": "weight", "limit": 1}, id=8)), "search_code")
        following = envelope_of(client.ask(call("search_code", {
            "commit": commit, "query": "weight", "limit": 1, "cursor": page["next_cursor"]},
            id=9)), "search_code")
        self.assertEqual(following["data"]["offset"], 1)
        self.assertNotEqual(following["data"]["hits"], page["data"]["hits"])
        self.assertEqual(client.close()[0], 0)

    def test_every_output_line_is_one_json_message(self) -> None:
        client = self.client
        client.send(request("tools/list", id=1))
        client.send(call("read_span", {"commit": self.fixture.commit_a,
                                       "path": "docs/crlf.txt", "start_line": 1,
                                       "end_line": 2}, id=2))
        for expected in (1, 2):
            line = client.receive_line()
            self.assertEqual(line.count(b"\n"), 1)
            self.assertLessEqual(len(line), 64 * 1024)
            self.assertEqual(json.loads(line)["id"], expected)
        self.assertEqual(client.close()[0], 0)


class ServeRefusalTest(unittest.TestCase):
    def test_analytics_dataset_directory_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="txray-mcp-dataset-") as tmp:
            dataset = Path(tmp) / "store"
            dataset.mkdir()
            (dataset / "dataset.json").write_text(
                json.dumps({"format": "timelinexray/analytics-dataset/v1"}), "utf-8")
            proc = subprocess.run(
                [sys.executable, "-m", "timelinexray", "mcp", "serve", "--store", str(dataset)],
                input=json.dumps(request("tools/list")).encode() + b"\n",
                capture_output=True, env=child_env(), timeout=120,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(proc.stdout, b"")
            self.assertIn(b"analytics dataset", proc.stderr)

    def test_the_server_process_never_imports_analytics(self) -> None:
        code = (
            "import io, json, sys\n"
            "from timelinexray import cli\n"
            "cli.build_parser()\n"
            "from timelinexray.mcp.server import Server\n"
            "from timelinexray.mcp.guard import StoreGuard\n"
            "server = Server(StoreGuard(sys.argv[1]), log=io.StringIO())\n"
            "meta = {'io.modelcontextprotocol/protocolVersion': '2026-07-28',"
            " 'io.modelcontextprotocol/clientCapabilities': {}}\n"
            "for name in ('tools/list', 'server/discover'):\n"
            "    server.handle_line(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': name,"
            " 'params': {'_meta': meta}}).encode())\n"
            "server.handle_line(json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',"
            " 'params': {'name': 'list_commits', 'arguments': {}, '_meta': meta}}).encode())\n"
            "print(json.dumps(sorted(m for m in sys.modules if 'analytics' in m)))\n"
            "print(json.dumps(sorted(m for m in sys.modules if m in"
            " ('socket', 'ssl', 'http.client', 'urllib.request', 'multiprocessing'))))\n"
        )
        with tempfile.TemporaryDirectory(prefix="txray-mcp-imports-") as tmp:
            proc = subprocess.run([sys.executable, "-c", code, tmp], capture_output=True,
                                  env=child_env(), timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        analytics, network = (json.loads(line) for line in proc.stdout.splitlines())
        self.assertEqual(analytics, [])
        self.assertEqual(network, [])


@unittest.skipIf(UPSTREAM is None, "no local x-algorithm clone with 77d431a "
                 "(set TXRAY_TEST_UPSTREAM to a local clone)")
class UpstreamStdioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert UPSTREAM is not None
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-mcp-upstream-")
        cls.store = Path(cls._tmp.name) / "store"
        cls.git_dir = UPSTREAM
        # the shared template: 77d431a pinned and indexed, nothing else
        copy_upstream_store("single", cls.store)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_every_tool_over_stdio(self) -> None:
        client = StdioClient(self.store)
        commit = UPSTREAM_COMMIT
        try:
            discover = client.ask(request("server/discover", id=0))["result"]
            self.assertEqual(discover["supportedVersions"][0], MODERN)
            tools = client.ask(request("tools/list", id=1))["result"]["tools"]
            self.assertEqual([tool["name"] for tool in tools], EXPECTED_TOOLS)

            def use(name: str, arguments: dict[str, Any], id: int) -> dict[str, Any]:
                response = client.ask(call(name, arguments, id=id))
                self.assertEqual(response["result"]["resultType"], "complete")
                envelope = envelope_of(response, name)
                self.assertIn(envelope["outcome"], ("OK", "INCOMPLETE"), envelope["error"])
                return envelope

            listing = use("list_commits", {}, 2)
            self.assertEqual([c["commit"] for c in listing["data"]["commits"]], [commit])
            resolved = use("resolve_commit", {"commit": commit[:7]}, 3)
            self.assertEqual(resolved["commit"], commit)
            manifest = use("manifest_summary", {"commit": commit}, 4)
            counts = manifest["data"]["counts"]
            self.assertEqual(counts["total"], 2147)
            self.assertEqual(counts["classification"]["excluded"], 7)
            search = use("search_code", {"commit": commit, "query": "ClickWeight", "limit": 20},
                         5)
            param_hits = [h for h in search["data"]["hits"]
                          if h["citation"]["path"] == "home-mixer/params/param.rs"]
            self.assertTrue(param_hits)
            hit = param_hits[0]["citation"]
            self.assertIn("public defaults at commit " + commit, " ".join(search["notes"]))
            span = use("read_span", {"commit": commit, "path": hit["path"],
                                     "start_line": hit["start_line"],
                                     "end_line": hit["start_line"] + 2,
                                     "anchor": "ClickWeight"}, 6)
            oracle = b"".join(git_show(self.git_dir, commit, hit["path"])
                              .splitlines(keepends=True)[hit["start_line"] - 1:
                                                         hit["start_line"] + 2])
            self.assertEqual(span["data"]["text"].encode("utf-8"), oracle)
            self.assertEqual(span["data"]["citation"]["span_sha256"],
                             hashlib.sha256(oracle).hexdigest())
            self.assertEqual(span["data"]["anchor"]["verdict"], "FOUND")
            symbols = use("find_symbols", {"commit": commit, "name": "ClickWeight",
                                           "kind": "param", "with_calls": True}, 7)
            [symbol] = [s for s in symbols["data"]["symbols"]
                        if s["citation"]["path"] == "home-mixer/params/param.rs"]
            self.assertIn("0.3", symbol["signature"])
            self.assertIn("public default", symbol["value_note"])
            self.assertEqual(symbol["citation"]["commit"], commit)
            coverage = use("index_coverage", {"commit": commit, "include_files": True,
                                              "lexical_status": "skipped"}, 8)
            self.assertEqual(coverage["data"]["summary"]["files"], 2147)
            self.assertEqual(coverage["data"]["total"], 7)
            big = use("find_symbols", {"commit": commit, "path_prefix": "home-mixer/",
                                       "with_calls": True, "limit": 50}, 9)
            self.assertEqual(big["outcome"], "INCOMPLETE")
            self.assertIsNotNone(big["next_cursor"])
            following = use("find_symbols", {"commit": commit, "path_prefix": "home-mixer/",
                                             "with_calls": True, "limit": 50,
                                             "cursor": big["next_cursor"]}, 10)
            self.assertEqual(following["data"]["offset"], big["data"]["returned"])
            param = use("get_param", {"commit": commit, "name": "ClickWeight"}, 11)
            self.assertEqual(sorted(d["citation"]["path"] for d in param["data"]["declarations"]),
                             ["home-mixer/params/param.rs", "vm-ranker/params.rs"])
            self.assertTrue(all(d["value"] == "0.3" and "public default" in d["value_note"]
                                for d in param["data"]["declarations"]))
            history = use("param_history", {"name": "ClickWeight"}, 12)
            self.assertEqual([c["commit"] for c in history["data"]["commits"]], [commit])
            self.assertEqual([t["points"][0]["value"] for t in history["data"]["timelines"]],
                             ["0.3", "0.3"])
        finally:
            code, _ = client.close()
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
