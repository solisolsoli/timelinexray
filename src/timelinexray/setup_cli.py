"""``txray setup``: pin and index one commit, then print the next steps.

::

    txray setup [--commit SHA | --latest] [--store DIR] [--upstream URL] [--no-index] [--json]
    txray setup --print-mcp-config [claude-code|json] [--store DIR] [--json]

Setup only composes existing, guarded operations: :meth:`SnapshotStore.pin` (which fetches
through :func:`timelinexray.netguard.fetch` only when the commit is not already in the
mirror), :meth:`CodeIndex.build` (which reads nothing when the generation is current) and,
for ``--latest``, the same guarded fetch of ``txray update`` followed by reading the head of
the mirrored ``main`` branch. It writes only inside the snapshot store. ``--print-mcp-config``
prints a client configuration and never edits one; it neither touches the store nor the
network. Registered from :func:`timelinexray.cli.build_parser` via :func:`register`.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import cli as _cli
from .diff.history import upstream_refs
from .errors import InvalidInput, NotFound
from .index import CodeIndex
from .netguard import DEFAULT_UPSTREAM_URL, ENV_ALLOW_FILE_URLS, Allowlist, fetch
from .snapshot.store import ENV_STORE, SnapshotStore, default_store_root

#: The upstream commit this release is tested with.
TESTED_COMMIT = "77d431aabf409ca1c1eed9bec7e2183f7c914e23"
#: The branch ``--latest`` resolves (the one ``txray update`` follows by default).
LATEST_BRANCH = "main"
MCP_SERVER_NAME = "timelinexray"
MCP_FORMATS = ("claude-code", "json")


def _store_root(args: argparse.Namespace) -> Path:
    root = Path(args.store).expanduser() if args.store else default_store_root()
    return Path(os.path.abspath(root))


def _executable() -> list[str]:
    """The command that starts txray: the entry point running now (it works even when its
    directory is not on PATH yet, right after install.sh), else the one on PATH, else this
    interpreter with ``-m timelinexray``. Symbolic links are kept, not resolved."""
    running = sys.argv[0] if sys.argv else ""
    if os.path.basename(running) == "txray" and os.path.isfile(running):
        return [os.path.abspath(running)]
    found = shutil.which("txray")
    if found:
        return [os.path.abspath(found)]
    return [os.path.abspath(sys.executable), "-m", "timelinexray"]


def _mcp_argv(store: Path) -> list[str]:
    return [*_executable(), "mcp", "serve", "--store", str(store)]


def claude_code_line(store: Path) -> str:
    argv = ["claude", "mcp", "add", "--transport", "stdio", MCP_SERVER_NAME, "--", *_mcp_argv(store)]
    return " ".join(shlex.quote(item) for item in argv)


def mcp_json(store: Path) -> dict[str, Any]:
    argv = _mcp_argv(store)
    return {"mcpServers": {MCP_SERVER_NAME: {"type": "stdio", "command": argv[0],
                                             "args": argv[1:]}}}


def _print_mcp_config(args: argparse.Namespace) -> int:
    for flag, value in (("--commit", args.commit), ("--latest", args.latest or None),
                        ("--upstream", args.upstream), ("--no-index", args.no_index or None)):
        if value is not None:
            raise InvalidInput(f"{flag} cannot be combined with --print-mcp-config "
                               "(it prints a client configuration and does nothing else)")
    store = _store_root(args)
    form = args.print_mcp_config
    if form == "claude-code":
        text, data = claude_code_line(store) + "\n", {"format": form, "command": claude_code_line(store)}
    else:
        config = mcp_json(store)
        text, data = json.dumps(config, indent=2, sort_keys=True) + "\n", {"format": form,
                                                                         "config": config}
    data["store"] = str(store)
    if args.json:
        _cli._emit_json("setup", {"outcome": "ok", "data": data, "warnings": []})
    else:
        _cli._write(text)
    return 0


def _resolve_latest(store: SnapshotStore, url: str, allowlist: Allowlist) -> str:
    """Fetch through the guarded path (as ``txray update`` does) and read the branch head."""
    repo = store.open_mirror(url)
    fetch(url, repo.git_dir, allowlist)
    head = upstream_refs(repo).get(f"refs/heads/{LATEST_BRANCH}")
    if head is None:
        raise NotFound(f"the upstream has no branch {LATEST_BRANCH!r} after the fetch")
    return head


def _next_steps(commit: str, store_arg: str | None, store: Path) -> list[str]:
    suffix = f" --store {shlex.quote(str(store))}" if store_arg else ""
    short = commit[:7]
    return [
        f"txray search {short} ClickWeight{suffix}",
        f"txray param ClickWeight --commit {short}{suffix}",
        f"txray show {short} README.md --lines 1-20{suffix}",
    ]


def _cmd_setup(args: argparse.Namespace) -> int:
    if args.print_mcp_config is not None:
        return _print_mcp_config(args)
    started = time.monotonic()
    upstream = args.upstream if args.upstream is not None else DEFAULT_UPSTREAM_URL
    allowlist = Allowlist.from_env()
    # the canonical form (a file:// URL through a symbolic link is its resolved URL, so every
    # command names one mirror); NetworkRefused (exit 3) before anything is created
    upstream = allowlist.check(upstream)
    root = _store_root(args)
    store = SnapshotStore(root)

    if args.latest:
        wanted = _resolve_latest(store, upstream, allowlist)
    else:
        wanted = args.commit if args.commit is not None else TESTED_COMMIT
    pinned = store.pin(wanted, upstream, allowlist=allowlist)
    commit = pinned.pin.commit
    fetched = pinned.fetched or args.latest  # --latest fetched while resolving the head

    report = None
    if not args.no_index:
        report = CodeIndex(store).build(commit)
    elapsed = time.monotonic() - started

    steps = _next_steps(commit, args.store, root)
    mcp_line = claude_code_line(root)
    data: dict[str, Any] = {
        "commit": commit,
        "store": str(root),
        "upstream": pinned.pin.upstream_url,
        "resolved": "latest" if args.latest else "commit",
        "pin": {"created": pinned.created, "fetched": fetched,
                "already_pinned": not pinned.created},
        "index": None if report is None else {
            "skipped": False,
            "already_indexed": report.up_to_date,
            "files": report.files,
            "files_indexed": report.lexical.get("indexed", 0),
            "symbols": report.symbols,
        },
        "elapsed_seconds": round(elapsed, 3),
        "next_steps": steps,
        "connect_agent": {"claude_code": mcp_line,
                          "print": "txray setup --print-mcp-config [claude-code|json]"
                          + (f" --store {shlex.quote(str(root))}" if args.store else "")},
    }
    if report is None:
        data["index"] = {"skipped": True}
    if args.json:
        _cli._emit_json("setup", {"outcome": "ok", "data": data, "warnings": []})
        return 0

    if fetched and pinned.created:
        pin_state = "pinned (fetched from the upstream)"
    elif fetched:
        pin_state = "already pinned (upstream fetched to resolve --latest)"
    elif pinned.created:
        pin_state = "pinned (commit was already in the local mirror; nothing fetched)"
    else:
        pin_state = "already pinned (nothing fetched)"
    lines = [
        f"commit     {commit}",
        f"store      {root}",
        f"pin        {pin_state}",
    ]
    if report is None:
        lines.append("index      skipped (--no-index); run: txray index " + commit[:7])
    else:
        state = "already indexed" if report.up_to_date else "indexed"
        lines.append(f"index      {state}: {report.files} paths, "
                     f"{report.lexical.get('indexed', 0)} files with indexed lines, "
                     f"{report.symbols} symbols")
    lines.append(f"elapsed    {elapsed:.2f} s")
    lines.append("")
    lines.append("try next")
    lines.extend(f"  {step}" for step in steps)
    lines.append("")
    lines.append("connect an agent (prints only; no client configuration is edited)")
    lines.append(f"  {mcp_line}")
    lines.append("  txray setup --print-mcp-config json   (an mcpServers snippet)")
    lines.append("")
    lines.append("Numbers in the code are public defaults at the pinned commit, not "
                 "production values.")
    _cli._write("\n".join(lines) + "\n")
    return 0


def register(commands: Any, common: argparse.ArgumentParser) -> None:
    """Add ``setup`` to the ``txray`` subcommands."""
    setup = commands.add_parser(
        "setup",
        parents=[common],
        help="pin and index a commit, then print next steps and the agent connection line",
        description=(
            "One command after installing: fetch the tested upstream commit if it is not "
            "local (only allowlisted repositories are contacted: "
            f"{DEFAULT_UPSTREAM_URL} plus file:// URLs listed in ${ENV_ALLOW_FILE_URLS}), "
            "pin it, build its code index, and print three example commands. A second run "
            "fetches and re-indexes nothing. Writes only inside the snapshot store "
            f"(--store, ${ENV_STORE} or ~/.cache/timelinexray). --print-mcp-config prints "
            "an MCP client configuration for this store and changes nothing."
        ),
    )
    group = setup.add_mutually_exclusive_group()
    group.add_argument("--commit", metavar="SHA", default=None,
                       help=f"commit to pin, 7 to 40 hex digits (default: {TESTED_COMMIT[:7]}, "
                            "the commit this release is tested with)")
    group.add_argument("--latest", action="store_true",
                       help=f"fetch through the guarded path and pin the head of "
                            f"'{LATEST_BRANCH}' (always contacts the upstream)")
    setup.add_argument("--upstream", default=None, metavar="URL",
                       help="allowlisted repository URL (default: the public upstream)")
    setup.add_argument("--no-index", action="store_true",
                       help="pin only; build the index later with txray index")
    setup.add_argument("--print-mcp-config", nargs="?", const="claude-code", default=None,
                       choices=MCP_FORMATS, metavar="FORMAT",
                       help="print the MCP client configuration for this store "
                            "(claude-code: a 'claude mcp add' line; json: an mcpServers "
                            "snippet) and exit; edits nothing")
    setup.set_defaults(handler=_cmd_setup)
