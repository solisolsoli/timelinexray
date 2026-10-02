"""``install.sh``: the one-command installer, tested without network and without installing.

The script runs under ``/bin/sh`` with a private PATH that holds only symlinks to a few
system utilities plus stub executables (``uv``, ``pipx``, ``python3``, ``git``, ``uname``),
so every preflight failure and every install method is exercised hermetically. The real
install (a wheel into a venv, then ``--uninstall``) is part of ``scripts/build_check.py``.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO_ROOT

SCRIPT = REPO_ROOT / "install.sh"
UTILITIES = ("uname", "sed", "mkdir", "ln", "rm", "rmdir", "dirname", "basename", "grep",
             "tail", "readlink", "cat", "chmod")
PYTHON_STUB = """#!/bin/sh
case "$2" in
    *fts5*)
        if [ "{fts5}" = ok ]; then echo 3.45.0; exit 0; fi
        echo "SQLite 3.45.0: no such tokenizer: trigram"; exit 1 ;;
    *version_info*) echo "{version}" ;;
esac
"""
#: A python that can make a "virtual environment" holding a pip and a txray command.
PYTHON_STUB_WITH_VENV = """#!/bin/sh
case "$2" in
    *fts5*) echo 3.45.0; exit 0 ;;
    *version_info*) echo "{version}"; exit 0 ;;
esac
case "$1 $2 $3" in
    "-m venv --clear")
        mkdir -p "$4/bin"
        printf '#!/bin/sh\\ncase "$*" in "-m pip"*) printf "#!/bin/sh\\\\necho txray 9.9.9\\\\n" > "$(dirname "$0")/txray"; chmod +x "$(dirname "$0")/txray";; esac\\n' > "$4/bin/python"
        chmod +x "$4/bin/python" ;;
esac
"""
UV_STUB = """#!/bin/sh
echo "uv $*" >> "{log}"
case "$1 $2" in
    "tool dir") echo "{bindir}" ;;
    "tool install")
        mkdir -p "{bindir}"
        printf '#!/bin/sh\\ncase "$1" in --version) echo "txray 9.9.9";; setup) exit {setup};; esac\\n' \\
            > "{bindir}/txray"
        chmod +x "{bindir}/txray" ;;
    "tool list") echo "timelinexray v9.9.9" ;;
    "tool uninstall") rm -f "{bindir}/txray" ;;
esac
"""
PIPX_STUB = """#!/bin/sh
echo "pipx $*" >> "{log}"
case "$1" in
    environment) echo "{bindir}" ;;
    list) echo "timelinexray 9.9.9" ;;
esac
"""


def _executable(path: Path, text: str) -> None:
    path.write_text(text, "utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class InstallScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="txray-install-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.tools = self.tmp / "tools"
        self.stubs = self.tmp / "stubs"
        self.home = self.tmp / "home"
        self.bindir = self.tmp / "bin"
        self.log = self.tmp / "calls.log"
        for directory in (self.tools, self.stubs, self.home):
            directory.mkdir()
        for name in UTILITIES:
            found = shutil.which(name)
            self.assertIsNotNone(found, f"the test needs the system utility {name}")
            (self.tools / name).symlink_to(found)
        self.log.write_text("", "utf-8")

    # -- helpers ---------------------------------------------------------------------

    def python(self, name: str = "python3", version: str = "3.12.4", fts5: str = "ok") -> None:
        _executable(self.stubs / name, PYTHON_STUB.format(version=version, fts5=fts5))

    def git(self, version: str = "2.45.0") -> None:
        _executable(self.stubs / "git", f'#!/bin/sh\necho "git version {version}"\n')

    def uname(self, system: str) -> None:
        _executable(self.stubs / "uname", f'#!/bin/sh\necho "{system}"\n')

    def uv(self, setup: int = 0) -> None:
        _executable(self.stubs / "uv", UV_STUB.format(log=self.log, bindir=self.bindir, setup=setup))

    def pipx(self) -> None:
        _executable(self.stubs / "pipx", PIPX_STUB.format(log=self.log, bindir=self.bindir))

    def healthy(self) -> None:
        self.python()
        self.git()

    def run_script(self, *args: str, shell: str = "/bin/sh", extra_env: dict[str, str] | None = None,
                   path_extra: str = "", drop_env: tuple[str, ...] = (),
                   cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {"PATH": f"{self.stubs}:{self.tools}{path_extra}", "HOME": str(self.home),
               "TXRAY_HOME": str(self.tmp / "data"), "TXRAY_BIN_DIR": str(self.bindir)}
        env.update(extra_env or {})
        for name in drop_env:
            env.pop(name, None)
        return subprocess.run([shell, str(SCRIPT), *args], env=env, capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL, cwd=cwd)

    def calls(self) -> list[str]:
        return self.log.read_text("utf-8").splitlines()

    # -- the file --------------------------------------------------------------------

    def test_syntax_is_valid_posix_sh(self) -> None:
        for shell in {"/bin/sh", shutil.which("dash"), shutil.which("bash")} - {None}:
            with self.subTest(shell=shell):
                proc = subprocess.run([shell, "-n", str(SCRIPT)], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_script_runs_everything_from_functions_and_ends_with_main(self) -> None:
        lines = SCRIPT.read_text("utf-8").splitlines()
        self.assertTrue(lines[0].startswith("#!/bin/sh"))
        self.assertEqual([line for line in lines if line.strip()][-1], 'main "$@"')
        self.assertEqual(sum(1 for line in lines if line.startswith('main "$@"')), 1)
        self.assertIn("set -eu", lines)

    def test_a_truncated_download_never_runs(self) -> None:
        """Piped to sh and cut at any line, the script does nothing (no output, no calls)."""
        self.healthy()
        self.uv()
        lines = SCRIPT.read_text("utf-8").splitlines(keepends=True)
        env = {"PATH": f"{self.stubs}:{self.tools}", "HOME": str(self.home)}
        for cut in list(range(1, len(lines) - 1, 7)) + [len(lines) - 1]:
            with self.subTest(cut=cut):
                proc = subprocess.run(["/bin/sh"], input="".join(lines[:cut]), env=env,
                                      capture_output=True, text=True, timeout=30)
                self.assertEqual(proc.stdout, "")
        self.assertEqual(self.calls(), [])

    def test_script_is_executable_and_free_of_personal_paths(self) -> None:
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        text = SCRIPT.read_text("utf-8")
        self.assertNotIn("/Users/", text)
        self.assertNotIn("sudo", text.replace("never uses sudo", ""))
        self.assertNotIn(".bashrc", text.replace("never edits shell rc files", ""))

    def test_help(self) -> None:
        proc = self.run_script("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for word in ("--method", "--ref", "--source", "--uninstall", "--dry-run", "TXRAY_PYTHON",
                     "TXRAY_INSTALL_METHOD", "Windows is not supported", "Exit codes"):
            self.assertIn(word, proc.stdout)
        self.assertEqual(self.run_script("-h").stdout, proc.stdout)

    def test_usage_errors_exit_2(self) -> None:
        self.healthy()
        for args, message in ((("--bogus",), "unknown option"), (("--version",), "unknown option"),
                              (("--method", "conda"), "unknown method"), (("--method",), "needs a value"),
                              (("--ref",), "needs a value"), (("--source", "/nonexistent/x.whl"), "does not exist")):
            with self.subTest(args=args):
                proc = self.run_script(*args)
                self.assertEqual(proc.returncode, 2)
                self.assertIn(message, proc.stderr)
        source = self.tmp / "not-a-checkout"
        source.mkdir()
        self.assertEqual(self.run_script("--source", str(source)).returncode, 2)
        stray = self.tmp / "stray.txt"
        stray.write_text("x", "utf-8")
        self.assertIn("not a wheel", self.run_script("--source", str(stray)).stderr)

    # -- preflight -------------------------------------------------------------------

    def test_unsupported_operating_systems_exit_10(self) -> None:
        self.healthy()
        for system, needle in (("MINGW64_NT-10.0", "Windows is not supported"),
                               ("CYGWIN_NT-10.0", "Windows is not supported"),
                               ("FreeBSD", "unsupported operating system: FreeBSD")):
            with self.subTest(system=system):
                self.uname(system)
                proc = self.run_script("--dry-run")
                self.assertEqual(proc.returncode, 10)
                self.assertIn(needle, proc.stderr)
        self.assertIn("docs/limits.md", proc.stderr)

    def test_old_python_exits_11_and_names_what_it_found(self) -> None:
        self.git()
        self.python(version="3.10.12")
        proc = self.run_script("--dry-run")
        self.assertEqual(proc.returncode, 11)
        self.assertIn("Python 3.11 or newer is required", proc.stderr)
        self.assertIn("python3 (3.10.12)", proc.stderr)

    def test_missing_python_exits_11(self) -> None:
        self.git()
        proc = self.run_script("--dry-run")
        self.assertEqual(proc.returncode, 11)
        self.assertIn("Python 3.11 or newer was not found", proc.stderr)
        self.assertIn("TXRAY_PYTHON", proc.stderr)

    def test_txray_python_must_exist_and_wins_over_python3(self) -> None:
        self.git()
        self.python()
        proc = self.run_script("--dry-run", extra_env={"TXRAY_PYTHON": str(self.tmp / "nope")})
        self.assertEqual(proc.returncode, 11)
        self.assertIn("TXRAY_PYTHON", proc.stderr)
        self.python("custom-python", version="3.13.1")
        proc = self.run_script("--dry-run", extra_env={"TXRAY_PYTHON": str(self.stubs / "custom-python")})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"python: {self.stubs}/custom-python (3.13.1", proc.stdout)

    def test_txray_python_with_spaces_is_one_candidate(self) -> None:
        self.git()
        spaced = self.stubs / "my python dir"
        spaced.mkdir()
        _executable(spaced / "python", PYTHON_STUB.format(version="3.13.1", fts5="ok"))
        for shell in {"/bin/sh", shutil.which("dash"), shutil.which("bash")} - {None}:
            with self.subTest(shell=shell):
                proc = self.run_script("--dry-run", shell=shell,
                                       extra_env={"TXRAY_PYTHON": str(spaced / "python")})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(f"python: {spaced}/python (3.13.1", proc.stdout)
                self.assertIn(f"[dry-run] {spaced}/python -m venv --clear", proc.stdout)

    def test_home_is_needed_only_for_the_defaults_it_provides(self) -> None:
        self.healthy()
        proc = self.run_script("--dry-run", drop_env=("HOME",))  # both TXRAY_* variables are set
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for missing in ("TXRAY_HOME", "TXRAY_BIN_DIR"):
            with self.subTest(missing=missing):
                proc = self.run_script("--dry-run", drop_env=("HOME", missing))
                self.assertEqual(proc.returncode, 2)
                self.assertIn("HOME is not set", proc.stderr)
        proc = self.run_script("--dry-run", drop_env=("TXRAY_HOME", "TXRAY_BIN_DIR"))
        self.assertEqual(proc.returncode, 0, proc.stderr)  # HOME provides the defaults
        self.assertIn(f"{self.home}/.local/share/timelinexray/venv", proc.stdout)

    def test_a_newer_versioned_python_is_used_when_python3_is_old(self) -> None:
        self.git()
        self.python(version="3.9.6")
        self.python("python3.12", version="3.12.2")
        proc = self.run_script("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"python: {self.stubs}/python3.12 (3.12.2", proc.stdout)

    def test_python_without_fts5_trigram_exits_12(self) -> None:
        self.git()
        self.python(fts5="missing")
        proc = self.run_script("--dry-run")
        self.assertEqual(proc.returncode, 12)
        self.assertIn("FTS5", proc.stderr)
        self.assertIn("trigram", proc.stderr)
        self.assertIn("TXRAY_PYTHON", proc.stderr)
        self.assertIn("no such tokenizer", proc.stderr)

    def test_python_without_fts5_is_skipped_for_another_candidate(self) -> None:
        self.git()
        self.python(fts5="missing")
        self.python("python3.11", version="3.11.9")
        proc = self.run_script("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("python3.11 (3.11.9, SQLite 3.45.0 with FTS5", proc.stdout)

    def test_missing_git_exits_13(self) -> None:
        self.python()
        proc = self.run_script("--dry-run")
        self.assertEqual(proc.returncode, 13)
        self.assertIn("git was not found", proc.stderr)

    def test_old_git_exits_13(self) -> None:
        self.python()
        for version in ("2.37.9", "1.9.5"):
            with self.subTest(version=version):
                self.git(version)
                proc = self.run_script("--dry-run")
                self.assertEqual(proc.returncode, 13)
                self.assertIn(f"git version {version}", proc.stderr)

    def test_git_238_is_accepted_and_findings_are_printed(self) -> None:
        self.python()
        self.git("2.38.0 (Apple Git-1)")
        proc = self.run_script("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("git: ", proc.stdout)
        self.assertIn("(2.38)", proc.stdout)
        self.assertIn("os: ", proc.stdout)

    def test_explicit_method_without_its_tool_exits_14(self) -> None:
        self.healthy()
        for method in ("uv", "pipx"):
            with self.subTest(method=method):
                proc = self.run_script("--dry-run", "--method", method)
                self.assertEqual(proc.returncode, 14)
                self.assertIn(f"'{method}' is not on PATH", proc.stderr)

    # -- methods (dry run) -----------------------------------------------------------

    def test_dry_run_uv(self) -> None:
        self.healthy()
        self.uv()
        proc = self.run_script("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("method: uv", proc.stdout)
        self.assertIn(f"[dry-run] uv tool install --force --python {self.stubs}/python3 "
                      "git+https://github.com/solisolsoli/timelinexray@main", proc.stdout)
        self.assertEqual(self.calls(), ["uv tool dir --bin"])  # nothing installed
        self.assertFalse(self.bindir.exists())

    def test_dry_run_pipx(self) -> None:
        self.healthy()
        self.pipx()
        proc = self.run_script("--dry-run", "--ref", "v1.2.3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("method: pipx", proc.stdout)
        self.assertIn(f"[dry-run] pipx install --force --python {self.stubs}/python3 "
                      "git+https://github.com/solisolsoli/timelinexray@v1.2.3", proc.stdout)
        self.assertNotIn("pipx install", "\n".join(self.calls()))

    def test_dry_run_venv_without_uv_or_pipx(self) -> None:
        self.healthy()
        proc = self.run_script("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        venv = self.tmp / "data" / "venv"
        self.assertIn("method: venv", proc.stdout)
        self.assertIn(f"[dry-run] {self.stubs}/python3 -m venv --clear {venv}", proc.stdout)
        self.assertIn(f"[dry-run] {venv}/bin/python -m pip install --disable-pip-version-check "
                      "git+https://github.com/solisolsoli/timelinexray@main", proc.stdout)
        self.assertIn(f"[dry-run] ln -sf {venv}/bin/txray {self.bindir}/txray", proc.stdout)
        self.assertFalse((self.tmp / "data").exists())
        self.assertFalse(self.bindir.exists())

    def test_relative_install_directories_become_absolute(self) -> None:
        """The txray link is a symbolic link: a relative target would be read from the link's
        directory and dangle (exit 16), so TXRAY_HOME and TXRAY_BIN_DIR are made absolute."""
        self.git()
        _executable(self.stubs / "python3", PYTHON_STUB_WITH_VENV.format(version="3.12.4"))
        work = self.tmp / "work"
        work.mkdir()
        env = {"TXRAY_HOME": "rel-data", "TXRAY_BIN_DIR": "rel-bin"}
        proc = self.run_script("--dry-run", extra_env=env, cwd=work)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"[dry-run] ln -sf {work}/rel-data/venv/bin/txray {work}/rel-bin/txray",
                      proc.stdout)
        proc = self.run_script(extra_env=env, cwd=work)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        link = work / "rel-bin" / "txray"
        self.assertTrue(os.path.isabs(os.readlink(link)))
        self.assertTrue(link.exists())
        self.assertIn("txray 9.9.9", proc.stdout)
        proc = self.run_script("--uninstall", extra_env=env, cwd=work)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(link.is_symlink())
        self.assertFalse((work / "rel-data").exists())

    def test_auto_prefers_uv_then_pipx_then_venv(self) -> None:
        self.healthy()
        self.uv()
        self.pipx()
        self.assertIn("method: uv", self.run_script("--dry-run").stdout)
        (self.stubs / "uv").unlink()
        self.assertIn("method: pipx", self.run_script("--dry-run").stdout)
        (self.stubs / "pipx").unlink()
        self.assertIn("method: venv", self.run_script("--dry-run").stdout)

    def test_explicit_method_overrides_auto(self) -> None:
        self.healthy()
        self.uv()
        self.pipx()
        for method in ("venv", "pipx", "uv"):
            with self.subTest(method=method):
                self.assertIn(f"method: {method}", self.run_script("--dry-run", "--method", method).stdout)
                self.assertIn(f"method: {method}", self.run_script("--dry-run", f"--method={method}").stdout)

    def test_environment_equivalents(self) -> None:
        self.healthy()
        self.uv()
        self.pipx()
        proc = self.run_script("--dry-run", extra_env={"TXRAY_INSTALL_METHOD": "pipx",
                                                       "TXRAY_INSTALL_REF": "abc1234"})
        self.assertIn("method: pipx", proc.stdout)
        self.assertIn("timelinexray@abc1234", proc.stdout)
        proc = self.run_script("--dry-run", "--method", "uv", "--ref", "dev",
                               extra_env={"TXRAY_INSTALL_METHOD": "pipx", "TXRAY_INSTALL_REF": "abc1234"})
        self.assertIn("method: uv", proc.stdout)  # the flags win
        self.assertIn("timelinexray@dev", proc.stdout)
        checkout = self.tmp / "checkout"
        checkout.mkdir()
        (checkout / "pyproject.toml").write_text("[project]\n", "utf-8")
        proc = self.run_script("--dry-run", extra_env={"TXRAY_INSTALL_SOURCE": str(checkout)})
        self.assertIn(f"source: {checkout} (--ref ignored)", proc.stdout)

    def test_source_directory_and_wheel(self) -> None:
        self.healthy()
        self.uv()
        self.pipx()
        checkout = self.tmp / "checkout"
        checkout.mkdir()
        (checkout / "pyproject.toml").write_text("[project]\n", "utf-8")
        wheel = self.tmp / "timelinexray-0.0.0-py3-none-any.whl"
        wheel.write_bytes(b"")
        python = f"{self.stubs}/python3"
        cases = (
            ("uv", checkout, f"uv tool install --force --python {python} {checkout}"),
            ("uv", wheel, f"uv tool install --force --python {python} --no-index {wheel}"),
            ("pipx", checkout, f"pipx install --force --python {python} {checkout}"),
            ("pipx", wheel, f"pipx install --force --python {python} --pip-args=--no-index {wheel}"),
            ("venv", checkout, f"{self.tmp}/data/venv/bin/python -m pip install "
                               f"--disable-pip-version-check {checkout}"),
            ("venv", wheel, f"{self.tmp}/data/venv/bin/python -m pip install "
                            f"--disable-pip-version-check --no-index {wheel}"),
        )
        for method, source, expected in cases:
            with self.subTest(method=method, source=source.name):
                proc = self.run_script("--dry-run", "--method", method, "--source", str(source))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(f"[dry-run] {expected}\n", proc.stdout)
                self.assertNotIn("github.com", proc.stdout)

    def test_runs_under_every_available_shell(self) -> None:
        """dash, bash and zsh, the last two invoked as ``sh`` (POSIX emulation, as on a
        system whose /bin/sh is bash or zsh)."""
        self.healthy()
        shells = ["/bin/sh"]
        for name in ("dash", "bash", "zsh"):
            found = shutil.which(name)
            if found:
                alias = self.tmp / f"as-sh-{name}"
                alias.mkdir()
                (alias / "sh").symlink_to(found)
                shells.append(str(alias / "sh"))
        for shell in shells:
            with self.subTest(shell=shell):
                proc = self.run_script("--dry-run", shell=shell)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("method: venv", proc.stdout)

    # -- the whole flow with a stub uv -----------------------------------------------

    def test_install_with_stub_uv_verifies_and_prints_next_steps(self) -> None:
        self.healthy()
        self.uv(setup=1)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("txray 9.9.9", proc.stdout)
        self.assertIn("txray pin 77d431aabf409ca1c1eed9bec7e2183f7c914e23 && txray index 77d431a",
                      proc.stdout)
        self.assertNotIn("Next step:\n  txray setup", proc.stdout)
        self.assertIn(f'export PATH="{self.bindir}:$PATH"', proc.stdout)  # not on PATH
        self.assertIn("uv tool install", " ".join(c for c in self.calls() if "install" in c))
        proc = self.run_script(path_extra=f":{self.bindir}")
        self.assertNotIn("export PATH", proc.stdout)

    def test_the_fallback_pin_is_the_tested_commit(self) -> None:
        from timelinexray.setup_cli import TESTED_COMMIT
        from tests.support import UPSTREAM_COMMIT
        text = SCRIPT.read_text("utf-8")
        self.assertIn(f'NEXT_PIN="{TESTED_COMMIT}"', text)
        self.assertEqual(TESTED_COMMIT, UPSTREAM_COMMIT)
        self.assertIn(f'txray index {TESTED_COMMIT[:7]}"', text)

    def test_next_step_is_txray_setup_when_it_exists(self) -> None:
        self.healthy()
        self.uv(setup=0)
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Next step:\n  txray setup\n", proc.stdout)
        self.assertNotIn("txray pin", proc.stdout)

    def test_failing_install_exits_15_and_missing_txray_exits_16(self) -> None:
        self.healthy()
        _executable(self.stubs / "uv", '#!/bin/sh\ncase "$1 $2" in "tool dir") echo "' + str(self.bindir)
                    + '";; "tool install") exit 1;; esac\n')
        proc = self.run_script()
        self.assertEqual(proc.returncode, 15)
        self.assertIn("uv tool install failed", proc.stderr)
        _executable(self.stubs / "uv", '#!/bin/sh\ncase "$1 $2" in "tool dir") echo "' + str(self.bindir)
                    + '";; esac\n')
        proc = self.run_script()
        self.assertEqual(proc.returncode, 16)
        self.assertIn("does not exist", proc.stderr)

    # -- uninstall -------------------------------------------------------------------

    def test_uninstall_uv_and_pipx_call_the_tools(self) -> None:
        self.uv()
        self.pipx()
        proc = self.run_script("--uninstall")  # needs neither python nor git
        self.assertEqual(proc.returncode, 0, proc.stderr)
        calls = self.calls()
        self.assertIn("uv tool uninstall timelinexray", calls)
        self.assertIn("pipx uninstall timelinexray", calls)
        self.log.write_text("", "utf-8")
        proc = self.run_script("--uninstall", "--method", "pipx")
        self.assertEqual(self.calls(), ["pipx list --short", "pipx uninstall timelinexray"])

    def test_a_failing_tool_uninstall_is_reported_not_swallowed(self) -> None:
        for tool, listing in (("uv", '"tool list") echo "timelinexray v9.9.9";; "tool uninstall") exit 1;;'),
                              ("pipx", '"list --short") echo "timelinexray 9.9.9";; "uninstall timelinexray") exit 1;;')):
            with self.subTest(tool=tool):
                _executable(self.stubs / tool,
                            f'#!/bin/sh\ncase "$1 $2" in {listing} esac\n')
                proc = self.run_script("--uninstall", "--method", tool)
                self.assertEqual(proc.returncode, 15, proc.stdout + proc.stderr)
                self.assertIn(f"{tool} uninstall of timelinexray failed", proc.stderr)
                self.assertNotIn("nothing to uninstall", proc.stdout)
                self.assertNotIn("was uninstalled", proc.stdout)
                (self.stubs / tool).unlink()

    def test_uninstall_matches_the_exact_package_name(self) -> None:
        _executable(self.stubs / "uv", f'#!/bin/sh\necho "uv $*" >> "{self.log}"\n'
                    'case "$1 $2" in "tool list") printf "timelinexray-foo v1.0\\n- txray\\n";; esac\n')
        _executable(self.stubs / "pipx", f'#!/bin/sh\necho "pipx $*" >> "{self.log}"\n'
                    'case "$1" in list) echo "timelinexray-foo 1.0";; esac\n')
        proc = self.run_script("--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.calls(), ["uv tool list", "pipx list --short"])
        self.assertIn("nothing to uninstall", proc.stdout)

    def test_uninstall_venv_removes_only_what_the_installer_made(self) -> None:
        data, bindir = self.tmp / "data", self.bindir
        (data / "venv" / "bin").mkdir(parents=True)
        (data / "venv" / "bin" / "txray").write_text("x", "utf-8")
        bindir.mkdir()
        (bindir / "txray").symlink_to(data / "venv" / "bin" / "txray")
        (bindir / "other").write_text("keep", "utf-8")
        proc = self.run_script("--uninstall", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"[dry-run] rm -f {bindir}/txray", proc.stdout)
        self.assertTrue((bindir / "txray").is_symlink())  # dry run changes nothing
        self.assertTrue((data / "venv").is_dir())
        proc = self.run_script("--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((bindir / "txray").is_symlink())
        self.assertFalse(data.exists())
        self.assertEqual((bindir / "other").read_text("utf-8"), "keep")
        self.assertIn("nothing to uninstall", self.run_script("--uninstall").stdout)

    def test_uninstall_leaves_a_foreign_txray_alone(self) -> None:
        data, bindir = self.tmp / "data", self.bindir
        (data / "venv").mkdir(parents=True)
        bindir.mkdir()
        foreign = self.tmp / "elsewhere"
        foreign.write_text("x", "utf-8")
        (bindir / "txray").symlink_to(foreign)
        proc = self.run_script("--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((bindir / "txray").is_symlink())
        self.assertIn("leaving it alone", proc.stderr)
        self.assertFalse((data / "venv").exists())


if __name__ == "__main__":
    unittest.main()
