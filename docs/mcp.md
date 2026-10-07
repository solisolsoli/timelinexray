# MCP server (Milestone 4: code tools and findings tools)

`txray mcp serve` is a hand-written stdio JSON-RPC 2.0 server (no MCP SDK) that serves one
snapshot store to MCP clients. It has one profile, **public-code**, and it is read-only:
pinned commits, manifests, exact source spans, lexical search, symbols with unresolved
call candidates and index coverage (Milestone 4a), parameter declarations with their
public defaults at a commit and over the pinned commits (`get_param`, `param_history`),
and the findings of one findings ledger with span re-verification (Milestone 4b, see
[Findings tools](#findings-tools)).

```
txray pin <commit>                        # outside the server: fetch + manifest
txray index <commit>                      # outside the server: code index
txray findings add|import|verify|reanchor|review ...   # outside the server: the ledger
txray mcp serve [--store DIR] [--ledger DIR] [--time-budget SECONDS]
txray mcp tools [--json [--strict]]       # the tool list, the tools/list definitions, or the strict output schemas
```

The store defaults to `$TXRAY_STORE`, else `~/.cache/timelinexray` (as for every command).
The findings ledger is `--ledger DIR`, else `$TXRAY_FINDINGS`, else `<store>/findings`
(`timelinexray.findings.ledger_directory`, as for `txray findings`). The server exits with
code 1 before reading any message if the store does not exist, if an explicit `--ledger`
is not an existing directory, or if an analytics dataset lies in or above the store or the
ledger; otherwise it serves until stdin closes and exits 0. A default ledger location that
does not exist (yet) or lies inside a git working tree does not stop the server: the
findings tools report it. Logs go to stderr; stdout carries only protocol messages.

## Protocol revisions and how they were verified

Supported: **`2026-07-28`** (current) and **`2025-11-25`** (previous). The server is
"dual-era" in the specification's terms: a request carrying per-request `_meta` is served
statelessly under 2026-07-28; an `initialize` request selects 2025-11-25 semantics for the
rest of the process.

P7 section 5.1 states that the current revision is 2026-07-28, that every request declares
its protocol version, that `server/discover` exposes supported versions and capabilities,
and that older handshake-based revisions need a separate compatibility path. These
statements were treated as claims and checked against the official specification pages
below, all retrieved on **2026-09-30**. All four statements are confirmed. P7's security
guidance link (section 5.5) was not re-fetched; the controls below are this project's own.

| Page (retrieved 2026-09-30) | What it establishes, as implemented here |
| --- | --- |
| <https://modelcontextprotocol.io/specification/versioning> | The current protocol version is 2026-07-28. Every request declares its version in `_meta["io.modelcontextprotocol/protocolVersion"]`; unsupported versions get `UnsupportedProtocolVersionError`; handshake-based revisions are 2025-11-25 and earlier. |
| <https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning> | Error `-32022` with `data.supported` and `data.requested`; servers MUST implement `server/discover`; a dual-era server picks its behaviour from how the client opens (per-request `_meta` = modern, `initialize` = legacy for that stdio process). |
| <https://modelcontextprotocol.io/specification/2026-07-28/basic/index> | Required request `_meta` keys: `protocolVersion` and `clientCapabilities` (missing = `-32602`); every result carries `resultType` (`"complete"`); servers SHOULD put `io.modelcontextprotocol/serverInfo` in each result's `_meta`; request ids are strings or integers, never null; codes `-32020`..`-32099` are reserved for the specification; the protocol is stateless. |
| <https://modelcontextprotocol.io/specification/2026-07-28/server/discover> | `DiscoverResult`: `supportedVersions`, `capabilities`, optional `instructions`, `serverInfo` in `_meta`. |
| <https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio> | One JSON-RPC message per line, no embedded newlines; stdout only valid MCP messages; stderr for logs; exit when stdin closes; dual-era clients probe with `server/discover` first. |
| <https://modelcontextprotocol.io/specification/2026-07-28/server/tools> | `tools/list` and `tools/call`; `structuredContent` plus the same JSON serialised in a text block (SHOULD); a declared `outputSchema` MUST be met; unknown tool = `-32602`; input validation failures are tool execution errors (`isError: true`); deterministic tool order; tool name rules. |
| <https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching> | `ttlMs` (>= 0) and `cacheScope` (`"public"`/`"private"`) are required on `server/discover` and `tools/list` results. |
| <https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/pagination> | Opaque cursors; an invalid cursor is `-32602`. |
| <https://modelcontextprotocol.io/specification/2026-07-28/changelog> | The `initialize` handshake, sessions and `ping` are removed in 2026-07-28; `resultType` added; caching fields required. |
| <https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle> | `initialize` / `notifications/initialized`; if the requested version is unsupported the server answers with another version it supports (SHOULD be its latest); `ping` is allowed. |
| <https://modelcontextprotocol.io/specification/2025-11-25/server/tools> | The same tool semantics without `resultType` or caching fields. |
| <https://modelcontextprotocol.io/specification/2025-11-25/basic/transports> | stdio framing; messages are UTF-8. |
| <https://github.com/modelcontextprotocol/specification/blob/main/schema/2026-07-28/schema.ts> | `LATEST_PROTOCOL_VERSION = "2026-07-28"`; shapes of `UnsupportedProtocolVersionError`, `DiscoverResult`, `RequestMetaObject`. |

The MCP protocol version, the result envelope version (`timelinexray/mcp-result/v1`) and
the package version are three separate identifiers.

## Methods

| Method | Behaviour |
| --- | --- |
| `server/discover` | Modern only. `supportedVersions` `["2026-07-28", "2025-11-25"]`, capabilities `{"tools": {"listChanged": false}}`, `instructions`, `ttlMs` 3600000, `cacheScope` `"public"`. |
| `initialize` | Legacy. Answers `protocolVersion` `2025-11-25` whatever was asked (a client that cannot use it disconnects, per 2025-11-25), plus capabilities, `serverInfo`, `instructions`. |
| (`instructions`) | The evidence rules in one paragraph, ending with the answer-or-abstain rules R1-R4 of [agents/README.md](agents/README.md#answer-or-abstain-rules-r1-r4) (request-time values are not in the code; rules need a span that states them; stop after two unproductive searches in a row or eight in all; an abstention states its scope). |
| `notifications/initialized`, any notification | Accepted, never answered. |
| `ping` | Legacy only: `{}`. With modern `_meta`: `-32601` (removed in 2026-07-28). |
| `tools/list` | The thirteen tools in a fixed order, one page (a cursor is `-32602`). Modern results add `ttlMs`/`cacheScope`. |
| `tools/call` | Arguments validated against the tool's `inputSchema`, then run under the time budget. |
| anything else | `-32601`. The server never sends requests or notifications. |

Errors: `-32700` for invalid UTF-8, invalid or non-standard JSON (`NaN`, `Infinity`),
duplicate object keys or a leading byte-order mark; `-32600` for non-objects, JSON-RPC
batches (not part of MCP), a wrong `jsonrpc`, a null/boolean/fractional id, and messages
over the size limit; `-32601` unknown method; `-32602` for malformed `params`, a missing or
malformed `_meta` (modern), a request without `_meta` before `initialize`, a missing tool
name, an unknown tool or non-object `arguments`; `-32022` for an unsupported modern
version; `-32603` for an unexpected internal error (details only on stderr). Everything a
model can correct (argument types, patterns, paths, ranges, not-found commits or paths,
refusals) is a tool execution error: a normal result with `isError: true` and the envelope
below.

## Tools

| Tool | Input (required **bold**) | Result `data` |
| --- | --- | --- |
| `list_commits` | none | Pinned commits, newest committer time first (at most 200): tree, manifest SHA-256, entry count, indexed flag, index generation. |
| `resolve_commit` | **`commit`** (7-40 lowercase hex) | The full commit id of a unique pin, with the same fields. Branch and tag names are refused. |
| `manifest_summary` | **`commit`** (40 hex) | Counts by class, exclusion reason and guessed language; classifier settings; parents; license files with keyword hints (not license determinations). |
| `read_span` | **`commit`**, **`path`**, **`start_line`**, **`end_line`**, `anchor` | Exact span text (or `base64` when not UTF-8), citation, byte offsets, blob SHA-256 and size, line terminators, anchor verdict (`FOUND`/`FOUND_MULTIPLE`/`MISSING`, first 50 lines), classification. At most 120 lines and 16 KiB, and the JSON-encoded text must fit the 64 KiB response line (it is sent twice, control characters escaped): larger or out-of-range spans are rejected, never clamped or shortened, and a span that would not fit the line is rejected before answering with the number of lines from `start_line` that do fit. |
| `search_code` | **`commit`**, **`query`** (<= 512 chars), `literal`, `path_prefix` or `path_glob`, `limit` (1-20, default 10), `cursor` | Lines containing every term (case-insensitive substring), ordered by path then line; `total` of all matching lines; each hit has a one-line citation and the snippet; `search_scope` and `coverage_complete` state what was not searchable. |
| `find_symbols` | **`commit`**, `name` (exact, or GLOB with `* ? [`), `kind`, `path_prefix` or `path_glob`, `with_calls`, `limit` (1-50, default 20), `cursor` | Declarations with a citation of their full span, signature, modifiers, backend; `value_note` "public default at commit ..." on constants, statics, fields and `param!` declarations whose signature contains a number; with `with_calls`, up to 50 call candidates per symbol (`CANDIDATE_CALL`, `unresolved`) and `calls_total`. |
| `index_coverage` | **`commit`**, `include_files`, `path_prefix`, `lexical_status`, `syntax_status`, `limit` (1-200, default 50), `cursor` | Index generation, the coverage summary (statuses, skip reasons, backends, per-language counts), and with `include_files` the matching per-path rows. |
| `get_param` | **`commit`**, **`name`** (exact name or qualified name, <= 256 chars, no GLOB) | Every `const`, `static`, `field` or `param!` declaration of that name at the indexed commit (found through the code index, then read from the blob with the Milestone 5b value extraction): `declaration`, `symbol_kind`, `qualname`, `ordinal`, a citation of the declaration span, the literal public default `value` (`null` when computed or in test code), `type` and `flag` of a `param!`, `signature`, and `value_note` "public default at commit ...; not a production value"; `mentions_total` and up to 10 `mentions` (lines containing the name outside the declarations; lexical, unresolved). Config-file keys are not reachable by name (use `search_code`). |
| `param_history` | **`name`**, `base`, `head` (40 hex, pinned; default: the oldest and the newest pin of the same mirror) | The value of every declaration of that name at every pinned commit on the first-parent chain from `base` to `head`, oldest first, from pinned commits only (nothing is fetched; commit messages are never read): `commits` (committer time, whether indexed, `lookup` `index` or `known-paths` for a pin that is not indexed, where only the paths indexed commits declare the name are checked), per declaration `points` (commit, committer time, value), `changes` (`declared`, `value-changed`, `removed`; the pinned commit where the new state is first seen, the previous pinned commit, both values and both declarations with citations), `reversions` (a value that comes back after a different one) and `current`; `history_complete` with `gaps` (unpinned commits between two pins) and `not_on_line` (pins reached through a merge, diverged, or outside an explicit range). The envelope's `commit` is `null`; a note says the values are public defaults at the cited commits. |
| `find_findings` | `query` (<= 512 chars, <= 16 terms), `component`, `statuses`, `evidence_classes`, `freshness`, `workflows`, `commit` (40 hex, pinned), `current_only` (default `true`), `limit` (1-20, default 10), `cursor` | Findings of the configured ledger in ledger order, each with id, title, claim, component, evidence class and scope; `status` with `status_basis` (`proposed`, `reported`, `reviewed`), `reviewed`, `status_by`; `workflow`; `current`; `freshness` (`value`, the `commit` it refers to, `check` `integrity`/`reanchor`, the verify `event` hash); citations; `value_note` "public default at commit ..." for `PARAM_DEFAULT`; review state (number of reviews, last reviewed status and event, open queue triggers); `superseded_by`; `not_current` counts of matches left out by `current_only`; `search_scope`; the ledger head. |
| `get_finding` | **`finding_id`**, `commit` | The same summary plus limitations, web sources, report attributes, the negative search, dependencies, provenance revisions (spans re-anchored on later commits), every verification check (event, sequence number, time, mode, target, freshness, verdict, reasons) and review (actor, status, rationale, objections) with event hashes, open queue items, supersession, retraction, the finding's event history newest first (sequence number, type, time, actor, event hash) and the ledger head (`head`, `events`). |
| `verify_claim` | `finding_id` **or** `citations` (1-8 of `commit`, `path`, `start_line`, `end_line`, `anchor`, optional `span_sha256`), `target_commit` | Every cited span re-read now with `timelinexray.verify.Verifier`: integrity at its own commit (`INTACT`/`CHANGED`/`MISSING`, reason, anchor verdict, baseline, freshness, observed span SHA-256) and, with `target_commit`, re-anchoring (`identical`, `unchanged`, `relocated`, `changed`, `ambiguous`, `missing`, `unusable`, with the current span, the proposed region of a changed span and candidates); for a finding also its dependencies and negative search, and the ledger's recorded check of the same scope (`recorded`, `matches_recorded`). Overall `at_cited` / `at_target` freshness; `semantic_verdict` is always `NOT_ASSESSED`; `ledger_written` is always `false`. |
| `stale_worklist` | `target` (40 hex, pinned; default: the newest pin), `area` (`parameter`, `scoring`, `other`), `limit` (1-20, default 5), `cursor` | The stale-review worklist of the configured ledger, the same data as `txray findings stale --json` (see [findings-memory.md](findings-memory.md), "Stale review"): `counts`, `batch` steps (findings, commits, command, effect), and per finding not `CURRENT` at the target its rank, area, priority, freshness, status, workflow, the check event, every non-current citation (old span, the located or line-diff-aligned candidate with SHA-256 and anchor verdict, occurrences, where the anchor occurs), dependencies, negative search, unpinned commits, whether a successor draft is possible and the `read` / `decide` commands; `total`, `offset`, `returned`; the ledger head. Commands carry no `--store`/`--ledger`; nothing is written and no draft file is produced. |

`txray mcp tools --json` prints the tool definitions exactly as `tools/list` publishes them
(JSON Schema 2020-12, a strict subset: see `timelinexray/mcp/schema.py`, which also supports
local `$defs`/`$ref`). Each tool has two output schemas (Milestone 6):

- the **strict** output schema (`txray mcp tools --json --strict`): the full envelope and
  `data` schema with every bound, pattern and description. The server validates **every
  result** against it before sending; a result that does not match is withheld and
  replaced by an `ERROR` envelope with code `invalid_output` (fail closed). The test suite
  validates every result it sees against both schemas;
- the **published** `outputSchema` in `tools/list`: a relaxation of the strict one
  (`schema.published_output`). The envelope is closed and lists every member with its JSON
  type, `enum` (`outcome`) or `const` (`schema_version`, `tool`); the `data` object is closed
  with every member required; below that only the shape is kept (member names, JSON types,
  `enum`, `const`), as deep as fits a budget of 1,800 published bytes per tool and always at
  least the members of `data`; deeper objects and arrays give only their type. Value bounds
  (lengths, counts, patterns), descriptions, the envelope's `required` list and the inner
  `required`/closed structure of `error` are only in the strict schema (every envelope
  member is always present); repeated shapes become `$defs` entries. Any value valid under
  the strict schema is valid under the published one. Tool definitions carry no `$schema`
  (JSON Schema 2020-12 is the MCP default dialect) and no `annotations.title` (the
  top-level `title` is the display name).

This keeps the whole `tools/list` response at about 33.1 KB (modern) and 32.9 KB (legacy)
of the 64 KiB line with thirteen tools: 39.9 KB before the lean envelope and the tightened
descriptions (0.11.0, a 2,100-byte budget, where `manifest_summary` had lost depth), 38.0 KB
with twelve tools and a 2,200-byte budget, about 34.3 KB with ten tools and a 2,600-byte
budget, about 61.8 KB with the full schemas. Measured (`tools/list` line, modern, bytes):
envelope bounds dropped 39,779 -> 39,160 (two tools then went deeper), budget 2,000 and
tightened descriptions, no `$schema` or `annotations.title` 36,486, envelope `required`
lists and the commit field's repeated description left to the strict schema 33,094; the
budget is 1,800 published bytes since then, which keeps every tool at its 0.11.0 depth and
gives `manifest_summary` back its `pin`, `counts` and `licenses` member shapes. Every
statement an agent relies on stays in the descriptions (pinned by
`tests/test_mcp.py::SchemaReferenceTest::test_tools_list_keeps_the_contract_statements`).
The published list is pinned by
`tests/mcp_tools_list.json`; a contract change must regenerate it deliberately:
`PYTHONPATH=src python3 -m timelinexray mcp tools --json > tests/mcp_tools_list.json`.

### Result envelope

Every `tools/call` result, success or tool error, has `structuredContent` of this shape and
the same JSON in its single text block:

| Field | Meaning |
| --- | --- |
| `schema_version` | `"timelinexray/mcp-result/v1"` |
| `tool` | The tool name. |
| `outcome` | Tool execution only, never an evidence status: `OK`, `INCOMPLETE` (fewer items than exist; see `warnings` and `next_cursor`), `NOT_FOUND` (commit, path or index missing), `DENIED` (refused: symlink, binary, analytics dataset, store escape), `ERROR`. |
| `commit` | The full commit every citation in `data` belongs to (`null` for `list_commits`, for errors, and for the findings tools, whose citations name their own commits and whose freshness names the commit it refers to). |
| `index_generation` | Identity of the index generation used (`g1-` + hash), or `null`. |
| `data` | Tool-specific, or `null` on errors. |
| `error` | `{code, message}` on errors (`invalid_arguments`, `invalid_input`, `not_found`, `refused`, `span_range`, `integrity_error`, `git_error`, `storage_error`, `time_budget_exceeded`, `output_too_large`), else `null`. |
| `warnings` | Including every truncation marker, which starts with `TRUNCATED:`. |
| `notes` | For results carrying upstream text: the untrusted-content note and "Numbers in upstream code are public defaults at commit <commit>, not production values ..."; the cursor note when a cursor is returned; `get_param` adds how declarations and mentions are found, `param_history` that its values are public defaults at the cited commits computed from pinned commits only. |
| `truncated`, `next_cursor` | Whether items were left out; the continuation cursor where the tool pages. |

A **citation** is `{repo, commit, path, start_line, end_line, span_sha256, blob_oid,
anchor, url, content_trust}`. `repo` is `xai-org/x-algorithm` and `url` a GitHub permalink
only when the pin's upstream is the allowlisted GitHub URL; for `file://` mirrors both are
`null` (no local location is ever shown). `content_trust` is always
`UNTRUSTED_SOURCE_DATA`. A citation's `span_sha256` is exactly what `read_span` and
`txray show` report for the same lines.

## Limits

| Limit | Value | Behaviour |
| --- | --- | --- |
| Request line | 16 KiB (16384 bytes, newline excluded) | Discarded unparsed (also when unterminated at end of input); `-32600` with `data.limit_bytes`; the stream continues. |
| Response line | 64 KiB, the whole serialised JSON-RPC line | List results are shortened by binary search to the most items that fit, with `outcome` `INCOMPLETE`, a `TRUNCATED: the result exceeded ...` warning and, for paging tools, a `next_cursor` at the first omitted item. A result that cannot be shortened (a span) becomes the tool error `output_too_large`. |
| Time budget per tool call | 10 s (`--time-budget`, 0.01-300) | `SIGALRM` interrupts Python code on the main thread (a git child process is killed by `gitio`), and handlers check the deadline between steps; the result is the tool error `time_budget_exceeded`. A signal cannot interrupt a wait inside SQLite's C library, so the index runs in WAL mode (readers never wait for a running `txray index`) and a reader's busy timeout is bounded to 5 s. |
| `read_span` | 120 lines, 16 KiB of span bytes; the JSON-encoded text must also fit the response line | Rejected, never shortened. The encoded size is checked before answering (the text is sent twice; a tab costs 5 bytes, a quote 6): a span within 120 lines and 16 KiB whose encoding would not fit is rejected as `invalid_input` naming the largest line count from `start_line` that fits, never as `output_too_large`. |
| Search | query 512 characters, 16 terms, 20 hits per page; only the first 1000 matching lines can be paged (`total` still counts all) | |
| Symbols | 50 per page, 50 call candidates per symbol | `calls_total` gives the full count. |
| Paths | 1024 characters | See security controls. |
| `find_findings` | 20 findings per page, 20 citations per finding, 16 query terms | `total` counts every match; findings left out by `current_only` are counted in `not_current`. |
| `get_finding` | 20 citations, dependencies, checks, reviews and queue items; 10 provenance revisions; 100 history events (newest first) | Marked with `TRUNCATED:` warnings; `history` is the list the server shortens to fit the response line. |
| `verify_claim` | 8 given citations; 20 results listed (the freshness covers all) | The time budget covers every blob read. |
| `tools/list` | about 33.1 KB of the 64 KiB response line with thirteen tools (39.9 KB in 0.11.0; 61.8 KB before Milestone 6) | A test fails at 34,000 bytes (`tests/test_mcp_findings.py`); `stale_worklist`'s definition must stay under 2,400 bytes (`tests/test_mcp_stale.py`). |
| `stale_worklist` | 20 entries per page (default 5); per entry 10 citations, 5 occurrences, 10 dependencies, 20 read commands; 50 finding ids per batch step; titles 300 and reasons 1,000 characters | `citations_total` and `findings_total` give the full counts. |

Cursors are `base64url(payload).hmac`: the payload binds tool, commit, index generation, a
hash of all other arguments and the offset; the HMAC-SHA256 key is random per server
process. A cursor from another query, commit, generation, tool or process is refused with
`invalid_input`; repeat the query without it.

## Security controls

- **Read-only, no network, no new processes.** The server writes nothing, opens the index
  database read-only, never fetches (it cannot pin), and imports no network module; the
  only child processes are the local `git cat-file` and `git config --get` reads that
  `gitio` already permits (the suite's static checks cover the new package).
- **Only the snapshot store.** The store root is resolved once at start. Every pin
  record, manifest, mirror and the index database must resolve inside it, so a tampered pin
  record (`"mirror": "../elsewhere"`) or a symlinked directory yields `DENIED`. Source bytes
  come only from git blobs selected through the verified manifest; symlink, submodule and
  binary entries are refused.
- **No analytics.** The package never imports `timelinexray.analytics` (enforced by the
  existing import test and by a child-process check of `sys.modules`). The server refuses
  to start, and every later call is `DENIED`, when a `dataset.json` declaring
  `"format": "timelinexray/analytics-dataset/v1"` (or any `dataset.json` that is not a
  regular file) exists anywhere in the store or in a directory above it. No analytics tool
  is listed or callable.
- **Strict inputs.** Closed schemas (unknown properties rejected), exact JSON types
  (`1.0` is not an integer, `true` is not a number), lengths, enums, patterns whose `$`
  matches only at the end (a trailing newline never passes), full 40-digit commit ids
  checked against the store's pins. Paths must be repository-relative and normalised: no
  leading `/` or `~`, no drive letter, no backslash, no `.`/`..`/empty segment, no NUL or
  control character, at most 1024 characters; lookups are exact manifest matches.
- **Untrusted content stays data.** Source text, snippets, signatures and paths are
  returned verbatim as JSON strings, marked `UNTRUSTED_SOURCE_DATA`, with a note to treat
  them as data. The server never interprets them. JSON escaping removes raw control
  characters; U+2028, U+2029 and U+0085 are escaped as well so that no client splits a
  line inside a message. HTML is not escaped: rendering is the client's responsibility.
- **No local locations in responses.** Error messages have the store and home directory
  replaced by `<store>` and `~`; `file://` upstreams are shown as
  `file:// mirror (local location withheld)`.
- **One findings ledger, chosen by the operator.** Clients never name a ledger or any file:
  the findings tools take ids, commits and repository-relative paths only. The ledger
  directory is fixed at start; `events.jsonl`, `HEAD` and `.lock` must resolve inside it and
  be regular files (a symlink out of it or a FIFO is `DENIED`). Every call re-reads the log
  through `Ledger(dir).events()` (projected as `FindingsMemory.view()` does), which re-verifies the whole hash
  chain (a damaged log is the tool error `integrity_error`), holding a shared `flock` on an
  existing `.lock` so a concurrent writer is never seen half-way. Nothing is written: no
  event, no `HEAD`, no lock file (the suite compares the ledger's names, bytes, sizes and
  modification times before and after every call). The analytics-dataset refusal applies to
  the ledger and every directory above it as it does to the store, and error messages show
  the ledger location as `<ledger>`.
- **Annotations are descriptive.** Every tool carries `readOnlyHint: true`,
  `destructiveHint: false`, `idempotentHint: true`, `openWorldHint: false`; these are
  hints for clients, not the access control, which is the list above.

## Deviations from the P7 contract bundle

`P7-mcp-tool-contracts.json` (version 1.0, status "design, not implementation tested")
was the starting point. Differences, and why:

| P7 | Here | Reason |
| --- | --- | --- |
| Eight public-code tools | `search_code`, `read_span`, `find_findings`, `verify_claim`, `get_param` and `param_history` kept; `list_commits`, `resolve_commit`, `manifest_summary`, `find_symbols`, `index_coverage` and `get_finding` added; `explain_component` and `diff_since` deferred | `explain_component` and `diff_since` need a reviewed component map and the digest over MCP. The earlier findings-slot name `search_findings` became P7's `find_findings`. `get_param` and `param_history` were deferred in 0.7.0 and added after the audit on top of the Milestone 5b value extraction. |
| `get_param`: one `parameter` plus `use_sites` | `declarations` (a name can be declared in several files, e.g. `ClickWeight` in `home-mixer/params/param.rs` and `vm-ranker/params.rs`), each with value, type, flag and citation; `mentions` (lexical lines, unresolved) with `mentions_total` | One answer per declaration, never a merged "the" value; mentions are not resolved uses. |
| `param_history`: `base` and `head` required, `limit`/`cursor`, `entries` per commit | `base`/`head` optional (default: the oldest to the newest pin); per-declaration `points`, `changes` with both citations, `reversions`, `current`; `history_complete` and `gaps` kept; `not_on_line`; the list the server may shorten is `timelines` | Pinned commits only (no fetch); the timeline is per declaration so both sides of every change are cited; 10 s like every tool. |
| Envelope `schema_version` `"1.0"`, `index_commit` | `"timelinexray/mcp-result/v1"`, `commit`; added `tool`, `error`, `notes`; `index_generation` may be `null` | A named envelope version; one commit field for all tools (`null` only where no single commit applies); errors in the same envelope as successes. |
| Citation `anchor_string` required, `url` required | `anchor` nullable; `repo`/`url` `null` for `file://` mirrors | Spans can be read without an anchor; no permalink can be made to a local mirror, and local paths are never shown. |
| `search_code`: `languages` filter, query <= 2048, hits with `symbol` and `retrieval_score`, snippet <= 1000 | No `languages` (use `path_glob`, e.g. `*.rs`); `literal`, `path_prefix`, `path_glob`; query <= 512; no symbol or score; snippet <= 240 | The Milestone 2 index limits and semantics: every line matching all terms, deterministic order, no ranking. |
| `read_span` output: text, byte length, citation | Adds `anchor` input and verdict, `base64` for non-UTF-8, offsets, blob hash, line terminators, classification | The Milestone 1 span record, so a citation can be checked completely. |
| Output cap 64 KiB | 64 KiB on the whole JSON-RPC line | The envelope is sent twice (structured and text), so capping the line is the stricter reading. |
| Per-tool budgets 10-30 s | 10 s for every current tool | The 30 s tools are not implemented. |
| Two concurrent code reads | Sequential | stdio messages are handled one at a time. |
| Resources (`project://manifest`, ...) | None | `manifest_summary` and `index_coverage` cover the manifest resource; resources would add a capability, URI scheme and caching rules for little gain now. |
| A search with no hits | `OK` with `total: 0` and `coverage_complete` | `NOT_FOUND` is reserved for a missing commit, path or index; exhaustiveness is stated by `coverage_complete` and `search_scope`, and truncated results are always `INCOMPLETE`. |
| `find_findings`: `commit` required; `query` <= 2048; filters `statuses`, `freshness` (without `NOT_CHECKED`) | `commit` optional (default: each finding's newest check, relative to the newest pin); `query` <= 512 characters and 16 terms; `freshness` includes `NOT_CHECKED` and the derived `NOT_APPLICABLE`; added `component`, `evidence_classes`, `workflows`, `current_only` (default `true`) | Findings are checked against different commits; the newest check is the natural default, a newer unchecked pin makes it not current, and a `commit` asks for a historical answer. Query limits as `search_code`. `NOT_CHECKED` is a real freshness value of the ledger; `NOT_APPLICABLE` is the reading of a finding with nothing to re-verify. `current_only` makes "never present a STALE finding as current" the default instead of a convention. |
| `find_findings` item: P7 `Claim` (`id`, `claim`, `status`, `evidence_class`, `freshness` string, `citations` <= 8, `limitations`) | `finding_id`, `title`, `claim`, `component`, `scope`, `status` with `status_basis`/`reviewed`/`status_by`, `workflow`, `current`, `freshness` object with its commit and check event, citations <= 20 (ledger citation shape), `value_note`, review state, `superseded_by`, `source_label`; `limitations` in `get_finding` | Status basis, workflow and the commit of the freshness are needed to avoid presenting proposed or stale findings as confirmed and current. |
| `verify_claim`: input `commit`, `claim`, `anchors` (1-8, each with a required `span_sha256`); output `anchor_integrity` (`valid`, `reason`), `entailment` (`VERIFIED_BY_RULE`, `HUMAN_REVIEWED`, `NOT_ESTABLISHED`, `CONTRADICTED`), `finding`, `verification_method`; 30 s | Input `finding_id` or `citations` (1-8, `span_sha256` optional: without it the read is a `baseline`) plus optional `target_commit`; no claim text; output per-citation integrity and re-anchoring, dependencies, negative search, `at_cited`/`at_target` freshness compared with the ledger, `semantic_verdict` `NOT_ASSESSED` (constant), `ledger_written` `false` (constant); 10 s like every tool (`--time-budget`) | The server cannot establish entailment, so it never returns an entailment verdict; the ledger's reviewed status is returned beside the integrity result instead. Checking a recorded finding by id is the common case and keeps its recorded hashes authoritative. |
| `get_finding` | Not in P7 | One finding with its full check, review and event history and the ledger head, for citing a finding's provenance. |

## Findings tools

Milestone 4b (`src/timelinexray/mcp/tools/findings.py`) serves the findings memory of
Milestone 3 (see [findings-memory.md](findings-memory.md)) read-only. Findings are recorded,
verified, re-anchored and reviewed only with `txray findings ...` outside the server.
`stale_worklist` (`src/timelinexray/mcp/tools/stale.py`) serves the stale-review worklist of
the same ledger through the same guarded, chain-verified read: a worklist, not a verdict.

- **Three separate fields.** Every finding carries its evidence `status` with
  `status_basis` (`proposed` by its author, `reported` by an imported research report,
  `reviewed` by a review event; `reviewed` is `true` only for the last), its `freshness`, and
  its `workflow` (`draft`, `imported`, `reviewed`, `superseded`, `retracted`).
- **Freshness names its commit, relative to the newest pin.** Without `commit`, a finding's
  freshness is its check at the newest pinned commit it was re-anchored on (`check:
  "reanchor"`, `commit` = that target), else the integrity check at its cited commit
  (`check: "integrity"`, `commit` = the cited commit when all evidence cites one commit),
  else `NOT_CHECKED`; `newer_pins` lists the pins of the same upstream with a later
  committer time and no recorded check (`newest_pin` is the store's newest), and
  `checkable` says whether the finding has code evidence at all. With `commit` C, it is the
  check against C, or the integrity check when the finding cites only C (a historical
  answer names C), else `NOT_CHECKED` at C, and `newer_pins` is empty. A finding with
  external evidence only (no code citation, dependency or negative search) reads
  `NOT_APPLICABLE` with `checkable: false`, a `note`, the latest recorded `retrieved`
  date and a `recheck_after` hint parsed from its text (see
  [findings-memory.md](findings-memory.md), "Current").
- **Only current findings by default.** `current` is `true` only for an active finding
  (`draft`, `imported`, `reviewed`) that is `CURRENT` with no unchecked newer pin, or
  `NOT_APPLICABLE`. `find_findings` returns only such findings unless `current_only` is
  `false`; asking for other freshness values or for superseded/retracted findings with
  `current_only` left `true` is `invalid_input`. `not_current` counts the matches that
  were left out by reason (`STALE`, `UNVERIFIABLE`, `NOT_CHECKED`, or
  `NEWER_PIN_UNCHECKED` for `CURRENT` findings with unchecked newer pins), and warnings say
  so, naming `txray findings reanchor --latest` as the way to refresh them (outside the
  server). `get_finding` returns any finding and warns when it is not current, naming the
  newest unchecked pin.
- **Citations.** A finding's code citations use the citation shape above plus
  `resolution`: resolved citations (every created finding, and imported ones whose commit
  was pinned) have every field set and `resolution` `null`; an imported citation that could
  not be read keeps the reported commit and lines, `null` hashes and the reason until a
  later `txray findings verify` reads it (the commit was pinned afterwards): the tools then
  show the resolved form from that provenance revision, with hashes and a permalink, and
  `verify_claim` compares against the recorded hashes. Provenance revisions, dependencies
  and `verify_claim` results use a compact span (`commit`, `path`, `start_line`, `end_line`,
  `span_sha256`).
- **Integrity is not truth.** `verify_claim` re-reads spans with the Milestone 3 verifier,
  exactly as `txray findings verify` (at the cited commit) and `txray findings reanchor`
  (at a target, starting from the latest provenance revision) do, but records nothing. A
  finding dependency is taken from the ledger (its recorded freshness at the target), not
  re-verified. `semantic_verdict` is always `NOT_ASSESSED`, and only a review event can set
  a reviewed status.
- **Errors.** No ledger configured (for example the default lies inside a git working
  tree): `DENIED` with the reason. Ledger directory missing: `NOT_FOUND`. A log that fails
  verification: `ERROR` / `integrity_error`. An unknown finding id or an unpinned `commit`
  or `target_commit`: `NOT_FOUND`. A cited commit that is not pinned is a `MISSING`
  verdict (`commit_not_pinned`), not an error.

Every result carries the untrusted-content note plus notes that finding text is ledger data
written by declared, unauthenticated actors, that only `status_basis` `reviewed` is an
assessed status, that freshness is relative to the named commit, and that numbers in
`PARAM_DEFAULT` findings are public defaults at the cited commit.

A matching hash is never presented as a `SUPPORTED` claim; only reviewer events can
produce that status.

## Client matrix

"Tested" means the client was actually run against this server; nothing else is claimed.

| Client | Version | Configuration | What was run (2026-09-30) | Status |
| --- | --- | --- | --- | --- |
| Test suite (`tests/test_mcp_stdio.py`, `tests/test_mcp_findings.py`) | this repository | child process over stdio | Legacy and modern sessions, every tool, malformed JSON, unknown method, invalid params, oversized request, path traversal, cursors; every code tool against 77d431a and the findings tools on a ClickWeight finding re-anchored from 4c5cfe8 to a707cc2 when a local clone is available | tested |
| Claude Code | 2.1.286, macOS | `claude -p ... --mcp-config FILE --strict-mcp-config --allowedTools ...` with an `mcpServers` stdio entry, `python3 -m timelinexray mcp serve --store DIR` and `PYTHONPATH` (the shape of `docs/agents/claude-code-source.mcp.json`) | The client probed with `server/discover` (2026-07-28), then `tools/list`, `tools/call list_commits`, `read_span`, `search_code`; answers matched direct queries | tested (print mode, modern protocol; code tools only - the findings tools have not been run with Claude Code) |
| Claude Code, interactive, project `.mcp.json` | - | `docs/agents/claude-code.mcp.json` with an installed `txray` | not run (needs interactive approval and an installed entry point) | untested |
| Any client speaking 2025-11-25 | - | - | Only the test suite's legacy session | untested with a real client |
| OpenAI Codex CLI / IDE, ChatGPT, Cursor, others | - | - | not installed or not run | untested |
