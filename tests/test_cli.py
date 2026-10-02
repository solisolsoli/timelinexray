"""The txray command line: pin, manifest and show, human and JSON output, exit codes."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import unittest
from typing import Any
from unittest import mock

from timelinexray import __version__
from timelinexray.cli import build_parser
from timelinexray.mcp import schema as S
from timelinexray.netguard import ENV_ALLOW_FILE_URLS
from tests.support import REPO_ROOT, FixtureRepo, git_show, run_cli

ENVELOPE_SCHEMA = json.loads((REPO_ROOT / "schemas" / "cli-envelope.schema.json").read_text("utf-8"))

FIXTURE: FixtureRepo


def setUpModule() -> None:
    global FIXTURE
    FIXTURE = FixtureRepo()


def tearDownModule() -> None:
    FIXTURE.cleanup()


def _store_args() -> list[str]:
    return ["--store", str(FIXTURE.store_dir)]


class PinCommandTest(unittest.TestCase):
    def test_pin_human_then_json(self) -> None:
        store = str(FIXTURE.root / "cli-store")
        env = {ENV_ALLOW_FILE_URLS: FIXTURE.url}
        code, out, err = run_cli(["pin", FIXTURE.commit[:9], "--upstream", FIXTURE.url,
                                  "--store", store], env)
        self.assertEqual((code, err), (0, b""))
        text = out.decode()
        self.assertIn(f"pinned     {FIXTURE.commit}", text)
        self.assertIn("fetched    yes", text)
        self.assertIn(f"paths      {len(FIXTURE.pin_result.manifest.entries)} total", text)

        code, out, _ = run_cli(["pin", FIXTURE.commit, "--upstream", FIXTURE.url,
                                "--store", store, "--json"], env)
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual((doc["command"], doc["outcome"]), ("pin", "ok"))
        self.assertFalse(doc["data"]["fetched"])
        self.assertEqual(doc["data"]["pin"]["manifest_sha256"],
                         FIXTURE.pin_result.pin.manifest_sha256)

    def test_refused_upstream_exit_code(self) -> None:
        store = FIXTURE.root / "cli-refused"
        code, out, err = run_cli(["pin", FIXTURE.commit, "--upstream", FIXTURE.url,
                                  "--store", str(store)], {ENV_ALLOW_FILE_URLS: ""})
        self.assertEqual(code, 3)
        self.assertIn(b"file:// URLs must be configured explicitly", err)
        self.assertFalse(store.exists())
        code, out, _ = run_cli(["pin", FIXTURE.commit, "--upstream", "https://evil.example/r.git",
                                "--store", str(store), "--json"])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(out)["error"]["code"], "network_refused")

    def test_bad_allowlist_configuration_is_invalid_input(self) -> None:
        code, _, err = run_cli(["pin", FIXTURE.commit, "--upstream", FIXTURE.url, *_store_args()],
                               {ENV_ALLOW_FILE_URLS: "https://evil.example/r.git"})
        self.assertEqual(code, 2)
        self.assertIn(ENV_ALLOW_FILE_URLS.encode(), err)


class ManifestCommandTest(unittest.TestCase):
    def test_listing_has_one_line_per_path(self) -> None:
        code, out, _ = run_cli(["manifest", FIXTURE.commit[:7], *_store_args()])
        self.assertEqual(code, 0)
        lines = out.decode().splitlines()
        self.assertEqual(lines[0], "classification\treason\tlanguage\tsize\tpath")
        self.assertEqual(len(lines) - 1, len(FIXTURE.pin_result.manifest.entries))
        self.assertIn("excluded\tsymlink\t-\t12\tlink/to_readme", lines)
        self.assertIn("excluded\tsubmodule\t-\t-\tsub/module", lines)

    def test_summary(self) -> None:
        code, out, _ = run_cli(["manifest", FIXTURE.commit, "--summary", *_store_args()])
        self.assertEqual(code, 0)
        text = out.decode()
        self.assertIn(f"manifest   sha256:{FIXTURE.pin_result.pin.manifest_sha256}", text)
        self.assertIn("LICENSE  [license]  Apache-2.0", text)
        self.assertRegex(text, r"  symlink +2\n")

    def test_json(self) -> None:
        code, out, _ = run_cli(["manifest", FIXTURE.commit, "--json", *_store_args()])
        doc = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(doc["data"]["manifest"], FIXTURE.pin_result.manifest.to_dict())
        code, out, _ = run_cli(["manifest", FIXTURE.commit, "--summary", "--json", *_store_args()])
        summary = json.loads(out)["data"]
        self.assertEqual(summary["counts"], FIXTURE.pin_result.manifest.counts())
        self.assertEqual(len(summary["licenses"]), 2)

    def test_unpinned_commit(self) -> None:
        code, _, err = run_cli(["manifest", "abcdef1", *_store_args()])
        self.assertEqual(code, 1)
        self.assertIn(b"is not pinned", err)
        self.assertIn(b"run: txray pin abcdef1", err)


class ShowCommandTest(unittest.TestCase):
    def test_raw_output_is_the_exact_span(self) -> None:
        original = git_show(FIXTURE.git_dir, FIXTURE.commit, "crlf/windows.txt")
        code, out, _ = run_cli(["show", FIXTURE.commit, "crlf/windows.txt", "--lines", "1-3",
                                "--raw", *_store_args()])
        self.assertEqual((code, out), (0, original))
        code, out, _ = run_cli(["show", FIXTURE.commit, "crlf/windows.txt", "--lines", "2",
                                "--raw", *_store_args()])
        self.assertEqual(out, b"beta\r\n")

    def test_human_output(self) -> None:
        code, out, _ = run_cli(["show", FIXTURE.commit, "src/app.py", "--lines", "1-5",
                                "--anchor", "WEIGHT", *_store_args()])
        self.assertEqual(code, 0)
        header, body = out.split(b"\n----\n", 1)
        self.assertIn(b"lines      1-5 of 5", header)
        self.assertIn(b"anchor     FOUND_MULTIPLE: 3 occurrences, all inside the span, at lines 1, 2, 5",
                      header)
        self.assertIn(b"class      parsed-candidate (python)", header)
        self.assertEqual(body, git_show(FIXTURE.git_dir, FIXTURE.commit, "src/app.py"))

    def test_human_output_marks_a_missing_final_newline(self) -> None:
        code, out, _ = run_cli(["show", FIXTURE.commit, "edge/no_newline.java", "--lines", "1",
                                *_store_args()])
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith(b"----\nclass A {}\n\\ No newline at end of file\n"))

    def test_json_output(self) -> None:
        code, out, _ = run_cli(["show", FIXTURE.commit[:8], "crlf/mixed.rs", "--lines", "1-2",
                                "--anchor=fn b", "--json", *_store_args()])
        self.assertEqual(code, 0)
        data = json.loads(out)["data"]
        self.assertEqual(data["text"], "fn a() {}\r\nfn b() {}\n")
        self.assertEqual(data["line_terminators"], {"lf": 1, "crlf": 1, "none": 0})
        self.assertEqual((data["anchor"]["verdict"], data["anchor"]["lines"]), ("FOUND", [2]))
        self.assertEqual(data["classification"], "parsed-candidate")

    def test_excluded_path_warning(self) -> None:
        code, out, _ = run_cli(["show", FIXTURE.commit, "Cargo.lock", "--lines", "1", "--json",
                                *_store_args()])
        doc = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(doc["warnings"],
                         ["path is excluded from indexing (generated: name:Cargo.lock)"])

    def test_errors_and_exit_codes(self) -> None:
        cases = [
            (["crlf/windows.txt", "--lines", "3-4"], 1,
             b"line range 3-4 is past end of file: crlf/windows.txt has 3 lines"),
            (["edge/empty.py", "--lines", "1"], 1, b"is empty (0 lines)"),
            (["link/escape", "--lines", "1"], 1, b"symlinks are recorded but never followed"),
            (["bin/data.bin", "--lines", "1"], 1, b"classified binary"),
            (["nope.txt", "--lines", "1"], 1, b"is not in the manifest"),
            (["../x", "--lines", "1"], 2, b"not normalised"),
            (["README.md", "--lines", "3-x"], 2, b"invalid line range"),
            (["README.md", "--lines", "1", "--raw", "--json"], 2, b"choose one of --raw and --json"),
        ]
        for args, expected_code, message in cases:
            with self.subTest(args=args):
                code, out, err = run_cli(["show", FIXTURE.commit, *args, *_store_args()])
                self.assertEqual(code, expected_code)
                self.assertIn(message, err + out)

    def test_json_error_envelope(self) -> None:
        code, out, err = run_cli(["show", FIXTURE.commit, "crlf/windows.txt", "--lines", "9",
                                  "--json", *_store_args()])
        self.assertEqual((code, err), (1, b""))
        doc = json.loads(out)
        self.assertEqual(doc["outcome"], "error")
        self.assertEqual(doc["error"]["code"], "span_range")
        # the error envelope has the members of a success envelope (FA-012)
        self.assertEqual((doc["data"], doc["warnings"], doc["command"]), (None, [], "show"))
        S.validate(doc, ENVELOPE_SCHEMA)

    def test_missing_lines_argument_is_a_usage_error(self) -> None:
        code, _, err = run_cli(["show", FIXTURE.commit, "README.md", *_store_args()])
        self.assertEqual(code, 2)
        self.assertIn(b"--lines", err)


def _leaf_commands(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()
                   ) -> list[tuple[str, ...]]:
    """Every command path that has no further subcommands (aliases counted once)."""
    groups = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if not groups:
        return [prefix]
    leaves: list[tuple[str, ...]] = []
    seen: set[int] = set()
    for group in groups:
        for name, sub in group.choices.items():
            if id(sub) in seen:
                continue
            seen.add(id(sub))
            leaves += _leaf_commands(sub, (*prefix, name))
    return leaves


class EnvelopeTest(unittest.TestCase):
    """One published envelope for every ``txray ... --json`` outcome (audit section 6,
    item 13; FA-011, FA-012)."""

    def envelope(self, argv: list[str], env: dict[str, str] | None = None
                 ) -> tuple[int, dict[str, Any], bytes]:
        code, out, err = run_cli(argv, env)
        doc = json.loads(out)
        S.validate(doc, ENVELOPE_SCHEMA)
        return code, doc, err

    def test_success_envelopes_match_the_published_schema(self) -> None:
        for argv in (["manifest", FIXTURE.commit, "--summary", "--json", *_store_args()],
                     ["show", FIXTURE.commit, "README.md", "--lines", "1", "--json",
                      *_store_args()],
                     ["pin", FIXTURE.commit, "--upstream", FIXTURE.url, "--json",
                      *_store_args()]):
            with self.subTest(argv=argv[:2]):
                code, doc, _ = self.envelope(argv, {ENV_ALLOW_FILE_URLS: FIXTURE.url})
                self.assertEqual((code, doc["outcome"]), (0, "ok"))
        # the documented exception: the MCP tools/list definitions, not the envelope
        code, out, _ = run_cli(["mcp", "tools", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("tools", json.loads(out))
        self.assertNotIn("outcome", json.loads(out))

    def test_every_command_reports_a_usage_error_as_the_envelope(self) -> None:
        leaves = _leaf_commands(build_parser())
        self.assertGreater(len(leaves), 20)
        self.assertIn(("findings", "add"), leaves)
        self.assertIn(("metrics", "import"), leaves)
        self.assertIn(("mcp", "serve"), leaves)
        for leaf in leaves:
            with self.subTest(command=" ".join(leaf)):
                code, doc, _ = self.envelope([*leaf, "--json", "--no-such-option"])
                self.assertEqual(code, 2)
                self.assertEqual(doc["outcome"], "error")
                self.assertEqual(doc["error"]["code"], "usage")
                self.assertEqual(doc["command"], " ".join(leaf))
                self.assertEqual((doc["data"], doc["warnings"]), (None, []))
        code, doc, _ = self.envelope(["--json"])
        self.assertEqual((code, doc["error"]["code"], doc["command"]), (2, "usage", None))
        code, doc, _ = self.envelope(["show", FIXTURE.commit, "README.md", "--json",
                                      *_store_args()])
        self.assertEqual((code, doc["error"]["code"]), (2, "usage"))
        self.assertIn("--lines", doc["error"]["message"])

    def test_usage_errors_without_json_stay_text(self) -> None:
        code, out, err = run_cli(["show", FIXTURE.commit, "README.md", *_store_args()])
        self.assertEqual((code, out), (2, b""))
        self.assertIn(b"usage: txray show", err)

    def test_operation_errors_of_other_commands_use_the_envelope(self) -> None:
        code, doc, _ = self.envelope(["manifest", "0000000", "--json", *_store_args()])
        self.assertEqual((code, doc["outcome"], doc["command"]), (1, "error", "manifest"))
        code, doc, _ = self.envelope(["findings", "show", "F-1", "--json", "--ledger",
                                      str(FIXTURE.root / "no-such-ledger"), *_store_args()])
        self.assertEqual((code, doc["outcome"], doc["command"]), (1, "error", "findings show"))
        code, doc, _ = self.envelope(["pin", FIXTURE.commit, "--upstream",
                                      "https://evil.example/r.git", "--json", *_store_args()])
        self.assertEqual((code, doc["error"]["code"]), (3, "network_refused"))

    def test_an_unexpected_exception_is_an_internal_error_envelope(self) -> None:
        def explode(args: Any) -> int:
            raise RuntimeError("synthetic failure")

        with mock.patch("timelinexray.cli._cmd_manifest", explode):
            code, doc, err = self.envelope(["manifest", FIXTURE.commit, "--json", *_store_args()])
            self.assertEqual((code, doc["error"]["code"]), (1, "internal_error"))
            self.assertEqual(doc["error"]["message"], "RuntimeError: synthetic failure")
            self.assertIn(b"Traceback", err)  # debugging information stays on stderr
            code, out, err = run_cli(["manifest", FIXTURE.commit, *_store_args()])
            self.assertEqual((code, out), (1, b""))
            self.assertIn(b"txray: error: RuntimeError: synthetic failure", err)


class BrokenPipeTest(unittest.TestCase):
    """A reader that closes the pipe ends ``txray`` silently with exit 0 (FA-011)."""

    ENV = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}

    def run_into_closed_pipe(self, *argv: str) -> subprocess.CompletedProcess[bytes]:
        read_end, write_end = os.pipe()
        os.close(read_end)  # the first write hits EPIPE: deterministic, whatever the size
        try:
            return subprocess.run([sys.executable, "-m", "timelinexray", *argv, *_store_args()],
                                  stdout=write_end, stderr=subprocess.PIPE, env=self.ENV,
                                  timeout=120)
        finally:
            os.close(write_end)

    def test_closed_pipe_is_silent(self) -> None:
        for argv in (["manifest", FIXTURE.commit],
                     ["manifest", FIXTURE.commit, "--json"],
                     ["show", FIXTURE.commit, "README.md", "--lines", "1-2", "--raw"],
                     ["findings", "list", "--ledger", str(FIXTURE.root / "no-such-ledger")]):
            with self.subTest(argv=argv[:2]):
                proc = self.run_into_closed_pipe(*argv)
                self.assertEqual((proc.returncode, proc.stderr), (0, b""))

    def test_head_pipeline(self) -> None:
        command = (f"{sys.executable} -m timelinexray manifest {FIXTURE.commit} "
                   f"--store {FIXTURE.store_dir} | head -1")
        # bash, not sh: pipefail is not POSIX before 2024 and dash (Ubuntu's sh) rejects it
        proc = subprocess.run(["bash", "-c", f"set -o pipefail; {command}"], capture_output=True,
                              env=self.ENV, timeout=120)
        self.assertEqual((proc.returncode, proc.stderr), (0, b""))
        self.assertEqual(proc.stdout, b"classification\treason\tlanguage\tsize\tpath\n")


class LegacyEncodingTest(unittest.TestCase):
    """Human-readable output under a stdout encoding that cannot show every character (a C
    locale, a legacy code page) escapes what it cannot show; ``--raw`` stays exact bytes."""

    ENV = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src"), "PYTHONIOENCODING": "ascii",
           "PYTHONUTF8": "0"}

    def run_txray(self, *argv: str) -> subprocess.CompletedProcess[bytes]:
        env = {**self.ENV, "TXRAY_STORE": str(FIXTURE.store_dir)}
        return subprocess.run([sys.executable, "-m", "timelinexray", *argv], env=env,
                              capture_output=True, timeout=120)

    def test_human_output_escapes_what_ascii_cannot_show(self) -> None:
        path = "unicode/na\u00efve.txt"
        for argv in (["manifest", FIXTURE.commit],
                     ["show", FIXTURE.commit, path, "--lines", "1"]):
            with self.subTest(argv=argv[0]):
                proc = self.run_txray(*argv)
                self.assertEqual((proc.returncode, proc.stderr), (0, b""), proc.stderr[-300:])
                # the text is escaped; the span of `show` follows the header as exact bytes
                text = proc.stdout.split(b"\n----\n")[0]
                text.decode("ascii")  # nothing unencodable got through
                self.assertIn(b"unicode/na\\xefve.txt", text)

    def test_raw_output_is_still_exact_bytes(self) -> None:
        path = "unicode/na\u00efve.txt"
        proc = self.run_txray("show", FIXTURE.commit, path, "--lines", "1", "--raw")
        self.assertEqual((proc.returncode, proc.stderr), (0, b""))
        self.assertEqual(proc.stdout, git_show(FIXTURE.git_dir, FIXTURE.commit, path))

    def test_json_is_unchanged(self) -> None:
        proc = self.run_txray("manifest", FIXTURE.commit, "--json")
        self.assertEqual((proc.returncode, proc.stderr), (0, b""))
        self.assertIn(b"na\\u00efve", proc.stdout)


class HelpCompletenessTest(unittest.TestCase):
    """Every findings subcommand argument, and manifest, show and each findings subcommand,
    has help text (and a description)."""

    @staticmethod
    def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return dict(action.choices)
        return {}

    def test_findings_subcommands_are_documented(self) -> None:
        top = self._subparsers(build_parser())
        findings = self._subparsers(top["findings"])
        self.assertEqual(len(findings), 12)
        missing = []
        for name, sub in findings.items():
            if not sub.description:
                missing.append(f"{name}: description")
            for action in sub._actions:
                if not action.help:
                    missing.append(f"{name}: {action.dest}")
        self.assertEqual(missing, [])
        for name in ("manifest", "show"):
            self.assertTrue(top[name].description, name)

    def test_the_epilog_lists_the_interrupt_exit_code(self) -> None:
        self.assertIn("130 interrupted", build_parser().epilog)


class EntryPointTest(unittest.TestCase):
    def test_module_entry_point(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
        proc = subprocess.run([sys.executable, "-m", "timelinexray", "--version"],
                              capture_output=True, env=env)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.decode().strip(), f"txray {__version__}")


if __name__ == "__main__":
    unittest.main()
