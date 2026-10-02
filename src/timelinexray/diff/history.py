"""Commit lineage between pins, from the local mirror only, and exceptional events.

Ancestry is answered by git itself (``git for-each-ref --contains``) against the
``refs/txray/pins/<commit>`` ref that pinning creates, so both commits must be pinned. The
first-parent chain and the commits a range contains are walked from commit objects
(:meth:`timelinexray.gitio.GitRepo.commit_info`). Nothing here contacts a network and
commit messages are never read: only the tree, parent and committer fields are parsed.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import GitError, NotFound
from ..gitio import CommitInfo, GitRepo, run_local
from ..snapshot.store import PIN_REF_PREFIX

#: Exceptional upstream events (P7 section 4.4) and their severities.
HISTORY_REWRITTEN = "history-rewritten"
COMMIT_UNREACHABLE = "commit-unreachable-upstream"
COMMIT_MISSING = "pinned-commit-missing"
OBSERVATION_FAILED = "fetch-failure"
BRANCH_MISSING = "branch-missing"
NOT_ANCESTOR = "not-ancestor"
NO_NEW_COMMIT = "no-new-commit"
LICENSE_CHANGED = "license-changed"
ATTENTION = "attention"
INFO = "info"

MAX_WALK = 200_000


@dataclass(frozen=True, slots=True)
class Event:
    """An exceptional or notable upstream observation; never silently dropped."""

    kind: str
    severity: str
    commit: str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "commit": self.commit,
            "message": self.message,
        }


def refs_containing(repo: GitRepo, commit: str, patterns: Sequence[str]) -> list[str]:
    """Names of refs matching ``patterns`` from which ``commit`` is reachable."""
    proc = run_local(
        repo.git_dir,
        ["for-each-ref", "--format=%(refname)", f"--contains={commit}", "--", *patterns],
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git for-each-ref --contains {commit} failed: {detail}")
    return [line for line in proc.stdout.decode("utf-8", "surrogateescape").splitlines() if line]


def upstream_refs(repo: GitRepo) -> dict[str, str]:
    """``refname -> commit`` for the mirrored upstream branches and tags (tags peeled)."""
    proc = run_local(
        repo.git_dir,
        ["for-each-ref", "--format=%(objectname) %(*objectname) %(refname)", "refs/heads/",
         "refs/tags/"],
    )
    refs: dict[str, str] = {}
    for line in proc.stdout.decode("utf-8", "surrogateescape").splitlines():
        parts = line.split(" ", 2)
        if len(parts) != 3:
            continue
        direct, peeled, name = parts
        refs[name] = peeled or direct
    return refs


def is_ancestor(repo: GitRepo, old: str, new: str) -> bool:
    """Whether pinned ``old`` is reachable from pinned ``new`` (equal commits count)."""
    if old == new:
        return True
    return bool(refs_containing(repo, old, [PIN_REF_PREFIX + new]))


class Lineage:
    """Walks of commit objects in one mirror, memoised."""

    def __init__(self, repo: GitRepo) -> None:
        self.repo = repo
        self._info: dict[str, CommitInfo] = {}

    def info(self, commit: str) -> CommitInfo:
        cached = self._info.get(commit)
        if cached is None:
            if self.repo.resolve_commit(commit) != commit:
                raise NotFound(f"commit {commit} is missing from the mirror")
            cached = self.repo.commit_info(commit)
            self._info[commit] = cached
        return cached

    def chain(self, old: str, new: str) -> list[str] | None:
        """Commits from ``old`` to ``new`` (both included) along first parents, or, if
        ``old`` is only reachable through a merge's other parent, along the shortest parent
        path. ``None`` when ``old`` is not an ancestor of ``new``."""
        path = [new]
        current = new
        steps = 0
        while current != old:
            parents = self.info(current).parents
            steps += 1
            if not parents or steps > MAX_WALK:
                break
            current = parents[0]
            path.append(current)
        if path[-1] == old:
            return list(reversed(path))
        # Breadth-first over all parents.
        came_from: dict[str, str | None] = {new: None}
        queue = deque([new])
        while queue and len(came_from) <= MAX_WALK:
            commit = queue.popleft()
            if commit == old:
                result = [commit]
                while came_from[result[-1]] is not None:
                    result.append(came_from[result[-1]])  # type: ignore[arg-type]
                return result
            for parent in self.info(commit).parents:
                if parent not in came_from:
                    came_from[parent] = commit
                    queue.append(parent)
        return None

    def side_commits(self, old: str, chain: list[str]) -> list[str]:
        """Commits in the range that are not on ``chain``: reached through merge parents.

        A commit stops the walk when it is on the chain or an ancestor of ``old``.
        """
        on_chain = set(chain)
        found: list[str] = []
        seen: set[str] = set(on_chain)
        for commit in chain[1:]:
            for parent in self.info(commit).parents[1:]:
                queue = deque([parent])
                while queue:
                    current = queue.popleft()
                    if current in seen:
                        continue
                    seen.add(current)
                    if refs_containing(self.repo, current, [PIN_REF_PREFIX + old]):
                        continue  # an ancestor of old: outside the range
                    found.append(current)
                    queue.extend(self.info(current).parents)
        return found
