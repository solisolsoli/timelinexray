"""Helpers for the MCP server tests: an indexed fixture store, message builders, a client.

``IndexedStore`` pins the two synthetic commits of :mod:`tests.index_support` and builds
their code index at the store's default location (``<store>/index``), which is where the
server looks. ``StdioClient`` runs ``python -m timelinexray mcp serve`` in a child process
and talks to it line by line, with a timeout on every read.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from timelinexray.index import CodeIndex
from tests.index_support import TwoCommitRepo
from tests.support import REPO_ROOT
from tests.templates import metadata
from timelinexray.snapshot import SnapshotStore

MODERN = "2026-07-28"
LEGACY = "2025-11-25"
META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "txray-tests", "version": "0"},
}
SNAPSHOT = REPO_ROOT / "tests" / "mcp_tools_list.json"

EXPECTED_TOOLS = [
    "list_commits",
    "resolve_commit",
    "manifest_summary",
    "read_span",
    "search_code",
    "find_symbols",
    "index_coverage",
    "get_param",
    "param_history",
    "find_findings",
    "get_finding",
    "verify_claim",
    "stale_worklist",
]
#: The Milestone 4b tools, which read the configured findings ledger.
FINDINGS_TOOLS = ["find_findings", "get_finding", "verify_claim"]
#: Every tool that reads the configured findings ledger (the findings tools and the
#: stale-review worklist).
LEDGER_TOOLS = [*FINDINGS_TOOLS, "stale_worklist"]
#: The parameter tools (P7 get_param / param_history) over the index and the pins.
PARAM_TOOLS = ["get_param", "param_history"]
CODE_TOOLS = [name for name in EXPECTED_TOOLS if name not in LEDGER_TOOLS]


def request(method: str, params: Mapping[str, Any] | None = None, *, id: Any = 1,
            modern: bool = True) -> dict[str, Any]:
    body = dict(params or {})
    if modern:
        body["_meta"] = dict(META)
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": body}


def call(name: str, arguments: Mapping[str, Any], *, id: Any = 1,
         modern: bool = True) -> dict[str, Any]:
    return request("tools/call", {"name": name, "arguments": dict(arguments)}, id=id,
                   modern=modern)


def initialize(version: str = LEGACY, *, id: Any = 0) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0", "id": id, "method": "initialize",
        "params": {"protocolVersion": version, "capabilities": {},
                   "clientInfo": {"name": "txray-tests", "version": "0"}},
    }


class IndexedStore(TwoCommitRepo):
    """Two pinned fixture commits, both indexed at ``<store>/index``."""

    TEMPLATE = "two-commit-indexed"

    @staticmethod
    def _build(directory: Path) -> None:
        TwoCommitRepo._build(directory)
        meta = metadata(directory)
        index = CodeIndex(SnapshotStore(directory / "store"))
        for commit in (meta["a"], meta["b"]):
            index.build(commit)


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    env.pop("TXRAY_STORE", None)
    env.pop("TXRAY_FINDINGS", None)
    env.update(extra or {})
    return env


class StdioClient:
    """``txray mcp serve`` in a child process, driven one line at a time."""

    def __init__(self, store: Path | str, *extra_args: str, timeout: float = 120.0,
                 env: Mapping[str, str] | None = None) -> None:
        self.timeout = timeout
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "timelinexray", "mcp", "serve", "--store", str(store),
             *extra_args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=child_env(env),
        )
        self._lines: queue.Queue[bytes] = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(b"")

    def send_raw(self, data: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def send(self, message: Mapping[str, Any]) -> None:
        self.send_raw(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")

    def receive_line(self) -> bytes:
        line = self._lines.get(timeout=self.timeout)
        if not line:
            raise AssertionError("the server closed stdout")
        return line

    def receive(self) -> dict[str, Any]:
        line = self.receive_line()
        assert line.endswith(b"\n") and b"\n" not in line[:-1]
        return json.loads(line.decode("utf-8"))

    def ask(self, message: Mapping[str, Any]) -> dict[str, Any]:
        self.send(message)
        return self.receive()

    def close(self) -> tuple[int, bytes]:
        """Close stdin (the shutdown signal) and return (exit code, stderr)."""
        assert self.proc.stdin is not None and self.proc.stderr is not None
        self.proc.stdin.close()
        code = self.proc.wait(timeout=self.timeout)
        stderr = self.proc.stderr.read()
        self.proc.stderr.close()
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        return code, stderr
