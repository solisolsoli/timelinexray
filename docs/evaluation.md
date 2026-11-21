# Evaluation

TimelineXray makes two kinds of promise. The first is mechanical: a citation reads back the
same bytes, a changed span is flagged, an index agrees with a clean rebuild. The second is
about use: an AI agent connected through the MCP server answers from the code with correct
citations, and abstains when the code cannot answer. This page describes how each is
measured, the thresholds, and every result so far. The complete record, including the
analysis of each failed run, is [release-checklist.md](release-checklist.md); the harnesses
are in [`eval/`](../eval/README.md).

## Mechanical gates (every `make ci`)

| Gate | Target | Result | Evidence |
| --- | --- | --- | --- |
| Citation integrity on the reference citations | 100 % | 35 / 35 citations with the expected integrity and anchor verdicts; 14 / 14 history expectations | `tests/test_findings_goldens.py` |
| Staleness on a controlled mutation suite | 100 % detected | 7 / 7 invalidating mutations flagged; 0 / 4 false positives on harmless changes | `tests/test_findings_reanchor.py` |
| Mutation kinds of the specification covered | all | 9 / 9 (insertion, rename, duplicate anchor, changed literal, deleted symbol, changed dependency, new hit for a negative finding, incomplete history, force-push) | reanchor, digest, update and security tests |
| Incremental index equals a clean rebuild | exact | 2 / 2 synthetic and 2 / 2 upstream transitions | `tests/test_index.py`, `tests/test_index_upstream.py` |
| Analytics egress (optional metrics feature) | zero leakage | 0 / 4 channels contained a canary | `tests/test_security.py`, `tests/test_analytics_offline.py` |
| Deterministic semantic-gate harness | every check | 237 / 237 on set revision 2 | `eval/run_gate.py`, `tests/test_eval_gate.py` |

The deterministic harness is the model-free half of the semantic gate: for every expected
citation of the question set it checks that `read_span` returns the span, that its SHA-256
equals the oracle hash, that the anchor is found, that the expected value is inside the
span, and that `get_param` / `find_symbols` find the same span; for every abstention item
it checks that its probe searches return zero hits. If the upstream clone is present,
`make ci` runs it and fails when any citation, value or probe no longer holds.

## The semantic gate (by hand, before a release)

### The question set

`eval/questions-v2.json` (revision 2 of a reviewed set): 40 questions about the published
code at pinned commits.

- **31 answer items**, each derived only from a research finding with a recorded root check:
  pipeline structure, value-model weight defaults, retrieval budgets and caps, one parameter
  history item. Each has expected citations (commit, path, lines, anchor, span SHA-256
  computed with plain `git show`) and answer patterns.
- **9 abstain items** that ask for what the code does not contain: production and live
  values, per-viewer and per-account data, revenue and payout rates, a reach prediction,
  suppression that nothing in the code shows, model weights and an external document.

The set is fixed before a run. A revision is a new file; an older revision is never edited,
and a test pins the SHA-256 of revision 1. A change made after seeing live results must be
listed with its reason. Rules for agents (R1-R4) are developed on a **separate** development
set (`eval/dev-abstain.json`, 14 abstain items and 12 answerable controls), never by tuning
on the gate set.

### Scoring, by rule

Each item is one `claude -p` call with only the TimelineXray MCP server, no built-in tools,
a fixed system prompt and a JSON reply schema (`eval/live_gate.py`). No model judges the
replies:

- an answer is **correct** only when one of its citations overlaps an expected citation at
  the pinned commit (same path, intersecting lines, readable through `read_span`) and every
  answer pattern matches; otherwise `wrong_value`, `uncited` or `false_abstention`;
- an abstain item is a **correct abstention** only when the agent abstains; any answer
  counts as `unsupported_answer`, and also against precision.

| Metric | Threshold |
| --- | --- |
| Semantic citation precision (correct / all answers given) | >= 0.95 |
| Coverage (correct / answer items) | >= 0.80 |
| Abstention accuracy (correct abstentions / abstain items) | 1.00 |
| False-abstention rate | <= 0.20 |
| Citation integrity of the agent's citations | 1.00 |
| Completeness (every item run, no parse errors) | every item |

The thresholds were written down on 2026-10-01, before the first live run, and were never
changed.

### Every run

All runs: Claude Code with model `claude-haiku-4-5-20251001`, `--strict-mcp-config`, USD
0.20 per item, a store with `4c5cfe8`, `a707cc2` and `77d431a` pinned and indexed; every item
run exactly once.

| Run | Set, contract | Precision | Coverage | Abstention | False abst. | Integrity | Complete | Gate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-10-01 | rev. 1, r1 (15 turns) | 27/30 = 0.900 | 27/31 | 5/9 | 0/31 | 50/50 | 5 at turn limit | **fail** |
| 2026-10-02 | rev. 1, r1 | 29/31 = 0.935 | 29/31 | 9/9 | 0/31 | 51/51 | 40/40 | **fail** |
| 2026-10-07 | rev. 2, r1 | 31/32 = 0.969 | 31/31 | 7/9 | 0/31 | 53/53 | 1 error | **fail** |
| 2026-10-07 | rev. 2, r2 | 30/30 = 1.000 | 30/31 | 9/9 | 1/31 | 48/48 | 40/40 | **pass** |

Per-item reports: `eval/live-run-2026-10-01.txt`, `eval/live-run-2026-10-02.txt`,
`eval/live-run-2026-10-07.txt`, `eval/live-run-2026-10-07-contract-r2.txt`.

What changed between runs, and why:

- After run 1, the four questions (Q01-Q04) that said "For You post pipeline" were made to
  name the pipeline the expected answers concern (the agent had read a different pipeline), the
  harness prompt stated two rules the agent contract already held, and the turn limit was
  treated as a client limit (15 -> 25). No threshold, expected citation or abstain item
  changed.
- After run 2, set revision 2 accepted correct forms the patterns had rejected (a second
  occurrence of the cited code, prose instead of identifiers, a thousands separator); each
  change is recorded in the file with its basis. Run 2 was not re-scored.
- After run 3, the agent contract gained rules R1-R4, developed and measured on the separate
  development set: both contracts scored 26/26 there, and r2 cut the turns on abstain items
  from 233 to 81 and the cost from USD 1.37 to USD 0.68. Run 4 used the unchanged set.

**What the pass means.** It is one run of one model. Earlier runs of the same set varied
(abstention 9/9, then 7/9), so it shows the gate can be met, not that every run meets it.
The one miss of run 4 was a false abstention on a question whose answer exists at two
places in the code.

## What has not been measured

- **The current `main`.** The MCP tool descriptions and published schemas were tightened
  after the passing run (the rules R1-R4 themselves are unchanged and pinned by a test). The
  passing result belongs to version 0.11.0; the next release needs a new live run before it
  is tagged ([CHANGELOG](../CHANGELOG.md)).
- **Other models and clients.** Only one model through one client was run; other clients
  are listed as untested in the [client matrix](mcp.md#client-matrix).
- **Questions at newer commits.** The gate sets are tied to `4c5cfe8`, `a707cc2` and
  `77d431a`; the 2026-10-15 and 2026-11-21 snapshots are covered by acceptance goldens
  (mechanical), not by semantic questions.
- **The classifier.** Its precision is measured by seeded hand checks, not by a gate; see
  [classifier.md](classifier.md).

## Reproducing

```sh
python scripts/fetch_test_upstream.py ../x-algorithm-upstream     # once; the public upstream over https
TXRAY_TEST_UPSTREAM=../x-algorithm-upstream make ci                # mechanical gates, no skips
python eval/run_gate.py --store "$STORE" --questions eval/questions-v2.json   # model-free harness
python eval/live_gate.py --store "$STORE" --out "$OUT" --questions eval/questions-v2.json \
    --model haiku --budget-usd 3.00                                # live run: needs the claude CLI and costs about USD 1-2
```

The store for the two harnesses holds `4c5cfe8`, `a707cc2` and `77d431a`, pinned from the
clone, with `77d431a` indexed; the exact commands are in [eval/README.md](../eval/README.md).
