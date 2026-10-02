# Generic stdio MCP client

Status: untested with clients other than those listed as tested in
[../mcp.md](../mcp.md#client-matrix).

- Transport: stdio. Start `txray mcp serve --store /path/to/timelinexray-store` (or
  `python3 -m timelinexray mcp serve ...` with `PYTHONPATH=/path/to/timelinexray/src`).
  Alternatively set `TXRAY_STORE=/path/to/timelinexray-store` in the server environment
  and omit `--store`. The findings tools read the ledger given by `--ledger DIR`, else
  `$TXRAY_FINDINGS`, else `<store>/findings`; clients cannot choose another location.
- Framing: one JSON-RPC 2.0 message per line, UTF-8. Close stdin to stop the server.
- Protocol revisions: `2026-07-28` (send `_meta` with
  `io.modelcontextprotocol/protocolVersion` and `io.modelcontextprotocol/clientCapabilities`
  on every request; `server/discover` is available) or `2025-11-25` (send `initialize`
  first).
- Limits: requests up to 16 KiB, responses up to 64 KiB, 10 s per tool call
  (`--time-budget SECONDS`).
- The server needs no credentials, no network and no write access. Pin and index commits
  beforehand with `txray pin <commit>` and `txray index <commit>`, and record findings with
  `txray findings ...`.

A typical `mcpServers`-style entry:

```json
{
  "mcpServers": {
    "timelinexray": {
      "command": "txray",
      "args": ["mcp", "serve"],
      "env": {"TXRAY_STORE": "/path/to/timelinexray-store"}
    }
  }
}
```
