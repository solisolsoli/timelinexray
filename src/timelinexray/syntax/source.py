"""Source-text helpers other packages may use (public internal API of :mod:`.syntax`).

The diff classifier reads upstream text with the same machinery as the Milestone 2
extractors, so a bracket, comma or ``=`` inside a comment or string literal is never read
as code. It imports these names from here, never from the backend modules:

* :func:`mask` / :class:`Masked` - comments and literals blanked with every character
  offset preserved (see :mod:`timelinexray.syntax.lexical` for the recognised forms);
* :class:`Lines` - 1-based line numbers of character offsets (only LF ends a line);
* :func:`match_delimiters` - the offset of the closer of every ``( [ {``, with recovery
  from mismatches reported as "unbalanced".

Validated extraction by a backend is :func:`timelinexray.syntax.extract_checked`.
"""

from __future__ import annotations

from .lexical import Lines, Masked, mask, match_delimiters

__all__ = ["Lines", "Masked", "mask", "match_delimiters"]
