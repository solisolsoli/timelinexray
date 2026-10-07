"""The stale-review worklist (``txray findings stale``) and the one-command fix for unpinned
citations (``txray findings verify --pin-cited``), on a synthetic two-commit history.

The base commit holds a parameter file, a scorer, two filters and a file whose cited line
is duplicated later; the second (newer) commit changes the parameter literal, the scorer and
one filter, and duplicates the cited line of the other. Nothing here uses the network: the
fixture is a local ``file://`` repository.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from timelinexray.errors import InvalidInput
from timelinexray.findings import Actor, FindingsMemory, Ledger
from timelinexray.findings.stale import DRAFT_KEY, worklist
from timelinexray.netguard import ENV_ALLOW_FILE_URLS, Allowlist
from timelinexray.snapshot import SnapshotStore
from tests.index_support import add_commit
from tests.support import build_fixture_repo, file_url, run_cli

PARAM = b"// synthetic parameters\npub const MIX_WEIGHT: f64 = 0.4;\npub const CAP: u32 = 50;\n"
SCORER = b"// synthetic scorer\nfn combine(a: f64) -> f64 {\n    a * 2.0\n}\n"
AGE = b"// synthetic filter\nfn keep(age: u32) -> bool {\n    age < 30\n}\n"
DUP = b"// synthetic list\nconst NAMES: [&str; 1] = [\"one\"];\n"
NOTES = b"# notes\nunchanged\n"
BASE = {"home/param.rs": PARAM, "scorers/combine.rs": SCORER, "filters/age.rs": AGE,
        "filters/dup.rs": DUP, "docs/notes.md": NOTES}
NEW = {
    "home/param.rs": PARAM.replace(b"0.4", b"0.3"),
    "scorers/combine.rs": SCORER.replace(b"a * 2.0", b"a * 3.0"),
    "filters/age.rs": AGE.replace(b"age < 30", b"age < 40"),
    "filters/dup.rs": DUP + b"const NAMES: [&str; 1] = [\"one\"];\n",
    "docs/notes.md": NOTES,
}
AUTHOR = Actor("author-a", "author")
REVIEWER = Actor("reviewer-b", "reviewer")


class Fixture:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-stale-")
        self.root = Path(self._tmp.name)
        self.git_dir, self.base = build_fixture_repo(self.root / "upstream", BASE, frozenset())
        self.new = add_commit(self.git_dir, NEW, self.base,
                              committer_date="2030-01-01T00:00:00+00:00")
        self.url = file_url(self.git_dir)
        self.allowlist = Allowlist([self.url])
        self.count = 0

    def store(self, *commits: str) -> SnapshotStore:
        self.count += 1
        store = SnapshotStore(self.root / f"store-{self.count}")
        for commit in commits:
            store.pin(commit, self.url, allowlist=self.allowlist)
        return store

    def memory(self, store: SnapshotStore) -> FindingsMemory:
        self.count += 1
        return FindingsMemory(Ledger(self.root / f"ledger-{self.count}"), store)

    def cite(self, path: str, lines: str, anchor: str) -> dict:
        return {"commit": self.base, "path": path, "lines": lines, "anchor": anchor}

    def cleanup(self) -> None:
        self._tmp.cleanup()


FIX: Fixture


def setUpModule() -> None:
    global FIX
    FIX = Fixture()


def tearDownModule() -> None:
    FIX.cleanup()


def spec(finding_id: str, cls: str, citation: dict, component: str = "synthetic") -> dict:
    return {"finding_id": finding_id, "title": f"Synthetic {finding_id}",
            "claim": f"Synthetic claim {finding_id}.", "component": component,
            "evidence_class": cls, "status": "SUPPORTED", "citations": [citation]}


def line_sha(content: bytes, line: int) -> str:
    return hashlib.sha256(content.splitlines(keepends=True)[line - 1]).hexdigest()


class WorklistTest(unittest.TestCase):
    """The worklist content, its order and that it never writes the ledger."""

    def setUp(self) -> None:
        self.store = FIX.store(FIX.base, FIX.new)
        self.memory = FIX.memory(self.store)
        add = self.memory.add
        add(spec("D-param", "PARAM_DEFAULT", FIX.cite("home/param.rs", "2", "MIX_WEIGHT")), AUTHOR)
        add(spec("C-score", "CODE", FIX.cite("scorers/combine.rs", "3", "a * 2.0")), AUTHOR)
        add(spec("A-dup", "CODE", FIX.cite("filters/dup.rs", "2", "NAMES")), AUTHOR)
        add(spec("B-age", "CODE", FIX.cite("filters/age.rs", "3", "age < 30")), AUTHOR)
        add(spec("E-notes", "REPO_DOC", FIX.cite("docs/notes.md", "2", "unchanged")), AUTHOR)
        add(spec("F-named", "CODE", FIX.cite("filters/age.rs", "2", "fn keep"),
                 component="ranking"), AUTHOR)
        self.memory.verify()
        self.memory.reanchor(FIX.new)

    def test_entries_show_the_old_and_the_aligned_span_with_hash_and_verdict(self) -> None:
        work = worklist(self.memory)  # default target: the newest pin
        self.assertEqual(work.target, FIX.new)
        entry = next(e for e in work.entries if e["finding_id"] == "D-param")
        self.assertEqual((entry["freshness"], entry["area"], entry["priority"]),
                         ("STALE", "parameter", 1))
        [citation] = entry["citations"]
        self.assertEqual(citation["outcome"], "changed")
        self.assertEqual(citation["old"], {
            "commit": FIX.base, "path": "home/param.rs", "start_line": 2, "end_line": 2,
            "span_sha256": line_sha(PARAM, 2), "anchor": "MIX_WEIGHT"})
        candidate = citation["candidate"]
        self.assertEqual((candidate["commit"], candidate["path"], candidate["start_line"],
                          candidate["end_line"], candidate["anchor_verdict"]),
                         (FIX.new, "home/param.rs", 2, 2, "FOUND"))
        self.assertEqual(candidate["span_sha256"], line_sha(NEW["home/param.rs"], 2))
        read = entry["commands"]["read"]
        self.assertEqual(read, [
            f"txray show {FIX.base[:12]} home/param.rs --lines 2-2 --anchor MIX_WEIGHT",
            f"txray show {FIX.new[:12]} home/param.rs --lines 2-2 --anchor MIX_WEIGHT"])
        decide = entry["commands"]["decide"]
        self.assertEqual(decide["assess_at_target"],
                         "txray findings review D-param --actor REVIEWER --role reviewer "
                         f"--status STATUS --target {FIX.new[:12]} --rationale RATIONALE")
        self.assertIn("txray findings supersede D-param --actor AUTHOR --file "
                      f"SPEC_DIR/D-param.{FIX.new[:7]}.json", decide["supersede"])
        self.assertEqual(decide["verify_successor"],
                         f"txray findings verify D-param.{FIX.new[:7]}")
        self.assertTrue(entry["draft"]["possible"])

    def test_order_is_area_then_trigger_priority_then_id(self) -> None:
        work = worklist(self.memory, FIX.new)
        order = [(e["finding_id"], e["area"], e["priority"]) for e in work.entries]
        self.assertEqual(order, [("D-param", "parameter", 1), ("C-score", "scoring", 1),
                                 ("B-age", "other", 1), ("A-dup", "other", 2)])
        self.assertEqual([e["rank"] for e in work.entries], [1, 2, 3, 4])
        counts = work.counts()
        self.assertEqual(counts["freshness"], {"CURRENT": 2, "STALE": 3, "UNVERIFIABLE": 1})
        self.assertEqual(counts["by_area"], {"other": 2, "parameter": 1, "scoring": 1})
        self.assertEqual(counts["by_outcome"], {"ambiguous": 1, "changed": 3})
        # only D-param's anchor survives in its aligned span: C-score's and B-age's anchors were
        # the changed text, so their successors must be cited by hand
        self.assertEqual((counts["checkable"], counts["not_current"], counts["drafts_possible"]),
                         (6, 4, 1))
        blocked = {e["finding_id"]: e["draft"]["why_not"] for e in work.entries}
        self.assertIn("without a confirming anchor", blocked["C-score"])
        self.assertIsNone(blocked["D-param"])
        # F-named stays CURRENT (its line is unchanged): a scoring component, not listed
        self.assertNotIn("F-named", [e["finding_id"] for e in work.entries])

    def test_an_ambiguous_span_lists_every_occurrence_and_has_no_draft(self) -> None:
        entry = next(e for e in worklist(self.memory, FIX.new).entries if e["finding_id"] == "A-dup")
        [citation] = entry["citations"]
        self.assertEqual(citation["outcome"], "ambiguous")
        self.assertEqual([(c["start_line"], c["end_line"]) for c in citation["candidates"]],
                         [(2, 2), (3, 3)])
        self.assertIsNone(citation["candidate"])
        self.assertFalse(entry["draft"]["possible"])
        self.assertIn("ambiguous", entry["draft"]["why_not"])
        self.assertIsNone(entry["commands"]["decide"]["supersede"])
        self.assertEqual(len(entry["commands"]["read"]), 3)  # the old span and both occurrences

    def test_the_worklist_never_writes_the_ledger(self) -> None:
        events = self.memory.ledger.events_path
        before = events.read_bytes()
        head = (self.memory.ledger.directory / "HEAD").read_bytes()
        with tempfile.TemporaryDirectory(prefix="txray-specs-") as specs:
            store_dir, ledger_dir = str(self.store.root), str(self.memory.ledger.directory)
            code, out, err = run_cli(["findings", "stale", "--store", store_dir, "--ledger",
                                      ledger_dir, "--spec-dir", specs, "--json"])
            self.assertEqual((code, err), (0, b""))
            data = json.loads(out)["data"]
            self.assertEqual([Path(p).name for p in data["drafts_written"]],
                             [f"D-param.{FIX.new[:7]}.json"])
        self.assertEqual(events.read_bytes(), before)
        self.assertEqual((self.memory.ledger.directory / "HEAD").read_bytes(), head)
        states = {s.finding_id: (s.status, s.status_basis, s.workflow)
                  for s in self.memory.view().states()}
        self.assertEqual(set(states.values()), {("SUPPORTED", "proposed", "draft")})

    def test_json_is_complete_and_deterministic(self) -> None:
        args = ["findings", "stale", "--store", str(self.store.root), "--ledger",
                str(self.memory.ledger.directory), "--json", "--limit", "1"]
        first, second = run_cli(args), run_cli(args)
        self.assertEqual(first, second)
        data = json.loads(first[1])["data"]
        self.assertEqual(data["schema"], "timelinexray/stale-review/v1")
        self.assertEqual((data["shown"], len(data["entries"])), (4, 4))  # --json lists all
        self.assertTrue(all("_located" not in entry for entry in data["entries"]))
        code, text, _ = run_cli(args[:-3] + ["--limit", "1"])
        self.assertEqual(code, 0)
        self.assertIn(b"[1] D-param  STALE  parameter", text)
        self.assertNotIn(b"[2]", text)
        self.assertIn(b"shown 1 of 4; --limit 0 lists all", text)
        self.assertIn(b"(a mechanical order, not a judgement)", text)
        # text: short commands and one export line; --json: the options as given
        self.assertNotIn(b"--store", text.replace(b"omit --store/--ledger", b""))
        self.assertIn(f"env           export TXRAY_STORE={self.store.root} "
                      f"TXRAY_FINDINGS={self.memory.ledger.directory}".encode(), text)
        self.assertIn(f"--store {self.store.root}".encode(), first[1])
        self.assertEqual(data["shell"]["export"],
                         f"export TXRAY_STORE={self.store.root} "
                         f"TXRAY_FINDINGS={self.memory.ledger.directory}")

    def test_short_commands_when_the_paths_are_the_defaults(self) -> None:
        store, ledger = str(self.store.root), str(self.memory.ledger.directory)
        env = {"TXRAY_STORE": store, "TXRAY_FINDINGS": ledger}
        for argv in (["findings", "stale"], ["findings", "stale", "--store", store, "--ledger",
                                             ledger]):
            with self.subTest(argv=argv):
                code, text, _ = run_cli(argv, env=env)
                self.assertEqual(code, 0)
                self.assertNotIn(b"--store", text)
                self.assertNotIn(b"--ledger", text)
                self.assertNotIn(b"export ", text)
                self.assertIn(b"    as is      txray findings review D-param --actor", text)
        # the ledger default <store>/findings needs neither option nor variable
        code, out, _ = run_cli(["findings", "stale", "--json", "--store", store, "--ledger",
                                ledger], env=env)
        data = json.loads(out)["data"]
        self.assertIsNone(data["shell"]["export"])
        self.assertIn(f"--store {store}", data["batch"][0]["command"]
                      if data["batch"] else data["entries"][0]["commands"]["decide"]["retract"])
        # only the store set through the environment: --ledger stays, as an export
        code, text, _ = run_cli(["findings", "stale", "--ledger", ledger],
                                env={"TXRAY_STORE": store, "TXRAY_FINDINGS": ""})
        self.assertEqual(code, 0)
        self.assertIn(f"env           export TXRAY_FINDINGS={ledger}   (run this".encode(), text)
        self.assertNotIn(b"TXRAY_STORE=", text)

    def test_one_line_hints_leave_out_default_locations(self) -> None:
        store, ledger = str(self.store.root), str(self.memory.ledger.directory)
        code, out, _ = run_cli(["findings", "reanchor", FIX.new[:12], "--summary", "--store",
                                store, "--ledger", ledger],
                               env={"TXRAY_STORE": store, "TXRAY_FINDINGS": ledger})
        self.assertEqual(code, 0)
        self.assertIn(b"next       txray findings stale --target " + FIX.new[:12].encode()
                      + b"   (", out)
        self.assertNotIn(b"--store", out)
        code, out, _ = run_cli(["findings", "reanchor", FIX.new[:12], "--summary", "--store",
                                store, "--ledger", ledger], env={"TXRAY_FINDINGS": ""})
        self.assertIn(f"--store {store} --ledger {ledger}".encode(), out)

    def test_reanchor_summary_replaces_the_per_finding_lines(self) -> None:
        args = ["--store", str(self.store.root), "--ledger", str(self.memory.ledger.directory)]
        code, out, err = run_cli(["findings", "reanchor", FIX.new[:12], "--summary", *args])
        self.assertEqual((code, err), (0, b""))
        self.assertNotIn(b"STALE         changed 1", out)
        self.assertIn(b"review     4 finding(s) to re-review at " + FIX.new[:12].encode(), out)
        self.assertIn(b"    1. D-param", out)
        self.assertIn(b"next       txray findings stale --target " + FIX.new[:12].encode(), out)
        code, out, _ = run_cli(["findings", "reanchor", FIX.new[:12], *args])
        self.assertIn(b"STALE         changed 1", out)  # the full listing is the default
        self.assertIn(b"next       txray findings stale", out)
        code, out, _ = run_cli(["findings", "reanchor", FIX.new[:12], "--json", *args])
        summary = json.loads(out)["data"]["stale_review"]
        self.assertEqual(summary["counts"]["not_current"], 4)
        self.assertEqual([item["finding_id"] for item in summary["first"]],
                         ["D-param", "C-score", "B-age", "A-dup"])


class DraftTest(unittest.TestCase):
    """A drafted successor is a file: refused until read and edited, then a normal draft
    finding that only a different reviewer can review."""

    def test_the_draft_goes_through_supersede_verify_and_review(self) -> None:
        store = FIX.store(FIX.base, FIX.new)
        memory = FIX.memory(store)
        memory.add(spec("D-param", "PARAM_DEFAULT", FIX.cite("home/param.rs", "2", "MIX_WEIGHT")),
                   AUTHOR)
        memory.verify()
        memory.reanchor(FIX.new)
        work = worklist(memory, FIX.new)
        [entry] = work.entries
        draft = work.draft_successor(entry)
        assert draft is not None
        self.assertEqual(draft[DRAFT_KEY][:30], "Read every cited span at the t")
        self.assertEqual(draft["claim"], "Synthetic claim D-param.")  # copied unchanged
        self.assertIn("claim was copied unchanged", draft["limitations"][-1])
        self.assertEqual(draft["citations"], [{
            "commit": FIX.new, "path": "home/param.rs", "lines": "2-2", "anchor": "MIX_WEIGHT",
            "span_sha256": line_sha(NEW["home/param.rs"], 2)}])
        with self.assertRaises(InvalidInput):  # an unread draft is refused by the write gate
            memory.supersede("D-param", AUTHOR, spec=draft, rationale="aligned")
        edited = {key: value for key, value in draft.items() if key != DRAFT_KEY}
        edited["claim"] = "The synthetic MIX_WEIGHT public default is 0.3."
        state = memory.supersede("D-param", AUTHOR, spec=edited, rationale="aligned at new")
        successor = f"D-param.{FIX.new[:7]}"
        self.assertEqual((state.workflow, state.superseded_by), ("superseded", successor))
        new = memory.get(successor)
        self.assertEqual((new.workflow, new.status_basis), ("draft", "proposed"))
        self.assertEqual(new.record["sources"][0]["commit"], FIX.new)
        memory.verify([successor])
        with self.assertRaises(Exception):  # no self-approval
            memory.review(successor, Actor("author-a", "reviewer"), "SUPPORTED", "mine")
        reviewed = memory.review(successor, REVIEWER, "SUPPORTED", "Read the new span.")
        self.assertEqual((reviewed.status, reviewed.status_basis), ("SUPPORTED", "reviewed"))
        self.assertEqual(worklist(memory, FIX.new).entries, [])

    def test_a_spec_dir_inside_a_git_working_tree_is_refused(self) -> None:
        store = FIX.store(FIX.base, FIX.new)
        memory = FIX.memory(store)
        memory.add(spec("D-param", "PARAM_DEFAULT", FIX.cite("home/param.rs", "2", "MIX_WEIGHT")),
                   AUTHOR)
        memory.reanchor(FIX.new)
        with tempfile.TemporaryDirectory(prefix="txray-tree-") as tmp:
            tree = Path(tmp)
            (tree / ".git").mkdir()
            (tree / "specs").mkdir()
            code, _, err = run_cli(["findings", "stale", "--store", str(store.root), "--ledger",
                                    str(memory.ledger.directory), "--spec-dir",
                                    str(tree / "specs")])
            self.assertEqual(code, 2)
            self.assertIn(b"inside the git working tree", err)
            self.assertEqual(list((tree / "specs").iterdir()), [])


class PinCitedTest(unittest.TestCase):
    """An import that cites a commit the store has not pinned: the stale list batches it and
    one command (``verify --pin-cited``) pins, re-verifies and re-anchors it."""

    def test_one_command_resolves_unpinned_citations(self) -> None:
        store = FIX.store(FIX.new)  # a fresh store that pinned only the newer commit
        memory = FIX.memory(store)
        finding = {
            "id": "R-1", "title": "Mix weight", "claim": "Synthetic.", "component": "home",
            "evidence_class": "PARAM_DEFAULT", "status": "SUPPORTED",
            "sources": [{"kind": "code", "path": "home/param.rs", "lines": "2",
                         "commit": FIX.base, "anchor": "MIX_WEIGHT"}],
            "creator_relevance": "low", "creator_controllable": "no", "implication": "",
            "volatility": "param", "misuse_risk": "low", "note": "",
        }
        source = FIX.root / f"import-{FIX.count}.json"
        source.write_text(json.dumps({"findings": [finding]}), "utf-8")
        args = ["--store", str(store.root), "--ledger", str(memory.ledger.directory)]
        code, out, err = run_cli(["findings", "import", str(source), "--source-label", "R",
                                  "--actor", "importer-1", *args])
        self.assertEqual((code, err), (0, b""))
        self.assertIn(b"to resolve them in one step: txray findings verify --pin-cited --label R",
                      out)
        self.assertNotIn(b"txray pin ", out)  # `txray pin` takes one commit only
        memory.reanchor(FIX.new)
        work = worklist(memory, FIX.new)
        self.assertEqual(work.entries, [])  # only unpinned: batched, not listed
        [step] = work.batch()
        self.assertEqual((step["findings"], step["commits"]), (["R:R-1"], [FIX.base]))
        self.assertTrue(step["command"].startswith("txray findings verify --pin-cited"))

        code, out, err = run_cli(["findings", "verify", "--pin-cited", "--upstream", FIX.url,
                                  *args], env={ENV_ALLOW_FILE_URLS: FIX.url})
        self.assertEqual((code, err), (0, b""))
        self.assertIn(b"pinned     1 cited commit(s) (1 new, 0 fetched", out)
        self.assertIn(b"R:R-1                    CURRENT       spans INTACT  (1 citation(s) "
                      b"resolved now)", out)
        self.assertIn(b"reanchored 1 finding(s) again at " + FIX.new[:12].encode(), out)
        self.assertIn(b"next       txray findings stale --target " + FIX.new[:12].encode(), out)
        self.assertIn(FIX.base, [pin.commit for pin in store.list_pins()])
        state = memory.get("R:R-1")
        self.assertEqual(state.freshness(), ("STALE", FIX.new))  # the literal changed
        self.assertEqual((state.status, state.status_basis), ("SUPPORTED", "reported"))
        work = worklist(memory, FIX.new)
        self.assertEqual([e["finding_id"] for e in work.entries], ["R:R-1"])
        self.assertEqual(work.batch(), [])
        # a second run has nothing left to pin: a plain verify of the selection
        code, out, _ = run_cli(["findings", "verify", "--pin-cited", "--upstream", FIX.url,
                                *args], env={ENV_ALLOW_FILE_URLS: FIX.url})
        self.assertEqual(code, 0)
        self.assertIn(b"pinned     0 cited commit(s)", out)
        self.assertNotIn(b"--pin-cited", out)

    def test_plain_verify_names_the_one_command_and_upstream_needs_pin_cited(self) -> None:
        store = FIX.store(FIX.new)
        memory = FIX.memory(store)
        finding = {
            "id": "R-2", "title": "Cap", "claim": "Synthetic.", "component": "home",
            "evidence_class": "PARAM_DEFAULT", "status": "SUPPORTED",
            "sources": [{"kind": "code", "path": "home/param.rs", "lines": "3",
                         "commit": FIX.base[:12], "anchor": "CAP"}],
            "creator_relevance": "low", "creator_controllable": "no", "implication": "",
            "volatility": "param", "misuse_risk": "low", "note": "",
        }
        source = FIX.root / f"import-{FIX.count}.json"
        source.write_text(json.dumps({"findings": [finding]}), "utf-8")
        memory.import_file(source, "R", Actor("importer-1", "importer"))
        args = ["--store", str(store.root), "--ledger", str(memory.ledger.directory)]
        code, out, _ = run_cli(["findings", "verify", "--label", "R", *args])
        self.assertEqual(code, 0)
        self.assertIn(b"unpinned   1 cited commit(s) are not pinned in this store", out)
        self.assertIn(b"next       txray findings verify --pin-cited --label R", out)
        code, _, err = run_cli(["findings", "verify", "--upstream", FIX.url, *args])
        self.assertEqual(code, 2)
        self.assertIn(b"--upstream is used only with --pin-cited", err)


class LocationsTest(unittest.TestCase):
    """Which --store/--ledger options a printed command needs, and the export line."""

    def test_defaults_need_nothing(self) -> None:
        from timelinexray.findings.stale import locations
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            env = {"XDG_CACHE_HOME": str(cache)}
            default = cache / "timelinexray"
            where = locations(str(default), str(default / "findings"), env)
            self.assertEqual((where.store_args, where.ledger_args, where.export_line),
                             ([], [], None))
            other = Path(tmp) / "other store"
            where = locations(str(other), None, env)
            self.assertEqual(where.store_args, ["--store", str(other)])
            self.assertEqual(where.ledger_args, [])  # <other>/findings is its default
            self.assertEqual(where.export_line, f"export TXRAY_STORE='{other}'")
            where = locations(None, str(other / "findings"), env)
            self.assertEqual(where.export_line, f"export TXRAY_FINDINGS='{other / 'findings'}'")

    def test_a_default_ledger_inside_a_working_tree_is_never_assumed(self) -> None:
        from timelinexray.findings.stale import locations
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "tree"
            (tree / ".git").mkdir(parents=True)
            where = locations(str(tree / "store"), str(Path(tmp) / "ledger"), {})
            self.assertEqual(where.ledger_args, ["--ledger", str(Path(tmp) / "ledger")])
            self.assertIn("TXRAY_FINDINGS=", where.export_line or "")


if __name__ == "__main__":
    unittest.main()
