"""Research import: the schema, the strict YAML subset, and a round trip of the example."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from timelinexray.errors import InvalidInput, Refused
from timelinexray.findings import Actor, FindingsMemory, Ledger, research, yamlsub
from tests.support import REPO_ROOT, FixtureRepo, run_cli

EXAMPLE = REPO_ROOT / "schemas" / "research-import.example.yaml"
SCHEMA = REPO_ROOT / "schemas" / "research-import.schema.json"
IMPORTER = Actor("importer-1", "importer")

FIXTURE: FixtureRepo


def setUpModule() -> None:
    global FIXTURE
    FIXTURE = FixtureRepo()


def tearDownModule() -> None:
    FIXTURE.cleanup()


def example() -> dict:
    return yamlsub.load(EXAMPLE.read_text("utf-8"))


class SchemaTest(unittest.TestCase):
    def test_the_schema_id_constant_is_the_schemas_id(self) -> None:
        schema = json.loads(SCHEMA.read_text("utf-8"))
        self.assertEqual(research.IMPORT_SCHEMA_ID, schema["$id"])

    def test_the_schema_and_the_validator_agree(self) -> None:
        schema = json.loads(SCHEMA.read_text("utf-8"))
        finding = schema["$defs"]["finding"]
        self.assertEqual(tuple(finding["required"]), research.FINDING_FIELDS)
        self.assertEqual(set(finding["properties"]), set(research.FINDING_FIELDS))
        for name, values in research.ENUMS.items():
            with self.subTest(field=name):
                self.assertEqual(tuple(finding["properties"][name]["enum"]), values)
        self.assertEqual(finding["properties"]["id"]["pattern"], research.ID_PATTERN)
        code = schema["$defs"]["code_source"]
        web = schema["$defs"]["web_source"]
        self.assertEqual(tuple(code["required"]), research.CODE_SOURCE_FIELDS)
        self.assertEqual(tuple(web["required"]), research.WEB_SOURCE_FIELDS)
        self.assertEqual(code["properties"]["lines"]["pattern"], research.LINES_PATTERN)
        self.assertEqual(code["properties"]["commit"]["pattern"], research.COMMIT_PATTERN)
        self.assertEqual(code["properties"]["anchor"]["maxLength"], research.MAX_ANCHOR)
        self.assertEqual(web["properties"]["url"]["pattern"], research.URL_PATTERN)
        self.assertEqual(web["properties"]["retrieved"]["pattern"], research.DATE_PATTERN)
        for name, limit in research.TEXT_FIELDS.items():
            if "maxLength" in finding["properties"][name]:
                self.assertEqual(finding["properties"][name]["maxLength"], limit, name)
        self.assertFalse(schema["additionalProperties"] or finding["additionalProperties"]
                         or code["additionalProperties"] or web["additionalProperties"])

    def test_the_example_is_valid_and_synthetic(self) -> None:
        data = example()
        self.assertEqual(research.validate(data), [])
        self.assertEqual(len(data["findings"]), 6)
        text = EXAMPLE.read_text("utf-8")
        self.assertIn("SYNTHETIC", text)
        commits = {s["commit"] for f in data["findings"] for s in f["sources"] if s["kind"] == "code"}
        self.assertEqual(commits, {FIXTURE.commit})

    def test_invalid_files_are_described(self) -> None:
        good = example()["findings"][0]
        cases = [
            ({"findings": []}, "non-empty list"),
            ({"items": [good]}, "only key is 'findings'"),
            ({"findings": [{k: v for k, v in good.items() if k != "note"}]}, "missing note"),
            ({"findings": [{**good, "extra": "x"}]}, "unknown field extra"),
            ({"findings": [{**good, "status": "VERIFIED"}]}, "status must be one of"),
            ({"findings": [{**good, "creator_controllable": False}]}, "creator_controllable"),
            ({"findings": [good, good]}, "duplicate id EX-001"),
            ({"findings": [{**good, "sources": [{**good["sources"][0], "lines": "0-3"}]}]},
             "lines must be"),
            ({"findings": [{**good, "sources": [{**good["sources"][0], "path": "/etc/x"}]}]},
             "absolute paths"),
            ({"findings": [{**good, "sources": [{**good["sources"][0], "commit": "HEAD"}]}]},
             "commit must be"),
            ({"findings": [{**good, "sources": [{"kind": "web", "url": "ftp://x", "publisher": "",
                                                 "published": "2026-1-1", "retrieved": "x",
                                                 "quote": ""}]}]}, "url must be"),
            ({"findings": [{**good, "sources": [{"kind": "file"}]}]}, "kind must be"),
        ]
        for data, text in cases:
            with self.subTest(text=text):
                problems = research.validate(data)
                self.assertTrue(any(text in problem for problem in problems), problems)


class YamlSubsetTest(unittest.TestCase):
    def test_supported_constructs(self) -> None:
        text = (
            "---\n"
            "# comment\n"
            "top:\n"
            "- a: plain value # trailing comment\n"
            "  b: 'single ''quoted'' value'\n"
            '  c: "double \\"quoted\\"\\tvalue \\u00e9"\n'
            "  d: continued plain\n"
            "    scalar over lines\n"
            "\n"
            "    and a paragraph\n"
            "  e:\n"
            "  f: null\n"
            "  g: []\n"
            "  h: 'no'\n"
            "  i: no\n"
            "  j: 12\n"
            "- nested:\n"
            "    deeper: \"folded\n"
            "      double\"\n"
            "  list:\n"
            "    - x\n"
            "    -\n"
            "      y: z\n"
            "...\n"
        )
        self.assertEqual(yamlsub.load(text), {"top": [
            {"a": "plain value", "b": "single 'quoted' value", "c": 'double "quoted"\tvalue \u00e9',
             "d": "continued plain scalar over lines\nand a paragraph", "e": None, "f": None,
             "g": [], "h": "no", "i": "no", "j": "12"},
            {"nested": {"deeper": "folded double"}, "list": ["x", {"y": "z"}]},
        ]})

    def test_unsupported_constructs_are_errors(self) -> None:
        cases = [
            ("a: &anchor x\n", "unsupported"),
            ("a: *alias\n", "unsupported"),
            ("a: !tag x\n", "unsupported"),
            ("a: |\n  block\n", "unsupported"),
            ("a: [1, 2]\n", "unsupported"),
            ("a: 1\na: 2\n", "duplicate key"),
            ("a:\n\tb: 1\n", "tabs"),
            ("a: b: c\n", "may not contain ': '"),
            ("a: 'open\n", "unterminated"),
            ("- - x\n", "nested sequences"),
            ("a: 1\n b: 2\n", "may not contain"),
            ("a: 1\n---\nb: 2\n", "unexpected content"),
        ]
        for text, message in cases:
            with self.subTest(text=text):
                with self.assertRaises(yamlsub.YamlError) as raised:
                    yamlsub.load(text)
                self.assertIn(message, str(raised.exception))


class ImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-import-")
        self.tmp = Path(self._tmp.name)
        self.memory = FindingsMemory(Ledger(self.tmp / "ledger"), FIXTURE.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_import_keeps_statuses_and_verifies_code_sources(self) -> None:
        report = self.memory.import_file(EXAMPLE, "EXAMPLE", IMPORTER)
        self.assertEqual((report["findings"], report["imported"], report["verified"]), (6, 6, 4))
        self.assertEqual(report["citations"], {"INTACT": 4})
        self.assertEqual(report["anchors"], {"FOUND": 2, "FOUND_MULTIPLE": 1, "MISSING": 1})
        view = self.memory.view()
        expected = {"EX-001": ("SUPPORTED", "CURRENT"), "EX-002": ("PARTIAL", "CURRENT"),
                    "EX-003": ("SUPPORTED", "CURRENT"), "EX-004": ("SUPPORTED", "UNVERIFIABLE"),
                    "EX-005": ("EXTERNAL_RECHECK", "NOT_CHECKED"),
                    "EX-006": ("PARTIAL", "NOT_CHECKED")}
        for state in view.states():
            with self.subTest(state.finding_id):
                report_id = state.finding_id.split(":", 1)[1]
                self.assertTrue(state.finding_id.startswith("EXAMPLE:"))
                self.assertEqual((state.status, state.freshness()[0]), expected[report_id])
                self.assertEqual((state.status_basis, state.workflow), ("reported", "imported"))
        cited = view.get("EXAMPLE:EX-003").record["sources"][0]
        self.assertEqual((cited["start_line"], cited["end_line"], cited["reported"]["lines"]),
                         (3, 3, "3"))
        web = view.get("EXAMPLE:EX-006").record["sources"][0]
        self.assertEqual(web["published_precision"], "month")
        self.assertEqual(view.queue(), sorted(view.queue(), key=lambda i: i["priority"]))
        self.assertEqual({i["trigger"] for i in view.queue()}, {"citation_unusable"})
        self.assertEqual(len(view.queue(include_imported=True)), 7)  # EX-002 is confirmed now

    def test_round_trip_yaml_and_json(self) -> None:
        self.memory.import_file(EXAMPLE, "EXAMPLE", IMPORTER)
        exported = self.memory.export("research", label="EXAMPLE")
        self.assertEqual(exported, example())
        json_file = self.tmp / "example.json"
        json_file.write_text(json.dumps(exported), "utf-8")
        report = self.memory.import_file(json_file, "EXAMPLE2", IMPORTER)
        self.assertEqual((report["format"], report["imported"]), ("json", 6))
        self.assertEqual(self.memory.export("research", label="EXAMPLE2"), example())
        view = self.memory.view()
        for number in range(1, 7):
            one = view.get(f"EXAMPLE:EX-00{number}").record
            two = view.get(f"EXAMPLE2:EX-00{number}").record
            self.assertEqual(one["origin"]["finding_sha256"], two["origin"]["finding_sha256"])
            self.assertEqual(one["sources"], two["sources"])

    def test_reimport_is_idempotent_and_changes_are_refused(self) -> None:
        self.memory.import_file(EXAMPLE, "EXAMPLE", IMPORTER)
        again = self.memory.import_file(EXAMPLE, "EXAMPLE", IMPORTER)
        self.assertEqual((again["imported"], again["unchanged"]), (0, 6))
        changed = example()
        changed["findings"][0]["note"] = "Edited note."
        path = self.tmp / "changed.json"
        path.write_text(json.dumps(changed), "utf-8")
        with self.assertRaises(Refused):
            self.memory.import_file(path, "EXAMPLE", IMPORTER)
        self.assertEqual(self.memory.view().events, 10)

    def test_invalid_file_imports_nothing(self) -> None:
        bad = example()
        bad["findings"][1]["status"] = "TRUE"
        path = self.tmp / "bad.json"
        path.write_text(json.dumps(bad), "utf-8")
        with self.assertRaises(InvalidInput) as raised:
            self.memory.import_file(path, "BAD", IMPORTER)
        self.assertIn("EX-002", str(raised.exception))
        self.assertEqual(self.memory.view().events, 0)
        with self.assertRaises(InvalidInput):
            self.memory.import_file(EXAMPLE, "bad label", IMPORTER)

    def test_markdown_and_unpinned_commits(self) -> None:
        yaml_text = (
            "findings:\n- id: MD-1\n  title: From markdown\n  claim: Synthetic.\n"
            "  component: src\n  evidence_class: CODE\n  status: SUPPORTED\n  sources:\n"
            "  - kind: code\n    path: src/lib.rs\n    lines: 1-1\n"
            f"    commit: {'1' * 40}\n    anchor: CLICK\n"
            "  creator_relevance: none\n  creator_controllable: 'no'\n  implication: ''\n"
            "  volatility: stable\n  misuse_risk: low\n  note: ''\n"
        )
        markdown = self.tmp / "report.md"
        markdown.write_text(f"# Report\n\nProse.\n\n```yaml\n{yaml_text}```\n\nMore prose.\n",
                            "utf-8")
        report = self.memory.import_file(markdown, "MD", IMPORTER)
        self.assertEqual((report["format"], report["imported"]), ("markdown", 1))
        state = self.memory.get("MD:MD-1")
        self.assertEqual(state.record["sources"][0]["resolution"], "commit_not_pinned")
        self.assertIsNone(state.record["sources"][0]["span_sha256"])
        self.assertEqual((state.integrity["verdict"], state.freshness()[0]),
                         ("MISSING", "UNVERIFIABLE"))
        twice = self.tmp / "twice.md"
        twice.write_text(f"```\n{yaml_text}```\n```\n{yaml_text}```\n", "utf-8")
        with self.assertRaises(InvalidInput):
            self.memory.import_file(twice, "MD2", IMPORTER)

    def test_citations_of_a_commit_pinned_later_are_resolved_by_verify(self) -> None:
        """An import names the commits it could not read; pinning them and running
        ``verify --label`` records the resolved citations (hashes, permalink data) as a
        provenance revision that every consumer displays, and the finding is CURRENT at
        its cited commit."""
        from timelinexray.mcp.guard import StoreGuard
        from timelinexray.mcp.server import Server
        from timelinexray.export.notes import build_notes
        import io

        store = FIXTURE.new_store("partial-store")
        ledger = self.tmp / "partial-ledger"
        memory = FindingsMemory(Ledger(ledger), store)  # type: ignore[arg-type]
        report = memory.import_file(EXAMPLE, "EXAMPLE", IMPORTER)
        self.assertEqual(report["unpinned_commits"], {FIXTURE.commit: 4})
        self.assertIn(f"txray pin {FIXTURE.commit}", report["hint"])
        self.assertIn("txray findings verify --label EXAMPLE", report["hint"])
        before = memory.get("EXAMPLE:EX-001")
        self.assertEqual(before.record["sources"][0]["resolution"], "commit_not_pinned")
        self.assertEqual(before.resolved_citations()[0]["span_sha256"], None)
        self.assertEqual(before.freshness(), ("UNVERIFIABLE", "cited"))

        store.pin(FIXTURE.commit, FIXTURE.url, allowlist=FIXTURE.allowlist)  # type: ignore[attr-defined]
        with self.assertRaises(InvalidInput):
            memory.verify(["EXAMPLE:EX-001"], label="EXAMPLE")
        results = {r["finding_id"]: r for r in memory.verify(label="EXAMPLE")}
        self.assertEqual(set(results), {f"EXAMPLE:EX-00{n}" for n in range(1, 7)})
        first = results["EXAMPLE:EX-001"]
        self.assertEqual((first["freshness"], first["verdict"], first["resolved_citations"],
                          first["recheck_targets"]), ("CURRENT", "INTACT", 1, []))
        after = memory.get("EXAMPLE:EX-001")
        self.assertEqual(after.record["sources"][0]["resolution"], "commit_not_pinned")  # immutable
        [citation] = after.resolved_citations()
        self.assertEqual((citation["commit"], citation["resolution"], len(citation["span_sha256"]),
                          citation["resolved_by"]), (FIXTURE.commit, None, 64, after.integrity["event"]))
        self.assertEqual(citation["reported"], {"commit": FIXTURE.commit, "lines": "1-1"})
        self.assertEqual(after.provenance[-1]["mode"], "integrity")
        self.assertEqual(after.freshness(), ("CURRENT", "cited"))
        server = Server(StoreGuard(store.root, ledger), log=io.StringIO())  # type: ignore[attr-defined]
        from tests.mcp_support import call
        line = server.handle_line(json.dumps(call("get_finding", {"finding_id": "EXAMPLE:EX-001"})).encode())
        finding = json.loads(line)["result"]["structuredContent"]["data"]["finding"]
        self.assertTrue(finding["current"])
        self.assertEqual((finding["freshness"]["value"], finding["freshness"]["commit"]),
                         ("CURRENT", FIXTURE.commit))
        self.assertEqual((finding["citations"][0]["resolution"], finding["citations"][0]["span_sha256"]),
                         (None, citation["span_sha256"]))
        notes = build_notes(memory.view())
        note = notes.files[[p for p in notes.files if "EX-001" in p][0]].decode()
        self.assertIn(f"span sha256 `{citation['span_sha256']}`", note)
        self.assertNotIn("not resolved", note)

    def test_import_report_names_the_commits_to_pin_and_consumers_agree_after_resolution(self) -> None:
        """Over the CLI: the import report lists the unpinned commits with the command that
        resolves them; after pinning, ``verify --label`` resolves the citations, and
        ``show``, the MCP ``verify_claim`` and ``get_finding`` display the resolved form."""
        import io
        from timelinexray.mcp.guard import StoreGuard
        from timelinexray.mcp.server import Server
        from tests.mcp_support import call

        store = FIXTURE.new_store("partial-cli-store")
        Path(store.root).mkdir()  # type: ignore[attr-defined]  # the MCP guard wants a directory
        ledger = self.tmp / "partial-cli-ledger"
        args = ["--store", str(store.root), "--ledger", str(ledger)]  # type: ignore[attr-defined]
        code, out, err = run_cli(["findings", "import", str(EXAMPLE), "--source-label",
                                  "EXAMPLE", "--actor", "importer-1", *args])
        self.assertEqual((code, err), (0, b""))
        self.assertIn(b"unpinned   " + FIXTURE.commit.encode(), out)
        self.assertIn(b"next       4 citation(s) name commits this store has not pinned; to "
                      b"resolve them: txray pin " + FIXTURE.commit.encode()
                      + b"; then txray findings verify --label EXAMPLE", out)
        code, out, _ = run_cli(["findings", "show", "EXAMPLE:EX-001", *args])
        self.assertEqual(code, 0)
        self.assertIn(b"span not resolved", out)
        self.assertIn(b"freshness  UNVERIFIABLE@", out)

        def claim() -> dict:
            server = Server(StoreGuard(store.root, ledger), log=io.StringIO())  # type: ignore[attr-defined]
            line = server.handle_line(json.dumps(call(
                "verify_claim", {"finding_id": "EXAMPLE:EX-001"})).encode("utf-8"))
            return json.loads(line)["result"]["structuredContent"]["data"]

        before = claim()
        self.assertEqual((before["at_cited"]["freshness"], before["citations"][0]["verdict"]),
                         ("UNVERIFIABLE", "MISSING"))

        store.pin(FIXTURE.commit, FIXTURE.url, allowlist=FIXTURE.allowlist)  # type: ignore[attr-defined]
        code, out, err = run_cli(["findings", "verify", "--label", "EXAMPLE", *args])
        self.assertEqual((code, err), (0, b""))
        self.assertIn(b"EXAMPLE:EX-001           CURRENT       spans INTACT  (1 citation(s) "
                      b"resolved now)", out)
        self.assertNotIn(b"next       ", out)  # nothing was re-anchored before, so no recheck
        code, out, _ = run_cli(["findings", "show", "EXAMPLE:EX-001", *args])
        self.assertEqual(code, 0)
        self.assertIn(b"(resolved by a later verify)", out)
        self.assertNotIn(b"span not resolved", out)
        self.assertIn(b"freshness  CURRENT@", out)
        self.assertIn(b"provenance r2 @ the cited commits (resolved)", out)
        after = claim()
        [citation] = after["citations"]
        # the recorded hashes are now compared (no baseline read) and match
        self.assertEqual((after["at_cited"]["freshness"], citation["verdict"], citation["baseline"],
                          after["at_cited"]["recorded"]["freshness"]),
                         ("CURRENT", "INTACT", False, "CURRENT"))

    def test_import_command(self) -> None:
        ledger = str(self.tmp / "cli-ledger")
        args = ["--store", str(FIXTURE.store_dir), "--ledger", ledger]
        code, out, err = run_cli(["findings", "import", str(EXAMPLE), "--source-label", "EXAMPLE",
                                  "--actor", "importer-1", "--json", *args])
        self.assertEqual((code, err), (0, b""))
        doc = json.loads(out)
        self.assertEqual((doc["command"], doc["data"]["imported"]), ("findings import", 6))
        out_file = self.tmp / "export.json"
        code, _, err = run_cli(["findings", "export", "--format", "research", "--label",
                                "EXAMPLE", "--out", str(out_file), *args])
        self.assertEqual((code, err), (0, b""))
        self.assertEqual(json.loads(out_file.read_text("utf-8")), example())


if __name__ == "__main__":
    unittest.main()
