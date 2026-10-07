# Agent instruction templates

Templates for projects whose agents use TimelineXray through its read-only MCP server
(`txray mcp serve`, see [../mcp.md](../mcp.md)). Replace `/path/to/timelinexray-store`
with a snapshot store outside every agent working directory, so upstream files never enter
an instruction-loading hierarchy.

Recommended way to get the connection line for your store (prints only; it edits no client
configuration, writes nothing and uses no network):

```sh
txray setup                                  # pin + index the tested commit once
txray setup --print-mcp-config claude-code   # a ready 'claude mcp add --transport stdio timelinexray -- <txray> mcp serve --store <store>' line
txray setup --print-mcp-config json          # the same as an mcpServers snippet (compare claude-code.mcp.json)
```

The executable is the absolute path of `txray` on `PATH`, else `<python> -m timelinexray`; the
store is made absolute. Pass `--store DIR` to both commands when you do not use the default.

| File | Purpose | Status (2026-09-30) |
| --- | --- | --- |
| [AGENTS-snippet.md](AGENTS-snippet.md) | Short evidence rules for a project's `AGENTS.md` or `CLAUDE.md` (Claude Code can import it with `@AGENTS.md`). | Text only; instruction loading was not tested. |
| [claude-code-source.mcp.json](claude-code-source.mcp.json) | Claude Code MCP configuration running the server from a source checkout (`python3 -m timelinexray`, `PYTHONPATH`). | **Tested** with Claude Code 2.1.286 in print mode: `claude -p ... --mcp-config claude-code-source.mcp.json --strict-mcp-config --allowedTools "mcp__timelinexray__list_commits mcp__timelinexray__search_code"` (paths filled in). |
| [claude-code.mcp.json](claude-code.mcp.json) | The same for an installed `txray` entry point, e.g. as a project `.mcp.json` or with `claude mcp add --transport stdio timelinexray -- txray mcp serve --store /path/to/timelinexray-store`. | Untested (no installed entry point; project servers need interactive approval). |
| [generic-stdio.md](generic-stdio.md) | What any stdio MCP client needs: command, arguments, environment, protocol revisions. | Untested with other clients. |

Tool names appear in Claude Code as `mcp__<server name>__<tool>`, for example
`mcp__timelinexray__read_span`.

## Answer or abstain (rules R1-R4)

These rules decide when an agent answers from the public code and when it abstains. They are
general (no question names, no tuning to a question set) and are stated in the same words in
the [snippet](AGENTS-snippet.md), the MCP server `instructions` (`txray mcp serve`, see
`timelinexray/mcp/server.py`) and the live harness prompt (`eval/live_gate.py`, contract
`r2`); `tests/test_agent_contract.py` fails when one of the four is missing from a copy.

| Rule | Text | Why |
| --- | --- | --- |
| **R1 Request-time values are not in the code.** | A value that exists only when a request is served - a model's score, probability, prediction, rank or feed position for a viewer, account or post; an engagement count or reach; an experiment assignment; the live, current or production state of a parameter, feature switch, decider or metric - is not in the public repository. The code shows how such a value is computed and which public defaults enter it, never the value itself. Abstain on a question that asks for one; the reason may say, with citations, how the code computes it. Explaining the mechanism is not an answer to a question that asks for the value. | The repository holds code and public defaults mirrored from a configuration system; models are trained elsewhere and requests carry live features. |
| **R2 Rules need a span that states them.** | A claim that the system suppresses, demotes, shadowbans, throttles, penalises or boosts some account or behaviour needs a span the agent read that implements exactly that rule (a filter, condition or weight on that behaviour). A weight on a related action, a filter on a different condition, a similar name, or a search without hits supports neither "yes" nor "no": abstain. | A related span invites an inference the code does not make; absence of a term is not evidence either way. |
| **R3 Search budget.** | Count the searches (`search_code`, `find_symbols`, `get_param`, `param_history`, `index_coverage`) that find no span directly stating the asked fact. After two such searches in a row, or eight in all, stop searching: answer with what the spans read directly support, otherwise abstain. A search that leads to a span the agent reads and cites does not count, and neither does reading a span (`read_span`); related hits do not reset the count. | An open-ended search for something the code does not contain ends at a client limit with no answer at all, while an answer that needs many productive lookups must not be cut off. The numbers (two, eight) are bounds chosen without a measurement; the development set reports turns per kind to check them. |
| **R4 An abstention states its scope.** | The reason names the commit, the searches run and whether their coverage was complete (`coverage_complete`); the harness reply lists the queries in `searched`. | A reader can repeat or extend the search; "not found" without a scope cannot be checked. |

The rules are evaluated on a separate development set (`eval/dev-abstain.json`: abstain items
in the three families above plus answerable controls next to them, so that abstaining on
everything scores badly), never by tuning against the release-gate sets
(`eval/questions*.json`). See `eval/README.md` and `docs/release-checklist.md` (section 3a).

