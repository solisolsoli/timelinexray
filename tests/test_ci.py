"""GitHub readiness: the workflow checker, the strict test runner and the CI status script.

The scripts in ``scripts/`` are developer tools, not part of the package. They are loaded
here from their files. ``ci_status.py`` runs against canned API responses through its
injectable opener: the suite blocks sockets, so a real request would fail loudly.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tokenize
import unittest
import urllib.error
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

from tests.support import REPO_ROOT, NetworkBlocked, upstream_git_dir

SCRIPTS = REPO_ROOT / "scripts"
UPSTREAM = upstream_git_dir()
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SHA = "0123456789abcdef0123456789abcdef01234567"


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"txray_script_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up while it loads
    spec.loader.exec_module(module)
    return module


check_workflows = _script("check_workflows")
ci_status = _script("ci_status")
build_check = _script("build_check")
check_sqlite = _script("check_sqlite")
check_lint = _script("check_lint")


class WorkflowCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-workflows-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for name in ("pyproject.toml", "Makefile", "CONTRIBUTING.md"):
            shutil.copy(REPO_ROOT / name, self.root / name)
        (self.root / ".github" / "workflows").mkdir(parents=True)
        self.workflow = self.root / ".github" / "workflows" / "ci.yml"
        self.text = WORKFLOW.read_text("utf-8")
        self.workflow.write_text(self.text, "utf-8")

    def problems_after(self, old: str, new: str, file: Path | None = None) -> list[str]:
        target = file or self.workflow
        text = target.read_text("utf-8")
        self.assertIn(old, text)
        target.write_text(text.replace(old, new, 1), "utf-8")
        return check_workflows.check(self.root)

    def test_the_repository_passes(self) -> None:
        self.assertEqual(check_workflows.check(REPO_ROOT), [])
        self.assertEqual(check_workflows.check(self.root), [])

    def test_matrix_equals_the_declared_python_versions(self) -> None:
        pyproject = (REPO_ROOT / "pyproject.toml").read_text("utf-8")
        versions, minimum = check_workflows.python_versions(pyproject)
        self.assertEqual((versions, minimum), (["3.11", "3.12", "3.13"], "3.11"))
        found = self.problems_after('    "Programming Language :: Python :: 3.13",\n', "",
                                    self.root / "pyproject.toml")
        self.assertTrue(any("must equal the declared versions" in p for p in found))

    def test_every_rule_catches_its_violation(self) -> None:
        checkout = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
        cases = {
            "not pinned to a full commit SHA": (checkout, "actions/checkout@v7"),
            "comment naming its tag": ("  # v7.0.1 (= v7)", ""),
            "exactly 'contents: read'": ("  contents: read", "  contents: write"),
            "references a secret": ("TXRAY_TEST_UPSTREAM: ${{ runner.temp }}",
                                    "TXRAY_TEST_UPSTREAM: ${{ secrets.X }}"),
            "triggers must be a subset": ("  workflow_dispatch:", "  pull_request_target:"),
            "cancel-in-progress": ("  cancel-in-progress: true", "  cancel-in-progress: false"),
            "timeout-minutes": ("    timeout-minutes: 20\n", ""),
            "persist-credentials: false": ("persist-credentials: false",
                                           "persist-credentials: true"),
            "run 'make ci' exactly once": ("        run: make ci", "        run: make test"),
            "neither 'make ci' nor an allowed setup command": (
                'run: python scripts/fetch_test_upstream.py "$RUNNER_TEMP/x-algorithm-upstream"',
                "run: curl https://example.invalid/install.sh"),
            "event text is interpolated": (
                'run: python scripts/fetch_test_upstream.py "$RUNNER_TEMP/x-algorithm-upstream"',
                "run: echo ${{ github.event.pull_request.title }}"),
            "matrix os must be": ("          - macos-latest\n", ""),
            "SQLite preflight": ("        run: python scripts/check_sqlite.py\n",
                                 "        run: make ci\n"),
            "not in the strict YAML subset": ("          - ubuntu-latest\n          - macos-latest",
                                              "          [ubuntu-latest, macos-latest]"),
        }
        for expected, (old, new) in cases.items():
            with self.subTest(rule=expected):
                self.workflow.write_text(self.text, "utf-8")
                found = self.problems_after(old, new)
                self.assertTrue(any(expected in problem for problem in found), found)

    def test_a_schedule_is_allowed_and_other_triggers_stay_refused(self) -> None:
        text = self.workflow.read_text("utf-8")
        self.assertIn('  schedule:\n    - cron: "17 5 * * 1"\n', text)
        data = check_workflows._load_yaml(text)  # the project's strict reader parses the cron list
        self.assertEqual(data["on"]["schedule"], [{"cron": "17 5 * * 1"}])
        self.assertEqual(check_workflows.check(self.root), [])
        for event in ("workflow_run", "pull_request_target", "issue_comment"):
            with self.subTest(event=event):
                self.workflow.write_text(text, "utf-8")
                found = self.problems_after("  workflow_dispatch:", f"  {event}:")
                self.assertTrue(any("triggers must be a subset" in p for p in found), found)

    def test_the_workflow_has_no_upstream_cache_and_always_clones(self) -> None:
        text = WORKFLOW.read_text("utf-8")
        self.assertNotIn("actions/cache", text)
        self.assertNotIn("cache-hit", text)
        self.assertNotIn("\n        if:", text)
        self.assertIn("    timeout-minutes: 20\n", text)

    def test_parity_with_the_makefile_and_contributing(self) -> None:
        found = self.problems_after("ci: pycheck lint hygiene test-strict build-check",
                                    "ci: pycheck lint test-strict build-check",
                                    self.root / "Makefile")
        self.assertTrue(any("ci target runs" in problem for problem in found), found)
        shutil.copy(REPO_ROOT / "Makefile", self.root / "Makefile")
        found = self.problems_after("`test-strict`, `build-check`", "`build-check`",
                                    self.root / "CONTRIBUTING.md")
        self.assertTrue(any("CONTRIBUTING.md" in problem for problem in found), found)

    def test_command_line(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
        proc = subprocess.run([sys.executable, str(SCRIPTS / "check_workflows.py"), "--json",
                               "--root", str(self.root)], capture_output=True, env=env, timeout=60)
        self.assertEqual((proc.returncode, json.loads(proc.stdout)["ok"]), (0, True))
        self.workflow.write_text(self.text.replace(" persist-credentials: false", " x: y"), "utf-8")
        proc = subprocess.run([sys.executable, str(SCRIPTS / "check_workflows.py"),
                               "--root", str(self.root)], capture_output=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"persist-credentials", proc.stderr)


class SqlitePreflightTest(unittest.TestCase):
    """FA-023: the runner's SQLite is checked for FTS5 with the trigram tokenizer before
    anything else, in the workflow and in ``make pycheck``."""

    def test_the_probe_passes_here_and_names_the_reason_when_it_fails(self) -> None:
        self.assertIsNone(check_sqlite.check())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(check_sqlite.main(), 0)
        first, *rest = out.getvalue().splitlines()
        self.assertIn("with FTS5 and the trigram tokenizer", first)
        self.assertTrue(first.startswith("sqlite check: "))
        # drift is traceable: the full interpreter version and git's version follow
        # the script prints sys.version with runs of white space collapsed: a build date
        # such as "Apr  2 2024" has two spaces, so compare the collapsed forms
        self.assertIn(f"python: {' '.join(sys.version.split())}", " ".join(rest))
        self.assertTrue(any(line.startswith("git: ") for line in rest), rest)
        self.assertTrue(any(line.startswith("time zone database: ") for line in rest), rest)

        class NoTrigram:
            def execute(self, sql: str) -> None:
                raise check_sqlite.sqlite3.OperationalError("no such tokenizer: trigram")

            def close(self) -> None:
                pass

        err = io.StringIO()
        with mock.patch.object(check_sqlite.sqlite3, "connect", return_value=NoTrigram()), \
                contextlib.redirect_stderr(err):
            self.assertEqual(check_sqlite.main(), 1)
        self.assertIn("lacks FTS5 with the trigram tokenizer (SQLite 3.34 or newer built with "
                      "FTS5 is required): no such tokenizer: trigram", err.getvalue())

    def test_a_missing_time_zone_database_is_information_not_a_failure(self) -> None:
        with mock.patch.object(check_sqlite.zoneinfo, "available_timezones", return_value=set()):
            self.assertIn("none found (install the tzdata package", check_sqlite.tz_database())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(check_sqlite.main(), 0)
            self.assertIn("time zone database: none found", out.getvalue())
        with mock.patch.object(check_sqlite.zoneinfo, "available_timezones",
                               side_effect=OSError("boom")):
            self.assertEqual(check_sqlite.tz_database(), "unknown (boom)")
        with mock.patch.object(check_sqlite.zoneinfo, "available_timezones",
                               return_value={"Etc/UTC", "Europe/Berlin"}):
            self.assertEqual(check_sqlite.tz_database(), "found (2 zones)")

    def test_the_probe_is_the_index_probe_and_runs_first(self) -> None:
        schema = (REPO_ROOT / "src" / "timelinexray" / "index" / "schema.py").read_text("utf-8")
        self.assertIn(f'connection.execute("{check_sqlite.PROBE}")', schema)
        steps = WORKFLOW.read_text("utf-8").split("      - name: ")[1:]
        runs = [step.split("run: ", 1)[1].strip() if "run: " in step else step.split("\n")[0]
                for step in steps]
        preflight = runs.index("python scripts/check_sqlite.py")
        self.assertLess(preflight, runs.index("make ci"))
        self.assertLess(preflight, next(i for i, step in enumerate(steps)
                                        if step.startswith("Clone the upstream")))
        makefile = (REPO_ROOT / "Makefile").read_text("utf-8")
        self.assertIn("pycheck:\n\t@$(PYTHON) -c 'import sys;", makefile)
        self.assertIn("\t@$(PYTHON) scripts/check_sqlite.py\n", makefile)
        proc = subprocess.run([sys.executable, str(SCRIPTS / "check_sqlite.py")],
                              capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)


def pep701_only(source: str) -> list[str]:
    """f-string syntax that only Python 3.12+ reads (PEP 701), which ``ast.parse`` with
    ``feature_version=(3, 11)`` does not refuse: a replacement field that reuses an
    enclosing quote, holds a backslash or a comment, or breaks a single-quoted line. Uses
    the 3.12+ tokenizer; on 3.11 the running parser already refuses all of it."""
    if sys.version_info < (3, 12):
        return []
    problems: list[str] = []
    stack: list[str] = []  # the quotes of the enclosing f-strings
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        line = token.start[0]
        if token.type == tokenize.FSTRING_START:
            quote = token.string.lstrip("rRfFbBuU")
            if any(enclosing in quote for enclosing in stack):
                problems.append(f"line {line}: a nested f-string reuses an enclosing quote")
            stack.append(quote)
        elif token.type == tokenize.FSTRING_END:
            stack.pop()
        elif not stack:
            continue
        elif token.type == tokenize.FSTRING_MIDDLE:
            if len(stack) > 1 and "\\" in token.string:
                problems.append(f"line {line}: a backslash inside a replacement field")
        elif token.type == tokenize.COMMENT:
            problems.append(f"line {line}: a comment inside a replacement field")
        elif token.type == tokenize.NL and len(stack[-1]) == 1:
            problems.append(f"line {line}: a line break inside a single-quoted f-string")
        elif token.type == tokenize.STRING:
            if "\\" in token.string:
                problems.append(f"line {line}: a backslash inside a replacement field")
            if any(enclosing in token.string.lstrip("rRbBuU") for enclosing in stack):
                problems.append(f"line {line}: a string reuses an enclosing f-string quote")
    return problems


class Python311SyntaxTest(unittest.TestCase):
    """Every source file parses as Python 3.11, the lowest declared version (audit section
    6, item 12): ``ast.parse(..., feature_version=(3, 11))`` plus the PEP 701 f-string
    check that ``feature_version`` does not perform."""

    def sources(self) -> list[Path]:
        files = [path for folder in ("src", "tests", "scripts")
                 for path in sorted((REPO_ROOT / folder).rglob("*.py"))]
        self.assertGreater(len(files), 100)
        return files

    def test_every_source_file_parses_as_python_3_11(self) -> None:
        for path in self.sources():
            source = path.read_text("utf-8")
            with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                ast.parse(source, filename=str(path), feature_version=(3, 11))
                self.assertEqual(pep701_only(source), [])

    def test_the_checks_refuse_3_12_syntax(self) -> None:
        for source in ("type Weights = dict[str, float]\n", "def f[T](x: T) -> T:\n    return x\n"):
            with self.subTest(source=source), self.assertRaises(SyntaxError):
                ast.parse(source, feature_version=(3, 11))
        if sys.version_info < (3, 12):
            return  # the 3.11 parser refuses PEP 701 f-strings by itself
        refused = [
            'f"{"a"}"',  # the enclosing quote reused inside the field
            "f\"{'\\n'.join(a)}\"",  # a backslash inside the field
            'f"{x  # c\n}"',  # a comment (and a line break) inside the field
            "f'{\n x}'",  # a line break inside a single-quoted f-string
            "f\"{f'{\"a\"}'}\"",  # a nested f-string reusing the outer quote
        ]
        accepted = [
            "f\"{'a'}\"",
            'f"""{"a"}"""',
            "f'{x!r:>{w}}'",
            'f"""{\n x}"""',
            "f\"{f'{x}'}\"",
            'f"a\\n{x}"',
        ]
        for source in refused:
            with self.subTest(refused=source):
                self.assertTrue(pep701_only(source + "\n"))
        for source in accepted:
            with self.subTest(accepted=source):
                ast.parse(source, feature_version=(3, 11))
                self.assertEqual(pep701_only(source + "\n"), [])


class ParallelRunnerTest(unittest.TestCase):
    """``run_tests.py --jobs N`` on a temporary test tree: counts are summed, one failing
    worker fails the run, and the strict skip rule is unchanged."""

    OK = "import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n" \
         "        pass\n\n    def test_b(self):\n        pass\n"
    FAILING = "import unittest\n\nclass T(unittest.TestCase):\n    def test_boom(self):\n" \
              "        self.fail('worker one broke')\n\n    def test_fine(self):\n        pass\n"
    SKIPPING = "import unittest\n\nclass T(unittest.TestCase):\n    @unittest.skip(%r)\n" \
               "    def test_skipped(self):\n        pass\n\n    def test_ran(self):\n" \
               "        pass\n"

    def run_tree(self, modules: dict[str, str], env_value: str | None = None,
                 *args: str, jobs: str = "3") -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="txray-runner-") as tmp:
            (Path(tmp) / "probe").mkdir()
            (Path(tmp) / "probe" / "__init__.py").write_text("")
            for name, source in modules.items():
                (Path(tmp) / "probe" / f"test_{name}.py").write_text(source)
            env = {k: v for k, v in os.environ.items()
                   if k not in ("TXRAY_TEST_UPSTREAM", "TXRAY_TEST_JOBS", "PYTHONPATH")}
            if env_value is not None:
                env["TXRAY_TEST_UPSTREAM"] = env_value
            return subprocess.run(
                [sys.executable, str(SCRIPTS / "run_tests.py"), "--start", "probe", "--top", ".",
                 "--jobs", jobs, *args], cwd=tmp, capture_output=True, text=True, env=env,
                timeout=120)

    def test_counts_are_summed_over_workers(self) -> None:
        proc = self.run_tree({"one": self.OK, "two": self.OK, "three": self.OK})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(proc.stderr, r"Ran 6 tests in ")
        self.assertRegex(proc.stderr, r"\nOK\n?$")

    def test_a_failure_in_one_worker_fails_the_run(self) -> None:
        proc = self.run_tree({"one": self.OK, "two": self.FAILING, "three": self.OK})
        self.assertEqual(proc.returncode, 1)
        self.assertRegex(proc.stderr, r"Ran 6 tests in ")
        self.assertIn("FAIL: test_boom (probe.test_two.T.test_boom)", proc.stderr)
        self.assertIn("worker one broke", proc.stderr)
        self.assertIn("FAILED (failures=1)", proc.stderr)

    def test_an_import_error_in_one_worker_fails_the_run(self) -> None:
        proc = self.run_tree({"one": self.OK, "two": "raise RuntimeError('no import')\n"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FAILED (errors=1)", proc.stderr)
        self.assertIn("no import", proc.stderr)

    def test_a_non_optional_skip_fails_only_when_the_upstream_is_required(self) -> None:
        modules = {"one": self.OK, "two": self.SKIPPING % "needs the upstream clone"}
        strict = self.run_tree(modules, "/some/clone")
        self.assertEqual(strict.returncode, 1)
        self.assertRegex(strict.stderr, r"Ran 4 tests in ")
        self.assertIn("OK (skipped=1)", strict.stderr)
        self.assertIn("probe.test_two.T.test_skipped: needs the upstream clone", strict.stderr)
        self.assertIn("no test may be skipped", strict.stderr)
        relaxed = self.run_tree(modules)
        self.assertEqual(relaxed.returncode, 0, relaxed.stderr)
        self.assertIn("notice: set TXRAY_TEST_UPSTREAM", relaxed.stderr)

    def test_an_optional_skip_never_fails(self) -> None:
        modules = {"one": self.OK, "two": self.SKIPPING % "optional: needs a third-party tool"}
        proc = self.run_tree(modules, "/some/clone")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("1 optional integration test(s) not run", proc.stderr)
        self.assertIn("OK (skipped=1)", proc.stderr)

    def test_the_combined_summary_matches_the_serial_runner(self) -> None:
        modules = {"one": self.OK, "two": self.FAILING, "three": self.SKIPPING % "x"}
        parallel = self.run_tree(modules, None, jobs="3")
        serial = self.run_tree(modules, None, jobs="1")
        self.assertEqual(parallel.returncode, serial.returncode)

        def verdict(text: str) -> tuple[str, str]:
            ran = re.search(r"Ran (\d+) tests?", text)
            end = re.search(r"^(OK|FAILED).*$", text, re.MULTILINE)
            assert ran and end
            return ran.group(1), end.group(0)

        self.assertEqual(verdict(parallel.stderr), verdict(serial.stderr))

    def test_jobs_values(self) -> None:
        runner = _script("run_tests")
        self.assertEqual(runner.parse_jobs("3"), 3)
        self.assertEqual(runner.parse_jobs("auto"), max(1, min(os.cpu_count() or 1, 4)))
        for bad in ("0", "-1", "many", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                runner.parse_jobs(bad)
        self.assertEqual(runner.schedule(["a", "b", "c"], {"b": 9, "c": 1}), ["b", "a", "c"])


class TemplateTest(unittest.TestCase):
    """tests/templates.py: processes sharing a directory build a template exactly once."""

    SCRIPT = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from tests.templates import template\n"
        "def build(directory):\n"
        "    with open(sys.argv[1], 'a') as log:\n"
        "        log.write('built\\n')\n"
        "    time.sleep(0.3)\n"
        "    (directory / 'content').write_text('x')\n"
        "print(template('probe', build) / 'content')\n"
    )

    def test_concurrent_processes_build_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="txray-template-") as tmp:
            shared, log = Path(tmp) / "shared", Path(tmp) / "log"
            env = {**os.environ, "TXRAY_TEST_SHARED": str(shared),
                   "PYTHONPATH": f"{REPO_ROOT / 'src'}{os.pathsep}{REPO_ROOT}"}
            procs = [subprocess.Popen([sys.executable, "-c", self.SCRIPT, str(log)], env=env,
                                      stdout=subprocess.PIPE, text=True, cwd=REPO_ROOT)
                     for _ in range(4)]
            outputs = [proc.communicate(timeout=120)[0].strip() for proc in procs]
            self.assertEqual([proc.returncode for proc in procs], [0] * 4)
            self.assertEqual(log.read_text().splitlines(), ["built"])
            self.assertEqual(set(outputs), {str(shared / "probe" / "content")})
            self.assertEqual((shared / "probe" / "content").read_text(), "x")

    def test_a_template_from_different_code_is_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="txray-template-") as tmp:
            shared, log = Path(tmp) / "shared", Path(tmp) / "log"
            env = {**os.environ, "TXRAY_TEST_SHARED": str(shared),
                   "PYTHONPATH": f"{REPO_ROOT / 'src'}{os.pathsep}{REPO_ROOT}"}

            def run() -> None:
                subprocess.run([sys.executable, "-c", self.SCRIPT, str(log)], env=env,
                               check=True, capture_output=True, cwd=REPO_ROOT, timeout=120)

            run()
            run()  # same code: reused
            self.assertEqual(log.read_text().splitlines(), ["built"])
            (shared / "probe.done").write_text("fingerprint of older code\n")
            run()  # a marker from different code: rebuilt
            self.assertEqual(log.read_text().splitlines(), ["built", "built"])


class StrictRunnerTest(unittest.TestCase):
    CASES = """
import unittest

class T(unittest.TestCase):
    def test_ok(self):
        pass

    @unittest.skipIf(True, "needs the upstream clone")
    def test_upstream(self):
        pass
"""

    def run_suite(self, source: str, env_value: str | None, init: str = ""
                  ) -> subprocess.CompletedProcess[bytes]:
        with tempfile.TemporaryDirectory(prefix="txray-runner-") as tmp:
            (Path(tmp) / "probe").mkdir()
            (Path(tmp) / "probe" / "__init__.py").write_text(init)
            (Path(tmp) / "probe" / "test_probe.py").write_text(source)
            env = {k: v for k, v in os.environ.items()
                   if k not in ("TXRAY_TEST_UPSTREAM", "PYTHONPATH")}
            if env_value is not None:
                env["TXRAY_TEST_UPSTREAM"] = env_value
            return subprocess.run([sys.executable, str(SCRIPTS / "run_tests.py"), "--start",
                                   "probe", "--top", "."], cwd=tmp, capture_output=True, env=env,
                                  timeout=120)

    def test_a_skip_fails_only_when_the_upstream_is_required(self) -> None:
        strict = self.run_suite(self.CASES, "/some/clone")
        self.assertEqual(strict.returncode, 1)
        self.assertIn(b"needs the upstream clone", strict.stderr)
        self.assertIn(b"no test may be skipped", strict.stderr)
        relaxed = self.run_suite(self.CASES, None)
        self.assertEqual(relaxed.returncode, 0)
        self.assertIn(b"notice: set TXRAY_TEST_UPSTREAM", relaxed.stderr)
        clean = self.run_suite(self.CASES.replace("skipIf(True", "skipIf(False"), "/some/clone")
        self.assertEqual(clean.returncode, 0)
        failing = self.run_suite(self.CASES.replace("pass\n\n", "self.fail('x')\n\n", 1), None)
        self.assertEqual(failing.returncode, 1)

    def test_the_package_is_importable_without_pythonpath(self) -> None:
        """FA-015: the documented ``python scripts/run_tests.py`` works without
        ``PYTHONPATH=src`` (the suite's ``tests/__init__.py`` imports the package)."""
        source = self.CASES.replace("skipIf(True", "skipIf(False")
        init = "import timelinexray\n"
        proc = self.run_suite(source, None, init=init)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(b"ModuleNotFoundError", proc.stderr)

    def test_an_optional_integration_skip_never_fails(self) -> None:
        source = self.CASES.replace('skipIf(True, "needs the upstream clone")',
                                    'skipIf(True, "optional: set TXRAY_TEST_CONTEXT_LAYER")')
        strict = self.run_suite(source, "/some/clone")
        self.assertEqual(strict.returncode, 0, strict.stderr)
        self.assertIn(b"optional integration test(s) not run", strict.stderr)
        self.assertNotIn(b"no test may be skipped", strict.stderr)
        required_skip = self.CASES.replace("class T(", "class U(")
        both = self.run_suite(source + required_skip, "/some/clone")
        self.assertEqual(both.returncode, 1)
        self.assertIn(b"no test may be skipped", both.stderr)


# -- ci_status ------------------------------------------------------------------------------


class _Response:
    def __init__(self, payload: Any) -> None:
        self.payload = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self.payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _Api:
    """Canned GitHub API: path -> payload (a callable gets the query)."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: float) -> _Response:
        self.requests.append(request)
        path, _, query = request.full_url.partition("?")
        path = path.replace("https://api.github.com", "")
        if path not in self.routes:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        payload = self.routes[path]
        return _Response(payload(query) if callable(payload) else payload)


def _routes(runs: list[dict[str, Any]], statuses: list[dict[str, Any]],
            workflows: list[dict[str, Any]], repo: str = "owner/name") -> dict[str, Any]:
    base = f"/repos/{repo}"
    return {
        f"{base}/commits/{SHA}/check-runs": {"total_count": len(runs), "check_runs": runs},
        f"{base}/commits/{SHA}/status": {"state": "success", "statuses": statuses},
        f"{base}/actions/runs": {"total_count": len(workflows), "workflow_runs": workflows},
    }


def _run(name: str, status: str = "completed", conclusion: str | None = "success") -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion}


class CiStatusTest(unittest.TestCase):
    def status(self, api: _Api, *extra: str, environ: dict[str, str] | None = None
               ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = ci_status.main(["--repo", "owner/name", "--commit", SHA, *extra],
                                      opener=api, environ=environ or {})
            except SystemExit as exc:
                code = int(exc.code or 0)
        return code, out.getvalue(), err.getvalue()

    def test_all_success(self) -> None:
        api = _Api(_routes([_run("make ci (ubuntu-latest, Python 3.11)")],
                           [{"context": "external", "state": "success"}], [_run("ci")]))
        code, out, _ = self.status(api)
        self.assertEqual(code, 0)
        self.assertIn(f"owner/name @ {SHA}: SUCCESS", out)
        self.assertIn("make ci (ubuntu-latest, Python 3.11)", out)

    def test_failure_pending_and_missing_are_distinguished(self) -> None:
        cases = [
            ([_run("a"), _run("b", conclusion="failure")], [], 1, "FAILURE"),
            ([_run("a"), _run("b", conclusion="cancelled")], [], 1, "FAILURE"),
            ([_run("a"), _run("b", conclusion="skipped")], [], 1, "FAILURE"),
            ([_run("a"), _run("b", status="in_progress", conclusion=None)], [], 3, "PENDING"),
            ([_run("a")], [{"context": "x", "state": "pending"}], 3, "PENDING"),
            ([_run("a")], [{"context": "x", "state": "error"}], 1, "FAILURE"),
            ([], [], 4, "MISSING"),
        ]
        for runs, statuses, expected, word in cases:
            with self.subTest(expected=expected, word=word, runs=runs):
                code, out, _ = self.status(_Api(_routes(runs, statuses, [])))
                self.assertEqual(code, expected)
                self.assertIn(word, out)
        api = _Api(_routes([_run("lint")], [], [_run("ci")]))
        code, out, _ = self.status(api, "--require", "ci", "--require", "release")
        self.assertEqual(code, 4)
        self.assertIn("no check with this name", out)
        code, out, _ = self.status(api, "--require", "ci", "--json")
        data = json.loads(out)
        self.assertEqual((code, data["state"], data["missing"]), (0, "success", []))

    def test_a_later_run_of_the_same_workflow_supersedes_an_earlier_one(self) -> None:
        """FA-017: a failed push run followed by a successful ``workflow_dispatch`` on the
        same commit passes; the failed run and its check runs are listed as superseded."""
        job = "make ci (ubuntu-latest, Python 3.11)"

        def flow(run_id: int, event: str, conclusion: str | None, hour: int, *,
                 workflow: int = 7, status: str = "completed") -> dict[str, Any]:
            return {"id": run_id, "name": "ci", "workflow_id": workflow, "event": event,
                    "status": status, "conclusion": conclusion, "run_attempt": 1,
                    "created_at": f"2026-10-01T{hour:02d}:00:00Z",
                    "check_suite_id": 100 + run_id}

        def job_run(conclusion: str | None, run_id: int) -> dict[str, Any]:
            return {**_run(job, conclusion=conclusion), "check_suite": {"id": 100 + run_id}}

        failed = flow(1, "push", "failure", 10)
        dispatched = flow(2, "workflow_dispatch", "success", 11)
        jobs = [job_run("failure", 1), job_run("success", 2)]
        for order in (1, -1):  # the order of the API's lists does not matter
            with self.subTest(order=order):
                api = _Api(_routes(jobs[::order], [], [failed, dispatched][::order]))
                code, out, _ = self.status(api, "--json")
                data = json.loads(out)
                self.assertEqual((code, data["state"]), (0, "success"))
                self.assertEqual(sorted((c["kind"], c["state"]) for c in data["checks"]),
                                 [("check-run", "success"), ("check-run", "superseded"),
                                  ("workflow-run", "success"), ("workflow-run", "superseded")])
        code, out, _ = self.status(_Api(_routes(jobs, [], [failed, dispatched])))
        self.assertIn("failure; superseded by run 2", out)
        self.assertIn("failure; its workflow ran again (run 2)", out)
        cases = {
            "a later failure is never hidden by an earlier success": (
                [flow(1, "push", "success", 10), flow(2, "workflow_dispatch", "failure", 11)], 1),
            "a pull-request run tests a merge commit, not the commit": (
                [failed, flow(3, "pull_request", "success", 12)], 1),
            "another workflow does not supersede": (
                [failed, flow(4, "push", "success", 12, workflow=8)], 1),
            "a later run still in progress keeps the gate pending": (
                [failed, flow(2, "workflow_dispatch", None, 11, status="in_progress")], 3),
        }
        for name, (workflows, expected) in cases.items():
            with self.subTest(name):
                code, _, _ = self.status(_Api(_routes([], [], workflows)))
                self.assertEqual(code, expected)

    def test_pagination(self) -> None:
        first = [_run(f"job-{i:03d}") for i in range(100)]

        def page(query: str) -> dict[str, Any]:
            runs = first if "page=1" in query.split("&") else [_run("job-100")]
            return {"total_count": 101, "check_runs": runs}

        routes = _routes([], [], [])
        routes[f"/repos/owner/name/commits/{SHA}/check-runs"] = page
        code, out, _ = self.status(_Api(routes), "--json")
        self.assertEqual((code, len(json.loads(out)["checks"])), (0, 101))

    def test_the_token_is_sent_only_as_a_header_and_never_shown(self) -> None:
        canary = "canary" + "-" * 3 + "0123456789abcdef"
        api = _Api(_routes([_run("ci", conclusion="failure")], [], []))
        with tempfile.TemporaryDirectory() as tmp:
            before = set(os.listdir(tmp))
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                code, out, err = self.status(api, environ={"GITHUB_TOKEN": canary})
            finally:
                os.chdir(cwd)
            self.assertEqual(set(os.listdir(tmp)), before)
        self.assertEqual(code, 1)
        self.assertNotIn(canary, out + err)
        headers = [dict(request.header_items()) for request in api.requests]
        self.assertTrue(headers)
        self.assertTrue(all(h.get("Authorization") == f"Bearer {canary}" for h in headers))
        self.assertTrue(all(r.full_url.startswith("https://api.github.com/") for r in api.requests))
        self.assertTrue(all(r.get_method() == "GET" for r in api.requests))
        code, _, _ = self.status(api)
        self.assertNotIn("Authorization", dict(api.requests[-1].header_items()))

    def test_api_errors_and_usage(self) -> None:
        code, _, err = self.status(_Api({}))
        self.assertEqual(code, 5)
        self.assertIn("HTTP 404", err)
        self.assertIn("GITHUB_TOKEN", err)
        for argv in (["--repo", "owner", "--commit", SHA],
                     ["--repo", "owner/name", "--commit", "abc"],
                     ["--repo", "owner/name", "--commit", SHA, "--api", "http://api.example"]):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
                    ci_status.main(argv, opener=_Api({}), environ={})
                self.assertEqual(caught.exception.code, 2)

    def test_the_default_opener_is_blocked_by_the_suite(self) -> None:
        client = ci_status.GitHub()
        with self.assertRaises(NetworkBlocked):  # the real opener would try a connection
            client.get(f"/repos/owner/name/commits/{SHA}/status")


class BuildCheckInstalledSetupTest(unittest.TestCase):
    """The verdicts of the checkout-install step (pure functions: the step itself needs pip
    and the upstream clone and runs in ``make ci``)."""

    COMMIT = "77d431aabf409ca1c1eed9bec7e2183f7c914e23"

    def test_the_setup_report_must_name_the_commit(self) -> None:
        build_check._check_setup_report({"data": {"commit": self.COMMIT}}, self.COMMIT)
        for report in ({"data": {"commit": "0" * 40}}, {"data": {}}, {}):
            with self.subTest(report=report), self.assertRaises(build_check.CheckFailed):
                build_check._check_setup_report(report, self.COMMIT)

    def test_the_mcp_config_must_point_at_the_linked_txray(self) -> None:
        link = "/path/to/bin/txray"
        good = {"data": {"config": {"mcpServers": {"timelinexray": {"command": link,
                                                                   "args": ["mcp", "serve"]}}}}}
        build_check._check_mcp_config(good, link)
        bad = {"data": {"config": {"mcpServers": {"timelinexray": {"command": "/path/to/venv/bin/txray"}}}}}
        for config in (bad, {"data": {}}, {}):
            with self.subTest(config=config), self.assertRaises(build_check.CheckFailed):
                build_check._check_mcp_config(config, link)

    def test_the_walk_installs_a_copy_of_the_checkout_and_the_module_entry_point(self) -> None:
        source = (SCRIPTS / "build_check.py").read_text("utf-8")
        for needle in ("_check_checkout_install(", '"-m", "timelinexray", "--version"',
                       '"--print-mcp-config", "json"', '"--uninstall", "--method", "venv"',
                       "TXRAY_ALLOW_FILE_URLS"):
            self.assertIn(needle, source)


class BuildCheckDistsTest(unittest.TestCase):
    """``build_check._check_dists`` on synthetic archives: the sdist must not ship tests
    (FA-016: a partial ``tests/`` cannot run outside a checkout) and the wheel only the
    package."""

    VERSION = "9.9.9"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-dists-")
        self.addCleanup(self._tmp.cleanup)
        self.dist = Path(self._tmp.name)

    def write(self, *, sdist_extra: tuple[str, ...] = (), wheel_extra: tuple[str, ...] = ()) -> None:
        base = f"timelinexray-{self.VERSION}"
        for path in self.dist.iterdir():
            path.unlink()
        with tarfile.open(self.dist / f"{base}.tar.gz", "w:gz") as archive:
            for name in ("LICENSE", "NOTICE", "pyproject.toml", "README.md",
                         "src/timelinexray/__init__.py", "src/timelinexray/cli.py", *sdist_extra):
                info = tarfile.TarInfo(f"{base}/{name}")
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
        info_dir = f"{base}.dist-info"
        with zipfile.ZipFile(self.dist / f"{base}-py3-none-any.whl", "w") as archive:
            archive.writestr(f"{info_dir}/METADATA", "Metadata-Version: 2.4\nName: timelinexray\n"
                             f"Version: {self.VERSION}\nLicense-Expression: Apache-2.0\n"
                             "Requires-Python: >=3.11\n")
            for name in (f"{info_dir}/licenses/LICENSE", f"{info_dir}/licenses/NOTICE",
                         "timelinexray/__init__.py", "timelinexray/cli.py", *wheel_extra):
                archive.writestr(name, "x")

    def test_sdist_with_tests_is_refused(self) -> None:
        self.write()
        sdist, wheel = build_check._check_dists(self.dist, self.VERSION)
        self.assertEqual((sdist.suffix, wheel.suffix), (".gz", ".whl"))
        self.write(sdist_extra=("tests/__init__.py", "tests/test_cli.py"))
        with self.assertRaises(build_check.CheckFailed) as caught:
            build_check._check_dists(self.dist, self.VERSION)
        self.assertIn("ships tests", str(caught.exception))
        self.assertIn("tests/test_cli.py", str(caught.exception))
        self.write(wheel_extra=("tests/test_cli.py",))
        with self.assertRaises(build_check.CheckFailed):
            build_check._check_dists(self.dist, self.VERSION)

    def test_the_manifest_prunes_tests(self) -> None:
        manifest = (REPO_ROOT / "MANIFEST.in").read_text("utf-8").splitlines()
        self.assertIn("prune tests", manifest)


class LintCheckTest(unittest.TestCase):
    def problems(self, text: str, name: str = "mod.py", **kwargs: bool) -> list[str]:
        return check_lint.check_source(text, name, **kwargs)

    def test_the_repository_passes(self) -> None:
        self.assertEqual(check_lint.check(REPO_ROOT), [])

    def test_an_unused_import_is_flagged(self) -> None:
        self.assertEqual(self.problems("import os\nfrom json import dumps, loads\nloads('1')\n"),
                         ["mod.py:1: unused import os", "mod.py:2: unused import dumps"])

    def test_used_exported_and_annotated_names_pass(self) -> None:
        text = ("from __future__ import annotations\n"
                "from typing import TYPE_CHECKING\n"
                "import os.path\nimport re\nfrom a import b\nfrom c import d  # noqa: F401\n"
                "if TYPE_CHECKING:\n    from e import Late\n"
                "__all__ = ['b']\n"
                "def f(x: 'Late') -> None:\n    os.path.join(x)\n")
        self.assertEqual(self.problems(text), ["mod.py:4: unused import re"])

    def test_package_init_reexports_are_not_flagged(self) -> None:
        self.assertEqual(self.problems("from .a import b\n", "pkg/__init__.py", package_init=True), [])

    def test_an_unused_private_name_is_flagged(self) -> None:
        text = "_used = 1\n_dead = 2\n__dunder__ = 3\ndef _helper(): pass\nprint(_used)\n"
        self.assertEqual(self.problems(text), ["mod.py:2: unused private name _dead",
                                               "mod.py:4: unused private name _helper"])

    def test_a_temporary_tree_with_a_violation_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="txray-lint-") as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "ok.py").write_text("import os\nos.getcwd()\n", "utf-8")
            self.assertEqual(check_lint.check(root), [])
            (root / "src" / "bad.py").write_text("import sys\n", "utf-8")
            self.assertEqual(check_lint.check(root), ["src/bad.py:1: unused import sys"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(check_lint.main(["--root", str(root)]), 1)
            self.assertIn("unused import sys", out.getvalue())


class ScriptBoundaryTest(unittest.TestCase):
    def test_scripts_are_not_part_of_the_package(self) -> None:
        for path in sorted((REPO_ROOT / "src").rglob("*.py")):
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                for name in names:
                    self.assertFalse(name.split(".")[0] in ("scripts", "ci_status",
                                                            "check_workflows", "build_check",
                                                            "check_sqlite", "check_lint"),
                                     f"{path} imports {name}")
        self.assertFalse((REPO_ROOT / "scripts" / "__init__.py").exists())


class FetchUpstreamScriptTest(unittest.TestCase):
    def run_script(self, *argv: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run([sys.executable, str(SCRIPTS / "fetch_test_upstream.py"), *argv],
                              capture_output=True, timeout=120)

    def test_arguments_are_validated(self) -> None:
        self.assertEqual(self.run_script("x", "--commit", "abc").returncode, 2)
        self.assertEqual(self.run_script("x", "--url", "git://example.invalid/r.git").returncode, 2)

    def test_a_fresh_directory_is_fetched_into_main(self) -> None:
        # Regression: git init leaves HEAD on the unborn main, and git refuses to fetch into
        # the branch HEAD names without --update-head-ok; CI's first run failed this way.
        script = _script("fetch_test_upstream")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
            subprocess.run(["git", "init", "--quiet", "--template=", str(source)], check=True, env=env)
            subprocess.run(["git", "-C", str(source), "commit", "--quiet", "--allow-empty", "-m", "c"],
                           check=True, env=env)
            commit = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], check=True,
                                    env=env, capture_output=True, text=True).stdout.strip()
            target = Path(tmp) / "target"
            with mock.patch.object(script, "TRANSPORT", script.TRANSPORT + ("-c", "protocol.file.allow=always")):
                script.fetch(target, source.as_uri(), commit)
                self.assertTrue(script._present(target, commit))
                main = subprocess.run(["git", "-C", str(target), "rev-parse", "refs/heads/main"],
                                      check=True, capture_output=True, text=True).stdout.strip()
                self.assertEqual(main, commit)

    def test_a_git_failure_is_reported_with_its_message(self) -> None:
        script = _script("fetch_test_upstream")
        error = subprocess.CalledProcessError(128, ["git", "-C", "d", "fetch", "--quiet", "u", "c:m"],
                                              stderr="fatal: example failure\n")
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(script, "fetch", side_effect=error), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            code = script.main([str(Path(tmp) / "new")])
        self.assertEqual(code, 1)
        self.assertIn("fatal: example failure", stderr.getvalue())
        self.assertIn("exit status 128", stderr.getvalue())

    @unittest.skipIf(UPSTREAM is None, "no local x-algorithm clone with 77d431a "
                     "(set TXRAY_TEST_UPSTREAM to a local clone)")
    def test_an_existing_clone_is_used_without_fetching(self) -> None:
        assert UPSTREAM is not None
        directory = UPSTREAM.parent if UPSTREAM.name == ".git" else UPSTREAM
        proc = self.run_script(str(directory), "--url", "https://example.invalid/none.git")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(b"with 39 commits of history", proc.stdout)


if __name__ == "__main__":
    unittest.main()
