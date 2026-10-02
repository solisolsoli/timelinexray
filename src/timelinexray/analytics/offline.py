"""The process-level offline guard of the analytics commands.

``txray metrics ...`` calls :func:`enter` before it imports any other analytics module.
:func:`enter` installs a CPython audit hook (``sys.addaudithook``). Audit hooks cannot be
removed, so from that moment until the process exits:

* every socket operation (creating a socket, resolving a name, connecting, binding,
  sending) raises :class:`OfflineViolation`: no analytics code can reach any network;
* starting a process (``subprocess``, ``os.system``, ``os.exec*``, ``os.posix_spawn``,
  ``os.fork``) and loading native code through ``ctypes`` are refused the same way;
* files may be written only inside the directories given as ``allowed_write_roots`` (the
  dataset directory of ``txray metrics import``, nothing for the other commands) and never
  inside a forbidden root such as the snapshot store, which the MCP server serves;
* the writers whose files the write check cannot see are refused outright: every
  ``sqlite3.connect`` (SQLite creates its database, journal and ``ATTACH``-ed files in C,
  without an ``open`` event) and the import of the ``dbm`` C modules ``_dbm`` and
  ``_gdbm`` (``dbm.ndbm``, ``dbm.gnu``). The analytics code uses neither.

The bytecode cache is switched off too, so importing modules afterwards writes nothing.

Audit hooks observe and veto the operations that Python code performs; they are not a
sandbox against hostile native code. They do cover every socket, process and file-write
path that the analytics code, which is pure Python, can take.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ..errors import NetworkRefused, Refused

#: Custom audit event raised once the guard is active (lets a test check the ordering).
ENTER_EVENT = "timelinexray.analytics.offline.enter"

#: Same variable as ``timelinexray.snapshot.store.ENV_STORE``.
ENV_STORE = "TXRAY_STORE"


class OfflineViolation(NetworkRefused):
    """An analytics process tried to use the network, start a process or load native code."""

    code = "offline_violation"


class WriteRefused(Refused):
    """An analytics process tried to write outside its declared output directory."""

    code = "write_refused"


_BLOCKED_PREFIXES = ("socket.", "ctypes.")
_BLOCKED_EVENTS = frozenset(
    {
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.fork",
        "os.forkpty",
        "os.startfile",
        "pty.spawn",
        "webbrowser.open",
        "urllib.Request",
        "http.client.connect",
        "ftplib.connect",
        "smtplib.connect",
        "imaplib.open",
        "poplib.connect",
        "nntplib.connect",
        "telnetlib.Telnet.open",
    }
)

#: Events of writers whose files are created in C without an audited ``open`` (FA-019).
_UNCHECKED_WRITER_EVENTS = frozenset({"sqlite3.connect"})
#: C modules that create files without an audited ``open``; refused at import.
_UNCHECKED_WRITER_MODULES = frozenset({"_dbm", "_gdbm"})

#: Events that modify the file system: (indexes of written paths, indexes of dir_fd arguments).
_WRITE_EVENTS: Mapping[str, tuple[tuple[int, ...], tuple[int, ...]]] = {
    "os.mkdir": ((0,), (2,)),
    "os.rename": ((0, 1), (2, 3)),
    "os.remove": ((0,), (1,)),
    "os.rmdir": ((0,), (1,)),
    "os.symlink": ((1,), (2,)),
    "os.link": ((1,), (2, 3)),
    "os.truncate": ((0,), ()),
    "os.chmod": ((0,), (2,)),
    "os.chown": ((0,), (3,)),
    "os.utime": ((0,), (3,)),
    "os.chflags": ((0,), ()),
    "os.lchflags": ((0,), ()),
    "os.lchmod": ((0,), ()),
    "os.setxattr": ((0,), ()),
    "os.removexattr": ((0,), ()),
    "os.mkfifo": ((0,), (2,)),
    "os.mknod": ((0,), (3,)),
    "shutil.copyfile": ((1,), ()),
    "shutil.copymode": ((1,), ()),
    "shutil.copystat": ((1,), ()),
    "shutil.copytree": ((1,), ()),
    "shutil.rmtree": ((0,), (1,)),
    "shutil.move": ((0, 1), ()),
    "shutil.chown": ((0,), ()),
    "shutil.make_archive": ((0,), ()),
}

_WRITE_MODE_CHARS = frozenset("wax+")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

_lock = threading.Lock()
_local = threading.local()
_state: dict[str, tuple[str, ...]] | None = None


def snapshot_store_root(
    store: str | os.PathLike[str] | None = None, environ: Mapping[str, str] | None = None
) -> Path:
    """The snapshot store location, by the rule of ``snapshot.store.default_store_root``.

    Restated here so that an analytics process never imports the snapshot store, which can
    start git. A test keeps the two rules equal.
    """
    if store:
        return Path(store).expanduser()
    environ = os.environ if environ is None else environ
    if environ.get(ENV_STORE):
        return Path(environ[ENV_STORE]).expanduser()
    base = environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(base).expanduser() / "timelinexray"


def resolve(path: str | bytes | os.PathLike[Any]) -> str:
    """Absolute, symlink-free form of ``path`` (which need not exist)."""
    return os.path.realpath(os.path.abspath(os.fsdecode(os.fspath(path))))


def is_within(path: str, root: str) -> bool:
    """True if resolved ``path`` is ``root`` or lies below it."""
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def check_output_location(
    out_dir: str | os.PathLike[str], forbidden_roots: Iterable[str | os.PathLike[str]]
) -> str:
    """Resolve ``out_dir`` and refuse it if it overlaps any forbidden root."""
    target = resolve(out_dir)
    for root in forbidden_roots:
        bad = resolve(root)
        if is_within(target, bad) or is_within(bad, target):
            raise WriteRefused(
                f"refused to write analytics data to {out_dir}: it overlaps the snapshot store "
                f"({root}), which other TimelineXray components serve; choose a private "
                "directory outside the store"
            )
    return target


def enter(
    *,
    allowed_write_roots: Iterable[str | os.PathLike[str]] = (),
    forbidden_roots: Iterable[str | os.PathLike[str]] = (),
) -> None:
    """Make this process offline for the rest of its life (see the module documentation)."""
    global _state
    allowed = tuple(sorted({resolve(p) for p in allowed_write_roots}))
    forbidden = tuple(sorted({resolve(p) for p in forbidden_roots}))
    for root in allowed:
        check_output_location(root, forbidden)
    wanted = {"allowed": allowed, "forbidden": forbidden}
    with _lock:
        if _state is not None:
            if _state == wanted:
                return
            raise RuntimeError("offline mode is already active with different write roots")
        _state = wanted
        sys.dont_write_bytecode = True
        sys.addaudithook(_hook)
    sys.audit(ENTER_EVENT, allowed, forbidden)


def is_active() -> bool:
    """True once :func:`enter` has run in this process."""
    return _state is not None


def status() -> dict[str, Any]:
    """Machine-readable description of the guard, for command output."""
    state = _state or {"allowed": (), "forbidden": ()}
    return {
        "active": _state is not None,
        "mechanism": "CPython audit hook installed before any analytics module is imported",
        "blocked": ["sockets and name resolution", "process creation", "ctypes",
                    "SQLite connections", "the dbm C modules"],
        "writes_allowed_in": list(state["allowed"]),
        "writes_forbidden_in": list(state["forbidden"]),
    }


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if _state is None or getattr(_local, "busy", False):
        return
    if event.startswith(_BLOCKED_PREFIXES) or event in _BLOCKED_EVENTS:
        raise OfflineViolation(
            f"refused {event}: analytics commands run offline (network access, process "
            "creation and native code loading are disabled in this process)"
        )
    if event in _UNCHECKED_WRITER_EVENTS:
        raise WriteRefused(
            f"refused {event}: analytics commands open no SQLite database (SQLite creates and "
            "attaches files that the write check cannot see)"
        )
    if event == "import":
        if args and args[0] in _UNCHECKED_WRITER_MODULES:
            raise WriteRefused(
                f"refused import of {args[0]}: analytics commands do not use the dbm C modules "
                "(they create files that the write check cannot see)"
            )
        return
    if event == "open":
        if _opens_for_writing(args):
            _check_write(event, args[:1], ())
        return
    spec = _WRITE_EVENTS.get(event)
    if spec is not None:
        paths = [args[i] for i in spec[0] if i < len(args)]
        dir_fds = [args[i] for i in spec[1] if i < len(args)]
        _check_write(event, paths, dir_fds)


def _opens_for_writing(args: tuple[Any, ...]) -> bool:
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else None
    if isinstance(mode, str) and _WRITE_MODE_CHARS.intersection(mode):
        return True
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def _check_write(event: str, paths: Iterable[Any], dir_fds: Iterable[Any]) -> None:
    assert _state is not None
    _local.busy = True
    try:
        for fd in dir_fds:
            if isinstance(fd, int) and fd >= 0:
                raise WriteRefused(f"refused {event}: dir_fd-relative writes are not allowed")
        for path in paths:
            if path is None or isinstance(path, int):
                continue  # a file descriptor that an earlier, checked call opened
            target = resolve(path)
            if any(is_within(target, root) for root in _state["forbidden"]):
                raise WriteRefused(f"refused {event} on {target}: inside the snapshot store")
            if not any(is_within(target, root) for root in _state["allowed"]):
                raise WriteRefused(
                    f"refused {event} on {target}: analytics commands write only to their "
                    "declared output directory"
                )
    finally:
        _local.busy = False
