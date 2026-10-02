"""Read-only MCP server, public-code profile (Milestone 4).

A hand-written stdio JSON-RPC 2.0 server (no SDK dependency) that serves one snapshot
store to MCP clients: pinned commits, manifests, exact source spans, lexical search,
symbols and call candidates, index coverage, and (Milestone 4b) the findings of one
findings ledger with span re-verification. It never fetches, never writes, never
starts processes other than the local ``git`` reads of :mod:`timelinexray.gitio`, never
imports the analytics package, and refuses a store or ledger that contains (or lies
inside) an analytics dataset.

* :mod:`.server`  - framing, protocol revisions ``2026-07-28`` and ``2025-11-25``, limits;
* :mod:`.tools`   - the tool registry (one small module per tool group);
* :mod:`.context` - guarded store access, citations, authenticated cursors;
* :mod:`.guard`   - store confinement, analytics-dataset refusal, path normalisation;
* :mod:`.schema`  - the strict JSON Schema subset used for tool inputs and outputs.

See ``docs/mcp.md`` for the protocol notes, tool contracts, limits and client matrix.
"""

from .guard import StoreGuard
from .server import (
    LEGACY_VERSIONS,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MODERN_VERSIONS,
    SUPPORTED_VERSIONS,
    Server,
    serve_stdio,
)
from .tools import build_registry

__all__ = [
    "LEGACY_VERSIONS",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "MODERN_VERSIONS",
    "SUPPORTED_VERSIONS",
    "Server",
    "StoreGuard",
    "build_registry",
    "serve_stdio",
]
