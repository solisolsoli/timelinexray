#!/usr/bin/env python3
"""Run the unittest suite; with ``TXRAY_TEST_UPSTREAM`` set, a skipped test is a failure.

Not part of the ``timelinexray`` package. Every skip in this suite means a test that needs
a local clone of the upstream repository did not run. CI always provides the clone, so
there a skip is an error: this runner exits non-zero and lists every skipped test. Without
``TXRAY_TEST_UPSTREAM`` skips are allowed and counted in a notice.

The one exception is an optional integration test with a third-party tool that is never
required (for example the Context Layer live test, which runs only when
``TXRAY_TEST_CONTEXT_LAYER`` names a ``context-layer`` executable): its skip reason starts
with ``optional:``. Such skips are listed but never fail the run.

    python scripts/run_tests.py [--start tests] [--top .] [--verbose] [--jobs N|auto]

``--jobs N`` (default: ``$TXRAY_TEST_JOBS``, else 1; ``auto`` is ``min(cpu count, 4)``) runs
the test modules in N worker processes, longest first (``test_timing_hints.json`` holds
rough seconds per module), and prints one combined summary in the serial format: the same
tests, the same skip rule, the same exit codes, output in module order. The workers share
one directory (``TXRAY_TEST_SHARED``) in which ``tests/templates.py`` builds each expensive
fixture once.

The project's ``src`` directory is put on ``sys.path`` first (as ``check_workflows.py``
does), so the command works without ``PYTHONPATH=src``; ``make test-strict`` runs it.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ENV = "TXRAY_TEST_UPSTREAM"
ROOT = Path(__file__).resolve().parent.parent
OPTIONAL = "optional:"
JOBS_ENV = "TXRAY_TEST_JOBS"
SHARED_ENV = "TXRAY_TEST_SHARED"
HINTS = Path(__file__).with_name("test_timing_hints.json")
DEFAULT_HINT = 2.0  # seconds assumed for a module without a hint
RULE = "=" * 70
THIN = "-" * 70


def parse_jobs(value: str) -> int:
    """``auto`` is min(cpu count, 4); otherwise a positive integer."""
    if value == "auto":
        return max(1, min(os.cpu_count() or 1, 4))
    try:
        jobs = int(value)
    except ValueError:
        jobs = 0
    if jobs < 1:
        raise ValueError(f"--jobs must be a positive integer or 'auto', not {value!r}")
    return jobs


def discover_modules(start: str, top: str) -> list[str]:
    """Dotted names of the ``test*.py`` modules ``unittest discover`` would load, sorted."""
    top_dir = Path(top).resolve()
    names: list[str] = []

    def walk(directory: Path) -> None:
        for entry in sorted(directory.iterdir()):
            if entry.is_file() and entry.name.startswith("test") and entry.suffix == ".py":
                names.append(".".join(entry.relative_to(top_dir).with_suffix("").parts))
            elif entry.is_dir() and (entry / "__init__.py").is_file():
                walk(entry)

    walk(Path(start).resolve())
    return sorted(names)


def load_hints() -> dict[str, float]:
    try:
        data = json.loads(HINTS.read_text("utf-8"))
        return {str(k): float(v) for k, v in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def schedule(modules: list[str], hints: dict[str, float]) -> list[str]:
    """Longest first (by hint, then name), so the long modules start before the short ones."""
    return sorted(modules, key=lambda m: (-hints.get(m, DEFAULT_HINT), m))


def run_worker(module: str, result_path: str, top: str, verbose: bool) -> int:
    """Run one module and write its outcome as JSON for the parent to aggregate."""
    top_dir = str(Path(top).resolve())
    if top_dir not in sys.path:
        sys.path.insert(0, top_dir)
    suite = unittest.defaultTestLoader.loadTestsFromName(module)
    runner = unittest.TextTestRunner(stream=io.StringIO(), verbosity=2 if verbose else 1)
    result = runner.run(suite)

    def block(kind: str, test: object, trace: str) -> str:
        return f"{RULE}\n{kind}: {result.getDescription(test)}\n{THIN}\n{trace}"  # type: ignore[attr-defined]

    outcome = {
        "module": module,
        "ran": result.testsRun,
        "failures": [block("FAIL", t, tb) for t, tb in result.failures],
        "errors": [block("ERROR", t, tb) for t, tb in result.errors],
        "unexpected_successes": [t.id() for t in result.unexpectedSuccesses],
        "expected_failures": len(result.expectedFailures),
        "skipped": [[t.id(), str(reason)] for t, reason in result.skipped],
    }
    Path(result_path).write_text(json.dumps(outcome), "utf-8")
    return 0


def run_parallel(modules: list[str], jobs: int, start: str, top: str, verbose: bool
                 ) -> tuple[list[dict], dict[str, str]]:
    """Run each module in its own process, at most ``jobs`` at a time."""
    pending = schedule(modules, load_hints())
    outcomes: dict[str, dict] = {}
    captured: dict[str, str] = {}
    lock = threading.Lock()
    with tempfile.TemporaryDirectory(prefix="txray-runner-") as tmp:
        env = dict(os.environ)
        if not env.get(SHARED_ENV):
            shared = Path(tmp) / "shared"
            shared.mkdir()
            env[SHARED_ENV] = str(shared)

        def work() -> None:
            while True:
                with lock:
                    if not pending:
                        return
                    module = pending.pop(0)
                path = Path(tmp) / (module + ".json")
                proc = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve()), "--start", start, "--top",
                     top, "--worker", module, "--result", str(path),
                     *(["--verbose"] if verbose else [])],
                    capture_output=True, env=env)
                text = proc.stderr.decode("utf-8", "replace")
                try:
                    outcome = json.loads(path.read_text("utf-8"))
                except (OSError, ValueError):
                    outcome = {
                        "module": module, "ran": 0, "failures": [], "unexpected_successes": [],
                        "expected_failures": 0, "skipped": [],
                        "errors": [f"{RULE}\nERROR: worker for {module} exited with status "
                                   f"{proc.returncode} without a result\n{THIN}\n{text[-4000:]}"],
                    }
                with lock:
                    outcomes[module] = outcome
                    captured[module] = text

        threads = [threading.Thread(target=work) for _ in range(max(1, min(jobs, len(pending))))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    return [outcomes[m] for m in sorted(outcomes)], captured


def summarize(results: list[dict], captured: dict[str, str], seconds: float) -> bool:
    """Print the combined report in unittest's format; return whether the run succeeded."""
    ran = sum(r["ran"] for r in results)
    failures = [b for r in results for b in r["failures"]]
    errors = [b for r in results for b in r["errors"]]
    unexpected = [i for r in results for i in r["unexpected_successes"]]
    skipped = sum(len(r["skipped"]) for r in results)
    expected = sum(r["expected_failures"] for r in results)
    for module in sorted(captured):  # what the tests themselves printed, in module order
        text = captured[module].strip()
        if text:
            print(f"[{module}]\n{text}", file=sys.stderr)
    print(file=sys.stderr)
    for block in errors + failures:
        print(block, file=sys.stderr)
    for ident in unexpected:
        print(f"{RULE}\nUNEXPECTED SUCCESS: {ident}\n", file=sys.stderr)
    print(THIN, file=sys.stderr)
    print(f"Ran {ran} test{'s' if ran != 1 else ''} in {seconds:.3f}s\n", file=sys.stderr)
    success = not failures and not errors and not unexpected
    infos = []
    if not success:
        print("FAILED", end="", file=sys.stderr)
        if failures:
            infos.append(f"failures={len(failures)}")
        if errors:
            infos.append(f"errors={len(errors)}")
    else:
        print("OK", end="", file=sys.stderr)
    if skipped:
        infos.append(f"skipped={skipped}")
    if expected:
        infos.append(f"expected failures={expected}")
    if unexpected:
        infos.append(f"unexpected successes={len(unexpected)}")
    print(f" ({', '.join(infos)})" if infos else "", file=sys.stderr)
    return success


def apply_skip_rule(success: bool, skips: list[tuple[str, str]]) -> int:
    """The strict rule: with the upstream required, a skip that is not ``optional:`` fails."""
    required = bool(os.environ.get(ENV))
    optional = [(i, r) for i, r in skips if str(r).startswith(OPTIONAL)]
    skipped = [(i, r) for i, r in skips if not str(r).startswith(OPTIONAL)]
    if optional:
        print(f"\n{len(optional)} optional integration test(s) not run (never required):",
              file=sys.stderr)
        for ident, reason in optional:
            print(f"  {ident}: {reason}", file=sys.stderr)
    if skipped:
        print(f"\n{len(skipped)} test(s) skipped:", file=sys.stderr)
        for ident, reason in skipped:
            print(f"  {ident}: {reason}", file=sys.stderr)
        if required:
            print(f"error: {ENV} is set, so no test may be skipped", file=sys.stderr)
            return 1
        print(f"notice: set {ENV} to a local upstream clone to run them (CI always does)",
              file=sys.stderr)
    return 0 if success else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--start", default="tests", help="discovery start directory")
    parser.add_argument("--top", default=".", help="top-level directory of the project")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--jobs", default=None,
                        help=f"worker processes: N or 'auto' (default: ${JOBS_ENV}, else 1)")
    parser.add_argument("--worker", help=argparse.SUPPRESS)  # internal: run one module
    parser.add_argument("--result", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)  # FA-015: the suite imports timelinexray from the checkout
    if args.worker:
        return run_worker(args.worker, args.result, args.top, args.verbose)
    try:
        jobs = parse_jobs(args.jobs or os.environ.get(JOBS_ENV) or "1")
    except ValueError as exc:
        parser.error(str(exc))
    if jobs > 1:
        began = time.perf_counter()
        results, captured = run_parallel(discover_modules(args.start, args.top), jobs,
                                         args.start, args.top, args.verbose)
        success = summarize(results, captured, time.perf_counter() - began)
        skips = [(ident, reason) for r in results for ident, reason in r["skipped"]]
        return apply_skip_rule(success, skips)
    suite = unittest.defaultTestLoader.discover(args.start, top_level_dir=args.top)
    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1)
    result = runner.run(suite)
    return apply_skip_rule(result.wasSuccessful(),
                           [(test.id(), str(reason)) for test, reason in result.skipped])


if __name__ == "__main__":
    raise SystemExit(main())
