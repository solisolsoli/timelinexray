"""The parameter tools on the real upstream history (local clone via file://, no network).

Acceptance (audit section 6, item 4): ``get_param ClickWeight`` at 77d431a returns both
declarations with "public default" notes and citations; ``param_history ClickWeight``
returns the 0.4 -> 0.3 timeline with commits, committer times and both citations, computed
from pinned commits only. Times are measured and printed, never asserted tightly.

Needs a local clone of xai-org/x-algorithm containing 77d431a (found through
``$TXRAY_TEST_UPSTREAM`` or at ``../x-algorithm-upstream``); skipped without one.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from timelinexray.mcp import schema as S
from timelinexray.mcp.guard import StoreGuard
from timelinexray.mcp.server import MAX_RESPONSE_BYTES, Server
from timelinexray.params import ParamResolver
from timelinexray.snapshot import SnapshotStore
from tests.mcp_support import call
from tests.support import UPSTREAM_COMMIT, run_cli, upstream_git_dir
from tests.templates import copy_upstream_store

UPSTREAM = upstream_git_dir()
OLD = "4c5cfe8f07f1c76d4f04277e803f20e6039f5191"  # ClickWeight 0.4
NEW = "a707cc27ba36d3fa79450c9cffcc48a82d080b02"  # ClickWeight 0.3 (parent of 77d431a)
LAST = UPSTREAM_COMMIT
PARAMETER_FILES = ("home-mixer/params/param.rs", "vm-ranker/params.rs")


@unittest.skipIf(UPSTREAM is None, "no local x-algorithm clone with 77d431a "
                 "(set TXRAY_TEST_UPSTREAM to a local clone)")
class UpstreamParamsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert UPSTREAM is not None
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-params-upstream-")
        root = Path(cls._tmp.name)
        start = time.perf_counter()
        # the shared template: 4c5cfe8, a707cc2 and 77d431a pinned and indexed
        copy_upstream_store("indexed", root / "store")
        cls.store = SnapshotStore(root / "store")
        cls.index_seconds = time.perf_counter() - start

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_get_param_click_weight_at_77d431a(self) -> None:
        start = time.perf_counter()
        snapshot = ParamResolver(self.store).declarations(LAST, "ClickWeight")
        seconds = time.perf_counter() - start
        self.assertEqual([d.path for d in snapshot.declarations], list(PARAMETER_FILES))
        for item in snapshot.declarations:
            with self.subTest(path=item.path):
                self.assertEqual((item.declaration, item.value, item.value_type, item.flag),
                                 ("param!", "0.3", "f64", "rust_home_mixer_click_weight"))
                span = self.store.read_span(LAST, item.path, item.start_line, item.end_line,
                                            anchor='ClickWeight, f64, "rust_home_mixer_click_weight", 0.3')
                self.assertEqual((span.sha256, span.anchor.verdict), (item.span_sha256, "FOUND"))
                self.assertEqual(item.to_dict()["value_note"],
                                 f"public default at commit {LAST}; not a production value")
        self.assertEqual(snapshot.declarations[0].start_line, 329)  # P4 ground truth
        server = Server(StoreGuard(self.store.root), log=io.StringIO())
        start = time.perf_counter()
        line = server.handle_line(json.dumps(call("get_param", {"commit": LAST,
                                                               "name": "ClickWeight"})).encode())
        mcp_seconds = time.perf_counter() - start
        assert line is not None
        envelope = json.loads(line)["result"]["structuredContent"]
        S.validate(envelope, server.registry.get("get_param").strict_output_schema)
        self.assertEqual(envelope["outcome"], "OK")
        self.assertEqual([d["citation"]["path"] for d in envelope["data"]["declarations"]],
                         list(PARAMETER_FILES))
        self.assertTrue(all("public default at commit " + LAST in d["value_note"]
                            for d in envelope["data"]["declarations"]))
        self.assertIn("public defaults at commit " + LAST, " ".join(envelope["notes"]))
        self.assertLessEqual(len(line), MAX_RESPONSE_BYTES)
        self.assertLess(seconds, 10)
        print(f"\n[upstream params] get_param ClickWeight at 77d431a: {seconds:.3f} s in process, "
              f"{mcp_seconds:.3f} s through the server ({len(line)} bytes); store of 3 indexed commits "
              f"copied in {self.index_seconds:.1f} s", file=sys.stderr)

    def test_param_history_click_weight_0_4_to_0_3_at_a707cc2(self) -> None:
        start = time.perf_counter()
        history = ParamResolver(self.store).history("ClickWeight")
        seconds = time.perf_counter() - start
        self.assertEqual((history.base, history.head, history.history_complete, history.gaps),
                         (OLD, LAST, True, ()))
        self.assertEqual([s.commit for s in history.commits], [OLD, NEW, LAST])
        self.assertEqual([t.path for t in history.timelines], list(PARAMETER_FILES))
        for timeline in history.timelines:
            with self.subTest(path=timeline.path):
                self.assertEqual([(c, v) for c, _, v in timeline.points],
                                 [(OLD, "0.4"), (NEW, "0.3"), (LAST, "0.3")])
                [change] = timeline.changes
                assert change.old is not None and change.new is not None
                self.assertEqual((change.event, change.previous_commit, change.commit,
                                  change.committer_time, change.old.value, change.new.value),
                                 ("value-changed", OLD, NEW, "2026-09-29T03:06:30Z", "0.4", "0.3"))
                for side, commit, value in ((change.old, OLD, "0.4"), (change.new, NEW, "0.3")):
                    span = self.store.read_span(commit, side.path, side.start_line, side.end_line,
                                                anchor=f'ClickWeight, f64, "rust_home_mixer_click_weight", {value}')
                    self.assertEqual((span.sha256, span.anchor.verdict), (side.span_sha256, "FOUND"))
        home = history.timelines[0]
        self.assertEqual((home.changes[0].old.start_line, home.changes[0].new.start_line), (322, 329))
        server = Server(StoreGuard(self.store.root), log=io.StringIO())
        start = time.perf_counter()
        line = server.handle_line(json.dumps(call("param_history", {"name": "ClickWeight"})).encode())
        mcp_seconds = time.perf_counter() - start
        assert line is not None
        envelope = json.loads(line)["result"]["structuredContent"]
        S.validate(envelope, server.registry.get("param_history").strict_output_schema)
        self.assertEqual(envelope["outcome"], "OK")
        [change] = envelope["data"]["timelines"][0]["changes"]
        self.assertEqual((change["old"]["citation"]["commit"], change["new"]["citation"]["commit"]),
                         (OLD, NEW))
        self.assertLessEqual(len(line), MAX_RESPONSE_BYTES)
        self.assertLess(seconds, 10)
        code, out, _ = run_cli(["param-history", "ClickWeight", "--store", str(self.store.root)])
        self.assertEqual(code, 0)
        self.assertIn(f"{NEW[:12]}  value changed: public default 0.4 -> 0.3", out.decode())
        print(f"\n[upstream params] param_history ClickWeight over 3 pins: {seconds:.3f} s in "
              f"process, {mcp_seconds:.3f} s through the server ({len(line)} bytes)",
              file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
