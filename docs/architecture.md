# Architecture

TimelineXray turns a public repository into evidence that can be checked: every claim
points at exact bytes of a git blob at a pinned commit, and every layer above the git
objects is derived, versioned and reproducible. This document describes the layers, how
the six milestones build them, and the contracts Milestone 1 fixes for the rest.

## Layers and authority

| Layer | Authoritative content | Milestone |
| --- | --- | --- |
| Git object store (bare mirror) | Exact source bytes at known commits | 1 |
| Snapshot manifest | Every path, blob identity, classification, exclusions, license files | 1 |
| Lexical and syntax index | Derived search and symbol records, keyed by blob and parser version | 2 |
| Findings ledger | Reviewed claims, statuses, citations, freshness, supersession | 3 |
| MCP server (public-code profile) | Read-only access to the layers above | 4 |
| Diffs and digests | Classified changes between pins; reviewed Markdown digests | 5 |
| Offline analytics | Descriptive creator metrics from a user's own export, in a separate network-disabled process | 5 |

Each layer only reads the layers above it in this table. Derived layers never become a
source of truth for the layer they derive from, and the analytics process shares nothing
with the others.

## Milestones

1. **Evidence foundation** (implemented). Snapshot store, per-commit manifest, license
   inventory, exact span reader, network guard, `txray pin | manifest | show`.
2. **Native code index** (implemented). Per-commit lexical index (SQLite FTS5) over `parsed-candidate`
   and `text` entries; symbol extraction for Rust, Scala, Python and Java behind an
   extractor interface (lexical fallback always available, optional tree-sitter extra);
   explicit unresolved call candidates; incremental rebuild equal to a clean rebuild;
   `txray search | symbols`.
3. **Findings memory** (implemented; see [findings-memory.md](findings-memory.md)). Append-only JSONL event log with a SHA-256 hash chain (create,
   verify, review, supersede, retract); status, freshness and workflow as separate fields;
   re-anchoring of spans on new commits; research import; review queue; first research
   release (0.1.0).
4. **Agent access** (implemented: 4a code tools, 4b findings tools; see [mcp.md](mcp.md)). Hand-written stdio JSON-RPC MCP server, public-code profile only, with
   input validation and serialized output limits; instruction templates; a client matrix
   that lists only clients actually tested.
5. **Updates and local analytics** (implemented: 5a offline analytics, 5b diff, classification
   and digest; see [updates.md](updates.md); the digest's affected-findings section reads the
   findings ledger since Milestone 6). Two-commit diff and change classification, invalidation
   queue, reviewed digests that never use commit messages as evidence; offline metrics
   (realized weighted engagement, relative reach) from one documented export schema, with a
   process-level network guard.
6. **Release hardening** (implemented, 0.6.0). License and notice review ([NOTICE](../NOTICE));
   security regression suite and threat model (`tests/test_security.py`,
   [SECURITY.md](../SECURITY.md): path inputs, hostile upstream trees, symlinks, huge and
   binary blobs, force-push, deleted-commit and fetch-failure drills, ledger tampering,
   malformed MCP requests, analytics canary egress); documented limits
   ([limits.md](limits.md)); the digest's affected findings read from the ledger; the
   anchor rule refined (`FOUND_MULTIPLE` confirms a span; uniqueness only for relocation);
   strict and published MCP output schemas (`tools/list` at about 34 KB then, 39.9 KB with
   thirteen tools); public internal
   APIs instead of cross-package private helpers; CI readiness (workflow, `make ci`,
   `scripts/`); clean-install walk ([release-checklist.md](release-checklist.md)).

**Optional integration (0.7.0).** `txray export context-layer` (`timelinexray/export/`,
registered by `export_cli.py`, imported only when the command runs) renders the findings
ledger's read-only view (`Ledger.read_events` + `findings.project`) as Markdown notes for a
Context Layer vault and writes them atomically, only inside `--out`, with a manifest that
bounds what a re-export may replace or remove; it refuses destinations in the working tree,
the store, the ledger or an analytics dataset (`mcp.guard.find_analytics_dataset`). It reads
no source bytes and no analytics data, and TimelineXray does not import or depend on Context
Layer. It is a consumer of the findings layer, not a layer of its own. See
[context-layer.md](context-layer.md).

**Audit fixes (0.9.0).** Freshness is read relative to the newest pin in one place
(`findings/freshness.py`), for the CLI, the MCP findings tools and the export; parameter
declarations with their public defaults are read by `params.py` (`txray param`,
`txray param-history`, MCP `get_param` and `param_history`); `txray update --reanchor
[--export]` (`digest/refresh.py`) fetches, pins, re-anchors and writes the digest in one
command. The semantic release gate lives outside the package, in `eval/` (a reviewed
question set, a model-free check in `make ci` and a live harness run by hand; see
[release-checklist.md](release-checklist.md)).

**Stale review (unreleased).** `findings/stale.py` turns the recorded re-anchoring checks
into a prioritised re-review list (`txray findings stale`, the `reanchor` summary and the
`update --reanchor` report, and read-only over MCP as `stale_worklist`): the old span, the
span located or aligned at the target, the exact commands and optional successor drafts. It only reads the ledger and the pins; review,
supersession and retraction stay with the findings service and a named actor. The set
revisions of the gate are separate files in `eval/` (`questions.json` is revision 1); the
abstention development set `eval/dev-abstain.json` is never the gate.

**Setup command.** `txray setup` (`setup_cli.py`) composes existing guarded operations and
adds no network code: `SnapshotStore.pin` (fetches through `netguard.fetch` only when the
commit is not in the mirror; default commit `77d431aabf409ca1c1eed9bec7e2183f7c914e23`, the
commit the release is tested with), `CodeIndex.build` (reads nothing when the generation is
current, so a second run neither fetches nor re-indexes) and, for `--latest`, the guarded
fetch of `txray update` followed by reading the head of the mirrored `main` branch. Flags:
`--commit SHA | --latest`, `--upstream URL`, `--no-index`, `--store DIR`, `--json`. A refused
URL exits 3 before the store is created. It writes only inside the snapshot store.
`txray setup --print-mcp-config [claude-code|json]` prints a `claude mcp add` line or an
`mcpServers` snippet with the absolute executable (`shutil.which("txray")`, else
`<python> -m timelinexray`) and the absolute store; it edits no client configuration and
touches neither the store nor the network.

## Milestone 1 contracts

### Snapshot store

```
<store>/
  mirrors/<name>-<sha256(url)[:12]>.git/   bare mirror of one upstream URL
  pins/<commit>.json                       pin record
  manifests/<commit>.json                  canonical manifest bytes
  manifests/<commit>.json.sha256           "<sha256>  <commit>.json"
```

The default store is `$TXRAY_STORE`, else `$XDG_CACHE_HOME/timelinexray`, else
`~/.cache/timelinexray`: outside the project tree, so upstream files never enter an agent's
instruction-loading hierarchy.

A mirror is created with `git init --bare` (no templates or hooks) and records its upstream
URL in `txray.upstream`; only `refs/heads/*` and `refs/tags/*` are fetched. `txray pin`
accepts only hexadecimal commit ids (7 to 40 digits), so a pin never follows a moving
branch. It fetches only when the commit is not yet present, then creates
`refs/txray/pins/<commit>` so a later pruning fetch cannot make the pinned commit
unreachable. Automatic garbage collection is disabled in mirrors.

The pin record holds the upstream URL, mirror path, tree, committer time, manifest path and
SHA-256, entry count, pin time and tool version. Re-pinning rebuilds the manifest and
requires it to be byte-identical to the stored one.

### Manifest

One entry per path listed by `git ls-tree -r` (blobs, symlinks and gitlinks), so the entry
count always equals that command's line count. Each entry records `path`, `mode`, `type`,
`oid`, `size`, `sha256`, `classification`, `reason`, `rule`, `language` and `utf8`.
Classification rules, applied in order, first match wins:

| Order | Result | Rule |
| --- | --- | --- |
| 1 | excluded / `submodule` | gitlink (mode 160000) |
| 2 | excluded / `symlink` | mode 120000; never followed |
| 3 | excluded / `vendored` | a directory named `vendor`, `vendored`, `third_party`, `third-party`, `thirdparty` or `node_modules` |
| 4 | excluded / `generated` | protobuf output (`_pb2.py`, `_pb2_grpc.py`, ...) or a lockfile name (`Cargo.lock`, ...) |
| 5 | excluded / `oversize` | larger than `max_file_bytes` (default 1 MiB) |
| 6 | excluded / `binary` | a NUL byte in the first 8000 bytes |
| 7 | excluded / `generated` | `@generated`, protoc or Go "Code generated ... DO NOT EDIT." marker in the first 2048 bytes |
| 8 | `parsed-candidate` | extension of Rust, Scala, Python or Java |
| 9 | `text` | everything else |

The manifest also carries commit metadata, the classifier settings (with a version number),
counts, and the license inventory: every file named like `LICENSE*`, `LICENCE*`,
`COPYING*`, `NOTICE*` or `THIRD_PARTY_NOTICES*` with keyword hints (for example
`Apache-2.0`, `MIT`, `spdx:BSD-3-Clause`). Hints are not license determinations.

The manifest contains no timestamps of its own. It is serialized with sorted keys, a
one-space indent, ASCII escapes and a trailing newline; its identity is the SHA-256 of those
bytes, which is checked against both the sidecar and the pin record on every load.

### Byte-to-line mapping

Spans are cut from blob bytes read with `git cat-file`; nothing is decoded or normalized.

1. Only LF (0x0A) ends a line. A line includes its LF; a CR before the LF stays in the line,
   so CRLF lines end in `\r\n`. A lone CR is ordinary content.
2. If the blob does not end in LF, the bytes after the last LF form a final unterminated
   line. A blob ending in LF has no extra empty line.
3. An empty blob has zero lines:
   `line_count = count(LF) + (1 if blob and not blob.endswith(LF) else 0)`.
4. `A-B` (1-based, inclusive, `1 <= A <= B <= line_count`) is the bytes from the first byte
   of line A through the end of line B, including its terminator if present. Ranges outside
   the file are errors, never clamped.
5. Offsets are 0-based, start inclusive, end exclusive: `blob[start_byte:end_byte]`.
   Adjacent spans concatenate to the blob exactly.

`span_sha256` hashes exactly the span bytes; `blob_sha256` hashes the whole blob. Anchors
are matched as exact UTF-8 bytes inside the span only; the verdict is `FOUND` (one
occurrence), `FOUND_MULTIPLE` (several, every one of them inside the span and all
reported; none is picked as "the" match) or `MISSING`. `FOUND` and `FOUND_MULTIPLE` both
confirm the span's content at its own commit (Milestone 6 refinement; `AMBIGUOUS` was the
earlier name of `FOUND_MULTIPLE` and made such citations unusable). Uniqueness is required
only when a span is relocated to another commit, and relocation matches the exact span
bytes, never an anchor occurrence (see [findings-memory.md](findings-memory.md)).

### Network guard

`timelinexray.netguard.fetch` is the only function that starts a network-capable process.
It checks the URL against the allowlist first (exact match for
`https://github.com/xai-org/x-algorithm.git`, realpath match for configured `file://`
URLs) and raises `NetworkRefused` otherwise. Git then runs with only that transport enabled,
redirects off, credential helpers and hooks off, submodule recursion off and object
checking on, in an environment that ignores inherited `GIT_*` variables and user git
configuration. All other git use goes through `timelinexray.gitio`, which permits only
local subcommands and disables every transport. The test suite checks this statically.

## Interfaces later milestones consume

- `SnapshotStore.load_manifest(commit)` returns the verified pin record and `Manifest`;
  `Manifest.entries` with `classification` in `parsed-candidate`/`text` is the index input.
- `SnapshotStore.repo_for(pin)` returns the `GitRepo` mirror; `GitRepo.read_blobs(oids)`
  streams verified blob bytes, so parse caches can be keyed by blob id.
- `SnapshotStore.read_span(commit, path, A, B, anchor=...)` and
  `timelinexray.span.read_span(...)` produce the citation record (commit, path, lines, byte
  range, blob and span SHA-256, anchor verdict) that findings, verification and the MCP
  server share.
- `span.find_occurrences` and `span.LineMap` support re-anchoring a span on a new commit.
- `netguard.fetch` stays the only fetch path when updates are scheduled in Milestone 5.

All modules are implemented: `findings` and `verify` (Milestone 3; see
[findings-memory.md](findings-memory.md)), `mcp` (Milestone 4: code and findings tools; see
[mcp.md](mcp.md)), `diff` and `digest` (Milestone 5b; see [updates.md](updates.md)).

- The MCP findings tools (`timelinexray/mcp/tools/findings.py`) use only
  `FindingsMemory(Ledger(dir), store).view()`, `ledger_directory` and
  `Verifier(store, index).check / relocate`; the dataset refusal covers the ledger and the
  directories above it.
- `DiffEngine(store, SymbolSource(index)).diff(old, new)` -> `CommitDiff` (files, hunks,
  classified items, citations on both commits); `timelinexray.diff.history` (`is_ancestor`,
  `Lineage.chain`, upstream refs and events).
- `timelinexray.digest.findings.AffectedFindingsProvider` / `default_findings_provider(store,
  ledger)`: `LedgerFindingsProvider` reads the findings ledger (verified, shared lock, never
  written; `Ledger.read_events`) and places each active finding's spans on the digest's
  commits.

Public internal APIs shared across packages (Milestone 6; `tests/test_internal_api.py` fails
on any cross-package use of a private name or module):

- `timelinexray.fsutil.atomic_write(path, data)`: the one way a file the tool owns is
  replaced (snapshot store, update state, digests, the update status file and exports).
- `timelinexray.fsutil.check_output_directory(out, store_root=, ledger=)`: the one rule
  for a `--out` directory (`digest`, `update`, and `export`, which adds its own): no
  symbolic link, existing parent, not inside the source tree or package, the store, the
  ledger or an analytics dataset.
- `timelinexray.syntax.extract_checked(backend, text, language, line_count)`: a backend run
  with its records validated (index build and diff symbols).
- `timelinexray.syntax.source`: `mask`, `Masked`, `Lines`, `match_delimiters` for the diff
  classifier.
- `timelinexray.textsafe.visible` / `visible_bytes`: untrusted text for terminals and
  Markdown.
- `timelinexray.mcp.schema.published_output`: the lean `outputSchema` published for a
  tool's strict schema, which every result is validated against.

- `timelinexray.verify.Verifier(store, index).check / relocate`: citation integrity and
  re-anchoring (outcomes and freshness) for the Milestone 5 update pipeline.
- `timelinexray.findings.FindingsMemory(Ledger(dir), store).view()`: read-only projection
  (`FindingState.summary()` / `to_dict()`, `View.queue()`) for the MCP findings tools, which use
  only read methods and resolve the ledger with `timelinexray.findings.ledger_directory`.
- `goldens/citations.json` and `timelinexray.findings.goldens.run` for release gates.
`index` and `syntax` are implemented (Milestone 2; see [code-index.md](code-index.md)) and
`analytics` is implemented (Milestone 5a; see [analytics.md](analytics.md)).

- `CodeIndex(store).search / symbols / calls / coverage / generation`: every result carries
  its commit; call candidates are syntactic and never presented as resolved edges.
- `syntax.Backend` / `Registry`: the lexical backend is always available; a tree-sitter
  backend is a documented hook that reports itself unavailable until a pinned runtime exists.

- The MCP server does not import `timelinexray.analytics` and refuses to serve any
  directory containing an analytics `dataset.json` (enforced by `timelinexray/mcp/guard.py`,
  `tests/test_mcp.py` and `tests/test_mcp_stdio.py`)
  (`"format": "timelinexray/analytics-dataset/v1"`); `tests/test_analytics_offline.py`
  fails if any module other than `metrics_cli.py` imports the analytics package.
