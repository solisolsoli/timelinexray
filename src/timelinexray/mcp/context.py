"""Per-server state shared by tool handlers: the guarded store, citations and cursors.

Cursors
-------
A continuation cursor is ``base64url(payload) "." hmac`` where the payload binds the tool,
the commit, the index generation, a hash of the other arguments and the next offset, and
the HMAC-SHA256 key is random per server process. A cursor is therefore opaque, cannot be
edited or moved to another query, commit, index generation or tool, and expires with the
process that issued it (stdio servers are restarted by their clients at will; the error
says to repeat the query without a cursor).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ..errors import InvalidInput, TxrayError
from ..index import CodeIndex, Generation
from ..index.schema import INDEX_SCHEMA_VERSION
from ..netguard import DEFAULT_UPSTREAM_URL
from ..snapshot.store import PinRecord, SnapshotStore
from .guard import StoreGuard
from .schema import UNTRUSTED

UPSTREAM_REPO = "xai-org/x-algorithm"
#: The server's response line limit, as seen by handlers that must fit it (``read_span``).
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024
_PERMALINK_BASE = "https://github.com/xai-org/x-algorithm/blob/"
LOCAL_UPSTREAM = "file:// mirror (local location withheld)"


class TimeBudgetExceeded(TxrayError):
    code = "time_budget_exceeded"


def display_path(path: str) -> str:
    """A repository path as valid Unicode text (non-UTF-8 bytes shown as escapes)."""
    return path.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def upstream_label(pin: PinRecord) -> str:
    return pin.upstream_url if pin.upstream_url == DEFAULT_UPSTREAM_URL else LOCAL_UPSTREAM


def generation_id(generation: Generation) -> str:
    """A short, stable identity of an index generation (changes whenever the index does)."""
    canonical = json.dumps(
        {"index_schema": INDEX_SCHEMA_VERSION, **generation.to_dict()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "g1-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


@dataclass
class Pager:
    """Paging state of one call: where this page starts and how to continue."""

    context: "ToolContext"
    tool: str
    commit: str
    generation: str
    query_hash: str
    offset: int
    total: int = 0

    def cursor_for(self, offset: int) -> str | None:
        if offset >= self.total:
            return None
        return self.context.make_cursor(
            {"t": self.tool, "c": self.commit, "g": self.generation, "q": self.query_hash,
             "o": offset}
        )


class ToolContext:
    """Everything a handler may touch. Created once per server; reset per call."""

    def __init__(self, guard: StoreGuard, *, max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES) -> None:
        self.guard = guard
        self.store = SnapshotStore(guard.root)
        self.index = CodeIndex(self.store)
        self._key = secrets.token_bytes(32)
        self.deadline = float("inf")
        self.max_response_bytes = max_response_bytes

    # -- time -----------------------------------------------------------------------

    def start_call(self, budget_seconds: float) -> None:
        self.deadline = time.monotonic() + budget_seconds

    def check_deadline(self) -> None:
        if time.monotonic() > self.deadline:
            raise TimeBudgetExceeded("the per-call time budget was exceeded")

    # -- store access ---------------------------------------------------------------

    def pin(self, commit: str) -> PinRecord:
        """The pin record of ``commit`` after checking its files lie inside the store."""
        pin = self.store.get_pin(commit)
        self.guard.inside(self.store.root / "pins" / f"{pin.commit}.json", "the pin record")
        self.guard.inside(self.store.root / pin.mirror, "the pin's mirror")
        self.guard.inside(self.store.root / pin.manifest, "the pin's manifest")
        return pin

    def generation(self, commit: str) -> tuple[Generation, str]:
        """The index generation of a pinned commit (NotFound if it is not indexed)."""
        self.guard.inside(self.index.path, "the index database")
        generation = self.index.generation(commit)
        return generation, generation_id(generation)

    def generation_or_none(self, commit: str) -> tuple[Generation, str] | None:
        if not self.index.path.is_file():
            return None
        try:
            return self.generation(commit)
        except TxrayError as exc:
            if exc.code == "not_found":
                return None
            raise

    # -- citations ------------------------------------------------------------------

    def citation(
        self,
        pin: PinRecord,
        path: str,
        start_line: int,
        end_line: int,
        span_sha256: str,
        blob_oid: str,
        anchor: str | None = None,
    ) -> dict[str, Any]:
        default = pin.upstream_url == DEFAULT_UPSTREAM_URL
        shown = display_path(path)
        url = None
        if default:
            lines = f"L{start_line}" if start_line == end_line else f"L{start_line}-L{end_line}"
            url = f"{_PERMALINK_BASE}{pin.commit}/{quote(shown, safe='/')}#{lines}"
        return {
            "repo": UPSTREAM_REPO if default else None,
            "commit": pin.commit,
            "path": shown,
            "start_line": start_line,
            "end_line": end_line,
            "span_sha256": span_sha256,
            "blob_oid": blob_oid,
            "anchor": anchor,
            "url": url,
            "content_trust": UNTRUSTED,
        }

    # -- cursors --------------------------------------------------------------------

    @staticmethod
    def query_hash(tool: str, args: dict[str, Any]) -> str:
        bound = {key: value for key, value in args.items() if key != "cursor"}
        text = json.dumps([tool, bound], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    def _sign(self, payload: bytes) -> str:
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()[:32]

    def make_cursor(self, fields: dict[str, Any]) -> str:
        payload = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("ascii")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return f"{encoded}.{self._sign(payload)}"

    def pager(
        self, tool: str, args: dict[str, Any], commit: str, generation: str
    ) -> Pager:
        """A pager for this call, starting where ``args["cursor"]`` says (or at 0)."""
        query = self.query_hash(tool, args)
        cursor = args.get("cursor")
        offset = 0
        if cursor is not None:
            fields = self._open_cursor(cursor)
            expected = {"t": tool, "c": commit, "g": generation, "q": query}
            if any(fields.get(key) != value for key, value in expected.items()):
                raise InvalidInput(
                    "cursor does not belong to this query, commit or index generation; "
                    "repeat the query without a cursor"
                )
            offset = fields.get("o")
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 1:
                raise InvalidInput("cursor is malformed; repeat the query without a cursor")
        return Pager(self, tool, commit, generation, query, offset)

    def _open_cursor(self, cursor: str) -> dict[str, Any]:
        problem = "cursor is not valid for this server process; repeat the query without a cursor"
        encoded, _, signature = cursor.partition(".")
        try:
            payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (ValueError, TypeError):
            raise InvalidInput(problem) from None
        if not hmac.compare_digest(self._sign(payload), signature):
            raise InvalidInput(problem)
        fields = json.loads(payload)
        if not isinstance(fields, dict):  # pragma: no cover - we only sign dicts
            raise InvalidInput(problem)
        return fields
