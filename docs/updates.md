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
items of several classes. Rules, in order:

| Class | Rule | Must not be read as |
| --- | --- | --- |
| `generated-vendored` / `unknown` | an entry that is not text on either side (manifest reason generated/vendored, else unknown with "not compared as text") | a content change of any kind |
| `license` | a license or notice file (`LICENSE*`, `NOTICE*`, `COPYING*`, ...), or a hunk whose every changed line is license header text | a determination of the applicable license |
| `generated-vendored` | the manifest says `generated` or `vendored` | reviewed upstream code |
| `docs-only` | documentation files (`.md`, `.rst`, `.txt`, `docs/`, `README*`, ...) | a change of behaviour |
| `test-only` | test files (`tests/`, `golden_corpus/`, `*_test.rs`, `test_*.py`, `*Test.java`, ...), or hunks entirely inside test code (Rust `#[cfg(test)]` modules and `#[test]` functions, Java `@Test` methods, Python `test_*`/`Test*`) | a change of production code |
| `parameter-default` | the value of a `param!(Name, type, "flag", value)` declaration, a `const`/`static` or Java `static final` with a literal value, a literal Java/Scala field, a Python `UPPER_CASE` constant with a literal value, or a key of a YAML/TOML/INI/JSON config file differs between the two cited declarations; `param!` declarations added or removed | a production value: every value is a **public default** at its commit |
| `registration` | entries of a list that registers components were added, removed or reordered, or such a list appeared or disappeared: a `vec![...]`, `Seq(...)`, `List.of(...)` or Python list whose entries name components (`...Filter`, `...Source`, `...Hydrator`, `...Scorer`, `...SideEffect`, `...Rule`, ...) or that is bound to a name like `filters`, `sources`, `side_effects`, `rules` | that a registered component is active for any request |
| `cosmetic` | the comment- and whitespace-insensitive tokens of both sides are equal (string literals kept whole; indentation kept for Python and YAML), or a file moved with identical content | semantic equivalence where parser coverage is incomplete |
| `scoring-logic` | other changed code whose path, or else enclosing symbol, names scoring, weights or ranking (`scor`, `weight`, `rank`, `boost`, `penalt`, `decay`, `diversit`, `blend`, ...) | that the change alters any ranking outcome |
| `model-config` | other changed code or configuration under a model, feature, config, schema, proto, thrift, inference or training location | that a model artifact was deployed |
| `unknown` | everything else | anything: it needs review |

Values are compared as their source text with comments removed and whitespace collapsed.
A constant computed by code (for example `Duration::from_secs(compute())`) is logic, not a
default. Adjacent hunks of the same class inside the same symbol form one item.

## The digest

`txray digest old new` (or `timelinexray.digest.DigestBuilder(store).build(old, new)`)
builds one JSON document (`timelinexray/digest/v1`) and renders it as Markdown:

- **Statements**: generated mechanically from diffs; commit messages never read; every
  value is a *public default* at the cited commit, not a production value; nothing has been
  posted; classification is heuristic and unreviewed; how to check a citation; the
  community-analysis disclaimer.
- **Range**: both commits with committer times and manifest SHA-256; the relationship
  (`ancestor`, `same`, `reversed`, `diverged`, `unrelated-mirrors`); the first-parent
  chain; commits reached only through merges (their changes appear in the merge step).
- **Exceptional events** (see below), or an explicit "none detected".
- **Summary by class**: net items (old -> new) and items across all first-parent steps.
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
- **Other classified changes**, then **unresolved and unknown changes** (listed, never
  interpreted), **intermediate history** (per step: files, lines, items by class) and
  **health** (history completeness, rename detection, symbol extraction coverage).

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
6. a digest `previous..head` (Markdown and JSON) is written into `DIR`, and
   `DIR/update-status.json` records the outcome, events, head, previous and accepted
   commits and the observation time (the status file, not the digest, carries timestamps).

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
   scope, the ledger head and event count) and `export` (counts only); the command prints
   `reanchor`, `queue` and `export` lines; and the exit code is 1 when new review items
   appeared (a changed span, a negative search with hits, a dependency that went stale),
   even when the observation itself was fine. Nothing is reviewed: freshness changes,
   statuses never do.

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
