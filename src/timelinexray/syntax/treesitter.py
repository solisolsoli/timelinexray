"""Hook for an optional tree-sitter backend. Not implemented in this release.

Status
------
No tree-sitter runtime or grammar is installed or depended on. The spec allows tree-sitter
only as an optional extra (``[syntax]``) with pinned grammar versions, and none could be
installed offline when Milestone 2 was built, so this module provides the hook and nothing
else: :class:`TreeSitterHook` is registered ahead of the lexical backend but its
:meth:`~TreeSitterHook.probe` always reports a reason, so it is never selected and never
imports anything.

Contract for a future backend
-----------------------------
A tree-sitter backend becomes usable by replacing :meth:`TreeSitterHook.probe` and
:meth:`TreeSitterHook.extract` (or registering another object implementing
:class:`timelinexray.syntax.Backend`) under these rules:

1. Packaging: an optional dependency group ``syntax`` that pins the exact versions of the
   ``tree_sitter`` runtime and of each grammar (Rust, Scala, Python, Java). Wheels only;
   no grammar is compiled or downloaded at run time.
2. Versioning: ``version`` is ``"<adapter version>+<grammar pins hash>"``, so a grammar
   upgrade changes the parse-cache key ``(blob_oid, language, backend, version)`` and forces
   those blobs to be re-extracted, while lexical rows and other languages stay cached.
3. ``probe()`` returns ``None`` only when the runtime and the pinned grammar for every
   language in ``languages`` import and report the pinned versions; otherwise it returns
   the reason, and :func:`timelinexray.syntax.select_backend` falls back to ``lexical``.
4. Output uses the same records and vocabulary: kinds from ``SYMBOL_KINDS``, 1-based
   inclusive ``start_line``/``name_line``/``end_line`` on the blob's LF line numbering, and
   call candidates that stay unresolved (tree-sitter gives syntax, not name resolution).
5. A file whose tree contains error nodes reports status ``partial`` with the reason
   ``syntax-errors``; the index keeps per-file coverage and per-symbol backend names, so a
   mixed index (tree-sitter for some languages, lexical for others) stays explicit.
6. Parsing must stay free of execution: no build scripts, macro expansion, plugins or
   imports of upstream code.
"""

from __future__ import annotations

from . import Extraction

NAME = "tree-sitter"
UNAVAILABLE = (
    "not bundled: no pinned tree-sitter runtime or grammar is installed, and this release "
    "implements the hook only (see timelinexray.syntax.treesitter)"
)


class TreeSitterHook:
    name = NAME
    version = "0"
    languages = frozenset({"java", "python", "rust", "scala"})

    def probe(self) -> str | None:
        return UNAVAILABLE

    def extract(self, text: str, language: str) -> Extraction:
        raise RuntimeError(f"the {NAME} backend is unavailable: {UNAVAILABLE}")
