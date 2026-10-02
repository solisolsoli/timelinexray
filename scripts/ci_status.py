#!/usr/bin/env python3
"""Report the GitHub check runs, commit statuses and workflow runs of one commit.

A developer script for the release gate - not part of the ``timelinexray`` package and
never imported by it (the package itself never uses the network except its guarded
``git fetch``). It asks the GitHub REST API about one commit of one repository and exits 0
only when there is at least one check and every check run, commit status and workflow run
concluded ``success``. Otherwise it prints a table and exits non-zero:

====  =====================================================================================
0     every check concluded ``success``
1     at least one check concluded otherwise (``failure``, ``cancelled``, ``timed_out``,
      ``action_required``, ``neutral``, ``skipped``, ``stale``, ``startup_failure``, error)
2     usage error
3     nothing failed, but at least one check is still pending (queued, in progress, ...)
4     missing: the commit has no checks at all, or a ``--require``-d check name is absent
5     the API could not be queried (HTTP or network error)
====  =====================================================================================

Superseded runs (FA-017). Re-running a workflow run (``Re-run jobs``) replaces that run's
conclusion, but dispatching the workflow again on the same commit adds a new run, so the
first, failed run would stay in the list forever. Only the latest run (by ``created_at``,
then ``run_attempt`` and ``id``) of each workflow and kind of tested tree counts: the
commit itself (``push``, ``workflow_dispatch`` and every other event) or a pull-request
merge (``pull_request*`` events, which test a merge commit, not the commit alone). Older
runs and the check runs of their check suites are listed as ``superseded`` and do not
affect the verdict. Commit statuses are already the latest per context.

A token is read only from the ``GITHUB_TOKEN`` environment variable (optional for public
repositories; needed for private ones). It is sent only as the ``Authorization`` header to
the API host and is never printed, logged or written anywhere. The HTTP layer is
injectable (:class:`GitHub` takes an ``opener``), so the tests run with canned responses
and no network.

    python scripts/ci_status.py --repo OWNER/NAME --commit SHA [--require NAME ...] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

API = "https://api.github.com"
TOKEN_ENV = "GITHUB_TOKEN"
REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
COMMIT = re.compile(r"[0-9a-f]{40}")
PENDING_STATES = {"queued", "in_progress", "waiting", "requested", "pending", "expected"}
MAX_PAGES = 10

SUCCESS, FAILURE, PENDING, MISSING = "success", "failure", "pending", "missing"
SUPERSEDED = "superseded"
EXIT = {SUCCESS: 0, FAILURE: 1, PENDING: 3, MISSING: 4}


class ApiError(RuntimeError):
    pass


Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - https only, checked


class GitHub:
    """A minimal read-only REST client (GET only) with an injectable opener."""

    def __init__(self, api: str = API, token: str | None = None, opener: Opener | None = None,
                 timeout: float = 30.0) -> None:
        parsed = urllib.parse.urlsplit(api)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("the API URL must be https")
        self.api = api.rstrip("/")
        self._token = token
        self.opener = opener or _default_opener
        self.timeout = timeout

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = f"{self.api}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "timelinexray-ci-status"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self.opener(request, self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            hint = " (a private repository needs GITHUB_TOKEN)" if exc.code in (401, 403, 404) else ""
            raise ApiError(f"GET {path} failed: HTTP {exc.code}{hint}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ApiError(f"GET {path} failed: {type(exc).__name__}") from None

    def pages(self, path: str, key: str, params: dict[str, str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            data = self.get(path, {**params, "per_page": "100", "page": str(page)})
            batch = data.get(key, []) if isinstance(data, dict) else []
            items.extend(batch)
            total = data.get("total_count") if isinstance(data, dict) else None
            if len(batch) < 100 or (isinstance(total, int) and len(items) >= total):
                break
        return items


@dataclass(frozen=True)
class Check:
    kind: str  # check-run, status, workflow-run
    name: str
    state: str  # success, failure, pending, superseded
    detail: str

    def row(self) -> list[str]:
        return [self.kind, self.name, self.state, self.detail]


def _classify(status: str | None, conclusion: str | None) -> tuple[str, str]:
    if status and status != "completed":
        return PENDING, status
    if conclusion == "success":
        return SUCCESS, "success"
    if conclusion is None:
        return PENDING, status or "unknown"
    return FAILURE, conclusion


def _number(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def superseded_runs(runs: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """``{index: latest run}`` for every workflow run that a later run of the same workflow
    and kind of tested tree supersedes (see the module documentation)."""
    latest: dict[tuple[str, str], int] = {}

    def key(run: dict[str, Any]) -> tuple[str, str]:
        workflow = run.get("workflow_id") or run.get("path") or run.get("name")
        tree = "merge" if str(run.get("event") or "").startswith("pull_request") else "commit"
        return str(workflow), tree

    def order(run: dict[str, Any]) -> tuple[str, int, int]:
        return str(run.get("created_at") or ""), _number(run.get("run_attempt")), \
            _number(run.get("id"))

    for index, run in enumerate(runs):
        best = latest.get(key(run))
        if best is None or order(run) > order(runs[best]):
            latest[key(run)] = index
    return {index: runs[latest[key(run)]] for index, run in enumerate(runs)
            if latest[key(run)] != index}


def collect(client: GitHub, repo: str, commit: str) -> list[Check]:
    base = f"/repos/{repo}"
    checks: list[Check] = []
    runs = client.pages(f"{base}/actions/runs", "workflow_runs", {"head_sha": commit})
    older = superseded_runs(runs)
    current_suites = {run.get("check_suite_id") for index, run in enumerate(runs)
                      if index not in older}
    old_suites: dict[Any, dict[str, Any]] = {}
    for index, later in older.items():
        suite = runs[index].get("check_suite_id")
        if suite is not None and suite not in current_suites:
            old_suites[suite] = later
    for run in client.pages(f"{base}/commits/{commit}/check-runs", "check_runs", {}):
        state, detail = _classify(run.get("status"), run.get("conclusion"))
        check_suite = run.get("check_suite")
        suite = check_suite.get("id") if isinstance(check_suite, dict) else None
        if suite is not None and suite in old_suites:
            state, detail = SUPERSEDED, f"{detail}; its workflow ran again (run " \
                f"{old_suites[suite].get('id')})"
        checks.append(Check("check-run", str(run.get("name")), state, detail))
    combined = client.get(f"{base}/commits/{commit}/status")
    for status in combined.get("statuses", []) if isinstance(combined, dict) else []:
        value = status.get("state")
        state = SUCCESS if value == "success" else PENDING if value in PENDING_STATES else FAILURE
        checks.append(Check("status", str(status.get("context")), state, str(value)))
    for index, run in enumerate(runs):
        state, detail = _classify(run.get("status"), run.get("conclusion"))
        if index in older:
            state, detail = SUPERSEDED, f"{detail}; superseded by run {older[index].get('id')}"
        checks.append(Check("workflow-run", str(run.get("name")), state, detail))
    return sorted(checks, key=lambda c: (c.kind, c.name, c.state))


def verdict(checks: list[Check], required: list[str]) -> tuple[str, list[str]]:
    """The overall state and the required names that are missing (superseded runs are
    listed but never counted)."""
    checks = [check for check in checks if check.state != SUPERSEDED]
    names = {check.name for check in checks}
    missing = [name for name in required if name not in names]
    if any(check.state == FAILURE for check in checks):
        return FAILURE, missing
    if missing or not checks:
        return MISSING, missing
    if any(check.state == PENDING for check in checks):
        return PENDING, missing
    return SUCCESS, missing


def table(checks: list[Check], missing: list[str]) -> str:
    rows = [["KIND", "NAME", "STATE", "DETAIL"], *(check.row() for check in checks),
            *(["required", name, MISSING, "no check with this name"] for name in missing)]
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    return "\n".join("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
                     for row in rows)


def main(argv: list[str] | None = None, *, opener: Opener | None = None,
         environ: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, metavar="OWNER/NAME")
    parser.add_argument("--commit", required=True, metavar="SHA", help="full 40-hex commit id")
    parser.add_argument("--require", action="append", default=[], metavar="NAME",
                        help="a check or workflow name that must be present (repeatable)")
    parser.add_argument("--api", default=API, help="API base URL (https)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not REPO.fullmatch(args.repo):
        parser.error("--repo must be OWNER/NAME")
    if not COMMIT.fullmatch(args.commit):
        parser.error("--commit must be a full 40-digit lowercase hexadecimal commit id")
    environ = os.environ if environ is None else environ
    try:
        client = GitHub(args.api, token=environ.get(TOKEN_ENV) or None, opener=opener)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        checks = collect(client, args.repo, args.commit)
    except ApiError as exc:
        print(f"ci_status: error: {exc}", file=sys.stderr)
        return 5
    state, missing = verdict(checks, args.require)
    if args.json:
        print(json.dumps({"repo": args.repo, "commit": args.commit, "state": state,
                          "missing": missing,
                          "checks": [check.__dict__ for check in checks]}, indent=2, sort_keys=True))
    else:
        print(f"{args.repo} @ {args.commit}: {state.upper()}")
        print(table(checks, missing) if checks or missing else "(no checks reported)")
    return EXIT[state]


if __name__ == "__main__":
    raise SystemExit(main())
