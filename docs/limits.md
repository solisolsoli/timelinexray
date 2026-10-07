# Known limits

Everything TimelineXray knows it does not do, does not know or has not tested, in one place
(0.11.0). Each item links to the document with the details. A limit here is a statement of
scope, not a bug report; where a limit is enforced, the test that enforces it is named.

## What the evidence can and cannot say

- **Public defaults are not production values.** The upstream README (at `77d431a`) says
  tunable values are read from a configuration system that is synced periodically and that
  experiments run on a share of traffic. Every number TimelineXray shows is a *public
  default at a named commit*; live values, experiment arms and request-time values are
  unknown ([updates.md](updates.md), [findings-memory.md](findings-memory.md)).
- **No model weights, no model runs.** TimelineXray contains, downloads and runs no model
  and no weights. Model code in the upstream repository is parsed as text; learned
  parameters, feature values, embeddings and predicted probabilities are unknown.
- **No production truth and no prediction.** Nothing computes an "algorithm score", a
  ranking, a reach prediction or an account label. Source presence is not evidence that a
  component is active for any request.
- **Text identity is not truth.** A matching SHA-256 shows that bytes are unchanged, not that
  a claim about them is true. The verifier never assesses claims; `verify_claim` reports
  `semantic_verdict: NOT_ASSESSED`; only a reviewer event sets a reviewed status.
- **Commit messages are never read**, so intent stated in a message is invisible to digests.
- **Only the public repository.** No X API, no web pages, no internal systems. Web sources in
  findings are recorded as given and never fetched.

## Parsing and search

- **Lexical parser only.** Symbols and call candidates come from the always-available
  lexical backend; the tree-sitter backend is a documented hook that reports itself
  unavailable (no grammar is a dependency). Extents are heuristic; names, types, imports
  and implicits are never resolved; macros are not expanded; call candidates stay
  `unresolved` ([code-index.md](code-index.md)).
- Rust enum variants, struct fields and Java enum constants are not reported as symbols.
- Non-UTF-8 files are not searchable; files over 1 MiB (`oversize`), binary, generated and
  vendored files are not indexed; blobs over 64 MiB are not read at all.
- A path name that is not UTF-8 is kept as its exact bytes and shown with each undecodable
  byte escaped (`caf\xe9.py`); in a `--path` pattern or an MCP `path_prefix` the escaped form
  is a lone surrogate (U+DC80-U+DCFF, Python's `surrogateescape`), which a shell cannot
  type, so such a file is easiest to reach with a `*` glob around the readable part
  (`tests/test_nonutf8_paths.py`). A lone surrogate in a search term or symbol name is an
  input error.
- Repository paths over 4,096 characters are refused on the command line; over MCP, paths
  over 1,024 characters or containing control characters or backslashes are refused, so such
  upstream files can only be read with `txray show` (or not at all).
- Search is substring matching on single lines (all terms, or one literal phrase), not
  semantic search.
- `txray param` / `get_param` and `txray param-history` / `param_history` find
  declarations by exact name through the index (`const`, `static`, `field`, `param!`), so
  config-file keys (YAML, TOML, INI, JSON) are not reachable by name, a parameter declared
  in several files gives several answers, and a value that is computed rather than literal
  is reported without a default. The history covers pinned commits only: a change is first
  seen at the pinned commit after it, and unpinned commits between two pins are reported
  as gaps; a declaration that moves to another file appears as removed and declared.

## Findings memory

- **A hash chain is not a signature.** A consistent rewrite of the whole log and `HEAD` is
  detected only against an independently kept head (`verify-log --expect-head`;
  `tests/test_security.py`). Actor names are declared, not authenticated; the
  no-self-approval rule compares names.
- A span moves to another commit only when its exact bytes occur there exactly once; any
  other change makes it `STALE` or `UNVERIFIABLE`, and the line-diff proposal for a changed
  span is only a proposal. Files over 50,000 lines are not diffed for proposals.
- **`current` is relative to the newest pin** (FA-001, fixed): a pin of the same upstream
  that is newer than a finding's latest check makes the finding *not current* until
  `txray findings reanchor --latest` (or `txray update --reanchor`) checks it there.
  Nothing re-anchors by itself; schedule `txray update --reanchor` to keep the ledger
  fresh ([updates.md](updates.md)).
- **External evidence is not re-verified** (FA-003, fixed as far as it can be): a finding
  with web sources only (`OFFICIAL`, `THIRD_PARTY`, an `INFERENCE` without spans) reads
  `NOT_APPLICABLE`, is shown and exported by default, and keeps its evidence status
  `EXTERNAL_RECHECK`. Nothing fetches the source again; the `recheck_after` hint exists
  only when the finding itself states a date.
- **Recovery is limited to a `HEAD` behind an intact log**: `verify-log --repair-head`
  repairs the crash window between the event append and the `HEAD` replacement; a torn
  write, a changed or removed event, or a truncated log needs a backup
  ([findings-memory.md](findings-memory.md), "Recovery").
- Dependencies are declared by authors; nothing infers them.
- **The stale-review order is a name heuristic.** `txray findings stale` puts parameter
  findings first and then findings whose cited path or component matches the digest's
  scoring-name rule; a scoring finding on a neutrally named path sorts with the rest. Every
  not-current finding is listed either way. Its successor drafts copy the claim unchanged
  (a changed parameter default makes the copied claim false): `supersede` refuses a draft
  until an author removes its `remove_after_reading` member, and the successor is a draft
  that a different reviewer must still review. No draft is offered when an anchor's own
  text changed, for ambiguous, missing or unusable spans, span dependencies or negative
  searches.
- `txray findings verify --pin-cited` pins from the store's single upstream (or
  `--upstream`); a store with pins of several upstreams needs `--upstream`, and a commit
  the upstream no longer has cannot be pinned.
- The reference sets are 35 citation goldens (8 with 14 history expectations) and the
  40-question semantic gate set in `eval/questions.json` (31 answer, 9 abstention items;
  root-reviewed 2026-10-01; neither live run, 2026-10-01 and 2026-10-02, passed), with its
  revision 2 `eval/questions-v2.json` (root-reviewed 2026-10-07: corrected answer patterns and
  one added expected citation; live run 2026-10-07 with contract r1: abstention 7/9, not passed; with contract r2: passed, one run). Its deterministic harness runs in `make ci`
  on both revisions; its live
  harness runs one agent once per release by hand, so the semantic precision, coverage and
  abstention numbers hold for that date, model and client only, with the sample sizes
  given in [release-checklist.md](release-checklist.md). Answer correctness is scored by
  rule (expected regexes and overlapping citations), not by a human rubric.

## Updates and digests

- Hunks and renames come from this tool's matcher; counts can differ from `git diff`.
- Classification is heuristic (names, paths, masked source, symbols); a value read from a
  configuration service at runtime is invisible; the `home-mixer` and `vm-ranker` copies of
  a weight are not asserted to correspond; registration detection sees list literals only.
  Name and path classes (`scoring-logic`, `model-config`) say where code lives, not what it
  does: in a hand check about half of the recent `scoring-logic` items and a quarter over the
  full history were scoring code ([updates.md](updates.md)). A filtering/visibility *name*
  rule was mostly wrong and does not exist; `visibility-rule` is a declared-type content rule
  for the rule DSL only, so other filtering code (hydration, staging, treatment) is `unknown`
  unless another rule applies. Content rules (`observability`, `data-type`,
  `visibility-rule`, `access-modifier`) cover a whole hunk or nothing. About 33 % of the items
  of `77d431a..78460ca` stay `unknown` (classifier v3); whole added files almost always do.
- The main Markdown digest is bounded by row budgets; on a large range it names what it cut
  and the appendix holds the rest. The appendix and the JSON document are not bounded
  (the appendix of `aaa167b..77d431a` is about 1.1 MB).
- Ancestry uses first parents; a commit reached only through a merge appears in the merge
  step.
- The digest's affected-findings section lists findings whose *spans* touch a change; a
  finding that depends on an affected *finding* is not listed (re-anchoring marks it
  `STALE`), and negative searches are re-run only by `txray findings reanchor`. Spans that
  cannot be placed on either commit are counted, not judged.
- TimelineXray schedules nothing and opens no pull requests (P7's "scheduled Project PRs"
  are not implemented); a schedule is not a freshness guarantee
  ([updates.md](updates.md)).

## MCP server

- stdio only; there is no HTTP profile and there are no MCP resources. The P7 tools
  `explain_component`, `get_param`, `param_history` and `diff_since` are not implemented
  ([mcp.md](mcp.md)).
- **Tested clients** (details in the client matrix of [mcp.md](mcp.md)): the test suite's
  own stdio client, and Claude Code 2.1.286 on macOS in print mode with the modern protocol
  and the code tools only. The findings tools have not been run with a real client. Every
  other client, including interactive Claude Code with a project `.mcp.json` and any
  client speaking only 2025-11-25, is **untested**; no compatibility is claimed for them.
- Published output schemas in `tools/list` are relaxations of the strict schemas the server
  enforces (`txray mcp tools --json --strict` prints the strict ones).
- Cursors are valid only for one server process; every call re-reads and re-verifies the
  ledger (no cache); the time budget uses `SIGALRM`, so it applies on the main thread of a
  POSIX system, and a signal cannot interrupt a wait inside SQLite's C library: the index
  runs in WAL mode so readers never wait for a running build, and a reader's busy timeout
  is bounded to 5 s (an index file made before WAL mode, by the released 0.7.0 or earlier,
  is converted by the next `txray index`; until then a call can overrun its budget by at
  most that bound).
- `read_span` measures the JSON-encoded size of its text before answering (the text is
  sent twice on the response line; control characters and quotes cost up to six bytes
  each), so a span within 120 lines and 16 KiB can still be rejected, with the number of
  lines that fit, when it is full of tabs; it is never truncated or `output_too_large`.

## Optional Context Layer export

- Exported notes are snapshots of the ledger at export time; nothing refreshes them. An
  edited exported note blocks the next export until it is moved away; notes are never
  merged ([context-layer.md](context-layer.md)).
- Permalinks are built from the recorded commit, path and lines without contacting GitHub.
- Output-directory checks are path validation, not a sandbox against a concurrent process.
  They compare directories by identity as well as by path, so another spelling of the store
  on a case-insensitive volume (default APFS) is refused too (`tests/test_fsutil.py`; its
  case-folding tests are skipped, as optional, on a case-sensitive disk).
- Tested live with Context Layer 0.4.0 (`init`, `index`, `search`) only. Running both MCP
  servers inside a real agent host, and Context Layer's GitHub reader on the upstream
  documentation, are **untested**.

## Offline analytics

- One export schema (`x-post-v1`). The Turkish header row was transcribed as text and is pinned
  by hash; the **English header names are a reconstruction marked `EXTERNAL_RECHECK`**:
  they have not been checked against a real English export, and every English import
  warns about it. `txray metrics import --dump-header` shows the header mapping without
  reading a data row; the status becomes `SUPPORTED` only through a recorded finding named
  in the table's `confirmed_by` ([analytics.md](analytics.md)).
- The date column format of real exports is not documented; ISO 8601 is the default and any
  other format must be declared.
- The five negative-feedback terms have no export column and are `unknown`, never zero;
  uncertainty (block resampling) is not computed; attribution of `extended` columns is the
  user's statement, not verified.
- Time zones other than `UTC` / `Etc/UTC` need an IANA time zone database (the system's, or
  `pip install tzdata`; typically missing in slim containers and on Windows, which is
  unsupported anyway). Without one, `txray metrics` accepts only UTC and says "no IANA time
  zone database found"; `scripts/check_sqlite.py` prints whether one was found, for
  information only (`tests/test_analytics_import.py::ZoneDatabaseTest`).
- The offline guard is a CPython audit hook: it vetoes what Python code does and is not a
  sandbox against hostile native code. Writers that create files in C without an audited
  `open` (SQLite, the `dbm` C modules) are refused outright rather than checked.

## Platforms, build and CI

- Linux and macOS only (`fcntl` locks, `SIGALRM`); Windows is not supported.
- The installer (`install.sh`, [install.md](install.md)) supports Linux and macOS only and
  refuses other systems (exit 10); there is no Windows installer. Its `venv` method was
  run for real (a local checkout and a built wheel, on macOS with Python 3.12, inside
  `make ci`); its `uv` and `pipx` methods were tested only against stub executables, never
  against the real tools, and the install from GitHub (`git+https://...`, the one-line
  `curl | sh`) needs a pushed `main` and was not run. It cannot make an unsuitable Python
  work: without 3.11+ and SQLite FTS5 with the trigram tokenizer it stops with a message.
  `txray setup` pins the tested commit from the real upstream over https; that fetch was
  only run against a local `file://` clone, and `--latest` only against a fixture.
- Python 3.11, 3.12 and 3.13 are declared and in the CI matrix; the local release walk ran
  on macOS with Python 3.12 and git 2.54. git 2.38 or newer is required, but only 2.54
  was tested and no feature pins 2.38 (FA-021): the newest git feature the tool relies on
  is `GIT_CONFIG_GLOBAL` (2.32; an older git would read the user's global configuration),
  then `fetch --no-write-fetch-head --no-auto-maintenance` (2.29) and `--end-of-options`
  (2.24); `GIT_NO_LAZY_FETCH` is ignored by a git without it. The netguard sets
  `protocol.allow=never` and the one allowed `protocol.<scheme>.allow` itself, so it does
  not depend on the `protocol.file.allow` default that 2.38.1 changed.
- The runtime has no third-party dependencies and no network access except the guarded
  fetch. `make ci` additionally builds the package in an isolated virtual environment that
  downloads the build requirement (`setuptools>=77`) from PyPI; that is a development step,
  not something the tool does.
- The GitHub Actions workflow is checked statically (`scripts/check_workflows.py`), runs
  locally with `make ci`, and **runs on GitHub-hosted runners** (Linux x64 and macOS arm64,
  Python 3.11 to 3.13) on every push and pull request and weekly; its first green run was
  on 2026-10-02. Only these two operating systems and architectures are tested.
  `scripts/ci_status.py` is the release gate; of several runs of one workflow on a commit it
  counts only the latest (FA-017). The runners' Python builds differ: SQLite 3.45.1 to
  3.50.4 at the first runs, and the Ubuntu build is compiled so that GLOB and LIKE never
  match a BLOB (the index casts paths to TEXT for that reason). The workflow's first step
  (`python scripts/check_sqlite.py`, also run by `make pycheck`) checks FTS5 with the
  trigram tokenizer and fails in seconds with the reason if it is missing (FA-023).

## Legal

- The license inventory records license and notice files with keyword hints; hints are not
  license determinations, and this document is not legal advice. TimelineXray does not vendor
  upstream code; the goldens record upstream paths, line numbers, hashes and short anchor
  strings only ([NOTICE](../NOTICE)).
