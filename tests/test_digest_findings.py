"""The digest's affected-findings section, wired to the findings ledger (read-only).

Synthetic range ``c0 -> c1 -> c2 -> m -> c3`` (see ``tests/diff_support.py``): the net diff
changes ``ClickWeight`` (line 3 of ``params/param.rs``) and line 2 of ``src/lib.rs``;
``EnableLegacy`` changes and changes back. Findings cite changed and unchanged lines at the
old commit, the new commit and intermediate commits, directly and as span dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timelinexray.digest import DigestBuilder, digest_json, render_markdown
from timelinexray.digest.findings import (
    AffectedFindingsProvider,
    ChangedRegion,
    LedgerFindingsProvider,
    _touches,
    default_findings_provider,
)
from timelinexray.findings import Actor, FindingsMemory, Ledger
from timelinexray.netguard import ENV_ALLOW_FILE_URLS, Allowlist
from timelinexray.snapshot import SnapshotStore
from timelinexray.verify import Verifier
from tests.diff_support import range_history
from tests.index_support import add_commit
from tests.support import build_fixture_repo, file_url, git, run_cli

AUTHOR = Actor("agent-a", "author")
REVIEWER = Actor("reviewer-b", "reviewer")
PARAMS = "params/param.rs"


def _snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    """Every file of a directory with its bytes and modification time (ns)."""
    return {str(path.relative_to(directory)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(directory.rglob("*")) if path.is_file()}


def _cite(commit: str, path: str, lines: str, anchor: str) -> dict[str, str]:
    return {"commit": commit, "path": path, "lines": lines, "anchor": anchor}


def _spec(finding_id: str, citation: dict[str, str], **extra: object) -> dict[str, object]:
    return {"finding_id": finding_id, "title": f"Synthetic {finding_id}",
            "claim": "A synthetic claim about the range fixture.", "component": "fixture",
            "evidence_class": "CODE", "status": "SUPPORTED", "citations": [citation], **extra}


class LedgerProviderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-digest-findings-")
        cls.root = Path(cls._tmp.name)
        git_dir, cls.commits, _ = range_history(cls.root)
        cls.url = file_url(git_dir)
        cls.allowlist = Allowlist([cls.url])
        cls.store = SnapshotStore(cls.root / "store")
        for commit in cls.commits:
            cls.store.pin(commit, cls.url, allowlist=cls.allowlist)
        c0, c1, c2, _, c3 = cls.commits
        cls.ledger = cls.root / "ledger"
        memory = FindingsMemory(Ledger(cls.ledger), cls.store)
        memory.add(_spec("F-click", _cite(c0, PARAMS, "3", "ClickWeight")), AUTHOR)
        memory.add(_spec("F-legacy", _cite(c0, PARAMS, "4", "EnableLegacy")), AUTHOR)
        memory.add(_spec("F-lib-new", _cite(c3, "src/lib.rs", "1-3", "pub fn f")), AUTHOR)
        memory.add(_spec("F-dep", _cite(c0, "README.md", "1", "Range fixture"),
                         depends_on=[{"span": _cite(c0, "src/lib.rs", "2", "1")}]), AUTHOR)
        memory.add(_spec("F-mid", _cite(c1, PARAMS, "4", "true")), AUTHOR)
        memory.add(_spec("F-mid-click", _cite(c2, PARAMS, "3", "0.3")), AUTHOR)
        memory.add(_spec("F-gone", _cite(c0, PARAMS, "3", "0.4")), AUTHOR)
        memory.retract("F-gone", AUTHOR, "withdrawn")
        memory.verify(["F-click", "F-legacy"])
        memory.reanchor(c3, ["F-legacy"])
        memory.review("F-legacy", REVIEWER, "SUPPORTED", "matches the cited line")
        cls.before = _snapshot(cls.ledger)
        cls.provider = LedgerFindingsProvider(cls.store, cls.ledger)
        cls.doc = DigestBuilder(cls.store, allowlist=cls.allowlist,
                                findings=cls.provider).build(c0, c3)
        cls.section = cls.doc["affected_findings"]
        cls.rows = {row["finding_id"]: row for row in cls.section["findings"]}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_it_is_a_provider(self) -> None:
        self.assertIsInstance(self.provider, AffectedFindingsProvider)
        self.assertEqual((self.section["provider"], self.section["available"], self.section["note"]),
                         ("ledger", True, None))

    def test_changed_old_line_is_affected_with_separate_status_and_freshness(self) -> None:
        row = self.rows["F-click"]
        c0 = self.commits[0]
        self.assertEqual((row["citation"]["commit"], row["citation"]["path"],
                          row["citation"]["start_line"], row["via"]), (c0, PARAMS, 3, "citation"))
        self.assertEqual((row["status"], row["status_basis"], row["workflow"]),
                         ("SUPPORTED", "proposed", "draft"))
        self.assertEqual((row["freshness"], row["freshness_checked_against"]), ("CURRENT", "cited"))
        items = {item["id"]: item for item in self.doc["items"]}
        self.assertEqual([items[i]["class"] for i in row["item_ids"]], ["parameter-default"])
        self.assertIn("old side", row["reason"])
        self.assertIn("changed lines 3-3 touch cited lines 3-3", row["reason"])

    def test_new_side_and_span_dependency(self) -> None:
        self.assertIn("new side", self.rows["F-lib-new"]["reason"])
        self.assertEqual(self.rows["F-lib-new"]["freshness"], "NOT_CHECKED")
        dep = self.rows["F-dep"]
        self.assertEqual((dep["via"], dep["citation"]["path"], dep["citation"]["start_line"]),
                         ("span dependency", "src/lib.rs", 2))

    def test_citation_at_an_intermediate_commit_is_placed_by_exact_bytes(self) -> None:
        row = self.rows["F-mid-click"]
        self.assertIn("new side", row["reason"])
        self.assertIn("placed here by its exact span bytes: identical", row["reason"])

    def test_unaffected_inactive_and_unplaceable_findings(self) -> None:
        self.assertNotIn("F-legacy", self.rows)  # placed on both sides, untouched
        self.assertNotIn("F-gone", self.rows)  # retracted: not active
        self.assertNotIn("F-mid", self.rows)
        coverage = self.section["coverage"]
        self.assertEqual((coverage["active_findings"], coverage["spans"], coverage["unplaced"]),
                         (6, 7, 1))
        [unplaced] = coverage["unplaced_spans"]
        self.assertEqual((unplaced["finding_id"], unplaced["old"], unplaced["new"]),
                         ("F-mid", "changed", "changed"))
        self.assertEqual(coverage["affected_findings"], 4)

    def test_markdown_and_inputs(self) -> None:
        md = render_markdown(self.doc)
        self.assertIn("| `F-click` | citation |", md)
        self.assertIn("SUPPORTED (proposed)", md)
        self.assertIn("Checked 6 active findings with 7 cited or dependency spans", md)
        self.assertNotIn(str(self.root), md + digest_json(self.doc))
        again = DigestBuilder(self.store, findings=LedgerFindingsProvider(
            self.store, self.ledger)).build(self.commits[0], self.commits[-1])
        self.assertEqual(digest_json(again), digest_json(self.doc))
        null = DigestBuilder(self.store).build(self.commits[0], self.commits[-1])
        self.assertNotEqual(null["inputs_sha256"], self.doc["inputs_sha256"])

    def test_the_ledger_is_never_written(self) -> None:
        self.assertEqual(_snapshot(self.ledger), self.before)
        self.assertFalse((self.ledger / ".lock").exists() and ".lock" not in self.before)

    def test_touch_rules(self) -> None:
        self.assertEqual(_touches((10, 15), None, ((12, 2),)), ["changed lines 12-13"])
        self.assertEqual(_touches((10, 15), None, ((16, 3),)), [])
        self.assertEqual(_touches((10, 15), None, ((12, 0),)),
                         ["lines inserted or removed after line 12"])
        self.assertEqual(_touches((10, 15), None, ((15, 0), (9, 0))), [])  # at the edges
        self.assertEqual(_touches((10, 15), (14, 20), ()), ["changed lines 14-20"])


class LedgerProviderStatesTest(unittest.TestCase):
    """Missing, damaged, unset and changed ledgers; the CLI option."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-digest-findings-states-")
        cls.root = Path(cls._tmp.name)
        git_dir, cls.commits, _ = range_history(cls.root)
        cls.url = file_url(git_dir)
        cls.store = SnapshotStore(cls.root / "store")
        for commit in cls.commits:
            cls.store.pin(commit, cls.url, allowlist=Allowlist([cls.url]))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def build(self, provider: object) -> dict:
        return DigestBuilder(self.store, findings=provider).build(  # type: ignore[arg-type]
            self.commits[0], self.commits[-1])

    def test_missing_ledger_is_not_computed(self) -> None:
        provider = default_findings_provider(self.store, self.root / "no-ledger")
        section = self.build(provider)["affected_findings"]
        self.assertFalse(section["available"])
        self.assertIn("no findings ledger exists", section["note"])
        self.assertIn("not evidence that no finding is affected", section["note"])
        self.assertNotIn(str(self.root), section["note"])
        self.assertFalse((self.root / "no-ledger").exists())

    def test_default_location_and_environment(self) -> None:
        provider = default_findings_provider(self.store, environ={})
        self.assertEqual(provider.directory, self.store.root / "findings")
        provider = default_findings_provider(self.store, environ={"TXRAY_FINDINGS": "/x/y"})
        self.assertEqual(provider.directory, Path("/x/y"))
        worktree = self.root / "worktree"
        (worktree / ".git").mkdir(parents=True, exist_ok=True)
        inside = default_findings_provider(SnapshotStore(worktree / "store"), environ={})
        self.assertFalse(inside.available())
        self.assertIn("inside a git working tree", inside.note)

    def test_damaged_ledger_is_not_computed(self) -> None:
        ledger = self.root / "damaged"
        memory = FindingsMemory(Ledger(ledger), self.store)
        memory.add(_spec("F-x", _cite(self.commits[0], PARAMS, "3", "ClickWeight")), AUTHOR)
        events = ledger / "events.jsonl"
        events.write_bytes(events.read_bytes().replace(b"F-x", b"F-y", 1))
        section = self.build(LedgerFindingsProvider(self.store, ledger))["affected_findings"]
        self.assertFalse(section["available"])
        self.assertIn("fails verification", section["note"])
        self.assertEqual(section["findings"], [])

    def test_a_fifo_ledger_file_is_refused_without_blocking(self) -> None:
        ledger = self.root / "fifo"
        ledger.mkdir()
        os.mkfifo(ledger / "events.jsonl")
        section = self.build(LedgerFindingsProvider(self.store, ledger))["affected_findings"]
        self.assertFalse(section["available"])
        self.assertIn("is not readable", section["note"])

    def test_a_new_event_changes_the_inputs_hash(self) -> None:
        ledger = self.root / "growing"
        memory = FindingsMemory(Ledger(ledger), self.store)
        memory.add(_spec("F-a", _cite(self.commits[0], PARAMS, "3", "ClickWeight")), AUTHOR)
        first = self.build(LedgerFindingsProvider(self.store, ledger))
        memory.verify(["F-a"])
        second = self.build(LedgerFindingsProvider(self.store, ledger))
        self.assertNotEqual(first["inputs_sha256"], second["inputs_sha256"])
        self.assertEqual([r["freshness"] for r in second["affected_findings"]["findings"]],
                         ["CURRENT"])

    def test_cli_digest_uses_the_ledger(self) -> None:
        ledger = self.root / "cli-ledger"
        memory = FindingsMemory(Ledger(ledger), self.store)
        memory.add(_spec("F-cli", _cite(self.commits[0], PARAMS, "3", "ClickWeight")), AUTHOR)
        before = hashlib.sha256((ledger / "events.jsonl").read_bytes()).hexdigest()
        env = {ENV_ALLOW_FILE_URLS: self.url}
        base = ["digest", self.commits[0], self.commits[-1], "--store", str(self.store.root)]
        code, out, err = run_cli([*base, "--ledger", str(ledger), "--format", "json"], env)
        self.assertEqual((code, err), (0, b""))
        doc = json.loads(out)
        self.assertEqual([row["finding_id"] for row in doc["affected_findings"]["findings"]],
                         ["F-cli"])
        code, out, _ = run_cli([*base, "--ledger", str(self.root / "absent")], env)
        self.assertEqual(code, 0)
        self.assertIn(b"Affected findings were not computed", out)
        self.assertEqual(hashlib.sha256((ledger / "events.jsonl").read_bytes()).hexdigest(), before)


WEIGHTS = b"pub const CLICK_WEIGHT: f64 = 0.4;\npub const REPLY_WEIGHT: f64 = 5.0;\npub fn f() {}\n"
OTHER = b"pub fn other() -> u8 {\n    1\n}\n"


class MovesAndPathIndexTest(unittest.TestCase):
    """FA-004: a byte-identical move (or a mode-only change) is never "changed lines";
    FA-009: only spans whose path a changed region touches (or that is absent at a
    commit) are placed, so untouched files cost nothing."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-digest-moves-")
        cls.root = Path(cls._tmp.name)
        base_files = {"src/weights.rs": WEIGHTS, "src/other.rs": OTHER, "README.md": b"# x\n"}
        git_dir, cls.base = build_fixture_repo(cls.root / "up", base_files, frozenset())
        cls.renamed = add_commit(git_dir, {"src/params/weights.rs": WEIGHTS,
                                           "src/other.rs": OTHER, "README.md": b"# x\n"},
                                 cls.base)
        git(git_dir, "update-ref", "refs/heads/renamed", cls.renamed)  # pins fetch branches
        cls.changed = add_commit(git_dir, {**base_files, "src/other.rs": OTHER.replace(b"1", b"2")},
                                 cls.base)
        cls.mode_only = _mode_change(git_dir, cls.base, "src/weights.rs")
        cls.url = file_url(git_dir)
        cls.allowlist = Allowlist([cls.url])
        cls.store = SnapshotStore(cls.root / "store")
        for commit in (cls.base, cls.renamed, cls.changed, cls.mode_only):
            cls.store.pin(commit, cls.url, allowlist=cls.allowlist)
        cls.ledger = cls.root / "ledger"
        memory = FindingsMemory(Ledger(cls.ledger), cls.store)
        memory.add(_spec("F-weights", _cite(cls.base, "src/weights.rs", "1", "CLICK_WEIGHT")),
                   AUTHOR)
        memory.add(_spec("F-other", _cite(cls.base, "src/other.rs", "2", "1")), AUTHOR)
        memory.add(_spec("F-moved-cite", _cite(cls.renamed, "src/params/weights.rs", "2",
                                               "REPLY_WEIGHT")), AUTHOR)
        memory.verify()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def digest(self, old: str, new: str) -> dict:
        provider = LedgerFindingsProvider(self.store, self.ledger)
        return DigestBuilder(self.store, allowlist=self.allowlist, findings=provider).build(old, new)

    def test_byte_identical_rename_is_a_relocation_candidate_not_an_affected_finding(self) -> None:
        doc = self.digest(self.base, self.renamed)
        self.assertEqual([(i["class"], i["kind"]) for i in doc["items"]], [("cosmetic", "moved")])
        section = doc["affected_findings"]
        self.assertEqual(section["findings"], [])
        rows = {row["finding_id"]: row for row in section["relocation_candidates"]}
        self.assertEqual(sorted(rows), ["F-moved-cite", "F-weights"])
        reason = rows["F-weights"]["reason"]
        self.assertNotIn("changed lines", reason)
        self.assertIn("src/weights.rs moved to src/params/weights.rs with identical bytes", reason)
        self.assertIn("cited lines 1-1 are intact", reason)
        self.assertIn("placed here by its exact span bytes: relocated", reason)  # new side
        self.assertEqual((rows["F-weights"]["status"], rows["F-weights"]["freshness"]),
                         ("SUPPORTED", "CURRENT"))
        self.assertEqual(section["coverage"]["relocation_candidates"], 2)
        self.assertEqual(section["coverage"]["affected_findings"], 0)
        md = render_markdown(doc)
        self.assertIn("Relocation candidates (not affected)", md)
        self.assertIn("| Finding | Via |", md) if section["findings"] else None
        self.assertNotIn("changed lines", md.split("## Affected findings")[1].split("## ")[0])
        # the digest agrees with re-anchoring (on a copy: the digest never writes the ledger)
        copy = self.root / "ledger-copy"
        shutil.copytree(self.ledger, copy)
        report = FindingsMemory(Ledger(copy), self.store).reanchor(self.renamed, ["F-weights"])
        self.assertEqual(report["results"][0]["outcomes"], {"relocated": 1})

    def test_mode_only_change_is_not_affected_and_not_a_move(self) -> None:
        doc = self.digest(self.base, self.mode_only)
        self.assertEqual([(i["class"], i["kind"]) for i in doc["items"]], [("unknown", "mode")])
        section = doc["affected_findings"]
        self.assertEqual((section["findings"], section["relocation_candidates"]), ([], []))
        self.assertNotIn("changed lines", render_markdown(doc))

    def test_only_spans_in_touched_or_absent_paths_are_placed(self) -> None:
        placed: list[str] = []
        real = Verifier.relocate

        def counting(verifier: Verifier, citation, target: str, **kwargs):
            placed.append(citation.path)
            return real(verifier, citation, target, **kwargs)

        with mock.patch.object(Verifier, "relocate", counting):
            doc = self.digest(self.base, self.changed)
        section = doc["affected_findings"]
        self.assertEqual([row["finding_id"] for row in section["findings"]], ["F-other"])
        self.assertIn("changed lines 2-2 touch cited lines 2-2 of src/other.rs",
                      section["findings"][0]["reason"])
        coverage = section["coverage"]
        # F-weights: src/weights.rs exists at both commits and no region touches it: skipped,
        # never placed. F-moved-cite: its path is absent at both commits, so its bytes may
        # have moved into a changed file: placed (found by blob id in src/weights.rs), not
        # affected. F-other: placed on both sides ("cited" on the old side, no relocation).
        self.assertEqual((coverage["spans"], coverage["skipped"], coverage["placed"],
                          coverage["unplaced"]), (3, 1, 2, 0))
        self.assertNotIn("src/weights.rs", placed)
        self.assertEqual(sorted(set(placed)), ["src/other.rs", "src/params/weights.rs"])
        self.assertIn("1 not placed because their path exists at both commits",
                      render_markdown(doc))

    def test_regions_carry_the_content_unchanged_flag(self) -> None:
        doc = self.digest(self.base, self.renamed)
        [item] = doc["items"]
        self.assertEqual(item["old"]["blob_oid"], item["new"]["blob_oid"])
        region = ChangedRegion("i", "cosmetic", "a" * 40, "b" * 40, "x", "y", (1, 3), (1, 3),
                               content_unchanged=True)
        self.assertTrue(region.to_dict()["content_unchanged"])
        self.assertFalse(ChangedRegion("i", "unknown", "a" * 40, "b" * 40, "x", "y", None,
                                       None).content_unchanged)


def _mode_change(git_dir: Path, parent: str, path: str) -> str:
    """A commit on ``parent`` that only makes ``path`` executable (bytes unchanged)."""
    worktree = git_dir.parent / "mode-change-worktree"  # update-index wants one; left empty
    worktree.mkdir(exist_ok=True)
    index = {"GIT_INDEX_FILE": str(git_dir / "mode-change.index"), "GIT_WORK_TREE": str(worktree)}
    git(git_dir, "read-tree", parent, env_overrides=index)
    oid = git(git_dir, "rev-parse", f"{parent}:{path}").decode().strip()
    git(git_dir, "update-index", "--cacheinfo", f"100755,{oid},{path}", env_overrides=index)
    tree = git(git_dir, "write-tree", env_overrides=index).decode().strip()
    commit = git(git_dir, "commit-tree", tree, "-p", parent, "-m", "mode").decode().strip()
    git(git_dir, "update-ref", "refs/heads/mode", commit)
    return commit


class RegionSidesTest(unittest.TestCase):
    def test_side_accessor(self) -> None:
        region = ChangedRegion("i", "unknown", "a" * 40, "b" * 40, "x", "y", (1, 2), None,
                               ((1, 2),), ((0, 0),))
        self.assertEqual(region.side("old"), ("x", (1, 2), ((1, 2),)))
        self.assertEqual(region.side("new"), ("y", None, ((0, 0),)))


if __name__ == "__main__":
    unittest.main()
