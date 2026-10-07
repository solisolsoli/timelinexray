"""The semantic release gate: the question set, the deterministic harness, the live scorer.

The structural checks and the scoring rules always run (no network, no model). The
deterministic harness runs against a local clone of xai-org/x-algorithm with 4c5cfe8,
a707cc2 and 77d431a (``$TXRAY_TEST_UPSTREAM`` or ``../x-algorithm-upstream``), pinned
through a ``file://`` URL and indexed at 77d431a in a temporary store; the MCP server runs
as a child process like it does for any client. Nothing is fetched from the network.
"""

from __future__ import annotations

import hashlib
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
QUESTIONS_V2 = EVAL / "questions-v2.json"
#: Revision 1 is immutable: its live runs (eval/live-run-2026-10-0*.txt) were scored with it.
V1_SHA256 = "e4e451e3c8eb3fe51b8562766b9f092e35d1980118d05aac4c92671d4ef90096"
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

    PATH = QUESTIONS

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = json.loads(cls.PATH.read_text("utf-8"))

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
        broken = json.loads(self.PATH.read_text("utf-8"))
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


class QuestionSetV2Test(QuestionSetTest):
    """eval/questions-v2.json passes every structural check revision 1 passes."""

    PATH = QUESTIONS_V2


def _regex_ok(item: dict, answer: str) -> bool:
    return all(re.search(pattern, answer, re.IGNORECASE | re.DOTALL)
               for pattern in item["expected"]["answer_regexes"])


class SetRevisionTest(unittest.TestCase):
    """Revision 2 of the question set: revision 1 untouched, every change listed with its
    reason, and each changed pattern accepts the correct replies v1 rejected while still
    rejecting wrong ones (synthetic replies written for this test; no model involved)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.v1 = json.loads(QUESTIONS.read_text("utf-8"))
        cls.v2 = json.loads(QUESTIONS_V2.read_text("utf-8"))
        cls.items1 = {item["id"]: item for item in cls.v1["items"]}
        cls.items2 = {item["id"]: item for item in cls.v2["items"]}

    def test_revision_one_is_unchanged_and_named_as_the_base(self) -> None:
        digest = hashlib.sha256(QUESTIONS.read_bytes()).hexdigest()
        self.assertEqual(digest, V1_SHA256, "eval/questions.json (revision 1) must never change")
        self.assertNotIn("revision", self.v1)
        self.assertEqual(run_gate.revision_number(self.v1), 1)
        self.assertEqual(self.v1["status"], "root-reviewed 2026-10-01")
        revision = self.v2["revision"]
        self.assertEqual(run_gate.revision_number(self.v2), 2)
        self.assertEqual(revision["base"], {"file": "eval/questions.json", "number": 1,
                                            "sha256": V1_SHA256,
                                            "status": "root-reviewed 2026-10-01"})
        self.assertEqual(self.v2["status"], "root-reviewed 2026-10-07")
        self.assertIn("unchanged", revision["thresholds"])
        self.assertEqual(run_gate.DEFAULT_QUESTIONS, QUESTIONS_V2)

    def test_only_listed_items_and_fields_differ(self) -> None:
        self.assertEqual(list(self.items1), list(self.items2))
        self.assertEqual(self.v1["commits"], self.v2["commits"])
        self.assertEqual(set(self.v2) - set(self.v1), {"revision"})
        listed = {change["id"]: change["fields"] for change in self.v2["revision"]["changes"]}
        for item_id, old in self.items1.items():
            new = self.items2[item_id]
            with self.subTest(item=item_id):
                fields = listed.get(item_id, [])
                for key in run_gate.ITEM_KEYS - {"expected"}:
                    if key not in fields:
                        self.assertEqual(new[key], old[key])
                for key in set(old["expected"]) | set(new["expected"]):
                    if f"expected.{key}" not in fields:
                        self.assertEqual(new["expected"].get(key), old["expected"].get(key))
                    else:
                        self.assertNotEqual(new["expected"][key], old["expected"][key])
                if item_id not in listed:
                    self.assertEqual(new, old)
        self.assertEqual({i for i in listed}, {"Q01", "Q02", "Q03", "Q04", "Q05", "Q15", "Q16",
                                                "Q17", "Q18", "Q19", "Q20", "Q21", "Q22", "Q25",
                                                "Q27", "Q29"})
        for item in self.v2["items"]:
            if item["kind"] == "abstain":
                self.assertNotIn(item["id"], listed)

    def test_existing_expected_citations_are_kept(self) -> None:
        for item_id, old in self.items1.items():
            if old["kind"] != "answer":
                continue
            new = self.items2[item_id]["expected"]["citations"]
            with self.subTest(item=item_id):
                self.assertEqual(new[:len(old["expected"]["citations"])],
                                 old["expected"]["citations"])
        added = self.items2["Q22"]["expected"]["citations"][1]
        self.assertEqual((added["path"], added["start_line"], added["end_line"]),
                         ("under-the-hood/scalding/UthDailyPostsJob.scala", 227, 227))

    def test_q22_second_citation_scores_the_reply_of_the_second_live_run(self) -> None:
        # the 2026-10-02 reply cited lines 217-232; v1 expected only line 200 (uncited)
        cited = [{"commit": "77d431aabf40", "start_line": 217, "end_line": 232,
                  "path": "under-the-hood/scalding/UthDailyPostsJob.scala"}]
        reply = {"kind": "answer", "reason": "", "citations": cited,
                 "answer": "The logical id is initialTweetId.getOrElse(tweetId) at 77d431a."}
        self.assertEqual(live_gate.score(self.items1["Q22"], reply)["category"], "uncited")
        self.assertEqual(live_gate.score(self.items2["Q22"], reply)["category"], "correct")

    CASES = {
        # id: (correct replies v1 rejected, wrong replies v2 must still reject)
        "Q01": (["Thunder, Tweet Mixer, SimClusters, Phoenix, Phoenix Topics, Phoenix MoE and "
                 "cached posts, in that order."],
                ["Phoenix, Thunder, Tweet Mixer, SimClusters, Phoenix Topics and cached posts."]),
        "Q02": (["19 filters; the last two are the FavHoldout and InventoryHoldout filters.",
                 "Nineteen; it ends with InventoryHoldout and then FavHoldout."],
                ["18 filters, ending with InventoryHoldoutFilter and FavHoldoutFilter.",
                 "19 filters, ending with VideoFilter and FavHoldoutFilter."]),
        "Q03": (["The Phoenix scorer, then the VM ranker."],
                ["The VM ranker, then the Phoenix scorer."]),
        "Q04": (["DedupConversationFilter, AncillaryVFFilter and VFFilter.",
                 "The VF filter, the ancillary VF filter and the dedup conversation filter."],
                ["AncillaryVFFilter and DedupConversationFilter.",
                 "VFFilter and DedupConversationFilter."]),
        "Q05": (["When EnablePhoenixSource is on, the request is not a topic request (or is a "
                 "bulk topic request), it is not in-network-only and it has no cached posts."],
                ["When EnablePhoenixSource is on and the request is not a topic request.",
                 "When the request is not in-network-only and has no cached posts."]),
        "Q15": (["Its public default is –47.52.", "A public default of negative 47.52."],
                ["Its public default is 47.52.", "Its public default is -47.5."]),
        "Q19": (["Its public default is –0.02."], ["Its public default is 0.02."]),
        "Q20": (["total_sum is the negative sum plus the positive sum."],
                ["total_sum is the sum of the positive weights only."]),
        "Q21": (["The share source tweet id must be empty and the post must not be null-cast."],
                ["The post must not be null-cast."]),
        "Q22": (["For an edited post the logical id is its initial tweet id.",
                 "It uses the initial post id."],
                ["It uses the tweet id of the latest edit."]),
        "Q25": (["Its public default is 1,200."],
                ["Its public default is 1,200,000.", "Its public default is 12000.",
                 "Its public default is 1,100."]),
        "Q27": (["Its public default is 1,000."],
                ["Its public default is 10,000.", "Its public default is 1,000.5."]),
        "Q29": (["The value is 2,800."], ["The value is 2,8000.", "The value is 28,000."]),
    }

    def test_changed_patterns_accept_correct_and_reject_wrong_replies(self) -> None:
        for item_id, (correct, wrong) in self.CASES.items():
            for answer in correct:
                with self.subTest(item=item_id, answer=answer):
                    self.assertFalse(_regex_ok(self.items1[item_id], answer), "v1 accepted it")
                    self.assertTrue(_regex_ok(self.items2[item_id], answer))
            for answer in wrong:
                with self.subTest(item=item_id, answer=answer):
                    self.assertFalse(_regex_ok(self.items2[item_id], answer))

    def test_replies_v1_accepted_are_still_accepted(self) -> None:
        kept = {
            "Q01": "thunder_source, tweet_mixer_source, simclusters_source, phoenix_source, "
                   "phoenix_topics_source, phoenix_moe_source, cached_posts_source",
            "Q02": "19 filters; the last two are InventoryHoldoutFilter and FavHoldoutFilter.",
            "Q03": "vec![phoenix_scorer, vm_ranker]",
            "Q04": "VFFilter, AncillaryVFFilter, DedupConversationFilter",
            "Q05": "EnablePhoenixSource, not a topic request, !in_network_only, !has_cached_posts",
            "Q15": "public default -47.52", "Q16": "public default −31.2",
            "Q17": "public default minus 58.8", "Q18": "public default -234.0",
            "Q19": "public default -0.02", "Q20": "self.positive_sum() + self.negative_sum()",
            "Q21": "shareSourceTweetId.isEmpty and !nullcast",
            "Q22": "initialTweetId.getOrElse(tweetId)", "Q25": "public default 1200",
            "Q27": "public default 1000", "Q29": "2800",
        }
        for item_id, answer in kept.items():
            with self.subTest(item=item_id):
                self.assertTrue(_regex_ok(self.items1[item_id], answer))
                self.assertTrue(_regex_ok(self.items2[item_id], answer))

    def test_validate_checks_the_revision_record(self) -> None:
        broken = json.loads(QUESTIONS_V2.read_text("utf-8"))
        broken["revision"]["base"]["number"] = 2
        broken["revision"]["changes"].append(dict(broken["revision"]["changes"][0]))
        broken["revision"]["changes"].append({"id": "Q99", "fields": ["expected.citations"],
                                              "basis": "x", "reason": "y"})
        broken["revision"]["changes"].append({"id": "Q03", "fields": ["status"],
                                              "basis": "x", "reason": ""})
        problems = run_gate.validate(broken)
        self.assertTrue(any("revision.base" in p for p in problems))
        self.assertTrue(any("listed twice" in p for p in problems))
        self.assertTrue(any("Q99: no such item" in p for p in problems))
        self.assertTrue(any("Q03: fields" in p for p in problems))
        self.assertTrue(any("Q03: reason" in p for p in problems))

    def test_reports_name_the_revision(self) -> None:
        report = run_gate.summarize(self.v2, [])
        self.assertEqual(report["questions"]["revision"], 2)
        self.assertIn("revision 2; status: root-reviewed 2026-10-07", run_gate.format_report(report))
        live = live_gate.summarize(self.v1, [])
        self.assertEqual(live["questions"]["revision"], 1)
        self.assertIn("revision 1; status: root-reviewed 2026-10-01",
                      live_gate.format_report(live, []))


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
        for path, citations in ((QUESTIONS, 49), (QUESTIONS_V2, 50)):
            with self.subTest(questions=path.name):
                report = run_gate.run(self.store_dir, path)
                text = run_gate.format_report(report)
                print(f"\n{text}", file=sys.stderr)
                self.assertEqual(report["failures"], [])
                self.assertTrue(report["ok"])
                checks = report["checks"]
                for kind in ("span", "hash", "anchor", "value", "get_param", "find_symbols",
                             "probe", "pinned"):
                    with self.subTest(kind=kind):
                        self.assertGreater(checks[kind]["total"], 0)
                        self.assertEqual(checks[kind]["passed"], checks[kind]["total"])
                self.assertEqual(report["questions"]["citations"], citations)
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
