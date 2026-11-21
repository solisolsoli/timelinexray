# Reviewing upstream changes

The upstream repository publishes new commits without release notes; most commit messages
read "Open-source X Recommendation Algorithm". This page describes how a new upstream
snapshot is taken in, what is checked mechanically, what is read by a person, and what the
two reviews so far found. The mechanics of each command are in [updates.md](updates.md).

## The procedure

1. **Observe, guarded.** `txray update --out DIR --ledger L --reanchor` makes one guarded
   `git fetch` from the allowlisted URL, pins the new head and writes a digest against the
   previous accepted commit. Exceptional events stop promotion instead of being smoothed
   over: rewritten history (force-push) and changed licence or notice files quarantine the
   head (`attention`, exit 1); a pinned commit that disappeared upstream is reported and
   stays citable from the local pin. A maintainer accepts a quarantined head explicitly
   with `txray update --since <head>`.
2. **Licence and notice review first.** A `license-changed` event is read before the
   snapshot is used for analysis. In the 2026-10-15 review it was raised for
   `phoenix/NOTICE`, `phoenix/THIRD_PARTY_NOTICES.md` and an added licence file; the new file
   was byte-identical to the unchanged root `LICENSE`, and the notice identified a modified
   third-party subset ([report](research/upstream-2026-10-15.md#reproduction-and-notice-review)).
3. **Inventory, then reading.** The digest is a mechanical inventory (files, lines, items by
   class). It decides where to look, not what is true: a person reads the material changes
   in the code with `txray show` at both commits, and every claim in the report links to an
   immutable full-commit permalink whose span, anchor and SHA-256 were reproduced. Changed
   public defaults are cited on both sides. Copies of a parameter in different files are
   kept apart.
4. **Re-anchor and re-review the ledger.** `--reanchor` checks every active finding on the
   new head before the digest reads the ledger. Each non-current finding is read against
   the new blobs and gets one outcome: re-verified (a successor with the moved spans),
   revised (a successor with the corrected claim, both values named), historical (left not
   current, assessable as it is), retired, or left `STALE` and listed. The author of the
   re-review approves nothing; every successor waits for a different reviewer.
5. **Check the classifier on the range.** A seeded sample of the items is read by hand
   (see [classifier.md](classifier.md)); a systematic error found here becomes a classifier
   change with its own tests, as version 5 did.
6. **Add acceptance coverage, additively.** New reference citations go into a new goldens
   file for the snapshot (commit, path, lines, anchor and the span SHA-256 computed with
   plain `git show`), with acceptance tests that pin exact bytes, separate declarations and
   new indexed paths. The developer/CI fetch helper is extended to supply the new commit with
   its full history. The tool's default setup pin, the historical goldens and the semantic
   gate sets are **not** changed by a snapshot review: they stay tied to the results that
   were measured with them.

## Acceptance coverage

"Acceptance coverage" means: for a snapshot, the suite checks that TimelineXray reads the
real upstream exactly as an independent oracle does. It validates retrieval and source
identity, not the behaviour of the upstream system.

| Snapshot | Goldens | Acceptance tests | What they pin |
| --- | --- | --- | --- |
| historical pins `4c5cfe8`, `a707cc2`, `77d431a` | `goldens/citations.json` (35 citations, 14 history expectations) | `tests/test_findings_goldens.py`, `tests/test_upstream.py`, `tests/test_index_upstream.py`, `tests/test_params_upstream.py`, `tests/test_digest_upstream.py` | integrity and anchor verdicts; re-anchoring outcome, freshness and lines at later commits; index and parameter history |
| `92f0918` (2026-10-15) | `goldens/upstream-2026-10-15.json` (19: 11 at `92f0918`, 8 at `78460ca`) | `tests/test_upstream_refresh.py` | exact source bytes, new indexed paths, separate Home Mixer and VM Ranker declarations |
| `ed78e06` (2026-11-21) | `goldens/upstream-2026-11-21.json` (38: 25 at `ed78e06`, 13 at `92f0918`) | `tests/test_upstream_refresh_2026_11.py` | the same, plus the absent `ReactWithVideoWeight` and the identity pairing of the rule files |

The tests read a local clone through a `file://` URL (`scripts/fetch_test_upstream.py`
makes one: `ed78e06` with its 75-commit history). Without the clone they are skipped; with
it, `make ci` fails on any skip, so a release cannot pass while acceptance coverage is
silently missing. Goldens are immutable: changing an expected result needs an independently
reviewed source change or a documented correction ([goldens/README.md](../goldens/README.md)).

## The reviews so far

Both reports state their range, coverage, every reviewed change with permalinks, and their
limits. Claims about the upstream below are quoted from them and are about the **published
code at the named commits**; none is a statement about production.

| Review | Range | Size | Examples of what was read in the code |
| --- | --- | --- | --- |
| [2026-10-15](research/upstream-2026-10-15.md) | `78460ca..92f0918`, five commits | 316 changed paths; 12 changes reviewed | changed defaults, new scoring and model inputs, gated video paths, visibility rule changes |
| [2026-11-21](research/upstream-2026-11-21.md) | `92f0918..ed78e06`, 26 commits | 660 changed files, 3,867 classified items (v5); 12 changes reviewed; all 17 changed Home Mixer defaults cited on both sides | at `ed78e06` the published code changes seven Home Mixer value-model weight defaults while the VM Ranker copies keep theirs; it adds a second (multiplicative) fusion mode whose public default selector is `"weighted"`; it no longer registers `Brazil2026ElectionFilter` |

The 2026-11-21 review also re-reviewed the research ledger: of 423 active findings
re-anchored on `ed78e06`, 223 were not current; 117 were re-verified, 78 revised, 16 left
historical and 12 left `STALE` because they were not examined (each is listed with its
reason); 5 new findings were drafted. After it, 200 successors and new findings await a
review by a different reviewer; nothing was approved by its author.

## Limits

A review covers the changes it lists and the ledger findings, not semantic equivalence of
every changed line. The pinned source does not establish which feature switches or dynamic
values are active in production, which model artifact is served, which experiments are
assigned, or what any viewer received ([limits.md](limits.md)).
