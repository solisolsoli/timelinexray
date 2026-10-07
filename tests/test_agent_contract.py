"""The answer-or-abstain contract (rules R1-R4) and the abstention development set.

The rules are stated in four places that agents read: ``docs/agents/README.md`` (the
reference), ``docs/agents/AGENTS-snippet.md`` (the template for other projects), the MCP
server ``instructions`` and the live harness prompt (contract ``r2``). These tests fail
when a copy loses a rule. The development set ``eval/dev-abstain.json`` is checked for
shape and for its separation from the release-gate sets, which stay byte-identical; with a
local upstream clone its citations and probes are checked through the MCP server like the
gate's. No model is called.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

from timelinexray.mcp.server import INSTRUCTIONS
from timelinexray.mcp.tools.code import SEARCH_CODE
from tests.support import REPO_ROOT, UPSTREAM_COMMIT, upstream_git_dir
from tests.templates import copy_upstream_store

EVAL = REPO_ROOT / "eval"
DEV = EVAL / "dev-abstain.json"
#: The release-gate sets are the held-out exam: this work never changes them.
GATE_SHA256 = {
    "questions.json": "e4e451e3c8eb3fe51b8562766b9f092e35d1980118d05aac4c92671d4ef90096",
    "questions-v2.json": "f76e8f6765f0769c84f4979e9c7656be78425b78e357c8a5ea3fe9593eb8538b",
}
#: SHA-256 of the r1 system prompt: the exact text of the 2026-10-02 and 2026-10-07 runs.
R1_PROMPT_SHA256 = "2e1f2121a666298f9f0d02c19830ae761633c36874dafc9a8a47849759fbac63"
RULES = (
    "R1 Request-time values are not in the code",
    "R2 Rules need a span that states them",
    "R3 Search budget",
    "R4 An abstention states its scope",
)
PHRASES = ("explaining the mechanism is not an answer", "or eight in all",
           "supports neither", "shadowban")
UPSTREAM = upstream_git_dir()


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"txray_contract_{name}", EVAL / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"txray_contract_{name}"] = module
    spec.loader.exec_module(module)
    return module


run_gate = _load("run_gate")
live_gate = _load("live_gate")


def _flat(text: str) -> str:
    return " ".join(text.replace("*", "").split()).lower()


class ContractCopiesTest(unittest.TestCase):
    def copies(self) -> dict[str, str]:
        return {
            "docs/agents/README.md": (REPO_ROOT / "docs" / "agents" / "README.md").read_text("utf-8"),
            "docs/agents/AGENTS-snippet.md":
                (REPO_ROOT / "docs" / "agents" / "AGENTS-snippet.md").read_text("utf-8"),
            "MCP instructions": INSTRUCTIONS,
            "harness prompt r2": live_gate.CONTRACTS["r2"][0],
        }

    def test_every_copy_states_every_rule(self) -> None:
        for name, text in self.copies().items():
            flat = _flat(text)
            for needle in (*RULES, *PHRASES):
                with self.subTest(copy=name, rule=needle):
                    self.assertIn(needle.lower(), flat)

    def test_the_repository_contract_points_to_the_rules(self) -> None:
        agents = (REPO_ROOT / "AGENTS.md").read_bytes()
        self.assertEqual(agents, (REPO_ROOT / "CLAUDE.md").read_bytes())
        flat = _flat(agents.decode("utf-8"))
        for needle in ("rules r1-r4", "request-time value", "or eight in all",
                       "an abstention states its scope"):
            self.assertIn(needle, flat)

    def test_search_code_says_zero_hits_is_not_absence(self) -> None:
        self.assertIn("not that a behaviour is absent", SEARCH_CODE.description)

    def test_the_r1_contract_is_kept_verbatim(self) -> None:
        prompt, schema = live_gate.CONTRACTS["r1"]
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        self.assertEqual(digest, R1_PROMPT_SHA256)
        self.assertNotIn("searched", schema["properties"])
        self.assertIn("searched", live_gate.CONTRACTS["r2"][1]["properties"])
        self.assertEqual(live_gate.DEFAULT_CONTRACT, "r2")

    def test_claude_command_states_the_chosen_contract(self) -> None:
        for contract, (prompt, schema) in live_gate.CONTRACTS.items():
            command = live_gate.claude_command("q", Path("cfg.json"), model="haiku",
                                               per_question_usd=0.2, max_turns=5,
                                               claude="claude", contract=contract)
            with self.subTest(contract=contract):
                self.assertEqual(command[command.index("--system-prompt") + 1], prompt)
                self.assertEqual(json.loads(command[command.index("--json-schema") + 1]), schema)

    def test_server_src_override(self) -> None:
        config = live_gate.mcp_config("/store", python="py", server_src="/elsewhere/src")
        self.assertEqual(config["mcpServers"]["txray"]["env"]["PYTHONPATH"], "/elsewhere/src")
        default = live_gate.mcp_config("/store", python="py")
        self.assertTrue(default["mcpServers"]["txray"]["env"]["PYTHONPATH"].endswith("src"))


class GateSetsUnchangedTest(unittest.TestCase):
    def test_gate_sets_are_byte_identical(self) -> None:
        for name, digest in GATE_SHA256.items():
            with self.subTest(file=name):
                self.assertEqual(hashlib.sha256((EVAL / name).read_bytes()).hexdigest(), digest)

    def test_the_default_question_set_is_still_the_gate(self) -> None:
        self.assertEqual(Path(run_gate.DEFAULT_QUESTIONS).name, "questions-v2.json")
        self.assertFalse(run_gate.is_development(run_gate.load_questions()))


class DevelopmentSetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = json.loads(DEV.read_text("utf-8"))

    def test_well_formed_and_labelled(self) -> None:
        self.assertEqual(run_gate.validate(self.data), [])
        self.assertTrue(run_gate.is_development(self.data))
        self.assertIn("not the release gate", self.data["status"])
        self.assertIn("NOT the release gate", self.data["description"])

    def test_families_and_controls(self) -> None:
        items = self.data["items"]
        abstain = [item for item in items if item["kind"] == "abstain"]
        answer = [item for item in items if item["kind"] == "answer"]
        families = {item["family"] for item in abstain}
        self.assertEqual(families, {"per-request", "live-config", "moderation"})
        for family in families:
            with self.subTest(family=family):
                self.assertGreaterEqual(sum(item["family"] == family for item in abstain), 3)
                controls = [item for item in answer if item["family"] == f"control-{family}"]
                self.assertGreaterEqual(len(controls), 3)
        # abstaining on everything must not look good: at least 40 % are answerable
        self.assertGreaterEqual(len(answer) / len(items), 0.4)
        for item in answer:
            self.assertIsNone(item["source"])
            self.assertTrue(item["expected"]["citations"])

    def test_disjoint_from_the_gate_sets(self) -> None:
        gate_questions: set[str] = set()
        gate_ids: set[str] = set()
        for name in GATE_SHA256:
            data = json.loads((EVAL / name).read_text("utf-8"))
            gate_questions |= {_flat(item["question"]) for item in data["items"]}
            gate_ids |= {item["id"] for item in data["items"]}
        for item in self.data["items"]:
            with self.subTest(item=item["id"]):
                self.assertNotIn(item["id"], gate_ids)
                self.assertNotIn(_flat(item["question"]), gate_questions)

    def test_validation_rejects_a_mislabelled_development_set(self) -> None:
        broken = json.loads(json.dumps(self.data))
        broken["status"] = "root-reviewed"
        broken["items"][0]["id"] = "A01"
        del broken["items"][1]["family"]
        problems = run_gate.validate(broken)
        self.assertTrue(any("not the release gate" in p for p in problems))
        self.assertTrue(any("start with D" in p for p in problems))
        self.assertTrue(any("missing 'family'" in p for p in problems))
        gate = json.loads((EVAL / "questions-v2.json").read_text("utf-8"))
        gate["items"][0]["source"] = None
        self.assertTrue(any("needs a source finding" in p for p in run_gate.validate(gate)))

    def test_controls_score_correct_answers_and_reject_wrong_ones(self) -> None:
        items = {item["id"]: item for item in self.data["items"]}

        def reply(item_id: str, text: str) -> dict:
            wanted = items[item_id]["expected"]["citations"][0]
            return {"kind": "answer", "answer": text, "reason": "", "searched": [],
                    "citations": [{key: wanted[key] for key in
                                   ("commit", "path", "start_line", "end_line")}]}

        cases = {
            "DC01": ("Yes: it removes candidates whose author the viewer muted or blocked.",
                     "No, muted and blocked authors are kept."),
            "DC03": ("No. The age falls back to unwrap_or(false), so the candidate is removed.",
                     "Yes, it is kept."),
            "DC04": ("48 hours (48 * 60 * 60 seconds, public default MAX_POST_AGE in config.rs).",
                     "24 hours, defined in config.rs as MAX_POST_AGE."),
            "DC06": ("No: it only removes out-of-network SimClusters posts by NSFW authors.",
                     "Yes, every NSFW author is removed, in-network too."),
            "DC07": ("The public default is false.", "The public default is true."),
            "DC08": ("15 percent.", "1500 likes."),
            "DC09": ("Public default 1800 seconds.", "Public default 3600 seconds."),
            "DC11": ("No, an in-network candidate always returns true in should_keep.",
                     "Yes, it removes in-network posts below the threshold."),
        }
        for item_id, (right, wrong) in cases.items():
            with self.subTest(item=item_id):
                good = live_gate.score(items[item_id], reply(item_id, right))
                bad = live_gate.score(items[item_id], reply(item_id, wrong))
                self.assertEqual(good["category"], "correct", right)
                self.assertNotEqual(bad["category"], "correct", wrong)


class DevelopmentReportTest(unittest.TestCase):
    def test_development_report_has_no_gate_verdict(self) -> None:
        data = json.loads(DEV.read_text("utf-8"))
        item = next(item for item in data["items"] if item["kind"] == "abstain")
        reply = {"kind": "abstain", "answer": "", "citations": [], "reason": "not in the code",
                 "searched": ["shadowbanned"]}
        rows = [{"id": item["id"], "status": "run", "verdict": live_gate.score(item, reply),
                 "cost_usd": 0.01, "num_turns": 3}]
        report = live_gate.summarize(data, rows)
        self.assertIsNone(report["gate"]["passed"])
        self.assertTrue(report["gate"]["development"])
        self.assertEqual(report["questions"]["purpose"], "development")
        self.assertEqual(report["metrics"]["abstentions_with_scope"]["numerator"], 1)
        self.assertEqual(report["families"][item["family"]], {"run": 1, "correct": 1})
        text = live_gate.format_report(report, rows)
        self.assertIn("development set: not the release gate", text)
        self.assertNotIn("PASS", text)

    def test_scope_is_stated_by_queries_or_the_commit(self) -> None:
        item = {"commit": UPSTREAM_COMMIT}
        self.assertTrue(live_gate.scope_stated(item, {"searched": ["x"], "reason": ""}))
        self.assertTrue(live_gate.scope_stated(item, {"searched": [],
                                                      "reason": f"searched at {UPSTREAM_COMMIT[:7]}"}))
        self.assertFalse(live_gate.scope_stated(item, {"searched": [], "reason": "not found"}))
        self.assertFalse(live_gate.scope_stated(item, {"searched": [], "reason": "at deadbeef"}))

    def test_parse_reply_keeps_searched(self) -> None:
        parsed = live_gate.parse_reply({"structured_output": {
            "kind": "abstain", "reason": "r", "searched": ["a", 3, "b"]}})
        self.assertEqual(parsed["searched"], ["a", "b"])


@unittest.skipIf(UPSTREAM is None, "no local x-algorithm clone with 77d431a "
                 "(set TXRAY_TEST_UPSTREAM to a local clone)")
class DevelopmentSetUpstreamTest(unittest.TestCase):
    """Every expected citation of the development set reads back with its oracle hash and
    anchor, and every probe has zero hits, through the MCP server."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-dev-set-")
        cls.store_dir = Path(cls._tmp.name) / "store"
        copy_upstream_store("pins", cls.store_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_every_citation_and_probe_passes(self) -> None:
        report = run_gate.run(self.store_dir, DEV)
        text = run_gate.format_report(report)
        self.assertEqual(report["failures"], [], text)
        self.assertTrue(report["ok"])
        self.assertEqual(report["questions"]["purpose"], "development")
        self.assertIn("not the gate", text)
        for kind in ("span", "hash", "anchor", "probe", "get_param", "value"):
            with self.subTest(kind=kind):
                self.assertGreater(report["checks"][kind]["total"], 0)


if __name__ == "__main__":
    unittest.main()
