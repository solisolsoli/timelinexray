# Updates: diffs, classification and digests (Milestone 5b)

TimelineXray compares pinned commits of the upstream repository, classifies what changed,
and writes digests that cite exact spans on both sides. Everything is computed from git
objects in the snapshot mirror: no working tree, no build, and **commit messages are never
read or used as evidence** (the parser reads only the tree, parent and committer fields of
a commit object). Nothing is posted anywhere; digests are local files.

```
txray diff   <old> <new> [--class C] [--json]
txray digest <old> <new> [--out DIR] [--format md|json] [--ledger DIR] [--json]
txray update --out DIR [--upstream URL] [--branch NAME] [--since COMMIT] [--ledger DIR]
             [--reanchor] [--export DIR] [--json]
```

All three take `--store DIR` and use the Milestone 1 exit codes (0 ok; 1 failed, not
found, or attention needed; 2 invalid arguments; 3 network refused). `diff` and `digest`
need both commits pinned (`txray pin`); `digest` additionally pins, locally, every commit of
the first-parent chain between them (the objects must already be in the mirror, nothing is
fetched; a `file://` upstream must be listed in `TXRAY_ALLOW_FILE_URLS` as for `txray pin`).

`--out DIR` is checked before anything is built, fetched or written, with the rules of
`txray export` (`timelinexray.fsutil.check_output_directory`): it is refused (exit 1) when
it is a symbolic link, when it exists and is not a directory, when its parent does not
exist (files are written only inside `DIR`), or when it lies inside the TimelineXray
working tree or package, the snapshot store, the findings ledger, or an analytics dataset.
Digests and `update-status.json` are written atomically (`atomic_write`: a temporary file
in `DIR`, `fsync`, `os.replace`), so a reader sees the old or the new file and a symbolic
link at a digest's name is replaced, never followed.

## The diff

`timelinexray.diff.DiffEngine(store).diff(old, new)` returns a `CommitDiff`:

- **Files.** The two verified manifests are compared path by path (mode, type and blob id):
  `added`, `removed`, `modified` and `renamed`, each with both blob ids, modes and manifest
  classifications. Renames: first identical blob ids, then text files whose line
  multisets overlap by at least 50 % (`200 * common / (lines_old + lines_new)`), best pairs
  first. When more than 40,000 removed-by-added pairs would have to be scored, inexact
  detection only pairs files with the same base name and the diff says
  `rename_detection: limited`.
- **Hunks.** Blob bytes are split into lines exactly as `txray show` numbers them (only LF
  ends a line, CR stays in its line, a final unterminated line counts), so every hunk line
  is a valid `txray show` line on its side. The matcher is `difflib.SequenceMatcher`
  without the junk heuristic, after trimming the common prefix and suffix. Pure insertions
  and deletions are then slid, among equivalent positions, to the one that cuts the fewest
  Milestone 2 symbol spans (the symbol-aware counterpart of git's indent heuristic), so an
  added `param!(...)` block is one whole declaration. Hunks can differ from `git diff`;
  applying them to the old lines always reproduces the new lines (tested).
- **Symbols.** Each changed region names the innermost Milestone 2 symbols enclosing its
  changed lines on each side (`symbols.old`, `symbols.new`). They come from the code index
  when the commit is indexed (`CodeIndex.coverage` and `ReadSession.file_symbols`, one
  read connection per command; its records equal `CodeIndex.symbols`) and otherwise from the same
  syntax registry; both give identical diffs (tested). Symbols are extracted only for
  files whose content is analysed: files classified by path (license, generated/vendored,
  documentation, tests) are not parsed.
- **Items.** Classified changes, each with an old and a new **citation**: commit, path,
  1-based line span, the SHA-256 of the exact span bytes (from `timelinexray.span.read_span`,
  so `txray show <commit> <path> --lines A-B` reproduces it) and the blob id. A side that
  has nothing to cite says why: `context` (the change is an insertion or deletion; the
  cited line is where it applies), `absent` (no such path at that commit), `empty` (empty
  blob) or `not-text` (binary, oversize, symlink or submodule).

## Classes

A class names what the rule saw; it is not a reviewed interpretation. One file can yield
items of several classes. Rules, in order (classifier version 4; version 3 let every
scoring name win over kernel and training locations, metrics and debug names and the
content classes, version 2 had no content
rules, matched scoring names as substrings and read neighbouring and test symbols, version 1
had no
`build-dependency`, no `train` location or test helper names, and its `unknown` items
carried no reason):

| Class | Rule | Must not be read as |
| --- | --- | --- |
| `generated-vendored` / `unknown` | an entry that is not text on either side (manifest reason generated/vendored, else unknown with "not compared as text") | a content change of any kind |
| `license` | a license or notice file (`LICENSE*`, `NOTICE*`, `COPYING*`, ...), or a hunk whose every changed line is license header text | a determination of the applicable license |
| `generated-vendored` | the manifest says `generated` or `vendored` | reviewed upstream code |
| `docs-only` | documentation files (`.md`, `.rst`, `.txt`, `docs/`, `README*`, ...) | a change of behaviour |
| `test-only` | test files (`tests/`, `golden_corpus/`, `*_test.rs`, `test_*.py`, `*Test.java`, `test_support.*`, `test_helpers.*`, `*_fixtures.*`, ...), or hunks whose code lines all lie inside test code (Rust `#[cfg(test)]` modules and items and `#[test]` functions, Java `@Test` methods, Python `test_*`/`Test*`) | a change of production code |
| `build-dependency` | a build-system or dependency file by name (`BUILD`, `*.bazel`, `*.bzl`, `WORKSPACE`, `Cargo.toml`, `build.rs`, `pyproject.toml`, `setup.py`, `requirements*.txt`, `package.json`, `Makefile`, `CMakeLists.txt`, `Dockerfile`, `pom.xml`, `build.gradle`, `build.sbt`, `go.mod`, ...), or a hunk whose every changed code line lies in a Milestone 2 `import` symbol (Rust `use`/`extern crate`, Java, Scala and Python imports) or a bodyless `module` symbol (`mod name;`, a `package` clause); blank and comment-only lines may accompany them; an added or removed empty `__init__.py` (a package marker) and `py.typed` | that behaviour is unchanged: an import can change name resolution and a dependency version can change behaviour |
| `parameter-default` | the value of a `param!(Name, type, "flag", value)` declaration, a `const`/`static` or Java `static final` with a literal value, a literal Java/Scala field, a Python `UPPER_CASE` constant with a literal value, or a key of a YAML/TOML/INI/JSON config file differs between the two cited declarations; `param!` declarations added or removed | a production value: every value is a **public default** at its commit |
| `registration` | entries of a list that registers components were added, removed or reordered, or such a list appeared or disappeared: a `vec![...]`, `Seq(...)`, `List.of(...)` or Python list whose entries name components (`...Filter`, `...Source`, `...Hydrator`, `...Scorer`, `...SideEffect`, `...Rule`, ...) or that is bound to a name like `filters`, `sources`, `side_effects`, `rules` | that a registered component is active for any request |
| `cosmetic` | the comment- and whitespace-insensitive tokens of both sides are equal (string literals kept whole; indentation kept for Python and YAML), or a file moved with identical content | semantic equivalence where parser coverage is incomplete |
| `access-modifier` | a hunk with changed lines on both sides whose tokens are equal once access modifiers are removed (Rust `pub`, `pub(crate)`, `pub(super)`, `pub(in path)`; Java `public`, `private`, `protected`; Scala `private`/`protected` with an optional `[scope]`); decided before the name rules, since no other token changed | that behaviour is unchanged: a newly visible item can be used from other modules |
| `model-config` (model side) | changed code under a GPU kernel or offline training location: a `cuda`, `cutedsl`, `pallas`, `triton`, `kernels`, `train`, `training` or `optimizers` directory, or a `.cu`/`.cuh` file; read before the scoring-name rule (`ranker_attention.py` there is an attention kernel of the ranker model) | that a model artifact was deployed |
| `scoring-logic` | other changed code whose path, or else a symbol enclosing a changed production line, has a whole word naming scoring, weights or ranking (`score`/`scorer`/`scoring`/`scored`, `weight`, `rank`/`ranker`/`ranking`, `rerank`, `rescore`, `boost`, `penalty`, `decay`, `diversity`, `blend`/`blender`, `multiplier`, `calibrate`/`calibration`; words split at punctuation and camelCase), unless the name contains PageRank or RankAll, or a telemetry word (`metric(s)`, `stat(s)`, `statistics`, `debug`) is in the path's file name or the symbol's last component (`SCORED_METRIC`, `get_debug_scored_posts`, `scored_stats_side_effect.rs`; read only when the hunk's enclosing symbols are all listed, at most eight; when every enclosing symbol has a telemetry name the path does not decide either); a hunk that is `observability`, or `data-type` with the scoring word in the path alone, takes that class instead | that the change alters any ranking outcome; in the hand check below about half of the recent items, and fewer over the full history, were scoring code |
| `model-config` | other changed code or configuration under a model, feature, config, schema, proto, thrift, inference, `train` or training location | that a model artifact was deployed |
| `observability` | (when no name rule matched, or instead of `scoring-logic`) every changed code line of the hunk belongs to a statement that only logs, traces or records a metric: Rust `trace!`..`error!` and `tracing::`/`log::` macros, `event!`, `metrics`-crate macros, Prometheus `register_*!` statics, `NAME.inc()`/`observe()` and `NAME.with_label_values(..).set()`; Python `logger.`/`logging.`/`log.` calls and `Metrics.counter|histogram|gauge|timer(...)` with an optional `.record()`/`.add()`; Java and Scala `log.`/`logger.` calls and `stats.counter(...).incr()`. Only these call shapes count: the words `metric` or `stats` alone never do, since in this upstream they often name ranking data (`Metric`, `metric_value`, a `stats` map). Statements are split on masked code (no string or comment can fake one), a line shared with any other statement or a block header does not count | that behaviour is unchanged: a logged expression can have side effects and a metric can drive alerts or experiments |
| `data-type` | (when no name rule matched, or instead of a scoring *path*, when no enclosing symbol is a scoring name: a `pacing` field of `RequestShape` in `vm_ranker_request.rs`; a field of `ValueModelWeights` stays `scoring-logic`) every changed code line lies inside a Rust `struct`, `enum` or `union` definition (Milestone 2 symbols; fields, variants, attributes) and contains no `=` (no discriminant, no attribute default such as `default_value_t = 8080`) | that behaviour is unchanged: a new field or variant changes what code stores, matches and serializes |
| `visibility-rule` | (only when no name rule matched) every changed code line lies inside a Rust `const`/`static` whose declared type, or a function whose return type, is built only from the visibility rule types `Condition`, `Predicate`, `Clause`, `RuleClause` (`Vec<RuleClause>`, `[Clause; 3]`, `&[Condition]`) | that any post's visibility changed: the definition may be a refactor, and whether a rule runs depends on its registration and safety level |
| `unknown` | everything else; each item says why (`detail.unknown_reason`): `no-rule` (a parsed language, and no rule matched), `not-parsed` (a language without symbol extraction, such as C, C++, CUDA or shell: only path and token rules could apply), `not-text`, `mode-only`, `empty-file` (an empty file added or removed) | anything: it needs review |

Every `scoring-logic` and `model-config` item records the rule that decided it in
`detail.matched_by` (`path` or `symbol`, and the matched name). New rules are kept only after
a seeded hand check of the items they move out of `unknown` (on `77d431a..78460ca`, seeds
20261007 and 20261008, 42 items, each hunk read at both commits): the build-file rule 1/1,
the import rule 22/22, the `train` location 18/18 and the test helper names 1/1 were
correct. A filtering/visibility name
rule (`filter`, `visib`, `safety` in a path or symbol, after every other rule) was tried and
**rejected**: of 37 distinct sampled items it would have moved, 10 were rule or policy logic,
8 hydration inputs to rules and 19 caches, telemetry, server wiring or staging tools, so the name says
where code lives, not what it does. Those changes stay `unknown`; the digest counts unknown
items by area, so a filtering subsystem still shows as such.

The `scoring-logic` name rule itself was hand-checked for classifier version 3 (85 items,
`random.Random(20261007).sample` over items sorted by id, stratified by the deciding rule: 27
path and all 13 symbol items of `77d431a..78460ca`, 25 path and 20 symbol items of
`aaa167b..77d431a`; every hunk read at both commits). Correct meant "the changed lines
compute, combine, weight, threshold or order candidate scores"; adjacent meant score plumbing
or feed assembly (ads blending, request payloads to the ranker); wrong meant anything else.
Version 2 precision (correct / adjacent / wrong): recent path 11/7/9, recent symbol 4/0/9,
full-history path 5/4/16, full-history symbol 3/6/11. Five errors were clear and testable and
are fixed in version 3: a substring inside another word (`rankall`, `UNSCORED`), the PageRank
user-reputation score of spam and bot rules, the Phoenix RankAll event-to-index pipeline
(all 13 sampled RankAll items were Kafka, indexing or configuration code), a pure insertion
taking the name of the neighbouring declaration at its anchor line, and a test function
deciding the class of a production line in the same hunk. Every sampled item the fix moved
out was wrong (21 of 21); none correct or adjacent moved. Version 3 precision on the items
that stay: recent path 11/7/4, recent symbol 4/0/4, full-history path 5/4/7, full-history
symbol 3/6/9. What remains wrong is not a clear name error: GPU attention kernels of the
ranker model (`ranker_fa4/`, `pallas/ranker_attention*`), telemetry and debug code with a
`scored` name, offline training and evaluation code (`decay`, `weighted`), and wiring inside a
`ranked_following` pipeline. Read `scoring-logic` as "worth a look", never as "changes
scoring". Every moved item went to `unknown` or, under a config/proto/thrift location, to
`model-config`.

Version 4 (hand check `random.Random(20261008).sample` over the version 3 `scoring-logic`
items sorted by id, 22 of 53 items of `77d431a..78460ca` and 22 of 176 of `aaa167b..77d431a`;
every hunk read at both commits; same verdicts as above) makes the precise classes win over
the scoring name where the hunk is clearly one of them: kernel and training locations
(model side, above), telemetry and debug names, an `observability` hunk, and a `data-type`
hunk whose scoring word came from the path alone. Precision, correct / adjacent / wrong:
recent 9/6/7 (strict 0.41, lenient 0.68) -> 9/6/4 on the 19 items that stay (0.47, 0.79);
full history 2/6/14 (0.09, 0.36) -> 2/6/5 on 13 (0.15, 0.62). All 12 sampled items that
moved were wrong; no correct or adjacent sampled item moved. Every item that moved in either
range was checked (47: 40 kernel or optimizer files, 3 of them recent; metric registrations,
metric-name constants, a debug endpoint, a stats filter, the metrics module of `vm-ranker`, a
metric sampling-rate constant, and one adjacent `RequestShape` field, now `data-type`): 19
read in full, the other 28, all whole files under `cutedsl/ranker_fa4/`, `pallas/` or
`optimizers/`, by path and symbol list. The telemetry words are read only when every enclosing symbol of the hunk is
listed: a whole added file with more than eight symbols keeps its scoring name, since its
unread symbols may hold scoring code (two SimClusters candidate sources that compute
candidate scores would otherwise have moved). A scan of every identifier of the upstream at
`77d431a` and `78460ca` (62,158) found 34 that carry both a scoring and a telemetry word; all
are metrics, statistics, debug endpoints, test helpers or training statistics. What still
reads wrong: module declarations and client builders in `mod.rs`, Kafka topic settings,
old-side symbols of rewritten servers, a reputation score (`UserCredV2`), storage readers in
a `scorer`-named file. The same model-side rule took 56 items out of `unknown` (17 recent, 39
full history: CUDA kernels and host bindings under `phoenix/xrex/cuda/`, their Python
wrappers, optimizers); a seeded sample of 20 (seed 20261008) was 20 correct.
Counts: `scoring-logic` 53 -> 45 (recent), 176 -> 137 (full history).

What `unknown` is made of (version 4, after the model-side rule): on `77d431a..78460ca`, 384
of 1,236 items; 318 regions of modified files, 28 of renamed files, 38 whole added or removed
files; Rust 322, Python 59, Strato 3; areas `visibility-filtering/hydration` 87,
`phoenix/xrex` 54, `visibility-filtering/staging` 33, `abuse-enforcement-service` 28, then a
long tail. A text heuristic over the 346 regions of modified or renamed files put 300 in
"other logic" and the rest in small groups (constant declarations 17, signature-only edits
16, Python annotations 7, test-named symbols 6). On `aaa167b..77d431a`, 1,352 of 2,698: 1,211
are whole added or removed files (Scala 360, Rust 312, Python 230, Java 198; BotMaker rule
files `.df`/`.bot` 69, Strato 26), the initial publication of subsystems such as `botmaker`,
`simclusters`, `grox`. A whole added file takes a class only when the whole file fits one:
its path class where that class is precise (license, generated/vendored, documentation,
tests, build files, as before), or a content class for all its lines; production code of a
new subsystem stays `unknown`, and that is honest. Version 4 adds three small rules, each
read in full (populations below 20 were read item by item): a Rust item marked
`#[cfg(test)]` (any item, not only modules; not `cfg(any(test, ...))` or `cfg(not(test))`)
is test code, and blank or comment-only lines may accompany test code (9 items moved to
`test-only`, 9 correct: `#[cfg(test)] mod ..._tests` blocks whose attribute or the blank line
before them sat outside the module's span, `#[cfg(test)]` helper functions); an added or
removed empty file is no longer reported as a mode change: an empty `__init__.py` is a
package marker (`build-dependency`, 19 items) and `py.typed` is a build file by name (10
items), any other empty file stays `unknown` with reason `empty-file`; a binary or symlink
under a precise path class takes it (1 item: a symlinked test resource). Tried and **not
kept**: files named `metrics.rs`/`metrics.py` as `observability` (24 items read, 20 correct:
two Python files compute evaluation metrics and two Rust items carry a hydration timeout
helper, so the name is not enough); constants with literal values added or removed in
modified files as `parameter-default` (11 items, below a meaningful check); a class for
BotMaker rule files (`.bot`, `.df`, 69 items, full history only) is left to a design
decision, since such a class would describe moderation rules. Result: `unknown` 404 -> 384
(33 % -> 31 %) on `77d431a..78460ca` and 1,422 -> 1,352 on `aaa167b..77d431a`, counting the
model-side rule.

The four content rules of version 3 say what the changed lines *do* and were each kept only
after a seeded hand check (`random.Random(20261007).sample` over the rule's items sorted by
id, every hunk read at both commits; correct = the stated evidence holds and the title is
fair): `observability` all 11 items of both ranges, 11 correct, plus a scan of every statement
the patterns accept in the whole upstream at `78460ca` (2,576 statements; 4 per pattern read
at seed 20261007, and every receiver name listed: only loggers, log macros and metric
objects); `access-modifier` all 11
items, 11 correct; `visibility-rule` 20 of 32 items, 20 correct (many are refactors of the
rule DSL, which the class allows and its caveat states); `data-type` 26 items (20 recent, 6
full history) of a first version without the `=` exclusion: 25 correct and one clap
`Args` struct whose attributes carry command-line defaults, which is why lines with `=` are
now excluded; of the 22 sampled items that stay `data-type`, 22 are correct. Measured on
`77d431a..78460ca`: `unknown` 469 -> 404 of 1,236 items (37 hunks to `visibility-rule`, 49 to
`data-type`, 10 to `access-modifier`, 5 to `observability`; 19 hunks came back from
`scoring-logic`). On `aaa167b..77d431a`, where most unknown items are whole added files that
no line rule can take, `unknown` is 1,381 -> 1,422 (49 hunks back from `scoring-logic`, 17
to `data-type` and `observability`).

Values are compared as their source text with comments removed and whitespace collapsed.
A constant computed by code (for example `Duration::from_secs(compute())`) is logic, not a
default. Adjacent hunks of the same class inside the same symbol form one item.

## The digest

`txray digest old new` (or `timelinexray.digest.DigestBuilder(store).build(old, new)`)
builds one JSON document (`timelinexray/digest/v1`) and renders it as two Markdown files:

- **`digest-<old>-<new>.md`, the main digest**, summary first and bounded in size: *At a
  glance* (range, files and lines, the parameter, registration, `scoring-logic`,
  and `unknown` counts with how they were decided, events, affected findings,
  and what this file does not list), *What to check next* (the exact commands), then the
  sections below. Parameter defaults, registrations, affected findings and the
  `scoring-logic` items are listed with both citations; every other class
  (visibility-rule, model-config, test-only, build-dependency, data-type, observability,
  access-modifier, cosmetic, docs, license, generated, unknown) is counted by area (the first two directories of a path). Each section has a row budget
  (`timelinexray.digest.render`: 60 parameter rows, 30 timelines, 30 registration rows, 40
  finding rows, 120 logic items per class, else a by-file table of 40 rows); whatever does not
  fit is named with its count at the top and in its section, and is in the appendix.
- **`digest-<old>-<new>-appendix.md`**: every item the main file only counts, grouped by
  class, area and file, each with its summary, its unknown reason or the name rule that
  decided it, and both citations; plus the table rows the main file cut. Nothing is dropped:
  main file + appendix list every item, and the JSON document holds all of them.
- `--format md` (default) prints the main digest, `--format appendix` the appendix,
  `--format json` the document; with `--out DIR`, `md` writes the main file and its
  appendix. `txray update` writes all three.
- The JSON document is complete (every item, file, hunk and citation) and gains, additively,
  `overview.classes` (per class: items, files, lines, areas, `listed_in`, `decided_by` for the
  name-rule classes, `reasons` for `unknown`) and `classes[].must_not_be_read_as`.

Sections, in order (the summary-first block above them):

- **Statements**: generated mechanically from diffs; commit messages never read; every
  value is a *public default* at the cited commit, not a production value; nothing has been
  posted; classification is heuristic and unreviewed; how to check a citation; the
  community-analysis disclaimer.
- **Range**: both commits with committer times and manifest SHA-256; the relationship
  (`ancestor`, `same`, `reversed`, `diverged`, `unrelated-mirrors`); the first-parent
  chain; commits reached only through merges (their changes appear in the merge step).
- **Exceptional events** (see below), or an explicit "none detected".
- **Summary by class**: net items (old -> new), files, items across all first-parent steps,
  where the class is listed (this file or the appendix), what the rule saw and what the class
  must not be read as.
- **Parameter default changes**: name, path, declaration, change, public default
  old -> new, and both citations. With more than one step, **values at intermediate
  commits** (a timeline per parameter that changed inside the range) and **intermediate
  reversions** (a value that changed and later returned to an earlier value, including a
  declaration added and removed again).
- **Registration changes**: list, entries added / removed / reordered, citations; per step
  when the range has several; entries added and removed again within the range.
- **Affected findings**: the changed regions of the net diff are passed to the findings
  ledger's read-only provider (below): every active finding whose citation or span
  dependency touches a changed region, with its evidence status (and basis), workflow and
  freshness as separate fields. When no ledger can be read, the section says *not
  computed*, which is not evidence that no finding is affected. Pinning the new head also
  makes every finding checked only at older pins *not current* until the ledger is
  re-anchored on it: `txray update` prints `txray findings reanchor --latest` after a new
  pin, or does it in the same run with `--reanchor` (below; see also
  [findings-memory.md](findings-memory.md), "Current").
- **Scoring-logic** items, grouped by file, each with both citations and
  the name rule (path or enclosing symbol) that decided it; **other classes, counted by
  area**; **unresolved and unknown changes** (counted by reason and area here, listed in the
  appendix, never interpreted); **intermediate history** (per step: files, lines, items by
  class) and **health** (history completeness, rename detection, symbol extraction
  coverage).

The document holds no timestamps other than committer times from git objects and no local
locations (a `file://` upstream is reported as "a local file:// mirror"), and it records
`inputs_sha256` over the tool and classifier versions, symbol backends, every commit's
manifest SHA-256 and the findings provider. The same inputs give byte-identical Markdown
and JSON (tested on the full upstream history, three concurrent runs).

### Findings provider

`timelinexray.digest.findings` defines the interface a findings source implements:

```python
class AffectedFindingsProvider(Protocol):
    name: str
    def available(self) -> bool: ...
    def affected_findings(self, regions: Sequence[ChangedRegion]) -> Sequence[AffectedFinding]: ...
```

`ChangedRegion` carries the item id, class, both commits, both paths, the changed line
range on each side and the item's hunks on each side (`(start, count)`; count 0 is an
insertion or deletion point); `AffectedFinding` carries the finding id, its citation, how it
was reached (`citation` or `span dependency`), the reason, the item ids it touches and,
separately, its evidence status with basis, its workflow state and its freshness with the
commit that freshness refers to. A provider must not append events, change statuses or
freshness, or mark anything reviewed.

`default_findings_provider(store, ledger)` (what `txray digest` and `txray update` use)
returns `LedgerFindingsProvider` for `--ledger DIR`, else `$TXRAY_FINDINGS`, else
`<store>/findings` (the `txray findings` resolution). It works as follows:

1. The ledger is read, never written: its hash chain is verified (a damaged log makes the
   section *not computed*, never "none affected"), under a shared lock of an existing
   lock file (never created); the log and `HEAD` must be regular files.
2. The changed regions are indexed by side and path. For every active finding (`draft`,
   `imported`, `reviewed`), each code citation of its latest provenance revision and each
   span dependency is placed on the digest's old and new commits, but only on a side
   where it can be affected: while its path exists at that commit, re-anchoring looks for
   the span in that path only, so a path that no region touches is skipped there
   (`coverage.skipped` counts spans skipped on both sides; nothing is read for them); a
   path absent at that commit is placed, because its bytes may have moved into a changed
   file. Placement is as cited when the citation is at that commit, otherwise by the
   Milestone 3 re-anchoring rules (the exact span bytes, found exactly once: `identical`,
   `unchanged` or `relocated`); one placement serves every finding citing the same span.
   A span that cannot be placed on any side it was tried on is counted and listed under
   `coverage.unplaced_spans` with each side's outcome (`untouched` for a side that was not
   tried); it is neither affected nor unaffected.
3. A placed span is affected when a changed hunk on that side overlaps its lines, or an
   insertion or deletion point falls strictly inside them (after line `p` with
   `A <= p < B`). The reason names the side, the changed lines and, for a citation made at
   another commit, how it was placed. A file that moved with byte-identical content
   (`cosmetic`/`moved`) or changed only its mode never counts as changed lines: a span
   placed in a moved file is listed under `relocation_candidates` instead (reason: "moved
   to ... with identical bytes; cited lines A-B are intact"), which `txray findings
   reanchor` resolves as `relocated`, `CURRENT`; a mode-only change lists nothing.
4. `freshness` is the finding's recorded freshness at the new commit when a check against
   it exists, else its latest recorded check (`freshness_checked_against` is the commit or
   `cited`); the digest never computes or changes freshness.
5. The ledger head and event count enter `inputs_sha256`, so the same ledger gives
   byte-identical digests and a changed ledger a different hash. No ledger path appears in
   the digest.

Findings that depend on an affected *finding* (not a span) are not listed; `txray findings
reanchor` marks them `STALE` (`dependency_changed`). Negative (`NOT_FOUND`) searches are
re-run by `reanchor`, not by the digest. `NullFindingsProvider` remains for callers that
want no ledger (the `DigestBuilder` default).

## Scheduled updates

`txray update --out DIR` observes the upstream once:

1. the URL is checked against the allowlist (refused: exit 3, nothing is contacted or
   created);
2. **one guarded fetch** through `timelinexray.netguard.fetch` (the only network call);
3. the branch head (`--branch`, default `main`) is read from the mirrored refs;
4. every pin of this upstream is checked: still reachable from an upstream branch or tag,
   and still present in the mirror;
5. the head is pinned (no second fetch) and compared with the previous pin: `--since`, else
   the accepted commit from `<store>/updates/state.json`, else the newest existing pin other than the head (a pin of the head
   is what an interrupted earlier run left, not a reported comparison);
6. a digest `previous..head` (the main Markdown digest, its appendix and the JSON document)
   is written into `DIR`, and `DIR/update-status.json` records the outcome, events, head,
   previous and accepted commits, the observation time (the status file, not the digest,
   carries timestamps) and `digest_summary` (items by class, parameter and registration
   changes, unknown reasons, affected findings); the command prints the same counts.

| Event (P7 section 4.4) | Detection | Response |
| --- | --- | --- |
| `fetch-failure` | the guarded fetch fails | outcome `failed`, exit 1; nothing compared; "no changes" is never claimed; pins and state kept |
| `branch-missing` | the branch is gone after the fetch | outcome `failed`, exit 1 |
| `history-rewritten` (force-push) | the previous accepted commit is not an ancestor of the new head | the head is pinned and **quarantined**, the accepted commit is unchanged, the digest compares the two trees only; outcome `attention`, exit 1 |
| `commit-unreachable-upstream` (deleted commit) | a pinned commit is reachable from no upstream branch or tag | reported, exit 1; the pin and its `refs/txray/pins/` ref are kept, so it stays citable as history |
| `pinned-commit-missing` | a pinned commit's objects are missing from the mirror (for example after the mirror was lost and re-fetched from a rewritten upstream) | outcome `failed` if it is the previous commit; exit 1; the pin record is kept |
| `license-changed` | the digest contains `license` items | promotion pauses: head quarantined, digest written, outcome `attention`, exit 1 |
| `no-new-commit` | the head equals the previous pin | outcome `no-change`, exit 0: "no new public commit observed", which does not establish that production stopped changing |

A maintainer resolves a quarantined head with `txray update --since <head>`: an explicit
`--since` equal to the head accepts it. Pins are never removed.

### Keeping the ledger fresh (`--reanchor`, `--export`)

`txray update --out DIR --ledger L --reanchor [--export VAULT_DIR]` does in one run what a
maintainer would otherwise do by hand after every new pin
(`timelinexray.digest.refresh`):

1. the guarded fetch and the pin of the new head, exactly as above;
2. every active finding of the ledger is re-anchored on the head (`verify` events, what
   `txray findings reanchor <head>` writes, actor `verifier`), *before* the digest is
   built, so the digest's affected findings show each finding's freshness at the head
   (`STALE @ head` for a changed span). On a head that was already pinned before the run
   (`no-change`, or a quarantined head observed again), only findings without a recorded
   check at it are re-anchored (findings recorded or imported since), so a daily run
   appends nothing to a ledger that is already fresh. A quarantined head (rewritten
   history, license change) is re-anchored too: the check is about the evidence at that
   commit, the quarantine about accepting it;
3. the digest, as above;
4. with `--export DIR`, the Context Layer notes are refreshed at the end
   (`txray export context-layer --out DIR`: `CURRENT` findings only; the export's
   destination rules apply and are checked before the fetch, as `--export`);
5. the review queue is compared before and after the re-anchoring. `update-status.json`
   records `ledger_refresh` (target, selection, findings re-anchored, freshness counts,
   `review_queue.before`/`after`/`added`/`removed` with finding id, trigger, priority and
   scope, the ledger head and event count), `stale_review` (the stale-review summary at the
   head: counts by freshness, area and outcome, the batch steps for unpinned cited commits
   and unchecked findings, the first ten worklist entries and the next command; see
   [findings-memory.md](findings-memory.md), "Stale review") and `export` (counts only);
   the command prints `reanchor`, `queue` (the first ten new items in the worklist order,
   parameter findings first, then `... and N more`), `review` (with the exact `txray
   findings stale` command), `unpinned` (with `txray findings verify --pin-cited`, when
   citations name unpinned commits) and `export` lines; and the exit code is 1 when new
   review items appeared (a changed span, a negative search with hits, a dependency that
   went stale), even when the observation itself was fine. Nothing is reviewed: freshness
   changes, statuses never do. (On the 275 research findings and `77d431a..78460ca`: 86
   new items, printed as ten lines plus one, where 0.10.0 printed 86.)

The ledger must exist and verify (`--ledger DIR`, else `$TXRAY_FINDINGS`, else
`<store>/findings` outside any git working tree); a missing or damaged ledger, or a refused
`--export` directory, stops the command before anything is fetched. The digest's provider
still only reads the ledger; the re-anchoring goes through the findings service (write
gate, hash chain).

### Scheduling (example only)

TimelineXray does not schedule anything itself. A crontab entry that observes once a day at
an off-hour minute, keeps the ledger fresh and the notes current, and keeps a log could look
like this (`$HOME` expands in crontab on most systems; adjust paths to your machine; drop
`--reanchor --export` to only observe):

```
# m  h  dom mon dow  command
17   6  *   *   *    TXRAY_STORE="$HOME/.cache/timelinexray" TXRAY_FINDINGS="$HOME/txray-ledger" txray update --out "$HOME/txray-digests" --reanchor --export "$HOME/notes/txray" >> "$HOME/txray-update.log" 2>&1
```

A non-zero exit means "look at `update-status.json`": an observation that failed or needs
attention, or (`ledger_refresh.review_queue.added`) new review items for
`txray findings queue`. A schedule is not a freshness guarantee: check `observed_at` and
`observation_succeeded` in the status file.

## Measured on the upstream history

Measured while writing this milestone (macOS, Python 3.12, local clone via `file://`);
reported, not promised.

| Range | Result |
| --- | --- |
| `77d431a..78460ca` (5 commits, 291 files), classifier v2 | 1,227 net items: parameter-default 25, registration 7, scoring-logic 65, model-config 162, docs-only 1, test-only 248, build-dependency 125, cosmetic 124, generated-vendored 1, unknown 469 (no-rule 459, not-parsed 10; classifier v1: 610 of 1,216). Main digest 39,991 bytes (classifier v1 and the one-file layout: 461,450), appendix 422,982, JSON 2,293,334; `PostUnexploredWeight` public default 0.02 -> 0.015 and `RetrievalCandidatesKafkaMaxCandidates` 200 -> 100000 in `home-mixer/params/param.rs` |
| `aaa167b..77d431a`, classifier v2 | 2,689 net items, unknown 1,381 (v1: 1,522 of 2,684); main digest 68,360 bytes with every row budget reached and the cut rows named, appendix 1,145,053 |
| `77d431a..78460ca`, classifier v3 | 1,236 net items: parameter-default 25, registration 7, scoring-logic 53, visibility-rule 32, model-config 163, docs-only 1, test-only 248, build-dependency 125, data-type 37, observability 5, access-modifier 11, cosmetic 124, generated-vendored 1, unknown 404 (no-rule 392, not-parsed 12). Main digest 38,940 bytes, appendix 434,543, JSON 2,306,169 |
| `aaa167b..77d431a`, classifier v3 | 2,698 net items: scoring-logic 176, data-type 10, observability 6, unknown 1,422; main digest 70,851 bytes (bounded, cuts named), appendix 1,148,942 |
| `77d431a..78460ca`, classifier v4 (scoring precedence) | 1,236 net items: scoring-logic 45, model-config 184, data-type 38, observability 6, unknown 389 (no-rule 386, not-parsed 3); other classes as in v3. Main digest 31,820 bytes (v3 in the same store: 33,583) |
| `aaa167b..77d431a`, classifier v4 (scoring precedence) | 2,698 net items: scoring-logic 137, model-config 527, unknown 1,385; main digest 38,658 bytes (v3 in the same store: 38,543) |
| `77d431a..78460ca`, classifier v4 (final) | 1,236 net items: parameter-default 25, registration 7, scoring-logic 45, visibility-rule 32, model-config 183, docs-only 1, test-only 254, build-dependency 125, data-type 38, observability 6, access-modifier 11, cosmetic 124, generated-vendored 1, unknown 384 (no-rule 381, not-parsed 3). Main digest 31,820 bytes, appendix 435,783, JSON 2,269,940 |
| `aaa167b..77d431a`, classifier v4 (final) | 2,698 net items: scoring-logic 137, model-config 527, build-dependency 209, test-only 83, unknown 1,352 (no-rule 1,241, not-parsed 110, not-text 1); main digest 38,577 bytes, appendix 1,066,008 |
| `4c5cfe8..a707cc2` (1 step, 89 files) | 381 items: parameter-default 8, registration 1, scoring-logic 16, model-config 43, docs-only 1, test-only 121, cosmetic 1, unknown 190. `ClickWeight` public default 0.4 -> 0.3 in `home-mixer/params/param.rs` (L322 -> L329) and `vm-ranker/params.rs` (L18 -> L18), with `ContClickDwellTimeWeight` 0.0 -> 0.4 and `NotInterestedWeight` -43.2 -> -47.52 in both files; about 2 s |
| `aaa167b..77d431a` (39 commits, 37 first-parent steps, one merge) | 2,164 files (2,090 added), 2,684 net items: parameter-default 210, registration 54, scoring-logic 243, model-config 466, license 4, docs-only 9, test-only 78, cosmetic 92, generated-vendored 6, unknown 1,522; timelines reproduce, for example, `ClickWeight` 0.4 at 47c1bcd -> 0.3 at a707cc2 and the reversion of `EnableAdsBrandSafetyVerdictV2` (false -> true -> false); about 30 s without an index; byte-identical across runs |
| `4c5cfe8..a707cc2` with a ledger of 478 active findings (588 cited or dependency spans, no index) | affected findings: 48 findings (83 rows); 286 spans skipped (path untouched at both commits), 268 placed, 34 not placeable; about 1.9 s against 1.7 s without a ledger (7.7 s before the spans were indexed by path, the proposals skipped and the blobs read in one git process per commit); the affected rows are byte-identical to the unfiltered run |

## Known limits

- Hunks and renames come from this tool's matcher and similarity score, not git's; counts of
  changed lines can differ slightly from `git diff --stat`.
- Classification is heuristic (names, paths, masked source, Milestone 2 symbols). Macro
  expansion, name resolution and data flow are out of scope; a value read from a
  configuration service at runtime is invisible.
- A parameter is identified by path and qualified name; the correspondence of copies in
  different files (for example the `home-mixer` and `vm-ranker` copies of a weight) is not
  asserted.
- Registration detection sees list literals only; components registered through builders,
  macros or reflection are not seen.
- Ancestry uses first parents; a commit reached only through a merge is listed and its
  change appears in the merge step, not separately.
