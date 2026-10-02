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
