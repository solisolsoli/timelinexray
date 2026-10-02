"""Small file-system helpers shared by several packages (public internal API).

:func:`atomic_write` is the one way TimelineXray replaces a file it owns: the bytes go to
a temporary file in the same directory, are flushed and ``fsync``-ed, and then replace the
target with :func:`os.replace`, so a reader sees either the old or the new content, never
a partial write (a symbolic link at the target is replaced, never followed). The
temporary file is removed when anything fails. The replaced file keeps its permission bits;
a new file gets ``0o644`` under the umask (never group- or world-writable).

:func:`check_output_directory` is the one rule for where a command may write a directory
of files the user asked for (``txray digest --out``, ``txray update --out``, and, with its
own additions, ``txray export``): it refuses a symbolic link, an existing path that is not
a directory, a missing parent (files are written only inside the directory given), a
location inside the TimelineXray source tree or installed package, a location inside or
equal to the snapshot store or the findings ledger, and a location inside, above or
around an analytics dataset. Paths are compared after resolving the symbolic links of
existing parents, and a directory also counts as the same when it is the same file-system
object under another spelling (case-insensitive volumes). This is path validation, not a
sandbox against another process that swaps directories while a command runs.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
import tomllib
from pathlib import Path

from .errors import Refused

_PACKAGE_DIR = Path(__file__).resolve().parent


def _is_source_tree(directory: Path) -> bool:
    """Whether ``directory`` holds a pyproject.toml that declares the timelinexray project."""
    candidate = directory / "pyproject.toml"
    try:
        if not candidate.is_file():
            return False
        data = tomllib.loads(candidate.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    project = data.get("project")
    return isinstance(project, dict) and project.get("name") == "timelinexray"


def is_within(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` or lies inside it.

    The textual test (``path`` equal to ``root`` or below it) is not enough on a
    case-insensitive file system (the default APFS volume on macOS), where ``Store`` and
    ``store`` are one directory: ``path`` or one of its parents must also not be the very
    same directory (``os.path.samestat``) as ``root``. A missing ``root`` or ancestor is
    skipped, so only the textual test applies to a path that does not exist yet.
    """
    if path == root or root in path.parents:
        return True
    try:
        root_stat = os.stat(root)
    except OSError:
        return False
    for ancestor in (path, *path.parents):
        try:
            if os.path.samestat(os.stat(ancestor), root_stat):
                return True
        except OSError:
            continue
    return False


def resolved_path(path: Path) -> Path:
    """``path`` made absolute (``~`` expanded) with existing symbolic links resolved."""
    return Path(os.path.abspath(Path(path).expanduser())).resolve(strict=False)


def check_output_directory(
    out: Path, *, store_root: Path | None = None, ledger: Path | None = None,
    option: str = "--out",
) -> Path:
    """The absolute directory ``out`` after the refusals of the module documentation.

    ``store_root`` and ``ledger`` name the snapshot store and the findings ledger the
    command uses (``None`` when it has none); ``option`` names the argument in messages.
    """
    from .mcp.guard import find_analytics_dataset  # the guard is loaded only when needed

    absolute = Path(os.path.abspath(Path(out).expanduser()))
    if os.path.islink(absolute):
        raise Refused(f"refused: {option} {absolute} is a symbolic link; give the real directory")
    if os.path.lexists(absolute) and not absolute.is_dir():
        raise Refused(f"refused: {option} {absolute} exists and is not a directory")
    real = absolute.resolve(strict=False)
    for ancestor in (real, *real.parents):
        if _is_source_tree(ancestor):
            raise Refused(f"refused: {option} {absolute} lies inside the TimelineXray working "
                          f"tree {ancestor}; write into a folder outside it")
    if is_within(real, _PACKAGE_DIR):
        raise Refused(f"refused: {option} {absolute} lies inside the TimelineXray package "
                      f"{_PACKAGE_DIR}")
    for what, root in (("snapshot store", store_root), ("findings ledger", ledger)):
        if root is not None and is_within(real, resolved_path(root)):
            raise Refused(f"refused: {option} {absolute} lies inside the {what} "
                          f"{resolved_path(root)}")
    if not absolute.exists() and not absolute.parent.is_dir():
        raise Refused(f"refused: the parent directory of {option} {absolute} does not exist; "
                      f"create it first (files are written only inside {option})")
    found = find_analytics_dataset(real, walk=real.is_dir())
    if found is not None:
        path, reason = found
        where = "contains" if real in path.parents and path.parent != real else "lies inside"
        raise Refused(f"refused: {option} {absolute} {where} an analytics dataset ({reason}). "
                      "Analytics data is private and never leaves its dataset; choose a "
                      "folder outside every analytics dataset")
    return absolute


def _create_temp(directory: Path, stem: str) -> tuple[int, str]:
    """A new, exclusively created file next to the target, like :func:`tempfile.mkstemp` but
    readable like an ordinary new file (``0o644`` under the umask), not ``0o600``."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    for _ in range(100):
        tmp = os.path.join(directory, f".{stem}.{secrets.token_hex(6)}.tmp")
        try:
            return os.open(tmp, flags, 0o644), tmp
        except FileExistsError:
            continue
    raise FileExistsError(f"cannot create a temporary file in {directory}")


def atomic_write(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically, creating missing parent directories.

    The file keeps the permission bits of the regular file it replaces; a new file gets
    ``0o644`` under the user's umask: readable by others as a ``mkstemp`` file (``0o600``) is
    not, but never group- or world-writable even under a permissive umask such as ``0o002``
    (store, ledger and digest files must not be writable by others). Secrecy is not this function's job: files that must stay
    private (the analytics dataset, ``analytics/dataset.py``) are written by their own code
    with an explicit ``0o600``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    fd, tmp = _create_temp(path.parent, path.name)
    try:
        with os.fdopen(fd, "wb") as handle:
            if existing is not None and stat.S_ISREG(existing.st_mode):
                os.fchmod(handle.fileno(), stat.S_IMODE(existing.st_mode))
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


__all__ = ["atomic_write", "check_output_directory", "is_within", "resolved_path"]
