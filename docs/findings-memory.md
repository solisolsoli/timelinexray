# Findings memory (Milestone 3)

The findings memory keeps research claims about a pinned upstream commit together with
evidence that can be re-checked mechanically: every code citation is an exact byte span of
a git blob (Milestone 1), and every change to a finding is an event in an append-only log
chained with SHA-256. A newer upstream commit never silently changes what a finding says;
it can only change whether the finding's evidence still holds there.

```
txray findings add        --actor NAME (--file SPEC.json | --title ... --cite C PATH A-B ANCHOR)
txray findings import     FILE --source-label LABEL --actor NAME
txray findings list       [--workflow W] [--status S] [--freshness F] [--target C] [--label L] [--current] [--all]
txray findings show       ID [--target C]
txray findings verify     [ID ...] [--label L]
txray findings reanchor   (TARGET | --latest) [ID ...] [--strict]
txray findings review     ID --actor NAME --role reviewer --status S --rationale TEXT [--objection TEXT] [--target C]
txray findings supersede  ID --actor NAME (--by ID | --file SPEC.json) --rationale TEXT
txray findings retract    ID --actor NAME --reason TEXT
txray findings queue      [--include-imported]
txray findings export     [--format findings|research|events] [--label L] [--out FILE]
txray findings verify-log [--expect-head HASH] [--repair-head]
```

Every command takes `--store DIR`, `--ledger DIR` and `--json`; write commands take
`--expect-head HASH`. Exit codes: 0 ok; 1 failed (refused, not found, integrity problems,
`verify-log` problems, `reanchor --strict` with a finding that is not `CURRENT`); 2 invalid
arguments.

## Three separate fields

| Field | Values | Changed by |
| --- | --- | --- |
| `status` (evidence status) | `SUPPORTED`, `PARTIAL`, `NOT_FOUND`, `CONTRADICTED`, `EXTERNAL_RECHECK` | `create`/`import` set it with basis `proposed`/`reported`; only `review` sets it with basis `reviewed` |
| `freshness` | `CURRENT`, `STALE`, `UNVERIFIABLE`, `NOT_CHECKED` | `verify` events only |
| `workflow` | `draft`, `imported`, `reviewed`, `superseded`, `retracted` | `create`, `import`, `review`, `supersede`, `retract` |

`SUPPORTED` + `STALE` means "supported by its historical evidence, not safe to present as
current"; it does not mean the historical claim was false. The evidence class (`CODE`,
`PARAM_DEFAULT`, `REPO_DOC`, `OFFICIAL`, `THIRD_PARTY`, `EMPIRICAL`, `INFERENCE`) is part of
the record. Freshness is relative to a commit, and `--target C` asks about one commit.

### Current: freshness relative to the newest pin

The ledger records checks; the question a reader asks is "may I present this finding as
current?", and that is relative to the **newest pinned commit of the upstream**, not to the
commit of the finding's latest check (`timelinexray/findings/freshness.py`; the same rule in
`list`/`show`, the MCP tools and the Context Layer export):

- Without `--target`, the reading names the newest pinned commit the finding was
  re-anchored on (by committer time), else its cited commit (the integrity check), and
  lists the `newer_pins`: pins of the same upstream with a later committer time and no
  recorded check. A finding with unchecked newer pins is **not current** (`list` says
  `not current`, `show` says `current no` and names the newest unchecked pin, the MCP
  tools return `current: false` with a warning, the export leaves the note out or labels it
  `NOT CURRENT (NEWER_PIN_UNCHECKED ...)`): its evidence may have changed there and nobody
  looked. `txray findings reanchor --latest` re-anchors every active finding on the newest
  pin of the store's upstream; `txray update` prints that command after pinning a new head,
  and `txray update --reanchor` runs it in the same observation (before the digest reads
  the ledger) and reports the review-queue delta, exit 1 when new items appeared
  (see [updates.md](updates.md), "Keeping the ledger fresh").
- A finding with **external evidence only** (web sources, no code citation, dependency or
  negative search: `OFFICIAL`, `THIRD_PARTY`, an `INFERENCE` without spans) has nothing to
  re-verify. Its reading is `NOT_APPLICABLE` (never written to the ledger; the four ledger
  values stay what they are), it is current by default, and it carries the latest
  recorded retrieval date and, when its claim, limitations, note, implication or a quote
  says "recheck after", "effective", "valid until", "expires" or "in force from" with an
  ISO date, a `recheck_after` hint. `EXTERNAL_RECHECK` stays its evidence status.
  `list --freshness NOT_APPLICABLE` and `find_findings freshness=["NOT_APPLICABLE"]`
  select them.
- `current` is therefore: active, and either `CURRENT` with no unchecked newer pin, or
  `NOT_APPLICABLE`. `list --current` shows exactly what the MCP tools and the export list
  by default. With `--target C` the reading is the recorded check at C and `newer_pins` is
  empty: a historical answer names its commit.

## Records

A record is written once, inside a `create`, `import` or `supersede` event, and never
edited; a correction is a new finding that supersedes the old one.

| Field | Content |
| --- | --- |
| `finding_id` | Stable id: given, or `F-` plus 12 hex digits of the record hash; imports use `LABEL:REPORT_ID` |
| `title`, `claim`, `component` | Text; the claim should be one falsifiable sentence |
| `evidence_class`, `status` | See above; `status` here is the proposed or reported one |
| `scope` | `public_code`, `public_default`, `historical` or `external`; there is no production scope. `PARAM_DEFAULT` findings always have `public_default` |
| `sources` | Code citations (below) and web sources (`url`, `publisher`, `published` with `published_precision` day/month/year/unknown, `retrieved`, `quote`); web sources are recorded, never fetched |
| `dependencies` | `{"kind": "finding", "finding_id"}` or `{"kind": "span", "citation"}` |
| `negative` | For a `NOT_FOUND` claim: the recorded search (below) |
| `limitations`, `attributes`, `origin` | Author limitations; imported report attributes; how the record was made |

A code citation is the Milestone 1 span record plus its anchor: `commit` (full id), `path`,
`start_line`, `end_line` (1-based, inclusive), `start_byte`, `end_byte`, `blob_oid`,
`blob_sha256`, `span_sha256` and `anchor`. Imported citations also keep the report's own
`reported.commit` and `reported.lines` strings and a `resolution` reason when the span could
not be read at import time (for example `commit_not_pinned`).

Public defaults are not production values: `show` prints every `PARAM_DEFAULT` finding with
"public default at" and its commit.

## The event log

A ledger is a directory:

```
events.jsonl   one event per line (schemas/findings-event.schema.json)
HEAD           {"hash": ..., "seq": ...} of the last event
.lock          advisory lock of writers
```

Each event is canonical JSON (sorted keys, no whitespace, ASCII escapes) with `schema`,
`seq` (1, 2, ...), `type` (`create`, `verify`, `review`, `supersede`, `retract`,
`import`), `time` (UTC), `actor` (`name`, `role`), `finding_id`, `payload`, `prev` and
`hash` = SHA-256 of the canonical JSON of the event without `hash`. `prev` is the previous
event's hash, 64 zeros for the first.

`txray findings verify-log` recomputes everything and reports, by line: a changed event
(`hash_mismatch`), removed, inserted or reordered events (`seq_mismatch`, `chain_broken`),
a truncated tail (`truncated`: `HEAD` names more events than the log holds), an interrupted
write (`torn_write`), events appended without `HEAD` (`head_mismatch`), a missing `HEAD`,
non-canonical lines, and with `--expect-head` a head other than an independently kept one.
Every command replays the log through the same check and refuses to read or extend a log
that fails it.

Writers take an exclusive `flock` on `.lock`, verify the chain, check `--expect-head` if
given (compare-and-swap), append all events of one operation in one write followed by
`fsync`, then replace `HEAD` atomically. An operation appends all of its events or none.

Limits: a hash chain is not a signature. Someone who rewrites the whole log and `HEAD`
consistently is not detected; keep a copy of the head elsewhere and compare it with
`--expect-head`. Actor names are declared, not authenticated. The log records when each
action happened; it is a record of actions, not a derived artifact, so it is the one place
where wall-clock time is part of hashed content.

Replay re-enforces the write rules (defence in depth against a consistent rewrite): a
`review` by the finding's own author, or a `review` that confirms `SUPPORTED` or `PARTIAL`
for a finding with code citations (or `NOT_FOUND` backed by a negative search) without
naming the finding's latest integrity check as `CURRENT`, makes the whole log fail
verification.

### Recovery

A writer appends the events of one operation, `fsync`s them, and only then replaces
`HEAD`. A crash or a killed process between those two steps (for example during a long
`txray findings import`) leaves an intact log with a `HEAD` behind it: `verify-log` reports
`head_mismatch`, and every command refuses the ledger until it is repaired.

```
txray findings verify-log --repair-head
```

rewrites `HEAD` from the verified log in exactly that case: every event must verify
(hashes, `seq`, `prev`, canonical lines, no torn write) and `HEAD` must be missing,
malformed or name fewer events than the log holds. It prints what changed (`repair HEAD
rewritten: ... (HEAD was seq N, now seq M)`) and exits 0 when the ledger then verifies.
It is refused, and nothing is written, for every other problem: a changed event
(`hash_mismatch`), removed, inserted or reordered events, a torn write, a `HEAD` that
names more events than the log (`truncated`: events were removed, which no `HEAD` can
restore) or the same number of events under another hash (the last event may have been
replaced). Those need a backup or an independently kept copy; a hash chain cannot
distinguish a repair from a rewrite, so `--repair-head` never touches the events file.

### Where the ledger lives

`--ledger DIR`, else `$TXRAY_FINDINGS`, else `<store>/findings`. The default is refused
when it would lie inside a git working tree (any ancestor with a `.git` entry), so findings
never land in a project checkout unless a location is given explicitly.

## Verification: integrity at the cited commit

`txray findings verify` re-reads every cited span from its git blob at its own commit:

| Verdict | Meaning |
| --- | --- |
| `INTACT` | readable, and its span SHA-256, byte range and blob equal the recorded values (a citation without recorded hashes is `INTACT` with `baseline`: observed now) |
| `CHANGED` | readable, but the bytes differ from what the citation records |
| `MISSING` | not readable: `invalid_commit`, `commit_not_pinned`, `path_missing`, `not_a_text_blob`, `invalid_path` or `invalid_range` |

A citation that could not be read when its record was written (an imported citation of a
commit that was not pinned: `resolution: commit_not_pinned`, null hashes) and reads now is
resolved by `verify`: the observed span (hashes, blob, byte range) is appended as a
provenance revision at the cited commits (`commit: null`, mode `integrity`), the record
itself is never changed, and `show`, the MCP tools and the export display the resolved form
(`show` marks it `resolved by a later verify`, with a hash and a permalink where the
export has one). Re-anchoring checks made before the resolution were made from the
unresolved citation and no longer count: `verify` reports them as `recheck_targets` and
prints the `reanchor` command to run again. `verify --label L` restricts the check to the
findings imported with label `L`.

The anchor verdict (`FOUND` once in the span, `FOUND_MULTIPLE` several times in the span,
`MISSING`) is reported beside it, with the line of every occurrence. A citation is usable
when it is `INTACT` and its anchor is `FOUND` or `FOUND_MULTIPLE`: an anchor that occurs
one or more times inside the cited lines confirms the content at the finding's own commit.
Uniqueness is required only for relocation to another commit (next section), where the
exact span bytes must occur exactly once; an anchor occurrence is never used to pick a
location. The finding's freshness at
its cited commits is `CURRENT` when all citations and dependencies are usable and a
recorded negative search still has no hits, `STALE` when bytes changed, and `UNVERIFIABLE`
otherwise. Verification never changes the status: a matching hash is text identity, not
proof that a claim is true.

## Re-anchoring on a newer commit

`txray findings reanchor TARGET` checks each active finding against a pinned target commit
(P7 section 3.3). Each citation, taken from the latest provenance revision, is looked up
byte for byte in the target:

| Situation at the target | Outcome | Freshness | Queue trigger |
| --- | --- | --- | --- |
| same path, same blob | `identical` | `CURRENT` | - |
| same path, exact span bytes once at the same lines | `unchanged` | `CURRENT` | - |
| exact span bytes once at other lines, or (path gone) in exactly one other file | `relocated` | `CURRENT` | - |
| exact span bytes more than once | `ambiguous` | `UNVERIFIABLE` | `ambiguous_span` |
| path present, exact span bytes absent | `changed` | `STALE` | `changed_span` |
| path gone, bytes found nowhere else | `missing` | `UNVERIFIABLE` | `missing_span` |
| citation not usable at its own commit | `unusable` | `UNVERIFIABLE` | `citation_unusable` (unless already queued by `verify`) |

- A match must start at a line start and end at a line end (a whole line range under the
  span contract). Of several matches none is chosen (never a first match); all are listed
  as candidates and the outcome is `ambiguous` (`UNVERIFIABLE`) with a reason naming the
  number of occurrences. This is the only place where uniqueness is required.
- While the path exists, the bytes are only looked up in that path. Other files are
  searched only when the path is gone: by identical blob id (a pure rename, no index
  needed), then, when the target is indexed, through the code index (a literal search for
  the anchor's longest line; its hits are the only lexically indexed files that can
  contain the span). Files the lexical index skips (binary, generated, oversized, not
  UTF-8) are not searched: a `missing` reason says "the lexically indexed files of the
  target" with the skipped files counted by reason (`search.lexical_coverage`). A
  truncated index search is reported as incomplete, never as a unique match.
- For a `changed` span, a line diff of the two blobs (`difflib`, junk heuristic off)
  proposes the corresponding region, and the review item carries the old citation, the
  proposal with its span SHA-256 and anchor verdict, the anchor's occurrences in the target
  file, a unified diff and the claim. A proposal never makes a finding current.
- When every citation is current and at least one moved, the verify event appends a new
  provenance revision (the target commit and the relocated citations); the original
  citation stays in the record. The next re-anchoring starts from the latest revision.
- Dependencies: a span dependency is re-anchored the same way; a finding dependency is
  checked first in the same run (dependencies before dependents; a cycle is
  `UNVERIFIABLE`). A dependency that is `STALE`, superseded or retracted makes the finding
  `STALE` (`dependency_changed`) even when its own spans are unchanged.
- Negative findings (`NOT_FOUND`) record their search: query, literal flag, path glob, the
  index generation (manifest SHA-256, backends, counts) and the coverage of the searched
  scope (paths in scope, paths searched, languages searched, skipped paths by reason). The
  search is re-run at the target: new hits make the finding `STALE`
  (`negative_now_found`, with the hits); a target without an index is `UNVERIFIABLE`
  (`negative_unverifiable`).
- The finding's freshness is the worst of its parts: `STALE` before `UNVERIFIABLE` before
  `CURRENT`.

## Review, supersession and retraction

- `review` records an assessed status with a rationale and optional objections; it is the
  only event that sets status basis `reviewed`. It closes the queue items of the scopes it
  assessed: the cited commits, plus the re-anchoring target named with `--target C` (which
  must have a recorded check). Items of other targets, for example a `changed_span` found
  by re-anchoring on a newer commit, stay open, so a finding that is `STALE` at a newer
  pin stays in the queue after its historical evidence is confirmed. The actor needs role
  `reviewer` or `maintainer`.
- No self-approval: the reviewer's name (compared case-insensitively) must differ from the
  actor that created the finding (`create`, `import` or the `supersede` that introduced
  it). This catches the same declared actor approving its own work; it is not an identity
  system.
- Merge condition: confirming `SUPPORTED` or `PARTIAL` for a finding with code citations
  (or `NOT_FOUND` backed by a negative search) requires a current integrity check at the
  cited commits. Other statuses do not.
- `supersede ID --by OTHER` marks a finding superseded by an existing active finding;
  `supersede ID --file SPEC` creates the successor through the write gate in the same
  event. `retract ID --reason` withdraws a finding. Superseded and retracted findings keep
  their history and are never reviewed again.

## Review queue

Items come from `verify` events (triggers) and from drafts awaiting review; a new check of
the same scope (the cited commits, or one target commit) replaces that scope's items;
`supersede` or `retract` closes all items of the finding, and `review` closes the items of
the scopes it assessed (the cited commits, plus `--target C`). A review event without
`closed_scopes` (written by the released 0.7.0 or earlier) closed every scope and still
does when the log is replayed.

| Priority | Triggers |
| --- | --- |
| 1 | `changed_span`, `citation_changed`, `negative_now_found` |
| 2 | `dependency_changed`, `ambiguous_span`, `missing_span`, `citation_unusable` |
| 3 | `negative_unverifiable`, `awaiting_review` (drafts) |
| 4 | `awaiting_review_imported` (only with `--include-imported`) |

Allowed actions for every item: review, supersede with a revised finding citing the target,
retract, or leave it unresolved.

## Adding a finding: the write gate

`txray findings add --file SPEC.json` (or the equivalent flags) takes:

```json
{
  "finding_id": "optional",
  "title": "Click weight public default",
  "claim": "At <commit> the public default of ClickWeight is <value>.",
  "component": "home-mixer",
  "evidence_class": "PARAM_DEFAULT",
  "status": "SUPPORTED",
  "scope": "public_default",
  "citations": [{"commit": "<7-40 hex>", "path": "<path>", "lines": "A-B", "anchor": "<text>",
                 "span_sha256": "optional expected hash"}],
  "web_sources": [{"url": "...", "publisher": "...", "published": null, "retrieved": "YYYY-MM-DD", "quote": ""}],
  "depends_on": [{"finding": "<id>"}, {"span": {"commit": "...", "path": "...", "lines": "A-B", "anchor": "..."}}],
  "negative": {"commit": "...", "query": "...", "literal": false, "path": "<GLOB>"},
  "limitations": ["..."]
}
```

The gate reads every cited span from its git blob: it must be readable with its anchor
inside the span (`FOUND` or `FOUND_MULTIPLE`; `MISSING` is refused), and an expected
`span_sha256` must match. `CODE`, `PARAM_DEFAULT` and
`REPO_DOC` claims need a code citation, except a `NOT_FOUND` claim, which needs a negative
search with zero hits at an indexed commit. A new finding is a `draft` with status basis
`proposed`.

## Research import

`txray findings import FILE --source-label LABEL --actor NAME` reads the format described
by `schemas/research-import.schema.json` (the findings format of the research reports):
an object whose only key `findings` lists findings with exactly `id`, `title`, `claim`,
`component`, `evidence_class`, `status`, `sources`, `creator_relevance`,
`creator_controllable`, `implication`, `volatility`, `misuse_risk` and `note`. The file may
be JSON, YAML, or Markdown with exactly one fenced block starting with `findings:`.

- YAML is read by a strict reader of the block-style subset such files use (mappings,
  sequences, plain, single- and double-quoted scalars over several lines, comments, `[]`,
  `{}`, null). Plain scalars other than null stay strings (`no` is `"no"`). Anchors,
  aliases, tags, block scalars, non-empty flow collections and duplicate keys are errors.
- A file with any schema problem imports nothing; the problems are listed.
- Each finding becomes `LABEL:ID` in workflow `imported` with the report's status and basis
  `reported`. Nothing is promoted: only a review can confirm a status.
- Code sources are resolved against pinned commits where possible, and a `verify` event
  records their integrity and anchor verdicts right after the import. An anchor that occurs
  more than once inside its span is `FOUND_MULTIPLE` and confirms the span like `FOUND`;
  an anchor `MISSING` from its span makes the imported finding `UNVERIFIABLE` until it is
  superseded by a corrected citation.
- The report lists the distinct commits the store has not pinned (`unpinned_commits`,
  citations per commit) with the command that resolves them (`hint`: `txray pin <commits>`,
  then `txray findings verify --label LABEL`); the human output prints them as `unpinned`
  and `next`. Resolving later never edits the record (see "Verification").
- Importing the same file again is a no-op (identity: the SHA-256 of each finding's
  canonical JSON); changed content under an existing id is refused (supersede instead).
- `txray findings export --format research --label LABEL` reconstructs the imported data
  exactly (round trip).

`schemas/research-import.example.yaml` is a small synthetic example that cites the test
suite's fixture repository. The repository ships no research data; real reports are
imported from wherever they are kept, into a ledger outside the repository.

## Goldens

`goldens/citations.json` holds reference citations taken from findings whose sources were
checked by hand: commit, path, line range, anchor, the span SHA-256 and the expected
verifier verdicts, plus, for history goldens, the expected outcome, freshness and line
range (or proposed range) at later commits. They hold no claims. The test suite runs them
against a local clone of the upstream (see `goldens/README.md`).

## Known limits

- The line diff that proposes a region for a changed span is heuristic and only a proposal;
  files over 50,000 lines are not diffed.
- Other files are searched for a moved span only when its path is gone, and only through
  the index when the target is indexed; otherwise only byte-identical renamed files are
  found.
- Dependencies are declared by authors; nothing infers them.
- Web sources are recorded as given; the tool never fetches them. The `recheck_after` hint
  of a finding with external evidence only is parsed from its recorded text; a source
  whose terms change on a date the record does not name gets no hint.
- "Newer" between pins is committer time; pins with equal committer times are not ordered,
  so neither counts as newer than the other.
