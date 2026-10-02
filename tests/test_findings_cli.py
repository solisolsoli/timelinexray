"""``txray findings ...`` end to end: envelopes, exit codes and the ledger location rule."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from timelinexray.findings import ENV_FINDINGS
from tests.findings_support import HistoryRepo, spec
from tests.support import run_cli

REPO: HistoryRepo


def setUpModule() -> None:
    global REPO
    REPO = HistoryRepo()
    REPO.indexed("base")


def tearDownModule() -> None:
    REPO.cleanup()


class FindingsCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-findings-cli-")
        self.tmp = Path(self._tmp.name)
        self.args = ["--store", str(REPO.store_dir), "--ledger", str(self.tmp / "ledger")]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def cli(self, *argv: str, expect: int = 0) -> tuple[bytes, bytes]:
        code, out, err = run_cli(["findings", *argv, *self.args])
        self.assertEqual(code, expect, (argv, out[-500:], err))
        return out, err

    def json(self, *argv: str, expect: int = 0) -> dict:
        out, _ = self.cli(*argv, "--json", expect=expect)
        return json.loads(out)

    def add_click(self) -> dict:
        base = REPO.commits["base"]
        return self.json("add", "--actor", "agent-a", "--id", "F-click", "--title", "Click",
                         "--claim", "The synthetic click weight is 0.4 (public default).",
                         "--component", "src", "--evidence-class", "PARAM_DEFAULT",
                         "--status", "SUPPORTED", "--cite", base, "src/weights.rs", "4",
                         "CLICK_WEIGHT")

    def test_full_workflow(self) -> None:
        doc = self.add_click()
        self.assertEqual((doc["command"], doc["outcome"]), ("findings add", "ok"))
        self.assertEqual((doc["data"]["workflow"], doc["data"]["status_basis"]),
                         ("draft", "proposed"))
        spec_file = self.tmp / "fav.json"
        spec_file.write_text(json.dumps(spec(REPO, finding_id="F-fav", citations=[
            REPO.cite("base", "src/weights.rs", "2", "FAVORITE_WEIGHT")],
            depends_on=[{"finding": "F-click"}])), "utf-8")
        out, _ = self.cli("add", "--actor", "agent-a", "--file", str(spec_file))
        self.assertIn(b"created    F-fav", out)
        self.assertIn(b"public default at", out)

        out, _ = self.cli("verify")
        self.assertIn(b"F-click                  CURRENT", out)
        out, _ = self.cli("reanchor", REPO.commits["shift"], "--strict")
        self.assertIn(b"(new provenance revision)", out)
        doc = self.json("reanchor", REPO.commits["literal"], "--strict", expect=1)
        self.assertEqual(doc["data"]["freshness"], {"STALE": 2})

        doc = self.json("queue")
        self.assertEqual([i["trigger"] for i in doc["data"]["items"]][:2],
                         ["changed_span", "dependency_changed"])
        out, _ = self.cli("queue")
        self.assertIn(b"P1 changed_span", out)

        out, err = self.cli("review", "F-click", "--actor", "agent-a", "--role", "reviewer",
                            "--status", "SUPPORTED", "--rationale", "Mine.", expect=1)
        self.assertIn(b"no self-approval", err)
        self.cli("review", "F-click", "--actor", "reviewer-b", "--role", "author",
                 "--status", "SUPPORTED", "--rationale", "Wrong role.", expect=1)
        out, _ = self.cli("review", "F-click", "--actor", "reviewer-b", "--role", "reviewer",
                          "--status", "SUPPORTED", "--rationale", "Span read at its commit.")
        self.assertIn(b"SUPPORTED (reviewed) by reviewer-b", out)
        self.assertIn(b"STALE@", out)

        doc = self.json("list", "--target", REPO.commits["shift"][:10])
        by_id = {item["finding_id"]: item for item in doc["data"]["findings"]}
        self.assertEqual(by_id["F-click"]["freshness"], "CURRENT")
        self.assertEqual(self.json("list", "--freshness", "STALE")["data"]["total"], 2)
        out, _ = self.cli("show", "F-click")
        for text in (b"status     SUPPORTED (reviewed) by reviewer-b", b"provenance r2",
                     b"event     1 create", b"review"):
            self.assertIn(text, out)

        self.cli("supersede", "F-fav", "--actor", "agent-a", "--by", "F-click",
                 "--rationale", "Merged.")
        self.cli("retract", "F-click", "--actor", "agent-a", "--reason", "Synthetic.")
        self.assertEqual(self.json("list")["data"]["total"], 0)
        self.assertEqual(self.json("list", "--all")["data"]["total"], 2)

        export = self.json("export")["data"]
        self.assertEqual(export["schema"], "timelinexray/findings-export/v1")
        events = self.json("export", "--format", "events")["data"]["events"]
        self.assertEqual([e["type"] for e in events][:3], ["create", "create", "verify"])
        log = self.json("verify-log")
        self.assertEqual((log["outcome"], log["data"]["events"]), ("ok", len(events)))
        self.cli("verify-log", "--expect-head", log["data"]["last"]["hash"])

    def test_compare_and_swap_and_errors(self) -> None:
        self.add_click()
        head = self.json("verify-log")["data"]["last"]["hash"]
        out, err = self.cli("retract", "F-click", "--actor", "agent-a", "--reason", "x",
                            "--expect-head", "0" * 64, expect=1)
        self.assertIn(b"not the expected", err)
        self.cli("retract", "F-click", "--actor", "agent-a", "--reason", "x",
                 "--expect-head", head)
        _, err = self.cli("add", "--actor", "agent-a", "--file", "x.json", "--title", "t",
                          expect=2)
        self.assertIn(b"either --file or the individual flags", err)
        base = REPO.commits["base"]
        _, err = self.cli("add", "--actor", "agent-a", "--title", "t", "--claim", "c",
                          "--component", "src", "--evidence-class", "CODE", "--status",
                          "SUPPORTED", "--cite", base, "src/weights.rs", "2", "CLICK_WEIGHT",
                          expect=1)
        self.assertIn(b"is MISSING in the span", err)
        doc = self.json("show", "F-none", expect=1)
        self.assertEqual(doc["error"]["code"], "not_found")
        self.cli("add", "--title", "t", expect=2)  # --actor is required

    def test_default_ledger_is_never_inside_a_working_tree(self) -> None:
        worktree = self.tmp / "project"
        (worktree / ".git").mkdir(parents=True)
        store = worktree / ".txray-store"
        code, _, err = run_cli(["findings", "list", "--store", str(store)])
        self.assertEqual(code, 1)
        self.assertIn(b"inside the git working tree", err)
        self.assertFalse((store / "findings").exists())
        explicit = self.tmp / "explicit"
        code, _, err = run_cli(["findings", "list", "--store", str(store)],
                               {ENV_FINDINGS: str(explicit)})
        self.assertEqual((code, err), (0, b""))
        code, _, _ = run_cli(["findings", "list", "--store", str(store), "--ledger",
                              str(worktree / "ledger")])
        self.assertEqual(code, 0)
        outside = self.tmp / "outside-store"
        code, _, _ = run_cli(["findings", "verify-log", "--store", str(outside)],
                             {ENV_FINDINGS: ""})
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
