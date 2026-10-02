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

## Deterministic harness (part of `make ci`)

```sh
TXRAY_ALLOW_FILE_URLS=file:///path/to/x-algorithm-upstream \
txray pin 4c5cfe8f07f1c76d4f04277e803f20e6039f5191 --upstream file:///path/to/x-algorithm-upstream --store "$STORE"
txray pin a707cc27ba36d3fa79450c9cffcc48a82d080b02 --upstream ... --store "$STORE"
txray pin 77d431aabf409ca1c1eed9bec7e2183f7c914e23 --upstream ... --store "$STORE"
txray index 77d431aabf409ca1c1eed9bec7e2183f7c914e23 --store "$STORE"
python eval/run_gate.py --store "$STORE" [--json report.json]
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
python eval/live_gate.py --store "$STORE" --out "$OUT" --model haiku --budget-usd 3.00
```

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
