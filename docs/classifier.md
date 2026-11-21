# The change classifier

When the upstream publishes new commits, the first question is "what changed, and where
should a reader look?". `txray diff`, `txray digest` and `txray update` answer it
mechanically: they compare two pinned commits, cut the changes into items, cite each item
on both sides, and sort it into a class. This page explains the method, the five classifier
versions, and what the measured precision means for a reader. The full rule table is in
[updates.md](updates.md#classes); the code is `timelinexray/diff/` (`engine.py`, `rules.py`,
`analysis.py`, `content.py`).

## What it reads, and what it never reads

- **Only git objects of pinned commits.** Two verified manifests are compared path by path
  (added, removed, modified, renamed); hunks are computed from blob bytes with the same line
  numbering as `txray show`, so every hunk line is a valid citation on its side.
- **Never commit messages.** The commit parser reads only the tree, parent and committer
  fields. Most upstream commit messages read "Open-source X Recommendation Algorithm", and a
  message is a statement about the code, not the code.
- **Never the upstream code as a program.** Nothing is built, imported or executed.

Each **item** carries an old and a new citation (commit, path, lines, span SHA-256, blob
id), the innermost declarations enclosing the change on each side (from the code index),
and its class with the rule that decided it.

## Classes, and how not to read them

A class names what a rule saw, not a reviewed interpretation. The most-read ones:

| Class | The rule saw | Must not be read as |
| --- | --- | --- |
| `parameter-default` | the literal value of a `param!` declaration, a constant, or a config-file key differs between the two cited declarations | a production value: every value is a public default at its commit |
| `registration` | entries of a list that registers components (`...Filter`, `...Scorer`, `...Hydrator`, ...) were added, removed or reordered | that a registered component is active for any request |
| `scoring-logic` | changed code whose path or enclosing symbol has a whole word such as `score`, `weight`, `rank`, `boost`, `penalty`, `blend` | that the change alters any ranking outcome |
| `visibility-rule` | every changed line lies inside a visibility rule definition | that any post's visibility changed |
| `model-config` | changed code under a model, kernel, training, config or schema location | that a model artifact was deployed |
| `observability`, `data-type`, `access-modifier`, `cosmetic` | logging/metric statements only; struct or enum fields only; visibility modifiers only; tokens equal without comments and whitespace | that behaviour is unchanged |
| `docs-only`, `test-only`, `build-dependency`, `license`, `generated-vendored` | a path or content class | a change of production behaviour (or, for `license`, a licence determination) |
| `unknown` | nothing matched; each item says why (`no-rule`, `not-parsed`, `not-text`, `mode-only`, `empty-file`) | anything: it needs review |

Rules are applied in a fixed order; precise classes (tests, kernels, telemetry, data types)
are decided before the broad scoring-name rule. A digest groups items by class, puts
parameter defaults, registrations and scoring changes in a short main digest with
citations, and lists everything else in an appendix and the complete JSON.

## Five versions, each from a measured error

A rule is kept only after a seeded hand check: a fixed random seed draws a sample of the
items a rule moves, and every sampled hunk is read at both commits. Each version fixed
errors such a check found. Item ids change with the classifier version.

| Version | Change | Evidence that led to it |
| --- | --- | --- |
| 1 | path and name rules; `unknown` items without a reason | first version |
| 2 | `build-dependency`, `train` locations and test helper names; every `unknown` item says why | hand check on `77d431a..78460ca` (seeds 20261007, 20261008; 42 items): build-file rule 1/1, import rule 22/22, `train` location 18/18, test helper names 1/1 correct |
| 3 | scoring names as whole words; four content rules (`observability`, `access-modifier`, `visibility-rule`, `data-type`); PageRank and RankAll excluded; a test function no longer decides a production line | 85 sampled `scoring-logic` items: five clear, testable error kinds; all 21 sampled items the fix moved out were wrong |
| 4 | precise classes win over the scoring name: kernel and training code, telemetry and debug names, `observability` and `data-type` hunks; `#[cfg(test)]` items are test code; empty files are not mode changes | 44 sampled items (seed 20261008): all 12 sampled items that moved were wrong, and all 47 moved items in both ranges were checked |
| 5 | YAML list items are keyed by their `id` or `name`, not by position | 2026-11-21 review: all 76 v4 `parameter-default` items of two abuse-enforcement rule files were positional pairings of different rules |

A rule that was tried and **rejected** is recorded too: a `filter`/`visib`/`safety` name
rule would have moved 37 sampled items, of which only 10 were rule or policy logic, so the
name says where code lives, not what it does. Those items stay `unknown`.

## Measured precision, and what it means

Correct = "the changed lines compute, combine, weight, threshold or order candidate scores";
adjacent = score plumbing or feed assembly (ads blending, ranker request payloads); wrong =
anything else. Strict precision counts only correct items; lenient counts correct and
adjacent.

| `scoring-logic` check | Sample | Strict | Lenient |
| --- | --- | --- | --- |
| v3 -> v4, recent range `77d431a..78460ca` | 22 of 53 items, seed 20261008 | 0.41 -> 0.47 | 0.68 -> 0.79 |
| v3 -> v4, full history `aaa167b..77d431a` | 22 of 176 items, seed 20261008 | 0.09 -> 0.15 | 0.36 -> 0.62 |
| v4 and v5 (same `scoring-logic` items), `92f0918..ed78e06` | 20 items (12 path, 8 symbol), seed 20261121 | 0.20 | 0.55 |

Read `scoring-logic` as **"worth a look"**, never as "changes scoring". A name is weak
evidence of what code does; on the newest range the remaining errors were test code in a
mixed hunk, client and Kafka wiring, statistics side effects whose telemetry word sits in
the type name, an abuse-model score and a model weight download
([upstream review](research/upstream-2026-11-21.md#classifier-check-on-this-range)).

`parameter-default` is much more reliable, because it compares parsed declarations: on
`92f0918..ed78e06` all 123 `param!` items agreed with an independent parse of both
parameter files, and all 16 `const` items matched their rule. The one systematic error
found (positional YAML pairing, 76 items) is what version 5 fixed; on the same files it now
reports the one real change, a `ttl_msec` of 2592000000 -> 604800000.

`unknown` stays large on purpose. On `77d431a..78460ca` it is 384 of 1,236 items under v4;
over the initial publication history it is mostly whole added files of new subsystems,
which no line rule can honestly classify. A digest counts unknown items by area so that a
busy subsystem still shows.

Item counts by class for every measured range and version are in
[updates.md](updates.md#measured-on-the-upstream-history).

## Limits

- Classification is heuristic: names, paths, masked source and lexical symbols. No macro
  expansion, name resolution or data flow; a value read from a configuration service at
  runtime is invisible.
- A parameter is identified by path and qualified name. The correspondence of copies in
  different files (for example the Home Mixer and VM Ranker copies of a weight, which at
  `ed78e06` hold different public defaults) is not asserted.
- Registration detection sees list literals only; components registered through builders,
  macros or reflection are not seen.
- Hunks come from this tool's matcher, not git's; line counts can differ slightly from
  `git diff --stat`.
