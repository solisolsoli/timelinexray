# Release checklist: 0.9.0

Version 0.6.0 completed Milestone 6 (release hardening) and with it all six milestones;
0.7.0 added the optional Context Layer export; 0.9.0 adds the fixes of an end-to-end
audit, parameter tools and the semantic release gate (section 3a). The owner
approved publishing the repository on 2026-10-02 although the semantic gate has not
passed; the repository is public and the README states the measured gate results.
**The first live run of the semantic gate failed; the `v0.9.0` tag and a GitHub release
wait for a passing run.** Version 0.10.0 (one-command install: `install.sh`, `txray setup`)
changed no gate-relevant code; its `v0.10.0` tag waits for the same passing run.

Sections 1 and 3a were run for 0.9.0. Section 2 is the full clean-install walk of 0.6.0,
kept as the last full walk; for 0.9.0 the build and clean-venv install inside `make ci`
ran (`build check passed: timelinexray 0.9.0`).

Environment of the walk: macOS 26, Python 3.12.4, git 2.54.0, 2026-10-01. Local locations
are written as `$WORK` (a fresh temporary directory) and `$UPSTREAM` (a local clone of
`xai-org/x-algorithm` holding `77d431aabf409ca1c1eed9bec7e2183f7c914e23` with its 39
commits of history).

## 1. Checks

| Check | Command | Observed |
| --- | --- | --- |
| Everything CI runs (0.10.0) | same | exit 0 (workflow check ok; hygiene 19 tests; 704 tests, OK, 1 skipped: the optional live Context Layer test; build check passed: `timelinexray 0.10.0`, including the real `install.sh` install and uninstall) |
| Everything CI runs (0.9.0) | `TXRAY_TEST_UPSTREAM=$UPSTREAM make ci; echo $?` | exit 0 (lint incl. `workflow check: ok` and the SQLite preflight; hygiene tests; 654 tests, OK, 1 skipped: the optional live Context Layer test, the only skip `make ci` allows; build check passed: `timelinexray 0.9.0`) |
| Everything CI runs (0.6.0) | same | exit 0; 535 tests with no skip allowed; build check passed |
| Workflow checker | `python scripts/check_workflows.py` | `workflow check: ok` |
| Privacy scan of every commit of every branch (maintainer-side script kept outside the repository) | - | clean |
| Publishing hygiene of the branch to be pushed | `python scripts/check_release_history.py --fsck BRANCH; echo $?` | exit 0 required: +0000 offsets, project identity, no trailers, no personal data in messages, no dangling objects |

## 2. Clean-install walk

`python scripts/build_check.py --full` with `TXRAY_TEST_UPSTREAM` set runs these commands
and checks their results; the transcript below is its output (timings from this machine,
not promised).

| # | Command | Observed |
| --- | --- | --- |
| 1 | `python3.12 -m venv $WORK/build-venv` | exit 0 |
| 2 | `$WORK/build-venv/bin/python -m pip install 'setuptools>=77'` | exit 0; setuptools 84.0.0 (downloaded from PyPI: the only download of the walk) |
| 3 | `$WORK/build-venv/bin/python -c 'setuptools.build_meta.build_sdist($WORK/dist)'` (in a copy of the working tree) | exit 0; `timelinexray-0.6.0.tar.gz`, 364,570 bytes |
| 4 | `$WORK/build-venv/bin/python -c 'setuptools.build_meta.build_wheel($WORK/dist)'` | exit 0; `timelinexray-0.6.0-py3-none-any.whl`, 281,682 bytes |
| 5 | content check of both archives (since 0.9.0 the sdist contains no `tests/`: `MANIFEST.in`; `build_check.py` fails otherwise) | sdist has `LICENSE`, `NOTICE`, `pyproject.toml`, `README.md` and the sources; the wheel has only `timelinexray/` and its dist-info with `licenses/LICENSE`, `licenses/NOTICE`, `License-Expression: Apache-2.0`, `Requires-Python: >=3.11`; no `scripts/` or `tests/` |
| 6 | `python3.12 -m venv $WORK/install-venv` | exit 0 (a fresh environment) |
| 7 | `$WORK/install-venv/bin/python -m pip install --no-index --no-deps $WORK/dist/timelinexray-0.6.0-py3-none-any.whl` | exit 0 (offline); `timelinexray` imports from the new environment, not from the source tree |
| 8 | `txray --version` | `txray 0.6.0` |
| 8a | `python -m timelinexray --version` (in the install venv) | `txray 0.10.0` (the module entry point agrees with the console script) |
| 9 | `txray mcp tools --json` | 10 tools: `list_commits`, `resolve_commit`, `manifest_summary`, `read_span`, `search_code`, `find_symbols`, `index_coverage`, `find_findings`, `get_finding`, `verify_claim` (34,122 bytes compact) |
| 10 | `txray pin 77d431aabf409ca1c1eed9bec7e2183f7c914e23 --upstream file://$UPSTREAM --store $WORK/store` (with `TXRAY_ALLOW_FILE_URLS=file://$UPSTREAM`) | exit 0 in 1.1 s |
| 11 | `txray manifest 77d431a --summary --json --store $WORK/store` | 2,147 paths: parsed-candidate 1,836, text 304, excluded 7 |
| 12 | `txray show 77d431a home-mixer/candidate_pipeline/phoenix_candidate_pipeline.rs --lines 322-330 --anchor 'let sources:' --json --store $WORK/store` | span SHA-256 `3cf0a5c059666a047f55d0724fb0dd9ddd870a9f95b7e81aa5c7da3b127e8683` (golden G01), anchor `FOUND` |
| 13 | `txray index 77d431a --json --store $WORK/store` | exit 0 in 10.9 s: 2,147 files, 2,140 indexed lexically (skipped: generated 5, binary 1, symlink 1), 1,836 parsed, 50,387 symbols, 151,726 call candidates |
| 14 | `txray search 77d431a PhoenixScorer --limit 3 --json --store $WORK/store` | 12 hits; first `README.md:127` |

| 15 | `sh install.sh --method venv --source $WORK/checkout-copy` (a copy of the checkout; `TXRAY_HOME` and `TXRAY_BIN_DIR` in `$WORK`; only with `TXRAY_TEST_UPSTREAM`; pip builds the copy and downloads `setuptools>=77` again) | exit 0; the `txray` link exists |
| 16 | `$WORK/checkout-bin/txray setup --commit 77d431a... --upstream file://$UPSTREAM --store $WORK/setup-store --no-index --json` (with `TXRAY_ALLOW_FILE_URLS`) | exit 0; the report names the commit |
| 17 | `$WORK/checkout-bin/txray setup --print-mcp-config json --store $WORK/setup-store --json` | `command` is the linked txray path |
| 18 | `sh install.sh --uninstall --method venv` | exit 0; the link and the venv are gone |

Steps 8a and 15-18 were added after 0.10.0's walk above was recorded; the table keeps the
0.6.0 timings of steps 1-14 and shows no timing for the new ones. Result: `build check passed:
timelinexray 0.6.0` (exit 0) for the recorded walk. `make ci` runs the same script
without `--full` (steps 1-12, 8a and 15-18 when the upstream clone is available).

## 3. Release gates (P7 section 7.3), with denominators

| Gate | Target | Result | Evidence |
| --- | --- | --- | --- |
| Mechanical citation integrity on release goldens | 100 % | 35 / 35 citations with the expected integrity and anchor verdicts; 14 / 14 history expectations (re-anchoring outcome, freshness, lines) | `tests/test_findings_goldens.py` against the upstream clone |
| Known mutation staleness detection | 100 % on the controlled suite | 7 / 7 invalidating mutations flagged not `CURRENT` (changed literal, bytes surviving only inside a longer line, duplicated span, deleted file, changed span dependency, changed finding dependency, new hit for a negative search); 0 / 4 false positives (line shift, pure rename, rename with an unrelated edit, unrelated edit stay `CURRENT`); plus the multi-occurrence anchor case (unique relocation `CURRENT`, duplicate `UNVERIFIABLE`) | `tests/test_findings_reanchor.py` |
| P7 section 7.4 mutation kinds covered | all | 9 / 9: line insertion, rename, duplicate anchor, changed literal, deleted symbol (as a deleted file; a symbol deleted inside a kept file is a changed span), unchanged span with changed dependency, new files matching a negative finding, incomplete history (digest `history_complete: false` for non-ancestor ranges), force-push (update drills, including an unrelated history) | `tests/test_findings_reanchor.py`, `tests/test_digest.py`, `tests/test_update.py`, `tests/test_security.py` |
| Semantic citation precision | >= 95 % on a reviewed set | **29 / 31 = 0.935 at the second live run (2026-10-02), 27 / 30 = 0.900 at the first (2026-10-01): below target, gate failed both times**; at 0.6.0 it was not measured | section 3a |
| Unanswerable production-value questions | no unsupported numeric answers | **not applicable as a measurement**: TimelineXray generates no answers; every number it shows is labelled a public default at a commit (enforced by the digest, findings and MCP tests) | `tests/test_digest.py`, `tests/test_mcp.py` |
| Analytics egress | zero canary leakage | 0 / 4 channels contained the canary text or link (metrics stdout/stderr, the dataset directory, the snapshot store, `tools/list`); the MCP server refuses a store containing the dataset; sockets, process creation and native code are blocked in the metrics process | `tests/test_security.py::AnalyticsEgressTest`, `tests/test_analytics_offline.py` |
| Incremental index correctness | exact agreement with clean rebuilds | 2 / 2 synthetic commits and 2 / 2 upstream transitions (a707cc2 to 77d431a and back) equal to clean builds | `tests/test_index.py`, `tests/test_index_upstream.py` |

## 3a. Semantic release gate (audit section 6 item 5; P7 sections 7.1-7.3)

Added in 0.9.0. The question set and the thresholds were root-reviewed on 2026-10-01
before the live run (see "Root review" below); the first live run failed the gate.

### Question set

`eval/questions.json`, status `root-reviewed 2026-10-01`: 40 items, 31 `answer` and 9 `abstain`
(`eval/README.md`). Answer items were derived only from research findings with a recorded
root check (ids in `source`): 5 CODE items on the pipeline structure (P1-013, 015, 016, 018,
028), 14 PARAM_DEFAULT weight items (P2-011, 012, 013, 016, 017, 019, 020, 023, 029, 030,
031, 032, 033, 034), 1 INFERENCE item (P2-095), 3 CODE items on the under-the-hood jobs
(P3-035, 037, 038), 7 items on retrieval budgets and caps (P1b-006, 007, 008, 010, 012,
024, 032) and 1 PARAM_DEFAULT history item (P4-006). 49 expected citations (45 at
`77d431a`, 2 at `a707cc2`, 2 at `4c5cfe8`), each with an oracle span SHA-256 and an anchor;
30 carry a `get_param` lookup and 7 a `find_symbols` lookup. The 9 abstention items ask
for production and live values, per-viewer and per-account data, revenue and payout rates,
a reach prediction, suppression that nothing in the code shows, model weights and an
external official document (P5-067); 17 `search_code` probes.

### Deterministic harness (in `make ci`)

`python eval/run_gate.py --store $WORK/store` on a temporary store with `4c5cfe8`, `a707cc2`
and `77d431a` pinned from `$UPSTREAM` and `77d431a` indexed (2026-10-01, 2.9 s):

| Check | Result |
| --- | --- |
| pinned commits resolve | 3 / 3 |
| `read_span` returns the span (`OK`) | 49 / 49 |
| span SHA-256 equals the oracle hash | 49 / 49 |
| anchor verdict `FOUND` | 49 / 49 |
| expected value inside the span text | 30 / 30 |
| `get_param` declaration at the cited span with the expected value | 30 / 30 |
| `find_symbols` symbol overlapping the cited span | 7 / 7 |
| abstention probes with zero hits (scope: 2,140 lexically indexed files of 2,147 manifest paths; 7 unsearchable) | 17 / 17 |

Result: PASS, 234 / 234 checks. `tests/test_eval_gate.py::DeterministicGateUpstreamTest`
repeats this inside `make ci` whenever `TXRAY_TEST_UPSTREAM` is set.

### Live harness (one run, by hand)

Run on 2026-10-01 with Claude Code 2.1.286, model `claude-haiku-4-5-20251001`, `--strict-mcp-config` (only the
TimelineXray server), max 15 turns and USD 0.20 per item, against a store with `4c5cfe8`, `a707cc2` and
`77d431a` pinned and indexed. The run was split into three invocations (items Q01-Q15, Q16-Q28, Q29-A09)
because one background invocation hit a wall-clock limit; every item ran exactly once and the three result
files were scored together with `live_gate.summarize`. Total cost USD 1.80 (budget USD 3.00); 370 turns.
Full per-item report: [`eval/live-run-2026-10-01.txt`](../eval/live-run-2026-10-01.txt).

| Metric | Measured | Threshold | Result |
| --- | --- | --- | --- |
| Semantic citation precision | 27 / 30 = 0.900 | >= 0.95 | **fail** |
| Coverage (recall) | 27 / 31 = 0.871 | >= 0.80 | pass |
| Abstention accuracy | 5 / 9 = 0.556 | 1.00 | **fail** |
| False-abstention rate | 0 / 31 = 0.000 | <= 0.20 | pass |
| Citation integrity of the agent's citations | 50 / 50 = 1.000 | 1.00 | pass |
| Completeness | 40 / 40 run; 5 runs ended at the turn limit | every item, no errors | **fail** |

**The gate failed at this run; the numbers are reported as measured, not tuned.** Root analysis of the
13 non-passing items:

- Q03, Q04 (`uncited`): the replies are true statements about a different pipeline: the agent read
  `ForYouCandidatePipeline` (`for_you_candidate_pipeline.rs`: no scorers; post-selection filter
  `AdAdjacentServedFilter`), while the expected answers concern `PhoenixCandidatePipeline`. The question
  wording "For You post pipeline" is ambiguous. Q01, Q02 ended at the turn limit, likely for the same reason.
- A06, A07, A09 ended at the turn limit while searching for something the code does not contain; A02 described
  the mechanism (the model predicts P(action)) instead of stating that a per-viewer probability is not in the
  repository (`unsupported_answer`).

Proposed changes for the next run (each a documented correction, not a change made to pass this run): name
the pipeline explicitly in Q01-Q04 (`PhoenixCandidatePipeline` in `phoenix_candidate_pipeline.rs`); add an
agent-contract sentence asking the agent to abstain when two targeted searches return no supporting span;
and decide whether the turn limit belongs to the gate (a client limit) or should be raised. Q05-Q31 and the
parameter questions were answered correctly with intact citations.

Corrections after the first live run (2026-10-02, decided before the second run and made
exactly as proposed above; every change is listed here with its reason, as the gate rule
requires):

1. Q01-Q04: the ambiguous "For You post pipeline" now names `PhoenixCandidatePipeline` in
   `home-mixer/candidate_pipeline/phoenix_candidate_pipeline.rs` (the first run's agent read
   `ForYouCandidatePipeline`, a different pipeline). Expected answers, citations, hashes and
   regexes are unchanged.
2. The live harness's system prompt (`eval/live_gate.py`) states two rules the agent contract
   already holds (AGENTS.md "Evidence contract"; `docs/limits.md`): the repository holds no
   production or live values, per-viewer or per-account data, experiment assignments, trained
   model weights or reach predictions, so a question needing them is answered by abstaining;
   and after two targeted searches without a supporting span the agent abstains with the
   search scope. The same two rules were added to `docs/agents/AGENTS-snippet.md`, so the
   harness and the shipped agent instructions agree.
3. Turn limit: a client limit, not part of the gate. The default `--max-turns` is 25 (was
   15); the per-item budget (USD 0.20) and the hard total budget are unchanged.

No threshold, expected citation or abstain item was changed.

Root review (2026-10-01, before the live run): the question set and thresholds were accepted. One change was made before any live result existed: 23 numeric answer patterns were tightened with `(?!\.\d)` so that an answer such as `0.3` no longer matches the expected value `0` (and `1.5` no longer matches `1`). No question, expected citation or threshold was changed.

### Second live run (2026-10-02)

Run after the corrections listed above, with Claude Code 2.1.287, model
`claude-haiku-4-5-20251001`, `--strict-mcp-config`, max 25 turns and USD 0.20 per item,
against a fresh store with `4c5cfe8`, `a707cc2` and `77d431a` pinned and indexed; one
invocation, every item ran exactly once. Total cost USD 1.40 (budget USD 3.00); 333 turns.
Full per-item report: [`eval/live-run-2026-10-02.txt`](../eval/live-run-2026-10-02.txt).

| Metric | Measured | Threshold | Result |
| --- | --- | --- | --- |
| Semantic citation precision | 29 / 31 = 0.935 | >= 0.95 | **fail** |
| Coverage (recall) | 29 / 31 = 0.935 | >= 0.80 | pass |
| Abstention accuracy | 9 / 9 = 1.000 | 1.00 | pass |
| False-abstention rate | 0 / 31 = 0.000 | <= 0.20 | pass |
| Citation integrity of the agent's citations | 51 / 51 = 1.000 | 1.00 | pass |
| Completeness | 40 / 40 run, 0 errors | every item, no errors | pass |

**The gate failed at this run as well; the numbers are reported as measured.** Root
analysis of the two non-passing items (checked at the source, not changed for this run):

- Q22 (`uncited`): the reply is correct. `initialTweetId.getOrElse(...)` occurs twice in
  `under-the-hood/scalding/UthDailyPostsJob.scala` at `77d431a`, at line 200 and at line
  227; the agent cited lines 217-232, the question set expects only line 200. The expected
  citation set is incomplete.
- Q05 (`wrong_value`): the reply states the four conditions correctly but in prose
  ("in-network-only", "cached posts"); the answer patterns require the identifiers
  `in_network_only` and `has_cached_posts` literally.

Proposed for a third run (each a documented correction to the item, not a change of this
run's result): add the line-227 span as a second expected citation of Q22; either ask Q05
for the identifiers by name or accept the prose forms. Re-scoring this run with corrected
items is not done: a pass requires a new run.

### Set revision 2 (prepared and root-reviewed 2026-10-07)

`eval/questions-v2.json` is a new revision of the set; `eval/questions.json` (revision 1),
the two live-run reports and every number above stay exactly as recorded (a test pins the
SHA-256 of revision 1). Thresholds unchanged. Nothing was re-scored and no live run was made
with revision 2: a pass needs a new run. Each change is recorded in the file's `revision`
member with its fields, basis and reason; in short:

| Item | Change | Basis |
| --- | --- | --- |
| Q22 | second expected citation `UthDailyPostsJob.scala:227` (the second `initialTweetId.getOrElse`, span hash by plain `git show`); one pattern for the initial post id, because the question asks only about edited posts | second live run, root analysis above |
| Q05 | the conditions may be written in prose (`in-network-only`, `cached posts`) as well as with the identifiers | second live run, root analysis above |
| Q02, Q04 | one pattern per filter: the questions do not ask for an order | reading |
| Q01, Q03, Q20, Q21 | identifiers may be written with a space or hyphen (`Tweet Mixer`, `VM ranker`, `positive sum`, `null-cast`); Q01 and Q03 keep the order they ask for | reading |
| Q25, Q27, Q29 | a thousands separator (`1,200`) states the same public default; longer numbers are still rejected | reading |
| Q15-Q19 | a negative public default may be written with an en dash or the word "negative" | reading |

The deterministic harness passes on both revisions (revision 2: 237 / 237 checks, 50
expected citations). The live command for the next run is in `eval/README.md`.


### Third live run (2026-10-07, set revision 2)

Run with Claude Code 2.1.292, model `claude-haiku-4-5-20251001`, `--strict-mcp-config`, max 25
turns and USD 0.20 per item, against a fresh store with `4c5cfe8`, `a707cc2` and `77d431a`
pinned and indexed (deterministic harness first: PASS 237/237); one invocation, every item ran
exactly once. Total cost USD 1.39 (budget USD 3.00); 316 turns. Full per-item report:
[`eval/live-run-2026-10-07.txt`](../eval/live-run-2026-10-07.txt).

| Metric | Measured | Threshold | Result |
| --- | --- | --- | --- |
| Semantic citation precision | 31 / 32 = 0.969 | >= 0.95 | pass |
| Coverage (recall) | 31 / 31 = 1.000 | >= 0.80 | pass |
| Abstention accuracy | 7 / 9 = 0.778 | 1.00 | **fail** |
| False-abstention rate | 0 / 31 = 0.000 | <= 0.20 | pass |
| Citation integrity of the agent's citations | 53 / 53 = 1.000 | 1.00 | pass |
| Completeness | 40 / 40 run, 1 error | every item, no errors | **fail** |

**The gate failed again; the numbers are reported as measured.** Every answer item was correct.
The two misses are abstention items, both of which passed in the second run:

- A02 (`unsupported_answer`): asked for the probability that a specific viewer likes a specific
  post, the agent described how such a probability is predicted and combined (with citations to
  the upstream README) instead of abstaining; the value itself is not in the public code.
- A06 (`error`): the agent searched for a link-based demotion until the 25-turn limit and gave
  no reply.

Root reading: the items are sound (no set change is proposed); the misses are agent behaviour
that varies between runs at the same settings. Nothing was re-scored. A pass needs a new run;
whether to strengthen the agent contract's abstention rule before it is a design decision that
would be evaluated on this same set, so it is recorded here rather than made now.

### Gate rule (root-reviewed 2026-10-01)

The release gate fails when any of these does not hold; every number is reported with
its denominator and the model, client version and date of the run.

| Gate | Threshold | How it is measured |
| --- | --- | --- |
| Deterministic citation and probe checks | 100 % (every check) | `eval/run_gate.py` in `make ci` |
| Semantic citation precision | >= 95 % of the answers given are correct (overlapping cited span at the pinned commit and the value stated correctly); unsupported answers on abstain items count against it | `eval/live_gate.py` |
| Coverage (recall) | >= 80 % of the answer items answered correctly | `eval/live_gate.py` |
| Abstention accuracy | 100 % of the abstain items abstained (no unsupported numeric answer) | `eval/live_gate.py` |
| False-abstention rate | <= 20 % of the answer items | `eval/live_gate.py` |
| Citation integrity of the agent's citations | 100 % read back at a pinned commit | `eval/live_gate.py` |
| Completeness | every item run (none skipped for budget), no parse errors | `eval/live_gate.py` |

The thresholds were written down before the live run; a run below a threshold is reported
as a failed gate, not tuned away. A question changed after seeing live results must be
listed here with the reason. P7 section 7.3's "unanswerable production-value questions: no
unsupported numeric answers" is the abstention-accuracy row.

## 4. Legal and documentation

- [x] `LICENSE` is the full Apache License 2.0 text (hash-checked); `NOTICE` states
      Apache-2.0, the non-affiliation sentence, runtime fetching without vendoring and the
      absence of X logos; `pyproject.toml` declares `Apache-2.0` with both license files.
- [x] No image assets in the repository; goldens are citations only (anchors <= 80
      characters).
- [x] `docs/limits.md` lists every known limit; `SECURITY.md` states the threat model.
- [x] README (status "public alpha", measured gate results), CHANGELOG, `AGENTS.md` = `CLAUDE.md` and
      `docs/architecture.md` describe 0.9.0.
- [x] No claim of untested client compatibility: the MCP client matrix names what ran.

## 5. Publishing (owner approval 2026-10-02)

0. Build the release branch with UTC dates and run
   `python scripts/check_release_history.py --fsck` on it; `git fsck --no-reflogs` must
   list nothing dangling after `git reflog expire --expire=now --all && git gc --prune=now`.
1. Add the remote and push (a separate, explicit approval; not part of this release work).
2. Wait for the `ci` workflow, then run the release gate:
   `python scripts/ci_status.py --repo OWNER/NAME --commit SHA` (exit 0 required; a
   private repository needs `GITHUB_TOKEN` in the environment).
3. Only then, and only after a passing live run of the semantic gate (section 3a), tag
   `v0.9.0`. Remove the `Private :: Do Not Upload` classifier only if the
   package is to be uploaded to an index.
