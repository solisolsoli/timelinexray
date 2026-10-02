# Goldens

`citations.json` holds reference citations for the verifier (Milestone 3). They were taken
from research findings whose sources were checked by hand against the upstream history,
with corrected line ranges where a report's range was wrong. Each golden records only:

- `id` and `source` (the research finding id it came from; no claim text);
- `commit` (full id), `path`, `start_line`, `end_line` (1-based, inclusive) and `anchor`;
- `span_sha256`: the SHA-256 of the exact span bytes, computed with plain `git show`;
- `expected`: the integrity verdict (`INTACT`, `CHANGED`, `MISSING`) and the anchor verdict
  (`FOUND`, `FOUND_MULTIPLE`, `MISSING`; several occurrences inside the span confirm it
  too) the verifier must return;
- `history` (history goldens only): for later commits, the expected re-anchoring `outcome`,
  `freshness`, and `lines` of a current span or `proposed_lines` of a changed one.

Expected values come from an independent oracle (byte search over `git show` output), not
from running TimelineXray. Two goldens keep a report's original, wrong range on purpose: the
span is intact but its anchor is `MISSING`. The history goldens include the ClickWeight
public default of `home-mixer/params/param.rs` and `vm-ranker/params.rs`: the line
`param!(ClickWeight, f64, "rust_home_mixer_click_weight", 0.4);` at 4c5cfe8 is `changed`
(`STALE`) at a707cc2 and at 77d431a, while unchanged weight lines relocate and stay
`CURRENT`.

The test suite runs every golden against a local clone of `xai-org/x-algorithm`
(`$TXRAY_TEST_UPSTREAM`, or `../x-algorithm-upstream`), pinned through a `file://` URL
allowed by `$TXRAY_ALLOW_FILE_URLS`; nothing is fetched from the network.

Goldens are immutable: changing an expected result requires an independently reviewed
source change or a documented correction to the test itself.
