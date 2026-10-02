#!/usr/bin/env python3
"""Fetch the upstream repository at the pinned test commit (developer and CI helper).

Not part of the ``timelinexray`` package and never imported by it. Creates DIR (a plain git
repository without a checkout) whose ``main`` is the pinned commit with its full history,
which is what ``TXRAY_TEST_UPSTREAM`` must point to so that no upstream-dependent test is
skipped. Already present: nothing is fetched. Uses the system ``git`` with user and system
configuration ignored and the single https transport allowed.

    python scripts/fetch_test_upstream.py DIR [--url URL] [--commit SHA]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

URL = "https://github.com/xai-org/x-algorithm.git"
COMMIT = "77d431aabf409ca1c1eed9bec7e2183f7c914e23"
HISTORY = 39  # commits reachable from COMMIT
# Transport policy for every git call: only https (tests widen it to a local file:// fixture).
TRANSPORT = ("-c", "protocol.allow=never", "-c", "protocol.https.allow=always")


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               LC_ALL="C")
    return subprocess.run(["git", *TRANSPORT, "-c", f"core.hooksPath={os.devnull}", *args],
                          capture_output=True, text=True, env=env, check=check)


def _present(directory: Path, commit: str) -> bool:
    proc = _git("-C", str(directory), "cat-file", "-e", f"{commit}^{{commit}}", check=False)
    return proc.returncode == 0


def fetch(directory: Path, url: str, commit: str) -> None:
    """Create DIR as a repository without a checkout whose ``main`` is COMMIT.

    ``git init`` leaves ``HEAD`` on the unborn ``main``; git refuses to fetch into the branch
    that ``HEAD`` names unless ``--update-head-ok`` is given (``refusing to fetch into branch
    'refs/heads/main' checked out``), which is safe here because nothing is checked out.
    """
    directory.mkdir(parents=True, exist_ok=True)
    _git("init", "--quiet", "--template=", "--initial-branch=main", str(directory))
    _git("-C", str(directory), "fetch", "--quiet", "--no-tags", "--update-head-ok", url,
         f"{commit}:refs/heads/main")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--commit", default=COMMIT)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        parser.error("--commit must be a full 40-digit commit id")
    if not args.url.startswith("https://"):
        parser.error("--url must be an https URL")
    directory: Path = args.directory
    if not (directory.exists() and _present(directory, args.commit)):
        try:
            fetch(directory, args.url, args.commit)
        except subprocess.CalledProcessError as error:
            print(f"error: git {' '.join(error.cmd[-6:])} failed with exit status "
                  f"{error.returncode}: {(error.stderr or '').strip()}", file=sys.stderr)
            return 1
    if not _present(directory, args.commit):
        print(f"error: {args.commit} is not in {directory}", file=sys.stderr)
        return 1
    count = int(_git("-C", str(directory), "rev-list", "--count", args.commit).stdout.strip())
    if args.commit == COMMIT and count != HISTORY:
        print(f"error: {args.commit} has {count} commits of history, expected {HISTORY}",
              file=sys.stderr)
        return 1
    print(f"upstream {args.commit} with {count} commits of history in {directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
