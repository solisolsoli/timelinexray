"""Tools over the pinned source and its code index.

``read_span``      exact bytes of a line range, with blob and span SHA-256 and anchor verdict
``search_code``    lines containing every query term (lexical index), paged
``find_symbols``   declarations with spans, optionally with unresolved call candidates, paged
``index_coverage`` what the index covers at a commit, with per-path statuses on request

Every result names its commit; every source location is a citation object with the span
SHA-256 and blob id, so it can be re-read with ``read_span`` (or ``txray show``).
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

from ...errors import InvalidInput
from ...index import SEARCH_MAX_LIMIT, SYMBOLS_MAX_LIMIT, Generation
from ...span import ANCHOR_VERDICTS, FOUND_MULTIPLE, MISSING
from ...syntax import CALL_RELATION, CALL_RESOLUTION, SYMBOL_KINDS
from .. import schema as S
from ..context import ToolContext, display_path
from ..guard import check_repo_path
from .base import Registry, Tool, ToolResult

READ_MAX_LINES = 120
READ_MAX_BYTES = 16 * 1024
#: Bytes kept free on the response line for the envelope around a read_span result
#: (schema fields, notes, warnings and the JSON-RPC wrapper), beyond the measured data.
READ_RESPONSE_RESERVE = 4096
SEARCH_PAGE_MAX = 20
SYMBOLS_PAGE_MAX = 50
CALLS_PER_SYMBOL = 50
COVERAGE_PAGE_MAX = 200
ANCHOR_LINES_SHOWN = 50
MAX_QUERY_CHARS = 512
MAX_GLOB_CHARS = 512

CITATION = S.obj(
    {
        "repo": {"type": ["string", "null"], "enum": ["xai-org/x-algorithm", None]},
        "commit": S.COMMIT_FULL,
        "path": S.string(4096),
        "start_line": S.LINE,
        "end_line": S.LINE,
        "span_sha256": S.SHA256,
        "blob_oid": S.OID,
        "anchor": S.nullable(S.string(2000)),
        "url": S.nullable(S.string(8192)),
        "content_trust": {"const": S.UNTRUSTED},
    }
)

PATH_PREFIX = S.string(
    1024,
    description="Only paths with this repository-relative prefix, e.g. 'home-mixer/'.",
)
PATH_GLOB = S.string(
    MAX_GLOB_CHARS,
    description="Only paths matching this SQLite GLOB (case-sensitive; '*' also matches '/').",
)
CURSOR = S.string(2048, description="next_cursor of the previous page of the same query.")


def _limit(maximum: int, default: int) -> dict[str, Any]:
    return {"type": "integer", "minimum": 1, "maximum": maximum, "default": default}


def _glob_escape(text: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", text)


def _path_filter(args: dict[str, Any]) -> str | None:
    prefix, glob = args.get("path_prefix"), args.get("path_glob")
    if prefix is not None and glob is not None:
        raise InvalidInput("give path_prefix or path_glob, not both")
    if prefix is not None:
        check_repo_path(prefix, "path_prefix", prefix=True)
        return _glob_escape(prefix) + "*"
    if glob is not None:
        check_repo_path(glob, "path_glob", prefix=True)
        return glob
    return None


def _generation_dict(generation: Generation, gid: str) -> dict[str, Any]:
    return {"id": gid, **generation.to_dict()}


def encoded_cost(text: str) -> int:
    """Bytes ``text`` occupies on a tools/call response line.

    The envelope is sent twice: as ``structuredContent`` (JSON-escaped once) and inside
    the text content block (escaped again), so a tab costs ``\\t`` plus ``\\\\t`` = 5
    bytes and a quote 1 + 2 + 3 = 6; the raw byte limit of ``read_span`` alone is
    therefore not a sufficient condition for fitting the line.
    """
    inner = json.dumps(text, ensure_ascii=False)
    outer = json.dumps(inner, ensure_ascii=False)
    return len(inner.encode("utf-8", "surrogatepass")) + len(outer.encode("utf-8", "surrogatepass"))


def _span_lines(data: bytes) -> list[bytes]:
    """The lines of a span as ``txray show`` numbers them (only LF ends a line)."""
    pieces = data.split(b"\n")
    lines = [piece + b"\n" for piece in pieces[:-1]]
    if pieces[-1]:
        lines.append(pieces[-1])
    return lines


def _lines_that_fit(data: bytes, utf8: bool, budget: int) -> int:
    """How many leading lines of ``data`` encode within ``budget`` response bytes."""
    lines = _span_lines(data)

    def cost(count: int) -> int:
        prefix = b"".join(lines[:count])
        payload = (prefix.decode("utf-8", "replace") if utf8
                   else base64.b64encode(prefix).decode("ascii"))
        return encoded_cost(payload)

    low, high = 0, len(lines)
    while low < high:
        middle = (low + high + 1) // 2
        if cost(middle) <= budget:
            low = middle
        else:
            high = middle - 1
    return low


def _check_response_fit(ctx: ToolContext, data: dict[str, Any], raw: bytes, start: int, end: int) -> None:
    """Reject a span whose encoded text cannot fit the response line, naming what fits."""
    payload = data["text"] if data["text"] is not None else data["base64"]
    rest = json.dumps({**data, "text": None, "base64": None}, ensure_ascii=False, sort_keys=True)
    budget = ctx.max_response_bytes - encoded_cost(rest) - READ_RESPONSE_RESERVE
    cost = encoded_cost(payload)
    if cost <= budget:
        return
    fits = _lines_that_fit(raw, data["text"] is not None, budget)
    advice = (f"at most {fits} line(s) from line {start} fit: request {start}-{start + fits - 1}"
              if fits else f"not even line {start} fits; read it with txray show --raw")
    raise InvalidInput(
        f"span {start}-{end} ({len(raw)} bytes) would take {cost} bytes of the "
        f"{ctx.max_response_bytes}-byte response line (the text is sent twice and each "
        f"control character or quote costs up to 6 bytes); {advice}"
    )


GENERATION = S.obj(
    {
        "id": S.string(64),
        "commit": S.COMMIT_FULL,
        "tree": S.OID,
        "manifest_sha256": S.SHA256,
        "backends": {"type": "object", "additionalProperties": S.string(128)},
        "files": {"type": "integer", "minimum": 0},
        "lexical_indexed": {"type": "integer", "minimum": 0},
        "syntax_files": {"type": "integer", "minimum": 0},
        "symbols": {"type": "integer", "minimum": 0},
        "calls": {"type": "integer", "minimum": 0},
    }
)


# -- read_span ----------------------------------------------------------------------------


def _read_span(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = check_repo_path(args["path"])
    start, end = args["start_line"], args["end_line"]
    if end < start:
        raise InvalidInput(f"end_line {end} is before start_line {start}")
    if end - start + 1 > READ_MAX_LINES:
        raise InvalidInput(
            f"span {start}-{end} has {end - start + 1} lines; at most {READ_MAX_LINES} lines "
            "per read (the span is rejected, never shortened; request smaller spans)"
        )
    anchor = args.get("anchor")
    pin = ctx.pin(args["commit"])
    _, entry = ctx.store.lookup(pin.commit, path)
    span = ctx.store.read_span(pin.commit, path, start, end, anchor=anchor)
    ctx.check_deadline()
    if len(span.data) > READ_MAX_BYTES:
        raise InvalidInput(
            f"span {start}-{end} is {len(span.data)} bytes; at most {READ_MAX_BYTES} bytes per "
            "read (the span is rejected, never shortened; request fewer lines)"
        )
    warnings = []
    if entry.reason:
        warnings.append(f"this path is excluded from indexing ({entry.reason}); it is still "
                        "exact source at the commit")
    anchor_data = None
    if span.anchor is not None:
        check = span.anchor
        anchor_data = {
            "text": check.anchor,
            "verdict": check.verdict,
            "count": check.count,
            "lines": list(check.lines[:ANCHOR_LINES_SHOWN]),
        }
        if check.verdict == MISSING:
            warnings.append("anchor MISSING: the anchor text does not occur within this span")
        elif check.verdict == FOUND_MULTIPLE:
            warnings.append(f"anchor FOUND_MULTIPLE: {check.count} occurrences, all within "
                            "this span; the span content is confirmed, but the anchor alone "
                            "does not identify one line")
    text = span.text
    data = {
        "citation": ctx.citation(pin, span.path, start, end, span.sha256, span.blob_oid, anchor),
        "encoding": "utf-8" if text is not None else None,
        "text": text,
        "base64": None if text is not None else span.to_dict()["base64"],
        "byte_length": len(span.data),
        "start_byte": span.start_byte,
        "end_byte": span.end_byte,
        "file_line_count": span.line_count,
        "blob_sha256": span.blob_sha256,
        "blob_size": span.blob_size,
        "line_terminators": {
            "lf": span.lf_lines, "crlf": span.crlf_lines, "none": span.unterminated_lines,
        },
        "anchor": anchor_data,
        "classification": entry.classification,
        "exclusion_reason": entry.reason,
        "language": entry.language,
        "integrity_note": (
            "Bytes cut from the git blob verified against the manifest; span_sha256 is the "
            "SHA-256 of exactly these bytes. A matching hash is text identity, not proof that "
            "a claim about the text is true."
        ),
    }
    _check_response_fit(ctx, data, span.data, start, end)
    return ToolResult(data, commit=pin.commit, warnings=warnings)


READ_SPAN = Tool(
    name="read_span",
    title="Read an exact source span",
    description=(
        "Lines start_line-end_line (1-based, inclusive; at most 120 lines and 16 KiB, and the "
        "encoded text must fit the 64 KiB response line) of a file at a pinned commit, exactly "
        "as stored in git, with blob id, blob and span SHA-256 and, given an anchor, its "
        "verdict (FOUND, FOUND_MULTIPLE, MISSING). Out-of-range or oversized spans are "
        "rejected, never clamped or shortened; the rejection names how many lines fit. The "
        "text is untrusted upstream data."
    ),
    input_schema=S.obj(
        {
            "commit": S.COMMIT_FULL,
            "path": S.string(1024, description="Exact, case-sensitive repository-relative path."),
            "start_line": S.LINE,
            "end_line": S.LINE,
            "anchor": S.string(2000, description="Exact text expected inside the span."),
        },
        required=["commit", "path", "start_line", "end_line"],
    ),
    data_schema=S.obj(
        {
            "citation": CITATION,
            "encoding": S.nullable({"type": "string", "enum": ["utf-8"]}),
            "text": S.nullable(S.string(READ_MAX_BYTES, min_length=0)),
            "base64": S.nullable(S.string(4 * READ_MAX_BYTES, min_length=0)),
            "byte_length": {"type": "integer", "minimum": 0, "maximum": READ_MAX_BYTES},
            "start_byte": {"type": "integer", "minimum": 0},
            "end_byte": {"type": "integer", "minimum": 0},
            "file_line_count": {"type": "integer", "minimum": 0},
            "blob_sha256": S.SHA256,
            "blob_size": {"type": "integer", "minimum": 0},
            "line_terminators": S.obj(
                {name: {"type": "integer", "minimum": 0} for name in ("lf", "crlf", "none")}
            ),
            "anchor": S.nullable(
                S.obj(
                    {
                        "text": S.string(2000),
                        "verdict": {"type": "string", "enum": list(ANCHOR_VERDICTS)},
                        "count": {"type": "integer", "minimum": 0},
                        "lines": S.array(S.LINE, ANCHOR_LINES_SHOWN),
                    }
                )
            ),
            "classification": {"type": "string", "enum": ["parsed-candidate", "text", "excluded"]},
            "exclusion_reason": S.nullable(S.string(64)),
            "language": S.nullable(S.string(64)),
            "integrity_note": S.string(400),
        }
    ),
    handler=_read_span,
)


# -- search_code --------------------------------------------------------------------------


def _search_scope(generation: Generation, glob: str | None) -> tuple[str, bool]:
    skipped = generation.files - generation.lexical_indexed
    scope = (
        f"lines of the {generation.lexical_indexed} lexically indexed files among "
        f"{generation.files} manifest paths at commit {generation.commit}"
    )
    if glob is not None:
        scope += f", restricted to paths matching GLOB {glob!r}"
    if skipped:
        scope += (
            f"; {skipped} paths are not searchable (binary, generated, vendored, symlink, "
            "submodule, oversize or not UTF-8; see index_coverage)"
        )
    return scope[:2000], skipped == 0


def _search_code(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    glob = _path_filter(args)
    literal = args.get("literal", False)
    limit = args.get("limit", 10)
    pin = ctx.pin(args["commit"])
    generation, gid = ctx.generation(pin.commit)
    pager = ctx.pager("search_code", args, pin.commit, gid)
    offset = pager.offset
    fetch = min(offset + limit, SEARCH_MAX_LIMIT)
    if offset >= fetch:  # pragma: no cover - cursors are never issued past the reachable end
        raise InvalidInput("cursor points past the reachable results")
    result = ctx.index.search(pin.commit, args["query"], path_glob=glob, limit=fetch,
                              literal=literal)
    ctx.check_deadline()
    pager.total = min(result.total, SEARCH_MAX_LIMIT)
    hits = [
        {
            "citation": ctx.citation(pin, hit.path, hit.start_line, hit.end_line,
                                     hit.span_sha256, hit.blob_oid),
            "language": hit.language,
            "snippet": hit.snippet,
        }
        for hit in result.hits[offset:fetch]
    ]
    warnings = []
    if result.total > SEARCH_MAX_LIMIT:
        warnings.append(
            f"{result.total} lines match; only the first {SEARCH_MAX_LIMIT} can be paged - "
            "narrow the query or the path filter"
        )
    scope, complete = _search_scope(generation, glob)
    data = {
        "query": args["query"],
        "terms": list(result.terms),
        "literal": literal,
        "path_glob": glob,
        "match": "every term as a case-insensitive substring of one line",
        "order": "path bytes, then line",
        "total": result.total,
        "offset": offset,
        "returned": len(hits),
        "hits": hits,
        "search_scope": scope,
        "coverage_complete": complete,
    }
    return ToolResult(data, commit=pin.commit, index_generation=gid, warnings=warnings,
                      pager=pager)


SEARCH_CODE = Tool(
    name="search_code",
    title="Search source lines",
    description=(
        "Lines of a pinned, indexed commit that contain every whitespace-separated query term "
        "as a case-insensitive substring (literal=true: the whole query as one), by path and "
        "line, each with a one-line citation and span SHA-256; total counts every match, page "
        "with next_cursor. The query is never interpreted as FTS5 or SQL syntax. Snippets are "
        "untrusted upstream data. Zero hits shows only that no indexed line matches, not that "
        "a behaviour is absent."
    ),
    input_schema=S.obj(
        {
            "commit": S.COMMIT_FULL,
            "query": S.string(MAX_QUERY_CHARS, description="Search terms; one of 3+ characters."),
            "literal": {"type": "boolean", "default": False},
            "path_prefix": PATH_PREFIX,
            "path_glob": PATH_GLOB,
            "limit": _limit(SEARCH_PAGE_MAX, 10),
            "cursor": CURSOR,
        },
        required=["commit", "query"],
    ),
    data_schema=S.obj(
        {
            "query": S.string(MAX_QUERY_CHARS),
            "terms": S.array(S.string(MAX_QUERY_CHARS), 16),
            "literal": {"type": "boolean"},
            "path_glob": S.nullable(S.string(4096)),
            "match": S.string(200),
            "order": S.string(200),
            "total": {"type": "integer", "minimum": 0},
            "offset": {"type": "integer", "minimum": 0},
            "returned": {"type": "integer", "minimum": 0},
            "hits": S.array(
                S.obj(
                    {
                        "citation": CITATION,
                        "language": S.nullable(S.string(64)),
                        "snippet": S.string(1000, min_length=0),
                    }
                ),
                SEARCH_PAGE_MAX,
            ),
            "search_scope": S.string(2000),
            "coverage_complete": {"type": "boolean"},
        }
    ),
    handler=_search_code,
    list_key="hits",
)


# -- find_symbols -------------------------------------------------------------------------

VALUE_KINDS = frozenset({"const", "static", "param", "field"})
_DIGIT = re.compile(r"[0-9]")


def _find_symbols(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    glob = _path_filter(args)
    limit = args.get("limit", 20)
    with_calls = args.get("with_calls", False)
    pin = ctx.pin(args["commit"])
    _, gid = ctx.generation(pin.commit)
    pager = ctx.pager("find_symbols", args, pin.commit, gid)
    offset = pager.offset
    fetch = min(offset + limit, SYMBOLS_MAX_LIMIT)
    result = ctx.index.symbols(
        pin.commit, path_glob=glob, kind=args.get("kind"), name=args.get("name"),
        limit=fetch, with_calls=with_calls,
    )
    ctx.check_deadline()
    pager.total = min(result.total, SYMBOLS_MAX_LIMIT)
    value_note = f"public default at commit {pin.commit}; not a production value"
    symbols = []
    for record in result.symbols[offset:fetch]:
        item: dict[str, Any] = {
            "citation": ctx.citation(pin, record.path, record.start_line, record.end_line,
                                     record.span_sha256, record.blob_oid),
            "kind": record.kind,
            "name": record.name,
            "qualname": record.qualname,
            "container": record.container,
            "name_line": record.name_line,
            "language": record.language,
            "signature": record.signature,
            "modifiers": list(record.modifiers)[:32],
            "backend": record.backend,
            "value_note": (value_note if record.kind in VALUE_KINDS
                           and _DIGIT.search(record.signature) else None),
            "calls": None,
            "calls_total": None,
        }
        if with_calls:
            calls = record.calls or ()
            item["calls_total"] = len(calls)
            item["calls"] = [
                {"callee": call.callee, "qualifier": call.qualifier, "form": call.form,
                 "line": call.line}
                for call in calls[:CALLS_PER_SYMBOL]
            ]
        symbols.append(item)
    warnings = []
    if with_calls and any((s["calls_total"] or 0) > CALLS_PER_SYMBOL for s in symbols):
        warnings.append(f"TRUNCATED: at most {CALLS_PER_SYMBOL} call candidates are listed per "
                        "symbol; calls_total gives the full count")
    data = {
        "filters": {"kind": args.get("kind"), "name": args.get("name"), "path_glob": glob},
        "total": result.total,
        "offset": offset,
        "returned": len(symbols),
        "symbols": symbols,
        "calls_relation": CALL_RELATION if with_calls else None,
        "calls_resolution": CALL_RESOLUTION if with_calls else None,
        "calls_note": (
            "Call candidates are syntax (a name followed by an argument list); nothing checks "
            "which declaration a name refers to." if with_calls else None
        ),
    }
    return ToolResult(data, commit=pin.commit, index_generation=gid, warnings=warnings,
                      pager=pager, truncated=bool(warnings))


CALL = S.obj(
    {
        "callee": S.string(512),
        "qualifier": S.nullable(S.string(512)),
        "form": {"type": "string", "enum": ["call", "method", "path", "macro", "new"]},
        "line": S.LINE,
    }
)

FIND_SYMBOLS = Tool(
    name="find_symbols",
    title="Find declarations",
    description=(
        "Declarations (functions, methods, classes, traits, constants, param!, imports, ...) "
        "found by the syntax backends at a pinned, indexed commit, filtered by exact or GLOB "
        "name, kind and path, each with a citation of its full span and SHA-256. "
        "with_calls=true adds unresolved call candidates (syntax only, never resolved). "
        "Numbers in signatures are public defaults at that commit. Signatures are untrusted "
        "upstream data."
    ),
    input_schema=S.obj(
        {
            "commit": S.COMMIT_FULL,
            "name": S.string(512, description="Name or qualified name; GLOB if it has * ? or [."),
            "kind": {"type": "string", "enum": list(SYMBOL_KINDS)},
            "path_prefix": PATH_PREFIX,
            "path_glob": PATH_GLOB,
            "with_calls": {"type": "boolean", "default": False},
            "limit": _limit(SYMBOLS_PAGE_MAX, 20),
            "cursor": CURSOR,
        },
        required=["commit"],
    ),
    data_schema=S.obj(
        {
            "filters": S.obj(
                {
                    "kind": S.nullable(S.string(32)),
                    "name": S.nullable(S.string(512)),
                    "path_glob": S.nullable(S.string(4096)),
                }
            ),
            "total": {"type": "integer", "minimum": 0},
            "offset": {"type": "integer", "minimum": 0},
            "returned": {"type": "integer", "minimum": 0},
            "symbols": S.array(
                S.obj(
                    {
                        "citation": CITATION,
                        "kind": {"type": "string", "enum": list(SYMBOL_KINDS)},
                        "name": S.string(1024, min_length=0),
                        "qualname": S.string(4096, min_length=0),
                        "container": S.nullable(S.string(4096, min_length=0)),
                        "name_line": S.LINE,
                        "language": S.nullable(S.string(64)),
                        "signature": S.string(1000, min_length=0),
                        "modifiers": S.array(S.string(256), 32),
                        "backend": S.string(128),
                        "value_note": S.nullable(S.string(200)),
                        "calls": S.nullable(S.array(CALL, CALLS_PER_SYMBOL)),
                        "calls_total": S.nullable({"type": "integer", "minimum": 0}),
                    }
                ),
                SYMBOLS_PAGE_MAX,
            ),
            "calls_relation": S.nullable({"type": "string", "enum": [CALL_RELATION]}),
            "calls_resolution": S.nullable({"type": "string", "enum": [CALL_RESOLUTION]}),
            "calls_note": S.nullable(S.string(400)),
        }
    ),
    handler=_find_symbols,
    list_key="symbols",
)


# -- index_coverage -----------------------------------------------------------------------

LEXICAL_STATUSES = ("indexed", "skipped")
SYNTAX_STATUSES = ("parsed", "partial", "failed", "not-applicable", "unsupported", "skipped")


def _index_coverage(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    prefix = args.get("path_prefix")
    if prefix is not None:
        check_repo_path(prefix, "path_prefix", prefix=True)
    include = args.get("include_files", False)
    limit = args.get("limit", 50)
    pin = ctx.pin(args["commit"])
    generation, gid = ctx.generation(pin.commit)
    pager = ctx.pager("index_coverage", args, pin.commit, gid)
    coverage = ctx.index.coverage(pin.commit)
    ctx.check_deadline()
    rows = [
        row for row in coverage.rows
        if (prefix is None or row.path.startswith(prefix))
        and args.get("lexical_status") in (None, row.lexical_status)
        and args.get("syntax_status") in (None, row.syntax_status)
    ]
    files = []
    if include:
        pager.total = len(rows)
        files = [
            {
                "path": display_path(row.path),
                "blob_oid": row.blob_oid,
                "language": row.language,
                "classification": row.classification,
                "lexical_status": row.lexical_status,
                "lexical_reason": row.lexical_reason,
                "line_count": row.line_count,
                "indexed_lines": row.indexed_lines,
                "syntax_status": row.syntax_status,
                "syntax_reason": row.syntax_reason,
                "backend": row.backend,
                "symbols": row.symbols,
                "calls": row.calls,
            }
            for row in rows[pager.offset : pager.offset + limit]
        ]
    data = {
        "generation": _generation_dict(generation, gid),
        "summary": coverage.summary,
        "total": len(rows) if include else None,
        "offset": pager.offset,
        "returned": len(files),
        "files": files,
    }
    return ToolResult(data, commit=pin.commit, index_generation=gid,
                      pager=pager if include else None)


OPTIONAL_COUNT = S.nullable({"type": "integer", "minimum": 0})

INDEX_COVERAGE = Tool(
    name="index_coverage",
    title="Code index coverage",
    description=(
        "What the code index covers at a pinned commit: index generation, lexical and syntax "
        "status counts, skip reasons, symbols and call candidates per language. "
        "include_files=true adds per-path rows (filtered by path prefix and status, paged) "
        "with the reason a path was skipped or only partly parsed."
    ),
    input_schema=S.obj(
        {
            "commit": S.COMMIT_FULL,
            "include_files": {"type": "boolean", "default": False},
            "path_prefix": PATH_PREFIX,
            "lexical_status": {"type": "string", "enum": list(LEXICAL_STATUSES)},
            "syntax_status": {"type": "string", "enum": list(SYNTAX_STATUSES)},
            "limit": _limit(COVERAGE_PAGE_MAX, 50),
            "cursor": CURSOR,
        },
        required=["commit"],
    ),
    data_schema=S.obj(
        {
            "generation": GENERATION,
            "summary": S.obj(
                {
                    "files": {"type": "integer", "minimum": 0},
                    "lexical": S.counts(),
                    "lexical_skipped_by_reason": S.counts(),
                    "syntax": S.counts(),
                    "syntax_backends": S.counts(),
                    "languages": {"type": "object", "additionalProperties": S.counts()},
                    "symbols": {"type": "integer", "minimum": 0},
                    "calls": {"type": "integer", "minimum": 0},
                }
            ),
            "total": OPTIONAL_COUNT,
            "offset": {"type": "integer", "minimum": 0},
            "returned": {"type": "integer", "minimum": 0},
            "files": S.array(
                S.obj(
                    {
                        "path": S.string(4096),
                        "blob_oid": S.OID,
                        "language": S.nullable(S.string(64)),
                        "classification": S.string(32),
                        "lexical_status": {"type": "string", "enum": list(LEXICAL_STATUSES)},
                        "lexical_reason": S.nullable(S.string(64)),
                        "line_count": OPTIONAL_COUNT,
                        "indexed_lines": OPTIONAL_COUNT,
                        "syntax_status": {"type": "string", "enum": list(SYNTAX_STATUSES)},
                        "syntax_reason": S.nullable(S.string(1000, min_length=0)),
                        "backend": S.nullable(S.string(128)),
                        "symbols": OPTIONAL_COUNT,
                        "calls": OPTIONAL_COUNT,
                    }
                ),
                COVERAGE_PAGE_MAX,
            ),
        }
    ),
    handler=_index_coverage,
    list_key="files",
)


def register(registry: Registry) -> None:
    for tool in (READ_SPAN, SEARCH_CODE, FIND_SYMBOLS, INDEX_COVERAGE):
        registry.add(tool)
