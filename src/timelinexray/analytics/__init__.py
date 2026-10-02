"""Offline creator metrics (Milestone 5): descriptive measurements from a user's own export.

The package implements the metric design of the P6 research report:

* the import contract for one documented post-level export schema (``x-post-v1``, with
  English and Turkish header aliases), see :mod:`.schema` and :mod:`.dataset`;
* M1, realized weighted engagement (RWE): observed actions per 1,000 recorded impressions,
  weighted by public default coefficients at a named upstream commit, see :mod:`.rwe` and
  :mod:`.coefficients`;
* M2, relative reach: impressions at a common observation age relative to the account's
  own pre-publication baseline, see :mod:`.reach`;
* M3 (pre-publish checklist) and M4 (visibility flags) as data structures only, see
  :mod:`.checks`.

Neither metric is a reach prediction or an "algorithm score". Missing values are unknown,
never zero.

Process rule: analytics commands run in their own ``txray metrics`` process, which calls
:func:`timelinexray.analytics.offline.enter` before it imports any other module of this
package. This ``__init__`` therefore imports nothing. No other part of TimelineXray imports
this package, so analytics data never reaches the snapshot store or the MCP server.
"""
