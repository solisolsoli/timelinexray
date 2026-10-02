"""The ``txray`` command-line interface.

Commands::

    txray setup [--commit SHA | --latest] [--no-index] | --print-mcp-config  pin + index in one step
    txray pin <commit> [--upstream URL]                                   (Milestone 1)
    txray manifest <commit> [--summary]                                   (Milestone 1)
    txray show <commit> <path> --lines A-B [--anchor TEXT] [--raw]        (Milestone 1)
    txray index <commit> [--rebuild] [--coverage]                         (Milestone 2)
    txray search <commit> <query> [--path GLOB] [--limit N] [--literal]   (Milestone 2)
    txray symbols <commit> [--path GLOB] [--kind K] [--name N] [--calls]  (Milestone 2)
    txray param <name> [--commit C] | param-history <name> [--base C] [--head C]
    txray findings add|import|list|show|verify|reanchor|review|...         (Milestone 3)
    txray mcp serve [--store DIR] [--ledger DIR] | tools [--json]  read-only MCP (Milestone 4)
    txray diff <old> <new> | digest <old> <new> | update --out DIR          (Milestone 5b)
    txray metrics import|rwe|reach ...   offline, network disabled        (Milestone 5a)
    txray export context-layer --out DIR   optional: findings as vault notes  (0.7.0)

Every command accepts ``--store DIR`` (default ``$TXRAY_STORE`` or
``~/.cache/timelinexray``) and ``--json`` for a machine-readable envelope. With ``--json``
on the command line every outcome, including a usage error and an unexpected exception, is
one JSON object of the published shape ``schemas/cli-envelope.schema.json``: ``outcome``
``ok`` (or ``attention`` / ``failed`` for commands that report a result and exit 1) with
``data`` and ``warnings``, or ``outcome: "error"`` with ``error: {code, message}``,
``data: null`` and ``warnings: []``. The one documented exception is ``txray mcp tools
--json``, which prints the MCP ``tools/list`` definitions themselves.

Exit codes: 0 success; 1 operation failed (not found, refused, integrity, range past end of
file, git failure, a file-system error, an unexpected exception); 2 invalid arguments;
3 network access refused by the allowlist; 130 interrupted. A reader that closes the pipe
(``txray manifest ... | head -1``) ends the command silently with exit 0.

Human-readable output shows untrusted upstream text (paths, source lines, names) with
terminal control characters escaped (:mod:`timelinexray.textsafe`); ``--json`` escapes
everything and ``show --raw`` writes the exact bytes. File-system arguments (``--store``,
``--ledger``, ``--out``, input files) are refused when empty, when they contain a NUL or a
control character, or when they are longer than :data:`MAX_FS_PATH` characters.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
import traceback
from collections.abc import Sequence
from typing import Any

from . import __version__
from .errors import InvalidInput, TxrayError
from .netguard import DEFAULT_UPSTREAM_URL, ENV_ALLOW_FILE_URLS, Allowlist
from .snapshot.manifest import Manifest
from .snapshot.store import ENV_STORE, PinRecord, SnapshotStore
from .span import FOUND, FOUND_MULTIPLE, SpanRead, parse_line_range
from .textsafe import visible, visible_bytes

JSON_SCHEMA_VERSION = 1
MAX_FS_PATH = 4096
_DIGIT = re.compile(rb"[0-9]")


def default_note(commit: str) -> str:
    """The footer every output showing numbers from upstream code carries (boundary 5)."""
    return (f"numbers in the output above are public defaults at commit {commit}, not "
            "production values")


def has_digit(*texts: str | bytes | None) -> bool:
    """Whether any of the shown upstream texts contains a digit (so a default may appear)."""
    for text in texts:
        if text is None:
            continue
        data = text if isinstance(text, bytes) else text.encode("utf-8", "surrogateescape")
        if _DIGIT.search(data):
            return True
    return False


def _display(path: str) -> str:
    """A path or other untrusted text for a terminal: undecodable bytes and controls escaped."""
    return visible(path.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace"))


def fs_path(text: str) -> str:
    """argparse type of every file-system argument (see the module documentation)."""
    if not text:
        raise argparse.ArgumentTypeError("must not be empty")
    if len(text) > MAX_FS_PATH:
        raise argparse.ArgumentTypeError(f"is longer than {MAX_FS_PATH} characters")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in text):
        raise argparse.ArgumentTypeError("must not contain NUL or control characters")
    return text


def _write(text: str) -> None:
    sys.stdout.write(text)


def _write_bytes(data: bytes) -> None:
    sys.stdout.flush()
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:  # pragma: no cover - only for unusual stdout replacements
        sys.stdout.write(data.decode("utf-8", "surrogateescape"))
        return
    buffer.write(data)
    buffer.flush()


def _emit_json(command: str | None, payload: dict[str, Any]) -> None:
    envelope = {
        "schema_version": JSON_SCHEMA_VERSION,
        "tool": "timelinexray",
        "version": __version__,
        "command": command,
        **payload,
    }
    _write(json.dumps(envelope, indent=2, sort_keys=True, ensure_ascii=True) + "\n")


def _emit_json_error(command: str | None, code: str, message: str) -> None:
    """The one error envelope (``schemas/cli-envelope.schema.json``): ``data`` is null and
    ``warnings`` empty, so a consumer reads the same members on every outcome."""
    _emit_json(command, {"outcome": "error", "error": {"code": code, "message": message},
                         "data": None, "warnings": []})


#: Set by :func:`main` while parsing: ``--json`` was on the command line, so a usage error
#: is reported as the JSON error envelope (code ``usage``) instead of argparse's text.
_json_requested = False
#: The ``prog`` of the deepest (sub)parser that started parsing: the command a usage error
#: belongs to, even when the top-level parser reports it (unrecognised arguments).
_current_prog = "txray"


class _Parser(argparse.ArgumentParser):
    """``argparse.ArgumentParser`` whose usage errors honour ``--json`` (subparsers inherit
    the class, so every ``txray ... --json`` failure is one envelope)."""

    def parse_known_args(self, args: Any = None, namespace: Any = None) -> Any:
        global _current_prog
        _current_prog = self.prog
        return super().parse_known_args(args, namespace)

    def error(self, message: str) -> Any:  # noqa: D401 - argparse's contract
        if _json_requested:
            prog = _current_prog.removeprefix("txray").strip()
            _emit_json_error(prog or None, "usage", message)
            self.exit(2)
        super().error(message)


def _store(args: argparse.Namespace) -> SnapshotStore:
    return SnapshotStore(args.store)


# -- pin ------------------------------------------------------------------------------


def _cmd_pin(args: argparse.Namespace) -> int:
    allowlist = Allowlist.from_env()
    result = _store(args).pin(args.commit, args.upstream, allowlist=allowlist)
    pin, counts = result.pin, result.manifest.counts()
    if args.json:
        _emit_json(
            "pin",
            {
                "outcome": "ok",
                "data": {
                    "pin": pin.to_dict(),
                    "fetched": result.fetched,
                    "created": result.created,
                    "counts": counts,
                },
                "warnings": [],
            },
        )
        return 0
    by_class = counts["classification"]
    _write(
        f"pinned     {pin.commit}\n"
        f"upstream   {pin.upstream_url}\n"
        f"tree       {pin.tree}\n"
        f"committed  {pin.committer_time}\n"
        f"fetched    {'yes' if result.fetched else 'no (commit already in mirror)'}\n"
        f"manifest   sha256:{pin.manifest_sha256}  ({pin.manifest})\n"
        f"paths      {counts['total']} total: {by_class['parsed-candidate']} parsed-candidate, "
        f"{by_class['text']} text, {by_class['excluded']} excluded\n"
    )
    return 0


# -- manifest -------------------------------------------------------------------------


def _summary_data(pin: PinRecord, manifest: Manifest) -> dict[str, Any]:
    return {
        "commit": pin.commit,
        "tree": manifest.tree,
        "committer_time": manifest.committer_time,
        "upstream_url": pin.upstream_url,
        "manifest_sha256": pin.manifest_sha256,
        "classifier": manifest.classifier,
        "counts": manifest.counts(),
        "licenses": [item.to_dict() for item in manifest.licenses],
    }


def _cmd_manifest(args: argparse.Namespace) -> int:
    pin, manifest = _store(args).load_manifest(args.commit)
    if args.json:
        if args.summary:
            data = _summary_data(pin, manifest)
        else:
            data = {
                "commit": pin.commit,
                "manifest_sha256": pin.manifest_sha256,
                "manifest": manifest.to_dict(),
            }
        _emit_json("manifest", {"outcome": "ok", "data": data, "warnings": []})
        return 0
    if args.summary:
        _write(_format_summary(pin, manifest))
        return 0
    lines = ["classification\treason\tlanguage\tsize\tpath"]
    for entry in manifest.entries:
        lines.append(
            "\t".join(
                (
                    entry.classification,
                    entry.reason or "-",
                    entry.language or "-",
                    "-" if entry.size is None else str(entry.size),
                    _display(entry.path),
                )
            )
        )
    _write("\n".join(lines) + "\n")
    return 0


def _format_summary(pin: PinRecord, manifest: Manifest) -> str:
    counts = manifest.counts()
    out = [
        f"commit     {pin.commit}",
        f"tree       {manifest.tree}",
        f"committed  {manifest.committer_time}",
        f"upstream   {pin.upstream_url}",
        f"manifest   sha256:{pin.manifest_sha256}",
        f"paths      {counts['total']}",
    ]
    for name, value in counts["classification"].items():
        out.append(f"  {name:<18}{value:>6}")
    out.append("excluded by reason")
    for name, value in counts["excluded_reason"].items():
        out.append(f"  {name:<18}{value:>6}")
    out.append("languages (guessed from file names)")
    for name, value in sorted(counts["language"].items(), key=lambda item: (-item[1], item[0])):
        out.append(f"  {name:<18}{value:>6}")
    out.append("license files")
    if not manifest.licenses:
        out.append("  (none found)")
    for item in manifest.licenses:
        hints = ", ".join(item.hints) if item.hints else "no keyword hints"
        out.append(f"  {_display(item.path)}  [{item.kind}]  {hints}")
    return "\n".join(out) + "\n"


# -- show -----------------------------------------------------------------------------


def _anchor_line(span: SpanRead) -> str | None:
    check = span.anchor
    if check is None:
        return None
    if check.verdict == FOUND:
        return f"FOUND at line {check.lines[0]} (byte {check.byte_offsets[0]})"
    if check.verdict == FOUND_MULTIPLE:
        where = ", ".join(str(line) for line in check.lines[:20])
        more = " ..." if check.count > 20 else ""
        return (f"FOUND_MULTIPLE: {check.count} occurrences, all inside the span, at lines "
                f"{where}{more}")
    return f"MISSING: not found within lines {span.start_line}-{span.end_line}"


def _cmd_show(args: argparse.Namespace) -> int:
    if args.raw and args.json:
        raise InvalidInput("choose one of --raw and --json")
    start, end = parse_line_range(args.lines)
    store = _store(args)
    _, entry = store.lookup(args.commit, args.path)
    span = store.read_span(args.commit, args.path, start, end, anchor=args.anchor)
    warnings = []
    if entry.reason:
        warnings.append(f"path is excluded from indexing ({entry.reason}: {entry.rule})")
    if args.raw:
        _write_bytes(span.data)
        return 0
    note = default_note(span.commit) if has_digit(span.data) else None
    if args.json:
        data = span.to_dict()
        data.update(
            classification=entry.classification, reason=entry.reason, language=entry.language
        )
        _emit_json("show", {"outcome": "ok", "data": data, "warnings": warnings, "note": note})
        return 0
    kind = entry.classification + (f" ({entry.language})" if entry.language else "")
    if entry.reason:
        kind += f", reason {entry.reason}"
    header = [
        f"commit     {span.commit}",
        f"path       {_display(span.path)}",
        f"lines      {span.start_line}-{span.end_line} of {span.line_count}"
        f"  (bytes {span.start_byte}-{span.end_byte}, {len(span.data)} bytes)",
        f"blob       {span.blob_oid}  sha256 {span.blob_sha256}",
        f"span       sha256 {span.sha256}",
        f"endings    lf={span.lf_lines} crlf={span.crlf_lines}"
        f" unterminated={span.unterminated_lines}",
        f"class      {kind}",
    ]
    anchor = _anchor_line(span)
    if anchor:
        header.append(f"anchor     {anchor}")
    if note:
        header.append(f"note       {note}")
    shown, escaped = visible_bytes(span.data)
    if escaped:
        warnings.append(f"{escaped} terminal control or direction-override sequence(s) are "
                        "shown escaped below; --raw writes the exact bytes")
    header.extend(f"warning    {text}" for text in warnings)
    _write("\n".join(header) + "\n----\n")
    _write_bytes(shown)
    if span.unterminated_lines:
        _write("\n\\ No newline at end of file\n")
    return 0


# -- parser ---------------------------------------------------------------------------


#: The module that registers each command it owns, in registration order (the order of the
#: commands in ``txray --help``); the commands ``pin``, ``manifest`` and ``show`` live here.
_COMMAND_MODULES: dict[str, tuple[str, ...]] = {
    "index_cli": ("index", "search", "symbols"),
    "params_cli": ("param", "param-history"),
    "findings_cli": ("findings",),
    "mcp_cli": ("mcp",),
    "digest_cli": ("diff", "digest", "update"),
    "metrics_cli": ("metrics",),
    "export_cli": ("export",),
    "setup_cli": ("setup",),
}


def _modules_for(argv: Sequence[str] | None) -> tuple[str, ...]:
    """The command modules to import and register for ``argv``.

    With no ``argv``, or when its first word is not the name of a registered command (no
    command, an option such as ``--help``, an unknown or misspelled command), every module:
    the top-level help, the list of commands in a usage error and every message stay what
    the full parser prints. When the first word names a
    command, only the module owning it: top-level options exist only before the command
    word, and everything after it is parsed by that command's own parser, whose help,
    usage and errors do not depend on its siblings. A leading ``--version`` prints and
    exits before any command is looked at, so it needs none.
    """
    if argv:
        for module, names in _COMMAND_MODULES.items():
            if argv[0] in names:
                return (module,)
        if argv[0] in ("pin", "manifest", "show", "--version"):
            return ()  # ``--version`` is the first action argparse runs, then it exits
    return tuple(_COMMAND_MODULES)


def build_parser(argv: Sequence[str] | None = None) -> argparse.ArgumentParser:
    """The parser; with ``argv``, only the command module that ``argv`` names is imported
    (see :func:`_modules_for`), the others are left alone."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--store",
        metavar="DIR",
        type=fs_path,
        default=None,
        help=f"snapshot store directory (default: ${ENV_STORE} or ~/.cache/timelinexray)",
    )
    common.add_argument("--json", action="store_true", help="print a JSON envelope")

    parser = _Parser(
        prog="txray",
        description=(
            "Evidence-first research tool for the public xai-org/x-algorithm repository. "
            "Independent community analysis of publicly available source code. "
            "Not affiliated with or endorsed by X or xAI."
        ),
        epilog="exit codes: 0 ok, 1 failed, 2 invalid arguments, 3 network refused, 130 interrupted",
    )
    parser.add_argument("--version", action="version", version=f"txray {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    pin = commands.add_parser(
        "pin",
        parents=[common],
        help="fetch a commit if needed, protect it in the mirror and record its manifest",
        description=(
            "Only allowlisted repositories are contacted: "
            f"{DEFAULT_UPSTREAM_URL} plus file:// URLs listed in ${ENV_ALLOW_FILE_URLS}."
        ),
    )
    pin.add_argument("commit", help="commit id, 7 to 40 hexadecimal digits")
    pin.add_argument(
        "--upstream", default=DEFAULT_UPSTREAM_URL, metavar="URL", help="allowlisted repository URL"
    )
    pin.set_defaults(handler=_cmd_pin)

    manifest = commands.add_parser(
        "manifest",
        parents=[common],
        help="list every path of a pinned commit with its class",
        description=(
            "List every path of the commit's manifest with its classification, language and "
            "exclusion reason, or with --summary only the counts and the license files."
        ),
    )
    manifest.add_argument("commit", help="pinned commit id or unique prefix (7+ digits)")
    manifest.add_argument(
        "--summary", action="store_true", help="print counts and license files only"
    )
    manifest.set_defaults(handler=_cmd_manifest)

    show = commands.add_parser(
        "show",
        parents=[common],
        help="print an exact line span of a file at a pinned commit, with hashes",
        description=(
            "Print the exact bytes of a 1-based inclusive line span of a file at a pinned "
            "commit, with the blob and span SHA-256, the line endings and, with --anchor, "
            "whether the text occurs inside the span."
        ),
    )
    show.add_argument("commit", help="pinned commit id or unique prefix (7+ digits)")
    show.add_argument("path", help="exact repository-relative path")
    show.add_argument("--lines", required=True, metavar="A-B", help="1-based inclusive line range")
    show.add_argument(
        "--anchor",
        metavar="TEXT",
        help="exact text to find within the span (write --anchor=TEXT if TEXT starts with '-')",
    )
    show.add_argument("--raw", action="store_true", help="write only the exact span bytes")
    show.set_defaults(handler=_cmd_show)
    for module in _modules_for(argv):
        importlib.import_module(f"{__package__}.{module}").register(commands, common)
    return parser


def _backslashreplace_unencodable() -> None:
    """A character the terminal's encoding cannot show (a C or ASCII locale, a legacy code
    page) is printed as a backslash escape instead of crashing a human-readable command.
    Only the text streams are changed: ``--raw`` writes bytes to the buffer and is untouched,
    and ``--json`` is ASCII already."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, OSError):  # a stream that cannot change (closed, replaced)
                pass


def main(argv: Sequence[str] | None = None) -> int:
    global _json_requested, _current_prog
    arguments = list(sys.argv[1:] if argv is None else argv)
    _backslashreplace_unencodable()
    _json_requested, _current_prog = "--json" in arguments, "txray"
    try:
        try:
            args = build_parser(arguments).parse_args(arguments)
        finally:
            _json_requested = False
        code = _dispatch(args)
        flush = getattr(sys.stdout, "flush", None)
        if flush is not None:  # a small output reaches the pipe only here: fail inside the try
            flush()
        return code
    except BrokenPipeError:
        return _silence_broken_pipe()


def _dispatch(args: argparse.Namespace) -> int:
    try:
        return args.handler(args)
    except TxrayError as exc:
        return _fail(args, exc.code, str(exc), exc.exit_code)
    except BrokenPipeError:
        raise
    except OSError as exc:  # a file-system problem with a path the user gave
        where = f": {_display(str(exc.filename))}" if exc.filename is not None else ""
        return _fail(args, "os_error", f"{exc.strerror or type(exc).__name__}{where}", 1)
    except KeyboardInterrupt:
        return _fail(args, "interrupted", "interrupted", 130)
    except Exception as exc:  # the envelope promise holds; the traceback stays on stderr
        sys.stderr.write(traceback.format_exc())
        return _fail(args, "internal_error", f"{type(exc).__name__}: {exc}", 1)


def _fail(args: argparse.Namespace, code: str, message: str, exit_code: int) -> int:
    if getattr(args, "json", False):
        _emit_json_error(args.command, code, message)
    else:
        sys.stderr.write(f"txray: error: {visible(message, keep=chr(10))}\n")
    return exit_code


def _silence_broken_pipe() -> int:
    """The reader went away (``txray ... | head -1``): point stdout at the null device so
    the interpreter's final flush is quiet, print nothing, and exit 0 (FA-011)."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, sys.stdout.fileno())
        finally:
            os.close(devnull)
    except (OSError, ValueError, AttributeError):  # stdout is not a real file descriptor
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
