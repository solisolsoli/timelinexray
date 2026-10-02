"""Parameter declarations and history: ``timelinexray.params``, ``txray param`` and
``txray param-history``."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from timelinexray.errors import InvalidInput, NotFound
from timelinexray.index import CodeIndex
from timelinexray.netguard import Allowlist
from timelinexray.params import ParamResolver, check_name, name_matches, returns
from timelinexray.snapshot import SnapshotStore
from tests.diff_support import range_history
from tests.mcp_support import IndexedStore
from tests.support import file_url, run_cli

FIXTURE: IndexedStore


def setUpModule() -> None:
    global FIXTURE
    FIXTURE = IndexedStore()


def tearDownModule() -> None:
    FIXTURE.cleanup()


def _store_args() -> list[str]:
    return ["--store", str(FIXTURE.store_dir)]


class ResolverTest(unittest.TestCase):
    def test_declarations_at_a_commit(self) -> None:
        resolver = ParamResolver(FIXTURE.store)
        snapshot = resolver.declarations(FIXTURE.commit_a[:9], "ClickWeight")
        self.assertEqual((snapshot.commit, snapshot.indexed, snapshot.lookup),
                         (FIXTURE.commit_a, True, "index"))
        [item] = snapshot.declarations
        self.assertEqual((item.declaration, item.symbol_kind, item.qualname, item.value,
                          item.value_type, item.flag, item.literal, item.ordinal),
                         ("param!", "param", "ClickWeight", "0.3", "f64",
                          "fixture_click_weight", True, 0))
        self.assertEqual((item.path, item.start_line, item.end_line), ("src/scoring.rs", 46, 51))
        span = FIXTURE.store.read_span(FIXTURE.commit_a, "src/scoring.rs", 46, 51)
        self.assertEqual((item.span_sha256, item.blob_oid), (span.sha256, span.blob_oid))
        self.assertEqual(item.to_dict()["value_note"],
                         f"public default at commit {FIXTURE.commit_a}; not a production value")
        total, hits = resolver.mentions(FIXTURE.commit_a, "ClickWeight", snapshot.declarations, 10)
        self.assertGreaterEqual(total, 1)
        self.assertFalse(any(hit.path == "src/scoring.rs" and 46 <= hit.start_line <= 51
                             for hit in hits))
        constants = resolver.declarations(FIXTURE.commit_a, "NEGATIVE_SCORES_OFFSET")
        self.assertEqual([(d.declaration, d.value) for d in constants.declarations],
                         [("const", "0.0")])
        self.assertEqual(resolver.declarations(FIXTURE.commit_a, "no_such_name").declarations, ())

    def test_names_are_exact_and_validated(self) -> None:
        for bad in ("", "Click*", "a?b", "x[y]", "a\nb", "n" * 257, None):
            with self.subTest(name=bad), self.assertRaises(InvalidInput):
                check_name(bad)
        self.assertTrue(name_matches("Outer::Inner::VALUE", "VALUE"))
        self.assertTrue(name_matches("Outer::Inner::VALUE", "Inner::VALUE"))
        self.assertTrue(name_matches("pkg.Cls.VALUE", "Cls.VALUE"))
        self.assertFalse(name_matches("Outer::Inner::VALUE", "nner::VALUE"))
        self.assertFalse(name_matches("MY_VALUE", "VALUE"))

    def test_unindexed_commit_is_not_found(self) -> None:
        store = SnapshotStore(FIXTURE.root / "unindexed-store")
        store.pin(FIXTURE.commit_a, FIXTURE.url, allowlist=FIXTURE.allowlist)
        with self.assertRaises(NotFound):
            ParamResolver(store).declarations(FIXTURE.commit_a, "ClickWeight")
        with self.assertRaises(NotFound):
            ParamResolver(store).history("ClickWeight")

    def test_history_over_two_indexed_commits(self) -> None:
        history = ParamResolver(FIXTURE.store).history("ClickWeight")
        self.assertEqual((history.base, history.head, history.history_complete, history.gaps,
                          history.not_on_line), (FIXTURE.commit_a, FIXTURE.commit_b, True, (), ()))
        [timeline] = history.timelines
        self.assertEqual([(c, v) for c, _, v in timeline.points],
                         [(FIXTURE.commit_a, "0.3"), (FIXTURE.commit_b, "0.25")])
        [change] = timeline.changes
        self.assertEqual((change.event, change.previous_commit, change.commit),
                         ("value-changed", FIXTURE.commit_a, FIXTURE.commit_b))
        assert change.old is not None and change.new is not None
        self.assertEqual((change.old.value, change.new.value), ("0.3", "0.25"))
        self.assertEqual(change.old.span_sha256,
                         FIXTURE.store.read_span(FIXTURE.commit_a, "src/scoring.rs", 46, 51).sha256)
        self.assertEqual(change.new.span_sha256,
                         FIXTURE.store.read_span(FIXTURE.commit_b, "src/scoring.rs", 46, 51).sha256)
        assert timeline.current is not None
        self.assertEqual(timeline.current.value, "0.25")
        unchanged = ParamResolver(FIXTURE.store).history("ReplyWeight")
        [timeline] = unchanged.timelines
        self.assertEqual((timeline.changes, [v for _, _, v in timeline.points]), ((), ["5.0", "5.0"]))

    def test_returns(self) -> None:
        self.assertEqual(returns(["a", "b", "a"]), [{"left_at": 0, "back_at": 2, "value": "a"}])
        self.assertEqual(returns(["a", "a", "b"]), [])
        self.assertEqual(returns(["a", "b", "c", "b"]), [{"left_at": 1, "back_at": 3, "value": "b"}])


class RangeHistoryTest(unittest.TestCase):
    """The multi-commit fixture: c0 -> c1 -> c2 -> m (merges side s) -> c3; EnableLegacy goes
    false -> true -> false (a reversion) and ClickWeight 0.4 -> 0.3; c2 and m are pinned
    but not indexed, s is pinned but off the first-parent line."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-params-range-")
        root = Path(cls._tmp.name)
        git_dir, cls.commits, cls.side = range_history(root)
        cls.url = file_url(git_dir)
        cls.store = SnapshotStore(root / "store")
        for commit in [*cls.commits, cls.side]:
            cls.store.pin(commit, cls.url, allowlist=Allowlist([cls.url]))
        index = CodeIndex(cls.store)
        for commit in (cls.commits[0], cls.commits[1], cls.commits[4]):
            index.build(commit)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_unindexed_pins_are_checked_at_known_paths_and_said_so(self) -> None:
        c0, c1, c2, merge, c3 = self.commits
        history = ParamResolver(self.store).history("EnableLegacy")
        self.assertEqual((history.base, history.head), (c0, c3))
        self.assertEqual([(s.commit, s.indexed, s.lookup) for s in history.commits],
                         [(c0, True, "index"), (c1, True, "index"), (c2, False, "known-paths"),
                          (merge, False, "known-paths"), (c3, True, "index")])
        self.assertEqual(history.not_on_line, (self.side,))
        self.assertTrue(history.history_complete)
        self.assertTrue(any("not indexed" in w and "known-paths" in w for w in history.warnings))
        [timeline] = history.timelines
        self.assertEqual([(c, v) for c, _, v in timeline.points],
                         [(c0, "false"), (c1, "true"), (c2, "false"), (merge, "false"), (c3, "false")])
        self.assertEqual([(c.event, c.old_value if c.old is None else c.old.value,
                           c.new.value if c.new else None, c.commit)
                          for c in timeline.changes],
                         [("value-changed", "false", "true", c1), ("value-changed", "true", "false", c2)])
        self.assertEqual(timeline.reversions, ({"value": "false", "left_at": c1, "back_at": c2},))
        # committer times come from the pins, oldest first
        times = [s.committer_time for s in history.commits]
        self.assertEqual(times, sorted(times))

    def test_explicit_range_gaps_and_ancestry(self) -> None:
        c0, c1, c2, merge, c3 = self.commits
        resolver = ParamResolver(self.store)
        history = resolver.history("ClickWeight", base=c0, head=c2)
        self.assertEqual([s.commit for s in history.commits], [c0, c1, c2])
        self.assertEqual([(c.event, c.old.value, c.new.value) for c in history.timelines[0].changes
                          if c.old and c.new], [("value-changed", "0.4", "0.3")])
        self.assertEqual(sorted(history.not_on_line), sorted([merge, c3, self.side]))
        self.assertFalse(any("not on the first-parent line" in w for w in history.warnings))
        with self.assertRaises(InvalidInput):
            resolver.history("ClickWeight", base=c2, head=c0)
        # a gap: unpin c1 by using a store that never pinned it
        sparse = SnapshotStore(Path(self._tmp.name) / "sparse")
        for commit in (c0, c3):
            sparse.pin(commit, self.url, allowlist=Allowlist([self.url]))
        CodeIndex(sparse).build(c3)
        gapped = ParamResolver(sparse).history("ClickWeight")
        self.assertFalse(gapped.history_complete)
        self.assertEqual(gapped.gaps, (f"3 unpinned commit(s) between {c0[:12]} and {c3[:12]}",))
        [timeline] = gapped.timelines
        self.assertEqual([(c, v) for c, _, v in timeline.points], [(c0, "0.4"), (c3, "0.3")])


class ParamCommandTest(unittest.TestCase):
    def test_param_human_and_json(self) -> None:
        code, out, err = run_cli(["param", "ClickWeight", "--commit", FIXTURE.commit_b[:8],
                                  *_store_args()])
        self.assertEqual((code, err), (0, b""))
        text = out.decode()
        self.assertIn(f"commit      {FIXTURE.commit_b}", text)
        self.assertIn("param!      ClickWeight  src/scoring.rs:46-51  sha256:", text)
        self.assertIn('public default 0.25  type f64  flag "fixture_click_weight"', text)
        self.assertIn(f"public defaults at commit {FIXTURE.commit_b}, not production values", text)
        code, out, _ = run_cli(["param", "ClickWeight", "--json", *_store_args()])  # newest pin
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual((doc["command"], doc["outcome"], doc["data"]["commit"]),
                         ("param", "ok", FIXTURE.commit_b))
        [item] = doc["data"]["declarations"]
        self.assertEqual((item["value"], item["flag"]), ("0.25", "fixture_click_weight"))
        self.assertIn("public default at commit " + FIXTURE.commit_b, item["value_note"])
        self.assertIn("public defaults at commit " + FIXTURE.commit_b, doc["note"])
        self.assertIn("mentions_total", doc["data"])
        code, out, _ = run_cli(["param", "no_such_name", "--json", *_store_args()])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["note"], None)
        code, _, err = run_cli(["param", "Click*", *_store_args()])
        self.assertEqual(code, 2)
        self.assertIn(b"matched exactly", err)

    def test_param_history_human_and_json(self) -> None:
        code, out, err = run_cli(["param-history", "ClickWeight", *_store_args()])
        self.assertEqual((code, err), (0, b""))
        text = out.decode()
        self.assertIn(f"line        {FIXTURE.commit_a[:12]}..{FIXTURE.commit_b[:12]}: 2 pinned", text)
        self.assertIn("history     complete", text)
        self.assertIn("param!      ClickWeight  src/scoring.rs", text)
        self.assertIn(f"{FIXTURE.commit_b[:12]}  value changed: public default 0.3 -> 0.25", text)
        self.assertIn("present at the start of the line, public default 0.3", text)
        self.assertIn("current: public default 0.25", text)
        self.assertIn("note        values are public defaults at the cited commits", text)
        code, out, _ = run_cli(["param-history", "ClickWeight", "--base", FIXTURE.commit_a,
                                "--head", FIXTURE.commit_b, "--json", *_store_args()])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual((doc["command"], doc["outcome"]), ("param-history", "ok"))
        [timeline] = doc["data"]["timelines"]
        self.assertEqual([c["event"] for c in timeline["changes"]], ["value-changed"])
        self.assertEqual(timeline["changes"][0]["old"]["commit"], FIXTURE.commit_a)
        self.assertIn("public defaults at the cited commits", doc["note"])
        code, out, err = run_cli(["param-history", "ClickWeight", "--base", FIXTURE.commit_b,
                                  "--head", FIXTURE.commit_a, *_store_args()])
        self.assertEqual(code, 2)
        self.assertIn(b"is not an ancestor of", err)


if __name__ == "__main__":
    unittest.main()
