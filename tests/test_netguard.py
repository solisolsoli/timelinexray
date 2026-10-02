"""The network allowlist, the single fetch function, and the absence of other network paths."""

from __future__ import annotations

import ast
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timelinexray import gitio, netguard
from timelinexray.errors import InvalidInput, NetworkRefused
from timelinexray.netguard import DEFAULT_UPSTREAM_URL, Allowlist, fetch, network_git_options
from tests.support import REPO_ROOT, NetworkBlocked, build_fixture_repo, file_url

SRC = REPO_ROOT / "src" / "timelinexray"


class AllowlistTest(unittest.TestCase):
    def test_default_upstream_is_the_only_network_url(self) -> None:
        allowlist = Allowlist()
        self.assertEqual(allowlist.check(DEFAULT_UPSTREAM_URL), DEFAULT_UPSTREAM_URL)
        self.assertEqual(allowlist.urls, (DEFAULT_UPSTREAM_URL,))

    def test_non_allowlisted_urls_are_refused(self) -> None:
        refused = [
            "http://github.com/xai-org/x-algorithm.git",
            "https://github.com/xai-org/x-algorithm",
            "https://github.com/xai-org/x-algorithm.git/",
            "https://github.com/xai-org/other.git",
            "https://GITHUB.com/xai-org/x-algorithm.git",
            "https://github.com:443/xai-org/x-algorithm.git",
            "https://user:token@github.com/xai-org/x-algorithm.git",
            "https://github.com@evil.example/xai-org/x-algorithm.git",
            "https://github.com.evil.example/xai-org/x-algorithm.git",
            "https://github.com/xai-org/x-algorithm.git?ref=main",
            "https://github.com/xai-org/x-algorithm.git#main",
            "https://github.com/xai-org/x-algorithm.git/../../evil/repo.git",
            "https://github.com/xai-org/x-algorithm.git\n",
            " https://github.com/xai-org/x-algorithm.git",
            "ssh://git@github.com/xai-org/x-algorithm.git",
            "git@github.com:xai-org/x-algorithm.git",
            "git://github.com/xai-org/x-algorithm.git",
            "ext::sh -c touch% /tmp/pwned",
            "--upload-pack=touch /tmp/pwned",
            "file:///tmp/not-configured",
            "file://localhost/tmp/repo",
            "/tmp/plain-path",
            "",
        ]
        allowlist = Allowlist()
        for url in refused:
            with self.subTest(url=url):
                with self.assertRaises(NetworkRefused) as ctx:
                    allowlist.check(url)
                self.assertEqual(ctx.exception.exit_code, 3)
        with self.assertRaises(NetworkRefused):
            allowlist.check(None)

    def test_configured_file_url_is_allowed_and_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "repo.git"
            real.mkdir()
            link = Path(tmp) / "alias"
            link.symlink_to(real)
            allowlist = Allowlist([f"file://{link}"])
            canonical = "file://" + os.path.realpath(real)
            self.assertEqual(allowlist.check(f"file://{real}"), canonical)
            self.assertEqual(allowlist.check(f"file://{link}"), canonical)
            with self.assertRaises(NetworkRefused):
                allowlist.check(f"file://{Path(tmp) / 'other.git'}")

    def test_configuration_cannot_add_network_urls(self) -> None:
        for url in ("https://example.com/x.git", "http://github.com/x.git", "ssh://h/x",
                    "file://relative/path", "file:///has space", "file:///a%20b", "file:///x?q=1"):
            with self.subTest(url=url):
                with self.assertRaises(InvalidInput):
                    Allowlist([url])

    def test_from_env(self) -> None:
        allowlist = Allowlist.from_env({netguard.ENV_ALLOW_FILE_URLS: " file:///a/b \n file:///c "})
        self.assertIn("file:///a/b", allowlist.urls)
        self.assertEqual(Allowlist.from_env({}).urls, (DEFAULT_UPSTREAM_URL,))
        with self.assertRaisesRegex(InvalidInput, netguard.ENV_ALLOW_FILE_URLS):
            Allowlist.from_env({netguard.ENV_ALLOW_FILE_URLS: "https://evil.example/x.git"})


class FetchGuardTest(unittest.TestCase):
    def test_refused_url_never_starts_a_process(self) -> None:
        with mock.patch.object(netguard.subprocess, "run") as run, \
                mock.patch.object(netguard.subprocess, "Popen") as popen:
            for url in ("https://evil.example/x.git", "file:///nowhere", "git@github.com:x/y.git"):
                with self.subTest(url=url):
                    with self.assertRaises(NetworkRefused):
                        fetch(url, Path("/nonexistent.git"), Allowlist())
            run.assert_not_called()
            popen.assert_not_called()

    def test_only_https_and_file_transports_exist(self) -> None:
        for scheme in ("ssh", "git", "ext", "http", "fd"):
            with self.subTest(scheme=scheme):
                with self.assertRaises(NetworkRefused):
                    network_git_options(scheme)
        options = network_git_options("file")
        self.assertIn("protocol.allow=never", options)
        self.assertIn("protocol.file.allow=always", options)
        self.assertNotIn("protocol.https.allow=always", options)
        self.assertIn("http.followRedirects=false", options)
        self.assertIn("fetch.fsckObjects=true", options)

    def test_git_itself_blocks_transports_that_were_not_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, _ = build_fixture_repo(Path(tmp) / "src", {"a.txt": b"a\n"}, frozenset())
            target = gitio.GitRepo.init_bare(Path(tmp) / "target.git")
            argv = ["git", f"--git-dir={target.git_dir}", *network_git_options("https"),
                    "fetch", "--quiet", "--", file_url(source), "+refs/heads/*:refs/heads/*"]
            proc = subprocess.run(argv, capture_output=True, env=gitio.git_env())
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(b"transport 'file' not allowed", proc.stderr)

    def test_allowed_file_fetch_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source, commit = build_fixture_repo(Path(tmp) / "src", {"a.txt": b"a\n"}, frozenset())
            target = gitio.GitRepo.init_bare(Path(tmp) / "target.git")
            url = file_url(source)
            self.assertEqual(fetch(url, target.git_dir, Allowlist([url])), url)
            self.assertEqual(target.resolve_commit(commit[:7]), commit)


class LocalGitTest(unittest.TestCase):
    def test_local_runner_rejects_network_subcommands(self) -> None:
        for sub in ("fetch", "clone", "pull", "push", "ls-remote", "remote", "submodule",
                    "archive", "fetch-pack", "http-fetch", "bundle"):
            with self.subTest(sub=sub):
                with self.assertRaises(ValueError):
                    gitio.run_local(Path("/nonexistent.git"), [sub])

    def test_git_env_drops_inherited_git_variables(self) -> None:
        hostile = {"GIT_ALLOW_PROTOCOL": "ext:ssh", "GIT_SSL_NO_VERIFY": "1",
                   "GIT_CONFIG_PARAMETERS": "'protocol.allow'='always'"}
        with mock.patch.dict(os.environ, hostile):
            env = gitio.git_env()
        for key in hostile:
            self.assertNotIn(key, env)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_NO_LAZY_FETCH"], "1")


class NetworkSurfaceTest(unittest.TestCase):
    """Static proof that netguard.fetch is the only place a fetch can start."""

    NETWORK_MODULES = {"socket", "ssl", "http", "urllib.request", "ftplib", "smtplib",
                       "telnetlib", "xmlrpc", "requests", "httpx", "aiohttp", "urllib3"}

    def _sources(self) -> dict[str, str]:
        return {str(p.relative_to(SRC)): p.read_text("utf-8") for p in sorted(SRC.rglob("*.py"))}

    def test_no_network_library_is_imported(self) -> None:
        for name, text in self._sources().items():
            for node in ast.walk(ast.parse(text)):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    modules = [node.module]
                for module in modules:
                    with self.subTest(file=name, module=module):
                        top = {module, module.split(".")[0]}
                        self.assertFalse(top & self.NETWORK_MODULES)

    def test_processes_start_only_in_gitio_and_netguard(self) -> None:
        process_attrs = {"system", "popen", "posix_spawn", "posix_spawnp", "fork", "forkpty"}
        found: set[str] = set()
        for name, text in self._sources().items():
            starts_processes = False
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Import):
                    starts_processes |= any(a.name in ("subprocess", "pty", "multiprocessing")
                                            for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    starts_processes |= node.module in ("subprocess", "pty", "multiprocessing")
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                        and node.value.id == "os":
                    starts_processes |= node.attr in process_attrs or node.attr.startswith(
                        ("exec", "spawn"))
            if starts_processes:
                found.add(name)
        self.assertEqual(found, {"gitio.py", "netguard.py"})

    def test_fetch_appears_only_in_netguard(self) -> None:
        for name, text in self._sources().items():
            tree = ast.parse(text)
            literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                        and isinstance(n.value, str)}
            if name == "netguard.py":
                self.assertIn("fetch", literals)  # the one git network subcommand in use
                continue
            for word in ("fetch", "clone", "ls-remote", "pull", "push"):
                with self.subTest(file=name, word=word):
                    self.assertNotIn(word, literals)

    def test_python_sockets_are_blocked_during_tests(self) -> None:
        with self.assertRaises(NetworkBlocked):
            socket.create_connection(("127.0.0.1", 9))
        with self.assertRaises(NetworkBlocked):
            socket.getaddrinfo("github.com", 443)


if __name__ == "__main__":
    unittest.main()
