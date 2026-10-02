"""``txray setup``: pin + index in one step, idempotence, the printed MCP configuration.
Everything runs against the local fixture upstream through a ``file://`` URL."""

from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import unittest
from unittest import mock

from timelinexray.netguard import ENV_ALLOW_FILE_URLS
from timelinexray.setup_cli import TESTED_COMMIT
from tests.support import UPSTREAM_COMMIT, FixtureRepo, run_cli

FIXTURE: FixtureRepo


def setUpModule() -> None:
    global FIXTURE
    FIXTURE = FixtureRepo()


def tearDownModule() -> None:
    FIXTURE.cleanup()


def _env() -> dict[str, str]:
    return {ENV_ALLOW_FILE_URLS: FIXTURE.url}


def _setup(store: str, *extra: str) -> tuple[int, bytes, bytes]:
    return run_cli(["setup", "--upstream", FIXTURE.url, "--store", store, *extra], _env())


def _json(store: str, *extra: str) -> dict:
    code, out, err = _setup(store, *extra, "--json")
    assert code == 0, (code, out, err)
    return json.loads(out)


class SetupTest(unittest.TestCase):
    def test_default_commit_is_the_tested_one(self) -> None:
        self.assertEqual(TESTED_COMMIT, UPSTREAM_COMMIT)

    def test_fresh_store_then_second_run(self) -> None:
        store = str(FIXTURE.root / "setup-fresh")
        code, out, err = _setup(store, "--commit", FIXTURE.commit)
        self.assertEqual((code, err), (0, b""))
        text = out.decode()
        self.assertIn(f"commit     {FIXTURE.commit}", text)
        self.assertIn("pin        pinned (fetched from the upstream)", text)
        self.assertIn("index      indexed:", text)
        self.assertIn("txray search " + FIXTURE.commit[:7], text)
        self.assertIn("txray param ClickWeight --commit " + FIXTURE.commit[:7], text)
        self.assertIn("claude mcp add --transport stdio timelinexray -- ", text)

        with mock.patch("timelinexray.snapshot.store.fetch",
                        side_effect=AssertionError("second run must not fetch")):
            code, out, err = _setup(store, "--commit", FIXTURE.commit)
        self.assertEqual((code, err), (0, b""))
        text = out.decode()
        self.assertIn("pin        already pinned (nothing fetched)", text)
        self.assertIn("index      already indexed:", text)

    def test_json_envelope_and_idempotence(self) -> None:
        store = str(FIXTURE.root / "setup-json")
        first = _json(store, "--commit", FIXTURE.commit[:9])
        self.assertEqual((first["command"], first["outcome"], first["warnings"]),
                         ("setup", "ok", []))
        data = first["data"]
        self.assertEqual(data["commit"], FIXTURE.commit)
        self.assertEqual(data["store"], os.path.abspath(store))
        self.assertEqual(data["pin"], {"created": True, "fetched": True, "already_pinned": False})
        self.assertFalse(data["index"]["already_indexed"])
        self.assertGreater(data["index"]["files"], 0)
        self.assertGreater(data["index"]["symbols"], 0)
        self.assertEqual(len(data["next_steps"]), 3)
        self.assertIsInstance(data["elapsed_seconds"], float)
        second = _json(store, "--commit", FIXTURE.commit)["data"]
        self.assertEqual(second["pin"], {"created": False, "fetched": False,
                                         "already_pinned": True})
        self.assertTrue(second["index"]["already_indexed"])
        self.assertEqual(second["index"]["files"], data["index"]["files"])
        self.assertEqual(second["index"]["symbols"], data["index"]["symbols"])

    def test_no_index(self) -> None:
        store = str(FIXTURE.root / "setup-noindex")
        data = _json(store, "--commit", FIXTURE.commit, "--no-index")["data"]
        self.assertEqual(data["index"], {"skipped": True})
        self.assertFalse(os.path.exists(os.path.join(store, "index")))
        code, out, _ = _setup(store, "--commit", FIXTURE.commit, "--no-index")
        self.assertEqual(code, 0)
        self.assertIn("index      skipped (--no-index)", out.decode())

    def test_latest_uses_the_fixture_head(self) -> None:
        store = str(FIXTURE.root / "setup-latest")
        data = _json(store, "--latest")["data"]
        self.assertEqual((data["commit"], data["resolved"]), (FIXTURE.commit, "latest"))
        self.assertTrue(data["pin"]["fetched"])
        again = _json(store, "--latest")["data"]  # --latest always asks the upstream
        self.assertEqual(again["commit"], FIXTURE.commit)
        self.assertTrue(again["pin"]["already_pinned"])
        self.assertTrue(again["index"]["already_indexed"])

    def test_a_symlinked_file_url_names_the_same_mirror(self) -> None:
        """``setup --latest`` resolved the head with the URL as typed but the allowlist knows
        the canonical one: a spelling through a symbolic link (macOS ``/var`` -> ``/private/var``)
        created a second mirror."""
        link = FIXTURE.root / "alias"
        os.symlink(FIXTURE.root, link, target_is_directory=True)
        url = FIXTURE.url.replace(str(FIXTURE.root), str(link), 1)
        self.assertNotEqual(url, FIXTURE.url)
        store = FIXTURE.root / "setup-symlink"
        code, out, err = run_cli(["setup", "--latest", "--no-index", "--upstream", url,
                                  "--store", str(store), "--json"], _env())
        self.assertEqual((code, err), (0, b""), out)
        data = json.loads(out)["data"]
        self.assertEqual((data["commit"], data["upstream"]), (FIXTURE.commit, FIXTURE.url))
        self.assertEqual(len(list((store / "mirrors").iterdir())), 1)
        again = _setup(str(store), "--latest", "--no-index")  # the canonical spelling: same mirror
        self.assertEqual(again[0], 0)
        self.assertEqual(len(list((store / "mirrors").iterdir())), 1)

    def test_default_commit_absent_from_fixture_fails_cleanly(self) -> None:
        store = str(FIXTURE.root / "setup-default")
        code, out, _ = _setup(store, "--json")
        self.assertEqual(code, 1)
        doc = json.loads(out)
        self.assertEqual((doc["outcome"], doc["error"]["code"]), ("error", "not_found"))
        self.assertIn(TESTED_COMMIT[:7], doc["error"]["message"])

    def test_refused_upstream_exit_3_and_nothing_written(self) -> None:
        store = FIXTURE.root / "setup-refused"
        for url in ("https://example.com/x/y.git", "file:///nonexistent/repo.git"):
            code, out, _ = run_cli(["setup", "--upstream", url, "--store", str(store),
                                    "--json"], _env())
            self.assertEqual(code, 3, url)
            self.assertEqual(json.loads(out)["error"]["code"], "network_refused")
        self.assertFalse(store.exists())

    def test_usage_errors_exit_2(self) -> None:
        store = str(FIXTURE.root / "setup-usage")
        for extra in (["--commit", FIXTURE.commit, "--latest"],
                      ["--print-mcp-config", "--latest"],
                      ["--print-mcp-config", "yaml"]):
            code, _, _ = _setup(store, *extra)
            self.assertEqual(code, 2, extra)
        self.assertFalse(os.path.exists(store))

    def test_help_lists_setup(self) -> None:
        code, out, _ = run_cli(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("setup", out.decode())
        code, out, _ = run_cli(["setup", "--help"])
        self.assertEqual(code, 0)
        for flag in ("--commit", "--latest", "--no-index", "--print-mcp-config", "--upstream"):
            self.assertIn(flag, out.decode())


class PrintMcpConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = os.path.abspath(FIXTURE.root / "mcp store")  # a space: quoting matters

    def test_claude_code_line_with_entry_point(self) -> None:
        with mock.patch("shutil.which", return_value="/opt/bin/txray"):
            code, out, err = run_cli(["setup", "--print-mcp-config", "claude-code",
                                      "--store", self.store])
        self.assertEqual((code, err), (0, b""))
        expected = ("claude mcp add --transport stdio timelinexray -- /opt/bin/txray mcp serve "
                    f"--store {shlex.quote(self.store)}\n")
        self.assertEqual(out.decode(), expected)

    def test_default_form_is_claude_code(self) -> None:
        with mock.patch("shutil.which", return_value="/opt/bin/txray"):
            _, out, _ = run_cli(["setup", "--print-mcp-config", "--store", self.store])
        self.assertTrue(out.decode().startswith("claude mcp add --transport stdio timelinexray"))

    def test_json_snippet_matches_docs_shape(self) -> None:
        with mock.patch("shutil.which", return_value="/opt/bin/txray"):
            code, out, _ = run_cli(["setup", "--print-mcp-config", "json", "--store", self.store])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"mcpServers": {"timelinexray": {
            "type": "stdio", "command": "/opt/bin/txray",
            "args": ["mcp", "serve", "--store", self.store]}}})

    def test_the_running_entry_point_wins_even_off_path(self) -> None:
        # right after install.sh the bin directory is often not on PATH yet
        with tempfile.TemporaryDirectory() as tmp:
            entry_point = os.path.join(tmp, "txray")
            with open(entry_point, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\n")
            with mock.patch("shutil.which", return_value=None), \
                    mock.patch.object(sys, "argv", [entry_point, "setup"]):
                _, out, _ = run_cli(["setup", "--print-mcp-config", "json", "--store", self.store])
        entry = json.loads(out)["mcpServers"]["timelinexray"]
        self.assertEqual(entry["command"], os.path.abspath(entry_point))
        self.assertEqual(entry["args"][:2], ["mcp", "serve"])

    def test_fallback_to_interpreter_module(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            _, out, _ = run_cli(["setup", "--print-mcp-config", "json", "--store", self.store])
            _, line, _ = run_cli(["setup", "--print-mcp-config", "claude-code", "--store",
                                  self.store])
        entry = json.loads(out)["mcpServers"]["timelinexray"]
        self.assertEqual(entry["command"], os.path.abspath(sys.executable))
        self.assertEqual(entry["args"][:2], ["-m", "timelinexray"])
        self.assertTrue(os.path.isabs(entry["command"]) and os.path.isabs(entry["args"][-1]))
        self.assertIn(f"-- {shlex.quote(os.path.abspath(sys.executable))} -m timelinexray "
                      "mcp serve --store", line.decode())

    def test_relative_store_becomes_absolute_and_nothing_is_created(self) -> None:
        with mock.patch("shutil.which", return_value="/opt/bin/txray"):
            _, out, _ = run_cli(["setup", "--print-mcp-config", "json", "--store", "rel-store"])
        arg = json.loads(out)["mcpServers"]["timelinexray"]["args"][-1]
        self.assertEqual(arg, os.path.abspath("rel-store"))
        self.assertFalse(os.path.exists("rel-store"))

    def test_json_envelope(self) -> None:
        with mock.patch("shutil.which", return_value="/opt/bin/txray"):
            code, out, _ = run_cli(["setup", "--print-mcp-config", "json", "--store", self.store,
                                    "--json"])
        doc = json.loads(out)
        self.assertEqual((code, doc["command"], doc["outcome"]), (0, "setup", "ok"))
        self.assertEqual(doc["data"]["format"], "json")
        self.assertEqual(doc["data"]["store"], self.store)


if __name__ == "__main__":
    unittest.main()
