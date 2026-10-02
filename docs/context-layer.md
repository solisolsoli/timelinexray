# Optional: Context Layer

[Context Layer](https://github.com/solisolsoli/context-layer) is a separate, MIT-licensed
tool that connects a folder of Markdown notes (a *vault*, for example an Obsidian vault) to
AI agents: it indexes the notes locally (`context-layer init`, `context-layer index`),
retrieves verbatim passages with their paths and hashes, follows the `[[wikilinks]]`
between notes, and serves this over its own MCP server.

**Everything on this page is optional.** TimelineXray does not import Context Layer, does
not depend on it (not even as an optional extra), never installs, configures or starts it,
and enables nothing by default. A user who ignores this page sees no change in behaviour:
the only addition to TimelineXray is the `txray export context-layer` command, which runs
only when invoked. There are three independent ways to connect the two tools; use any, all
or none of them.

| Way | What runs | Network | Status |
| --- | --- | --- | --- |
| (a) [Export findings into a vault](#a-export-findings-into-a-context-layer-vault) | `txray export context-layer`, then Context Layer's own `init` / `index` | none | tested, including a live run with Context Layer (below) |
| (b) [Both MCP servers side by side](#b-both-mcp-servers-in-one-agent-host) | your agent host starts `txray mcp serve` and `context-layer mcp` | none from either server | both servers checked side by side over stdio; not run inside a real agent host together |
| (c) [Context Layer's GitHub context on the upstream docs](#c-context-layers-github-context-on-the-upstream-documentation) | Context Layer only (TimelineXray is not involved) | Context Layer's anonymous HTTPS reads from `api.github.com` | configuration validated offline; no fetch run |

**Context Layer version read and tested.** Its `README.md`, `docs/host-integration.md`,
`docs/github-context.md` and `docs/cli.md` were read at commit
`23e5b1c2c0e5ea5777354e16730349745e7157f1` (package version 0.4.0, `git describe`
`v0.4.0-18-g23e5b1c`) on 2026-10-01; the configuration rules for (c) and the link rules for
(a) were also checked against its source at that commit (`context_layer/github_context.py`,
`context_layer/graph.py`, `router/build_index.py`, `router/source_policy.py`). Every
Context Layer command and option below is taken from those documents; where they and this
page disagree, Context Layer's documents win. Later Context Layer versions may differ.

## (a) Export findings into a Context Layer vault

```sh
txray export context-layer --out ~/MyVault/timelinexray \
    [--ledger DIR] [--commit C] [--include-stale] [--store DIR] [--json]

# then, with Context Layer (its own commands, see its README and docs/cli.md):
context-layer init ~/MyVault     # once per vault: writes .context/routes.json
context-layer index ~/MyVault    # after every export
context-layer search ~/MyVault --prompt "click weight public default"
```

`--ledger` is the findings ledger to read (default `$TXRAY_FINDINGS`, else
`<store>/findings`, as for `txray findings`); it is read with a verified hash chain and
never written. `--commit C` (a commit pinned in the store) asks for freshness at C instead
of relative to the newest pin. `--json` prints the machine-readable envelope with the
selection, the counts and the files written, unchanged and removed. Exit codes: 0 ok, 1
refused or failed, 2 invalid arguments.

### What is written

```
<out>/
  txray-README.md                           provenance and how to read the notes
  txray-index.md                            links every exported note with [[wikilinks]]
  txray-findings/txray-finding-<id>.md      one note per finding
  .txray-export.json                        manifest: every exported file and its SHA-256
```

A finding id becomes a file name by replacing every character other than letters, digits,
`.`, `_` and `-` with `_` (`P1b:P1-013` becomes `txray-finding-P1b_P1-013`); ids that would
then coincide, ignoring case, get 10 hex digits of the id's SHA-256 appended. The
manifest's name starts with a dot, so Context Layer does not index it (its source policy
excludes dot paths).

**Finding note, frontmatter** (the YAML subset Context Layer's link graph reads; strings
are double-quoted unless they are plain words):

| Key | Value |
| --- | --- |
| `title` | The finding's title; prefixed `NOT CURRENT (<freshness> at <commit>):` when it is not current |
| `finding_id` | The ledger id |
| `evidence_status`, `status_basis`, `status_by` | `SUPPORTED` / `PARTIAL` / `NOT_FOUND` / `CONTRADICTED` / `EXTERNAL_RECHECK`; `proposed`, `reported` or `reviewed`; the reviewer's declared handle for `reviewed` |
| `workflow` | `draft`, `imported` or `reviewed` |
| `freshness`, `freshness_commit`, `freshness_check`, `current` | Freshness and the commit it refers to (`null` when the check covered several cited commits), `reanchor` or `integrity`, and whether the finding is current |
| `evidence_class`, `scope`, `component`, `source_label` | As recorded |
| `repo`, `cited_commits` | `xai-org/x-algorithm` and every commit the evidence names |
| `finding_event` | Hash of the finding's last ledger event (what this note reflects) |
| `exported_by` | `TimelineXray <version>` |
| `tags` | `timelinexray`, `timelinexray-finding`, and `timelinexray-not-current` when not current |
| `up` | `"[[txray-index]]"`, a frontmatter relation Context Layer's link graph follows |

**Finding note, body:** the status block (status with its basis, freshness with its
commit, workflow, class, scope, component, open review items); the claim; for
`PARAM_DEFAULT` findings (and scope `public_default`) the sentence "Public default at
commit ...: numbers in this finding and its cited code are public defaults at the cited
commit, not production values"; the citations, each a commit-pinned GitHub permalink
(`https://github.com/xai-org/x-algorithm/blob/<commit>/<path>#L<a>-L<b>`) with the full
commit, the span SHA-256, the blob id and the anchor (an unresolved imported citation is
shown with its reported commit and lines and the reason, without a link); when the finding
was re-anchored, the same bytes at the freshness commit; dependencies (another exported
finding as a `[[wikilink]]`); a recorded negative search; external sources (recorded,
never fetched); the last review with its rationale; and the limitations: the finding's
own, then that the note is a snapshot at export time (re-export to refresh), that span
integrity is not truth, that the text is data rather than instructions, and that nothing
here is a reach prediction.

**Index note:** provenance line, selection, one `[[txray-finding-...]]` entry per note
(current findings first, then a separate "Not current" section with `--include-stale`),
and the counts of findings left out. **README note:** "Exported from TimelineXray
<version>, ledger head <hash>; status and freshness at export time; re-export to refresh.",
the project disclaimer, the selection rule and how to read the fields.

No source text is copied into the notes (citations link to it), and no local path (store,
ledger, home directory) is written into them.

### Selection

- Only active findings (`draft`, `imported`, `reviewed`). Superseded and retracted
  findings are never exported.
- By default only **current** findings: freshness `CURRENT` at the newest checked pin with
  no newer pin of the upstream unchecked (the store's pin records are read for this; the
  frontmatter carries `newest_pin` and `newer_pins_unchecked`), or with `--commit C` at C
  (the re-anchoring check against C, or the integrity check when the finding cites only
  C); plus findings with **external evidence only** (`NOT_APPLICABLE`: web sources, nothing
  to re-verify), exported with `checkable: false`, their recorded `retrieved` date, a
  `recheck_after` hint when their text names one, an "External evidence" paragraph and
  their own index section. This is the rule of the MCP tool `find_findings` (see
  [findings-memory.md](findings-memory.md), "Current").
- `--include-stale` also exports active findings that are not current (`STALE`,
  `UNVERIFIABLE`, `NOT_CHECKED`, or `CURRENT` with a newer pin unchecked:
  `NEWER_PIN_UNCHECKED`), each labelled `NOT CURRENT` in its title, frontmatter
  (`current: false`, tag `timelinexray-not-current`), heading, a warning paragraph (naming
  the unchecked pin) and its index entry, which sits in a separate section.

### Safety of the output directory

- **Only inside `--out`.** `--out` is created if its parent exists (the parent is never
  created); nothing is written or removed anywhere else.
- **Refused destinations**, before anything is read or written: an `--out` that is a
  symbolic link or not a directory; one inside a TimelineXray source tree (any directory
  whose `pyproject.toml` declares the `timelinexray` project) or the installed package; one
  inside, or containing, the snapshot store or the findings ledger (a vault indexed there
  would index them); one inside an analytics dataset, or containing one, or below a
  directory that holds one (the `dataset.json` marker the MCP server refuses). The ledger
  is refused, too, when an analytics dataset lies in or above it.
- **No analytics data, ever.** The notes are rendered from the findings ledger alone; the
  export never opens an analytics dataset or imports the analytics package (a test runs an
  export next to a dataset holding a canary and finds the canary in no output).
- **Atomic, symlink-free writes.** Each file is replaced through a temporary file in the
  same directory and `os.replace`; a file whose bytes are already right is not rewritten
  (so an unchanged ledger gives no changes to re-index). Symbolic links are never followed:
  a link where the manifest, `txray-findings/` or a note would be is refused.
- **Manifest-based cleanup.** `.txray-export.json` lists every file the export created
  with its SHA-256. A re-export replaces or removes only files listed there, and only while
  their bytes are still the recorded ones. It refuses (and changes nothing) when a file it
  would write exists but was not created by an export, when an exported note was edited
  after the export (move your text to a note of your own, delete the exported one, export
  again), or when the manifest names a file an export cannot create. The manifest is
  written before the notes (listing the files about to be written as `pending`) and again
  at the end, so an interrupted export can be re-run.
- **Deterministic.** The same ledger, options and TimelineXray version give byte-identical
  files: no timestamps, ledger order, sorted keys. A finding note changes only when that
  finding's own events (or the notes it links to) change; the index and README carry the
  ledger head.
- **Untrusted text stays data.** Titles, claims, limitations, quotes and rationales are
  ledger records written by declared, unauthenticated actors. Single-line text is written
  with Markdown link, HTML, tag, table and code syntax escaped; multi-line text verbatim
  inside fenced `text` blocks (Context Layer's link graph ignores links inside code). No
  ledger text can add a link, heading, tag or frontmatter to the vault.

These are path checks, not a sandbox: another process that swaps directories while an
export runs is out of scope (as in Context Layer's own source policy).

### Reading exported notes through Context Layer

A note is a snapshot of the ledger at export time. Before presenting a finding from the
vault as current, check `current: true` in the note and, when TimelineXray's MCP server is
also connected (way b), confirm it with `find_findings` / `get_finding`; cite the commit
and span, never the note. Re-export after `txray findings reanchor` or `review`, then run
`context-layer index`. Two exports into the same vault (in different folders) give
duplicate note names, which Context Layer reports as ambiguous links; export into one
folder per vault.

## (b) Both MCP servers in one agent host

Each tool keeps its own server; the agent host starts both. Their tool names do not
overlap (TimelineXray: `list_commits`, `resolve_commit`, `manifest_summary`, `read_span`,
`search_code`, `find_symbols`, `index_coverage`, `find_findings`, `get_finding`,
`verify_claim`; Context Layer 0.4.0: `search_vault`, `read_source`, `vault_status`,
`memory_record`, `memory_resume`, `graph_neighbors`, `read_packet`, `jev_status`,
`check_claims`, `github_context`), and hosts prefix them with the server name anyway
(Claude Code: `mcp__timelinexray__read_span`, `mcp__context-layer__search_vault`).

**Claude Code**, project `.mcp.json` (replace the `/path/to/...` placeholders; keep the
TimelineXray store and ledger outside the project directory):

```json
{
  "mcpServers": {
    "timelinexray": {
      "type": "stdio",
      "command": "txray",
      "args": ["mcp", "serve", "--store", "/path/to/timelinexray-store",
               "--ledger", "/path/to/timelinexray-findings"]
    },
    "context-layer": {
      "command": "context-layer",
      "args": ["mcp", "--vault", "/path/to/vault"],
      "env": {"PYTHONUTF8": "1"}
    }
  }
}
```

The `context-layer` entry is the shape Context Layer's `docs/host-integration.md`
documents and writes itself; with Context Layer installed you can let it merge its entry
into an existing `.mcp.json` instead (it keeps the other `mcpServers` keys and shows a diff
before `--apply`):

```sh
context-layer install claude-code --vault /path/to/vault --project /path/to/project           # preview
context-layer install claude-code --vault /path/to/vault --project /path/to/project --apply
```

Or with Claude Code's own command, one server at a time (for Context Layer, its host guide
shows `claude mcp add --scope user context-layer -- context-layer mcp --vault
/path/to/vault` and places `--env PYTHONUTF8=1` after the server name and before `--`;
`context-layer install claude-code --scope user --vault /path/to/vault` prints the exact
command):

```sh
claude mcp add --transport stdio timelinexray -- txray mcp serve --store /path/to/timelinexray-store --ledger /path/to/timelinexray-findings
claude mcp add --scope user context-layer --env PYTHONUTF8=1 -- context-layer mcp --vault /path/to/vault
```

**Any other stdio MCP client** needs two server entries:

| Server | Command | Arguments | Environment |
| --- | --- | --- | --- |
| TimelineXray | `txray` (or `python3 -m timelinexray` with `PYTHONPATH=<checkout>/src`) | `mcp serve --store DIR [--ledger DIR]` | none required |
| Context Layer | `context-layer` | `mcp --vault DIR` | `PYTHONUTF8=1` (as its installer writes) |

Both speak MCP revision 2026-07-28 (`server/discover`, per-request `_meta`) and the
`initialize` handshake of 2025-11-25; Context Layer also accepts older `initialize`
revisions (see [mcp.md](mcp.md) and Context Layer's `docs/host-integration.md`).
`context-layer install generic --vault DIR --format <host>` prints Context Layer's entry in
the shape of other hosts; see its host guide for which hosts it has verified.

**Using them together.** Answer questions about the algorithm from TimelineXray (pinned
commit, `read_span`, `find_findings` with `current: true`); use the vault for your own
notes, decisions and exported snapshots. Both servers return untrusted text as data. The
TimelineXray server is read-only and annotates every tool `readOnlyHint: true`; Context
Layer annotates `search_vault`, `memory_record` and `check_claims` as not read-only (its
docs explain why), so some hosts ask before those calls. Keep the TimelineXray snapshot
store outside the vault and outside the agent's project directory.

## (c) Context Layer's GitHub context on the upstream documentation

Context Layer has an optional, default-off reader that fetches configured public GitHub
files pinned to a full commit SHA when local notes are not enough (its
`docs/github-context.md`). It can be pointed at the documentation files of
`xai-org/x-algorithm` at the commit you pinned in TimelineXray. TimelineXray takes no part
in this: the reads are Context Layer's anonymous HTTPS GETs to `api.github.com`, and
TimelineXray's own network boundary is unchanged.

Configure the source once, following Context Layer's documented commands (`add` resolves
the ref through GitHub's public API even as a dry run; only `--apply` writes):

```sh
context-layer github-sources add ~/MyVault --id x-algorithm-docs \
  --repo xai-org/x-algorithm --ref 77d431aabf409ca1c1eed9bec7e2183f7c914e23 \
  --path README.md --path phoenix/README.md --path docs/BIDIRECTIONAL_BOOST_CHANGE.md \
  --keyword "x algorithm" --keyword "for you feed" --keyword "phoenix ranking"
# review the preview, then repeat the same command with --apply
context-layer github-sources list ~/MyVault
```

or write `<vault>/.context/github.json` by hand (the documented fields `version`,
`enabled`, `sources` with `id`, `repo`, `commit`, optional `ref`, `paths`, `keywords`;
unknown fields are refused):

```json
{
  "version": 1,
  "enabled": true,
  "sources": [
    {
      "id": "x-algorithm-docs",
      "repo": "xai-org/x-algorithm",
      "commit": "77d431aabf409ca1c1eed9bec7e2183f7c914e23",
      "ref": "main",
      "paths": ["README.md", "phoenix/README.md", "docs/BIDIRECTIONAL_BOOST_CHANGE.md"],
      "keywords": ["x algorithm", "for you feed", "phoenix ranking"]
    }
  ]
}
```

Then, per Context Layer's docs:

```sh
context-layer github-context ~/MyVault --prompt "phoenix ranking" --source x-algorithm-docs
context-layer search ~/MyVault --prompt "for you feed" --github   # only after a clean, empty local search
context-layer github-sources check ~/MyVault --id x-algorithm-docs --ref main   # review a newer version
```

- **The commit.** `77d431aabf409ca1c1eed9bec7e2183f7c914e23` is TimelineXray's reference
  pin; use the commit you pinned (`txray pin`) so that the documentation and the code you
  cite agree. The three paths exist at that commit (46,761, 26,928 and 19,293 bytes: within
  Context Layer's bounds of four files and 128 KiB per request). Retrieval always uses
  `commit`; `ref` is used only by `github-sources check`. Move the pin deliberately with
  `github-sources update --expected-commit OLD --commit NEW` after `txray pin NEW`.
- **What the passages are.** Upstream documentation text, delivered by Context Layer as
  untrusted external evidence (at most 2,000 characters per file, 6,000 per result). They
  are not TimelineXray evidence: to cite upstream documentation in a finding, read the span
  with `txray show <commit> <path> --lines A-B` and record it with evidence class
  `REPO_DOC`. The upstream README's statements about configuration (public defaults are
  not production values) apply to anything these files say about numbers.
- **Privacy.** Per Context Layer's docs, GitHub sees the repository, commit and paths
  requested, not your prompt or notes; no token is used or stored.

## What was tested and what was not

| Item | How | Result |
| --- | --- | --- |
| Export: selection, `--include-stale` labels, `--commit`, frontmatter and body content, permalinks, span SHA-256, public-default wording, wikilinks resolving to exported notes, escaping of ledger text, determinism (two exports byte-identical; a re-export writes nothing), manifest cleanup, interrupted export re-run, refusals (working tree, store, ledger, analytics dataset, symbolic links, foreign and edited files, tampered manifest, missing or damaged ledger), no analytics canary in any output | `tests/test_context_layer_export.py` (25 tests, synthetic fixture and the synthetic research-import example, offline) | pass |
| No module imports `context_layer`; no dependency or extra; the export package is not imported by the CLI parser or the MCP tools; the MCP tool list is unchanged | `tests/test_context_layer_export.py` (`IndependenceTest`) | pass |
| Live: export into a temporary vault next to a note of the user's own, `context-layer init`, `context-layer index`, `context-layer search --prompt "quokka marker truthy candidate"` returns the exported note with the SHA-256 of the bytes TimelineXray wrote | `tests/test_context_layer_live.py`, run with `TXRAY_TEST_CONTEXT_LAYER` set to Context Layer 0.4.0 at `23e5b1c2c0e5ea5777354e16730349745e7157f1`, installed with `pip` 24.0 into a fresh Python 3.12.4 virtual environment from a `git archive` copy of that commit (macOS, 2026-10-01) | pass (all three commands exit 0; packet status `PARTIAL` with the note). Skipped with an `optional:` reason when the variable is unset; `scripts/run_tests.py` never counts that skip as a failure |
| Link graph of an export | the same Context Layer build, by hand: `context-layer graph health` on an export | 0 broken, 0 ambiguous links; every frontmatter block parsed; `search --method synaptic` added the linked README note |
| (b) Both servers side by side | by hand: each started with the command and arguments above, `server/discover` and `tools/list` over stdio (2026-07-28) | both answer; 10 + 10 tools, no name in common |
| (b) Both servers inside Claude Code or another host | - | **not tested** (the TimelineXray server alone was tested with Claude Code, see [mcp.md](mcp.md)) |
| (c) The example `github.json` | by hand: `context-layer github-sources list` on a vault holding it (offline) | accepted (`status: OK`); the three paths exist at the commit in a local upstream clone |
| (c) Fetching the files, `github-sources add`, caching, fallback | - | **not tested** (needs network access to `api.github.com`) |

## Limits

- The notes are snapshots; nothing updates them automatically. Re-export and re-index.
- Permalinks are built from the recorded commit, path and lines without contacting GitHub;
  a finding recorded against a local mirror of another repository would give a dead link.
- An exported note that you edit is protected, not merged: the next export refuses until
  you move your text elsewhere and delete the note.
- Context Layer's own behaviour (retrieval, link following, GitHub reads, host installs) is
  documented and tested by Context Layer, not here.
