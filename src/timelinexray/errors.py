"""Error types shared by the library and the CLI.

Every error carries a stable machine-readable ``code`` and the process exit code the CLI
uses for it. Messages are written for a person reading a terminal: they say what was
requested, what was found, and what to do next.
"""

from __future__ import annotations


class TxrayError(Exception):
    """Base class for all expected, user-facing errors."""

    code = "error"
    exit_code = 1


class InvalidInput(TxrayError):
    """A command-line value or API argument is malformed."""

    code = "invalid_input"
    exit_code = 2


class NetworkRefused(TxrayError):
    """A URL is not on the network allowlist; nothing was contacted."""

    code = "network_refused"
    exit_code = 3


class GitError(TxrayError):
    """A git subprocess failed or produced output this tool cannot interpret."""

    code = "git_error"


class NotFound(TxrayError):
    """A commit, pin, or path does not exist where it was looked up."""

    code = "not_found"


class Refused(TxrayError):
    """The object exists but this operation is deliberately not performed on it."""

    code = "refused"


class IntegrityError(TxrayError):
    """Stored bytes do not match their recorded hash or identity."""

    code = "integrity_error"


class SpanRangeError(TxrayError):
    """A requested line range does not lie inside the blob."""

    code = "span_range"
