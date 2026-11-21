# Staleness: when a finding stops being current

A finding is written at one commit. The upstream keeps changing. This page explains how
TimelineXray decides, for every newer commit, whether a finding's evidence still holds
there, and why a finding is never silently "updated". Reference: the
[findings memory](findings-memory.md#re-anchoring-on-a-newer-commit); code:
`timelinexray/verify.py`, `timelinexray/findings/freshness.py` and
`timelinexray/findings/stale.py`.

## Freshness is relative to a commit

Freshness answers "does the cited evidence still hold *at commit C*?". It is not a property
of the claim alone, and it is not truth: a `STALE` finding may still be right, and a
`CURRENT` one may have been wrong all along. Freshness only says whether the bytes the claim
rests on are still where it says they are.

The question a reader actually asks is "may I present this finding as current?", and the
answer is taken relative to the **newest pinned commit**. A finding checked only at an older
pin is *not current* until it is re-anchored on the newer one: the CLI says `not current`,
the MCP tools return `current: false` with the unchecked `newer_pins`, and the Context Layer
export leaves it out or labels it `NOT CURRENT`. A finding with external evidence only (an official web page, no code
citation) has nothing to re-check; its reading is `NOT_APPLICABLE` and its date is its
retrieval date.

## Re-anchoring one span

`txray findings reanchor <commit>` (or `--latest`) looks for every cited span of every
active finding in the target commit. The search is for the **exact span bytes**, never for
the anchor word and never "the most similar lines".

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/reanchor-dark.svg">
  <img src="img/reanchor-light.svg" width="100%" alt="Decision tree for one cited span. If the target has the same blob at the same path, the outcome is identical (CURRENT). If the path still exists, the exact span bytes are searched in that file as whole lines: once means unchanged or relocated (CURRENT), several times means ambiguous with none picked (UNVERIFIABLE), none means changed (STALE) with a line-diff proposal. If the path is gone, other files are searched by identical blob id, then through the code index: one match is relocated (CURRENT), several are ambiguous and none is missing (both UNVERIFIABLE). A citation not usable at its own commit is unusable (UNVERIFIABLE). The finding takes the worst outcome: STALE, then UNVERIFIABLE, then CURRENT.">
</picture>

| At the target | Outcome | Freshness |
| --- | --- | --- |
| same path, same blob | `identical` | `CURRENT` |
| same path, exact span bytes once at the same lines | `unchanged` | `CURRENT` |
| exact span bytes once at other lines, or (path gone) in exactly one other file | `relocated` | `CURRENT` |
| exact span bytes more than once | `ambiguous` | `UNVERIFIABLE` |
| path present, exact span bytes absent | `changed` | `STALE` |
| path gone, bytes found nowhere else | `missing` | `UNVERIFIABLE` |
| citation not usable at its own commit | `unusable` | `UNVERIFIABLE` |

Four design choices make this conservative:

1. **Whole lines, exact bytes.** A match must start at a line start and end at a line end.
   A literal that survives only inside a longer line is not a match.
2. **No first match.** When the bytes occur more than once, none is chosen; every
   occurrence is listed and the outcome is `ambiguous`. This is the only place where
   uniqueness is required (at the finding's own commit, `FOUND_MULTIPLE` is fine).
3. **The file first.** While the path exists, the bytes are looked for only in that file.
   A byte-identical line in another file is not a relocation: in the example below the old
   `ClickWeight` line still exists, byte for byte, in `vm-ranker/params.rs`, yet the Home
   Mixer finding is correctly `STALE`. Other files are searched only when the path is gone:
   by identical blob id (a pure rename), then through the code index; files the index
   skipped are named in the reason.
4. **A proposal is not a check.** For a `changed` span, a line diff of the two blobs
   proposes the corresponding region (with its own hash and anchor verdict). The proposal is
   shown to the reviewer; it never makes a finding current.

**Dependencies and absence claims.** A finding may depend on another finding or on a span;
a dependency that is stale, superseded or retracted makes the finding `STALE`
(`dependency_changed`) even when its own spans are unchanged. A `NOT_FOUND` finding re-runs
its recorded search at the target: new hits make it `STALE` (`negative_now_found`).

**Worst part wins.** The finding's freshness is the worst of its citations, dependencies and
negative search: `STALE` before `UNVERIFIABLE` before `CURRENT`. When every part is current
and some moved, a new provenance revision records the new locations; the original citation
stays in the record.

## A worked example on the real upstream

Real output, in a store holding `92f0918` and `ed78e06` (see the [README](../README.md#how-it-works)
for the commands that create the finding):

```console
$ txray findings reanchor --latest
target     ed78e0645cfafcd026cfc1fd0da964f630533334
findings   1: STALE 1
F-edd0332b2ae6           STALE         changed 1  citation 1 home-mixer/params/param.rs:347-347@92f0918f6cb5 changed: the cited bytes of lines 347-347 are not in home-mixer/params/param.rs at the target; the line diff aligns them with lines 492-492
review     1 finding(s) to re-review at ed78e0645cfa (parameter 1; outcomes changed 1); 1 with a successor draft possible
    1. F-edd0332b2ae6           STALE         parameter  changed
next       txray findings stale --target ed78e0645cfa   (the prioritised re-review list with the old and aligned spans and the exact commands; nothing is approved or rewritten)
```

The cited line was `param!(ClickWeight, f64, "rust_home_mixer_click_weight", 0.3);`. At
`ed78e06`, line 492 of the same file declares the public default `0.274`, so the exact bytes
are gone from that file and the finding is `STALE`, although it was reviewed `SUPPORTED` at
its own commit. Both readings are true at the same time, and the record keeps both.

## From stale to re-reviewed

`txray findings stale` (and the MCP tool `stale_worklist`) turns the recorded checks into a
worklist. It writes nothing and approves nothing.

```console
$ txray findings stale          # excerpt
[1] F-edd0332b2ae6  STALE  parameter  PARAM_DEFAULT  SUPPORTED (reviewed)  reviewed
    title      ClickWeight public default (Home Mixer)
    citation 1  changed  home-mixer/params/param.rs
      old        92f0918f6cb5 lines 347-347  sha256 4b5c535dbf7b2b6a  anchor 'ClickWeight'
      candidate  ed78e0645cfa lines 492-492  sha256 edcf89e31c669970  anchor FOUND  (line diff; a proposal, not a check)
    read       txray show 92f0918f6cb5 home-mixer/params/param.rs --lines 347-347 --anchor ClickWeight
               txray show ed78e0645cfa home-mixer/params/param.rs --lines 492-492 --anchor ClickWeight
    holds      edit the draft (claim, then delete "remove_after_reading"), then:
               txray findings supersede F-edd0332b2ae6 --actor AUTHOR --file SPEC_DIR/F-edd0332b2ae6.ed78e06.json --rationale RATIONALE
    as is      txray findings review F-edd0332b2ae6 --actor REVIEWER --role reviewer --status STATUS --target ed78e0645cfa --rationale RATIONALE
    withdraw   txray findings retract F-edd0332b2ae6 --actor AUTHOR --reason REASON
```

A person then takes one of three decisions:

1. **The claim still holds, or holds in corrected form:** write a successor that cites the
   new spans (`--spec-dir` drafts one, with the expected span hashes, and refuses to be used
   until an author has removed a `remove_after_reading` marker), then verify it and have a
   different reviewer review it.
2. **Assess it as it is at the target:** a review with `--target` closes the target's queue
   items; the finding stays not current there.
3. **Withdraw it:** retract.

Entries are ordered mechanically: parameter findings first, then findings on paths the
classifier's scoring-name rule matches, then the rest. The order says where to look first,
not what matters most.

## How this is tested

| What | Result | Where |
| --- | --- | --- |
| Controlled mutations that must invalidate a finding: changed literal, bytes surviving only inside a longer line, duplicated span, deleted file, changed span dependency, changed finding dependency, a new hit for a negative search | 7 / 7 flagged not `CURRENT` | `tests/test_findings_reanchor.py` |
| Harmless changes that must not: line shift, pure rename, rename with an unrelated edit, unrelated edit | 0 / 4 false positives | `tests/test_findings_reanchor.py` |
| Multi-occurrence anchors | unique relocation `CURRENT`, duplicate `UNVERIFIABLE`, never a first match | `tests/test_findings_reanchor.py` |
| Reference citations on the real upstream, with expected history (outcome, freshness, lines at later commits) | 35 citations, 14 history expectations; plus 19 (2026-10-15) and 38 (2026-11-21) snapshot citations | `goldens/`, `tests/test_findings_goldens.py`, `tests/test_upstream_refresh*.py` |

These numbers are the release gates recorded in
[release-checklist.md](release-checklist.md#3-release-gates-p7-section-73-with-denominators);
the suite runs them on every `make ci` with the upstream clone present.

On real data: re-anchoring the 423 active research findings of the maintainers' ledger on
`ed78e06` gave 200 `CURRENT`, 211 `STALE` and 12 `UNVERIFIABLE`; each of the 223 non-current
findings was then read by hand ([upstream review](research/upstream-2026-11-21.md#findings)).

## Limits

- The line-diff proposal is a heuristic and only a proposal; files over 50,000 lines are
  not diffed.
- Other files are searched for a moved span only when its path is gone, and only through
  the index when the target is indexed.
- Dependencies are declared by authors; nothing infers them.
- "Newer" between pins is committer time; pins with equal committer times are not ordered.
- A semantic change that leaves the cited bytes intact (for example, a caller that stops
  using a correctly cited function) is invisible to re-anchoring unless the finding cites
  or depends on the changed code. That is why claims should cite every span they rely on.
