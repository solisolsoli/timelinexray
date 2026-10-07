"""Digests over a synthetic range: intermediate history, reversions, merges, providers."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from timelinexray.digest import (
    STATEMENTS,
    AffectedFinding,
    AffectedFindingsProvider,
    ChangedRegion,
    DigestBuilder,
    NullFindingsProvider,
    default_findings_provider,
    digest_json,
    render_appendix,
    render_markdown,
)
from timelinexray.digest.render import digest_names
from timelinexray.errors import NetworkRefused
from timelinexray.mcp.guard import ANALYTICS_DATASET_FILE, ANALYTICS_DATASET_FORMAT
from timelinexray.netguard import ENV_ALLOW_FILE_URLS, Allowlist
from timelinexray.snapshot import SnapshotStore
from tests.diff_support import CLASSES_NEW, CLASSES_OLD, MISLEADING_MESSAGE, linear_history, range_history
from tests.support import REPO_ROOT, file_url, run_cli

TMP: tempfile.TemporaryDirectory
ROOT: Path
GIT_DIR: Path
COMMITS: list[str]
SIDE: str
URL: str


def setUpModule() -> None:
    global TMP, ROOT, GIT_DIR, COMMITS, SIDE, URL
    TMP = tempfile.TemporaryDirectory(prefix="txray-digest-")
    ROOT = Path(TMP.name)
    GIT_DIR, COMMITS, SIDE = range_history(ROOT)
    URL = file_url(GIT_DIR)


def tearDownModule() -> None:
    TMP.cleanup()


def _store(name: str, pins: Sequence[str]) -> SnapshotStore:
    store = SnapshotStore(ROOT / name)
    for commit in pins:
        store.pin(commit, URL, allowlist=Allowlist([URL]))
    return store


class _Provider:
    """A read-only provider that reports one finding on the parameter file."""

    name = "fixture"

    def __init__(self) -> None:
        self.regions: list[ChangedRegion] = []

    def available(self) -> bool:
        return True

    def affected_findings(self, regions: Sequence[ChangedRegion]) -> Sequence[AffectedFinding]:
        self.regions = list(regions)
        hits = [r for r in regions if r.old_path == "params/param.rs" and r.old_lines]
        return [AffectedFinding("F-001", r.old_commit, "params/param.rs", r.old_lines[0],
                                r.old_lines[1], None, "cited span overlaps a changed region",
                                (r.item_id,), "SUPPORTED", "CURRENT") for r in hits[:1]]


class RangeDigestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.store = _store("store", [COMMITS[0], COMMITS[-1]])
        cls.doc = DigestBuilder(cls.store, allowlist=Allowlist([URL])).build(COMMITS[0], COMMITS[-1])
        cls.md = render_markdown(cls.doc)

    def test_range_lineage_and_local_pins(self) -> None:
        rng = self.doc["range"]
        self.assertEqual(rng["relationship"], "ancestor")
        self.assertEqual(rng["first_parent_chain"], COMMITS)
        self.assertEqual(rng["reached_through_merges"], [SIDE])
        self.assertEqual(rng["commits_in_range"], 5)
        self.assertTrue(rng["history_complete"])
        self.assertEqual(self.doc["summary"]["steps"], 4)
        self.assertEqual(self.doc["events"], [])
        for commit in COMMITS:  # intermediate commits were pinned locally
            self.assertEqual(self.store.get_pin(commit).commit, commit)
        self.assertEqual([s["new"] for s in self.doc["steps"]], COMMITS[1:])
        merge_step = self.doc["steps"][2]
        self.assertEqual(merge_step["items_by_class"], {"docs-only": 1})

    def test_net_parameters_and_reversions(self) -> None:
        params = self.doc["parameters"]
        [net] = params["net"]
        self.assertEqual((net["name"], net["old_value"], net["new_value"]), ("ClickWeight", "0.4", "0.3"))
        history = {row["name"]: row for row in params["history"]}
        self.assertEqual(set(history), {"ClickWeight", "EnableLegacy"})
        legacy = [(p["commit"], p["value"]) for p in history["EnableLegacy"]["points"]]
        self.assertEqual(legacy, [(COMMITS[0], "false"), (COMMITS[1], "true"), (COMMITS[2], "false")])
        [reversion] = params["reversions"]
        self.assertEqual((reversion["name"], reversion["returns"]),
                         ("EnableLegacy", [{"from_point": 0, "back_at_point": 2}]))
        for point in history["EnableLegacy"]["points"]:
            c = point["citation"]
            span = self.store.read_span(c["commit"], c["path"], c["start_line"], c["end_line"],
                                        anchor=point["value"])
            self.assertEqual((span.sha256, span.anchor.verdict), (c["span_sha256"], "FOUND"))

    def test_registration_reversions(self) -> None:
        regs = self.doc["registrations"]
        self.assertEqual(regs["net"], [])
        self.assertEqual([row["added"] or row["removed"] for row in regs["history"]],
                         [["SpamFilter"], ["SpamFilter"]])
        [reversion] = regs["reversions"]
        self.assertEqual(reversion["entry"], "SpamFilter")
        self.assertEqual(reversion["events"], [{"commit": COMMITS[1], "event": "added"},
                                               {"commit": COMMITS[2], "event": "removed"}])

    def test_markdown_statements_and_sections(self) -> None:
        md = self.md
        for statement in STATEMENTS:
            self.assertIn(statement, md)
        self.assertIn("public default `0.4` → `0.3`", md)
        self.assertIn("### Intermediate reversions", md)
        self.assertIn("`EnableLegacy` | `params/param.rs` | `false` (", md)
        self.assertIn("### Registration reversions", md)
        self.assertIn(NullFindingsProvider.note, md)
        self.assertIn("## Unresolved and unknown changes", md)
        self.assertIn("## Intermediate history", md)
        self.assertIn("reached only through merges", md)
        for text in (md, digest_json(self.doc)):
            self.assertNotIn(MISLEADING_MESSAGE, text)
            self.assertNotIn("12345.678", text)
            self.assertNotIn(str(ROOT), text)
            self.assertNotIn("file:///", text)
        self.assertFalse(self.doc["affected_findings"]["available"])
        self.assertEqual(self.doc["affected_findings"]["findings"], [])

    def test_deterministic(self) -> None:
        again = DigestBuilder(self.store).build(COMMITS[0], COMMITS[-1])
        self.assertEqual(digest_json(again), digest_json(self.doc))
        self.assertEqual(render_markdown(again), self.md)
        other = _store("store-deterministic", [COMMITS[0], COMMITS[-1]])
        third = DigestBuilder(other, allowlist=Allowlist([URL])).build(COMMITS[0][:9], COMMITS[-1][:9])
        self.assertEqual(digest_json(third), digest_json(self.doc))

    def test_provider_interface(self) -> None:
        self.assertIsInstance(NullFindingsProvider(), AffectedFindingsProvider)
        default = default_findings_provider(self.store, ROOT / "no-ledger-here")
        self.assertEqual((default.name, default.available()), ("ledger", False))
        provider = _Provider()
        self.assertIsInstance(provider, AffectedFindingsProvider)
        doc = DigestBuilder(self.store, findings=provider).build(COMMITS[0], COMMITS[-1])
        section = doc["affected_findings"]
        self.assertEqual((section["provider"], section["available"], section["note"]),
                         ("fixture", True, None))
        [finding] = section["findings"]
        self.assertEqual(finding["finding_id"], "F-001")
        ids = {item["id"] for item in doc["items"]}
        self.assertTrue(set(finding["item_ids"]) <= ids)
        self.assertEqual(len(provider.regions), len(doc["items"]))
        self.assertIn("`F-001`", render_markdown(doc))
        self.assertNotEqual(doc["inputs_sha256"], self.doc["inputs_sha256"])


class DigestLayoutTest(unittest.TestCase):
    """The main digest is summary-first and bounded; main + appendix list every item; the
    JSON overview agrees with the items (synthetic CLASSES fixture: every class occurs)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="txray-digest-layout-")
        root = Path(cls.tmp.name)
        git_dir, commits = linear_history(root, [CLASSES_OLD, CLASSES_NEW])
        url = file_url(git_dir)
        store = SnapshotStore(root / "store")
        for commit in commits:
            store.pin(commit, url, allowlist=Allowlist([url]))
        cls.doc = DigestBuilder(store, allowlist=Allowlist([url])).build(*commits)
        cls.md = render_markdown(cls.doc)
        cls.appendix = render_appendix(cls.doc)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_summary_first(self) -> None:
        headings = [line for line in self.md.splitlines() if line.startswith("## ")]
        self.assertEqual(headings[:4], ["## At a glance", "## What to check next", "## Statements",
                                        "## Parameter default changes"])
        self.assertLess(self.md.index("## Weight/scoring logic change"), self.md.index("## Summary by class"))
        for statement in STATEMENTS:
            self.assertIn(statement, self.md)
            self.assertIn(statement, self.appendix)
        main, appendix, data = digest_names(self.doc)
        self.assertIn(f"`{appendix}`", self.md)
        self.assertIn(f"`{data}`", self.md)
        self.assertIn(f"`{main}`", self.appendix)

    def test_every_item_is_cited_in_the_main_file_or_the_appendix(self) -> None:
        tables = {"parameter-default", "registration"}
        for item in self.doc["items"]:
            with self.subTest(item=item["id"], cls=item["class"]):
                where = self.md if item["class"] == "scoring-logic" else self.appendix
                if item["class"] in tables:
                    where = self.md
                for side in ("old", "new"):
                    sha = item[side].get("span_sha256")
                    if sha:
                        self.assertIn(sha, where)
                if item["class"] == "unknown":
                    self.assertIn(f"reason `{item['detail']['unknown_reason']}`", self.appendix)
        # the main file lists the logic classes; the appendix does not repeat them
        self.assertNotIn("## Weight/scoring logic change", self.appendix)
        self.assertIn("## Unknown / unresolved change (`unknown`)", self.appendix)
        self.assertIn("## Unresolved and unknown changes", self.md)

    def test_overview_agrees_with_the_items(self) -> None:
        rows = {row["class"]: row for row in self.doc["overview"]["classes"]}
        net = self.doc["summary"]["net"]["items_by_class"]
        for name, row in rows.items():
            with self.subTest(name):
                self.assertEqual(row["items"], net[name])
                self.assertEqual(sum(area["items"] for area in row["areas"]), row["items"])
                self.assertEqual(row["listed_in"], "main" if name in (
                    "parameter-default", "registration", "scoring-logic") else "appendix")
        self.assertEqual(rows["scoring-logic"]["decided_by"], {"path": 1})
        self.assertEqual(sum(rows["unknown"]["reasons"].values()), rows["unknown"]["items"])
        for entry in self.doc["classes"]:
            self.assertTrue(entry["must_not_be_read_as"])

    def test_row_budgets_bound_the_main_file_and_move_rows_to_the_appendix(self) -> None:
        doc = copy.deepcopy(self.doc)
        [row] = [r for r in doc["parameters"]["net"] if r["name"] == "ClickWeight"]
        doc["parameters"]["net"] = [dict(row, name=f"Weight{n:03d}") for n in range(200)]
        scoring = [item for item in doc["items"] if item["class"] == "scoring-logic"][0]
        doc["items"] += [dict(scoring, new_path=f"scorers/s{n:03d}.rs", id=f"i-{n:016x}")
                         for n in range(300)]
        doc.pop("overview")  # recomputed from the items by the renderer
        md, appendix = render_markdown(doc), render_appendix(doc)
        self.assertIn("140 more parameter default changes are in", md)
        self.assertIn("Cut from this file for size (all in the appendix): 140 of 200 parameter "
                      "default rows", md)
        self.assertNotIn("`Weight060`", md)
        self.assertIn("`Weight199`", appendix)
        self.assertIn("## Parameter default changes, continued", appendix)
        self.assertIn("counted by file here", md)
        self.assertIn("`scorers/s299.rs`", appendix)
        self.assertLess(len(md.encode()), 60_000)
        self.assertEqual(render_markdown(doc), md)  # deterministic

    def test_compact_citations_keep_commit_lines_and_hash(self) -> None:
        [scoring] = [item for item in self.doc["items"] if item["class"] == "scoring-logic"]
        old, new = scoring["old"], scoring["new"]
        self.assertIn(f"L{old['start_line']} `{old['span_sha256']}`", self.md)
        self.assertIn(f"(at `{old['commit'][:12]}`)", self.md)
        self.assertIn(f"(at `{new['commit'][:12]}`)", self.md)
        self.assertIn("A citation reads `commit` L<first>-L<last> `span SHA-256`", self.md)


class OtherRangesTest(unittest.TestCase):
    def test_reversed_range_is_flagged_and_tree_only(self) -> None:
        store = _store("store-reversed", [COMMITS[0], COMMITS[-1]])
        doc = DigestBuilder(store).build(COMMITS[-1], COMMITS[0])
        self.assertEqual(doc["range"]["relationship"], "reversed")
        self.assertFalse(doc["range"]["history_complete"])
        self.assertEqual([e["kind"] for e in doc["events"]], ["not-ancestor"])
        self.assertEqual(doc["summary"]["steps"], 0)
        self.assertIn("`not-ancestor`", render_markdown(doc))

    def test_side_commit_range_is_not_linear(self) -> None:
        store = _store("store-side", [SIDE, COMMITS[2]])
        doc = DigestBuilder(store).build(SIDE, COMMITS[2])
        self.assertEqual(doc["range"]["relationship"], "diverged")
        self.assertEqual(doc["events"][0]["kind"], "not-ancestor")

    def test_same_commit(self) -> None:
        store = _store("store-same", [COMMITS[1]])
        doc = DigestBuilder(store).build(COMMITS[1], COMMITS[1])
        self.assertEqual((doc["range"]["relationship"], doc["summary"]["net"]["items"]), ("same", 0))

    def test_local_pinning_needs_the_allowlist(self) -> None:
        store = _store("store-refused", [COMMITS[0], COMMITS[-1]])
        with self.assertRaises(NetworkRefused):
            DigestBuilder(store, allowlist=Allowlist()).build(COMMITS[0], COMMITS[-1])


class DigestCommandTest(unittest.TestCase):
    def test_stdout_out_dir_and_json(self) -> None:
        store = _store("store-cli", [COMMITS[0], COMMITS[-1]])
        args = ["--store", str(store.root)]
        env = {ENV_ALLOW_FILE_URLS: URL}
        code, out, err = run_cli(["digest", COMMITS[0][:8], COMMITS[-1][:8], *args], env)
        self.assertEqual((code, err), (0, b""))
        self.assertTrue(out.decode().startswith(f"# Change digest {COMMITS[0][:12]}..{COMMITS[-1][:12]}"))
        out_dir = ROOT / "digests"
        code, out, _ = run_cli(["digest", COMMITS[0], COMMITS[-1], "--out", str(out_dir),
                                "--format", "json", *args], env)
        self.assertEqual(code, 0)
        written = out_dir / f"digest-{COMMITS[0][:12]}-{COMMITS[-1][:12]}.json"
        doc = json.loads(written.read_text("utf-8"))
        self.assertEqual(doc["schema"], "timelinexray/digest/v1")
        self.assertIn(b"parameters 1 public-default changes", out)
        code, out, _ = run_cli(["digest", COMMITS[0], COMMITS[-1], "--out", str(out_dir),
                                "--json", *args], env)
        envelope = json.loads(out)
        self.assertEqual((code, envelope["command"], envelope["data"]["written"]),
                         (0, "digest", [written.name.replace(".json", ".md"),
                                        written.name.replace(".json", "-appendix.md")]))
        self.assertTrue((out_dir / written.name.replace(".json", ".md")).is_file())
        appendix = out_dir / written.name.replace(".json", "-appendix.md")
        code, out, _ = run_cli(["digest", COMMITS[0], COMMITS[-1], "--format", "appendix", *args], env)
        self.assertEqual((code, out), (0, appendix.read_bytes()))
        code, _, err = run_cli(["digest", COMMITS[0], "0" * 40, *args], env)
        self.assertEqual(code, 1)

    def test_out_is_checked_first_and_written_atomically(self) -> None:
        """FA-022: the destination rules of ``txray export`` and an atomic replace."""
        store = _store("store-out", [COMMITS[0], COMMITS[-1]])
        args = ["digest", COMMITS[0], COMMITS[-1], "--store", str(store.root)]
        env = {ENV_ALLOW_FILE_URLS: URL}
        ledger = ROOT / "ledger-out"
        ledger.mkdir()
        real = ROOT / "real-out"
        real.mkdir()
        link = ROOT / "link-out"
        link.symlink_to(real, target_is_directory=True)
        dataset = ROOT / "private-out"
        dataset.mkdir()
        (dataset / ANALYTICS_DATASET_FILE).write_text(
            json.dumps({"format": ANALYTICS_DATASET_FORMAT}), "utf-8")
        afile = ROOT / "a-file-out"
        afile.write_text("x", "utf-8")
        refused = [
            (link, b"is a symbolic link", []),
            (store.root / "digests", b"inside the snapshot store", []),
            (ledger / "digests", b"inside the findings ledger", ["--ledger", str(ledger)]),
            (REPO_ROOT / "digest-probe", b"inside the TimelineXray working tree", []),
            (ROOT / "missing" / "digests", b"does not exist", []),
            (dataset / "digests", b"lies inside an analytics dataset", []),
            (afile, b"is not a directory", []),
        ]
        for out, message, extra in refused:
            with self.subTest(str(out.relative_to(ROOT) if ROOT in out.parents else out)):
                existed = out.exists()
                with mock.patch.object(DigestBuilder, "build",
                                       side_effect=AssertionError("built before the check")):
                    code, _, err = run_cli([*args, "--out", str(out), *extra], env)
                self.assertEqual(code, 1, err)
                self.assertIn(message, err)
                self.assertEqual(out.exists(), existed)
        self.assertEqual(list(real.iterdir()), [])
        self.assertFalse((REPO_ROOT / "digest-probe").exists())
        # a symbolic link where the digest would be is replaced, never followed
        out = ROOT / "digests-atomic"
        out.mkdir()
        elsewhere = ROOT / "elsewhere.md"
        elsewhere.write_text("# keep\n", "utf-8")
        name = f"digest-{COMMITS[0][:12]}-{COMMITS[-1][:12]}.md"
        (out / name).symlink_to(elsewhere)
        code, _, err = run_cli([*args, "--out", str(out)], env)
        self.assertEqual((code, err), (0, b""))
        self.assertEqual(elsewhere.read_text("utf-8"), "# keep\n")
        self.assertFalse((out / name).is_symlink())
        self.assertTrue((out / name).read_text("utf-8").startswith("# Change digest"))
        # no temporary file left; the main digest brings its appendix
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         sorted([name, name.replace(".md", "-appendix.md")]))


if __name__ == "__main__":
    unittest.main()
