#!/usr/bin/env python3
"""Preflight: does this Python's SQLite have FTS5 with the trigram tokenizer? (FA-023)

Not part of the ``timelinexray`` package and never imported by it; standard library only.
The code index needs SQLite 3.34 or newer built with FTS5 (``tokenize = 'trigram'``,
the same probe as ``timelinexray.index.schema``). The CI workflow runs this right after
setting up Python (it also prints the full Python version and ``git --version``, so drift
of the runner is traceable), and ``make pycheck`` (the first prerequisite of every Makefile target,
``make ci`` included) runs it locally, so an unsuitable interpreter fails in seconds with
the reason instead of deep inside the test run.

    python scripts/check_sqlite.py

It also prints, for information only (never a failure), whether an IANA time zone database
is available: ``txray metrics`` needs one for any zone other than UTC.

Exit status 0 when the probe succeeds, 1 otherwise (the reason is printed on stderr).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import zoneinfo

PROBE = "CREATE VIRTUAL TABLE temp.txray_probe USING fts5(x, tokenize = 'trigram')"


def check() -> str | None:
    """``None`` when FTS5 with the trigram tokenizer works, else the reason."""
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(PROBE)
            connection.execute("DROP TABLE temp.txray_probe")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        return (f"this Python's SQLite {sqlite3.sqlite_version} lacks FTS5 with the trigram "
                f"tokenizer (SQLite 3.34 or newer built with FTS5 is required): {exc}")
    return None


def git_version() -> str:
    """``git --version`` as printed, or why it could not be run."""
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable ({exc})"
    return proc.stdout.strip() or f"unavailable (exit {proc.returncode})"


def tz_database() -> str:
    """One line about the IANA time zone database (information only)."""
    try:
        zones = zoneinfo.available_timezones()
    except Exception as exc:  # information only: nothing here may fail the preflight
        return f"unknown ({exc})"
    if zones:
        return f"found ({len(zones)} zones)"
    return ("none found (install the tzdata package; txray metrics then accepts only the "
            "time zone UTC)")


def main() -> int:
    problem = check()
    if problem:
        print(f"sqlite check: {problem}", file=sys.stderr)
        return 1
    print(f"sqlite check: SQLite {sqlite3.sqlite_version} with FTS5 and the trigram "
          f"tokenizer (Python {sys.version.split()[0]})")
    print(f"python: {' '.join(sys.version.split())}")
    print(f"git: {git_version()}")
    print(f"time zone database: {tz_database()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
