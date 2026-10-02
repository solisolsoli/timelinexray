#!/usr/bin/env python3
"""Check the GitHub Actions workflows statically (developer and CI script, stdlib only).

Not part of the ``timelinexray`` package and never imported by it. Each file in
``.github/workflows`` is read with the project's strict YAML-subset reader
(``timelinexray.findings.yamlsub``: block style only, so the workflow has to be written in
that subset) and checked:

* every ``uses:`` is pinned to a full 40-hex commit SHA, with a comment naming the tag;
* ``permissions`` is ``contents: read`` at the top and nothing grants more in a job;
* no secret is referenced (``secrets.``, ``GITHUB_TOKEN``) and no event text is
  interpolated into a command (``${{ github.event...`` / ``github.head_ref`` in ``run:``);
* only ``push``, ``pull_request``, ``workflow_dispatch`` and ``schedule`` trigger it (no
  ``pull_request_target``, ``workflow_run`` or any other event; a schedule is allowed
  because the workflow has no secrets and read-only permissions: it catches drift of the
  runner image, Python, SQLite and git);
* concurrency cancels superseded runs; every job has a ``timeout-minutes``;
* every checkout sets ``persist-credentials: false``;
* every job runs exactly ``make ci``, and every other ``run:`` step is an allowed setup
  command;
* every job runs the SQLite preflight ``python scripts/check_sqlite.py`` (FTS5 with the
  trigram tokenizer, FA-023) before ``make ci``, so an unsuitable runner fails in seconds;
* the matrix covers ``ubuntu-latest`` and ``macos-latest`` and exactly the Python versions
  that ``pyproject.toml`` declares (classifiers), whose lowest equals ``requires-python``;
* parity: the ``ci`` target of the Makefile has exactly the documented ``# ci-steps:``
  list as its prerequisites (after ``pycheck``), and ``CONTRIBUTING.md`` documents the same
  steps.

    python scripts/check_workflows.py [--root DIR] [--json]

Exit status 0 when every check passes, 1 otherwise (each problem is printed).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}$")
TAG_COMMENT = re.compile(r"#\s*v[0-9]+(\.[0-9]+)*")
ALLOWED_EVENTS = {"push", "pull_request", "workflow_dispatch", "schedule"}
OPERATING_SYSTEMS = ["ubuntu-latest", "macos-latest"]
PREFLIGHT = "python scripts/check_sqlite.py"
SETUP_COMMANDS = (re.compile(r'^python scripts/fetch_test_upstream\.py "\$RUNNER_TEMP/[A-Za-z0-9_.-]+"$'),
                  re.compile("^" + re.escape(PREFLIGHT) + "$"))
INJECTION = re.compile(r"\$\{\{\s*github\.(event|head_ref)")


def _load_yaml(text: str) -> Any:
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from timelinexray.findings.yamlsub import load
    finally:
        sys.path.pop(0)
    return load(text)


def python_versions(pyproject: str) -> tuple[list[str], str]:
    """Declared ``3.X`` classifiers (sorted) and the ``requires-python`` lower bound."""
    project = tomllib.loads(pyproject)["project"]
    versions = sorted(
        (item.rsplit(":: ", 1)[1] for item in project.get("classifiers", [])
         if re.fullmatch(r"Programming Language :: Python :: 3\.[0-9]+", item)),
        key=lambda v: tuple(int(p) for p in v.split(".")))
    match = re.fullmatch(r">=\s*(3\.[0-9]+)", project.get("requires-python", ""))
    return versions, match.group(1) if match else ""


def makefile_steps(makefile: str) -> tuple[list[str], list[str]]:
    """``(documented ci-steps, prerequisites of the ci target without pycheck)``."""
    documented = re.search(r"^# ci-steps:(.*)$", makefile, re.MULTILINE)
    target = re.search(r"^ci:(.*)$", makefile, re.MULTILINE)
    steps = documented.group(1).split() if documented else []
    prerequisites = [p for p in (target.group(1).split() if target else []) if p != "pycheck"]
    return steps, prerequisites


def check_workflow(name: str, text: str, versions: list[str], minimum: str) -> list[str]:
    problems: list[str] = []

    def problem(message: str) -> None:
        problems.append(f"{name}: {message}")

    try:
        data = _load_yaml(text)
    except Exception as exc:  # the reader names the line
        return [f"{name}: not in the strict YAML subset: {exc}"]
    if not isinstance(data, dict):
        return [f"{name}: the workflow must be a mapping"]
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.split("#", 1)[0] if "uses:" not in line else line
        if re.search(r"secrets\.|GITHUB_TOKEN", stripped):
            problem(f"line {number}: references a secret or token")
        if "uses:" in line and not TAG_COMMENT.search(line):
            problem(f"line {number}: a pinned action needs a comment naming its tag")
    events = data.get("on")
    events = set(events) if isinstance(events, dict) else {events} if isinstance(events, str) else set()
    if not events or not events <= ALLOWED_EVENTS:
        problem(f"triggers must be a subset of {sorted(ALLOWED_EVENTS)}, got {sorted(events)}")
    if data.get("permissions") != {"contents": "read"}:
        problem("top-level permissions must be exactly 'contents: read'")
    concurrency = data.get("concurrency")
    if not isinstance(concurrency, dict) or concurrency.get("cancel-in-progress") != "true" \
            or not concurrency.get("group"):
        problem("concurrency must set a group and cancel-in-progress: true")
    jobs = data.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        return problems + [f"{name}: no jobs"]
    for job_id, job in jobs.items():
        where = f"job {job_id}"
        if not isinstance(job, dict):
            problem(f"{where}: must be a mapping")
            continue
        permissions = job.get("permissions")
        if permissions is not None and (not isinstance(permissions, dict) or any(
                value not in ("read", "none") for value in permissions.values())):
            problem(f"{where}: job permissions may only be read or none")
        timeout = job.get("timeout-minutes")
        if not (isinstance(timeout, str) and timeout.isdigit() and 1 <= int(timeout) <= 360):
            problem(f"{where}: timeout-minutes (1-360) is required")
        matrix = (job.get("strategy") or {}).get("matrix") or {}
        if matrix.get("os") != OPERATING_SYSTEMS:
            problem(f"{where}: matrix os must be {OPERATING_SYSTEMS}, got {matrix.get('os')}")
        if matrix.get("python") != versions:
            problem(f"{where}: matrix python {matrix.get('python')} must equal the declared "
                    f"versions {versions}")
        if not versions or versions[0] != minimum:
            problem(f"{where}: the lowest declared version {versions[:1]} must equal "
                    f"requires-python >={minimum}")
        steps = job.get("steps")
        if not isinstance(steps, list) or not steps:
            problem(f"{where}: no steps")
            continue
        make_ci = 0
        preflight_first = False
        for index, step in enumerate(steps, 1):
            label = f"{where} step {index}"
            if not isinstance(step, dict):
                problem(f"{label}: must be a mapping")
                continue
            uses, run = step.get("uses"), step.get("run")
            if uses is not None:
                if not isinstance(uses, str) or not PINNED.match(uses):
                    problem(f"{label}: '{uses}' is not pinned to a full commit SHA")
                elif uses.split("@")[0] == "actions/checkout":
                    if (step.get("with") or {}).get("persist-credentials") != "false":
                        problem(f"{label}: checkout must set persist-credentials: false")
            if run is not None:
                if not isinstance(run, str):
                    problem(f"{label}: run must be a single command line")
                elif INJECTION.search(run):
                    problem(f"{label}: event text is interpolated into a command")
                elif run == "make ci":
                    make_ci += 1
                elif run == PREFLIGHT and not make_ci:
                    preflight_first = True
                elif not any(pattern.match(run) for pattern in SETUP_COMMANDS):
                    problem(f"{label}: '{run}' is neither 'make ci' nor an allowed setup command")
        if make_ci != 1:
            problem(f"{where}: must run 'make ci' exactly once (found {make_ci})")
        if not preflight_first:
            problem(f"{where}: must run the SQLite preflight '{PREFLIGHT}' before 'make ci'")
    return problems


def check_parity(makefile: str, contributing: str) -> list[str]:
    steps, prerequisites = makefile_steps(makefile)
    problems = []
    if not steps:
        problems.append("Makefile: no '# ci-steps:' documentation line")
    if steps != prerequisites:
        problems.append(f"Makefile: ci target runs {prerequisites}, documented {steps}")
    documented = re.search(r"make ci` runs, in order: (.+?)\.", contributing.replace("\n", " "))
    listed = re.findall(r"`([a-z-]+)`", documented.group(1)) if documented else []
    if listed != steps:
        problems.append(f"CONTRIBUTING.md: documents make ci as {listed}, Makefile has {steps}")
    return problems


def check(root: Path = ROOT) -> list[str]:
    versions, minimum = python_versions((root / "pyproject.toml").read_text("utf-8"))
    workflows = sorted((root / ".github" / "workflows").glob("*.y*ml"))
    problems = [] if workflows else ["no workflow in .github/workflows"]
    for path in workflows:
        problems += check_workflow(path.name, path.read_text("utf-8"), versions, minimum)
    contributing = root / "CONTRIBUTING.md"
    problems += check_parity((root / "Makefile").read_text("utf-8"),
                             contributing.read_text("utf-8") if contributing.exists() else "")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    problems = check(args.root)
    if args.json:
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
    elif problems:
        print("\n".join(f"workflow check: {item}" for item in problems), file=sys.stderr)
    else:
        print("workflow check: ok")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
