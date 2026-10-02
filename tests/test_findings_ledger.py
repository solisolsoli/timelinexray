"""The findings event log: SHA-256 hash chain, HEAD, tamper/reorder/truncation detection."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from timelinexray.errors import IntegrityError, Refused
from timelinexray.findings import Actor, Ledger, verify_log
from timelinexray.findings import model
from timelinexray.findings.ledger import (
    _EVENT_KEYS,
    EVENT_SCHEMA,
    EVENTS_FILE,
    GENESIS,
    HEAD_FILE,
    compute_hash,
)
from timelinexray.findings.model import canonical_json
from tests.support import REPO_ROOT, run_cli

AUTHOR = Actor("agent-a", "author")


def _record(finding_id: str) -> dict:
    return {"finding_id": finding_id, "status": "SUPPORTED", "sources": [],
            "title": f"Synthetic {finding_id}"}


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-ledger-")
        self.dir = Path(self._tmp.name) / "ledger"
        self.ledger = Ledger(self.dir, clock=lambda: "2026-01-01T00:00:00Z")
        with self.ledger.writer() as writer:
            for number in range(1, 5):
                writer.append("create", AUTHOR, f"F-{number}", {"record": _record(f"F-{number}")})
        self.events = self.dir / EVENTS_FILE

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def lines(self) -> list[bytes]:
        return self.events.read_bytes().splitlines(keepends=True)

    def codes(self) -> set[str]:
        return {problem["code"] for problem in verify_log(self.dir).problems}

    def test_head_has_the_mode_of_the_log_and_keeps_a_changed_mode(self) -> None:
        """HEAD went through ``mkstemp`` and so was always 0600 next to a 0644 log."""
        def mode(path: Path) -> int:
            return os.stat(path).st_mode & 0o777

        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        head = self.dir / HEAD_FILE
        with self.ledger.writer() as writer:
            writer.append("create", AUTHOR, "F-9", {"record": _record("F-9")})
        self.assertEqual((mode(head), mode(self.events)), (0o644, 0o644))
        head.chmod(0o640)
        with self.ledger.writer() as writer:
            writer.append("create", AUTHOR, "F-10", {"record": _record("F-10")})
        self.assertEqual(mode(head), 0o640)
        self.assertTrue(verify_log(self.dir).ok)

    def test_a_clean_log_verifies(self) -> None:
        report = verify_log(self.dir)
        self.assertTrue(report.ok, report.problems)
        self.assertEqual(len(report.events), 4)
        self.assertEqual(report.events[0].prev, GENESIS)
        for before, after in zip(report.events, report.events[1:]):
            self.assertEqual(after.prev, before.hash)
            self.assertEqual(after.seq, before.seq + 1)
        head = json.loads((self.dir / HEAD_FILE).read_text())
        self.assertEqual(head, {"hash": report.events[-1].hash, "seq": 4})

    def test_every_line_is_canonical_and_self_hashing(self) -> None:
        for raw in self.lines():
            obj = json.loads(raw)
            self.assertEqual(raw.rstrip(b"\n"), canonical_json(obj))
            body = {key: value for key, value in obj.items() if key != "hash"}
            self.assertEqual(compute_hash(body), obj["hash"])

    def test_a_changed_byte_is_detected(self) -> None:
        lines = self.lines()
        lines[1] = lines[1].replace(b"Synthetic F-2", b"Synthetic F-X")
        self.events.write_bytes(b"".join(lines))
        report = verify_log(self.dir)
        self.assertIn("hash_mismatch", {p["code"] for p in report.problems})
        self.assertEqual([p["line"] for p in report.problems if p["code"] == "hash_mismatch"], [2])

    def test_a_recomputed_hash_breaks_the_chain(self) -> None:
        lines = self.lines()
        obj = json.loads(lines[1])
        obj["payload"]["record"]["title"] = "forged"
        body = {key: value for key, value in obj.items() if key != "hash"}
        obj["hash"] = compute_hash(body)
        lines[1] = canonical_json(obj) + b"\n"
        self.events.write_bytes(b"".join(lines))
        self.assertIn("chain_broken", self.codes())

    def test_reordered_events_are_detected(self) -> None:
        lines = self.lines()
        lines[1], lines[2] = lines[2], lines[1]
        self.events.write_bytes(b"".join(lines))
        self.assertTrue({"seq_mismatch", "chain_broken"} <= self.codes())

    def test_a_truncated_tail_is_detected(self) -> None:
        self.events.write_bytes(b"".join(self.lines()[:-1]))
        report = verify_log(self.dir)
        self.assertEqual([p["code"] for p in report.problems], ["truncated"])

    def test_a_removed_first_event_is_detected(self) -> None:
        self.events.write_bytes(b"".join(self.lines()[1:]))
        self.assertTrue({"seq_mismatch", "chain_broken"} <= self.codes())

    def test_a_torn_final_write_is_detected(self) -> None:
        data = self.events.read_bytes()
        self.events.write_bytes(data[:-40])
        self.assertTrue({"torn_write", "malformed"} <= self.codes())

    def test_an_appended_line_without_head_update_is_detected(self) -> None:
        lines = self.lines()
        self.events.write_bytes(b"".join(lines) + lines[-1])
        self.assertTrue({"seq_mismatch", "chain_broken", "head_mismatch"} <= self.codes())

    def crash_window(self) -> None:
        """Append one chain-valid event and leave HEAD behind (a crash in ``_append``)."""
        last = json.loads(self.lines()[-1])
        body = {key: value for key, value in last.items() if key != "hash"}
        body.update(seq=last["seq"] + 1, prev=last["hash"], finding_id="F-5",
                    payload={"record": _record("F-5")})
        with open(self.events, "ab") as handle:
            handle.write(canonical_json({**body, "hash": compute_hash(body)}) + b"\n")

    def test_repair_head_recovers_the_crash_window(self) -> None:
        self.crash_window()
        self.assertEqual(self.codes(), {"head_mismatch"})
        with self.assertRaises(IntegrityError):
            self.ledger.events()
        result = self.ledger.repair_head()
        self.assertEqual((result["repaired"], result["previous"]["seq"], result["head"]["seq"]),
                         (True, 4, 5))
        report = verify_log(self.dir)
        self.assertTrue(report.ok, report.problems)
        self.assertEqual([event.finding_id for event in self.ledger.events()][-1], "F-5")
        again = self.ledger.repair_head()
        self.assertEqual((again["repaired"], again["reason"]),
                         (False, "HEAD already matches the log"))
        (self.dir / HEAD_FILE).unlink()
        self.assertTrue(self.ledger.repair_head()["repaired"])
        self.assertTrue(verify_log(self.dir).ok)
        (self.dir / HEAD_FILE).write_text("not json\n")
        self.assertEqual(self.codes(), {"head_malformed"})
        self.assertTrue(self.ledger.repair_head()["repaired"])
        self.assertTrue(verify_log(self.dir).ok)

    def test_a_real_crash_in_the_window_is_recoverable(self) -> None:
        """The crash window through the real write path (audit section 6, item 12): a
        writer process dies (``os._exit``, no cleanup) after the event is appended and
        synced but before ``HEAD`` is replaced. The log stays chain-valid, only
        ``head_mismatch`` is reported, nothing reads or extends the ledger until
        ``repair_head``, and afterwards the next write continues the chain."""
        crash = (
            "import os, sys\n"
            "from timelinexray.findings import Actor, Ledger\n"
            "from timelinexray.findings import ledger as ledger_module\n"
            "real = os.replace\n"
            "def replace(src, dst, *args, **kwargs):\n"
            "    if os.path.basename(dst) == 'HEAD':\n"
            "        os._exit(17)  # the process dies inside the crash window\n"
            "    return real(src, dst, *args, **kwargs)\n"
            "ledger_module.os.replace = replace\n"
            "ledger = Ledger(sys.argv[1], clock=lambda: '2026-01-01T00:00:00Z')\n"
            "record = {'finding_id': 'F-5', 'status': 'SUPPORTED', 'sources': [],\n"
            "          'title': 'Synthetic F-5'}\n"
            "with ledger.writer() as writer:\n"
            "    writer.append('create', Actor('agent-a', 'author'), 'F-5', {'record': record})\n"
            "sys.exit(0)\n"
        )
        head_before = (self.dir / HEAD_FILE).read_bytes()
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
        proc = subprocess.run([sys.executable, "-c", crash, str(self.dir)], env=env,
                              capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 17, proc.stderr)
        self.assertEqual((self.dir / HEAD_FILE).read_bytes(), head_before)
        self.assertEqual(len(self.lines()), 5)
        self.assertEqual(self.codes(), {"head_mismatch"})
        with self.assertRaises(IntegrityError):
            self.ledger.events()
        with self.assertRaises(IntegrityError):
            with self.ledger.writer() as writer:
                writer.append("create", AUTHOR, "F-6", {"record": _record("F-6")})
        self.assertEqual(len(self.lines()), 5)  # the refused write appended nothing
        result = self.ledger.repair_head()
        self.assertEqual((result["repaired"], result["head"]["seq"]), (True, 5))
        with self.ledger.writer() as writer:
            writer.append("create", AUTHOR, "F-6", {"record": _record("F-6")})
        report = verify_log(self.dir)
        self.assertTrue(report.ok, report.problems)
        self.assertEqual([event.finding_id for event in report.events][-2:], ["F-5", "F-6"])
        self.assertEqual(report.events[-1].prev, report.events[-2].hash)

    def test_repair_head_refuses_everything_but_a_head_behind_an_intact_log(self) -> None:
        lines = self.lines()
        head = (self.dir / HEAD_FILE).read_text()
        cases = {
            "hash_mismatch": (lines[0].replace(b"Synthetic", b"Synthetiq") + b"".join(lines[1:]),
                              head),
            "truncated": (b"".join(lines[:-1]), head),
            "torn_write": (b"".join(lines)[:-30], head),
            "same count, other hash": (b"".join(lines), head.replace(head[10:14], "beef")),
        }
        for name, (data, head_text) in cases.items():
            with self.subTest(name=name):
                self.events.write_bytes(data)
                (self.dir / HEAD_FILE).write_text(head_text)
                with self.assertRaises(Refused):
                    self.ledger.repair_head()
                self.assertEqual((self.events.read_bytes(), (self.dir / HEAD_FILE).read_text()),
                                 (data, head_text))  # nothing was changed
        self.events.write_bytes(b"".join(lines))
        (self.dir / HEAD_FILE).write_text(head)
        self.assertTrue(verify_log(self.dir).ok)

    def test_repair_head_command(self) -> None:
        self.crash_window()
        code, _, _ = run_cli(["findings", "verify-log", "--ledger", str(self.dir)])
        self.assertEqual(code, 1)
        code, out, _ = run_cli(["findings", "verify-log", "--ledger", str(self.dir),
                                "--repair-head"])
        self.assertEqual(code, 0)
        self.assertIn(b"repair     HEAD rewritten", out)
        self.assertIn(b"chain      ok", out)
        code, out, _ = run_cli(["findings", "verify-log", "--ledger", str(self.dir),
                                "--repair-head", "--json"])
        data = json.loads(out)["data"]
        self.assertEqual((code, data["ok"], data["repair"]["repaired"]), (0, True, False))
        lines = self.lines()
        self.events.write_bytes(lines[0].replace(b"Synthetic", b"Synthetiq") + b"".join(lines[1:]))
        code, _, err = run_cli(["findings", "verify-log", "--ledger", str(self.dir),
                                "--repair-head"])
        self.assertEqual(code, 1)
        self.assertIn(b"only a HEAD that fell behind an intact log", err)

    def test_missing_head_and_expected_head(self) -> None:
        last = verify_log(self.dir).last_hash
        self.assertTrue(verify_log(self.dir, expect_head=last).ok)
        self.assertIn("unexpected_head", {p["code"] for p in
                                          verify_log(self.dir, expect_head="0" * 64).problems})
        (self.dir / HEAD_FILE).unlink()
        self.assertEqual(self.codes(), {"head_missing"})

    def test_a_damaged_log_is_never_read_or_extended(self) -> None:
        lines = self.lines()
        lines[0] = lines[0].replace(b"Synthetic", b"Synthetiq")
        self.events.write_bytes(b"".join(lines))
        with self.assertRaises(IntegrityError):
            self.ledger.events()
        with self.assertRaises(IntegrityError):
            with self.ledger.writer() as writer:
                writer.append("create", AUTHOR, "F-9", {"record": _record("F-9")})
        self.assertEqual(self.events.read_bytes(), b"".join(lines))

    def test_expected_head_is_a_compare_and_swap(self) -> None:
        head = verify_log(self.dir).last_hash
        with self.assertRaises(Refused):
            with self.ledger.writer(expect_head="f" * 64) as writer:
                writer.append("create", AUTHOR, "F-9", {"record": _record("F-9")})
        with self.ledger.writer(expect_head=head) as writer:
            writer.append("create", AUTHOR, "F-9", {"record": _record("F-9")})
        self.assertEqual(len(self.ledger.events()), 5)

    def test_an_operation_appends_all_or_nothing(self) -> None:
        before = self.events.read_bytes()
        with self.assertRaises(RuntimeError):
            with self.ledger.writer() as writer:
                writer.append("create", AUTHOR, "F-7", {"record": _record("F-7")})
                writer.append("create", AUTHOR, "F-8", {"record": _record("F-8")})
                raise RuntimeError("interrupted")
        self.assertEqual(self.events.read_bytes(), before)
        self.assertTrue(verify_log(self.dir).ok)

    def test_verify_log_command_exit_codes(self) -> None:
        code, out, _ = run_cli(["findings", "verify-log", "--ledger", str(self.dir),
                                "--store", str(self.dir.parent / "store")])
        self.assertEqual(code, 0)
        self.assertIn(b"chain      ok", out)
        self.events.write_bytes(b"".join(self.lines()[:-1]))
        code, out, _ = run_cli(["findings", "verify-log", "--ledger", str(self.dir), "--json",
                                "--store", str(self.dir.parent / "store")])
        self.assertEqual(code, 1)
        doc = json.loads(out)
        self.assertEqual(doc["outcome"], "failed")
        self.assertEqual(doc["data"]["problems"][0]["code"], "truncated")

    def test_the_event_schema_matches_the_code(self) -> None:
        schema = json.loads((REPO_ROOT / "schemas" / "findings-event.schema.json").read_text())
        self.assertEqual(schema["$id"], EVENT_SCHEMA)
        self.assertEqual(set(schema["required"]), set(_EVENT_KEYS))
        self.assertEqual(tuple(schema["properties"]["type"]["enum"]), model.EVENT_TYPES)
        actor = schema["properties"]["actor"]["properties"]
        self.assertEqual(tuple(actor["role"]["enum"]), model.ROLES)
        self.assertEqual(actor["name"]["pattern"], "^" + model._NAME.pattern + "$")
        self.assertEqual(schema["properties"]["finding_id"]["pattern"],
                         "^" + model._ID.pattern + "$")
        for raw in self.lines():
            event = json.loads(raw)
            self.assertEqual(set(event), set(schema["required"]))

    def test_an_empty_ledger_verifies(self) -> None:
        report = verify_log(Path(self._tmp.name) / "nothing")
        self.assertTrue(report.ok)
        self.assertEqual((len(report.events), report.last_hash), (0, GENESIS))


if __name__ == "__main__":
    unittest.main()
