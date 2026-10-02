"""The network allowlist and the single function through which every fetch passes.

TimelineXray's only network activity is ``git fetch`` from an allowlisted upstream. The
allowlist holds exactly one network URL, :data:`DEFAULT_UPSTREAM_URL`, plus ``file://``
repository URLs that the operator configures explicitly (used for tests and offline
mirrors). Extra entries of any other scheme are rejected when the allowlist is built, so
configuration cannot add a network host.

:func:`fetch` is the only code path that starts a network-capable git process. It checks
the URL *before* any process is started, then runs git with a transport allowlist that
permits only the checked URL's scheme, no redirects, no credential helpers, no hooks, no
submodule recursion, and object validation (``fetch.fsckObjects``).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from .errors import GitError, InvalidInput, NetworkRefused
from .gitio import git_env

#: The one network URL TimelineXray will ever contact.
DEFAULT_UPSTREAM_URL = "https://github.com/xai-org/x-algorithm.git"

#: Environment variable holding extra ``file://`` URLs, separated by whitespace.
ENV_ALLOW_FILE_URLS = "TXRAY_ALLOW_FILE_URLS"

#: Refs mirrored from upstream. Pull-request and other refs are deliberately not fetched.
MIRROR_REFSPECS: tuple[str, ...] = ("+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")

FETCH_TIMEOUT_SECONDS = 900

_SAFE_FILE_PATH = re.compile(r"/[A-Za-z0-9._~+@/-]*")


def canonical_file_url(url: str) -> str:
    """Normalise a ``file://`` repository URL to ``file://<realpath>`` or raise InvalidInput."""
    if not isinstance(url, str) or not url.startswith("file://"):
        raise InvalidInput(f"not a file:// URL: {url!r}")
    parts = urlsplit(url)
    if parts.netloc:
        raise InvalidInput(f"file:// URL must not name a host: {url!r}")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise InvalidInput(f"file:// URL must not carry a query or fragment: {url!r}")
    if not _SAFE_FILE_PATH.fullmatch(parts.path):
        raise InvalidInput(
            f"file:// URL path must be absolute and use only letters, digits and ._~+@/- : {url!r}"
        )
    return "file://" + os.path.realpath(parts.path)


def _has_unsafe_characters(url: str) -> bool:
    return any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url)


class Allowlist:
    """The set of repository URLs that :func:`fetch` may contact."""

    def __init__(self, file_urls: Iterable[str] = ()) -> None:
        self._network = frozenset({DEFAULT_UPSTREAM_URL})
        self._files = frozenset(canonical_file_url(url) for url in file_urls)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Allowlist":
        """Default allowlist plus the ``file://`` URLs listed in ``TXRAY_ALLOW_FILE_URLS``."""
        environ = os.environ if environ is None else environ
        raw = environ.get(ENV_ALLOW_FILE_URLS, "")
        try:
            return cls(raw.split())
        except InvalidInput as exc:
            raise InvalidInput(f"{ENV_ALLOW_FILE_URLS}: {exc}") from exc

    @property
    def urls(self) -> tuple[str, ...]:
        return tuple(sorted(self._network | self._files))

    def check(self, url: object) -> str:
        """Return the canonical form of an allowed ``url``; raise NetworkRefused otherwise."""
        if not isinstance(url, str) or not url:
            raise NetworkRefused(f"refused: repository URL must be a non-empty string, got {url!r}")
        if _has_unsafe_characters(url):
            raise NetworkRefused(f"refused {url!r}: URL contains whitespace or control characters")
        if url in self._network:
            return url
        if url.startswith("file://"):
            try:
                canonical = canonical_file_url(url)
            except InvalidInput as exc:
                raise NetworkRefused(f"refused {url!r}: {exc}") from exc
            if canonical in self._files:
                return canonical
            raise NetworkRefused(
                f"refused {url!r}: file:// URLs must be configured explicitly "
                f"(set {ENV_ALLOW_FILE_URLS})"
            )
        raise NetworkRefused(
            f"refused {url!r}: not on the allowlist; the only network URL is {DEFAULT_UPSTREAM_URL}"
        )


def network_git_options(scheme: str) -> tuple[str, ...]:
    """Git ``-c`` options for a network-capable command restricted to one transport."""
    if scheme not in ("https", "file"):
        raise NetworkRefused(f"refused: transport {scheme!r} is never enabled")
    return (
        "-c", "protocol.allow=never",
        "-c", f"protocol.{scheme}.allow=always",
        "-c", "http.followRedirects=false",
        "-c", "credential.helper=",
        "-c", f"core.hooksPath={os.devnull}",
        "-c", "core.fsmonitor=false",
        "-c", "fetch.fsckObjects=true",
        "-c", "submodule.recurse=false",
    )


def fetch(
    url: str,
    git_dir: Path,
    allowlist: Allowlist,
    *,
    refspecs: Sequence[str] = MIRROR_REFSPECS,
    timeout: float = FETCH_TIMEOUT_SECONDS,
) -> str:
    """Fetch ``refspecs`` from ``url`` into the bare repository ``git_dir``.

    This is the only function in TimelineXray that starts a network-capable process. The
    allowlist check happens first; a refused URL raises :class:`NetworkRefused` without
    starting git. Returns the canonical URL that was fetched.
    """
    canonical = allowlist.check(url)
    scheme = canonical.split(":", 1)[0]
    argv = [
        "git",
        f"--git-dir={git_dir}",
        *network_git_options(scheme),
        "fetch",
        "--quiet",
        "--prune",
        "--no-tags",
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        "--no-auto-maintenance",
        "--",
        canonical,
        *refspecs,
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, env=git_env(), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git fetch from {canonical} timed out after {timeout:.0f} s") from exc
    except FileNotFoundError as exc:  # pragma: no cover - depends on the host
        raise GitError("git executable not found on PATH") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git fetch from {canonical} failed (exit {proc.returncode}): {detail}")
    return canonical
