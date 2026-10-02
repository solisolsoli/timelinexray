"""Digest replay over the real upstream history, from a local clone via file:// (no network).

Needs a local clone of xai-org/x-algorithm containing 77d431a and its 38 ancestors (found
through ``$TXRAY_TEST_UPSTREAM`` or at ``../x-algorithm-upstream``); skipped without one.

* 4c5cfe8..a707cc2 must report ClickWeight 0.4 -> 0.3 as a public-default change in both
  parameter files, with citations that ``txray show`` reproduces;
* a finding in a findings ledger that cites the ClickWeight line at 4c5cfe8 must be listed
  as affected by that digest (status and freshness as separate fields), one citing the
  unchanged OpenLinkWeight line must not, and the ledger must be left untouched;
* the digest of the whole history (aaa167b..77d431a) must be byte-identical across two
  runs: one in this process and one ``txray digest`` subprocess per format, run at the
  same time. Timings are printed, never asserted.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from timelinexray.digest import DigestBuilder, default_findings_provider, digest_json, render_markdown
from timelinexray.digest.findings import LedgerFindingsProvider
from timelinexray.findings import Actor, FindingsMemory, Ledger
from timelinexray.gitio import git_env
from timelinexray.netguard import ENV_ALLOW_FILE_URLS, Allowlist
from timelinexray.snapshot import SnapshotStore
from tests.support import REPO_ROOT, file_url, upstream_git_dir

UPSTREAM = upstream_git_dir()
OLD = "4c5cfe8f07f1c76d4f04277e803f20e6039f5191"
NEW = "a707cc27ba36d3fa79450c9cffcc48a82d080b02"
FIRST = "aaa167b3de8a674587c53545a43c90eaad360010"
LAST = "77d431aabf409ca1c1eed9bec7e2183f7c914e23"
PARAMETER_FILES = ("home-mixer/params/param.rs", "vm-ranker/params.rs")


def _history(git_dir: Path) -> list[str]:
    proc = subprocess.run(
        ["git", f"--git-dir={git_dir}", "-c", "protocol.allow=never", "rev-list", LAST],
        capture_output=True, env=git_env(), check=True,
    )
    return proc.stdout.decode("ascii").split()


@unittest.skipIf(UPSTREAM is None, "no local x-algorithm clone with 77d431a "
                 "(set TXRAY_TEST_UPSTREAM to a local clone)")
class UpstreamDigestTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert UPSTREAM is not None
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-digest-upstream-")
        cls.root = Path(cls._tmp.name)
        cls.url = file_url(UPSTREAM.parent if UPSTREAM.name == ".git" else UPSTREAM)
        cls.allowlist = Allowlist([cls.url])
        cls.store = SnapshotStore(cls.root / "store")
        start = time.perf_counter()
        cls.commits = _history(UPSTREAM)
        for commit in cls.commits:
            cls.store.pin(commit, cls.url, allowlist=cls.allowlist)
        cls.pin_seconds = time.perf_counter() - start

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_history_has_39_commits(self) -> None:
        self.assertEqual(len(self.commits), 39)
        self.assertIn(OLD, self.commits)
        self.assertEqual(self.commits[-1], FIRST)

    def test_click_weight_change_between_4c5cfe8_and_a707cc2(self) -> None:
        doc = DigestBuilder(self.store, allowlist=self.allowlist).build(OLD[:7], NEW[:7])
        self.assertEqual(doc["range"]["relationship"], "ancestor")
        self.assertEqual(doc["events"], [])
        rows = [row for row in doc["parameters"]["net"] if row["name"] == "ClickWeight"]
        self.assertEqual(sorted(row["path"] for row in rows), sorted(PARAMETER_FILES))
        md = render_markdown(doc)
        for row in rows:
            with self.subTest(path=row["path"]):
                self.assertEqual((row["change"], row["declaration"], row["old_value"],
                                  row["new_value"], row["flag"]),
                                 ("value-changed", "param!", "0.4", "0.3",
                                  "rust_home_mixer_click_weight"))
                for side, value in (("old", "0.4"), ("new", "0.3")):
                    c = row[side]
                    self.assertEqual((c["commit"], c["path"], c["role"]),
                                     (OLD if side == "old" else NEW, row["path"], "span"))
                    span = self.store.read_span(c["commit"], c["path"], c["start_line"],
                                                c["end_line"], anchor=f"ClickWeight, f64, "
                                                f"\"rust_home_mixer_click_weight\", {value}")
                    self.assertEqual((span.sha256, span.anchor.verdict), (c["span_sha256"], "FOUND"))
                line = (f"| `ClickWeight` | `{row['path']}` | param! | value changed | "
                        f"public default `0.4` → `0.3` |")
                self.assertIn(line, md)
        # P4 ground truth for this step: the home-mixer copy moved from line 322 to 329.
        home = next(row for row in rows if row["path"] == PARAMETER_FILES[0])
        self.assertEqual((home["old"]["start_line"], home["new"]["start_line"]), (322, 329))

    def test_ledger_finding_on_the_click_weight_line_is_affected(self) -> None:
        ledger = self.root / "ledger"
        memory = FindingsMemory(Ledger(ledger), self.store)
        author = Actor("agent-a", "author")
        for finding_id, line, anchor in (("F-click", "322", "ClickWeight"),
                                         ("F-open", "323", "OpenLinkWeight")):
            memory.add({
                "finding_id": finding_id, "title": f"{anchor} public default",
                "claim": f"At 4c5cfe8 the {anchor} param! declaration has a public default.",
                "component": "home-mixer", "evidence_class": "PARAM_DEFAULT",
                "status": "SUPPORTED",
                "citations": [{"commit": OLD, "path": PARAMETER_FILES[0], "lines": line,
                               "anchor": anchor}],
            }, author)
        memory.verify()
        memory.reanchor(NEW)
        before = {p.name: p.read_bytes() for p in ledger.iterdir()}
        doc = DigestBuilder(self.store, allowlist=self.allowlist,
                            findings=LedgerFindingsProvider(self.store, ledger)).build(OLD, NEW)
        section = doc["affected_findings"]
        self.assertTrue(section["available"])
        [row] = section["findings"]
        self.assertEqual(row["finding_id"], "F-click")
        self.assertEqual((row["citation"]["commit"], row["citation"]["path"],
                          row["citation"]["start_line"]), (OLD, PARAMETER_FILES[0], 322))
        self.assertEqual((row["status"], row["status_basis"], row["workflow"]),
                         ("SUPPORTED", "proposed", "draft"))
        self.assertEqual((row["freshness"], row["freshness_checked_against"]), ("STALE", NEW))
        items = {item["id"]: item for item in doc["items"]}
        touched = [items[item_id] for item_id in row["item_ids"]]
        self.assertIn(("parameter-default", "ClickWeight"),
                      [(item["class"], item["detail"].get("name")) for item in touched])
        self.assertEqual(section["coverage"]["unplaced"], 0)
        self.assertIn("| `F-click` | citation |", render_markdown(doc))
        self.assertEqual({p.name: p.read_bytes() for p in ledger.iterdir()}, before)

    def test_full_history_digest_is_deterministic(self) -> None:
        out = {name: self.root / name for name in ("md", "json")}
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src"), ENV_ALLOW_FILE_URLS: self.url}
        procs = {
            fmt: subprocess.Popen(
                [sys.executable, "-m", "timelinexray", "digest", FIRST, LAST, "--store",
                 str(self.store.root), "--out", str(out[fmt]), "--format", fmt],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            )
            for fmt in ("md", "json")
        }
        results: dict[str, bytes] = {}
        errors: dict[str, bytes] = {}

        def wait(fmt: str) -> None:
            _, errors[fmt] = procs[fmt].communicate(timeout=900)

        threads = [threading.Thread(target=wait, args=(fmt,)) for fmt in procs]
        for thread in threads:
            thread.start()
        start = time.perf_counter()
        doc = DigestBuilder(self.store, allowlist=self.allowlist,
                            findings=default_findings_provider(self.store)).build(FIRST, LAST)
        local_seconds = time.perf_counter() - start
        results["md"] = render_markdown(doc).encode("utf-8")
        results["json"] = digest_json(doc).encode("utf-8")
        for thread in threads:
            thread.join()
        for fmt, proc in procs.items():
            with self.subTest(format=fmt):
                self.assertEqual(proc.returncode, 0, errors[fmt].decode("utf-8", "replace"))
                name = f"digest-{FIRST[:12]}-{LAST[:12]}.{fmt}"
                self.assertEqual((out[fmt] / name).read_bytes(), results[fmt])
        rng = doc["range"]
        self.assertEqual((rng["relationship"], rng["commits_in_range"], len(rng["first_parent_chain"])),
                         ("ancestor", 38, 38))
        self.assertEqual(len(rng["reached_through_merges"]), 1)
        self.assertTrue(rng["history_complete"])
        history = {(row["path"], row["name"]): row for row in doc["parameters"]["history"]}
        click = [(p["commit"][:7], p["value"]) for p in history[(PARAMETER_FILES[0], "ClickWeight")]["points"]]
        self.assertEqual(click[1:], [("47c1bcd", "0.4"), ("a707cc2", "0.3")])
        reversions = {row["name"] for row in doc["parameters"]["reversions"]}
        self.assertIn("EnableAdsBrandSafetyVerdictV2", reversions)
        counts = doc["summary"]["net"]["items_by_class"]
        print(f"\n[upstream digest] pinned 39 commits in {self.pin_seconds:.1f} s; full-history "
              f"digest built in {local_seconds:.1f} s (in process, alongside two CLI runs); "
              f"net items by class: " + ", ".join(f"{k} {v}" for k, v in counts.items()),
              file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
