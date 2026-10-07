# Changelog

All notable changes to this project are recorded here. Versions follow PEP 440.

## [0.11.0] - 2026-10-07 - Readable digests, agent contract r2, semantic gate passed

### Measured

- Fourth live run of the semantic gate (2026-10-07, set revision 2, agent contract r2, haiku,
  USD 1.10): **passed** (precision 30/30, coverage 30/31, abstention 9/9, false abstentions
  1/31, 48/48 intact citations, no errors). One run; earlier runs varied. The abstention
  development set (26 items) scored 26/26 under both contracts; r2 cut abstain turns from 233
  to 81 and the cost from USD 1.37 to USD 0.68 (`eval/dev-run-2026-10-07-contract-r*.txt`).
- Third live run of the semantic gate (2026-10-07, set revision 2, haiku, USD 1.39):
  precision 31/32 = 0.969 and coverage 31/31 pass, abstention 7/9 and one turn-limit error
  fail, so the gate still fails (`eval/live-run-2026-10-07.txt`,
  docs/release-checklist.md, "Third live run"). No item was re-scored.

### Added

- **Answer-or-abstain rules R1-R4** (`docs/agents/README.md`): a request-time value (a
  model's score, probability, prediction, rank or feed position for a viewer, account or
  post; reach; an experiment assignment; the live or production state of a parameter,
  switch, decider or metric) is not in the code, and explaining the mechanism is not an
  answer; a suppression or boost claim needs a span implementing exactly that rule; stop
  searching after two unproductive searches in a row or eight in all (searches that lead to
  a cited span do not count); an abstention states its scope. Stated in the MCP server
  instructions, `AGENTS-snippet.md`, `AGENTS.md`/`CLAUDE.md` and the live harness (contract
  `r2`, the new default; `--contract r1` keeps the 2026-10-07 contract verbatim, and
  `--server-src DIR` runs the server from another `src` tree for before/after runs). The
  `search_code` description says that zero hits is not evidence of absence.
- **MCP tool `stale_worklist`** (`timelinexray/mcp/tools/stale.py`): the stale-review
  worklist of the configured ledger, the same data as `txray findings stale --json`, read
  only through the guarded, chain-verified ledger read of the findings tools (no event, no
  lock file, no draft file). Inputs `target`, `area`, `limit` (1-20, default 5), `cursor`;
  bounded per entry (10 citations, 5 occurrences, 10 dependencies, 20 read commands) and
  per batch step (50 ids); commands carry no `--store`/`--ledger`. Thirteen tools; the
  published output-schema budget went from 2,200 to 2,100 bytes per tool so `tools/list`
  stays under the 40,000-byte test limit (about 39.9 KB; only `manifest_summary` lost
  depth). A `Tool` may set its own `output_budget`.
- **Abstention development set** `eval/dev-abstain.json` (`"purpose": "development"`): 14
  new abstain items (per-request outputs, live configuration, moderation claims) and 12
  answerable controls; never the release gate (the gate files are byte-unchanged and
  pinned by a test). `run_gate.py` checks it (78 / 78); `live_gate.py` reports it with
  per-family results, turns and `abstentions_with_scope`, without a gate verdict. No live
  run was made; the commands and expected costs are in docs/release-checklist.md.
- **`txray findings stale [--target C] [--limit N] [--spec-dir DIR] [--json]`**
  (`timelinexray/findings/stale.py`): a read-only, prioritised re-review list of every
  active finding that is not `CURRENT` at the target (default: the newest pin). Each entry
  shows the old span (commit, path, lines, span SHA-256, anchor), the outcome and the span
  the tool located or line-diff-aligned at the target with its SHA-256 and anchor verdict
  (every occurrence for an ambiguous span, the searched scope for a missing one), and the
  exact commands: `txray show` for both spans, then supersede (with a successor draft),
  verify and review by a different reviewer, or review at the target as it is, or retract.
  Order: parameter findings, then findings on scoring paths (the digest's scoring-name
  rule), then the rest; then the queue's trigger priority (a mechanical order, stated as
  such). Findings whose only problem is an unpinned cited commit, or that have no check at
  the target, are batched into one command each. `--spec-dir` writes successor
  specifications whose citations carry the expected `span_sha256` and whose
  `remove_after_reading` member makes `supersede --file` refuse the draft until an author
  has read the spans and edited the copied claim; refused inside a git working tree. It
  writes no ledger event, approves nothing and moves no evidence. Measured on the 275
  research findings re-anchored on `78460ca` (dogfood of 0.10.0): 67 entries (3 parameter,
  17 scoring, 47 other), 53 drafts possible, one batch step for 18 findings.
- **`txray findings verify --pin-cited [--upstream URL]`**: one command for imported
  citations of commits the store has not pinned. It pins them exactly as `txray pin` (the
  guarded fetch only when the mirror lacks the commit), verifies the affected findings and
  re-anchors them again on every target whose earlier check was made from the unresolved
  citations. Dogfood: 8 commits pinned (none fetched), 18 findings resolved in 2 s;
  freshness at `78460ca` went from CURRENT 190 / STALE 60 / UNVERIFIABLE 25 to CURRENT 192
  / STALE 74 / UNVERIFIABLE 9 (the 9 left are research citations that are unusable at their
  own commit or a file gone at the target). A plain `verify` that leaves unpinned commits
  prints the command.
- **`txray findings reanchor --summary`**: totals, the first ten worklist entries, the
  batch steps and `next txray findings stale` instead of one line per finding (16 lines
  instead of 278 on the dogfood); without it the same summary follows the per-finding
  lines, and `--json` carries it as `stale_review`.
- **Semantic gate set revision 2** (`eval/questions-v2.json`, status *root-reviewed 2026-10-07*):
  a new file with a top-level `revision` record (base revision 1 by file, number and SHA-256;
  thresholds unchanged; one change per item with its fields, basis and reason, and the values
  before and after). It corrects 16 answer items whose expected answers rejected a correct
  reply: Q22 gains the second `initialTweetId.getOrElse` span (line 227) as an expected
  citation and Q05 accepts the prose forms of its conditions (both from the root analysis of
  the second live run); by reading, Q02 and Q04 drop an order their questions do not ask for,
  identifier patterns accept a space or hyphen, three counts accept a thousands separator and
  the negative weights accept an en dash or "negative". No question text, abstain item, probe,
  threshold or existing expected citation changed; `eval/questions.json` (revision 1) and its
  live-run reports are unchanged, and `tests/test_eval_gate.py` pins its SHA-256, fails when
  revision 2 differs from it outside the listed fields, and checks every changed pattern with
  synthetic correct and wrong replies. `run_gate.py` and `live_gate.py` default to the newest
  revision, validate the `revision` record and print and record the revision they used; the
  deterministic harness runs on both revisions in `make ci` (revision 2: 237 / 237 checks).
  No live run was made.

### Changed

- `txray findings stale` prints its suggested commands without `--store`/`--ledger`:
  when the paths are the defaults (or set through `TXRAY_STORE`/`TXRAY_FINDINGS`) they work
  as printed, otherwise one `env  export TXRAY_STORE=... TXRAY_FINDINGS=...` line precedes
  them. `--json` keeps the options as given and adds `shell.export`. The one-line hints of
  `findings reanchor`, `findings verify` and `update --reanchor` leave out a default
  `--store`/`--ledger`.
- **`txray update --reanchor`** prints the first ten new review items in the worklist
  order (then `... and N more`; `update-status.json` lists all of them), a `review` line
  with the exact `txray findings stale` command and an `unpinned` line with `txray findings
  verify --pin-cited` when needed; `ledger_refresh.stale_review` records the summary
  (additive). On `77d431a..78460ca` with the research ledger: 86 new items printed as 11
  lines instead of 86.
- **Import hint**: the report of `txray findings import` names `txray findings verify
  --pin-cited --label LABEL`. It used to print `txray pin C1 C2 ...; then txray findings
  verify --label LABEL`, but `txray pin` takes one commit, so the printed command exited 2.
- `tests/test_repo_hygiene.py` accepts an `Unreleased` section above the first released
  version in this file (the released section must still equal the package version).
- **The digest is two Markdown files, summary first and bounded** (`timelinexray/digest/render.py`).
  `digest-<old>-<new>.md` opens with *At a glance* (files, lines, parameter, registration,
  `scoring-logic` and `unknown` counts with how they were decided, events, affected findings,
  what the file does not list and what was cut) and *What to check next* (exact commands),
  then the statements, parameter defaults, registrations, `scoring-logic` items grouped by
  file, affected findings, the summary by class (with "listed in" and "must not be read
  as"), every other class counted by area, unknown items counted by reason and area, range,
  events, history and health. Citations in the main file are compact (lines and span
  SHA-256; the commit is named once per section); every section has a row budget and says
  what it moved. `digest-<old>-<new>-appendix.md` lists every item the main file only counts,
  by class, area and file, with both citations, the unknown reason or the deciding rule, and
  every cut table row. `txray digest --format md|appendix|json` (with `--out`, `md` writes
  both Markdown files); `txray update` writes all three and prints the item, parameter and
  unknown counts. The JSON document is unchanged in schema and complete; it gains
  `overview.classes` (items, files, lines, areas, `listed_in`, `decided_by`, `reasons`) and
  `classes[].must_not_be_read_as`; `update-status.json` gains `digest_summary`. Measured on
  `77d431a..78460ca`: main digest 461,450 -> 39,991 bytes (appendix 422,982, JSON 2,293,334);
  on `aaa167b..77d431a` the main digest is 68,360 bytes with its cuts named.
- **Change classifier version 2** (`timelinexray/diff/rules.py`, `engine.py`, `analysis.py`),
  every rule with synthetic fixture tests (`tests/test_diff.py`, `ClassifierV2Test`):
  - new class `build-dependency`: build-system and dependency files by name (`BUILD`,
    `*.bazel`, `*.bzl`, `Cargo.toml`, `build.rs`, `pyproject.toml`, `requirements*.txt`,
    `Makefile`, `Dockerfile`, ...), and hunks whose every changed code line is an import,
    `use`, `extern crate`, bodyless `mod` or `package` declaration (Milestone 2 symbols);
  - `model-config` also covers a `train/` directory; `test-only` also covers
    `test_support.*`, `test_helpers.*` and `*_fixtures.*`;
  - `detail.unknown_reason` on every `unknown` item (`no-rule`, `not-parsed` for C, C++,
    CUDA, shell and other languages without symbol extraction, `not-text`, `mode-only`), and
    `detail.matched_by` (the deciding `path` or `symbol` rule and name) on every
    `scoring-logic` and `model-config` item;
  - class descriptions say which rules are name or path heuristics.
  A seeded hand check (seeds 20261007 and 20261008, 42 items the kept rules move out of
  `unknown`, each hunk read at both commits): build files 1/1, import hunks 22/22, `train`
  18/18, test helper names 1/1 correct. A
  filtering/visibility name rule was tried and **rejected** (37 distinct sampled items: 10
  rule or policy logic, 8 hydration inputs, 19 caches, telemetry, wiring or tooling); that code stays
  `unknown`. Measured on `77d431a..78460ca`: `unknown` 610 -> 469 of 1,227 net items (188
  hunks to `build-dependency`, 137 to `model-config`, 1 to `test-only`); the only hunks that
  left another class are import-only hunks (17 from `scoring-logic`, 46 from
  `model-config`) and build manifests such as `Cargo.toml` that were `model-config` by
  extension. On `aaa167b..77d431a`: `unknown` 1,522 -> 1,381.
- **Change classifier version 3: the `scoring-logic` name rule, hand-checked and tightened**
  (`timelinexray/diff/rules.py` `is_scoring_name`, `name_words`; `engine.py`
  `_rule_symbols`; tests `ScoringNameRuleTest`). A seeded hand check of 85 `scoring-logic`
  items (seed 20261007, stratified by path/symbol rule over `77d431a..78460ca` and
  `aaa167b..77d431a`, every hunk read) found version 2 precision of 11/27, 4/13, 5/25 and
  3/20 correct (recent path, recent symbol, full path, full symbol). Version 3 matches whole
  words split at punctuation and camelCase (no `rankall`, `UNSCORED`, `lightweight`),
  excludes names containing PageRank or RankAll, reads only symbols that enclose a changed
  line (a pure insertion no longer takes the name of the declaration at its anchor line) and
  ignores test symbols for production lines. All 21 sampled items that left the class were
  wrong; none correct moved. Kept precision: 11/22, 4/8, 5/16, 3/18. On `77d431a..78460ca`:
  `scoring-logic` 65 -> 53 items (19 hunks to `unknown`, 4 to `model-config`); on
  `aaa167b..77d431a`: 228 -> 176. `findings stale` reads the same rule for its scoring area.
- **Classifier version 3: four content classes** (what the changed lines do; new module
  `timelinexray/diff/content.py`, `analysis.py`, `engine.py`; tests `ContentRulesTest` with
  what each rule must not take, and four new files in the shared class fixture):
  - `access-modifier`: tokens equal once Rust `pub(...)` or Java/Scala `public`/`private`/
    `protected` modifiers are removed (decided before the name rules);
  - `observability`: every changed code line belongs to a statement that only logs, traces
    or records a metric (statements split on masked code; Rust, Python, Java, Scala); only
    call shapes of logging and metrics libraries count, never the words `metric` or `stats`,
    which name ranking data in this upstream;
  - `data-type`: every changed code line lies inside a Rust `struct`/`enum`/`union`
    definition and contains no `=`;
  - `visibility-rule`: every changed code line lies inside a Rust `const`/`static` or
    function whose declared or return type is built only from `Condition`, `Predicate`,
    `Clause`, `RuleClause`.
  The last three apply only when no name rule matched. Seeded hand checks (seed 20261007):
  `observability` 11/11 (all items), `access-modifier` 11/11 (all items), `visibility-rule`
  20/20 (20 of 32), `data-type` 25/26 for a first version and 22/22 after excluding lines with
  `=` (a clap `Args` struct carried command-line defaults in attributes). Measured on
  `77d431a..78460ca`: `unknown` 469 -> 404 of 1,236 items (classifier v2 -> v3, including the
  19 hunks the tightened scoring rule returned); main digest 39,988 -> 38,940 bytes. On
  `aaa167b..77d431a`: `unknown` 1,381 -> 1,422 (whole added files; 49 hunks back from
  `scoring-logic`). New classes are counted by area in the main digest and listed in the
  appendix and JSON with both citations.

## [0.10.0] - 2026-10-02 - One-command install

Two commands from nothing to a cited answer: `install.sh`, then `txray setup`. Also the
portability fixes that the first GitHub CI run needed. The `v0.10.0` tag waits for the
semantic release gate, like `v0.9.0` (the first live run did not pass; see 0.9.0,
"Measured").

### Added

- **`install.sh`**, a one-command installer (POSIX `sh`; `curl -fsSL
  https://raw.githubusercontent.com/solisolsoli/timelinexray/main/install.sh | sh`). It checks
  the machine first (Linux or macOS, Python 3.11+ whose SQLite has FTS5 with the trigram
  tokenizer, git 2.38+), installs with `uv`, `pipx` or a private virtual environment, runs
  `txray --version` to verify and prints the next step. Options: `--method`, `--ref`,
  `--source` (a checkout or a wheel), `--uninstall`, `--dry-run`, `--help`; distinct exit
  codes 0, 2, 10-16. It never edits shell files, never uses `sudo` and sends no telemetry;
  the whole script is wrapped in functions so a truncated download runs nothing.
  `tests/test_install_script.py` (37 tests, stub tools, no network) and `scripts/build_check.py`
  (installs the built wheel through `install.sh` into a venv and uninstalls it) cover it. The
  `uv` and `pipx` methods were tested only with stubs.
- **`txray setup [--commit SHA | --latest] [--no-index] [--upstream URL] [--store DIR] [--json]`**
  (`timelinexray/setup_cli.py`, composed from `pin`, `index` and the guarded fetch; no new
  network or process code): pins the tested commit `77d431a` (fetching only if it is not
  local, through the allowlist), builds the code index, prints a summary, three example
  commands and the agent connection line. A second run fetches and indexes nothing.
  `txray setup --print-mcp-config [claude-code|json]` prints a `claude mcp add` line or an
  `mcpServers` snippet with absolute paths and edits no client configuration.
- **`docs/install.md`** (options, exit codes, alternatives, uninstall, troubleshooting); the
  README quickstart leads with the two commands; `docs/limits.md` lists the installer's limits.

- **Community files**: the three issue templates are GitHub issue forms
  (`bug_report.yml`, `wrong_citation.yml`, `feature_request.yml`; labels `bug`, `evidence`,
  `enhancement`; required fields), blank issues are off and the contact links are the private
  security advisory and Discussions; `CODE_OF_CONDUCT.md` is the Contributor Covenant 2.1 with
  GitHub's private reporting as the enforcement contact (no e-mail address); `pyproject.toml`
  has `Source` and `Security` project URLs.
- **`scripts/build_check.py`** installs a copy of the checkout with `install.sh --method venv
  --source <copy>` (with an upstream clone available), runs the linked `txray setup --commit
  77d431a... --no-index --json` and `txray setup --print-mcp-config json` (its `command` must
  be the linked txray), uninstalls again, and checks `python -m timelinexray --version` in the
  wheel venv.

### Changed

- **CI workflow**: no upstream-clone cache (the clone takes a few seconds; the shared cache key
  raced with "Unable to reserve cache", and a cache hit skipped the integrity checks of
  `fetch_test_upstream.py`), `timeout-minutes: 20`, and a weekly `schedule` trigger
  (`17 5 * * 1`) that catches runner, Python, SQLite and git drift.
  `scripts/check_workflows.py` allows `schedule` and still refuses `pull_request_target`,
  `workflow_run` and every other event. The preflight `scripts/check_sqlite.py` also prints
  `sys.version` and `git --version` after its first line.
- **Lint gate**: `make lint` (and so `make ci`) also runs `scripts/check_lint.py` (stdlib
  `ast` only), which reports unused imports in `src`, `tests` and `scripts` and unused
  top-level private names; tests in `tests/test_ci.py`, documented in `CONTRIBUTING.md`.
  Unused imports it found are removed: `TxrayError` (`params.py`), `SPAN` (`diff/engine.py`),
  `code_citations` (`export/notes.py`), `Path` (`tests/test_fixture_repo.py`), `META`
  (`tests/test_mcp.py`) and an unused local in `tests/test_findings_freshness.py`.
- **Parallel, template-based test runner**: `scripts/run_tests.py --jobs N|auto` (default
  `$TXRAY_TEST_JOBS`, else 1; `auto` is `min(cpu count, 4)`) runs the test modules in N
  worker processes, longest first (`scripts/test_timing_hints.json`), and prints one combined
  summary in the serial format with the same strict skip rule and exit codes. `make test` and
  `make test-strict` (so `make ci`) use `JOBS ?= auto`; the workflow still only runs
  `make ci`. `tests/templates.py` builds the expensive fixtures once per run (under a file
  lock, shared by the workers) and copies them: the upstream store (77d431a pinned and
  indexed, 4c5cfe8 and a707cc2 pinned, all three indexed) for `DeterministicGateUpstreamTest`,
  `UpstreamParamsTest` and `UpstreamStdioTest`, and the deterministic synthetic
  `HistoryRepo`, `TwoCommitRepo` and `IndexedStore` stores; `UpstreamIndexTest` still builds
  its own. The same 759 tests and assertions run (8 new tests for the runner and the
  templates). Development machine, not promised: the full suite with the upstream clone took
  a median of 286 s serial before and 267 s after, and 123 s before and 106 s with
  `--jobs auto` after (4 workers, shared machine, 2 to 3 runs each).
- **CLI help**: every `findings` subcommand flag and positional has a help text, `manifest`,
  `show` and each `findings` subcommand have a description, and the epilog of `txray --help`
  lists exit code 130 (interrupted). `schemas/cli-envelope.schema.json` lists the real error
  codes (`integrity_error`, not `integrity`). `tests/test_cli.py::HelpCompletenessTest`.
- **Internal clean-up, no behaviour change**: the MCP findings tools use
  `findings.freshness.cited_commits` instead of a copy; `index/query.py` `symbols` and
  `calls` share one prologue helper; a reused local in `diff/analysis.py` no longer changes
  type. `tests/test_findings_import.py` pins `research.IMPORT_SCHEMA_ID` to the schema `$id`.
- **Faster masking, same output**: `syntax/lexical.py` guards each language's token pattern
  with a lookahead of the characters a token can start with (Rust `/"'rbc`, Scala and Java
  `/"'`, Python `#"'rRbBuUfF`), so `mask()` skips positions that cannot start a comment or
  literal. Masking all 1,839 Rust, Scala, Java and Python files of `77d431a` went from about
  1.71 s to 0.29 s of CPU (development machine, not promised); a fresh index of that commit
  went from 7.4 s to 6.6 s. `tests/test_perf_equivalence.py` compares the guarded and the
  plain pattern on tricky snippets, random texts and (with `TXRAY_TEST_UPSTREAM`) every
  upstream file.
- **Cheaper registration-list pre-check, same output**: `BlobAnalysis.lists` first tests for the
  literal words of `_COMPONENT_WORD` with substring searches (`_may_name_component`, a
  necessary condition: capitalised suffixes verbatim, plurals on the lower-cased text; text
  with non-ASCII characters skips it, because the regex folds a few of them to ASCII
  letters) before the backtracking regex. Over the 1,839 source files of `77d431a` the check
  went from about 1.17 s to 0.52 s of CPU (development machine, not promised);
  `txray diff 4c5cfe8 a707cc2` from about 1.10 s to 0.93 s and `txray digest 4c5cfe8 77d431a`
  from about 2.40 s to 1.94 s (medians of three, noisy). A test proves the pre-check is True
  whenever the regex matches, on curated, generated and (with `TXRAY_TEST_UPSTREAM`) all
  upstream texts.
- **One index connection per diff, digest or parameter command, same output**: the per-file
  symbol lookups of `diff/symbols.py` and `params.py` no longer call `CodeIndex.symbols`
  (a new read-only connection, two pin lookups, a generation check and a GLOB scan of the
  commit's files for every file). `CodeIndex.read_session()` returns a `ReadSession` that
  keeps one read-only connection (a single read snapshot in WAL mode), resolves each pin and
  checks each commit's generation once, and selects the file's symbols by exact equality on
  the stored path bytes (so non-UTF-8 paths work), with the same refusals as before. Records
  and order equal `CodeIndex.symbols(path_glob=glob_literal(path))` (tested for every path
  of a fixture including a non-UTF-8 and a glob-character name, for the integrity error, and
  for a build that commits while a session is open). Measured with before and after run
  interleaved on a busy machine, CPU time, medians of five (development machine, not
  promised): `txray diff 4c5cfe8 a707cc2` 0.85 s to 0.72 s, `txray diff a707cc2 77d431a` 1.52 s
  to 1.32 s, `txray digest 4c5cfe8 77d431a` 3.43 s to 3.06 s, `txray param ClickWeight`
  0.32 s to 0.29 s. `glob_literal` now lives in `timelinexray.index`.
- **Lazy command imports, same output**: `cli.build_parser(argv)` imports and registers only the
  command module that the first word of `argv` names (`index_cli` for `index`, `search` and
  `symbols`, and so on; `pin`, `manifest` and `show` live in `cli.py`; a leading `--version`
  needs none). No command, `--help`, an unknown or misspelled command and any other first
  word build the full parser, so the top-level help, every usage error and every `--json`
  error envelope stay as before: `tests/test_perf_equivalence.py` compares (exit code,
  stdout, stderr) of about 190 argument lists, with and without `--json`, against the eager
  parser, and a before and after run of 104 argument lists through `python -m timelinexray`
  gave identical exit codes and stdout and stderr hashes. CPU time per process, median of
  nine, with the modules loaded in brackets (development machine, not promised):
  `txray --version` 98 ms to 57 ms (52 to 13 `timelinexray` modules), `txray manifest
  77d431a --summary` 107 ms to 66 ms, `txray search 77d431a ClickWeight` 138 ms to 111 ms,
  `txray param ClickWeight --commit 77d431a` 215 ms to 186 ms.

### Fixed

- **A valid upstream path with a control character crashed `diff`, `digest` and `param`**:
  the exact-path symbol lookup (`index.query.file_symbols`) still ran the GLOB pattern
  checks, which refuse control characters and long patterns. Exact byte equality needs no
  pattern validation; the lookup now finds such a file, while `--path` GLOB patterns are
  still validated.
- **New store, ledger and digest files could be writable by others**: `atomic_write`
  created new files at `0o666` under the umask, so a permissive umask such as `0o002` made
  them group-writable. New files are now `0o644` under the umask (a replaced file still
  keeps its mode; analytics datasets stay `0o600`).
- Test infrastructure: a shared fixture template records a fingerprint of the code and is
  rebuilt when it was built by different code; a time-budget test has a wider margin for
  loaded runners.

- **`txray metrics` could not use even `UTC` without a time zone database, and blamed the name**
  (LOW): on a machine with neither the system `tzdata` nor the `tzdata` package (slim
  containers), `load_zone("UTC")` failed with "unknown IANA time zone". `UTC` and `Etc/UTC` now
  are `datetime.timezone.utc` (identical results, no database needed), and any other name says
  "no IANA time zone database found (install the tzdata package)" when the database is
  missing, "unknown IANA time zone" only when it is not. `scripts/check_sqlite.py` prints a
  `time zone database:` line (information only, never a failure); `docs/limits.md`.
  `tests/test_analytics_import.py::ZoneDatabaseTest`, `tests/test_ci.py`.
- **`fsutil.atomic_write` and the ledger's `HEAD` made every file `0600` and dropped the mode
  of the file they replaced** (LOW): both used `tempfile.mkstemp`, so a digest, a manifest,
  an exported note or the ledger `HEAD` (next to a `0644` log) lost its group or world
  read bit, or an executable bit, on every rewrite. They now keep the replaced regular
  file's permission bits and give a new file `0666` under the umask; the ledger writes `HEAD`
  with `atomic_write`. Analytics datasets stay private on purpose (`0600` files in a `0700`
  directory, written by `analytics/dataset.py` itself, now stated in its docstring and
  pinned for any umask). `tests/test_fsutil.py::AtomicWriteModeTest`,
  `tests/test_findings_ledger.py`, `tests/test_analytics_import.py`.
- **The line-diff proposal of a changed span counted lines with `str.splitlines`** (LOW): a form
  feed, vertical tab, NEL or U+2028 in a source file shifted the hunk header numbers
  (`@@ -4,2 +4,2 @@` for a change at lines 3-4), contradicting the LF-only line rule of every
  citation. `verify._unified` splits with the same line map as the rest of the verifier.
  `tests/test_findings_reanchor.py::UnifiedDiffLinesTest`.
- **Human-readable output crashed under a non-UTF-8 stdout encoding** (LOW): with
  `PYTHONIOENCODING=ascii` (or a C locale on an old Python) any name such as `naïve.txt`
  raised `UnicodeEncodeError`. `cli.main()` now reconfigures stdout and stderr (the text
  streams that support it) with `errors="backslashreplace"`, so such characters print as
  `\xef`; `--raw` writes bytes to the buffer and `--json` is ASCII, both unchanged.
  `tests/test_cli.py::LegacyEncodingTest`.
- **`install.sh` with relative or unusual settings** (three small fixes, LOW to MEDIUM): a relative
  `TXRAY_HOME` or `TXRAY_BIN_DIR` produced a dangling `txray` symbolic link (its target was read
  relative to the link's directory, exit 16); both are now made absolute. A `TXRAY_PYTHON` path
  with spaces was split into words and not found (exit 11); the candidates are now
  positional parameters, never word-split. `HOME` is required only when `TXRAY_HOME` or
  `TXRAY_BIN_DIR` is unset (and for the default bin directory of `uv` or `pipx` if the tool
  cannot report it). `tests/test_install_script.py`; still POSIX `sh` (checked with dash).
- **`txray setup --latest` created a second mirror for a `file://` URL through a symbolic
  link** (MEDIUM): `setup_cli.py` discarded the canonical URL that `Allowlist.check` returns
  and fetched with the URL as typed, so a spelling through a link (macOS `/var` ->
  `/private/var`) and its canonical form had different mirrors. It now uses the canonical
  URL, as `txray update` does. `tests/test_setup.py::SetupTest`.
- **`--out` inside the store was not refused under another spelling on a case-insensitive
  volume** (MEDIUM): the "inside the store, ledger or package" and "contains the store or
  ledger" checks (`fsutil.check_output_directory`, `export/writer.py`) compared paths as text,
  so on default APFS `--out .../store/out` with the store at `.../Store` was accepted. They now
  also compare directories with `os.path.samestat` (`fsutil.is_within`).
  `tests/test_fsutil.py` (the case-folding tests are skipped, as optional, on a
  case-sensitive disk; a symbolic-link spelling test runs everywhere).
- **`txray update` reported "no change" for a change it had never reported** (HIGH, wrong
  evidence): without an accepted commit (a store pinned by `txray pin` or `setup`), the previous
  commit was the newest pin, but the head is pinned before its digest is built, so after a
  crash in between the retry found the head as the newest pin and compared it with itself. The
  fallback now ignores a pin of the head itself; the accepted commit was already recorded only
  after the digest is written. `tests/test_update.py::UpdateFlowTest`.
- **A non-UTF-8 upstream path name crashed every path-filtered query** (HIGH): git bytes are
  decoded with `surrogateescape`, and a lone surrogate cannot be bound to SQLite, so with an
  indexed commit that contains a path such as `caf\xe9.py`, `txray diff`, `txray digest`,
  `txray param-history`, `txray search --path`, `symbols --path`, `calls --path` and the
  coverage of a recorded negative search failed with `UnicodeEncodeError` (exit 1). Path
  patterns are now bound as bytes (`surrogateescape`) and compared as
  `CAST(f.path AS TEXT) GLOB CAST(? AS TEXT)` (also on SQLite built with
  `LIKE_DOESNT_MATCH_BLOBS`); a lone surrogate in a name, a search term or a path pattern
  that cannot be a path is `InvalidInput` (exit 2, MCP `invalid_input`), never an internal
  error; digest Markdown and human output show such a name as `\xe9` (`textsafe.visible`).
  `tests/test_nonutf8_paths.py`.
- **`install.sh --uninstall` swallowed a failing `uv tool uninstall` or `pipx uninstall`** and
  reported "nothing to uninstall" (exit 0). It now names the failing tool and exits 15 after
  trying the other methods, and it detects an installed package by the exact name
  `timelinexray`, not by prefix (`timelinexray-foo`). A test pins the installer's fallback
  commit to `setup_cli.TESTED_COMMIT`.

- **`scripts/fetch_test_upstream.py` fetched into the branch `HEAD` names**: `git init`
  leaves `HEAD` on the unborn `main`, and git refuses to fetch into it without
  `--update-head-ok`, so every job of the first GitHub CI run failed before the tests (the
  local runs reused an existing clone). The fetch passes the flag, a failing git command is
  reported with its message, and `tests/test_ci.py` fetches into a fresh directory from a
  local fixture.
- **Path filters matched nothing on SQLite built with `LIKE_DOESNT_MATCH_BLOBS`** (the Python
  builds on GitHub's Ubuntu runners): paths are stored as BLOBs and `GLOB` is false for a BLOB
  operand there, so `txray search --path`, `symbols --path` and `calls --path` returned no
  rows. The three queries now use `CAST(f.path AS TEXT) GLOB ?` (the same bytes are
  compared); `tests/test_index.py::BlobPathGlobTest` emulates such a build.
- **Two portability fixes in the checks**: the pipefail test (`tests/test_cli.py`) runs with
  `bash` because `sh` is `dash` on Ubuntu and rejects `set -o pipefail`, and
  `scripts/build_check.py` compares resolved paths (on macOS the temporary directory is
  reached through the `/var` -> `/private/var` link).

### Removed

- Dead code with no reference in the package, tests, scripts or docs:
  `analytics.checks.field_names`, `diff.model.Citation.has_span`,
  `findings.state.FindingState.needs_current_check`, `mcp.server.Server.run_tool` and
  `syntax.lexical.Lines.start_of`. (`syntax.CALL_FORMS` and `sorted_kinds` stay: they are
  public through `__all__`.)

### Measured (development machine, macOS, Python 3.12, git 2.54; not promised)

- Semantic release gate, second live run (2026-10-02, after the documented corrections:
  pipeline named in Q01-Q04, the contract's abstention rules in the harness prompt, turn
  limit 25): precision 29/31 = 0.935 (threshold 0.95: **fail**), coverage 29/31, abstention
  9/9, false abstention 0/31, citation integrity 51/51, 40/40 run without errors, USD 1.40
  (`eval/live-run-2026-10-02.txt`). The two misses are question-set defects found at the
  source (Q22 expects one of two identical spans; Q05 requires literal identifiers); they
  were not re-scored. `v0.10.0` stays untagged.

- `TXRAY_TEST_UPSTREAM=<clone> make ci`: exit 0. Hygiene 19 tests OK; the strict suite ran
  704 tests, OK, 1 skipped (the optional live Context Layer test, the only skip `make ci`
  allows; 654 in 0.9.0); eval gate 49 / 49 anchors; `build check passed: timelinexray 0.10.0`
  (including the real `install.sh --method venv` install of the built wheel and its
  uninstall). `txray setup` on the local upstream clone: 8.2 s first run (2,147 paths,
  50,387 symbols), 0.2 s second run.
- Not run: `make ci` on GitHub (the `ci` workflow has not run there yet), the `uv` and `pipx`
  install methods against the real tools, `curl | sh` from GitHub, Linux and Python 3.11 or
  3.13 for the installer, and the semantic release gate (so no `v0.10.0` tag).

## [0.9.0] - 2026-10-01 - Audit fixes, parameter tools and the semantic release gate

Fixes and additions from an end-to-end audit of 0.7.0 (25 findings: 0 P0, 3 P1, 7 P2,
15 P3), on top of the 0.7.0 section below. Published on GitHub on 2026-10-02 together with a
README for first-time readers, `CITATION.cff`, issue and pull request templates and
project URLs (no code change). The first live run of the semantic release gate did not
pass (see "Measured"); the `v0.9.0` tag waits for a passing run.

### Changed

- **Freshness is read relative to the newest pin** (`timelinexray/findings/freshness.py`,
  one rule for `txray findings list/show`, the MCP findings tools and the Context Layer
  export): without a commit of the caller's choice, a finding's reading names the newest
  pinned commit it was re-anchored on (by committer time), else its cited commit, and lists
  the `newer_pins` of the same upstream that were never checked; a finding with unchecked
  newer pins is not current (`current: false`, reason `NEWER_PIN_UNCHECKED`, a warning
  naming the newest unchecked pin; the export leaves it out or labels it `NOT CURRENT`).
  Before, `current` was relative to the finding's own latest check, so after `txray update`
  pinned a new head every finding stayed "current" at the old pin while the store already
  knew the commit where its evidence had changed. `txray findings reanchor --latest`
  re-anchors on the newest pin of the store's upstream; `txray findings list --current`
  shows what the MCP tools and the export list; `txray update` prints the re-anchor
  command after pinning a new head. With `--target`/`commit` the reading is the recorded
  check at that commit (a historical answer).
- **Evidence without code spans is first-class**: a finding with external evidence only
  (web sources; no code citation, dependency or negative search) reads `NOT_APPLICABLE`
  (derived, never written to the ledger; the four ledger values are unchanged) instead of
  staying `NOT_CHECKED` forever. Such findings are current by default in `find_findings`,
  the export (an "External evidence" paragraph and index section, `checkable: false`) and
  `list --current`, carry their latest recorded `retrieved` date and a `recheck_after` hint
  when their recorded text names a date ("recheck after", "effective", "valid until",
  "expires", "in force from"), and keep `EXTERNAL_RECHECK` as their evidence status.
  Before, `OFFICIAL`/`THIRD_PARTY` findings never appeared in the default views or the
  default export and were labelled "not current" when included.
- **Imported citations are resolved when their commit is pinned later**: `txray findings
  import` reports the distinct unpinned commits (`unpinned_commits`, `hint`; `unpinned` and
  `next` lines) with the command that resolves them; `txray findings verify [--label L]`
  then records the observed span (hashes, blob, byte range) as a provenance revision at the
  cited commits, which `show` (`resolved by a later verify`), the MCP tools (hash,
  permalink, `verify_claim` against the recorded hashes), the export and the digest use;
  re-anchoring checks made from the unresolved citation no longer count and `verify` lists
  them as `recheck_targets`. The immutable record keeps the reported form.
- From the previous commit on this branch: the code index uses WAL journal mode and readers
  never wait for a running build (the MCP time budget holds); `txray findings verify-log
  --repair-head` rewrites `HEAD` after an interrupted write; a review closes only the
  queue scopes it assessed; replay re-enforces the review merge condition.
- **"public default" footers in `txray search`, `txray symbols` and `txray show`**
  (FA-013): whenever the shown snippets, signatures or span contain a digit, the human
  output ends with (for `show`: the header carries) `note  numbers in the output above are
  public defaults at commit <commit>, not production values`, and `--json` has a `note`
  member (`null` when no number is shown). `tests/test_index.py::PublicDefaultFooterTest`.
- **`read_span` fits the response line or says what fits** (FA-010): the tool measures the
  JSON-encoded size of its text (sent twice on the line; a tab costs 5 bytes, a quote 6)
  before answering and rejects a span that would exceed the 64 KiB response line as
  `invalid_input` naming the largest line count from `start_line` that fits, instead of
  the later `output_too_large` with no guidance. `ToolContext.max_response_bytes` carries
  the server's limit; the tool description and `tests/mcp_tools_list.json` follow.
  `tests/test_mcp.py::ReadSpanFitTest`.
- **Index lookups by exact name use the `symbols_by_name` index** (`index/query.py`): the
  `name = ? OR qualname = ?` clause made SQLite scan every symbol of the commit (0.6 s per
  lookup on the upstream index); the clause now also restricts `name` to the suffixes of
  the requested name, which an exact qualified-name match implies (1 ms).
  `tests/test_index.py` checks the statement. `gitio` allows the read-only `rev-list`
  subcommand.
- **Published `tools/list` schemas** use a 2,200-byte budget per tool (was 2,600) so the
  twelve-tool listing stays at about 38.2 KB (test limit 40,000 bytes);
  `tests/mcp_tools_list.json` regenerated.

- **Agent contract sharpened** (FA-024, audit section 6 item 10): `AGENTS.md` =
  `CLAUDE.md` and `docs/agents/AGENTS-snippet.md` carry the same four sentences: a finding
  is current only when `current` is `true` and `txray findings reanchor --latest` (or
  `verify_claim` with `target_commit`, which writes nothing) comes first when
  `freshness.newer_pins` is not empty; public defaults are not production values ("public
  default" and the commit next to every number); a digest item is a mechanical
  classification, not a finding; `verify_claim` checks span integrity only.
  `tests/test_repo_hygiene.py::AgentContractTest` greps them.

### Fixed

- Digest affected findings (FA-004): a file that moved with byte-identical content, or
  changed only its mode, is never reported as "changed lines". A finding cited in a moved
  file is listed under `relocation_candidates` ("moved to ... with identical bytes; cited
  lines A-B are intact"), which agrees with `txray findings reanchor` (`relocated`,
  `CURRENT`); a mode-only change lists nothing. `ChangedRegion` carries
  `content_unchanged`; the Markdown digest prints the candidates after the affected table.
- Digest affected findings (FA-009): the changed regions are indexed by side and path, and
  a span whose path exists at a commit untouched by any region is skipped there (while a
  path exists, re-anchoring looks nowhere else); a path absent at a commit is still placed.
  One placement serves every finding citing the same span, the blobs of the spans to place
  are read in one git process per commit (`SnapshotStore.read_blobs`, `Verifier.prefetch`),
  and the digest asks for no line-diff proposal (`Verifier.relocate(..., propose=False)`).
  `coverage.skipped` counts the spans not placed; `unplaced_spans` shows `untouched` for a
  side that was not tried. Measured on `4c5cfe8..a707cc2` with a 478-finding ledger:
  7.7 s -> 1.9 s (1.7 s without a ledger), identical affected rows.
- Digest destinations (FA-022): `txray digest --out` writes atomically (`atomic_write`; a
  symbolic link at a digest's name is replaced, never followed), and both `digest --out`
  and `update --out` are checked before anything is built, fetched or written, with the
  rules of `txray export` (`timelinexray.fsutil.check_output_directory`, now shared with
  the export writer): a symbolic link, an existing non-directory, a missing parent, or a
  location inside the TimelineXray working tree or package, the snapshot store, the
  findings ledger or an analytics dataset is refused (exit 1).
- Packaging (FA-016): the sdist ships no `tests/` (`MANIFEST.in` prunes `tests/`,
  `scripts/` and `.github/`); the suite reads the checkout (goldens, schemas, docs, git)
  and a partial copy could never run. `scripts/build_check.py` fails when a `tests/` entry
  is in the sdist.
- CLI (FA-012, audit section 6, item 13): every `txray ... --json` failure is one error
  envelope, published as `schemas/cli-envelope.schema.json`: `outcome: "error"`,
  `error: {code, message}`, `data: null`, `warnings: []`, so a consumer reads the same
  members on every outcome. Usage errors with `--json` on the command line are the same
  envelope (code `usage`, exit 2, the command named even when the top-level parser reports
  unrecognised arguments), an unexpected exception is `internal_error` (exit 1, traceback
  on stderr) and an interrupt `interrupted` (exit 130). The documented exception is
  `txray mcp tools --json`, which prints the MCP `tools/list` definitions. A test runs every
  leaf command with `--json` and validates the output against the schema.
- CLI (FA-011): a reader that closes the pipe (`txray manifest ... | head -1`) ends the
  command silently with exit 0; stdout is flushed inside the handler so a small output
  that reaches the pipe only at exit is handled the same way, and the interpreter's final
  flush goes to the null device (no `Exception ignored ... BrokenPipeError`, no exit 120).
- Offline guard (FA-019): `sqlite3.connect` and the import of the `dbm` C modules
  (`_dbm`, `_gdbm`) are refused in a metrics process. Both create files in C without an
  audited `open`, so the write check could not see them (a database, journal or
  `ATTACH`-ed file, or an `ndbm` file, outside the dataset directory); the analytics code
  uses neither. `tests/test_analytics_offline.py` shows the files that were created before.
- Re-anchoring (FA-025): a span whose file is gone and whose bytes were not found is
  reported as searched in "the lexically indexed files of the target (N of M files; K not
  lexically indexed: binary 1, ..., not searched)" instead of "the code index of the
  target", which overstated the coverage; `search.lexical_coverage` has the counts.
- `scripts/run_tests.py` (FA-015) puts the checkout's `src` on `sys.path`, so the
  documented `python scripts/run_tests.py` works without `PYTHONPATH=src` (discovery
  failed at `tests/__init__.py` before).
- `scripts/ci_status.py` (FA-017): of several workflow runs of one workflow on the commit
  only the latest counts (per kind of tested tree: the commit itself, or a pull-request
  merge); older runs and the check runs of their suites are listed as `superseded`. A
  failed run followed by a successful `workflow_dispatch` on the same commit no longer
  blocks the gate forever; a later failure is never hidden by an earlier success.
- Docs (FA-014, FA-021, audit section 6, item 14): `goldens/README.md` lists the anchor
  verdicts `FOUND`, `FOUND_MULTIPLE`, `MISSING` (no longer the pre-0.6.0 name);
  `docs/limits.md`, `docs/findings-memory.md` and `findings/state.py` no longer name an
  unreleased "0.9.0" (the WAL conversion and the review-scope rule refer to the released
  0.7.0); `docs/limits.md` states what is fixed and what is not for FA-001 (`current` is
  relative to the newest pin; nothing re-anchors by itself), FA-003 (external evidence is
  `NOT_APPLICABLE`, never re-fetched), ledger recovery (only a `HEAD` behind an intact
  log), FA-017 and FA-023, and why git 2.38 is the stated floor although only 2.54 was
  tested (the newest feature relied on is `GIT_CONFIG_GLOBAL`, 2.32). Two hygiene tests
  keep the goldens README and every version named in the docs and the package in step.

### Added

- Tests: `tests/test_findings_freshness.py` (a history with distinct committer dates: a
  newer unchecked pin makes a finding not current in every consumer, `reanchor --latest`,
  external-only evidence listed, exported and marked current with its dates), the import
  test of a citation resolved after pinning (CLI report, `verify --label`, `show`,
  `verify_claim` and `get_finding` agreeing) and the re-anchoring test of checks retired by
  a later resolution. Test fixtures can set a commit's committer date.
- `docs/findings-memory.md` ("Current"), `docs/mcp.md`, `docs/context-layer.md`, the agent
  snippet and `AGENTS.md` describe the rule; `tests/mcp_tools_list.json` regenerated.
- `txray update --reanchor [--export DIR]` (AUDIT section 6, item 1): one command that
  keeps the ledger fresh. After the guarded fetch and the pin of the new head, every active
  finding is re-anchored on it (`verify` events, as `txray findings reanchor <head>`) before
  the digest is built, so affected findings show their freshness at the head; on a head
  that was already pinned (no change, or a quarantined head observed again), only findings
  without a check at it are re-anchored (nothing is appended to a fresh ledger). `--export DIR` then refreshes the Context Layer notes. The review
  queue is compared before and after: `update-status.json` records `ledger_refresh`
  (target, selection, freshness counts, `review_queue.added`/`removed`, ledger head) and
  `export` (counts only); the command prints `reanchor`, `queue` and `export` lines and
  exits 1 when new review items appeared. A missing or damaged ledger or a refused export
  directory stops the command before anything is fetched. Documented as the cron entry in
  `docs/updates.md`. Module: `timelinexray.digest.refresh`.
- **Parameter tools** (audit section 6, item 4; the P7 `get_param` / `param_history`
  tools deferred in 0.7.0): `timelinexray.params.ParamResolver` finds every `const`,
  `static`, `field` or `param!` declaration of an exact name through the Milestone 2 index
  and reads its literal public default, declared type and flag string with the Milestone
  5b value extraction (`diff.analysis`), so values agree with `txray diff`. CLI
  `txray param <name> [--commit C]` (default: the newest pin) and `txray param-history
  <name> [--base C] [--head C]`: the value at every pinned commit on the first-parent chain
  (default: the oldest to the newest pin; one `git rev-list`, nothing fetched, commit
  messages never read), every change (`declared`, `value-changed`, `removed`) with the
  citation on both sides, reversions, gaps where commits between pins are unpinned, and
  pins not on the line; a pinned commit that is not indexed is checked only at the paths
  indexed commits declare the name and the output says so. MCP `get_param` (declarations
  with "public default" `value_note`s and citations, plus up to 10 lexical mentions) and
  `param_history` (per-declaration timelines; the envelope's `commit` is `null`); both
  listed between `index_coverage` and `find_findings`. Measured on the local upstream with
  39 pinned and indexed commits: `get_param ClickWeight` at 77d431a 0.03-0.07 s,
  `param_history ClickWeight` 0.3-0.8 s (`tests/test_params_upstream.py` prints its own
  numbers).
- `tests/test_params.py`, `tests/test_params_upstream.py` (acceptance: both ClickWeight
  declarations at 77d431a; 0.4 -> 0.3 at a707cc2 with both citations), `get_param` /
  `param_history` cases in `tests/test_mcp.py` and `tests/test_mcp_stdio.py`.
- CI preflight (FA-023): `scripts/check_sqlite.py` checks that the interpreter's SQLite has
  FTS5 with the trigram tokenizer (the code index's own probe) and names the SQLite
  version and the reason when it does not. The workflow runs it right after setting up
  Python, before the upstream clone, and `make pycheck` (the first prerequisite of every
  target, `make ci` included) runs it locally; `scripts/check_workflows.py` requires the
  step before `make ci` in every job.
- Tests (audit section 6, item 12): a seeded property test of `LineMap` and
  `find_occurrences` against the independent regular-expression oracle (400 random blobs
  of terminators, lone CRs, NULs, invalid UTF-8 and BOMs); the ledger's crash window
  through the real write path (a writer process killed between the synced append and the
  `HEAD` replacement: only `head_mismatch`, every read and write refused, `repair_head`,
  then the chain continues); every source file parsed with `feature_version=(3, 11)` plus a
  PEP 701 f-string check that `feature_version` does not perform (a reused enclosing
  quote, a backslash, a comment or a line break inside a replacement field).

- `txray metrics import EXPORT --schema S --lang L --dump-header [--json]` (audit section
  6, item 11): prints the header row as exported, each name normalised, the field it maps
  to, the unmapped names, the missing fields and how many columns each language's table
  would map; the file is opened read-only and read line by line as bytes, only the
  first CSV record is decoded (an unterminated quote or a non-UTF-8 byte in a data row is
  not even noticed), nothing is written (the offline guard allows no write), and no count or id is
  shown, so a user can confirm an alias table without sharing data. `--out` and
  `--captured-at` stay required for an import (exit 2 names them) and are not needed for
  the dump. `HeaderAliases.confirmed_by` names what establishes a table's status (a
  recorded finding id, or the hash pin of the transcribed Turkish row); the English table
  stays `EXTERNAL_RECHECK` and `tests/test_analytics_import.py` refuses a `SUPPORTED`
  table without a finding id or hash pin, so the status changes only through a recorded
  finding (`OFFICIAL` or `EMPIRICAL`), never by editing the table alone.

- `scripts/check_release_history.py` (FA-008, FA-020; audit section 6, item 9): the
  publishing gate for a git history. For every commit reachable from a ref or range it
  requires `+0000` author and committer offsets, the project identity, no trailer
  (`Co-Authored-By`, `Signed-off-by`, a trailing `Token: value` block) and no personal
  data in the message (local absolute paths, e-mail addresses outside the noreply and
  example domains, secret-looking strings, plus the regular expressions of
  `--patterns FILE` kept outside the repository); `--fsck` also refuses dangling objects
  and prints the pruning commands without running them. `--json` for scripts; exit 0
  clean, 1 problems, 2 usage or git failure. Tested on synthetic repositories
  (`tests/test_release_history.py`), and the repository's own history is checked for
  identity, trailers and messages on every run. The history itself is not rewritten
  here: the release branch is built in UTC by the maintainer.
- **Semantic release gate** (audit section 6, item 5; P7 section 7.1): `eval/questions.json`
  is a 40-item question set (31 answer items derived only from root-checked research
  findings, each with expected commit-pinned citations carrying an oracle span SHA-256, an
  anchor and the tool lookup that must find them; 9 abstention items whose answers are not
  in the public code, each with `search_code` probes that must return no hits); status
  "root-reviewed 2026-10-01" (23 numeric answer patterns were tightened before any
  live result existed), no claim text. `eval/run_gate.py` checks every expected citation
  through the MCP server without a model (`read_span` span, hash, anchor and value;
  `get_param` / `find_symbols` lookups) and every abstention probe, and reports each
  denominator; `tests/test_eval_gate.py::DeterministicGateUpstreamTest` runs it inside
  `make ci` on a temporary store built from the local upstream clone. `eval/live_gate.py`
  runs the set through Claude Code (`claude -p --strict-mcp-config`, txray MCP tools only,
  structured output) under a hard total budget and scores by rule: precision, coverage,
  abstention accuracy, false-abstention rate and citation integrity, each with its
  denominator, plus the total cost. The proposed gate thresholds and the measured numbers
  are in `docs/release-checklist.md` (root-reviewed 2026-10-01); `eval/README.md`
  documents both harnesses.

### Measured

- **Semantic release gate, first live run (2026-10-01): failed.** Claude Code 2.1.286 with
  `claude-haiku-4-5-20251001`, TimelineXray MCP tools only (`--strict-mcp-config`), at most
  15 turns and USD 0.20 per item, 40 items, total USD 1.80. Precision 27 / 30 = 0.900
  (threshold 0.95: not met); coverage 27 / 31 = 0.871 (0.80: met); abstention accuracy
  5 / 9 = 0.556 (1.00: not met); false-abstention rate 0 / 31 (0.20: met); citation
  integrity 50 / 50 (met); completeness: all 40 items ran, 5 ended at the turn limit (not
  met). The numbers were not tuned. Root analysis and the proposed corrections for the next
  run (name the pipeline in Q01-Q04, an abstention sentence in the agent contract, a
  decision on the turn limit) are in `docs/release-checklist.md` section 3a; per-item
  report: `eval/live-run-2026-10-01.txt`.

## [0.7.0] - 2026-10-01 - Optional Context Layer integration

Everything in this release is optional and off by default: TimelineXray does not import,
depend on (not even as an extra), install or start Context Layer, and users who do not run
`txray export` see no change in behaviour. Nothing has been pushed.

### Added

- **`txray export context-layer --out DIR [--ledger DIR] [--commit C] [--include-stale]
  [--json]`** (`timelinexray/export/`, `export_cli.py`): the findings ledger as Markdown
  notes a Context Layer vault can index. One note per finding (YAML frontmatter: finding
  id, title, evidence status and basis, freshness with its commit and check, current flag,
  evidence class, scope, component, cited commits; body: status, the claim in a fenced
  block, commit-pinned GitHub permalinks to `xai-org/x-algorithm` with line ranges, span
  SHA-256, blob and anchor, the location at the freshness commit after re-anchoring, the
  "public default" wording for `PARAM_DEFAULT` findings, dependencies as `[[wikilinks]]`,
  negative search, external sources, last review, limitations); an index note linking every
  exported note with `[[wikilinks]]`; a README note with the provenance ("exported from
  TimelineXray <version>, ledger head <hash>; status and freshness at export time;
  re-export to refresh"). Only current findings by default (the `find_findings` rule);
  `--include-stale` adds findings that are not current, labelled `NOT CURRENT`; superseded
  and retracted findings are never exported. Deterministic (byte-identical re-export;
  unchanged files are not rewritten). Writes atomically and only inside `--out`, never
  through a symbolic link; refuses an `--out` in the TimelineXray working tree or package,
  in or around the snapshot store or ledger, or in, above or around an analytics dataset;
  a manifest (`.txray-export.json`) bounds what a re-export may replace or remove, and
  foreign or edited files are refused. Ledger text is escaped or fenced so it cannot add
  links, headings or tags. No analytics data and no source bytes are read.
- `docs/context-layer.md`: three optional ways to connect (export into a vault; both MCP
  servers side by side in one host, with Claude Code and generic stdio examples; Context
  Layer's GitHub reader on the upstream documentation at the pinned commit), the Context
  Layer version and commit read (0.4.0, `23e5b1c`), and what was and was not tested.
- Tests: `tests/test_context_layer_export.py` (25 tests: selection, labels, `--commit`,
  imported findings, frontmatter and body, wikilinks, escaping, determinism, manifest
  cleanup, interrupted re-run, refusals, analytics canary, a static check that no module
  imports `context_layer`, no dependency, nothing loaded by default) and the optional live test
  `tests/test_context_layer_live.py` (runs `context-layer init`, `index` and `search` when
  `TXRAY_TEST_CONTEXT_LAYER` names an executable; run once with Context Layer 0.4.0 at
  `23e5b1c`: pass).

### Changed

- `scripts/run_tests.py`: a skip whose reason starts with `optional:` (an optional
  integration test with a third-party tool) is listed but never fails the strict run.
- `timelinexray.mcp.guard.find_analytics_dataset(root, walk=False)` checks only `root` and
  the directories above it (used for an `--out` that does not exist yet).
- README, `docs/architecture.md`, `docs/limits.md`, `SECURITY.md` and the agent contract
  (`AGENTS.md` = `CLAUDE.md`) describe 0.7.0 and the optional integration.

## [0.6.0] - 2026-10-01 - Milestone 6: release hardening (all six milestones)

Version 0.6.0 completes the six milestones of P7 section 10.2. The repository stays private
until the owner approves publishing; nothing has been pushed.

### Added

- **Affected findings in digests** (open item of Milestone 5). `LedgerFindingsProvider`
  reads the findings ledger read-only (`Ledger.read_events`: chain verified, shared lock of
  an existing lock file, never created; FIFO and other non-regular files refused) and lists
  every active finding whose code citation or span dependency a changed hunk overlaps or an
  insertion point falls inside, with evidence status (and basis), workflow and recorded
  freshness (and the commit it refers to) as separate fields. Citations made at other
  commits are placed by exact span bytes; unplaceable spans are counted under `coverage`.
  `txray digest` and `txray update` take `--ledger DIR`; the ledger head enters
  `inputs_sha256`. Real history: a finding citing the ClickWeight line at 4c5cfe8 is listed
  in the 4c5cfe8..a707cc2 digest (STALE at a707cc2).
- **Security regression suite** `tests/test_security.py` (32 tests) and a threat model in
  `SECURITY.md`: every CLI and MCP path input (traversal, absolute, `..`, NUL, control and
  direction-override characters, long names); a hostile upstream tree (names with
  newlines, leading dashes, shell metacharacters and escapes, 450-character paths,
  absolute/escaping/directory symlinks, binary and oversized blobs, escape sequences in
  content) with every git argument vector recorded; `..` and `.git` tree entries refused by
  fetch-time fsck; drills for force-push to an unrelated history, a commit deleted and
  garbage-collected upstream, fetch failure then recovery, a corrupt upstream, invalid
  branch names and look-alike URLs; ledger tampering refused by CLI, MCP and digest
  (consistent rewrites caught by `--expect-head`, forged self-approval); MCP size
  boundary, hostile values and a garbage stream over stdio; an analytics canary found in
  none of four output channels (and the MCP server refusing the dataset); static checks for `shell=`, `eval`, `exec`, `pickle`.
- `timelinexray.textsafe`: terminal control characters and direction overrides in upstream
  names and content are shown escaped in human-readable CLI output and Markdown digests;
  `txray show` warns and points to `--raw` for exact bytes.
- `docs/limits.md`: every known limit in one place. `tests/test_licensing.py`.
- CI readiness: `.github/workflows/ci.yml` (Linux and macOS x Python 3.11-3.13, upstream
  clone at the pinned commit cached, `make ci`; actions pinned to full SHAs: checkout
  v7.0.1, setup-python v7.0.0, cache v6.1.0), `make ci` (lint, hygiene, strict tests,
  build + clean-venv install), `scripts/check_workflows.py`, `scripts/run_tests.py`,
  `scripts/build_check.py`, `scripts/fetch_test_upstream.py`, `scripts/ci_status.py` (the
  release gate), `CONTRIBUTING.md`, `tests/test_ci.py`.
- `docs/release-checklist.md`: the clean-install walk with commands and observed results,
  and the P7 section 7.3 release gates with denominators.
- `txray mcp tools --json --strict` prints the strict output schemas.

### Changed

- **Anchor rule.** At a citation's own commit an anchor occurring one or more times inside
  the span confirms it: `AMBIGUOUS` is now `FOUND_MULTIPLE` and counts as usable (`INTACT`,
  `CURRENT`) in `txray show`, the verifier, the write gate, goldens and the MCP schemas.
  Relocation to another commit still needs the exact span bytes exactly once; several
  matches stay `ambiguous` (`UNVERIFIABLE`) with a reason, never a first match. Goldens
  unchanged and passing.
- **MCP output schemas.** Each tool keeps a strict output schema, and the server now
  validates every result against it before sending (a mismatch is withheld as an
  `invalid_output` error). `tools/list` publishes a relaxation (exact envelope and `data`
  object, shape below, 2,600-byte budget per tool, no descriptions, `$defs` for repeated
  shapes); `mcp/schema.py` supports local `$defs`/`$ref`. The `tools/list` line shrank from
  61,856 to 34,321 bytes; `tests/mcp_tools_list.json` regenerated.
- **Public internal APIs** replace cross-package private calls: `fsutil.atomic_write`,
  `syntax.extract_checked`, `syntax.source` (`mask`, `Masked`, `Lines`,
  `match_delimiters`); `tests/test_internal_api.py` enforces the boundary.
- File-system arguments with NUL or control characters or over 4,096 characters are usage
  errors; file-system failures are reported as `txray: error` (JSON code `os_error`) instead
  of a traceback; repository paths over 4,096 characters and blobs over 64 MiB are refused.
- NOTICE: SPDX identifier, runtime fetching without vendoring, citations-only goldens, no X
  or xAI logos, third-party software. README, AGENTS.md = CLAUDE.md and
  `docs/architecture.md` describe 0.6.0; development status Alpha.
- `.github/workflows/test.yml` is replaced by `ci.yml`.

### Measured (development machine, macOS, Python 3.12, git 2.54; not promised)

- `make ci` with `TXRAY_TEST_UPSTREAM`: exit 0; see `docs/release-checklist.md` for the test
  count and timings.
- Clean-install walk: wheel 281,682 bytes; `txray index 77d431a` about 11 s in the fresh
  environment.

### Known limitations

- The GitHub workflow has not run on GitHub (nothing is pushed); `scripts/ci_status.py` is
  the gate once it is. See `docs/limits.md` for everything else.

## [0.2.0.dev1] - 2026-09-30 - Milestones 4b (MCP findings tools) and 5b (diff, classification, digest)

## Milestone 4b: agent access (MCP findings tools)

### Added

- Three read-only MCP tools over the findings ledger, in
  `src/timelinexray/mcp/tools/findings.py` (registered in `TOOL_MODULES`; see the
  "Findings tools" section of `docs/mcp.md`):
  - `find_findings`: search by text, component, evidence status, evidence class,
    freshness, workflow state and commit. Each result carries the evidence status with its
    basis (`proposed`, `reported`, `reviewed`), the workflow state, `current`, the
    freshness with the commit it refers to and the verify event behind it, citations in the
    citation shape (plus `resolution`), a "public default at commit ..." note for
    `PARAM_DEFAULT` findings and the review/queue state. `current_only` (default `true`)
    returns only active `CURRENT` findings; the matches it leaves out are counted in
    `not_current`. `commit` evaluates freshness at a pinned commit for historical answers.
    Paged with cursors bound to the ledger head.
  - `get_finding`: one finding with limitations, web sources, dependencies, provenance
    revisions, every verification check and review with event sequence numbers and hashes,
    open queue items, supersession/retraction, its event history and the ledger head.
  - `verify_claim`: re-reads the cited spans of a finding, or of up to 8 given citations,
    with the Milestone 3 verifier (integrity at the cited commit and, with
    `target_commit`, re-anchoring), re-checks dependencies and negative searches, compares
    the result with the ledger's recorded checks, and states that span integrity is not
    semantic truth (`semantic_verdict` is always `NOT_ASSESSED`). It never writes to the
    ledger.
- `txray mcp serve --ledger DIR` (the ledger otherwise comes from `$TXRAY_FINDINGS` or
  `<store>/findings`, resolved with `timelinexray.findings.ledger_directory`). Clients
  cannot name a ledger or any file. An explicit `--ledger` must exist; a default that is
  missing or inside a git working tree leaves the code tools working and makes the
  findings tools report why.
- Ledger guard: `events.jsonl`, `HEAD` and `.lock` must resolve inside the ledger directory
  and be regular files; the analytics-dataset refusal covers the ledger and every
  directory above it; error messages show the ledger as `<ledger>`. Reads re-verify the
  whole hash chain under a shared `flock` of an existing lock file and never create one.
- Tool results can carry tool-specific envelope notes (`ToolResult.notes`); the findings
  tools add notes on ledger text as untrusted data, status basis, freshness and public
  defaults. The server instructions mention the findings tools, and its start-up log line
  says whether a ledger is available.
- `docs/agents/AGENTS-snippet.md` and `generic-stdio.md` cover the findings tools and
  `--ledger`.
- Tests (`tests/test_mcp_findings.py`, 30 tests): a synthetic ledger with reviewed, draft,
  stale, unchecked, negative, dependent, superseded and retracted findings; the default
  `current_only` view, historical commits, every filter, paging and cursor binding to the
  ledger head, citations checked against `git show`, input validation and conflicts,
  checks/reviews/history against the raw events, `verify_claim` on changed, relocated,
  missing and baseline spans, dependencies and negative searches; the no-write guarantee
  (ledger names, bytes, sizes, modification times and `HEAD` unchanged after every call, no
  lock file created); missing, unconfigured, damaged, symlinked and FIFO ledgers; datasets
  in and above the ledger; output limits; the `tools/list` line limit; ledger location
  resolution; stdio round trips (modern and legacy, `--ledger` and `$TXRAY_FINDINGS`); and,
  with a local upstream clone, the ClickWeight finding at 4c5cfe8 re-anchored as `changed`/
  `STALE` at a707cc2 (proposed line 329) over stdio.

### Changed

- `tools/list` lists ten tools; `tests/mcp_tools_list.json` was regenerated and
  `EXPECTED_TOOLS` extended. The legacy stdio session test checks that the findings tools
  answer `NOT_FOUND` when no ledger exists.
- The analytics-dataset refusal message names the refused location ("snapshot store" or
  "findings ledger").

### Measured (development machine; not promised)

- The `tools/list` response line is about 61.8 KB of the 64 KiB limit with ten tools.
- On a synthetic ledger of 600 findings (1,800 events, 2.8 MB), `find_findings` takes about
  0.07 s, `get_finding` 0.06 s and `verify_claim` with a target commit 0.2 s.

### Known limitations

- Every call re-reads and re-verifies the whole event log; there is no cache.
- `verify_claim` takes a finding dependency's freshness from the ledger instead of
  re-verifying the other finding.
- The findings tools have not been run with Claude Code or any other real client yet.

<!-- Changelog fragment for CHANGELOG.md, to be merged by the integrator. -->

## Milestone 5b: updates (diff, classification, reviewed digest)

### Added

- `txray diff <old> <new> [--class C] [--json]`: a two-commit diff of pinned commits read
  from git objects only: added, removed, modified and renamed paths with blob ids (renames
  by identical blob, else line-multiset similarity >= 50 %), zero-context line hunks on the
  `txray show` line contract, slid to the position that cuts the fewest Milestone 2 symbol
  spans, per-region attribution to the Milestone 2 symbols of each side (from the code
  index when the commit is indexed, else from the same registry; both paths give identical
  diffs), and classified items.
- Ten change classes (`timelinexray.diff.rules`, classifier v1): `parameter-default`
  (`param!(Name, type, "flag", value)`, literal const/static/Java `static final`/literal
  fields, Python constants, YAML/TOML/INI/JSON config keys; old -> new value; `param!`
  added/removed), `registration` (component lists whose entries were added, removed or
  reordered), `scoring-logic`, `model-config`, `license`, `docs-only`, `test-only`
  (test files and in-file test code), `cosmetic` (token-equal, or a move with identical
  content), `generated-vendored` and `unknown`. Every item cites an exact span on each
  commit (commit, path, lines, span SHA-256, blob id, via `timelinexray.span.read_span`),
  or says why a side has none (`context`, `absent`, `empty`, `not-text`). Commit messages
  are never read.
- `txray digest <old> <new> [--out DIR] [--format md|json]`: a deterministic digest
  (`timelinexray/digest/v1`) generated from diffs: statements (generated from diffs,
  public defaults, nothing posted, unreviewed), range and lineage (first-parent chain,
  commits reached through merges), exceptional events, counts by class (net and across
  steps), a parameter table with both citations, per-parameter timelines and intermediate
  reversions, registration changes and reversions, an affected-findings section from a
  read-only `AffectedFindingsProvider` (the null provider says explicitly that nothing was
  computed), other classified changes, unknown changes, intermediate history, health and
  an input SHA-256. Intermediate first-parent commits are pinned locally (never fetched).
- `txray update --out DIR [--upstream URL] [--branch B] [--since COMMIT]`: one guarded
  fetch through `netguard.fetch`, pin of the branch head, digest against the previous
  accepted pin, `update-status.json`, and exceptional events: `fetch-failure`,
  `branch-missing`, `history-rewritten` (force-push; the head is quarantined),
  `commit-unreachable-upstream` (deleted commit; pins kept), `pinned-commit-missing`,
  `license-changed` (promotion paused) and `no-new-commit`. Exit 1 whenever attention is
  needed. State in `<store>/updates/state.json`. A cron example is documented; nothing is
  scheduled or posted by the tool.
- `docs/updates.md` (diff contract, class rules, digest contents, findings provider,
  update flow and events, cron example, measurements, limits).
- Tests (47): synthetic fixture histories covering every class, rename (exact and with
  edits), reorder-only, cosmetic-only (comments, formatting, CRLF), Python indentation and
  string-literal negatives, value parsers for Rust/Java/Scala/Python/YAML/TOML/JSON, hunk
  reconstruction and alignment, citations re-read with the span reader, index-vs-extraction
  equality, commit messages never echoed, digest timelines/reversions/merge side commits,
  provider interface, reversed and diverged ranges, CLI exit codes; update drills for
  force-push, deleted commit, commit missing locally, fetch failure, license change,
  branch missing and refused URLs (only one guarded fetch per run, no fetch while pinning);
  and a replay over the real upstream history: `4c5cfe8..a707cc2` reports `ClickWeight`
  0.4 -> 0.3 in both parameter files with verified citations, and the full 39-commit
  digest is byte-identical across three concurrent runs.

### Changed

- `src/timelinexray/diff.py` and `src/timelinexray/digest.py` (stubs) are replaced by the
  packages `src/timelinexray/diff/` and `src/timelinexray/digest/`; `txray` registers the
  new commands from `timelinexray/digest_cli.py`.

## [0.1.0] - 2026-09-30 - Milestone 3: findings memory (first research release)

## Milestone 3: findings memory

### Added

- Findings memory (`timelinexray.findings`, `docs/findings-memory.md`): records whose code
  citations are Milestone 1 span records (commit, path, lines, byte range, blob and span
  SHA-256, anchor). Evidence status (with its basis `proposed`, `reported` or `reviewed`),
  freshness (`CURRENT`, `STALE`, `UNVERIFIABLE`, `NOT_CHECKED`) and workflow (`draft`,
  `imported`, `reviewed`, `superseded`, `retracted`) are three separate fields; scopes are
  bounded (`public_code`, `public_default`, `historical`, `external`), and `PARAM_DEFAULT`
  findings are always `public_default`.
- Append-only JSONL event log (`create`, `verify`, `review`, `supersede`, `retract`,
  `import`; `schemas/findings-event.schema.json`) chained with SHA-256, with a `HEAD` record,
  an exclusive lock, compare-and-swap writes (`--expect-head`) and all-or-nothing appends.
  `txray findings verify-log` detects changed, reordered, removed, inserted and truncated
  events and torn writes; a damaged log is never read or extended. The ledger lives in
  `--ledger`, `$TXRAY_FINDINGS` or `<store>/findings`, and the default is refused inside a
  git working tree.
- Verifier (`timelinexray.verify`): span integrity at the cited commit (`INTACT`, `CHANGED`,
  `MISSING`, with the anchor verdict beside it) and re-anchoring on a target commit per P7
  section 3.3: `identical`/`unchanged`/`relocated` (CURRENT, with a new provenance revision
  when a span moved), `changed` (STALE, with a diff-based proposal and a review item),
  `ambiguous` and `missing` (UNVERIFIABLE; of several matches none is chosen). Renamed
  files are followed by blob id, or through the Milestone 2 index when the target is
  indexed. The verifier never changes an evidence status.
- Declared dependencies (other findings, or spans) make a finding `STALE` when they change;
  negative (`NOT_FOUND`) findings record their query, path glob, index generation and
  coverage, and are re-run at every target.
- Reviews: only a `review` by a `reviewer` or `maintainer` sets a reviewed status; no
  self-approval (the creating actor cannot review); confirming `SUPPORTED`/`PARTIAL` needs a
  current integrity check. Supersession (by an existing finding or a new record) and
  retraction. A prioritized review queue with evidence packets.
- Research import: `schemas/research-import.schema.json` (the research reports' findings
  format) and `txray findings import FILE --source-label LABEL` for JSON, a strict YAML
  subset reader (`timelinexray.findings.yamlsub`, standard library only) and Markdown with
  one findings block. Imports keep the report's status (basis `reported`), never promote,
  verify code sources immediately, are idempotent, and round-trip through
  `txray findings export --format research`. A small synthetic example ships as
  `schemas/research-import.example.yaml`.
- `txray findings add|import|list|show|verify|reanchor|review|supersede|retract|queue|export|verify-log`,
  all with `--json`; `reanchor --strict` exits 1 when a finding is not `CURRENT`.
- Goldens: `goldens/citations.json`, 35 reference citations (27 at 77d431a, 8 history
  goldens with 14 later-commit checks) taken from hand-checked research findings, with span
  SHA-256 and expected verdicts computed by an independent `git show` oracle. They include
  the ClickWeight public default `0.4` at 4c5cfe8, which re-anchors as `changed`/`STALE` at
  a707cc2 and 77d431a, and unchanged weight lines that relocate and stay `CURRENT`.
- Tests: hash-chain tamper/reorder/truncation/torn-write detection; the P7 mutation suite on
  a synthetic history (line shift, pure rename, rename with edit with and without the index,
  literal change, duplicate anchor, whole-line matching, deleted file, span and finding
  dependency changes, negative-search invalidation); write gate; no self-approval;
  supersession and retraction; schema/validator agreement; YAML subset constructs and
  errors; import round trip of the synthetic example (YAML and JSON); CLI envelopes and exit
  codes; goldens against a local upstream clone.

### Measured (development machine; not promised)

- All 35 goldens pass against a local clone at 4c5cfe8, a707cc2 and 77d431a.
- The research-import files of the seven research reports (508 findings) validate against
  the schema; importing them with integrity checks takes about 3 s, re-anchoring all
  425 code-backed findings on one target about 0.5-2 s. The YAML subset reader returns the
  same data as a YAML 1.1 reader on those files and on tens of thousands of generated documents.

### Known limitations

- The diff-based proposal for a changed span is heuristic; files over 50,000 lines are not
  diffed. Other files are searched for a moved span only when its path is gone.
- A hash chain is not a signature; actor names are declared, not authenticated.
- Dependencies are declared by authors, not inferred.

## [0.1.0.dev4] - 2026-09-30 - Milestone 4a: read-only MCP server (code tools)

<!-- Changelog fragment for CHANGELOG.md, to be merged by the integrator. -->

## Milestone 4a: agent access (read-only MCP server, code tools)

### Added

- `txray mcp serve [--store DIR] [--time-budget SECONDS]`: a hand-written stdio JSON-RPC
  2.0 MCP server (no SDK), public-code profile only, read-only. Protocol revisions
  `2026-07-28` (per-request `_meta`, `server/discover`, `resultType`, `ttlMs`/`cacheScope`,
  `-32022` for unsupported versions) and `2025-11-25` (`initialize` handshake), verified
  against the official specification pages on 2026-09-30 (URLs in `docs/mcp.md`).
- Seven tools, registered from small modules (`timelinexray/mcp/tools/pins.py`, `code.py`)
  with a documented slot for findings tools after Milestone 3: `list_commits`,
  `resolve_commit`, `manifest_summary`, `read_span` (exact span, blob and span SHA-256,
  anchor verdict; at most 120 lines and 16 KiB, rejected rather than shortened),
  `search_code`, `find_symbols` (optional unresolved call candidates) and
  `index_coverage`. Every result uses one envelope (`timelinexray/mcp-result/v1`) with the
  commit, index generation, outcome, warnings and notes; every source location is a
  citation with span SHA-256, blob id and `content_trust: UNTRUSTED_SOURCE_DATA`; numbers
  from code are labelled public defaults at their commit.
- `txray mcp tools [--json]`: the tool list, or the full `tools/list` definitions with
  input and output JSON Schemas; `tests/mcp_tools_list.json` pins them.
- Limits and guards: 16 KiB request lines, 64 KiB response lines (list results shortened
  with `TRUNCATED:` markers and continuation cursors; otherwise `output_too_large`), a
  10 s per-call time budget, closed input schemas with strict types, lengths, enums and
  patterns, normalised repository-relative paths (no absolute, `..`, backslash, NUL or
  control characters), HMAC-authenticated cursors bound to tool, commit, index generation
  and query, confinement of every opened file to the snapshot store, and refusal of any
  store containing or inside an analytics dataset. The package imports neither the
  analytics package nor any network module; local locations never appear in responses.
- `docs/mcp.md` (protocol verification, methods, tools, envelope, limits, security
  controls, deviations from the P7 contract bundle, findings slot, client matrix) and
  `docs/agents/` (an AGENTS/CLAUDE snippet and client configurations; only what was run is
  marked tested: Claude Code 2.1.286 in print mode with `--mcp-config`).
- Tests (54): JSON-RPC framing and errors (malformed JSON, invalid UTF-8, duplicate keys,
  `NaN`, deep nesting, batches, bad ids, unknown methods, invalid params), both protocol
  revisions, argument validation, path traversal, symlink/binary refusal, store escape via
  a tampered pin record, analytics-dataset refusal (inside, above, malformed, symlinked,
  appearing after start), output truncation and continuation, oversized requests, the time
  budget, cursor binding and forgery, schema validity of every result, the `tools/list`
  snapshot, a child-process import check, and subprocess stdio round trips on a fixture
  store and on upstream `77d431a` (pinned from a local clone via `file://`).

### Changed

- `src/timelinexray/mcp.py` (stub) is replaced by the package `src/timelinexray/mcp/`;
  `txray` registers the `mcp` command from the new `timelinexray/mcp_cli.py`.

## [0.1.0.dev3] - 2026-09-30 - Milestone 2: native code index

## Milestone 2: native code index

### Added

- Code index per snapshot store (`<store>/index/index.sqlite3`): SQLite FTS5 (trigram
  tokenizer) over the non-blank lines of every `parsed-candidate` and `text` blob, read from
  the mirror and checked against the manifest SHA-256. Commit-scoped rows (`generations`,
  `files`) carry the commit id; lines, parses, symbols and calls are keyed by blob id and
  reached only through a commit's `files` rows. See `docs/code-index.md`.
- `txray index <commit> [--rebuild] [--coverage]`: incremental by blob (only blobs new to
  the index are read and parsed; an unchanged commit is a no-op), in-place rebuild, one
  transaction per build, and a coverage row for every manifest path (lexical `indexed` or
  `skipped` with the manifest reason or `not-utf8`; syntax `parsed`, `partial`, `failed`,
  `not-applicable`, `unsupported` or `skipped`, with the backend used).
- `txray search <commit> <query> [--path GLOB] [--limit N] [--literal]`: lines containing
  every term as a case-insensitive substring. Terms are passed to FTS5 as quoted literals
  and every candidate is re-checked, so query text is never FTS5 syntax or SQL. Each hit
  carries commit, path, line range, byte range, snippet and the span SHA-256 computed by
  the Milestone 1 span reader.
- `txray symbols <commit> [--path GLOB] [--kind K] [--name N] [--limit N] [--calls]`:
  declarations with kinds, containers, 1-based line ranges, span SHA-256, signature,
  modifiers and the producing backend; `--calls` adds unresolved call candidates
  (`CANDIDATE_CALL`, resolution `unresolved`).
- Syntax extraction behind `timelinexray.syntax.Backend` and a priority `Registry`, with a
  parse cache keyed by `(blob_oid, language, backend, backend_version)`. The `lexical`
  backend (standard library only) covers Rust (including `param!(Name, ...)`,
  `macro_rules!`, `impl` blocks, nested generics, raw strings, lifetimes), Scala (objects,
  case classes, expression bodies, interpolated and triple-quoted strings), Java
  (annotations, inner and nested types, records, generics, anonymous classes) and Python
  (decorators, async, nested functions, multi-line headers). A tree-sitter backend is a
  documented hook only (`timelinexray/syntax/treesitter.py`); no dependency was added.
- `timelinexray.span.read_span` accepts an optional precomputed `line_map` and
  `blob_sha256` for callers that cut many spans from one blob (results unchanged).
- Tests: synthetic fixtures in all four languages with exact expected symbols and calls,
  CRLF/BOM invariance, mutation robustness, masking, the backend hook, FTS query-injection
  safety, commit scoping, coverage of every path, grep-equivalence of search, rollback of an
  interrupted build, and incremental == clean (every query and a full logical dump) on a
  two-commit fixture and on upstream `a707cc2` and `77d431a`.

### Measured at 77d431aabf409ca1c1eed9bec7e2183f7c914e23 (development machine; not promised)

- 2147 paths: 2140 indexed lexically (380,116 non-blank lines), 7 skipped (1 binary,
  5 generated, 1 symlink); 1836 native files parsed by `lexical/1` (0 partial, 0 failed),
  304 text files not applicable.
- 50,387 symbols (Rust 17,029; Scala 17,179; Python 9,926; Java 6,253) and 151,726
  unresolved call candidates.
- Clean build into an empty index 7.0-8.2 s; incremental build after its parent `a707cc2`
  (65 changed blobs) 0.9-1.0 s; re-indexing an indexed commit is a no-op. Database 92.7 MB
  for 77d431a, 102.3 MB with a707cc2.
- Sanity checks: `compute_weighted_score` (function, `xai-value-model/scoring.rs` 65-119),
  the `ReplyWeight` param (`home-mixer/params/param.rs` 303), and `PhoenixScorer` hits in
  `home-mixer/candidate_pipeline/phoenix_candidate_pipeline.rs` (lines 81 and 399).

### Known limitations

- Symbol extraction is lexical and heuristic (see `docs/code-index.md`); call candidates are
  never resolved; tree-sitter is not bundled.
- Non-UTF-8 text files are listed as skipped (`not-utf8`) and are not searchable.

## [0.1.0.dev2] - 2026-09-30 - Milestone 5a: offline creator metrics

### Added (Milestone 5a: offline creator metrics)

- `timelinexray.analytics` package (replaces the `analytics.py` stub) and
  `timelinexray/metrics_cli.py`, registered in `cli.py` with one lazy line:
  `txray metrics import | rwe | reach`.
- Offline process guard: before any other analytics module is imported, `txray metrics`
  installs a CPython audit hook that refuses sockets and name resolution, process creation and
  `ctypes` for the rest of the process, restricts writes to the declared `--out` directory
  (none for `rwe` and `reach`), never allows writes inside the snapshot store, and switches
  off the bytecode cache.
- Import contract of P6 §2.2 for one documented schema, `x-post-v1` (post-level export of
  the X analytics web interface), with versioned header-alias tables in English
  (`EXTERNAL_RECHECK`) and Turkish (header row as transcribed, pinned by SHA-256): original
  headers kept, post ids as exact strings, declared number locale (`en`, `tr`), declared
  source time zone with daylight-saving gaps and folds recorded, `observed` / `blank` /
  `missing-column` kept distinct, post grain only, capture time, count kind and scope
  recorded, snapshots keyed by (post, capture time, scope) and never summed, conflicting
  re-imports refused, decreasing cumulative counts reported. Post text and links are not
  stored. Datasets are private files (0600) with a SHA-256 integrity check.
- M1 realized weighted engagement (P6 §4.1): variants `core` (default), `extended` (only
  with `--attribution-validated`) and `sensitivity`; exact arithmetic; per-component counts,
  rates, contributions and one-event sensitivity; negative terms shown as unknown; pooled
  versus mean post values; optional common horizon.
- Versioned coefficient table of the 25 value-model terms and 2 conditional boosts as public
  defaults at `77d431aabf409ca1c1eed9bec7e2183f7c914e23`, checked against the pinned upstream
  source through the span reader.
- M2 relative reach (P6 §5.1): pre-publication-only baselines, weekday/time buckets in a
  declared time zone, the stated shrinkage heuristic, RR (undefined for a zero baseline) and
  RR+ as separate fields, with every undefined value explained.
- M3 checklist and M4 visibility-flag records with their evidence status; no scores,
  percentages, probabilities or points.
- `docs/analytics.md`; tests with synthetic English and Turkish exports, locale number
  formats, missing columns, zero impressions, duplicate and cumulative snapshots, the P6
  worked example (RWE core 16.5), RR/RR+ edge cases, the coefficient table against the
  pinned upstream, and child-process proofs that network, process and write attempts fail
  inside a metrics process.

### Known limitations (Milestone 5a)

- The English header names of `x-post-v1` have not been checked against a real export.
- The date column format of real exports is not documented; ISO 8601 is read by default and
  other formats must be declared with `--date-format`.
- Uncertainty intervals (block resampling), the validation protocol and experiment design of
  P6 §7-§9 are documented, not implemented.

## [0.1.0.dev1] - 2026-09-30 - Milestone 1: evidence foundation

### Added

- Snapshot store with bare mirrors keyed by upstream URL, commit pins protected by
  `refs/txray/pins/<commit>`, and atomic writes.
- Per-commit manifest of every path (`git ls-tree -r`, gitlinks included): classification
  `parsed-candidate` (Rust, Scala, Python, Java), `text`, or `excluded` with one reason
  (`binary`, `generated`, `oversize`, `submodule`, `symlink`, `vendored`) and the rule that
  fired; blob id, size, SHA-256, UTF-8 flag and language guess. Stored as deterministic JSON
  with a `shasum`-compatible SHA-256 sidecar. JSON Schema in `schemas/manifest.schema.json`.
- License inventory of LICENSE, COPYING, NOTICE and third-party notice files, with keyword
  hints.
- Exact span reader over git blobs with a documented byte-to-line mapping (LF-delimited,
  CRLF and missing final newline preserved), span and blob SHA-256, and anchor verdicts
  `FOUND`, `AMBIGUOUS`, `MISSING` restricted to the span.
- Network allowlist and a single guarded fetch function with hardened git options.
- CLI `txray pin | manifest | show` with `--json` output and documented exit codes.
- unittest suite (`make test`) with a deterministic fixture repository (CRLF, lone CR,
  empty file, no final newline, BOM, non-UTF-8, binary, oversize, vendored, generated,
  symlinks, submodule) and acceptance tests at upstream commit `77d431a` via a local
  `file://` clone. Python sockets are blocked for the whole test run.
- Project documents: README, LICENSE (Apache-2.0), NOTICE, SECURITY, AGENTS.md = CLAUDE.md,
  docs/architecture.md, CI workflow for Linux and macOS (not yet run on hosted CI).

### Measured at 77d431aabf409ca1c1eed9bec7e2183f7c914e23

- 2147 paths: 1836 parsed-candidate, 304 text, 7 excluded (1 binary, 5 generated,
  1 symlink); 4 license/notice files.

### Known limitations

- The exact `pyproject.toml` was not built: its PEP 639 license field needs
  `setuptools>=77`, and only older setuptools was available offline. A scratch copy with
  only that field in the legacy table form installed with setuptools 69, and the installed
  `txray` console script ran.
- The CI workflow file has not been run.
