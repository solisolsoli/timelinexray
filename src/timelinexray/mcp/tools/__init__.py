"""The MCP tool registry of the public-code profile.

Tools are defined in small modules, each with a ``register(registry)`` function, and
listed here in the order ``tools/list`` shows them:

* :mod:`.pins` - ``list_commits``, ``resolve_commit``, ``manifest_summary``;
* :mod:`.code` - ``read_span``, ``search_code``, ``find_symbols``, ``index_coverage``;
* :mod:`.params` - ``get_param``, ``param_history``: parameter declarations with their
  public defaults at a commit and over the pinned commits (P7 tools, since 0.9.0);
* :mod:`.findings` - ``find_findings``, ``get_finding``, ``verify_claim`` (Milestone 4b):
  read-only access to the findings ledger the operator configured.

A new tool group is a module with the same shape - tools built on :class:`.base.Tool`,
returning :class:`.base.ToolResult` inside the common envelope, reading only through
:class:`timelinexray.mcp.context.ToolContext` - enabled by adding it to
:data:`TOOL_MODULES`. The tools/list snapshot test then has to be regenerated deliberately
(see ``docs/mcp.md``).

Only public-code tools may be registered here. Analytics tools belong to a separate,
offline profile that this server never offers; this package never imports the analytics
package.
"""

from __future__ import annotations

from . import code, findings, params, pins
from .base import (
    ANNOTATIONS,
    OUTCOMES,
    PROFILE,
    RESULT_SCHEMA_VERSION,
    Registry,
    Tool,
    ToolResult,
    envelope_schema,
)

#: Tool modules in listing order.
TOOL_MODULES = (pins, code, params, findings)


def build_registry() -> Registry:
    """A registry holding every tool of the public-code profile."""
    registry = Registry()
    for module in TOOL_MODULES:
        module.register(registry)
    return registry


__all__ = [
    "ANNOTATIONS",
    "OUTCOMES",
    "PROFILE",
    "RESULT_SCHEMA_VERSION",
    "TOOL_MODULES",
    "Registry",
    "Tool",
    "ToolResult",
    "build_registry",
    "envelope_schema",
]
