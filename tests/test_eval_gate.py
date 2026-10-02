"""The semantic release gate: the question set, the deterministic harness, the live scorer.

The structural checks and the scoring rules always run (no network, no model). The
deterministic harness runs against a local clone of xai-org/x-algorithm with 4c5cfe8,
a707cc2 and 77d431a (``$TXRAY_TEST_UPSTREAM`` or ``../x-algorithm-upstream``), pinned
through a ``file://`` URL and indexed at 77d431a in a temporary store; the MCP server runs
as a child process like it does for any client. Nothing is fetched from the network.
"""

from __future__ import annotations

import importlib.util
import json
import py_compile
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

from tests.support import REPO_ROOT, UPSTREAM_COMMIT, upstream_git_dir
from tests.templates import NEW as NEW_PIN
from tests.templates import OLD as OLD_PIN
from tests.templates import copy_upstream_store

EVAL = REPO_ROOT / "eval"
QUESTIONS = EVAL / "questions.json"
GOLDENS = REPO_ROOT / "goldens" / "citations.json"
UPSTREAM = upstream_git_dir()


def _load(name: str) -> ModuleType:
    """Import a harness module from ``eval/`` (developer tools outside the package)."""
    spec = importlib.util.spec_from_file_location(f"txray_eval_{name}", EVAL / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"txray_eval_{name}"] = module
    spec.loader.exec_module(module)
    return module


run_gate = _load("run_gate")
live_gate = _load("live_gate")


class QuestionSetTest(unittest.TestCase):
    """eval/questions.json: citations and abstentions only, no prose, consistent with goldens."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = json.loads(QUESTIONS.read_text("utf-8"))

    def test_harness_modules_compile(self) -> None:
        for path in sorted(EVAL.glob("*.py")):
            with self.subTest(path=path.name):
                py_compile.compile(str(path), doraise=True)

    def test_question_set_is_well_formed(self) -> None:
        self.assertEqual(run_gate.validate(self.data), [])
        self.assertRegex(self.data["status"],
                         r"^(pending root review|root-reviewed \d{4}-\d{2}-\d{2})$")
        items = self.data["items"]
        self.assertGreaterEqual(len(items), 25)
        self.assertLessEqual(len(items), 40)
        kinds = {item["kind"] for item in items}
        self.assertEqual(kinds, {"answer", "abstain"})
        self.assertGreaterEqual(sum(item["kind"] == "abstain" for item in items), 5)
        self.assertIn(UPSTREAM_COMMIT, self.data["commits"]["indexed"])

    def test_validate_reports_problems(self) -> None:
        broken = json.loads(QUESTIONS.read_text("utf-8"))
        broken["items"][0]["kind"] = "guess"
        broken["items"][1]["expected"]["citations"][0]["span_sha256"] = "short"
        broken["items"][1]["expected"]["citations"][0]["anchor"] = "x" * 81
        broken["items"].append(dict(broken["items"][2], id=broken["items"][2]["id"]))
        problems = run_gate.validate(broken)
        self.assertTrue(any("kind must be" in p for p in problems))
        self.assertTrue(any("not a SHA-256" in p for p in problems))
        self.assertTrue(any("anchor must be" in p for p in problems))
        self.assertTrue(any("duplicate id" in p for p in problems))
        self.assertEqual(run_gate.validate({"schema": "other"}),
                         [f"schema must be {run_gate.SCHEMA}"])

    def test_answer_items_cite_spans_and_match_goldens(self) -> None:
        goldens = json.loads(GOLDENS.read_text("utf-8"))["goldens"]
        by_source: dict[str, list[dict]] = {}
        for golden in goldens:
            if golden["expected"]["anchor"] == "FOUND":
                by_source.setdefault(golden["source"], []).append(golden)
        for item in self.data["items"]:
            if item["kind"] != "answer":
                continue
            with self.subTest(item=item["id"]):
                citations = item["expected"]["citations"]
                self.assertTrue(citations)
                for citation in citations:
                    self.assertLessEqual(citation["end_line"] - citation["start_line"] + 1, 120)
                    self.assertLessEqual(len(citation["anchor"]), 80)
                matching = by_source.get(item["source"], [])
                if matching:
                    overlapping = [
                        golden for golden in matching for citation in citations
                        if golden["commit"] == citation["commit"] and run_gate.overlaps(
                            golden["path"], golden["start_line"], golden["end_line"],
                            citation["path"], citation["start_line"], citation["end_line"])
                    ]
                    self.assertTrue(overlapping, "no expected citation overlaps a golden "
                                                 f"of {item['source']}")

    def test_abstain_items_have_probes_and_no_citations(self) -> None:
        for item in self.data["items"]:
            if item["kind"] != "abstain":
                continue
            with self.subTest(item=item["id"]):
                self.assertNotIn("citations", item["expected"])
                self.assertTrue(item["expected"]["probes"])
                self.assertTrue(item["expected"]["abstention_reason"].strip())

    def test_no_claim_text_or_prose_members(self) -> None:
        for item in self.data["items"]:
            with self.subTest(item=item["id"]):
                self.assertLessEqual(set(item), run_gate.ITEM_KEYS)
                self.assertIn("?", item["question"])
                self.assertNotIn("claim", item)
                self.assertNotIn("title", item)


class LiveScoringTest(unittest.TestCase):
    """The live harness scores by rule; these cases fix the rule (no agent involved)."""

    COMMIT = "0" * 40
    ITEM = {
        "id": "T01", "kind": "answer", "source": "P2-016", "evidence_class": "PARAM_DEFAULT",
        "commit": COMMIT, "question": "What is the public default of ClickWeight?",
        "expected": {
            "answer_regexes": [r"(?<![\d.])0\.30*(?![\d])"],
            "citations": [{"commit": COMMIT, "path": "a/param.rs", "start_line": 10,
                           "end_line": 12, "anchor": "ClickWeight", "span_sha256": "f" * 64}],
        },
    }
    ABSTAIN = {
        "id": "T02", "kind": "abstain", "source": None, "evidence_class": "EMPIRICAL",
        "commit": COMMIT, "question": "What is the production value?",
        "expected": {"abstention_reason": "production value", "probes": [{"query": "production"}]},
    }

    def reply(self, kind: str, answer: str = "", citations: list | None = None) -> dict:
        return {"kind": kind, "answer": answer, "reason": "", "citations": citations or []}

    def test_correct_answer_needs_overlap_and_value(self) -> None:
        cited = [{"commit": self.COMMIT[:12], "path": "a/param.rs", "start_line": 12, "end_line": 14}]
        verdict = live_gate.score(self.ITEM, self.reply("answer", "0.3 public default", cited))
        self.assertEqual(verdict["category"], "correct")
        self.assertTrue(verdict["correct"])
        wrong = live_gate.score(self.ITEM, self.reply("answer", "0.4 public default", cited))
        self.assertEqual(wrong["category"], "wrong_value")
        elsewhere = [{"commit": self.COMMIT, "path": "a/param.rs", "start_line": 13, "end_line": 14}]
        uncited = live_gate.score(self.ITEM, self.reply("answer", "0.3", elsewhere))
        self.assertEqual(uncited["category"], "uncited")
        other_commit = [{"commit": "1" * 40, "path": "a/param.rs", "start_line": 10, "end_line": 12}]
        self.assertEqual(live_gate.score(self.ITEM, self.reply("answer", "0.3", other_commit))["category"],
                         "uncited")
        short = [{"commit": self.COMMIT[:6], "path": "a/param.rs", "start_line": 10, "end_line": 12}]
        self.assertEqual(live_gate.score(self.ITEM, self.reply("answer", "0.3", short))["category"],
                         "uncited")

    def test_abstentions_and_errors(self) -> None:
        self.assertEqual(live_gate.score(self.ITEM, self.reply("abstain"))["category"], "false_abstention")
        self.assertEqual(live_gate.score(self.ITEM, None)["category"], "error")
        self.assertEqual(live_gate.score(self.ABSTAIN, self.reply("abstain"))["category"],
                         "correct_abstention")
        self.assertEqual(live_gate.score(self.ABSTAIN, self.reply("answer", "0.3"))["category"],
                         "unsupported_answer")

    def test_parse_reply_accepts_structured_output_and_json_text(self) -> None:
        structured = {"structured_output": {"kind": "answer", "answer": "x", "citations": [
            {"commit": "ABC1234", "path": "./a.rs", "start_line": "3", "end_line": 4}]}}
        reply = live_gate.parse_reply(structured)
        assert reply is not None
        self.assertEqual(reply["citations"], [{"commit": "abc1234", "path": "a.rs",
                                               "start_line": 3, "end_line": 4}])
        text = {"result": 'noise {"kind": "abstain", "reason": "r"} noise'}
        parsed = live_gate.parse_reply(text)
        assert parsed is not None
        self.assertEqual(parsed["kind"], "abstain")
        self.assertIsNone(live_gate.parse_reply({"result": "no json here"}))
        self.assertIsNone(live_gate.parse_reply({"structured_output": {"kind": "maybe"}}))

    def test_summary_reports_denominators_and_budget_stops(self) -> None:
        data = {"status": "pending root review", "items": [self.ITEM, self.ABSTAIN,
                                                           dict(self.ITEM, id="T03")]}
        cited = [{"commit": self.COMMIT, "path": "a/param.rs", "start_line": 10, "end_line": 12}]
        rows = [
            {"id": "T01", "status": "run", "cost_usd": 0.05, "num_turns": 4, "models": ["m"],
             "usage": {"input_tokens": 10, "output_tokens": 5},
             "verdict": live_gate.score(self.ITEM, self.reply("answer", "0.3", cited))},
            {"id": "T02", "status": "run", "cost_usd": 0.02, "num_turns": 2, "models": ["m"],
             "usage": {"input_tokens": 3, "output_tokens": 1},
             "verdict": live_gate.score(self.ABSTAIN, self.reply("abstain"))},
            {"id": "T03", "status": "not_run", "reason": "budget", "cost_usd": 0.0,
             "verdict": live_gate.score(self.ITEM, None)},
        ]
        report = live_gate.summarize(data, rows)
        metrics = report["metrics"]
        self.assertEqual((metrics["precision"]["numerator"], metrics["precision"]["denominator"]), (1, 1))
        self.assertEqual((metrics["coverage"]["numerator"], metrics["coverage"]["denominator"]), (1, 1))
        self.assertEqual(metrics["coverage_of_set"]["denominator"], 2)
        self.assertEqual(metrics["abstention_accuracy"]["value"], 1.0)
        self.assertEqual(metrics["false_abstention_rate"]["value"], 0.0)
        self.assertEqual(metrics["citation_integrity"]["denominator"], 1)
        self.assertAlmostEqual(report["cost_usd"], 0.07)
        self.assertEqual(report["tokens"]["input_tokens"], 13)
        self.assertEqual(report["questions"]["not_run"], 1)
        self.assertFalse(report["gate"]["complete"])
        self.assertFalse(report["gate"]["passed"])
        text = live_gate.format_report(report, rows)
        self.assertIn("1 / 1", text)
        self.assertIn("T03  not_run", text)

    def test_claude_command_confines_the_agent_to_the_mcp_server(self) -> None:
        command = live_gate.claude_command("q", Path("cfg.json"), model="haiku",
                                           per_question_usd=0.2, max_turns=5, claude="claude")
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--allowedTools") + 1], "mcp__txray__*,mcp__txray")
        self.assertEqual(command[command.index("--output-format") + 1], "json")
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "0.20")
        self.assertIn("public default", command[command.index("--system-prompt") + 1])
        config = live_gate.mcp_config("/store", python="py")
        server = config["mcpServers"]["txray"]
        self.assertEqual(server["args"][:4], ["-m", "timelinexray", "mcp", "serve"])
        self.assertIn("PYTHONPATH", server["env"])


@unittest.skipIf(UPSTREAM is None, "no local x-algorithm clone with 77d431a "
                 "(set TXRAY_TEST_UPSTREAM to a local clone)")
class DeterministicGateUpstreamTest(unittest.TestCase):
    """eval/run_gate.py on a temporary store built from the local clone: every check passes."""

    @classmethod
    def setUpClass(cls) -> None:
        assert UPSTREAM is not None
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-eval-gate-")
        cls.store_dir = Path(cls._tmp.name) / "store"
        data = json.loads(QUESTIONS.read_text("utf-8"))
        # the shared template pins 4c5cfe8, a707cc2 and 77d431a and indexes 77d431a only,
        # exactly the sets eval/questions.json names
        assert data["commits"]["pinned"] == [OLD_PIN, NEW_PIN, UPSTREAM_COMMIT]
        assert data["commits"]["indexed"] == [UPSTREAM_COMMIT]
        copy_upstream_store("pins", cls.store_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_every_expected_citation_and_probe_passes(self) -> None:
        report = run_gate.run(self.store_dir, QUESTIONS)
        text = run_gate.format_report(report)
        print(f"\n{text}", file=sys.stderr)
        self.assertEqual(report["failures"], [])
        self.assertTrue(report["ok"])
        checks = report["checks"]
        for kind in ("span", "hash", "anchor", "value", "get_param", "find_symbols", "probe", "pinned"):
            with self.subTest(kind=kind):
                self.assertGreater(checks[kind]["total"], 0)
                self.assertEqual(checks[kind]["passed"], checks[kind]["total"])
        self.assertEqual(checks["span"]["total"], report["questions"]["citations"])
        self.assertEqual(checks["probe"]["total"], report["questions"]["probes"])
        self.assertTrue(any(re.search(r"\d+ lexically indexed files", scope)
                            for scope in report["search_scope"]))

    def test_main_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            code = run_gate.main(["--store", str(self.store_dir), "--json", str(out)])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out.read_text("utf-8"))["ok"])
        self.assertEqual(run_gate.main(["--store", str(Path(self._tmp.name) / "missing")]), 2)
