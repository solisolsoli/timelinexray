"""The reference citations in goldens/ and the ClickWeight history on the real upstream.

The structural checks always run. The upstream checks need a local clone of
xai-org/x-algorithm containing 4c5cfe8, a707cc2 and 77d431a (``$TXRAY_TEST_UPSTREAM`` or
``../x-algorithm-upstream``); it is pinned through ``txray pin`` with a ``file://`` URL
allowed by ``$TXRAY_ALLOW_FILE_URLS``. Nothing is fetched from the network.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from timelinexray.findings import Actor, FindingsMemory, Ledger, goldens
from timelinexray.netguard import ENV_ALLOW_FILE_URLS
from timelinexray.snapshot import SnapshotStore
from timelinexray.verify import CURRENT, STALE, Verifier
from tests.support import REPO_ROOT, UPSTREAM_COMMIT, file_url, run_cli, upstream_git_dir

GOLDENS = REPO_ROOT / "goldens" / "citations.json"
UPSTREAM = upstream_git_dir()
CLICK_OLD = "4c5cfe8f07f1c76d4f04277e803f20e6039f5191"
CLICK_NEW = "a707cc27ba36d3fa79450c9cffcc48a82d080b02"
PARAM = "home-mixer/params/param.rs"


class GoldenFileTest(unittest.TestCase):
    def test_goldens_are_citations_only(self) -> None:
        data = json.loads(GOLDENS.read_text("utf-8"))
        self.assertEqual(set(data), {"schema", "upstream", "description", "goldens"})
        loaded = goldens.load(GOLDENS)
        self.assertGreaterEqual(len(loaded), 25)
        for entry in data["goldens"]:
            with self.subTest(entry["id"]):
                self.assertLessEqual(set(entry), goldens.ENTRY_KEYS)
                self.assertRegex(entry["commit"], r"^[0-9a-f]{40}$")
                self.assertRegex(entry["span_sha256"], r"^[0-9a-f]{64}$")
                self.assertRegex(entry["source"], r"^P[0-9]b?-[0-9]{3}$")
                self.assertLessEqual(len(entry["anchor"]), 80)
        history = [golden for golden in loaded if golden.history]
        self.assertGreaterEqual(len(history), 5)
        outcomes = {item["outcome"] for golden in history for item in golden.history}
        self.assertEqual(outcomes, {"changed", "relocated", "unchanged", "identical"})
        self.assertIn({"verdict": "INTACT", "anchor": "MISSING"},
                      [{"verdict": g.expected_verdict, "anchor": g.expected_anchor}
                       for g in loaded])

    def test_the_click_weight_history_golden(self) -> None:
        click = [g for g in goldens.load(GOLDENS)
                 if g.citation.commit == CLICK_OLD and g.citation.path == PARAM
                 and g.citation.anchor == "ClickWeight"]
        self.assertEqual(len(click), 1)
        self.assertEqual({(item["target"], item["freshness"], item["outcome"])
                          for item in click[0].history},
                         {(CLICK_NEW, STALE, "changed"), (UPSTREAM_COMMIT, STALE, "changed")})


@unittest.skipIf(UPSTREAM is None, "no local x-algorithm clone with 77d431a "
                 "(set TXRAY_TEST_UPSTREAM to a local clone)")
class UpstreamGoldensTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert UPSTREAM is not None
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-goldens-")
        cls.root = Path(cls._tmp.name)
        cls.store_dir = cls.root / "store"
        url = file_url(UPSTREAM.parent if UPSTREAM.name == ".git" else UPSTREAM)
        cls.goldens = goldens.load(GOLDENS)
        for commit in goldens.commits(cls.goldens):
            code, _, err = run_cli(["pin", commit, "--upstream", url, "--store",
                                    str(cls.store_dir)], {ENV_ALLOW_FILE_URLS: url})
            assert code == 0, err
        cls.store = SnapshotStore(cls.store_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_every_golden_passes(self) -> None:
        results = goldens.run(Verifier(self.store), self.goldens)
        failures = [result for result in results if not result["passed"]]
        at_77 = [r for r in results if r["citation"].endswith("@" + UPSTREAM_COMMIT[:12])]
        history = sum(len(r["history"]) for r in results)
        sys.stderr.write(
            f"\n[goldens] {len(results) - len(failures)}/{len(results)} passed "
            f"({len(at_77)} cited at 77d431a, {len(results) - len(at_77)} at earlier commits; "
            f"{history} history checks)\n"
        )
        self.assertEqual(failures, [])

    def test_click_weight_history_through_the_findings_memory(self) -> None:
        memory = FindingsMemory(Ledger(self.root / "ledger"), self.store)
        author = Actor("agent-a", "author")
        click = memory.add({
            "finding_id": "H-click", "title": "Click weight public default",
            "claim": "At 4c5cfe8 the public default of ClickWeight is 0.4.",
            "component": "home-mixer", "evidence_class": "PARAM_DEFAULT", "status": "SUPPORTED",
            "citations": [{"commit": CLICK_OLD[:7], "path": PARAM, "lines": "322",
                           "anchor": "ClickWeight"}],
        }, author)
        memory.add({
            "finding_id": "H-favorite", "title": "Favorite weight public default",
            "claim": "At 4c5cfe8 the public default of FavoriteWeight is 0.5.",
            "component": "home-mixer", "evidence_class": "PARAM_DEFAULT", "status": "SUPPORTED",
            "citations": [{"commit": CLICK_OLD, "path": PARAM, "lines": "295",
                           "anchor": "FavoriteWeight"}],
        }, author)
        self.assertEqual(click.record["sources"][0]["commit"], CLICK_OLD)
        for target in (CLICK_NEW, UPSTREAM_COMMIT):
            report = memory.reanchor(target)
            by_id = {result["finding_id"]: result for result in report["results"]}
            self.assertEqual((by_id["H-click"]["freshness"], by_id["H-click"]["outcomes"]),
                             (STALE, {"changed": 1}))
            self.assertEqual(by_id["H-favorite"]["freshness"], CURRENT)
        view = memory.view()
        items = [item for item in view.queue() if item["trigger"] == "changed_span"]
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertEqual((item["detail"]["proposed"]["start_line"],
                              item["detail"]["proposed"]["end_line"]), (329, 329))
            self.assertRegex(item["detail"]["diff"], re.escape('click_weight", 0.3);'))
        favorite = view.get("H-favorite")
        self.assertEqual([(r["commit"], r["citations"][0]["start_line"])
                          for r in favorite.provenance[1:]], [(CLICK_NEW, 302)])
        self.assertEqual(favorite.freshness(), (CURRENT, UPSTREAM_COMMIT))
        code, out, _ = run_cli(["findings", "reanchor", UPSTREAM_COMMIT[:7], "H-click",
                                "--strict", "--store", str(self.store_dir), "--ledger",
                                str(self.root / "ledger")])
        self.assertEqual(code, 1)
        self.assertIn(b"H-click", out)
        self.assertIn(b"STALE", out)


if __name__ == "__main__":
    unittest.main()
