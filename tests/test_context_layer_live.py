"""Optional live check with a real Context Layer installation (never required).

Runs only when ``TXRAY_TEST_CONTEXT_LAYER`` names a ``context-layer`` executable; otherwise
it is skipped with a reason starting with ``optional:``, which ``scripts/run_tests.py``
never counts as a failure. TimelineXray does not import Context Layer: this test only runs
its command line in child processes (``init``, ``index`` and ``search``, which make no
network requests) against a temporary vault that holds one note of the user's own and a
TimelineXray export, and checks that a search for a finding's claim returns the exported
note with the exact bytes TimelineXray wrote.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.findings_support import AUTHOR, HistoryRepo, spec
from tests.support import run_cli

ENV = "TXRAY_TEST_CONTEXT_LAYER"
EXECUTABLE = os.environ.get(ENV) or ""


@unittest.skipUnless(EXECUTABLE and os.path.isfile(EXECUTABLE) and os.access(EXECUTABLE, os.X_OK),
                     f"optional: set {ENV} to a context-layer executable to run this live test")
class ContextLayerLiveTest(unittest.TestCase):
    def run_context_layer(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        proc = subprocess.run([EXECUTABLE, *args], capture_output=True, timeout=180,
                              env={**os.environ, "PYTHONUTF8": "1"})
        self.assertEqual(proc.returncode, 0, (args[0], proc.stderr.decode("utf-8", "replace")))
        return proc

    def test_context_layer_finds_an_exported_note(self) -> None:
        repo = HistoryRepo()
        self.addCleanup(repo.cleanup)
        memory = repo.memory()
        finding = memory.add(spec(
            repo, title="Filter apply keeps truthy candidates",
            claim="The synthetic apply function keeps every truthy candidate (quokka marker).",
            evidence_class="CODE", component="src",
            citations=[repo.cite("base", "src/filters.py", "6-7", "def apply")]), AUTHOR)
        memory.verify()
        with tempfile.TemporaryDirectory(prefix="txray-clvault-") as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            (vault / "own-note.md").write_text("# My own note\n\nNothing about filters.\n",
                                               "utf-8")
            out = vault / "timelinexray"
            code, _, err = run_cli(["export", "context-layer", "--out", str(out), "--ledger",
                                    str(memory.ledger.directory), "--store",
                                    str(repo.store_dir)])
            self.assertEqual(code, 0, err)
            note = f"timelinexray/txray-findings/txray-finding-{finding.finding_id}.md"
            self.assertTrue((vault / note).is_file())
            self.run_context_layer("init", str(vault))
            self.run_context_layer("index", str(vault))
            proc = self.run_context_layer("search", str(vault), "--prompt",
                                          "quokka marker truthy candidate")
            packet = json.loads(proc.stdout)
            self.assertEqual(packet["status"], "PARTIAL", packet)
            paths = {item["source_path"]: item["source_sha256"] for item in packet["evidence"]}
            self.assertIn(note, paths)
            self.assertEqual(paths[note], hashlib.sha256((vault / note).read_bytes()).hexdigest())
            self.assertNotIn(".txray-export.json", " ".join(paths))


if __name__ == "__main__":
    unittest.main()
