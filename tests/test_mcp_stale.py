"""The ``stale_worklist`` MCP tool: the same worklist as ``txray findings stale --json``,
bounded and paged, read only, with no local location in its output.

Uses the synthetic two-commit history of ``tests/test_findings_stale.py`` (a parameter file,
a scorer and two filters that change between the commits); nothing uses the network.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from pathlib import Path
from typing import Any

from timelinexray.mcp import schema as S
from timelinexray.mcp.guard import StoreGuard
from timelinexray.mcp.server import Server
from timelinexray.mcp.tools import build_registry
from tests.mcp_support import call
from tests.support import run_cli
from tests.test_findings_stale import AUTHOR, Fixture, spec

REGISTRY = build_registry()
TOOL = REGISTRY.get("stale_worklist")


def ledger_state(directory: Path) -> dict[str, Any]:
    """Everything a write could change in a ledger directory."""
    state: dict[str, Any] = {".": os.stat(directory).st_mtime_ns}
    for path in sorted(directory.iterdir()):
        info = path.lstat()
        state[path.name] = (info.st_mode, info.st_size, info.st_mtime_ns, path.read_bytes())
    return state


class StaleWorklistToolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fix = fix = Fixture()
        cls.store = fix.store(fix.base, fix.new)
        cls.memory = memory = fix.memory(cls.store)
        memory.add(spec("D-param", "PARAM_DEFAULT", fix.cite("home/param.rs", "2", "MIX_WEIGHT")),
                   AUTHOR)
        memory.add(spec("C-score", "CODE", fix.cite("scorers/combine.rs", "3", "a * 2.0")), AUTHOR)
        memory.add(spec("A-dup", "CODE", fix.cite("filters/dup.rs", "2", "NAMES")), AUTHOR)
        memory.add(spec("B-age", "CODE", fix.cite("filters/age.rs", "3", "age < 30")), AUTHOR)
        memory.add(spec("E-notes", "REPO_DOC", fix.cite("docs/notes.md", "2", "unchanged")), AUTHOR)
        memory.verify()
        memory.reanchor(fix.new)
        cls.ledger = memory.ledger.directory

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fix.cleanup()

    def server(self) -> Server:
        return Server(StoreGuard(self.store.root, self.ledger), log=io.StringIO())

    def ask(self, server: Server, arguments: dict[str, Any], id: int = 1) -> tuple[dict, bytes]:
        line = server.handle_line(json.dumps(call("stale_worklist", arguments, id=id)).encode())
        assert line is not None
        envelope = json.loads(line)["result"]["structuredContent"]
        S.validate(envelope, TOOL.strict_output_schema)
        S.validate(envelope, TOOL.output_schema)
        return envelope, line

    def cli_json(self) -> dict[str, Any]:
        env = {"TXRAY_STORE": str(self.store.root), "TXRAY_FINDINGS": str(self.ledger)}
        code, out, _ = run_cli(["findings", "stale", "--json"], env=env)
        self.assertEqual(code, 0)
        return json.loads(out)["data"]

    def test_same_worklist_as_the_cli_json(self) -> None:
        expected = self.cli_json()
        server = self.server()
        envelope, _ = self.ask(server, {"limit": 20})
        self.assertEqual(envelope["outcome"], "OK")
        data = envelope["data"]
        self.assertEqual(data["target"], expected["target"])
        self.assertEqual(data["counts"], expected["counts"])
        self.assertEqual([step["command"] for step in data["batch"]],
                         [step["command"] for step in expected["batch"]])
        self.assertEqual(data["total"], len(expected["entries"]))
        for got, want in zip(data["entries"], expected["entries"], strict=True):
            with self.subTest(finding=want["finding_id"]):
                for key in ("rank", "finding_id", "freshness", "area", "priority", "status",
                            "workflow", "evidence_class", "draft", "unpinned_commits",
                            "check_event"):
                    self.assertEqual(got[key], want[key], key)
                self.assertEqual(got["commands"], want["commands"])
                self.assertEqual([c["old"] for c in got["citations"]],
                                 [c["old"] for c in want["citations"]])
                self.assertEqual([c["candidate"] for c in got["citations"]],
                                 [c["candidate"] for c in want["citations"]])
        self.assertEqual(data["entries"][0]["finding_id"], "D-param")
        self.assertEqual(data["entries"][0]["area"], "parameter")

    def test_paging_area_and_target(self) -> None:
        server = self.server()
        first, _ = self.ask(server, {"limit": 1})
        self.assertEqual(first["outcome"], "INCOMPLETE")
        self.assertEqual(first["data"]["returned"], 1)
        self.assertTrue(first["next_cursor"])
        second, _ = self.ask(server, {"limit": 1, "cursor": first["next_cursor"]}, id=2)
        self.assertEqual(second["data"]["offset"], 1)
        self.assertNotEqual(second["data"]["entries"][0]["finding_id"],
                            first["data"]["entries"][0]["finding_id"])
        scoring, _ = self.ask(server, {"area": "scoring", "limit": 20}, id=3)
        self.assertTrue(scoring["data"]["entries"])
        self.assertEqual({entry["area"] for entry in scoring["data"]["entries"]}, {"scoring"})
        explicit, _ = self.ask(server, {"target": self.fix.new, "limit": 20}, id=4)
        default, _ = self.ask(server, {"limit": 20}, id=5)
        self.assertEqual(explicit["data"], default["data"])
        unpinned, _ = self.ask(server, {"target": "0" * 40}, id=6)
        self.assertEqual(unpinned["outcome"], "NOT_FOUND")
        bad, _ = self.ask(server, {"limit": 21}, id=7)
        self.assertEqual(bad["error"]["code"], "invalid_arguments")
        at_base, _ = self.ask(server, {"target": self.fix.base, "limit": 20}, id=8)
        self.assertEqual(at_base["data"]["target"], self.fix.base)

    def test_read_only_and_no_local_locations(self) -> None:
        before = ledger_state(self.ledger)
        server = self.server()
        for number in range(3):
            envelope, line = self.ask(server, {"limit": 20}, id=number)
            text = line.decode("utf-8")
            self.assertNotIn(str(self.store.root), text)
            self.assertNotIn(str(self.ledger), text)
            commands = json.dumps([entry["commands"] for entry in envelope["data"]["entries"]])
            self.assertIn("txray findings review", commands)
            self.assertNotIn("--store", commands)
            self.assertNotIn("--ledger", commands)
        self.assertEqual(ledger_state(self.ledger), before)
        self.assertFalse((self.ledger / ".lock").exists() and ".lock" not in before)

    def test_notes_say_it_is_a_worklist_not_a_verdict(self) -> None:
        envelope, _ = self.ask(self.server(), {})
        notes = " ".join(envelope["notes"])
        self.assertIn("A worklist, not a verdict", notes)
        self.assertIn("omit --store/--ledger", notes)
        self.assertIn("parameter findings first", notes)
        self.assertTrue(TOOL.definition()["annotations"]["readOnlyHint"])

    def test_definition_stays_small(self) -> None:
        size = len(json.dumps(TOOL.definition(), separators=(",", ":")))
        self.assertLess(size, 2400)


class StaleWorklistBoundsTest(unittest.TestCase):
    def test_entry_projection_is_bounded(self) -> None:
        from timelinexray.mcp.tools import stale as tool
        entry = {
            "rank": 1, "finding_id": "X", "title": "t" * 5000, "component": "c",
            "evidence_class": "CODE", "status": "SUPPORTED", "status_basis": "proposed",
            "workflow": "draft", "freshness": "STALE", "area": "other", "priority": 2,
            "check_event": None, "current_citations": 0,
            "citations": [{"index": i, "outcome": "ambiguous", "reason": "r" * 5000,
                           "candidates": [{"path": "p", "start_line": 1, "end_line": 1,
                                           "extra": 1}] * 30}
                          for i in range(40)],
            "dependencies": [{"kind": "span", "freshness": "STALE", "citation": {}}] * 30,
            "negative": {"freshness": "STALE", "total": 3, "hits": ["x"] * 99},
            "unpinned_commits": ["a" * 40] * 99, "draft": {"possible": False},
            "commands": {"read": ["txray show"] * 99, "decide": {}},
        }
        projected = tool._entry(entry)
        self.assertEqual(len(projected["citations"]), tool.CITATIONS_MAX)
        self.assertEqual(projected["citations_total"], 40)
        self.assertEqual(len(projected["citations"][0]["candidates"]), tool.OCCURRENCES_MAX)
        self.assertIsNone(projected["citations"][0]["anchor_at_target"])
        self.assertNotIn("extra", projected["citations"][0]["candidates"][0])
        self.assertLessEqual(len(projected["citations"][0]["reason"]), tool.TEXT_MAX)
        self.assertLessEqual(len(projected["title"]), tool.TITLE_MAX)
        self.assertEqual(len(projected["dependencies"]), tool.DEPENDENCIES_MAX)
        self.assertEqual(projected["negative"], {"freshness": "STALE", "total": 3})
        self.assertEqual(len(projected["unpinned_commits"]), tool.COMMITS_MAX)
        self.assertEqual(len(projected["commands"]["read"]), tool.READ_MAX)
        step = tool._batch({"why": "w", "findings": [str(i) for i in range(80)], "command": "c",
                            "effect": "e"})
        self.assertEqual((len(step["findings"]), step["findings_total"]), (50, 80))


if __name__ == "__main__":
    unittest.main()
