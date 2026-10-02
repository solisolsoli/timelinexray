"""Freshness relative to the newest pin (FA-001) and evidence without code spans (FA-003).

A small history whose commits have distinct committer dates (the shared fixture gives every
commit the same date, so no pin there is newer than another): ``base`` (January, the cited
``CLICK_WEIGHT`` line), ``changed`` (February, the literal changes), ``same`` (March, an
unrelated file is added and the cited blob is identical to ``base``) and ``later`` (April,
another unrelated file). Every consumer - :mod:`timelinexray.findings.freshness`, the MCP
findings tools, ``txray findings list/show/reanchor`` and the Context Layer export - must
agree that a finding checked only at an older pin is not current until it is re-anchored
on the newest pin, and that a finding with external evidence only is current with the
reading ``NOT_APPLICABLE`` (nothing to re-verify), shown with its retrieval date and the
recheck date its recorded text names.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from timelinexray.export.notes import build_notes
from timelinexray.findings import FindingsMemory, Ledger
from timelinexray.findings.freshness import (
    NEWER_PIN_UNCHECKED,
    NOT_APPLICABLE,
    PinIndex,
    evaluate,
    is_current,
    recheck_after,
)
from timelinexray.mcp import schema as S
from timelinexray.mcp.guard import StoreGuard
from timelinexray.mcp.server import Server
from timelinexray.netguard import Allowlist
from timelinexray.snapshot import SnapshotStore
from tests.findings_support import AUTHOR, BASE
from tests.index_support import add_commit
from tests.mcp_support import call
from tests.support import build_fixture_repo, file_url, run_cli

FEBRUARY = "2026-02-01T00:00:00+0000"
MARCH = "2026-03-01T00:00:00+0000"
APRIL = "2026-04-01T00:00:00+0000"


class TimedRepo:
    """Four commits in a known time order, each pinned on demand into a test's own store."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-freshness-")
        self.root = Path(self._tmp.name)
        self.git_dir, base = build_fixture_repo(self.root / "upstream", BASE, frozenset())
        changed = add_commit(self.git_dir, {
            **BASE, "src/weights.rs": BASE["src/weights.rs"].replace(b"0.4", b"0.3")},
            base, committer_date=FEBRUARY)
        same = add_commit(self.git_dir, {**BASE, "docs/extra.md": b"# Extra\n"}, changed,
                          committer_date=MARCH)
        later = add_commit(self.git_dir, {**BASE, "docs/extra.md": b"# Extra\n",
                                          "docs/more.md": b"# More\n"}, same,
                           committer_date=APRIL)
        self.commits = {"base": base, "changed": changed, "same": same, "later": later}
        self.url = file_url(self.git_dir)
        self.allowlist = Allowlist([self.url])

    def cleanup(self) -> None:
        self._tmp.cleanup()


REPO: TimedRepo


def setUpModule() -> None:
    global REPO
    REPO = TimedRepo()


def tearDownModule() -> None:
    REPO.cleanup()


CLICK = {
    "finding_id": "F-click", "title": "Synthetic click weight default", "component": "src",
    "claim": "The synthetic fixture sets CLICK_WEIGHT to 0.4 as a public default.",
    "evidence_class": "PARAM_DEFAULT", "status": "SUPPORTED",
}
EXTERNAL = {
    "finding_id": "F-ext", "title": "Official terms restrict scraping", "component": "external",
    "claim": "The retrieved terms prohibit scraping without consent.",
    "evidence_class": "OFFICIAL", "status": "EXTERNAL_RECHECK", "scope": "external",
    "web_sources": [{"url": "https://example.org/terms", "publisher": "Example",
                     "published": None, "retrieved": "2026-09-30", "quote": ""}],
    "limitations": ["The terms say they are effective 2026-10-09; recheck after 2026-10-09."],
}
INFERENCE = {
    "finding_id": "F-inf", "title": "An inference without spans", "component": "src",
    "claim": "The ranking probably prefers early engagement.", "evidence_class": "INFERENCE",
    "status": "PARTIAL", "limitations": ["No code span supports this directly."],
    "web_sources": [{"url": "https://example.org/blog", "publisher": "Example",
                     "published": "2026-05", "retrieved": "2026-09-01", "quote": ""}],
}


def ids(envelope: dict[str, Any]) -> list[str]:
    return [item["finding_id"] for item in envelope["data"]["findings"]]


class FreshnessCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-freshness-case-")
        self.tmp = Path(self._tmp.name)
        self.store = SnapshotStore(self.tmp / "store")
        self.ledger_dir = self.tmp / "ledger"
        self.memory = FindingsMemory(Ledger(self.ledger_dir), self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def pin(self, *names: str) -> None:
        for name in names:
            self.store.pin(REPO.commits[name], REPO.url, allowlist=REPO.allowlist)

    def add_click(self) -> None:
        self.memory.add({**CLICK, "citations": [{
            "commit": REPO.commits["base"], "path": "src/weights.rs", "lines": "4",
            "anchor": "CLICK_WEIGHT"}]}, AUTHOR)

    def reading(self, finding_id: str, commit: str | None = None):
        state = self.memory.get(finding_id)
        fresh = evaluate(state, PinIndex.of(self.store), commit)
        return state, fresh

    def use(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        server = Server(StoreGuard(self.store.root, self.ledger_dir), log=io.StringIO())
        line = server.handle_line(json.dumps(call(name, arguments)).encode("utf-8"))
        assert line is not None
        envelope = json.loads(line)["result"]["structuredContent"]
        S.validate(envelope, server.registry.get(name).strict_output_schema)
        return envelope

    def cli(self, *argv: str, expect: int = 0) -> bytes:
        """stdout of ``txray findings ...`` (plus stderr when a failure is expected)."""
        code, out, err = run_cli(["findings", *argv, "--store", str(self.store.root),
                                  "--ledger", str(self.ledger_dir)])
        self.assertEqual(code, expect, (argv, out[-500:], err))
        return out if expect == 0 else out + err

    def cli_json(self, *argv: str, expect: int = 0) -> dict[str, Any]:
        return json.loads(self.cli(*argv, "--json", expect=expect))


class NewestPinTest(FreshnessCase):
    def test_a_newer_unchecked_pin_makes_a_finding_not_current_everywhere(self) -> None:
        """A finding verified only at ``base`` while ``changed`` is pinned too (where the
        cited literal differs) is not current in the module, the MCP tools, the CLI and
        the export, each naming the unchecked pin; the historical reading at ``base`` stays
        CURRENT and names its commit."""
        base, changed = REPO.commits["base"], REPO.commits["changed"]
        self.pin("base", "changed")
        self.add_click()
        self.memory.verify()

        state, fresh = self.reading("F-click")
        self.assertEqual((fresh.value, fresh.commit, fresh.check), ("CURRENT", base, "integrity"))
        self.assertEqual((fresh.newer_pins, fresh.newest_pin), ([changed], changed))
        self.assertFalse(fresh.current)
        self.assertFalse(is_current(state, fresh))
        self.assertEqual(fresh.reason, NEWER_PIN_UNCHECKED)
        _, at_base = self.reading("F-click", base)
        self.assertEqual((at_base.value, at_base.commit, at_base.newer_pins, at_base.current),
                         ("CURRENT", base, [], True))

        found = self.use("find_findings", {})
        self.assertEqual((ids(found), found["data"]["not_current"], found["data"]["newest_pin"]),
                         ([], {NEWER_PIN_UNCHECKED: 1}, changed))
        self.assertTrue(any("reanchor --latest" in w for w in found["warnings"]), found["warnings"])
        shown = self.use("find_findings", {"current_only": False})
        [item] = shown["data"]["findings"]
        self.assertEqual((item["current"], item["freshness"]["value"],
                          item["freshness"]["commit"], item["freshness"]["newer_pins"]),
                         (False, "CURRENT", base, [changed]))
        detail = self.use("get_finding", {"finding_id": "F-click"})
        self.assertFalse(detail["data"]["finding"]["current"])
        self.assertIn(changed, detail["warnings"][0])
        self.assertIn("do not present it as current", detail["warnings"][0])
        historical = self.use("find_findings", {"commit": base})
        self.assertEqual(ids(historical), ["F-click"])
        self.assertEqual((historical["data"]["findings"][0]["current"],
                          historical["data"]["findings"][0]["freshness"]["newer_pins"]),
                         (True, []))

        notes = build_notes(self.memory.view(), pins=self.store.list_pins())
        summary = notes.summary()["findings"]
        self.assertEqual((summary["exported"], summary["left_out"], notes.newest_pin),
                         (0, {f"not_current_{NEWER_PIN_UNCHECKED}": 1}, changed))
        notes = build_notes(self.memory.view(), pins=self.store.list_pins(), include_stale=True)
        [path] = [p for p in notes.files if "F-click" in p]
        note = notes.files[path].decode("utf-8")
        self.assertIn(f"NOT CURRENT ({NEWER_PIN_UNCHECKED}: CURRENT at {base[:12]}", note)
        self.assertIn("current: false", note)
        self.assertIn("newer_pins_unchecked: 1", note)
        self.assertIn(f"A newer pinned commit (`{changed}`) was never checked.", note)

        listed = self.cli_json("list")
        self.assertEqual(listed["data"]["newest_pin"], changed)
        [entry] = listed["data"]["findings"]
        self.assertEqual((entry["current"], entry["reading"]["newer_pins"]), (False, [changed]))
        lines = self.cli("list").splitlines()
        self.assertIn(b"newest pin " + changed[:12].encode(), lines[0])
        self.assertIn(b"not current  ", lines[1])
        self.assertIn(b"CURRENT@" + base[:12].encode() + b"; 1 newer pin(s) unchecked", lines[2])
        shown_text = self.cli("show", "F-click")
        self.assertIn(b"current    no", shown_text)
        self.assertIn(b"newer pin(s) unchecked", shown_text)
        self.assertEqual(self.cli_json("show", "F-click")["data"]["current"], False)

        # re-anchoring on the newest pin records what changed there: STALE, and nothing newer
        report = self.cli_json("reanchor", "--latest")["data"]
        self.assertEqual((report["target"], report["results"][0]["freshness"]), (changed, "STALE"))
        _, fresh = self.reading("F-click")
        self.assertEqual((fresh.value, fresh.commit, fresh.check, fresh.newer_pins),
                         ("STALE", changed, "reanchor", []))
        found = self.use("find_findings", {})
        self.assertEqual((ids(found), found["data"]["not_current"]), ([], {"STALE": 1}))

    def test_reanchoring_on_the_latest_pin_makes_a_finding_current_until_the_next_pin(self) -> None:
        same, later = REPO.commits["same"], REPO.commits["later"]
        self.pin("base", "same")
        self.add_click()
        self.memory.verify()
        _, fresh = self.reading("F-click")
        self.assertEqual((fresh.current, fresh.newer_pins), (False, [same]))

        report = self.cli_json("reanchor", "--latest")["data"]
        self.assertEqual((report["target"], report["results"][0]["freshness"],
                          report["results"][0]["outcomes"]), (same, "CURRENT", {"identical": 1}))
        state, fresh = self.reading("F-click")
        self.assertEqual((fresh.value, fresh.commit, fresh.newer_pins), ("CURRENT", same, []))
        self.assertTrue(is_current(state, fresh))
        self.assertEqual(ids(self.use("find_findings", {})), ["F-click"])
        self.assertEqual(self.cli_json("list", "--current")["data"]["total"], 1)
        notes = build_notes(self.memory.view(), pins=self.store.list_pins())
        self.assertEqual(notes.summary()["findings"]["exported"], 1)

        # an older pin added afterwards is not newer than the checked one
        self.pin("changed")
        _, fresh = self.reading("F-click")
        self.assertEqual((fresh.current, fresh.newer_pins), (True, []))

        # a newer pin after the check takes the finding out of the current view again
        self.pin("later")
        state, fresh = self.reading("F-click")
        self.assertEqual((fresh.value, fresh.commit, fresh.newer_pins, fresh.newest_pin),
                         ("CURRENT", same, [later], later))
        self.assertFalse(is_current(state, fresh))
        found = self.use("find_findings", {})
        self.assertEqual((ids(found), found["data"]["not_current"]), ([], {NEWER_PIN_UNCHECKED: 1}))
        self.assertIn(later, self.use("get_finding", {"finding_id": "F-click"})["warnings"][0])
        self.assertEqual(self.cli_json("list", "--current")["data"]["total"], 0)
        self.assertEqual(build_notes(self.memory.view(), pins=self.store.list_pins())
                         .summary()["findings"]["exported"], 0)
        self.assertEqual(self.cli_json("reanchor", "--latest")["data"]["target"], later)
        self.assertEqual(ids(self.use("find_findings", {})), ["F-click"])

    def test_reanchor_latest_needs_exactly_one_target(self) -> None:
        base = REPO.commits["base"]
        self.assertIn(b"no pinned commits", self.cli("reanchor", "--latest", expect=2))
        self.pin("base")
        self.add_click()
        self.assertIn(b"TARGET commit or --latest", self.cli("reanchor", expect=2))
        self.assertIn(b"not both", self.cli("reanchor", base, "--latest", expect=2))
        self.assertEqual(self.cli_json("reanchor", "--latest")["data"]["target"], base)


class ExternalEvidenceTest(FreshnessCase):
    def test_findings_without_code_spans_are_current_and_visible_by_default(self) -> None:
        """A web-only OFFICIAL finding and an INFERENCE without spans read NOT_APPLICABLE:
        they are listed, exported and marked current by default, with the retrieval date
        and the recheck date their recorded text names; a code finding checked only at an
        older pin is not."""
        changed = REPO.commits["changed"]
        self.pin("base", "changed")
        self.add_click()
        self.memory.add(EXTERNAL, AUTHOR)
        self.memory.add(INFERENCE, AUTHOR)
        results = self.memory.verify()
        self.assertEqual([r["finding_id"] for r in results if "skipped" in r], ["F-ext", "F-inf"])

        state, fresh = self.reading("F-ext")
        self.assertEqual((fresh.value, fresh.checkable, fresh.retrieved, fresh.recheck_after,
                          fresh.newer_pins, fresh.newest_pin, fresh.commit),
                         (NOT_APPLICABLE, False, "2026-09-30", "2026-10-09", [], changed, None))
        self.assertTrue(fresh.current)
        self.assertTrue(is_current(state, fresh))
        self.assertTrue(fresh.to_dict()["note"].startswith("external evidence only"))
        _, inference = self.reading("F-inf")
        self.assertEqual((inference.value, inference.retrieved, inference.recheck_after),
                         (NOT_APPLICABLE, "2026-09-01", None))

        found = self.use("find_findings", {})
        self.assertEqual((ids(found), found["data"]["not_current"]),
                         (["F-ext", "F-inf"], {NEWER_PIN_UNCHECKED: 1}))
        ext = found["data"]["findings"][0]
        self.assertTrue(ext["current"])
        self.assertEqual((ext["freshness"]["value"], ext["freshness"]["retrieved"],
                          ext["freshness"]["recheck_after"], ext["citations"]),
                         (NOT_APPLICABLE, "2026-09-30", "2026-10-09", []))
        for arguments, expected in (
            ({"statuses": ["EXTERNAL_RECHECK"]}, ["F-ext"]),
            ({"evidence_classes": ["OFFICIAL", "THIRD_PARTY"]}, ["F-ext"]),
            ({"freshness": [NOT_APPLICABLE]}, ["F-ext", "F-inf"]),
            ({"freshness": ["CURRENT", NOT_APPLICABLE]}, ["F-ext", "F-inf"]),
            ({"query": "scraping"}, ["F-ext"]),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(ids(self.use("find_findings", arguments)), expected)
        detail = self.use("get_finding", {"finding_id": "F-ext"})
        self.assertTrue(detail["data"]["finding"]["current"])
        [warning] = detail["warnings"]
        for text in ("external evidence only", "retrieved 2026-09-30", "recheck after 2026-10-09"):
            self.assertIn(text, warning)
        self.assertNotIn("do not present it as current", warning)

        notes = build_notes(self.memory.view(), pins=self.store.list_pins())
        summary = notes.summary()["findings"]
        self.assertEqual((summary["exported"], summary["external"], summary["current"],
                          summary["left_out"]),
                         (2, 2, 2, {f"not_current_{NEWER_PIN_UNCHECKED}": 1}))
        [path] = [p for p in notes.files if "F-ext" in p]
        note = notes.files[path].decode("utf-8")
        for text in ("freshness: NOT_APPLICABLE", "current: true", "checkable: false",
                     'retrieved: "2026-09-30"', 'recheck_after: "2026-10-09"',
                     "**External evidence.**", "recheck after `2026-10-09`"):
            self.assertIn(text, note)
        self.assertNotIn("NOT CURRENT", note)
        self.assertTrue(note.split("\n")[1].startswith("title: "))
        index = notes.files[[p for p in notes.files if p.endswith("index.md")][0]].decode("utf-8")
        current_part, external_part = index.split("## External evidence, not re-verifiable (2)")
        self.assertIn("## Current findings (0)", current_part)
        self.assertNotIn("F-ext", current_part)
        self.assertIn("F-ext", external_part)
        self.assertIn("F-inf", external_part)

        listed = self.cli_json("list")
        by_id = {item["finding_id"]: item for item in listed["data"]["findings"]}
        self.assertEqual((by_id["F-ext"]["current"], by_id["F-ext"]["reading"]["value"],
                          by_id["F-click"]["current"]), (True, NOT_APPLICABLE, False))
        text = self.cli("list")
        self.assertIn(b"NOT_APPLICABLE (external evidence, not re-verifiable); retrieved "
                      b"2026-09-30; recheck after 2026-10-09", text)
        self.assertEqual(sorted(i["finding_id"] for i in
                                self.cli_json("list", "--freshness", NOT_APPLICABLE)["data"]["findings"]),
                         ["F-ext", "F-inf"])
        self.assertEqual(sorted(i["finding_id"] for i in
                                self.cli_json("list", "--current")["data"]["findings"]),
                         ["F-ext", "F-inf"])
        self.assertIn(b"current    yes", self.cli("show", "F-ext"))

    def test_recheck_dates_are_read_from_recorded_text(self) -> None:
        cases = [
            ({"claim": "Valid until 2026-12-31."}, "2026-12-31"),
            ({"limitations": ["Expires 2027-01-15.", "Effective from 2026-10-09."]}, "2027-01-15"),
            ({"attributes": {"note": "Recheck after: 2026-11-01"}}, "2026-11-01"),
            ({"sources": [{"kind": "web", "quote": "in force from 2026-10-09"}]}, "2026-10-09"),
            ({"claim": "The policy changed on 2026-10-09."}, None),
            ({}, None),
        ]
        for record, expected in cases:
            with self.subTest(record=record):
                self.assertEqual(recheck_after(record), expected)


if __name__ == "__main__":
    unittest.main()
