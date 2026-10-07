# Semantic release gate

`questions.json` is the reviewed question set of P7 section 7.1 (status: **root-reviewed
2026-10-01**); `run_gate.py` checks it through the MCP server without any model; `live_gate.py`
runs it through a real agent and scores the replies by rule. The thresholds and the
measured numbers are in [docs/release-checklist.md](../docs/release-checklist.md).

## The question set

- 40 items: 31 `answer` items and 9 `abstain` items. Each item has `id`, `kind`, `source`
  (the research finding it was derived from, or `null`), `evidence_class`, `commit` (the
  full pinned commit the question is asked at), `question` (English) and `expected`.
- Answer items were derived **only** from research findings whose root check is recorded
  as non-empty in the maintainer's verification ledger (outside this repository); the
  finding id is `source`. Their `expected` holds `answer_regexes` (every pattern must match
  the reply, case-insensitive; numbers as public defaults, signs included) and
  `citations`: commit, path, 1-based inclusive lines, an anchor (<= 80 characters) and the
  SHA-256 of the exact span bytes computed with plain `git show` (the same oracle as
  `goldens/citations.json`; where a golden exists for the source, the citation overlaps
  it). A citation may name the `lookup` that must find it: `get_param` with the expected
  `value`, or `find_symbols` with `kind` and `path_prefix`.
- Abstain items ask for facts that are not in the public code: production or live values,
  per-viewer and per-account data, revenue and payout rates, reach predictions, model
  weights, suppression that nothing in the code shows, and an external official document
  (derived from the OFFICIAL finding `P5-067`). Their `expected` holds the
  `abstention_reason` and `probes`: `search_code` queries that must return zero hits at the
  pinned commit. The probes were checked against the index before the live run (one probe
  of A09 was replaced because it hit a README sentence about how weights work; the
  question did not change).
- No claim text and no report prose: `tests/test_eval_gate.py` enforces the member set,
  the size (25-40), the kinds, the anchors, the regexes and the golden overlap.
- The set is immutable in the sense of the goldens: changing an expected result requires an
  independently reviewed source change or a documented correction to the item. The agent
  being evaluated never edits it. Any change made after seeing live results must be
  recorded in the release checklist with its reason.

## Set revisions

A revision is a new file; an older revision is never edited, so a live result stays tied to
the exact set it was scored with (the live report records the file, the revision and the
file's SHA-256).

| Revision | File | Status | Live runs |
| --- | --- | --- | --- |
| 1 | `questions.json` | root-reviewed 2026-10-01 | 2026-10-01 and 2026-10-02 (`live-run-2026-10-0*.txt`), both below the precision threshold |
| 2 | `questions-v2.json` | root-reviewed 2026-10-07 | 2026-10-07 contract r1 (`live-run-2026-10-07.txt`): abstention 7/9, fails; 2026-10-07 contract r2 (`live-run-2026-10-07-contract-r2.txt`): **passes** |

A revision file carries a top-level `revision` record: its number, date, the base revision
(file, number, SHA-256, status), the thresholds (unchanged) and one change per item with the
fields it changed (`question`, `expected.answer_regexes`, `expected.citations`, ...), the
basis (a live-run analysis or a reading of the item) and the reason, plus the values before
and after. `tests/test_eval_gate.py` pins the SHA-256 of revision 1, fails when revision 2
differs from it anywhere except in the listed fields, keeps every existing expected citation,
and checks each changed pattern against synthetic replies: correct replies that revision 1
rejected are accepted, wrong replies are still rejected, and replies revision 1 accepted are
still accepted.

Revision 2 changes 16 answer items and no question text, abstain item, probe or threshold:
Q22 gains the second occurrence of `initialTweetId.getOrElse` (line 227) as an expected
citation and asks only for the initial post id, and Q05 accepts the prose forms of its
conditions (both from the root analysis of the second live run); by reading, Q02 and Q04 no
longer require an order their questions do not ask for, identifier patterns (Q01, Q03, Q20,
Q21) accept a space or hyphen, Q25, Q27 and Q29 accept a thousands separator, and the negative
weights (Q15-Q19) accept an en dash or "negative". The reasons are in the file.

## Abstention development set (not the gate)

`dev-abstain.json` (`"purpose": "development"`) is a separate, clearly labelled set for
developing the agent contract's abstention rules (R1-R4, `docs/agents/README.md`) without
touching or tuning against the gate sets above, which stay byte-identical (their SHA-256 is
pinned by `tests/test_agent_contract.py`). It was written on 2026-10-07 before any live run.

- 14 abstain items with new wording in three families: `per-request` (a model score,
  probability, dwell or feed position for one viewer and post, reach of one account),
  `live-config` (the production state of a feature switch or decider, a live threshold,
  yesterday's metrics, the cluster that serves most users) and `moderation` (shadowban or
  penalty claims the code does not state). Each has probes that must return zero hits.
- 12 answerable controls next to those families (`control-*`): yes/no moderation questions
  the code does answer (a filter that drops muted or blocked authors; one that keeps every
  in-network post; a "no" backed by `unwrap_or(false)`), public defaults next to the
  "production value" questions, and per-request mechanisms (the holdout bucket's inputs).
  They were written by reading the code at `77d431a`; no research finding is behind them,
  so `source` is `null` (allowed only in a development set). A contract that abstains on
  everything loses these.
- `run_gate.py --questions eval/dev-abstain.json` checks its citations and probes exactly
  like the gate's (78 checks); `tests/test_agent_contract.py` does so in `make ci`.
- `live_gate.py --questions eval/dev-abstain.json` runs and scores it like the gate, adds
  per-family results, turns per kind and `abstentions_with_scope` (rule R4), and prints no
  gate verdict. `--contract r1 --server-src <src of 0c4df26>` reproduces the contract of the
  2026-10-07 run (prompt, reply schema, server instructions and tool descriptions) for a
  before/after comparison; the commands and expected costs are in
  `docs/release-checklist.md`, section 3a, "Abstention development set".

## Deterministic harness (part of `make ci`)

```sh
TXRAY_ALLOW_FILE_URLS=file:///path/to/x-algorithm-upstream \
txray pin 4c5cfe8f07f1c76d4f04277e803f20e6039f5191 --upstream file:///path/to/x-algorithm-upstream --store "$STORE"
txray pin a707cc27ba36d3fa79450c9cffcc48a82d080b02 --upstream ... --store "$STORE"
txray pin 77d431aabf409ca1c1eed9bec7e2183f7c914e23 --upstream ... --store "$STORE"
txray index 77d431aabf409ca1c1eed9bec7e2183f7c914e23 --store "$STORE"
python eval/run_gate.py --store "$STORE" [--json report.json]                     # revision 2 (default)
python eval/run_gate.py --store "$STORE" --questions eval/questions.json        # revision 1
```

For every expected citation: `read_span` at the pinned commit must return `OK`, the span
SHA-256 must equal the oracle hash, the anchor verdict must be `FOUND`, the expected value
must occur in the span text, and the named lookup (`get_param` value and citation,
`find_symbols` span) must find the same span. For every abstention probe: `search_code`
must return zero hits (the search scope, including the unsearchable paths of the manifest,
is reported). Exit 0 only when every check passes; the report lists each denominator.
`tests/test_eval_gate.py::DeterministicGateUpstreamTest` runs the same harness on a
temporary store built from the local upstream clone, so `make ci` (with
`TXRAY_TEST_UPSTREAM` set) fails when a citation, a value or a probe no longer holds.

## Live harness (run by hand before a release)

```sh
python eval/live_gate.py --store "$STORE" --out "$OUT" --questions eval/questions-v2.json \
    --model haiku --budget-usd 3.00
```

Pass `--questions` explicitly so the report names the revision that was run. `--contract`
(`r2`, the default, with rules R1-R4; `r1`, the contract of the 2026-10-02 and 2026-10-07
runs) is recorded in the report; the reply schema of `r2` adds `searched` (the queries an
abstention ran), which is reported and never scored.

Each item becomes one `claude -p` call: `--strict-mcp-config` with a generated
configuration for `txray mcp serve` on the store, `--tools ""` (no built-in tools),
`--allowedTools "mcp__txray__*,mcp__txray"`, a fixed system prompt (the evidence rules),
`--output-format json` with a `--json-schema` for the reply (`kind`, `answer`,
`citations`, `reason`), `--max-turns` and `--max-budget-usd` per item. The harness stops
before the hard total budget would be exceeded and lists the items not run.

Scoring (no judgement by a model): an answer item is `correct` only when the reply answers,
one of its citations overlaps an expected citation at the pinned commit (same path,
intersecting lines, cited commit a prefix of the expected one, span readable through
`read_span`) and every `answer_regex` matches the answer text; otherwise `wrong_value`
(overlap, value wrong), `uncited` (no overlapping span) or `false_abstention`. An abstain
item is `correct_abstention` when the reply abstains, else `unsupported_answer`. Replies
that do not parse are `error`. Metrics with denominators: precision = correct / all
answers given (including unsupported answers on abstain items); coverage = correct /
answer items run (and over the whole set); abstention accuracy = correct abstentions /
abstain items run; false-abstention rate; citation integrity = cited spans that read back
/ cited spans. The report records the date, the model actually used, the client version,
turns, tokens and the total cost.
