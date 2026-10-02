#!/usr/bin/env python3
"""Check that a git history is fit to publish (developer script, stdlib only).

Not part of the ``timelinexray`` package and never imported by it. Every commit reachable
from ``REF`` (default ``HEAD``; any ``git rev-list`` argument such as ``A..B`` works) must
satisfy the publishing rules of ``AGENTS.md``:

* the author and the committer time zone offsets are ``+0000`` (a commit page would
  otherwise reveal the author's time zone and, with the times, working hours);
* the author and the committer are exactly the project identity
  (``solisolsoli <solisolsoli@users.noreply.github.com>``; ``--identity`` overrides);
* the message carries no trailer: no ``Co-Authored-By``, ``Signed-off-by`` or similar line
  anywhere, and the last paragraph is not a block of ``Token: value`` lines;
* the message contains no personal data: no local absolute path, no e-mail address
  outside the GitHub noreply and the example or reserved domains, no secret-looking
  string, and nothing matching the regular expressions of ``--patterns FILE`` (one per
  line; the maintainer keeps that file outside the repository).

With ``--fsck`` the repository must also hold no dangling object (``git fsck
--no-reflogs``): dangling objects are never pushed, but an earlier version of a commit
that was amended or reset away stays in the object store until it is pruned, and a
publication should leave no doubt. The script prints the pruning commands; it never runs
them (pruning changes nothing reachable, but a repository shared by several worktrees or
agents should be pruned by its owner only).

    python scripts/check_release_history.py [REF] [--repo DIR] [--identity 'Name <email>']
                                            [--patterns FILE] [--fsck] [--json]

Exit status 0 when the history is clean, 1 when a problem is found (each one is printed
with its commit), 2 for a usage error or a git failure.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

IDENTITY = "solisolsoli <solisolsoli@users.noreply.github.com>"
RECORD, FIELD = "\x1e", "\x00"  # what git prints for %x1e and %x00 (argv cannot hold a NUL)
LOG_FORMAT = "%x00".join(("%H", "%an <%ae>", "%ad", "%cn <%ce>", "%cd", "%B")) + "%x1e"
RAW_DATE = re.compile(r"^-?[0-9]+ ([+-][0-9]{4})$")

# Trailer tokens that must not appear anywhere in a message (case-insensitive).
TRAILER_TOKENS = ("co-authored-by", "signed-off-by", "reviewed-by", "acked-by", "tested-by",
                  "reported-by", "suggested-by", "helped-by", "change-id", "cc")
TRAILER_LINE = re.compile(r"^(?:[A-Za-z0-9-]+)\s*:\s+\S|^\(cherry picked from commit [0-9a-f]+\)$")
TRAILER_ANYWHERE = re.compile(r"^(" + "|".join(TRAILER_TOKENS) + r")\s*:", re.IGNORECASE)

# Personal data in messages. The patterns are written in pieces so that the project's own
# hygiene scan of this file does not match them.
PERSONAL = {
    "local path": re.compile("/" + r"Users/[A-Za-z]|/" + r"home/[a-z]|[A-Za-z]:\\+Users\\+"),
    "secret": re.compile("|".join((
        "gh" + "p_[A-Za-z0-9]{20,}",
        "github" + "_pat_[A-Za-z0-9_]{20,}",
        "gh[ousr]" + "_[A-Za-z0-9]{20,}",
        "s" + "k-[A-Za-z0-9_-]{20,}",
        "AK" + "IA[0-9A-Z]{16}",
        "xo" + "x[baprs]-[A-Za-z0-9-]{10,}",
        "AI" + "za[0-9A-Za-z_-]{30,}",
        "-----BEGIN [A-Z ]*" + "PRIVATE KEY-----",
        "[Bb]earer" + " [A-Za-z0-9._~+/-]{20,}",
    ))),
}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ALLOWED_EMAIL = re.compile(
    r"@(users\.noreply\.github\.com|github\.com|[A-Za-z0-9.-]*example(\.[a-z]+)?|"
    r"[A-Za-z0-9.-]+\.(invalid|test|example|localhost))$")
PRUNE_COMMANDS = ("git reflog expire --expire=now --all", "git gc --prune=now")


class GitFailed(RuntimeError):
    pass


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          errors="replace")
    if proc.returncode != 0:
        raise GitFailed(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[-500:]}")
    return proc.stdout


def read_commits(repo: Path, ref: str) -> list[dict[str, str]]:
    """``[{"commit", "author", "author_date", "committer", "committer_date", "message"}]``."""
    out = git(repo, "log", "--date=raw", f"--format={LOG_FORMAT}", ref, "--")
    commits = []
    for record in out.split(RECORD):
        if not record.strip():
            continue
        fields = record.lstrip("\n").split(FIELD)
        if len(fields) != 6:
            raise GitFailed("unexpected git log record")
        commits.append(dict(zip(("commit", "author", "author_date", "committer",
                                 "committer_date", "message"), fields)))
    return commits


def _offset(raw_date: str) -> str:
    match = RAW_DATE.match(raw_date.strip())
    return match.group(1) if match else raw_date.strip()


def message_trailers(message: str) -> list[str]:
    """Trailer lines of ``message``: known tokens anywhere, or a trailing ``Token: value`` block."""
    lines = message.rstrip("\n").split("\n")
    found = [line for line in lines if TRAILER_ANYWHERE.match(line.strip())]
    paragraphs = [p for p in re.split(r"\n\s*\n", message.strip()) if p.strip()]
    if len(paragraphs) >= 2:
        last = [line for line in paragraphs[-1].split("\n") if line.strip()]
        if last and all(TRAILER_LINE.match(line) or line[:1].isspace() for line in last):
            found += [line for line in last if line not in found]
    return found


def personal_data(message: str, extra: list[re.Pattern[str]]) -> list[str]:
    problems = []
    for label, pattern in PERSONAL.items():
        match = pattern.search(message)
        if match:
            problems.append(f"{label}: {match.group(0)[:40]!r}")
    for match in EMAIL.finditer(message):
        if not ALLOWED_EMAIL.search(match.group(0)):
            problems.append(f"e-mail address: {match.group(0)!r}")
    for pattern in extra:
        match = pattern.search(message)
        if match:
            problems.append(f"pattern {pattern.pattern!r}: {match.group(0)[:40]!r}")
    return problems


def check_commits(commits: list[dict[str, str]], identity: str,
                  extra: list[re.Pattern[str]]) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []

    def problem(commit: str, code: str, message: str) -> None:
        problems.append({"commit": commit, "code": code, "message": message})

    for entry in commits:
        sha = entry["commit"]
        for role in ("author", "committer"):
            offset = _offset(entry[f"{role}_date"])
            if offset != "+0000":
                problem(sha, "timezone", f"{role} time zone offset is {offset}, not +0000")
            if entry[role] != identity:
                problem(sha, "identity", f"{role} is {entry[role]!r}, not {identity!r}")
        for line in message_trailers(entry["message"]):
            problem(sha, "trailer", f"trailer line {line.strip()[:60]!r}")
        for text in personal_data(entry["message"], extra):
            problem(sha, "personal_data", text)
    return problems


def dangling_objects(repo: Path) -> list[str]:
    """``git fsck --no-reflogs`` lines naming dangling or unreachable objects."""
    proc = subprocess.run(["git", "-C", str(repo), "fsck", "--no-reflogs", "--no-progress"],
                          capture_output=True, text=True, errors="replace")
    if proc.returncode not in (0, 1, 2, 4):  # fsck exit codes are bit flags, 0 = fine
        raise GitFailed(f"git fsck failed: {proc.stderr.strip()[-500:]}")
    lines = (proc.stdout + "\n" + proc.stderr).splitlines()
    return sorted(line.strip() for line in lines
                  if line.startswith(("dangling ", "unreachable ")))


def load_patterns(path: Path | None) -> list[re.Pattern[str]]:
    if path is None:
        return []
    patterns = []
    for number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line, re.IGNORECASE))
        except re.error as exc:
            raise ValueError(f"{path}:{number}: invalid regular expression ({exc})") from None
    return patterns


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ref", nargs="?", default="HEAD",
                        help="commit, branch or range (git rev-list syntax); default HEAD")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), metavar="DIR")
    parser.add_argument("--identity", default=IDENTITY, metavar="'NAME <EMAIL>'")
    parser.add_argument("--patterns", type=Path, metavar="FILE",
                        help="extra regular expressions (one per line) that no message may match")
    parser.add_argument("--fsck", action="store_true",
                        help="also fail when the repository holds dangling objects")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.ref.startswith("-"):
        parser.error("REF must not start with '-'")
    try:
        extra = load_patterns(args.patterns)
        if not (args.repo / ".git").exists() and not (args.repo / "HEAD").exists():
            raise GitFailed(f"{args.repo} is not a git repository")
        commits = read_commits(args.repo, args.ref)
        problems = check_commits(commits, args.identity, extra)
        dangling = dangling_objects(args.repo) if args.fsck else []
    except (GitFailed, OSError, ValueError) as exc:
        print(f"check_release_history: {exc}", file=sys.stderr)
        return 2
    report = {"ok": not problems and not dangling, "ref": args.ref, "commits": len(commits),
              "problems": problems, "dangling": dangling,
              "prune": list(PRUNE_COMMANDS) if dangling else []}
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for item in problems:
            print(f"{item['commit'][:12]}  {item['code']}: {item['message']}")
        for line in dangling:
            print(f"repository    dangling: {line}")
        if dangling:
            print("prune before publishing (the repository owner, not a shared worktree): "
                  + " && ".join(PRUNE_COMMANDS))
        if report["ok"]:
            print(f"release history check: ok ({len(commits)} commits on {args.ref}: UTC offsets, "
                  f"identity {args.identity!r}, no trailers, no personal data"
                  + (", no dangling objects" if args.fsck else "") + ")")
        else:
            print(f"release history check: {len(problems)} problem(s) in {len(commits)} commits"
                  + (f", {len(dangling)} dangling object(s)" if dangling else ""))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
