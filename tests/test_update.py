"""``txray update``: baseline, update, no change, and the exceptional-event drills
(force-push, deleted commit, commit missing locally, fetch failure, refused URL)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timelinexray import netguard
from timelinexray.digest import update as update_module
from timelinexray.digest.build import DigestBuilder
from timelinexray.digest.findings import LedgerFindingsProvider
from timelinexray.digest.update import load_state, run_update
from timelinexray.errors import IntegrityError, NetworkRefused, Refused
from timelinexray.export.notes import FINDINGS_DIR
from timelinexray.findings import Actor, FindingsMemory, Ledger
from timelinexray.gitio import git_env
from timelinexray.netguard import ENV_ALLOW_FILE_URLS, Allowlist
from timelinexray.snapshot import SnapshotStore
from tests.diff_support import (
    commit,
    delete_branch,
    init_bare,
    range_params,
    run_git,
    set_branch,
)
from tests.support import file_url, run_cli, upstream_git_dir

CLOCK = "2026-03-01T06:17:00Z"


def _files(click: str, extra: str = "") -> dict[str, bytes]:
    return {"params/param.rs": range_params(click, "false"), "notes.md": f"# Notes{extra}\n".encode()}


class _Upstream:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-update-")
        self.root = Path(self._tmp.name)
        self.git_dir = init_bare(self.root / "upstream.git")
        self.url = file_url(self.git_dir)
        self.allowlist = Allowlist([self.url])
        self.store = SnapshotStore(self.root / "store")
        self.out = self.root / "out"
        self.day = 0

    def commit(self, files: dict[str, bytes], parents: list[str]) -> str:
        self.day += 1
        return commit(self.git_dir, files, parents, self.day)

    def update(self, **kwargs: object):
        return run_update(self.store, out_dir=self.out, allowlist=self.allowlist,
                          upstream=self.url, clock=lambda: CLOCK, **kwargs)  # type: ignore[arg-type]

    def status(self) -> dict:
        return json.loads((self.out / "update-status.json").read_text("utf-8"))

    def cleanup(self) -> None:
        self._tmp.cleanup()


class UpdateFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.up = _Upstream()
        self.addCleanup(self.up.cleanup)
        self.c0 = self.up.commit(_files("0.4"), [])
        self.c1 = self.up.commit(_files("0.4", " one"), [self.c0])
        set_branch(self.up.git_dir, "main", self.c1)

    def test_baseline_update_no_change(self) -> None:
        result = self.up.update()
        self.assertEqual((result.outcome, result.exit_code, result.accepted), ("baseline", 0, self.c1))
        self.assertEqual(result.written, [])
        self.assertEqual(self.up.status()["outcome"], "baseline")

        c2 = self.up.commit(_files("0.3", " one"), [self.c1])
        set_branch(self.up.git_dir, "main", c2)
        result = self.up.update()
        self.assertEqual((result.outcome, result.exit_code, result.previous, result.head),
                         ("updated", 0, self.c1, c2))
        name = f"digest-{self.c1[:12]}-{c2[:12]}"
        self.assertEqual(result.written, [name + ".md", name + "-appendix.md", name + ".json"])
        self.assertTrue((self.up.out / (name + "-appendix.md")).read_text("utf-8").startswith(
            "# Change digest appendix"))
        md = (self.up.out / (name + ".md")).read_text("utf-8")
        self.assertIn("public default `0.4` → `0.3`", md)
        self.assertEqual(load_state(self.up.store)["accepted"], c2)
        status = self.up.status()
        self.assertEqual((status["observed_at"], status["observation_succeeded"]), (CLOCK, True))
        self.assertEqual(status["digests"], result.written)
        summary = status["digest_summary"]
        self.assertEqual((summary["old"], summary["new"], summary["parameter_changes"]),
                         (self.c1, c2, 1))
        self.assertEqual(summary["items_by_class"]["parameter-default"], 1)
        self.assertNotIn(str(self.up.root), json.dumps(status))

        result = self.up.update()
        self.assertEqual((result.outcome, result.exit_code), ("no-change", 0))
        self.assertEqual([e.kind for e in result.events], ["no-new-commit"])
        self.assertIn("no new public commit observed", result.events[0].message)

    def test_first_update_compares_with_the_newest_existing_pin(self) -> None:
        self.up.store.pin(self.c0, self.up.url, allowlist=self.up.allowlist)
        result = self.up.update()
        self.assertEqual((result.outcome, result.previous, result.head), ("updated", self.c0, self.c1))

    def test_a_failed_first_update_is_not_reported_as_no_change_next_time(self) -> None:
        """The head is pinned before the digest is built and the accepted commit is only
        recorded after it is written, so a crash in between leaves the head pinned and no
        accepted commit: the retry must still compare against the older pin, not the head."""
        self.up.store.pin(self.c0, self.up.url, allowlist=self.up.allowlist)
        with mock.patch.object(DigestBuilder, "build", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.up.update()
        self.assertIn(self.c1, {pin.commit for pin in self.up.store.list_pins()})
        self.assertIsNone((load_state(self.up.store) or {}).get("accepted"))
        result = self.up.update()
        self.assertEqual((result.outcome, result.previous, result.head),
                         ("updated", self.c0, self.c1))
        self.assertTrue(result.written)

    def test_force_push_drill(self) -> None:
        self.assertEqual(self.up.update().accepted, self.c1)
        rewritten = self.up.commit(_files("0.2", " rewritten"), [self.c0])
        set_branch(self.up.git_dir, "main", rewritten)
        result = self.up.update()
        self.assertEqual((result.outcome, result.exit_code), ("attention", 1))
        kinds = {(e.kind, e.commit) for e in result.events}
        self.assertIn(("history-rewritten", self.c1), kinds)
        self.assertIn(("commit-unreachable-upstream", self.c1), kinds)
        self.assertEqual(result.accepted, self.c1)  # not advanced
        state = load_state(self.up.store)
        self.assertEqual((state["accepted"], state["quarantined"]), (self.c1, [rewritten]))
        # the old pin is kept and still readable; the new head is pinned (preserved)
        span = self.up.store.read_span(self.c1, "notes.md", 1, 1)
        self.assertEqual(span.data, b"# Notes one\n")
        self.assertEqual(self.up.store.get_pin(rewritten).commit, rewritten)
        doc = json.loads((self.up.out / f"digest-{self.c1[:12]}-{rewritten[:12]}.json").read_text())
        self.assertEqual(doc["range"]["relationship"], "diverged")
        self.assertIn("history-rewritten", [e["kind"] for e in doc["events"]])
        self.assertNotIn("not-ancestor", [e["kind"] for e in doc["events"]])
        self.assertEqual(doc["parameters"]["net"][0]["new_value"], "0.2")
        # a maintainer decision resolves the quarantine
        result = self.up.update(since=rewritten)
        self.assertEqual((result.outcome, result.accepted), ("no-change", rewritten))
        self.assertEqual(load_state(self.up.store)["quarantined"], [])

    def test_license_change_pauses_promotion(self) -> None:
        self.up.update()
        c2 = self.up.commit({**_files("0.4", " one"), "LICENSE": b"Relicensed text.\n"}, [self.c1])
        set_branch(self.up.git_dir, "main", c2)
        result = self.up.update()
        self.assertEqual((result.outcome, result.exit_code, result.accepted), ("attention", 1, self.c1))
        [event] = result.events
        self.assertEqual((event.kind, event.commit), ("license-changed", c2))
        self.assertIn("LICENSE", event.message)
        md = (self.up.out / f"digest-{self.c1[:12]}-{c2[:12]}.md").read_text("utf-8")
        self.assertIn("`license-changed`", md)
        self.assertEqual(load_state(self.up.store)["quarantined"], [c2])
        self.assertEqual(self.up.update(since=c2).accepted, c2)

    def test_deleted_commit_drill(self) -> None:
        feature = self.up.commit(_files("0.9", " feature"), [self.c1])
        set_branch(self.up.git_dir, "feature", feature)
        self.up.update()
        self.up.store.pin(feature, self.up.url, allowlist=self.up.allowlist)
        delete_branch(self.up.git_dir, "feature")
        c2 = self.up.commit(_files("0.4", " two"), [self.c1])
        set_branch(self.up.git_dir, "main", c2)
        result = self.up.update()
        self.assertEqual((result.outcome, result.exit_code, result.accepted), ("updated", 1, c2))
        self.assertEqual([(e.kind, e.commit) for e in result.events],
                         [("commit-unreachable-upstream", feature)])
        self.assertEqual(self.up.store.read_span(feature, "notes.md", 1, 1).data, b"# Notes feature\n")
        md = (self.up.out / f"digest-{self.c1[:12]}-{c2[:12]}.md").read_text("utf-8")
        self.assertIn("`commit-unreachable-upstream`", md)

    def test_pinned_commit_missing_locally_drill(self) -> None:
        self.up.update()
        rewritten = self.up.commit(_files("0.2", " rewritten"), [self.c0])
        set_branch(self.up.git_dir, "main", rewritten)
        shutil.rmtree(self.up.store.mirror_path(self.up.url))  # the local objects are lost
        result = self.up.update()
        self.assertEqual((result.outcome, result.exit_code, result.accepted), ("failed", 1, self.c1))
        self.assertIn(("pinned-commit-missing", self.c1), {(e.kind, e.commit) for e in result.events})
        self.assertEqual(result.written, [])
        self.assertEqual(load_state(self.up.store)["accepted"], self.c1)
        self.assertEqual(self.up.store.get_pin(self.c1).commit, self.c1)  # the pin record is kept

    def test_fetch_failure_drill(self) -> None:
        self.up.update()
        before = sorted(p.commit for p in self.up.store.list_pins())
        moved = self.up.root / "moved.git"
        self.up.git_dir.rename(moved)
        result = self.up.update()
        self.assertEqual((result.outcome, result.exit_code, result.accepted), ("failed", 1, self.c1))
        [event] = result.events
        self.assertEqual(event.kind, "fetch-failure")
        self.assertIn("no claim of 'no changes'", event.message)
        status = self.up.status()
        self.assertFalse(status["observation_succeeded"])
        self.assertEqual(status["digests"], [])
        self.assertEqual(sorted(p.commit for p in self.up.store.list_pins()), before)
        state = load_state(self.up.store)
        self.assertEqual((state["accepted"], state["last_attempt"]["outcome"]), (self.c1, "failed"))

    def test_branch_missing(self) -> None:
        result = self.up.update(branch="release")
        self.assertEqual((result.outcome, result.exit_code), ("failed", 1))
        self.assertEqual([e.kind for e in result.events], ["branch-missing"])

    def test_refused_url_contacts_nothing(self) -> None:
        with mock.patch.object(netguard.subprocess, "run") as run:
            with self.assertRaises(NetworkRefused):
                run_update(self.up.store, out_dir=self.up.out, allowlist=Allowlist(),
                           upstream=self.up.url)
            with self.assertRaises(NetworkRefused):
                run_update(self.up.store, out_dir=self.up.out, allowlist=self.up.allowlist,
                           upstream="https://example.invalid/repo.git")
        run.assert_not_called()
        self.assertFalse(self.up.store.root.exists())
        self.assertFalse(self.up.out.exists())

    def test_out_is_checked_before_anything_is_fetched_or_written(self) -> None:
        """FA-022: the destination rules of ``txray export`` apply to ``update --out``."""
        real = self.up.root / "real"
        real.mkdir()
        link = self.up.root / "link"
        link.symlink_to(real, target_is_directory=True)
        cases = [
            (self.up.store.root / "digests", "inside the snapshot store"),
            (link, "is a symbolic link"),
            (self.up.root / "missing" / "digests", "does not exist"),
        ]
        for out, message in cases:
            with self.subTest(message), mock.patch.object(netguard.subprocess, "run") as run:
                with self.assertRaises(Refused) as caught:
                    run_update(self.up.store, out_dir=out, allowlist=self.up.allowlist,
                               upstream=self.up.url)
                self.assertIn(message, str(caught.exception))
                run.assert_not_called()
        self.assertFalse(self.up.store.root.exists())
        self.assertEqual(list(real.iterdir()), [])
        args = ["update", "--out", str(link), "--upstream", self.up.url, "--store",
                str(self.up.store.root)]
        code, _, err = run_cli(args, {ENV_ALLOW_FILE_URLS: self.up.url})
        self.assertEqual(code, 1)
        self.assertIn(b"is a symbolic link", err)
        self.assertFalse(self.up.store.root.exists())

    def test_the_guarded_fetch_is_the_only_network_call(self) -> None:
        calls = []
        real = update_module.fetch

        def counting(*args: object, **kwargs: object) -> str:
            calls.append(args[0])
            return real(*args, **kwargs)  # type: ignore[arg-type]

        c2 = self.up.commit(_files("0.3"), [self.c1])
        c3 = self.up.commit(_files("0.2"), [c2])
        set_branch(self.up.git_dir, "main", c3)
        self.up.store.pin(self.c0, self.up.url, allowlist=self.up.allowlist)
        with mock.patch.object(update_module, "fetch", counting), \
                mock.patch("timelinexray.snapshot.store.fetch", side_effect=AssertionError("pin fetched")):
            result = self.up.update()
        self.assertEqual(calls, [self.up.url])
        self.assertEqual((result.outcome, result.previous, result.head), ("updated", self.c0, c3))
        doc = json.loads((self.up.out / f"digest-{self.c0[:12]}-{c3[:12]}.json").read_text())
        self.assertEqual(doc["range"]["first_parent_chain"], [self.c0, self.c1, c2, c3])


AUTHOR = Actor("agent-a", "author")
PARAMS = "params/param.rs"


def _finding(finding_id: str, commit: str, path: str, lines: str, anchor: str,
             evidence_class: str = "CODE") -> dict[str, object]:
    return {"finding_id": finding_id, "title": f"Synthetic {finding_id}",
            "claim": "A synthetic claim about the fixture.", "component": "fixture",
            "evidence_class": evidence_class, "status": "SUPPORTED",
            "citations": [{"commit": commit, "path": path, "lines": lines, "anchor": anchor}]}


def _verify_events_at(ledger: Path, target: str) -> list[str]:
    return sorted(event.finding_id for event in Ledger(ledger).read_events()
                  if event.type == "verify" and event.payload.get("target") == target)


class LedgerRefreshTest(unittest.TestCase):
    """AUDIT section 6, item 1: one invocation pins the head, re-anchors every active
    finding on it, writes the digest with affected findings, refreshes the notes and
    reports the review-queue delta with a non-zero exit. Three commits: c0 -> c1 (an
    unrelated note change) -> c2 (ClickWeight 0.4 -> 0.3, which makes F-click STALE)."""

    def setUp(self) -> None:
        self.up = _Upstream()
        self.addCleanup(self.up.cleanup)
        self.c0 = self.up.commit(_files("0.4"), [])
        self.c1 = self.up.commit(_files("0.4", " one"), [self.c0])
        self.c2 = self.up.commit(_files("0.3", " one"), [self.c1])
        set_branch(self.up.git_dir, "main", self.c0)
        self.assertEqual(self.up.update().outcome, "baseline")
        self.ledger = self.up.root / "ledger"
        self.memory = FindingsMemory(Ledger(self.ledger), self.up.store)
        self.memory.add(_finding("F-click", self.c0, PARAMS, "3", "ClickWeight"), AUTHOR)
        self.memory.add(_finding("F-legacy", self.c0, PARAMS, "4", "EnableLegacy"), AUTHOR)
        self.memory.verify()
        self.vault = self.up.root / "vault"
        self.vault.mkdir()
        self.notes = self.vault / "notes"

    def refresh(self, **kwargs: object):
        return self.up.update(ledger=self.ledger, reanchor=True,
                              findings=LedgerFindingsProvider(self.up.store, self.ledger),
                              **kwargs)

    def test_three_commits_from_one_invocation_each(self) -> None:
        set_branch(self.up.git_dir, "main", self.c1)
        result = self.refresh()
        self.assertEqual((result.outcome, result.exit_code, result.head), ("updated", 0, self.c1))
        refresh = result.refresh
        self.assertIsNotNone(refresh)
        self.assertEqual((refresh.target, refresh.selection, refresh.findings, refresh.freshness,
                          refresh.added, refresh.removed),
                         (self.c1, "every active finding", 2, {"CURRENT": 2}, [], []))
        self.assertEqual(_verify_events_at(self.ledger, self.c1), ["F-click", "F-legacy"])
        self.assertEqual((refresh.queue_before, refresh.queue_after), (2, 2))  # the two drafts

        # the STALE-inducing change, with the notes refreshed in the same run
        set_branch(self.up.git_dir, "main", self.c2)
        result = self.refresh(export_dir=self.notes)
        self.assertEqual((result.outcome, result.exit_code, result.head, result.accepted),
                         ("updated", 1, self.c2, self.c2))
        refresh = result.refresh
        self.assertEqual((refresh.findings, refresh.freshness), (2, {"CURRENT": 1, "STALE": 1}))
        self.assertEqual([(i["finding_id"], i["trigger"], i["scope"]) for i in refresh.added],
                         [("F-click", "changed_span", self.c2)])
        self.assertEqual(refresh.removed, [])
        self.assertEqual(_verify_events_at(self.ledger, self.c2), ["F-click", "F-legacy"])
        doc = json.loads((self.up.out / f"digest-{self.c1[:12]}-{self.c2[:12]}.json").read_text())
        [row] = doc["affected_findings"]["findings"]
        self.assertEqual((row["finding_id"], row["freshness"], row["freshness_checked_against"]),
                         ("F-click", "STALE", self.c2))
        status = self.up.status()
        self.assertEqual(status["ledger_refresh"]["freshness"], {"CURRENT": 1, "STALE": 1})
        [added] = status["ledger_refresh"]["review_queue"]["added"]
        self.assertEqual((added["finding_id"], added["trigger"], added["priority"], added["scope"]),
                         ("F-click", "changed_span", 1, self.c2))
        self.assertEqual(status["ledger_refresh"]["ledger"]["head"], self.memory.view().head)
        self.assertNotIn(str(self.up.root), json.dumps(status))
        self.assertEqual(status["export"]["findings"]["exported"], 1)  # F-legacy only
        self.assertEqual(status["export"]["files"]["removed"], 0)
        names = [p.name for p in (self.notes / FINDINGS_DIR).iterdir()]
        self.assertTrue(any("F-legacy" in name for name in names), names)
        self.assertFalse(any("F-click" in name for name in names), names)  # not current

        # a fresh ledger gets nothing appended on the next, unchanged observation
        count = len(Ledger(self.ledger).read_events())
        result = self.refresh(export_dir=self.notes)
        self.assertEqual((result.outcome, result.exit_code), ("no-change", 0))
        refresh = result.refresh
        self.assertEqual((refresh.selection, refresh.findings, refresh.freshness, refresh.added),
                         ("findings without a check at the head", 0, {}, []))
        self.assertEqual(len(Ledger(self.ledger).read_events()), count)
        self.assertEqual((result.export["files"]["written"], result.export["files"]["removed"]),
                         (0, 0))

        # a finding recorded since is checked at the head on the next run (and only it)
        self.memory.add(_finding("F-notes", self.c0, "notes.md", "1", "Notes"), AUTHOR)
        result = self.refresh()
        self.assertEqual((result.outcome, result.exit_code), ("no-change", 1))
        refresh = result.refresh
        self.assertEqual((refresh.findings, refresh.freshness), (1, {"STALE": 1}))
        self.assertEqual([(i["finding_id"], i["trigger"]) for i in refresh.added],
                         [("F-notes", "changed_span")])
        self.assertEqual(_verify_events_at(self.ledger, self.c2),
                         ["F-click", "F-legacy", "F-notes"])
        self.assertEqual(len(Ledger(self.ledger).read_events()), count + 2)  # create + verify

    def test_refusals_happen_before_the_fetch(self) -> None:
        damaged = self.up.root / "damaged"
        shutil.copytree(self.ledger, damaged)
        events = damaged / "events.jsonl"
        events.write_bytes(events.read_bytes().replace(b"F-click", b"F-clack", 1))
        cases = [
            ({"reanchor": True}, Refused, "need a findings ledger"),
            ({"reanchor": True, "ledger": self.up.root / "no-ledger"}, Refused,
             "no findings ledger exists"),
            ({"reanchor": True, "ledger": damaged}, (IntegrityError, Refused), ""),
            ({"ledger": self.ledger, "export_dir": self.up.store.root / "notes"}, Refused,
             "--export"),
            ({"ledger": self.ledger, "export_dir": self.up.root / "missing" / "notes"}, Refused,
             "does not exist"),
        ]
        before = sorted(p.name for p in self.up.store.root.iterdir())
        for kwargs, error, message in cases:
            with self.subTest(message or "damaged"), \
                    mock.patch.object(netguard.subprocess, "run") as run:
                with self.assertRaises(error) as caught:
                    self.up.update(**kwargs)
                self.assertIn(message, str(caught.exception))
                run.assert_not_called()
        self.assertEqual(sorted(p.name for p in self.up.store.root.iterdir()), before)
        self.assertFalse(self.notes.exists())

    def test_command_line(self) -> None:
        set_branch(self.up.git_dir, "main", self.c2)
        args = ["update", "--out", str(self.up.out), "--upstream", self.up.url, "--store",
                str(self.up.store.root), "--ledger", str(self.ledger), "--reanchor",
                "--export", str(self.notes)]
        env = {ENV_ALLOW_FILE_URLS: self.up.url}
        code, out, err = run_cli(args, env)
        self.assertEqual((code, err), (1, b""), out)
        self.assertIn(f"reanchor   {self.c2}: 2 finding(s) re-anchored (every active finding): "
                      "CURRENT 1, STALE 1".encode(), out)
        self.assertIn(b"queue      2 -> 3 open item(s); 1 new, 0 closed", out)
        self.assertIn(b"P1 changed_span", out)
        self.assertIn(b"export     1 finding(s) exported (1 current, 0 not current)", out)
        self.assertIn(b"next       txray findings stale", out)
        self.assertIn(b"txray findings queue lists the items by trigger", out)
        # the stale-review summary names the exact command with this run's locations
        self.assertIn(b"review     1 finding(s) not current at the head (other 1): txray findings "
                      b"stale --target " + self.c2[:12].encode() + b" --store "
                      + str(self.up.store.root).encode() + b" --ledger "
                      + str(self.ledger).encode(), out)
        self.assertNotIn(b"reanchor --latest", out)
        code, out, _ = run_cli([*args, "--json"], env)
        doc = json.loads(out)
        self.assertEqual((code, doc["outcome"], doc["data"]["outcome"]), (0, "ok", "no-change"))
        self.assertEqual(doc["data"]["ledger_refresh"]["findings"], 0)
        stale = doc["data"]["ledger_refresh"]["stale_review"]
        self.assertEqual((stale["counts"]["not_current"], stale["counts"]["freshness"]),
                         (1, {"CURRENT": 1, "STALE": 1}))
        self.assertEqual(stale["next"], f"txray findings stale --target {self.c2[:12]}")
        self.assertEqual(doc["data"]["export"]["files"]["written"], 0)
        self.assertEqual(doc["warnings"], [])
        code, out, _ = run_cli(args[:-3], env)  # neither flag: nothing is re-anchored
        self.assertEqual(code, 0)
        self.assertNotIn(b"reanchor   ", out)


UPSTREAM = upstream_git_dir()
OLD = "4c5cfe8f07f1c76d4f04277e803f20e6039f5191"
NEW = "a707cc27ba36d3fa79450c9cffcc48a82d080b02"
LAST = "77d431aabf409ca1c1eed9bec7e2183f7c914e23"


@unittest.skipIf(UPSTREAM is None, "no local x-algorithm clone with 77d431a "
                 "(set TXRAY_TEST_UPSTREAM to a local clone)")
class UpstreamLedgerRefreshTest(unittest.TestCase):
    """The same flow on the real history (a bare copy of the local clone, branch moved by
    hand): a ClickWeight finding at 4c5cfe8 goes STALE at a707cc2 (public default 0.4 ->
    0.3, line 322 -> 329) and again at 77d431a, one invocation per head."""

    def test_click_weight_finding_goes_stale(self) -> None:
        assert UPSTREAM is not None
        tmp = tempfile.TemporaryDirectory(prefix="txray-update-upstream-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        mirror = root / "upstream.git"
        subprocess.run(["git", "clone", "--bare", "--quiet", str(UPSTREAM), str(mirror)],
                       check=True, capture_output=True, env=git_env())
        run_git(mirror, "update-ref", "refs/heads/main", OLD)
        url = file_url(mirror)
        allowlist = Allowlist([url])
        store = SnapshotStore(root / "store")
        out = root / "out"
        ledger = root / "ledger"

        def update(**kwargs: object):
            return run_update(store, out_dir=out, allowlist=allowlist, upstream=url,
                              clock=lambda: CLOCK, **kwargs)  # type: ignore[arg-type]

        def refresh():
            return update(ledger=ledger, reanchor=True,
                          findings=LedgerFindingsProvider(store, ledger))

        self.assertEqual(update().outcome, "baseline")
        memory = FindingsMemory(Ledger(ledger), store)
        memory.add({
            "finding_id": "F-click", "title": "ClickWeight public default",
            "claim": "At 4c5cfe8 the ClickWeight param! declaration has public default 0.4.",
            "component": "home-mixer", "evidence_class": "PARAM_DEFAULT", "status": "SUPPORTED",
            "citations": [{"commit": OLD, "path": "home-mixer/params/param.rs", "lines": "322",
                           "anchor": "ClickWeight"}],
        }, AUTHOR)
        memory.verify()

        run_git(mirror, "update-ref", "refs/heads/main", NEW)
        result = refresh()
        self.assertEqual((result.outcome, result.exit_code, result.head), ("updated", 1, NEW))
        self.assertEqual(result.refresh.freshness, {"STALE": 1})
        self.assertEqual([(i["finding_id"], i["trigger"], i["scope"]) for i in result.refresh.added],
                         [("F-click", "changed_span", NEW)])
        self.assertEqual(_verify_events_at(ledger, NEW), ["F-click"])
        doc = json.loads((out / f"digest-{OLD[:12]}-{NEW[:12]}.json").read_text("utf-8"))
        [row] = doc["affected_findings"]["findings"]
        self.assertEqual((row["finding_id"], row["freshness"], row["freshness_checked_against"]),
                         ("F-click", "STALE", NEW))
        self.assertTrue(any(p["name"] == "ClickWeight" and (p["old_value"], p["new_value"])
                            == ("0.4", "0.3") for p in doc["parameters"]["net"]))

        # a707cc2..77d431a changes a NOTICE file upstream: the head is pinned but quarantined
        # (license-changed, attention); the ledger is re-anchored on it all the same
        run_git(mirror, "update-ref", "refs/heads/main", LAST)
        result = refresh()
        self.assertEqual((result.outcome, result.exit_code, result.head, result.accepted),
                         ("attention", 1, LAST, NEW))
        self.assertEqual([e.kind for e in result.events], ["license-changed"])
        self.assertEqual(result.refresh.freshness, {"STALE": 1})
        self.assertEqual([i["scope"] for i in result.refresh.added], [LAST])
        state = memory.get("F-click")
        self.assertEqual(sorted(state.targets), sorted([NEW, LAST]))
        self.assertEqual(sorted(i["scope"] for i in state.queue_items()
                                if i["trigger"] == "changed_span"), sorted([NEW, LAST]))

        # observed again while quarantined: attention again, but the ledger is fresh at the
        # head, so nothing is re-anchored and no review item appears
        count = len(Ledger(ledger).read_events())
        result = refresh()
        self.assertEqual((result.outcome, result.exit_code, result.refresh.findings,
                          result.refresh.added), ("attention", 1, 0, []))
        self.assertEqual(len(Ledger(ledger).read_events()), count)
        # the maintainer accepts the head: no change, nothing to re-anchor, exit 0
        result = update(since=LAST, ledger=ledger, reanchor=True,
                        findings=LedgerFindingsProvider(store, ledger))
        self.assertEqual((result.outcome, result.exit_code, result.accepted,
                          result.refresh.findings), ("no-change", 0, LAST, 0))


class UpdateCommandTest(unittest.TestCase):
    def test_exit_codes_and_json(self) -> None:
        up = _Upstream()
        self.addCleanup(up.cleanup)
        c0 = up.commit(_files("0.4"), [])
        set_branch(up.git_dir, "main", c0)
        args = ["update", "--out", str(up.out), "--upstream", up.url, "--store", str(up.store.root)]
        code, out, err = run_cli(args, {ENV_ALLOW_FILE_URLS: up.url})
        self.assertEqual((code, err), (0, b""))
        self.assertIn(b"outcome    baseline", out)
        c1 = up.commit(_files("0.3"), [c0])
        set_branch(up.git_dir, "main", c1)
        code, out, _ = run_cli([*args, "--json"], {ENV_ALLOW_FILE_URLS: up.url})
        doc = json.loads(out)
        self.assertEqual((code, doc["command"], doc["outcome"], doc["data"]["outcome"]),
                         (0, "update", "ok", "updated"))
        code, _, err = run_cli(args, {ENV_ALLOW_FILE_URLS: ""})
        self.assertEqual(code, 3)
        up.git_dir.rename(up.root / "elsewhere.git")
        code, out, _ = run_cli(args, {ENV_ALLOW_FILE_URLS: up.url})
        self.assertEqual(code, 1)
        self.assertIn(b"fetch-failure", out)
        code, _, _ = run_cli(["update", "--store", str(up.store.root)])
        self.assertEqual(code, 2)  # --out is required


if __name__ == "__main__":
    unittest.main()
