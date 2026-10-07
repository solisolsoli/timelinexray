"""The MCP findings tools (Milestone 4b): ``find_findings``, ``get_finding``, ``verify_claim``.

A synthetic history (``tests.findings_support.HistoryRepo``) is pinned into a temporary
store and a temporary findings ledger is filled through the Milestone 3 API: reviewed,
draft, stale, unchecked, superseded, retracted, negative and dependent findings, verified
at their cited commit and re-anchored on two later commits. The tools are exercised in
process (every result validated against its published ``outputSchema``) and over stdio in a
child process. ``UpstreamFindingsStdioTest`` re-anchors a real ClickWeight finding from
4c5cfe8 to a707cc2 of a local x-algorithm clone (``TXRAY_TEST_UPSTREAM``; skipped without).

Every test that calls a tool also checks the no-write guarantee where it matters: the
ledger directory (names, bytes, sizes, modification times, HEAD) is identical before and
after, and no lock file is created.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from timelinexray.errors import Refused
from timelinexray.findings import FindingsMemory, Ledger
from timelinexray.mcp import schema as S
from timelinexray.mcp.guard import StoreGuard
from timelinexray.mcp.server import MAX_RESPONSE_BYTES, Server
from timelinexray.mcp.tools import build_registry
from timelinexray.mcp_cli import _ledger_location
from tests.findings_support import AUTHOR, REVIEWER, HistoryRepo, spec
from tests.mcp_support import (
    EXPECTED_TOOLS,
    FINDINGS_TOOLS,
    LEGACY,
    StdioClient,
    call,
    child_env,
    initialize,
    request,
)
from tests.support import file_url, git_show, run_cli, upstream_git_dir

REGISTRY = build_registry()
UPSTREAM = upstream_git_dir()


class Fixture:
    """The synthetic history, pinned and partly indexed, plus a filled findings ledger."""

    def __init__(self) -> None:
        self.repo = repo = HistoryRepo()
        repo.indexed("base", "shift", "negative")
        self.store_dir = repo.store_dir
        self.ledger = repo.root / "ledger"
        self.commits = repo.commits
        memory = FindingsMemory(Ledger(self.ledger), repo.store, repo.index)
        memory.add(spec(repo, finding_id="F-click", title="Synthetic click weight"), AUTHOR)
        memory.add(spec(repo, finding_id="F-reply", title="Synthetic reply weight",
                        claim="The fixture sets REPLY_WEIGHT to 5.0 as a public default.",
                        citations=[repo.cite("base", "src/weights.rs", "3", "REPLY_WEIGHT")]),
                   AUTHOR)
        memory.add({
            "finding_id": "F-filters", "title": "Filter list", "component": "filters",
            "claim": "The fixture declares two filters.", "evidence_class": "CODE",
            "status": "PARTIAL", "limitations": ["only the list, not its use"],
            "citations": [repo.cite("base", "src/filters.py", "1-4", "DropDuplicates")],
        }, AUTHOR)
        memory.add({
            "finding_id": "F-web", "title": "External statement", "component": "docs",
            "claim": "An external page describes the fixture.", "evidence_class": "THIRD_PARTY",
            "status": "EXTERNAL_RECHECK",
            "web_sources": [{"url": "https://example.org/fixture", "publisher": "Example",
                             "published": "2026-01", "retrieved": "2026-09-30",
                             "quote": "synthetic quote"}],
        }, AUTHOR)
        memory.add({
            "finding_id": "F-boost", "title": "No boost weight", "component": "src",
            "claim": "The fixture has no BOOST_WEIGHT.", "evidence_class": "CODE",
            "status": "NOT_FOUND",
            "negative": {"commit": self.commits["base"], "query": "BOOST_WEIGHT",
                         "literal": True},
        }, AUTHOR)
        memory.add(spec(repo, finding_id="F-fav", title="Synthetic favorite weight",
                        claim="The fixture sets FAVORITE_WEIGHT to 0.5.",
                        citations=[repo.cite("base", "src/weights.rs", "2", "FAVORITE_WEIGHT")]),
                   AUTHOR)
        memory.add({
            "finding_id": "F-notes", "title": "Notes file", "component": "docs",
            "claim": "The fixture has notes.", "evidence_class": "REPO_DOC",
            "status": "SUPPORTED",
            "citations": [repo.cite("base", "docs/notes.md", "3", "Synthetic notes")],
        }, AUTHOR)
        memory.add({
            "finding_id": "F-dep", "title": "Filter application", "component": "filters",
            "claim": "apply keeps truthy candidates.", "evidence_class": "CODE",
            "status": "SUPPORTED",
            "citations": [repo.cite("base", "src/filters.py", "6-7", "def apply")],
            "depends_on": [{"finding": "F-filters"},
                           {"span": repo.cite("base", "src/weights.rs", "6-8", "pub fn score")}],
        }, AUTHOR)
        memory.verify()
        memory.review("F-click", REVIEWER, "SUPPORTED", "Read the cited span at base.")
        memory.supersede("F-fav", AUTHOR, by="F-reply", rationale="merged into F-reply")
        memory.retract("F-notes", AUTHOR, "not about the algorithm")
        memory.reanchor(self.commits["shift"])
        memory.reanchor(self.commits["literal"], ["F-click"])
        self.memory = memory

    def server(self, **kwargs: Any) -> Server:
        ledger = kwargs.pop("ledger", self.ledger)
        return Server(StoreGuard(self.store_dir, ledger), log=io.StringIO(), **kwargs)

    def cleanup(self) -> None:
        self.repo.cleanup()


FIX: Fixture


def setUpModule() -> None:
    global FIX
    FIX = Fixture()


def tearDownModule() -> None:
    FIX.cleanup()


def ledger_state(directory: Path) -> dict[str, Any]:
    """Everything a write could change in a ledger directory."""
    state: dict[str, Any] = {".": os.stat(directory).st_mtime_ns}
    for path in sorted(directory.iterdir()):
        info = path.lstat()
        data = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        state[path.name] = (info.st_mode, info.st_size, info.st_mtime_ns, data)
    return state


def exchange(server: Server, message: Any) -> dict[str, Any]:
    line = server.handle_line(json.dumps(message).encode("utf-8"))
    assert line is not None
    assert len(line) <= server.max_response_bytes
    return json.loads(line.decode("utf-8"))


def use(server: Server, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """The envelope of one tools/call, validated against the published outputSchema."""
    result = exchange(server, call(name, arguments))["result"]
    envelope = result["structuredContent"]
    S.validate(envelope, server.registry.get(name).strict_output_schema)
    S.validate(envelope, server.registry.get(name).output_schema)
    assert json.loads(result["content"][0]["text"]) == envelope
    assert result["isError"] == (envelope["outcome"] in ("NOT_FOUND", "DENIED", "ERROR"))
    return envelope


def ids(envelope: dict[str, Any]) -> list[str]:
    return [item["finding_id"] for item in envelope["data"]["findings"]]


def line_hash(git_dir: Path, commit: str, path: str, start: int, end: int) -> str:
    lines = git_show(git_dir, commit, path).splitlines(keepends=True)
    return hashlib.sha256(b"".join(lines[start - 1:end])).hexdigest()


class FindFindingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FIX.server()
        self.before = ledger_state(FIX.ledger)

    def tearDown(self) -> None:
        self.assertEqual(ledger_state(FIX.ledger), self.before, "a call changed the ledger")

    def test_default_lists_only_current_active_findings(self) -> None:
        found = use(self.server, "find_findings", {})
        self.assertEqual(found["outcome"], "OK")
        # F-web (external evidence only) is current by default: NOT_APPLICABLE, nothing
        # to re-verify; every fixture commit shares one committer time, so no pin is
        # newer than another and the re-anchored findings are current at `shift`
        self.assertEqual(ids(found), ["F-reply", "F-filters", "F-web", "F-boost", "F-dep"])
        for item in found["data"]["findings"]:
            with self.subTest(finding=item["finding_id"]):
                self.assertTrue(item["current"])
                self.assertEqual(item["freshness"]["newer_pins"], [])
                if item["finding_id"] == "F-web":
                    self.assertEqual(item["freshness"]["value"], "NOT_APPLICABLE")
                    self.assertFalse(item["freshness"]["checkable"])
                    continue
                self.assertTrue(item["freshness"]["checkable"])
                self.assertEqual(item["freshness"]["value"], "CURRENT")
                self.assertEqual(item["freshness"]["commit"], FIX.commits["shift"])
                self.assertEqual(item["freshness"]["check"], "reanchor")
        data = found["data"]
        self.assertTrue(data["current_only"])
        self.assertEqual(data["workflows"], ["draft", "imported", "reviewed"])
        self.assertEqual(data["not_current"], {"STALE": 1})
        self.assertIn("1 further matching finding(s) are not current", found["warnings"][0])
        self.assertIn(data["newest_pin"], FIX.commits.values())
        self.assertIsNone(found["commit"])
        self.assertEqual(data["ledger"]["head"], Ledger(FIX.ledger).verify().last_hash)
        self.assertTrue(any("status_basis=reviewed" in note for note in found["notes"]))
        self.assertTrue(any("public defaults" in note for note in found["notes"]))

    def test_current_only_false_labels_every_finding(self) -> None:
        found = use(self.server, "find_findings", {"current_only": False})
        self.assertEqual(ids(found), ["F-click", "F-reply", "F-filters", "F-web", "F-boost",
                                      "F-dep"])
        by_id = {item["finding_id"]: item for item in found["data"]["findings"]}
        click = by_id["F-click"]
        self.assertEqual((click["status"], click["status_basis"], click["reviewed"],
                          click["status_by"]), ("SUPPORTED", "reviewed", True, "reviewer-b"))
        self.assertEqual(click["freshness"]["value"], "STALE")
        self.assertEqual(click["freshness"]["commit"], FIX.commits["literal"])
        self.assertFalse(click["current"])
        self.assertEqual(click["review"]["last_status"], "SUPPORTED")
        self.assertEqual(click["review"]["open_triggers"], ["changed_span"])
        reply = by_id["F-reply"]
        self.assertEqual((reply["status_basis"], reply["reviewed"]), ("proposed", False))
        self.assertEqual(reply["review"]["open_triggers"], ["awaiting_review"])
        web = by_id["F-web"]
        self.assertTrue(web["current"])
        self.assertEqual({key: web["freshness"][key] for key in (
            "value", "commit", "check", "event", "checkable", "retrieved", "recheck_after")}, {
            "value": "NOT_APPLICABLE", "commit": None, "check": None, "event": None,
            "checkable": False, "retrieved": "2026-09-30", "recheck_after": None})
        self.assertIn("external evidence only", web["freshness"]["note"])
        self.assertEqual(found["data"]["not_current"], {})
        old = use(self.server, "find_findings", {"current_only": False,
                                                 "workflows": ["superseded", "retracted"]})
        self.assertEqual(ids(old), ["F-fav", "F-notes"])
        fav, notes = old["data"]["findings"]
        self.assertEqual((fav["workflow"], fav["superseded_by"], fav["current"]),
                         ("superseded", "F-reply", False))
        self.assertEqual((notes["workflow"], notes["current"]), ("retracted", False))

    def test_a_historical_commit_names_its_commit(self) -> None:
        base = FIX.commits["base"]
        found = use(self.server, "find_findings", {"commit": base})
        self.assertEqual(ids(found), ["F-click", "F-reply", "F-filters", "F-web", "F-boost",
                                      "F-dep"])
        self.assertEqual(found["data"]["evaluated_at"], base)
        for item in found["data"]["findings"]:
            if item["finding_id"] == "F-web":
                self.assertEqual(item["freshness"]["value"], "NOT_APPLICABLE")
                continue
            self.assertEqual((item["freshness"]["commit"], item["freshness"]["check"]),
                             (base, "integrity"))
        literal = FIX.commits["literal"]
        none = use(self.server, "find_findings", {"commit": literal})
        self.assertEqual((ids(none), none["data"]["not_current"]),
                         (["F-web"], {"NOT_CHECKED": 4, "STALE": 1}))
        shown = use(self.server, "find_findings", {"commit": literal, "current_only": False})
        by_id = {item["finding_id"]: item["freshness"] for item in shown["data"]["findings"]}
        self.assertEqual((by_id["F-click"]["value"], by_id["F-click"]["commit"]),
                         ("STALE", literal))
        self.assertEqual((by_id["F-reply"]["value"], by_id["F-reply"]["commit"]),
                         ("NOT_CHECKED", literal))

    def test_filters(self) -> None:
        cases = [
            ({"query": "click", "current_only": False}, ["F-click"]),
            ({"query": "SYNTHETIC weight", "current_only": False}, ["F-click", "F-reply"]),
            ({"query": "not its use"}, ["F-filters"]),
            ({"component": "FILTERS"}, ["F-filters", "F-dep"]),
            ({"statuses": ["NOT_FOUND"]}, ["F-boost"]),
            ({"evidence_classes": ["PARAM_DEFAULT"], "current_only": False},
             ["F-click", "F-reply"]),
            ({"freshness": ["STALE"], "current_only": False}, ["F-click"]),
            ({"freshness": ["NOT_CHECKED"], "current_only": False}, []),
            ({"freshness": ["NOT_APPLICABLE"]}, ["F-web"]),
            ({"evidence_classes": ["THIRD_PARTY"]}, ["F-web"]),
            ({"statuses": ["EXTERNAL_RECHECK"]}, ["F-web"]),
            ({"freshness": ["CURRENT"]}, ["F-reply", "F-filters", "F-boost", "F-dep"]),
            ({"workflows": ["reviewed"], "current_only": False}, ["F-click"]),
            ({"workflows": ["draft"], "statuses": ["SUPPORTED"]}, ["F-reply", "F-dep"]),
            ({"query": "no such words anywhere"}, []),
        ]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                found = use(self.server, "find_findings", arguments)
                self.assertEqual(found["outcome"], "OK")
                self.assertEqual(ids(found), expected)

    def test_invalid_arguments_and_conflicts(self) -> None:
        cases = [
            ({"freshness": ["STALE"]}, "invalid_input", "current_only"),
            ({"workflows": ["retracted"]}, "invalid_input", "never current"),
            ({"query": "   "}, "invalid_input", "no terms"),
            ({"query": " ".join(f"t{n}" for n in range(17))}, "invalid_input", "at most 16"),
            ({"limit": 0}, "invalid_arguments", "limit"),
            ({"limit": 21}, "invalid_arguments", "limit"),
            ({"limit": 2.0}, "invalid_arguments", "limit"),
            ({"statuses": []}, "invalid_arguments", "statuses"),
            ({"statuses": ["TRUE"]}, "invalid_arguments", "statuses"),
            ({"freshness": ["CURRENT", "CURRENT", "CURRENT", "CURRENT", "CURRENT", "CURRENT"]},
             "invalid_arguments", "freshness"),
            ({"commit": "abc"}, "invalid_arguments", "commit"),
            ({"commit": FIX.commits["base"] + "\n"}, "invalid_arguments", "commit"),
            ({"ledger": "/elsewhere"}, "invalid_arguments", "unknown property"),
            ({"path": "../findings"}, "invalid_arguments", "unknown property"),
            ({"cursor": "not-a-cursor"}, "invalid_input", "cursor"),
            ({"current_only": "false"}, "invalid_arguments", "current_only"),
        ]
        for arguments, code, text in cases:
            with self.subTest(arguments=arguments):
                envelope = use(self.server, "find_findings", arguments)
                self.assertEqual((envelope["outcome"], envelope["error"]["code"]), ("ERROR", code))
                self.assertIn(text, envelope["error"]["message"])
                self.assertIsNone(envelope["data"])
        missing = use(self.server, "find_findings", {"commit": "0" * 40})
        self.assertEqual((missing["outcome"], missing["error"]["code"]), ("NOT_FOUND", "not_found"))

    def test_citations_are_exact_commit_pinned_spans(self) -> None:
        found = use(self.server, "find_findings", {"query": "click", "current_only": False})
        [click] = found["data"]["findings"]
        [citation] = click["citations"]
        base = FIX.commits["base"]
        self.assertEqual(citation, {
            "repo": None, "commit": base, "path": "src/weights.rs", "start_line": 4,
            "end_line": 4, "span_sha256": line_hash(FIX.repo.git_dir, base, "src/weights.rs", 4, 4),
            "blob_oid": citation["blob_oid"], "anchor": "CLICK_WEIGHT", "url": None,
            "content_trust": "UNTRUSTED_SOURCE_DATA", "resolution": None,
        })
        self.assertEqual(click["citations_total"], 1)
        self.assertEqual(click["value_note"],
                         f"public default at commit {base}; not a production value")
        filters = use(self.server, "find_findings", {"component": "filters"})["data"]["findings"]
        self.assertIsNone(filters[0]["value_note"])
        self.assertNotIn(str(FIX.repo.root), json.dumps(found))

    def test_pages_and_cursors_are_bound_to_the_ledger_head(self) -> None:
        first = use(self.server, "find_findings", {"current_only": False, "limit": 4})
        self.assertEqual((first["outcome"], first["data"]["total"], first["data"]["returned"]),
                         ("INCOMPLETE", 6, 4))
        self.assertIsNotNone(first["next_cursor"])
        second = use(self.server, "find_findings", {"current_only": False, "limit": 4,
                                                    "cursor": first["next_cursor"]})
        self.assertEqual((second["outcome"], second["data"]["offset"], ids(second)),
                         ("OK", 4, ["F-boost", "F-dep"]))
        other = use(self.server, "find_findings", {"current_only": False, "limit": 3,
                                                   "cursor": first["next_cursor"]})
        self.assertEqual(other["error"]["code"], "invalid_input")
        with tempfile.TemporaryDirectory(prefix="txray-mcp-ledger-") as tmp:
            copy = Path(tmp) / "ledger"
            shutil.copytree(FIX.ledger, copy)
            server = FIX.server(ledger=copy)
            page = use(server, "find_findings", {"current_only": False, "limit": 4})
            FindingsMemory(Ledger(copy), FIX.repo.store, FIX.repo.index).retract(
                "F-web", AUTHOR, "the ledger moves on")
            stale = use(server, "find_findings", {"current_only": False, "limit": 4,
                                                  "cursor": page["next_cursor"]})
            self.assertEqual(stale["error"]["code"], "invalid_input")
            self.assertIn("repeat the query without a cursor", stale["error"]["message"])


class GetFindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FIX.server()
        self.before = ledger_state(FIX.ledger)

    def tearDown(self) -> None:
        self.assertEqual(ledger_state(FIX.ledger), self.before, "a call changed the ledger")

    def test_checks_reviews_history_and_head(self) -> None:
        envelope = use(self.server, "get_finding", {"finding_id": "F-click"})
        self.assertEqual(envelope["outcome"], "OK")
        data = envelope["data"]
        events = [event for event in Ledger(FIX.ledger).events() if event.finding_id == "F-click"]
        self.assertEqual([(item["seq"], item["event"], item["type"]) for item in data["history"]],
                         [(event.seq, event.hash, event.type) for event in reversed(events)])
        self.assertEqual(data["history_total"], len(events))
        self.assertEqual(data["ledger"], {"head": Ledger(FIX.ledger).verify().last_hash,
                                          "events": len(Ledger(FIX.ledger).events())})
        checks = data["checks"]
        self.assertEqual([(c["mode"], c["target"], c["freshness"]) for c in checks], [
            ("reanchor", FIX.commits["literal"], "STALE"),
            ("reanchor", FIX.commits["shift"], "CURRENT"),
            ("integrity", None, "CURRENT"),
        ])
        self.assertEqual(checks[2]["verdict"], "INTACT")
        self.assertIn("changed", checks[0]["reasons"][0])
        by_hash = {event.hash: event.seq for event in events}
        for check in checks:
            self.assertEqual(check["seq"], by_hash[check["event"]])
        [review] = data["reviews"]
        self.assertEqual((review["actor"], review["status"], review["rationale"]),
                         ({"name": "reviewer-b", "role": "reviewer"}, "SUPPORTED",
                          "Read the cited span at base."))
        self.assertEqual(review["seq"], by_hash[review["event"]])
        self.assertEqual([(r["revision"], r["commit"]) for r in data["provenance"]],
                         [(1, None), (2, FIX.commits["shift"])])
        self.assertEqual([(s["start_line"], s["commit"]) for s in data["provenance"][1]["spans"]],
                         [(7, FIX.commits["shift"])])
        self.assertEqual([item["trigger"] for item in data["queue"]], ["changed_span"])
        self.assertFalse(data["finding"]["current"])
        self.assertIn("do not present it as current", envelope["warnings"][0])
        self.assertIn(FIX.commits["literal"], envelope["warnings"][0])

    def test_a_requested_commit_and_current_findings(self) -> None:
        envelope = use(self.server, "get_finding", {"finding_id": "F-click",
                                                    "commit": FIX.commits["base"]})
        self.assertEqual(envelope["data"]["evaluated_at"], FIX.commits["base"])
        self.assertTrue(envelope["data"]["finding"]["current"])
        self.assertEqual(envelope["warnings"], [])

    def test_dependencies_negative_search_and_sources(self) -> None:
        dep = use(self.server, "get_finding", {"finding_id": "F-dep"})["data"]
        self.assertEqual(dep["dependencies"][0], {"kind": "finding", "finding_id": "F-filters",
                                                  "span": None})
        span = dep["dependencies"][1]["span"]
        self.assertEqual((span["path"], span["start_line"], span["end_line"]),
                         ("src/weights.rs", 6, 8))
        boost = use(self.server, "get_finding", {"finding_id": "F-boost"})["data"]
        self.assertEqual(boost["negative"], {"commit": FIX.commits["base"], "query": "BOOST_WEIGHT",
                                             "literal": True, "path_glob": None})
        self.assertEqual(boost["finding"]["citations"], [])
        web = use(self.server, "get_finding", {"finding_id": "F-web"})["data"]
        self.assertEqual(web["web_sources"], [{"url": "https://example.org/fixture",
                                               "publisher": "Example", "published": "2026-01",
                                               "retrieved": "2026-09-30",
                                               "quote": "synthetic quote"}])
        filters = use(self.server, "get_finding", {"finding_id": "F-filters"})["data"]
        self.assertEqual(filters["limitations"], ["only the list, not its use"])

    def test_superseded_and_retracted_findings(self) -> None:
        fav = use(self.server, "get_finding", {"finding_id": "F-fav"})
        finding = fav["data"]["finding"]
        self.assertEqual((finding["workflow"], finding["superseded_by"]),
                         ("superseded", "F-reply"))
        self.assertIn("superseded by F-reply", fav["warnings"][0])
        reply = use(self.server, "get_finding", {"finding_id": "F-reply"})["data"]
        self.assertEqual(reply["supersedes"], ["F-fav"])
        notes = use(self.server, "get_finding", {"finding_id": "F-notes"})["data"]
        self.assertEqual(notes["retraction"]["reason"], "not about the algorithm")

    def test_unknown_and_malformed_ids(self) -> None:
        missing = use(self.server, "get_finding", {"finding_id": "F-nothing"})
        self.assertEqual((missing["outcome"], missing["error"]["code"]), ("NOT_FOUND", "not_found"))
        for arguments in ({"finding_id": "../ledger"}, {"finding_id": "a b"}, {},
                          {"finding_id": "F-click", "extra": 1}, {"finding_id": "x" * 129},
                          {"finding_id": "F-click\n"}):
            with self.subTest(arguments=arguments):
                envelope = use(self.server, "get_finding", arguments)
                self.assertEqual(envelope["error"]["code"], "invalid_arguments")


class VerifyClaimTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FIX.server()
        self.before = ledger_state(FIX.ledger)

    def tearDown(self) -> None:
        self.assertEqual(ledger_state(FIX.ledger), self.before, "a call changed the ledger")

    def test_a_changed_span_is_stale_at_the_target_and_never_assessed(self) -> None:
        literal = FIX.commits["literal"]
        envelope = use(self.server, "verify_claim", {"finding_id": "F-click",
                                                     "target_commit": literal})
        self.assertEqual(envelope["outcome"], "OK")
        data = envelope["data"]
        self.assertEqual(data["finding"], {"finding_id": "F-click", "status": "SUPPORTED",
                                           "status_basis": "reviewed", "reviewed": True,
                                           "workflow": "reviewed"})
        [check] = data["citations"]
        self.assertEqual((check["verdict"], check["anchor_verdict"], check["freshness"],
                          check["baseline"]), ("INTACT", "FOUND", "CURRENT", False))
        self.assertEqual(check["observed_span_sha256"], check["span"]["span_sha256"])
        [moved] = data["relocations"]
        self.assertEqual((moved["outcome"], moved["freshness"]), ("changed", "STALE"))
        self.assertEqual((moved["from"]["commit"], moved["from"]["start_line"]),
                         (FIX.commits["shift"], 7))
        self.assertEqual((moved["proposed"]["start_line"], moved["proposed"]["anchor_verdict"]),
                         (4, "FOUND"))
        self.assertIsNone(moved["current"])
        self.assertEqual((data["at_cited"]["freshness"], data["at_cited"]["verdict"],
                          data["at_cited"]["matches_recorded"]), ("CURRENT", "INTACT", True))
        self.assertEqual((data["at_target"]["commit"], data["at_target"]["freshness"],
                          data["at_target"]["matches_recorded"]), (literal, "STALE", True))
        self.assertEqual(data["semantic_verdict"], "NOT_ASSESSED")
        self.assertFalse(data["ledger_written"])
        self.assertIn("not semantic truth", data["integrity_note"])
        self.assertEqual(data["ledger"]["head"], Ledger(FIX.ledger).verify().last_hash)

    def test_given_citations_relocate_and_report_integrity(self) -> None:
        base, shift = FIX.commits["base"], FIX.commits["shift"]
        good = line_hash(FIX.repo.git_dir, base, "src/weights.rs", 3, 3)
        cite = {"commit": base, "path": "src/weights.rs", "start_line": 3, "end_line": 3,
                "anchor": "REPLY_WEIGHT"}
        envelope = use(self.server, "verify_claim", {
            "citations": [dict(cite, span_sha256=good), cite,
                          dict(cite, span_sha256="0" * 64),
                          dict(cite, commit="1" * 40),
                          dict(cite, path="src/absent.rs"),
                          dict(cite, start_line=90, end_line=91),
                          dict(cite, anchor="CLICK_WEIGHT")],
            "target_commit": shift})
        data = envelope["data"]
        self.assertEqual((data["mode"], data["finding"], data["ledger"]), ("citations", None, None))
        rows = [(c["verdict"], c["reason"], c["anchor_verdict"], c["freshness"], c["baseline"])
                for c in data["citations"]]
        self.assertEqual(rows, [
            ("INTACT", None, "FOUND", "CURRENT", False),
            ("INTACT", None, "FOUND", "CURRENT", True),
            ("CHANGED", "span_sha256_differs", "FOUND", "STALE", False),
            ("MISSING", "commit_not_pinned", None, "UNVERIFIABLE", False),
            ("MISSING", "path_missing", None, "UNVERIFIABLE", False),
            ("MISSING", "invalid_range", None, "UNVERIFIABLE", False),
            ("INTACT", None, "MISSING", "UNVERIFIABLE", True),
        ])
        first = data["relocations"][0]
        self.assertEqual((first["outcome"], first["freshness"]), ("relocated", "CURRENT"))
        self.assertEqual((first["current"]["commit"], first["current"]["start_line"],
                          first["current"]["span_sha256"]), (shift, 6, good))
        self.assertEqual(data["at_cited"]["verdict"], "MISSING")
        self.assertEqual(data["at_cited"]["freshness"], "STALE")
        self.assertIsNone(data["at_cited"]["matches_recorded"])
        self.assertEqual(data["semantic_verdict"], "NOT_ASSESSED")

    def test_dependencies_and_negative_searches_are_rechecked(self) -> None:
        shift, negative = FIX.commits["shift"], FIX.commits["negative"]
        dep = use(self.server, "verify_claim", {"finding_id": "F-dep", "target_commit": shift})
        data = dep["data"]
        self.assertEqual([(d["kind"], d["at_cited"]["freshness"], d["at_target"]["freshness"])
                          for d in data["dependencies"]],
                         [("finding", "CURRENT", "CURRENT"), ("span", "CURRENT", "CURRENT")])
        self.assertEqual((data["at_target"]["freshness"], data["at_target"]["matches_recorded"]),
                         ("CURRENT", True))
        boost = use(self.server, "verify_claim", {"finding_id": "F-boost",
                                                  "target_commit": negative})["data"]
        self.assertEqual(boost["citations"], [])
        self.assertEqual((boost["negative"]["at_cited"]["freshness"],
                          boost["negative"]["at_target"]["freshness"],
                          boost["negative"]["at_target"]["total"]), ("CURRENT", "STALE", 1))
        self.assertEqual(boost["at_target"]["freshness"], "STALE")
        self.assertIsNone(boost["at_target"]["recorded"])
        self.assertIsNone(boost["at_target"]["matches_recorded"])
        web = use(self.server, "verify_claim", {"finding_id": "F-web"})
        self.assertEqual(web["data"]["at_cited"]["freshness"], "NOT_CHECKED")
        self.assertIn("nothing to verify", web["warnings"][0])

    def test_citations_can_be_checked_without_a_ledger(self) -> None:
        server = Server(StoreGuard(FIX.store_dir), log=io.StringIO())
        envelope = use(server, "verify_claim", {"citations": [{
            "commit": FIX.commits["base"], "path": "src/filters.py", "start_line": 2,
            "end_line": 2, "anchor": "DropDuplicates"}]})
        self.assertEqual(envelope["data"]["at_cited"]["freshness"], "CURRENT")
        by_id = use(server, "verify_claim", {"finding_id": "F-click"})
        self.assertEqual(by_id["outcome"], "DENIED")

    def test_input_validation(self) -> None:
        cite = {"commit": FIX.commits["base"], "path": "src/weights.rs", "start_line": 4,
                "end_line": 4, "anchor": "CLICK_WEIGHT"}
        cases = [
            ({}, "invalid_input", "exactly one"),
            ({"finding_id": "F-click", "citations": [cite]}, "invalid_input", "exactly one"),
            ({"citations": [dict(cite, path="../../etc/passwd")]}, "invalid_input", "normalised"),
            ({"citations": [dict(cite, path="/etc/passwd")]}, "invalid_input", "absolute"),
            ({"citations": [dict(cite, start_line=5)]}, "invalid_input", "before start_line"),
            ({"citations": [cite] * 9}, "invalid_arguments", "at most 8"),
            ({"citations": []}, "invalid_arguments", "at least 1"),
            ({"citations": [dict(cite, commit=FIX.commits["base"][:12])]}, "invalid_arguments",
             "pattern"),
            ({"citations": [dict(cite, extra=True)]}, "invalid_arguments", "unknown property"),
            ({"citations": [dict(cite, anchor="")]}, "invalid_arguments", "at least 1"),
            ({"citations": [dict(cite, span_sha256="A" * 64)]}, "invalid_arguments", "pattern"),
            ({"finding_id": "F-click", "target_commit": "HEAD"}, "invalid_arguments", "pattern"),
        ]
        for arguments, code, text in cases:
            with self.subTest(arguments=arguments):
                envelope = use(self.server, "verify_claim", arguments)
                self.assertEqual(envelope["error"]["code"], code)
                self.assertIn(text, envelope["error"]["message"])
        missing = use(self.server, "verify_claim", {"finding_id": "F-click",
                                                    "target_commit": "2" * 40})
        self.assertEqual(missing["outcome"], "NOT_FOUND")
        unknown = use(self.server, "verify_claim", {"finding_id": "F-unknown"})
        self.assertEqual(unknown["outcome"], "NOT_FOUND")


class LedgerAccessTest(unittest.TestCase):
    def calls(self) -> list[tuple[str, dict[str, Any]]]:
        base, shift, literal = (FIX.commits[name] for name in ("base", "shift", "literal"))
        cite = {"commit": base, "path": "src/weights.rs", "start_line": 4, "end_line": 4,
                "anchor": "CLICK_WEIGHT"}
        return [
            ("find_findings", {}),
            ("find_findings", {"current_only": False, "limit": 2}),
            ("find_findings", {"commit": literal, "current_only": False}),
            ("find_findings", {"freshness": ["STALE"]}),
            ("get_finding", {"finding_id": "F-click"}),
            ("get_finding", {"finding_id": "F-dep", "commit": base}),
            ("get_finding", {"finding_id": "F-missing"}),
            ("verify_claim", {"finding_id": "F-click", "target_commit": literal}),
            ("verify_claim", {"finding_id": "F-dep", "target_commit": shift}),
            ("verify_claim", {"finding_id": "F-boost", "target_commit": FIX.commits["negative"]}),
            ("verify_claim", {"citations": [cite], "target_commit": FIX.commits["delete"]}),
            ("verify_claim", {"citations": [cite]}),
        ]

    def test_no_call_writes_to_the_ledger(self) -> None:
        with tempfile.TemporaryDirectory(prefix="txray-mcp-ledger-") as tmp:
            unlocked = Path(tmp) / "ledger"
            shutil.copytree(FIX.ledger, unlocked)
            (unlocked / ".lock").unlink()
            for ledger in (FIX.ledger, unlocked):
                server = FIX.server(ledger=ledger)
                for name, arguments in self.calls():
                    with self.subTest(ledger=ledger.name, tool=name, arguments=arguments):
                        before = ledger_state(ledger)
                        head = (ledger / "HEAD").read_bytes()
                        use(server, name, arguments)
                        self.assertEqual(ledger_state(ledger), before)
                        self.assertEqual((ledger / "HEAD").read_bytes(), head)
            self.assertFalse((unlocked / ".lock").exists())
            self.assertEqual(sorted(p.name for p in unlocked.iterdir()), ["HEAD", "events.jsonl"])

    def test_without_a_ledger(self) -> None:
        server = Server(StoreGuard(FIX.store_dir), log=io.StringIO())
        envelope = use(server, "find_findings", {})
        self.assertEqual((envelope["outcome"], envelope["error"]["code"]), ("DENIED", "refused"))
        self.assertIn("no findings ledger is configured", envelope["error"]["message"])
        problem = Server(StoreGuard(FIX.store_dir, ledger_problem="the default lies in a tree"),
                         log=io.StringIO())
        self.assertEqual(use(problem, "get_finding", {"finding_id": "F-click"})["error"]["message"],
                         "the default lies in a tree")
        with tempfile.TemporaryDirectory(prefix="txray-mcp-ledger-") as tmp:
            absent = Path(tmp) / "not-yet"
            envelope = use(FIX.server(ledger=absent), "find_findings", {})
            self.assertEqual(envelope["outcome"], "NOT_FOUND")
            self.assertIn("no findings ledger exists at <ledger>", envelope["error"]["message"])
            self.assertNotIn(tmp, envelope["error"]["message"])
            self.assertFalse(absent.exists())

    def test_a_damaged_ledger_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="txray-mcp-ledger-") as tmp:
            copy = Path(tmp) / "ledger"
            shutil.copytree(FIX.ledger, copy)
            events = copy / "events.jsonl"
            events.write_bytes(events.read_bytes().replace(b"CLICK_WEIGHT", b"CLICK_WEIGHX", 1))
            before = ledger_state(copy)
            for name, arguments in (("find_findings", {}), ("get_finding", {"finding_id": "F-web"}),
                                    ("verify_claim", {"finding_id": "F-web"})):
                with self.subTest(tool=name):
                    envelope = use(FIX.server(ledger=copy), name, arguments)
                    self.assertEqual((envelope["outcome"], envelope["error"]["code"]),
                                     ("ERROR", "integrity_error"))
                    self.assertIn("<ledger>", envelope["error"]["message"])
                    self.assertNotIn(tmp, envelope["error"]["message"])
            self.assertEqual(ledger_state(copy), before)

    def test_ledger_files_must_stay_inside_the_ledger(self) -> None:
        with tempfile.TemporaryDirectory(prefix="txray-mcp-ledger-") as tmp:
            copy = Path(tmp) / "ledger"
            shutil.copytree(FIX.ledger, copy)
            outside = Path(tmp) / "elsewhere.jsonl"
            (copy / "events.jsonl").rename(outside)
            os.symlink(outside, copy / "events.jsonl")
            envelope = use(FIX.server(ledger=copy), "find_findings", {})
            self.assertEqual(envelope["outcome"], "DENIED")
            self.assertIn("resolves outside the findings ledger", envelope["error"]["message"])
            (copy / "events.jsonl").unlink()
            outside.rename(copy / "events.jsonl")
            (copy / "HEAD").unlink()
            os.mkfifo(copy / "HEAD")
            envelope = use(FIX.server(ledger=copy), "find_findings", {})
            self.assertEqual(envelope["outcome"], "DENIED")
            self.assertIn("not a regular file", envelope["error"]["message"])

    def test_an_analytics_dataset_in_or_above_the_ledger_is_refused(self) -> None:
        marker = json.dumps({"format": "timelinexray/analytics-dataset/v1"})
        with tempfile.TemporaryDirectory(prefix="txray-mcp-ledger-") as tmp:
            parent = Path(tmp) / "research"
            copy = parent / "ledger"
            shutil.copytree(FIX.ledger, copy)
            server = FIX.server(ledger=copy)
            self.assertEqual(use(server, "find_findings", {})["outcome"], "OK")
            (copy / "dataset.json").write_text(marker, "utf-8")
            with self.assertRaisesRegex(Refused,
                                        "findings ledger: an analytics dataset lies inside"):
                StoreGuard(FIX.store_dir, copy)
            later = use(server, "get_finding", {"finding_id": "F-click"})
            self.assertEqual((later["outcome"], later["data"]), ("DENIED", None))
            (copy / "dataset.json").rename(parent / "dataset.json")
            with self.assertRaisesRegex(Refused, "lies above"):
                StoreGuard(FIX.store_dir, copy)

    def test_output_limits(self) -> None:
        arguments = {"current_only": False, "limit": 20}
        full = FIX.server().handle_line(json.dumps(call("find_findings", arguments)).encode())
        assert full is not None
        server = FIX.server(max_response_bytes=len(full))
        response = exchange(server, call("find_findings", arguments))
        envelope = response["result"]["structuredContent"]
        S.validate(envelope, server.registry.get("find_findings").strict_output_schema)
        S.validate(envelope, server.registry.get("find_findings").output_schema)
        self.assertEqual(envelope["outcome"], "INCOMPLETE")
        self.assertTrue(any(w.startswith("TRUNCATED: the result exceeded")
                            for w in envelope["warnings"]))
        kept = envelope["data"]["returned"]
        self.assertLess(kept, 6)
        follow = use(server, "find_findings", dict(arguments, cursor=envelope["next_cursor"]))
        self.assertEqual(follow["data"]["offset"], kept)
        detail = FIX.server().handle_line(json.dumps(call("get_finding",
                                                          {"finding_id": "F-click"})).encode())
        assert detail is not None
        short = FIX.server(max_response_bytes=len(detail))
        envelope = exchange(short, call("get_finding", {"finding_id": "F-click"}))[
            "result"]["structuredContent"]
        S.validate(envelope, short.registry.get("get_finding").strict_output_schema)
        S.validate(envelope, short.registry.get("get_finding").output_schema)
        self.assertEqual(envelope["outcome"], "INCOMPLETE")
        self.assertLess(len(envelope["data"]["history"]), envelope["data"]["history_total"])
        tiny = FIX.server(max_response_bytes=3000)
        for name, args in (("get_finding", {"finding_id": "F-click"}),
                           ("verify_claim", {"finding_id": "F-click"})):
            with self.subTest(tool=name):
                envelope = exchange(tiny, call(name, args))["result"]["structuredContent"]
                self.assertEqual(envelope["error"]["code"], "output_too_large")

    def test_tools_list_fits_one_response_line(self) -> None:
        server = FIX.server()
        modern = server.handle_line(json.dumps(request("tools/list")).encode())
        server.handle_line(json.dumps(initialize(LEGACY)).encode())
        legacy = server.handle_line(json.dumps(request("tools/list", id=2, modern=False)).encode())
        for line in (modern, legacy):
            assert line is not None
            self.assertLessEqual(len(line), MAX_RESPONSE_BYTES)
            # real headroom: ~61.8 KB before Milestone 6, 39.9 KB with thirteen tools in
            # 0.11.0, about 33.1 KB after the lean envelope and tighter descriptions
            self.assertLess(len(line), 34_000)
            listed = json.loads(line)["result"]["tools"]
            self.assertEqual([tool["name"] for tool in listed], EXPECTED_TOOLS)

    def test_the_findings_tools_are_read_only_public_code_tools(self) -> None:
        for name in FINDINGS_TOOLS:
            definition = REGISTRY.get(name).definition()
            self.assertEqual(definition["annotations"]["readOnlyHint"], True)
            self.assertEqual(definition["annotations"]["destructiveHint"], False)
            self.assertNotIn("ledger", definition["inputSchema"]["properties"])
            self.assertNotIn("path", definition["inputSchema"]["properties"])


class LedgerLocationTest(unittest.TestCase):
    def test_resolution_order(self) -> None:
        with tempfile.TemporaryDirectory(prefix="txray-mcp-cli-") as tmp:
            store = Path(tmp) / "store"
            store.mkdir()
            explicit = Path(tmp) / "mine"
            explicit.mkdir()
            with mock.patch.dict(os.environ, {"TXRAY_FINDINGS": str(Path(tmp) / "env")}):
                self.assertEqual(_ledger_location(str(explicit), str(store)),
                                 (str(explicit), None))
                self.assertEqual(_ledger_location(None, str(store)),
                                 (str(Path(tmp) / "env"), None))
            with mock.patch.dict(os.environ, {"TXRAY_FINDINGS": ""}):
                self.assertEqual(_ledger_location(None, str(store)),
                                 (str(store / "findings"), None))
                (Path(tmp) / ".git").mkdir()
                ledger, problem = _ledger_location(None, str(store))
                self.assertIsNone(ledger)
                self.assertIn("inside a git working tree", problem or "")
                self.assertNotIn(tmp, problem or "")

    def test_serve_refuses_a_missing_explicit_ledger(self) -> None:
        with tempfile.TemporaryDirectory(prefix="txray-mcp-cli-") as tmp:
            code, out, err = run_cli(["mcp", "serve", "--store", str(FIX.store_dir),
                                      "--ledger", str(Path(tmp) / "absent")])
            self.assertEqual((code, out), (1, b""))
            self.assertIn(b"does not exist or is not a directory", err)
        code, out, _ = run_cli(["mcp", "serve", "--help"])
        self.assertEqual(code, 0)
        self.assertIn(b"--ledger DIR", out)


class FindingsStdioTest(unittest.TestCase):
    def tearDown(self) -> None:
        client = getattr(self, "client", None)
        if client is not None and client.proc.poll() is None:
            client.proc.kill()
            client.proc.wait()

    def envelope(self, response: dict[str, Any], name: str) -> dict[str, Any]:
        result = response["result"]
        envelope = result["structuredContent"]
        S.validate(envelope, REGISTRY.get(name).strict_output_schema)
        S.validate(envelope, REGISTRY.get(name).output_schema)
        self.assertEqual(json.loads(result["content"][0]["text"]), envelope)
        return envelope

    def test_three_tools_round_trip(self) -> None:
        before = ledger_state(FIX.ledger)
        self.client = client = StdioClient(FIX.store_dir, "--ledger", str(FIX.ledger))
        listed = client.ask(request("tools/list", id=1))["result"]["tools"]
        self.assertEqual([tool["name"] for tool in listed][-4:-1], FINDINGS_TOOLS)
        found = self.envelope(client.ask(call("find_findings", {}, id=2)), "find_findings")
        self.assertEqual(ids(found), ["F-reply", "F-filters", "F-web", "F-boost", "F-dep"])
        detail = self.envelope(client.ask(call("get_finding", {"finding_id": "F-click"}, id=3)),
                               "get_finding")
        self.assertEqual(detail["data"]["finding"]["freshness"]["value"], "STALE")
        checked = self.envelope(client.ask(call("verify_claim", {
            "finding_id": "F-click", "target_commit": FIX.commits["literal"]}, id=4)),
            "verify_claim")
        self.assertEqual(checked["data"]["at_target"]["freshness"], "STALE")
        client.ask(initialize(LEGACY, id=5))
        legacy = client.ask(call("find_findings", {"current_only": False, "limit": 1}, id=6,
                                 modern=False))
        self.assertNotIn("resultType", legacy["result"])
        self.assertEqual(self.envelope(legacy, "find_findings")["outcome"], "INCOMPLETE")
        code, stderr = client.close()
        self.assertEqual(code, 0)
        self.assertIn(b"findings ledger: <ledger> (read-only)", stderr)
        self.assertNotIn(str(FIX.repo.root).encode(), stderr)
        self.assertEqual(ledger_state(FIX.ledger), before)

    def test_the_ledger_from_the_environment(self) -> None:
        self.client = client = StdioClient(FIX.store_dir, env={"TXRAY_FINDINGS": str(FIX.ledger)})
        found = self.envelope(client.ask(call("find_findings", {"statuses": ["NOT_FOUND"]})),
                              "find_findings")
        self.assertEqual(ids(found), ["F-boost"])
        self.assertEqual(client.close()[0], 0)


GOLDEN_SPAN = "b01734cae3e1124eb6a960d8e84b2a8d8d7df35bc2c009bb6f9880e29e16b16a"
OLD, NEW = ("4c5cfe8f07f1c76d4f04277e803f20e6039f5191",
            "a707cc27ba36d3fa79450c9cffcc48a82d080b02")
PARAM_PATH = "home-mixer/params/param.rs"


@unittest.skipIf(UPSTREAM is None, "no local x-algorithm clone "
                 "(set TXRAY_TEST_UPSTREAM to a local clone)")
class UpstreamFindingsStdioTest(unittest.TestCase):
    """A real finding: the ClickWeight public default 0.4 at 4c5cfe8, STALE at a707cc2."""

    @classmethod
    def setUpClass(cls) -> None:
        assert UPSTREAM is not None
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-mcp-findings-upstream-")
        root = Path(cls._tmp.name)
        cls.store, cls.ledger = root / "store", root / "ledger"
        url = file_url(UPSTREAM.parent if UPSTREAM.name == ".git" else UPSTREAM)
        env = child_env({"TXRAY_ALLOW_FILE_URLS": url})
        common = ["--store", str(cls.store)]
        where = [*common, "--ledger", str(cls.ledger)]
        for argv in (
            ["pin", OLD[:7], "--upstream", url, *common],
            ["pin", NEW[:7], "--upstream", url, *common],
            ["findings", "add", *where, "--actor", "agent-a", "--id", "U-click-weight",
             "--title", "ClickWeight public default", "--component", "home-mixer",
             "--claim", "At 4c5cfe8 the public default of ClickWeight is 0.4.",
             "--evidence-class", "PARAM_DEFAULT", "--status", "SUPPORTED",
             "--cite", OLD[:7], PARAM_PATH, "322", "ClickWeight"],
            ["findings", "verify", *where],
            ["findings", "reanchor", NEW[:7], *where],
        ):
            proc = subprocess.run([sys.executable, "-m", "timelinexray", *argv],
                                  capture_output=True, env=env, timeout=600)
            if proc.returncode != 0:
                raise AssertionError(proc.stderr.decode("utf-8", "replace"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_a_reanchored_real_finding_over_stdio(self) -> None:
        assert UPSTREAM is not None
        before = ledger_state(self.ledger)
        client = StdioClient(self.store, "--ledger", str(self.ledger))
        try:
            def use_stdio(name: str, arguments: dict[str, Any], id: int) -> dict[str, Any]:
                result = client.ask(call(name, arguments, id=id))["result"]
                envelope = result["structuredContent"]
                S.validate(envelope, REGISTRY.get(name).strict_output_schema)
                S.validate(envelope, REGISTRY.get(name).output_schema)
                self.assertIn(envelope["outcome"], ("OK", "INCOMPLETE"), envelope["error"])
                return envelope

            current = use_stdio("find_findings", {}, 1)
            self.assertEqual((current["data"]["total"], current["data"]["not_current"]),
                             (0, {"STALE": 1}))
            shown = use_stdio("find_findings", {"current_only": False}, 2)
            [item] = shown["data"]["findings"]
            self.assertEqual((item["freshness"]["value"], item["freshness"]["commit"],
                              item["current"]), ("STALE", NEW, False))
            [citation] = item["citations"]
            self.assertEqual((citation["commit"], citation["path"], citation["start_line"],
                              citation["end_line"], citation["span_sha256"]),
                             (OLD, PARAM_PATH, 322, 322, GOLDEN_SPAN))
            self.assertEqual(citation["span_sha256"],
                             line_hash(UPSTREAM, OLD, PARAM_PATH, 322, 322))
            self.assertIsNone(citation["url"])  # a file:// mirror: no permalink, no location
            self.assertEqual(item["value_note"],
                             f"public default at commit {OLD}; not a production value")
            historical = use_stdio("find_findings", {"commit": OLD}, 3)
            self.assertEqual([f["freshness"]["value"] for f in historical["data"]["findings"]],
                             ["CURRENT"])
            detail = use_stdio("get_finding", {"finding_id": "U-click-weight"}, 4)
            checks = detail["data"]["checks"]
            self.assertEqual([(c["mode"], c["target"], c["freshness"]) for c in checks],
                             [("reanchor", NEW, "STALE"), ("integrity", None, "CURRENT")])
            head = json.loads((self.ledger / "HEAD").read_text("utf-8"))
            self.assertEqual(detail["data"]["ledger"], {"head": head["hash"],
                                                        "events": head["seq"]})
            checked = use_stdio("verify_claim", {"finding_id": "U-click-weight",
                                                 "target_commit": NEW}, 5)
            data = checked["data"]
            self.assertEqual((data["at_cited"]["verdict"], data["at_cited"]["freshness"]),
                             ("INTACT", "CURRENT"))
            [moved] = data["relocations"]
            self.assertEqual((moved["outcome"], moved["freshness"], moved["proposed"]["start_line"],
                              moved["proposed"]["end_line"]), ("changed", "STALE", 329, 329))
            target = data["at_target"]
            self.assertEqual((target["freshness"], target["matches_recorded"]),
                             ("STALE", True))
            self.assertEqual(data["semantic_verdict"], "NOT_ASSESSED")
        finally:
            code, _ = client.close()
        self.assertEqual(code, 0)
        self.assertEqual(ledger_state(self.ledger), before)


if __name__ == "__main__":
    unittest.main()
