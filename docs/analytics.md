# Offline creator metrics

Milestone 5a. Descriptive measurements from a user's **own** post-level analytics export,
following the metric design of the P6 research report ("From the Algorithm to an Honest
Creator Metric", research date 2026-09-30, pinned upstream
`77d431aabf409ca1c1eed9bec7e2183f7c914e23`).

Every metric output starts with the P6 notice, verbatim:

> **This is not a reach prediction.** These measurements summarize your exported analytics
> using a documented historical baseline and, where specified, public reference
> coefficients. They do not reconstruct X’s personalized ranking score, observe missing
> negative feedback, or estimate impressions lost to visibility restrictions.

There is no "algorithm score", no reach prediction, no percentage of "algorithm coverage",
no shadowban estimate and no checklist score. Missing values are unknown, never zero.

## Commands

```sh
txray metrics import EXPORT.csv --schema x-post-v1 --lang tr|en --out DIR \
    --captured-at 2026-09-30T18:00:00+09:00 [--counts cumulative|window|unknown] \
    [--scope organic|promoted|combined|unknown] [--number-locale en|tr] \
    [--source-timezone ZONE] [--date-format FORMAT]
txray metrics import EXPORT.csv --schema x-post-v1 --lang tr|en --dump-header [--json]
txray metrics rwe DIR [--variant core|extended|sensitivity] [--attribution-validated] \
    [--horizon 24h [--horizon-tolerance 2h]] [--scope S] [--post ID ...] [--json]
txray metrics reach DIR --horizon 24h [--horizon-tolerance 2h] [--timezone ZONE] \
    [--band-hours 6] [--kappa 5] [--min-parent 5] [--scope S] [--json]
```

`--store DIR` (or `TXRAY_STORE`) names the snapshot store so the analytics commands can stay
out of it. Exit codes are those of `txray`: 0 ok, 1 failed (rejected export, dataset
conflict, write refused), 2 invalid arguments, 3 network refused.

## The offline process

`txray metrics ...` runs in its own process. Before any analytics module other than the
guard itself is imported, `timelinexray.analytics.offline.enter` installs a CPython audit
hook (`sys.addaudithook`), which cannot be removed for the rest of the process:

- every socket event (creation, name resolution, connect, bind, send) raises
  `OfflineViolation` (exit code 3);
- process creation (`subprocess`, `os.system`, `os.exec*`, `os.posix_spawn`, `os.fork`) and
  `ctypes` are refused the same way;
- file writes are allowed only inside the `--out` directory of `metrics import`, nowhere for
  `metrics import --dump-header`, `rwe` and `reach`, and never inside the snapshot store;
  `--out` overlapping the store is refused before anything is written;
- the writers whose files that check cannot see are refused outright: every
  `sqlite3.connect` (SQLite creates its database, journal and `ATTACH`-ed files without an
  audited `open`) and the import of the `dbm` C modules `_dbm` and `_gdbm`;
- the bytecode cache is switched off, so imports write nothing.

The analytics package imports nothing from the rest of TimelineXray except `errors` and the
version string (never the snapshot store, `netguard`, `gitio` or the future MCP server),
and no other module imports the analytics package; `timelinexray/metrics_cli.py` only
declares the command line and imports analytics code inside its handler, after `enter`.
The test suite checks all of this statically, and runs the commands in child processes to
show that a socket, a connection, a name lookup, a process and an unexpected write all fail
after a metrics command ran, and that the guard event precedes the import of every other
analytics module.

Audit hooks observe and veto what Python code does; they are not a sandbox against hostile
native code. The analytics code is pure Python and uses none of the blocked operations.

## Import contract (P6 section 2.2)

| P6 requirement | Implementation |
| --- | --- |
| Localized headers | Versioned alias table per (schema, language): `x-post-v1/en/v1`, `x-post-v1/tr/v1`. The original header row and the column-to-field mapping are stored with every import. |
| Ambiguous translations | No global dictionary. A header is interpreted only by the table of the declared schema and language; unmatched headers are kept as "unmapped". A header row that matches the other language fails with a hint instead of being translated. |
| Turkish text | Unicode NFC, collapsed whitespace, case folding with Turkish dotted/dotless i rules for the Turkish table; the original spelling is never replaced. |
| IDs | Post ids stay exact digit strings; scientific notation (a spreadsheet rounding) is rejected. |
| Dates and time zones | The raw date text is stored. Values with an offset keep it; values without one are placed in `--source-timezone` (default UTC); daylight-saving gaps and folds are recorded as `nonexistent-local-time` / `ambiguous-local-time`, never resolved silently. `reach` derives a separate analysis-local time in `--timezone`. |
| Numeric localization | Counts are parsed with the declared locale (`--number-locale`, default the header language): `en` groups with `,`, `tr` with `.`; fractions, signs and abbreviations are rejected. |
| Missing versus zero | Each count is `observed` (with value), `blank` (empty cell) or `missing-column`. Only `observed` enters a metric; anything else makes the dependent value undefined. |
| Post versus account grain | `x-post-v1` is post grain. Exports without a post id column (daily account rows) are rejected; nothing is ever distributed across posts. |
| Observation age | Each snapshot records its capture time (`--captured-at`, with offset), the count kind (`--counts`) and the scope (`--scope`); outputs show the observation age. |
| Repeated exports | Snapshots are keyed by (post, capture time, scope). Re-importing the same file is a no-op, an identical snapshot from another file is counted as already present, a conflicting one is refused, and cumulative snapshots are never summed. Decreasing cumulative counts are reported, not clipped. |
| Zero impressions | Every impression-normalized value is undefined (`zero-impressions`); no denominator is substituted. |

Post text and post links are not stored: only a SHA-256 of the text and whether the link
points at the row's post id. Dataset files are written with mode 0600 in a 0700 directory,
and `dataset.json` records the SHA-256 of `snapshots.jsonl`, checked on every load.

## Schema `x-post-v1`

Post-level ("content") CSV export of the X analytics web interface, one row per post.

| Canonical field | Kind | English header (`EXTERNAL_RECHECK`) |
| --- | --- | --- |
| `post_id` | id | Post id |
| `created_at` | timestamp | Date |
| `text` | text (hashed only) | Post text |
| `url` | link (checked only) | Post Link |
| `impressions` | count | Impressions |
| `likes` | count | Likes |
| `engagements` | count (aggregate; never added to its components) | Engagements |
| `bookmarks` | count (no value-model coefficient found) | Bookmarks |
| `shares` | count (generic; never weighted) | Shares |
| `new_follows` | count | New follows |
| `replies` | count | Replies |
| `reposts` | count | Reposts |
| `profile_visits` | count | Profile visits |
| `detail_expands` | count | Detail Expands |
| `url_clicks` | count | URL Clicks |
| `hashtag_clicks` | count | Hashtag Clicks |
| `permalink_clicks` | count | Permalink Clicks |

The Turkish header row is the row exported by the X analytics web interface in Turkish,
transcribed as text (no export file was opened or copied). It is stored as alias data in
`src/timelinexray/analytics/schema.py`, written with Unicode escapes so that repository text
stays English; the tests pin it by the SHA-256 of its UTF-8 bytes
(`56e239160bb5d75e0af670671df4fe18232b5c651d416e46bac7504aa9e3d48f`, 336 bytes) and compare
two independent transcriptions. Its status is `SUPPORTED` as transcribed text. The English
names were written from the documented column set, aligned one to one with the Turkish
row, and have not been checked against a real English export: status `EXTERNAL_RECHECK`,
reported as a warning on every English import.

To check a table against an export without sharing data, run `txray metrics import
EXPORT.csv --schema x-post-v1 --lang en --dump-header`: it prints the header row as
exported, each name normalised, the field it maps to, the unmapped names, the missing
fields and how many columns each language's table would map; it reads no data row (a
malformed data row is not even noticed) and writes nothing. The confirmation is recorded
as a finding, not in code: `OFFICIAL` when the names come from X's documentation,
`EMPIRICAL` when they come from the dumped header of a real export. A table's status
becomes `SUPPORTED` only by naming that finding's id in `HeaderAliases.confirmed_by`
(`src/timelinexray/analytics/schema.py`); the Turkish table names the hash pin of its
transcribed row, and `tests/test_analytics_import.py` refuses a `SUPPORTED` table without
either.

The date column format of real exports is not documented here. ISO 8601 is read by default;
another format must be declared with `--date-format` (a `strptime` pattern). Date-only
values are kept with date precision and are excluded from horizon-based metrics.

## M1: realized weighted engagement (P6 section 4)

```
RWE(v, O)_i(h) = 1000 * sum over a in O of w_a(v) * C_ia(h) / I_i(h)
```

Unit: **reference-weighted observed events per 1,000 recorded impressions**. Worked example
(P6, hypothetical): 10,000 impressions, 200 likes, 10 replies, 15 reposts give
`1000 * (0.5*200 + 5*10 + 15) / 10000 = 16.5`.

| Variant | Action set O | Condition |
| --- | --- | --- |
| `core` (default) | likes x favorite (0.5), replies x reply (5.0, base), reposts x retweet (1.0) | none |
| `extended` | core + URL clicks x open_link (0.2) + new follows x follow_author (4.0) | `--attribution-validated`: the user states that both columns are attributed to the post; the tool records that it did not verify this |
| `sensitivity` | core + detail expands x click (0.3) | approximate mapping; never a default |

Generic shares, engagements, bookmarks, account-level follows and inferred dwell are never
weighted. Arithmetic is exact (rational numbers) and rounded half-even to six decimals only
for output. Each post shows every component's count, rate per 1,000 impressions,
contribution and the change one more event would cause (`1000 * w / I`), plus unweighted
rates of every exported action. The five negative terms (not interested, block, mute,
report, not dwelled) have no export column and are shown as `unknown`; the partial sum is not
a bound on any full score. Aggregates report the pooled rate (sum of weighted counts over
sum of impressions) separately from the mean and median post values. By default the latest
snapshot of each post is used and its observation age is shown; `--horizon` selects the
snapshot at a common age instead.

### Coefficient table

`src/timelinexray/analytics/coefficients.py`, table `x-algorithm-value-model-public-defaults`
version 1: every value is a **public default** at
`77d431aabf409ca1c1eed9bec7e2183f7c914e23`, `home-mixer/params/param.rs` (file annotation:
"mirrored from config feature-switch defaults; last sync 2026-09-29T17:02:52Z"). Public
defaults are not production values; in production the coefficients weight predicted
probabilities, not exported counts (upstream README, lines 349-351 and 408-412).

| Head | Public default | Export column | Mapping |
| --- | --- | --- | --- |
| favorite_score | 0.5 | likes | exact (core) |
| reply_score | 5.0 (+15.0 mutual-follow boost, viewer-dependent, not applied) | replies | exact (core, base) |
| retweet_score | 1.0 | reposts | exact (core) |
| photo_expand_score | 0.05 | none | none |
| video_open_score | 0.07 | none | none |
| click_score | 0.3 | detail_expands | approximate (sensitivity) |
| open_link_score | 0.2 | url_clicks | exact (extended, attribution) |
| profile_click_score | 0.0 | profile_visits | approximate, not weighted |
| vqv_score | 0.0 | none | none |
| share_score | 2.0 | shares | approximate, never weighted |
| share_via_dm_score | 5.0 | none | none |
| share_via_copy_link_score | 20.0 | none | none |
| dwell_score | 0.05 | none | none |
| quote_score | 5.0 | none | none |
| quoted_click_score | 0.05 | none | none |
| quoted_vqv_score | 0.0 | none | none |
| dwell_time | 0.004 | none | none |
| click_dwell_time | 0.4 | none | none |
| follow_author_score | 4.0 | new_follows | approximate (extended, attribution) |
| not_interested_score | -47.52 | none | unknown |
| block_author_score | -31.2 | none | unknown |
| mute_author_score | -58.8 | none | unknown |
| report_score | -234.0 | none | unknown |
| not_dwelled_score | -0.02 | none | unknown |
| post_unexplored_score | 0.02 | none | none |

`tests/test_analytics_coefficients.py` pins the upstream commit from a local clone through a
`file://` URL allowed by `TXRAY_ALLOW_FILE_URLS` and re-reads, with the snapshot span reader:
every `param!` declaration (name, feature switch, literal), the order and weight expressions
of the 25 terms in `compute_weighted_score`, the adapter that fills each field from its
parameter, the boost functions and eligibility predicate, the sync annotation, and the two
README statements; it then recomputes the worked example from the values read from source.

## M2: relative reach (P6 section 5)

For post i published at t_i, with impressions I_i(h) at a common horizon h:

1. baseline observations are the other posts whose horizon snapshot was **captured before
   t_i** (only information available before publication);
2. `z_j = log(1 + I_j(h))`; bucket b = weekday and time band of t_i in `--timezone`
   (`--band-hours`, default 6); `m_b` = median of z in the bucket (n_b posts), `m_p` = median
   over all baseline posts (n_p);
3. shrinkage heuristic: `lambda_b = n_b / (n_b + kappa)`,
   `m~_b = lambda_b m_b + (1 - lambda_b) m_p` (`--kappa`, default 5);
4. `B = exp(m~_b) - 1`; `RR = I_i(h) / B`, **undefined when B = 0**;
5. `LR = log(1 + I_i(h)) - m~_b`, `RR+ = exp(LR) = (1 + I_i(h)) / (1 + B)`, always defined
   when a baseline exists.

RR and RR+ are separate fields and never substituted for each other. The effective sample
size is the plain count n_b (dependence between posts is not modelled). A baseline needs at
least `--min-parent` posts (default 5). The horizon snapshot is the earliest one with counts
declared cumulative and observation age in [h, h + tolerance] (default tolerance: a tenth of
h, at least one hour); its count can exceed the count at exactly h. Output per post:
publication time (UTC and analysis-local), bucket, I(h), snapshot age, baseline value,
medians, n_b, n_p, lambda, kappa, the baseline window, RR, RR+, LR and the reason for every
undefined value. kappa, the band width and the minimum parent size are declared tool
settings, not validated values. Uncertainty (block resampling) is not computed.

A same-horizon RR contains its own outcome: correlating RR with I(h) is not evidence of
predictive validity (P6-018). A multiple of one account's baseline is not comparable across
accounts.

## M3 and M4: data structures only (P6 section 6)

`src/timelinexray/analytics/checks.py` defines records, no computation:

- `ChecklistItem` (M3): question, what the source establishes, evidence status, evidence
  class, freshness, citations, honest interpretation. `ChecklistAnswer` holds the separate
  applicability (`pass`, `fail`, `unknown`, `not-applicable`) chosen by the user for one
  proposed post. No field holds a score, points, percentage or probability, and nothing
  aggregates the items; there are no points for asking for engagement.
- `VisibilityFlag` (M4): the report's own wording, scope (account label, post-label
  aggregate, identified post), time (covered period, generation, retrieval, completeness),
  code relationship (label, rule, commit, cited predicate), context, and a conclusion from a
  closed vocabulary (reported observation, possible effect under stated conditions,
  insufficient information). Remediation is limited to reviewing accurate labeling or an
  applicable appeal. Labels are never summed or turned into lost impressions.

Evidence status of the shipped items (from P6, imported and not reviewed in TimelineXray;
freshness `NOT_CHECKED` until the findings ledger re-verifies them; the tests confirm only
that each cited span exists with its anchor at `77d431a`):

| Item | Source | Status / class |
| --- | --- | --- |
| M3 audience/access | `home-mixer/filters/ineligible_subscription_filter.rs` 14-29 | SUPPORTED / CODE |
| M3 post type | `xai-value-model/inputs.rs` 17-21 | SUPPORTED / CODE |
| M3 labels with scope and date | `visibility-filtering/rules/registry.rs` 268-287 | SUPPORTED / CODE |
| M3 authentic engagement | X Help, Authenticity (retrieved by P6 on 2026-09-30) | SUPPORTED / OFFICIAL in P6; not rechecked here |
| M4 report month selection | `under-the-hood/strato/columns/underTheHoodReport.User.strato` 152-165 | SUPPORTED / CODE |
| M4 label denominators (posts, days) | same file, 261-263 and 297-299 | SUPPORTED / CODE |

## P6 coverage

Implemented: the §2.2 import contract (one schema, two header languages), §3 mapping table,
§4.1 M1 with core/extended/sensitivity variants, §4.3 negative terms as unknown, §4.4 exact
counts with one-event sensitivity and pooled versus mean rates, §5.1 M2 with RR and RR+,
§10 reporting sections (identity, data quality, outcomes, M1, M2, provenance) and the
verbatim notice, §11.1 provenance (coefficient commit, values, head mask, mapping version,
definition version, source hashes, time zones, horizon, calculation time).

Documentation only: §6 M3/M4 (data structures, no workflow), §4.4 and §5.2 uncertainty
(block resampling), §7 alternative models, §8 validation and backtesting protocol, §9
randomized experiment and power (HYPOTHETICAL planning numbers: about 454 posts per arm, or
227 matched pairs at rho 0.5, for a 25% geometric-mean change with log SD 1.2), §11.2
fixed-reference versus native-reference series and the reweighting decomposition, §11.3
update protocol.
