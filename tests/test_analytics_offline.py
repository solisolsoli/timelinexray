"""The analytics process is offline, writes only where declared, and is isolated from the rest.

Analytics commands are run in child processes: entering offline mode is irreversible, and
the test runner itself must keep git and subprocesses available for the other suites.
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from timelinexray import metrics_cli
from timelinexray.analytics import dataset as analytics_dataset
from timelinexray.analytics import offline
from timelinexray.snapshot.store import default_store_root
from tests.analytics_support import (CANONICAL, EN_HEADERS, post, run_python, run_txray,
                                     write_export)
from tests.support import REPO_ROOT

SRC = REPO_ROOT / "src" / "timelinexray"
ANALYTICS = SRC / "analytics"

#: Runs ``txray <argv>`` in this process through the real entry point, then tries to reach
#: the network, start a process and write a file; prints what happened as JSON on stderr.
PROBE = r"""
import json, os, runpy, socket, sys
events = []
def observe(event, args):
    if event in ("import", "timelinexray.analytics.offline.enter"):
        events.append([event, str(args[0]) if args else ""])
sys.addaudithook(observe)
result = {}
try:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()
    result["socket_before"] = "allowed"
except Exception as exc:
    result["socket_before"] = type(exc).__name__
sys.argv = ["txray", *json.loads(os.environ["PROBE_ARGV"])]
try:
    runpy.run_module("timelinexray", run_name="__main__", alter_sys=True)
except SystemExit as exc:
    result["exit"] = exc.code
attempts = {
    "socket": lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM),
    "connect": lambda: socket.create_connection(("127.0.0.1", 9), timeout=1),
    "resolve": lambda: socket.getaddrinfo("localhost", 443),
    "unix_socket": lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM),
    "process": lambda: __import__("subprocess").run(["true"]),
    "system": lambda: os.system("true"),
    "ctypes": lambda: __import__("ctypes").CDLL(None),
    "write": lambda: open(os.environ["PROBE_WRITE"], "w"),
}
for name, attempt in attempts.items():
    try:
        attempt()
        result[name] = "allowed"
    except Exception as exc:
        result[name] = type(exc).__name__
result["events"] = events
print(json.dumps(result), file=sys.stderr)
"""

#: Enters offline mode directly and tries writes inside and outside the allowed directory.
WRITES = r"""
import json, os, sys
from timelinexray.analytics import offline
allowed, store, outside = sys.argv[1:4]
offline.enter(allowed_write_roots=[allowed], forbidden_roots=[store])
results = {}
def attempt(name, fn):
    try:
        fn()
        results[name] = "allowed"
    except Exception as exc:
        results[name] = type(exc).__name__
attempt("write_allowed", lambda: open(os.path.join(allowed, "ok.txt"), "w").close())
attempt("mkdir_allowed", lambda: os.mkdir(os.path.join(allowed, "sub")))
attempt("read_outside", lambda: open(sys.executable, "rb").close())
attempt("write_store", lambda: open(os.path.join(store, "x.json"), "w"))
attempt("append_store", lambda: open(os.path.join(store, "x.json"), "a"))
attempt("os_open_store", lambda: os.open(os.path.join(store, "y"), os.O_WRONLY | os.O_CREAT))
attempt("mkdir_store", lambda: os.mkdir(os.path.join(store, "d")))
attempt("rename_into_store", lambda: os.replace(os.path.join(allowed, "ok.txt"),
                                               os.path.join(store, "moved.txt")))
attempt("write_outside", lambda: open(os.path.join(outside, "stray.txt"), "w"))
attempt("remove_outside", lambda: os.remove(os.path.join(outside, "keep.txt")))
attempt("chmod_outside", lambda: os.chmod(os.path.join(outside, "keep.txt"), 0o777))
attempt("symlink_outside", lambda: os.symlink(allowed, os.path.join(outside, "link")))
attempt("reenter_other_roots", lambda: offline.enter(allowed_write_roots=[outside]))
# writers that create files in C without an audited open (FA-019): refused outright
import sqlite3
attempt("sqlite_outside", lambda: sqlite3.connect(os.path.join(outside, "x.db")))
attempt("sqlite_allowed", lambda: sqlite3.connect(os.path.join(allowed, "x.db")))
attempt("sqlite_attach", lambda: sqlite3.connect(":memory:").execute(
    "ATTACH DATABASE ? AS a", (os.path.join(outside, "a.db"),)))
attempt("dbm_outside", lambda: __import__("dbm").open(os.path.join(outside, "z"), "c"))
attempt("dbm_ndbm", lambda: __import__("dbm.ndbm"))
attempt("bytecode_off", lambda: None if sys.dont_write_bytecode else 1 / 0)
print(json.dumps(results))
"""


class _Tmp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-offline-")
        self.tmp = Path(self._tmp.name)
        self.store = self.tmp / "store"
        self.store.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def dataset(self) -> Path:
        export = write_export(self.tmp / "export.csv",
                              [post("1000000000000000001", impressions=10000, likes=200,
                                    replies=10, reposts=15)], lang="tr")
        proc = run_txray(["metrics", "import", str(export), "--schema", "x-post-v1", "--lang",
                          "tr", "--out", str(self.tmp / "ds"), "--captured-at",
                          "2026-09-30T21:00:00+09:00", "--counts", "cumulative", "--scope",
                          "combined", "--store", str(self.store)])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        return self.tmp / "ds"


class OfflineProcessTest(_Tmp):
    def probe(self, argv: list[str]) -> dict:
        proc = run_python(PROBE, env={"PROBE_ARGV": json.dumps(argv),
                                      "PROBE_WRITE": str(self.tmp / "stray.txt")})
        lines = proc.stderr.decode().strip().splitlines()
        self.assertTrue(lines, proc.stderr.decode())
        return json.loads(lines[-1])

    def test_network_attempt_fails_inside_a_metrics_process(self) -> None:
        dataset = self.dataset()
        result = self.probe(["metrics", "rwe", str(dataset), "--json", "--store",
                             str(self.store)])
        self.assertEqual(result["socket_before"], "allowed")  # the guard, not the environment
        self.assertEqual(result["exit"], 0)
        for name in ("socket", "connect", "resolve", "unix_socket", "process", "system",
                     "ctypes"):
            with self.subTest(attempt=name):
                self.assertEqual(result[name], "OfflineViolation")
        self.assertEqual(result["write"], "WriteRefused")
        self.assertFalse((self.tmp / "stray.txt").exists())

    def test_guard_is_active_before_any_analytics_code_is_imported(self) -> None:
        dataset = self.dataset()
        for argv in (["metrics", "rwe", str(dataset)],
                     ["metrics", "reach", str(dataset), "--horizon", "24h"]):
            with self.subTest(command=argv[1]):
                events = self.probe([*argv, "--store", str(self.store)])["events"]
                enter = [event for event, _ in events].index(offline.ENTER_EVENT)
                before = {name for event, name in events[:enter] if event == "import"
                          and name.startswith("timelinexray.analytics")}
                after = {name for event, name in events[enter:] if event == "import"
                         and name.startswith("timelinexray.analytics.")}
                self.assertEqual(before, {"timelinexray.analytics",
                                          "timelinexray.analytics.offline"})
                self.assertIn("timelinexray.analytics.commands", after)
                self.assertIn("timelinexray.analytics.dataset", after)

    def test_other_commands_never_enter_offline_mode(self) -> None:
        result = self.probe(["--version"])
        self.assertEqual((result["exit"], result["socket"], result["process"]),
                         (0, "allowed", "allowed"))

    def test_writes_are_confined_to_the_declared_directory(self) -> None:
        allowed, outside = self.tmp / "allowed", self.tmp / "outside"
        allowed.mkdir()
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", "utf-8")
        proc = run_python(WRITES, str(allowed), str(self.store), str(outside))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        results = json.loads(proc.stdout)
        allowed_ops = {"write_allowed", "mkdir_allowed", "read_outside", "bytecode_off"}
        for name, outcome in results.items():
            with self.subTest(operation=name):
                if name in allowed_ops:
                    self.assertEqual(outcome, "allowed")
                elif name == "reenter_other_roots":
                    self.assertEqual(outcome, "RuntimeError")
                else:
                    self.assertEqual(outcome, "WriteRefused")
        self.assertEqual(sorted(p.name for p in self.store.iterdir()), [])
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["keep.txt"])

    def test_enter_refuses_an_allowed_root_inside_a_forbidden_one(self) -> None:
        code = ("import sys\nfrom timelinexray.analytics import offline\n"
                "try:\n    offline.enter(allowed_write_roots=[sys.argv[1]], "
                "forbidden_roots=[sys.argv[2]])\nexcept offline.WriteRefused:\n"
                "    print('refused', offline.is_active())\n")
        proc = run_python(code, str(self.store / "ds"), str(self.store))
        self.assertEqual(proc.stdout.decode().strip(), "refused False")


class MetricsCliTest(_Tmp):
    def test_import_rwe_and_reach_end_to_end(self) -> None:
        dataset = self.dataset()
        proc = run_txray(["metrics", "rwe", str(dataset), "--json", "--store", str(self.store)])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        doc = json.loads(proc.stdout)
        self.assertEqual((doc["command"], doc["outcome"]), ("metrics rwe", "ok"))
        (row,) = doc["data"]["posts"]
        self.assertEqual((row["post_id"], row["rwe"]), ("1000000000000000001", 16.5))
        self.assertTrue(doc["data"]["offline"]["active"])
        self.assertEqual(doc["data"]["offline"]["writes_allowed_in"], [])

        text = run_txray(["metrics", "rwe", str(dataset), "--store", str(self.store)])
        self.assertEqual(text.returncode, 0)
        out = text.stdout.decode()
        self.assertTrue(out.startswith("This is not a reach prediction."))
        self.assertIn("RWE 16.5", out)
        self.assertIn("likes 200 x 0.5 -> 10", out)
        self.assertIn("not_interested_score", out)
        self.assertIn("public default at 77d431aabf409ca1c1eed9bec7e2183f7c914e23", out)

        reach = run_txray(["metrics", "reach", str(dataset), "--horizon", "72h", "--json",
                           "--store", str(self.store)])
        self.assertEqual(reach.returncode, 0, reach.stderr.decode())
        (row,) = json.loads(reach.stdout)["data"]["posts"]
        self.assertEqual(row["rr_reason"], "no-pre-publication-baseline")

    def test_dump_header_reads_no_data_row_and_writes_nothing(self) -> None:
        """``--dump-header`` (audit section 6, item 11): the header row as exported, each
        name normalised and its field; no data row is read (a malformed one is not even
        noticed), nothing is written, and no count or id leaves the process."""
        canary = "9" * 18 + "7"
        export = write_export(self.tmp / "export.csv", [post(canary, impressions=12345, likes=6)])
        with open(export, "a", encoding="utf-8", newline="") as handle:
            handle.write('"unterminated quote,' + canary + "\n")  # a full import rejects this row
        before = sorted(path.name for path in self.tmp.iterdir())
        base = ["metrics", "import", str(export), "--schema", "x-post-v1", "--lang", "en",
                "--dump-header", "--store", str(self.store)]
        proc = run_txray(base)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        out = proc.stdout.decode()
        self.assertIn("header table x-post-v1/en/v1  status EXTERNAL_RECHECK", out)
        self.assertIn("columns    17 in the header row: 17 mapped, 0 unmapped; 0 missing field(s)",
                      out)
        self.assertIn("column   1  'Post id'  normalised 'post id'  ->  post_id", out)
        self.assertIn("column  17  'Permalink Clicks'  normalised 'permalink clicks'  ->  "
                      "permalink_clicks", out)
        self.assertIn("warning    header alias table x-post-v1/en/v1 has status EXTERNAL_RECHECK",
                      out)
        self.assertIn("no data row was read and nothing was written", out)
        for secret in (canary, "12345", "synthetic post", "unterminated"):
            self.assertNotIn(secret, out)
        proc = run_txray([*base, "--json"])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        doc = json.loads(proc.stdout)
        self.assertEqual((doc["command"], doc["outcome"]), ("metrics import", "ok"))
        header = doc["data"]["header"]
        self.assertEqual(header["original"], list(EN_HEADERS))
        self.assertEqual(header["normalized"][:2], ["post id", "date"])
        self.assertEqual([column["field"] for column in header["columns"]], list(CANONICAL))
        self.assertEqual((header["alias_table"], header["alias_status"], header["confirmed_by"]),
                         ("x-post-v1/en/v1", "EXTERNAL_RECHECK", None))
        self.assertEqual((header["missing_fields"], header["unmapped_headers"]), ([], []))
        self.assertEqual(header["matching_languages"], {"en": 17, "tr": 0})
        self.assertEqual((doc["data"]["data_rows_read"], doc["data"]["written"]), (0, []))
        self.assertTrue(doc["data"]["offline"]["active"])
        self.assertEqual(doc["data"]["offline"]["writes_allowed_in"], [])
        self.assertNotIn(canary, proc.stdout.decode())
        self.assertEqual(sorted(path.name for path in self.tmp.iterdir()), before)
        self.assertEqual(list(self.store.iterdir()), [])
        # the other language: every column unmapped, every field missing, and the hint
        proc = run_txray([*base[:6], "tr", *base[7:]])
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        out = proc.stdout.decode()
        self.assertIn("17 in the header row: 0 mapped, 17 unmapped; 17 missing field(s)", out)
        self.assertIn("->  (unmapped)", out)
        self.assertIn("try --lang en", out)
        # a full import still needs --out and --captured-at
        proc = run_txray(["metrics", "import", str(export), "--schema", "x-post-v1", "--lang",
                          "en", "--store", str(self.store)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"required: --out, --captured-at", proc.stderr)
        self.assertEqual(sorted(path.name for path in self.tmp.iterdir()), before)

    def test_output_inside_the_store_is_refused(self) -> None:
        export = write_export(self.tmp / "export.csv", [post("1", impressions=1)])
        base = ["metrics", "import", str(export), "--schema", "x-post-v1", "--lang", "en",
                "--captured-at", "2026-09-30T12:00:00Z"]
        by_flag = run_txray([*base, "--out", str(self.store / "ds"), "--store", str(self.store)])
        self.assertEqual(by_flag.returncode, 1)
        self.assertIn(b"overlaps the snapshot store", by_flag.stderr)
        by_env = run_txray([*base, "--out", str(self.store / "ds2"), "--json"],
                           env={"TXRAY_STORE": str(self.store)})
        self.assertEqual(json.loads(by_env.stdout)["error"]["code"], "write_refused")
        around = run_txray([*base, "--out", str(self.tmp), "--store", str(self.store)])
        self.assertEqual(around.returncode, 1)
        self.assertEqual(list(self.store.iterdir()), [])

    def test_argument_errors(self) -> None:
        dataset = self.dataset()
        cases = [
            (["metrics", "rwe", str(dataset), "--variant", "extended"], 2,
             b"--attribution-validated"),
            (["metrics", "rwe", str(dataset), "--horizon", "soon"], 2, b"not a duration"),
            (["metrics", "reach", str(dataset)], 2, b"--horizon"),
            (["metrics", "reach", str(dataset), "--horizon", "24h", "--band-hours", "5"], 2,
             b"divide 24"),
            (["metrics", "rwe", str(self.tmp)], 1, b"not an analytics dataset"),
        ]
        for argv, code, message in cases:
            with self.subTest(argv=argv[1:3]):
                proc = run_txray([*argv, "--store", str(self.store)])
                self.assertEqual(proc.returncode, code, proc.stderr.decode())
                self.assertIn(message, proc.stderr)
        export = write_export(self.tmp / "e.csv", [post("1", impressions=1)])
        proc = run_txray(["metrics", "import", str(export), "--schema", "x-post-v1", "--lang",
                          "en", "--out", str(self.tmp / "x"), "--captured-at",
                          "2026-09-30T12:00:00", "--store", str(self.store)])
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b"has no UTC offset", proc.stderr)
        self.assertFalse((self.tmp / "x").exists())


class IsolationTest(unittest.TestCase):
    def _imports(self, path: Path) -> set[str]:
        found = set()
        package = ".".join(path.relative_to(SRC.parent).with_suffix("").parts[:-1])
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".")
                    parts = parts[: len(parts) - node.level + 1]
                    base = ".".join(parts + ([node.module] if node.module else []))
                found.add(base)
                found.update(f"{base}.{alias.name}" for alias in node.names)
        return found

    def test_analytics_never_imports_the_rest_of_timelinexray(self) -> None:
        def allowed(name: str) -> bool:
            return (name in ("timelinexray", "timelinexray.__version__")
                    or name == "timelinexray.errors" or name.startswith("timelinexray.errors.")
                    or name.startswith("timelinexray.analytics"))

        for path in sorted(ANALYTICS.glob("*.py")):
            for name in self._imports(path):
                if name.startswith("timelinexray"):
                    with self.subTest(file=path.name, module=name):
                        self.assertTrue(allowed(name))
            with self.subTest(file=path.name):
                self.assertFalse(self._imports(path) & {"subprocess", "socket", "ssl",
                                                        "urllib.request", "http.client"})

    def test_only_the_metrics_command_imports_analytics(self) -> None:
        for path in sorted(SRC.rglob("*.py")):
            if ANALYTICS in path.parents:
                continue
            names = {n for n in self._imports(path) if n.startswith("timelinexray.analytics")}
            with self.subTest(file=str(path.relative_to(SRC))):
                if path.name == "metrics_cli.py":
                    tree = ast.parse(path.read_text("utf-8"))
                    top_level = {n for stmt in tree.body for n in self._stmt_imports(stmt)}
                    self.assertFalse(any("analytics" in n for n in top_level))
                else:
                    self.assertEqual(names, set())

    @staticmethod
    def _stmt_imports(stmt: ast.stmt) -> set[str]:
        if isinstance(stmt, ast.Import):
            return {alias.name for alias in stmt.names}
        if isinstance(stmt, ast.ImportFrom):
            return {stmt.module or ""} | {alias.name for alias in stmt.names}
        return set()

    def test_cli_registration_is_lazy(self) -> None:
        text = (SRC / "cli.py").read_text("utf-8")
        self.assertEqual(text.count("metrics_cli"), 1)
        self.assertNotIn("analytics", text)

    def test_parser_constants_match_the_dataset_module(self) -> None:
        self.assertEqual(metrics_cli.SCOPES, analytics_dataset.SCOPES)
        self.assertEqual(metrics_cli.COUNT_KINDS, analytics_dataset.COUNT_KINDS)

    def test_store_rule_matches_the_snapshot_store(self) -> None:
        cases = [{}, {"TXRAY_STORE": "/tmp/a-store"}, {"XDG_CACHE_HOME": "/tmp/xdg"},
                 {"TXRAY_STORE": "/tmp/s", "XDG_CACHE_HOME": "/tmp/xdg"}]
        for environ in cases:
            with self.subTest(environ=environ):
                self.assertEqual(offline.snapshot_store_root(None, environ),
                                 default_store_root(environ))
        self.assertEqual(offline.snapshot_store_root("/tmp/explicit"), Path("/tmp/explicit"))

    def test_the_test_runner_itself_never_went_offline(self) -> None:
        self.assertFalse(offline.is_active())


if __name__ == "__main__":
    unittest.main()
