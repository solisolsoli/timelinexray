"""Scheduled updates: one guarded fetch, a pin of the new head, a digest against the
previous accepted pin, and explicit exceptional events (P7 section 4.4).

``txray update`` is meant to be run by a scheduler the operator controls (a cron example
is in ``docs/updates.md``); this module never schedules anything, never posts anything and
contacts no network except through :func:`timelinexray.netguard.fetch`.

State lives in the snapshot store (``<store>/updates/state.json``): the upstream URL, the
branch, the *accepted* head (the last pin a digest was produced for on an intact lineage)
and quarantined heads. Pins are never removed: a pin whose commit disappears upstream
keeps its ``refs/txray/pins/<commit>`` ref and stays citable as history.

Outcomes (what happened to the head):

* ``baseline`` - no previous pin: the head is pinned and accepted, nothing is compared;
* ``no-change`` - the head equals the previous pin: "no new public commit observed";
* ``updated`` - a digest ``previous..head`` was written and the head accepted;
* ``attention`` - a digest was written but the head is quarantined, not accepted: upstream
  history was rewritten, or license/notice text changed (promotion pauses for review);
* ``failed`` - the observation failed (fetch failure, branch missing, previous commit
  missing locally): nothing is compared, "no changes" is never claimed, state is kept.

The exit code is 1 when the outcome is ``failed`` or any event needs attention (this
includes pins no longer reachable upstream), else 0. A maintainer resolves a quarantined
head by running ``txray update --since <head>``: an explicit ``--since`` equal to the head
accepts it.

With ``reanchor`` (``txray update --reanchor``) the findings ledger is re-anchored on the
head right after it is pinned and before the digest is built (every active finding on a
head pinned in this run; only the findings without a check at it on a head that was
already pinned), and the review-queue delta is reported; new review items also make the
exit code 1. With ``export_dir`` the Context Layer notes are refreshed at the end of a
successful observation. See :mod:`timelinexray.digest.refresh`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..diff.history import (
    ATTENTION,
    BRANCH_MISSING,
    COMMIT_MISSING,
    COMMIT_UNREACHABLE,
    HISTORY_REWRITTEN,
    INFO,
    LICENSE_CHANGED,
    NO_NEW_COMMIT,
    OBSERVATION_FAILED,
    Event,
    is_ancestor,
    refs_containing,
    upstream_refs,
)
from ..diff.symbols import SymbolSource
from ..errors import GitError, IntegrityError, Refused, TxrayError
from ..findings import VERIFIER_ACTOR, Actor, FindingsMemory, Ledger
from ..fsutil import atomic_write, check_output_directory
from ..netguard import DEFAULT_UPSTREAM_URL, Allowlist, fetch
from ..snapshot.store import SnapshotStore, validate_commit_input
from . import digest_filename, digest_json
from .build import DigestBuilder, upstream_info
from .findings import AffectedFindingsProvider, NullFindingsProvider
from .refresh import LedgerRefresh, refresh_export, refresh_ledger
from .render import render_markdown

STATE_SCHEMA = "timelinexray/update-state/v1"
STATUS_SCHEMA = "timelinexray/update-status/v1"
STATUS_FILE = "update-status.json"

BASELINE = "baseline"
NO_CHANGE = "no-change"
UPDATED = "updated"
NEEDS_ATTENTION = "attention"
FAILED = "failed"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class UpdateResult:
    outcome: str
    exit_code: int
    previous: str | None
    head: str | None
    accepted: str | None
    events: list[Event] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    status: dict[str, Any] = field(default_factory=dict)
    refresh: LedgerRefresh | None = None
    export: dict[str, Any] | None = None


def state_path(store: SnapshotStore) -> Path:
    return store.root / "updates" / "state.json"


def load_state(store: SnapshotStore) -> dict[str, Any] | None:
    path = state_path(store)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"update state {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
        raise IntegrityError(f"update state {path} does not have schema {STATE_SCHEMA}")
    return data


def _save_state(store: SnapshotStore, state: dict[str, Any]) -> None:
    text = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    atomic_write(state_path(store), text.encode("ascii"))


def run_update(
    store: SnapshotStore,
    *,
    out_dir: Path,
    allowlist: Allowlist,
    upstream: str = DEFAULT_UPSTREAM_URL,
    branch: str = "main",
    since: str | None = None,
    findings: AffectedFindingsProvider | None = None,
    symbols: SymbolSource | None = None,
    formats: tuple[str, ...] = ("md", "json"),
    clock: Callable[[], str] = _utc_now,
    ledger: Path | None = None,
    reanchor: bool = False,
    export_dir: Path | None = None,
    actor: Actor = VERIFIER_ACTOR,
) -> UpdateResult:
    """Observe the upstream once and write what changed; see the module documentation.

    ``ledger`` is the findings ledger that ``reanchor`` writes to (as ``actor``) and that
    ``export_dir`` exports from; both are refused, before anything is fetched, without it.
    """
    url = allowlist.check(upstream)  # NetworkRefused before anything else happens
    if not branch or any(ch in branch for ch in " \t\n\\:?*[~^") or ".." in branch:
        raise Refused(f"invalid branch name {branch!r}")
    out_dir = check_output_directory(  # refused before anything is fetched or written
        out_dir, store_root=store.root, ledger=ledger or getattr(findings, "directory", None))
    memory: FindingsMemory | None = None
    if reanchor or export_dir is not None:
        if ledger is None:
            raise Refused("--reanchor and --export need a findings ledger: pass --ledger DIR or "
                          "set $TXRAY_FINDINGS (the default <store>/findings is used only when "
                          "it lies in no git working tree)")
        if not ledger.is_dir():
            raise Refused(f"no findings ledger exists at {ledger}; record findings with "
                          "txray findings add / import, or pass --ledger DIR")
    if reanchor:
        memory = FindingsMemory(Ledger(ledger), store)
        memory.view()  # a ledger that fails verification is refused before the fetch
    if export_dir is not None:
        from ..export.writer import check_destination, check_ledger  # only with --export

        check_ledger(ledger)
        export_dir = check_destination(export_dir, store_root=store.root, ledger=ledger,
                                       option="--export")
    state = load_state(store)
    if state is not None and state.get("upstream_url") != url:
        raise Refused(
            f"this store's updates follow {state.get('upstream_url')!r}; use a separate store "
            "for another upstream"
        )
    if state is None:
        state = {"schema": STATE_SCHEMA, "upstream_url": url, "branch": branch,
                 "accepted": None, "quarantined": []}
    observed_at = clock()
    events: list[Event] = []
    result = UpdateResult(FAILED, 1, None, None, state.get("accepted"), events)

    pinned_before: set[str] = set()

    def refresh(head: str) -> None:
        """Re-anchor the ledger on the pinned ``head`` (before the digest reads it): every
        active finding on a head pinned in this run; on a head that was already pinned
        (no change, or a quarantined head observed again) only the findings without a
        recorded check at it, so nothing is appended to a ledger that is fresh."""
        if memory is not None:
            result.refresh = refresh_ledger(memory, head, only_unchecked=head in pinned_before,
                                            actor=actor)

    def finish(*, observed: bool) -> UpdateResult:
        if export_dir is not None and observed and result.outcome != FAILED:
            assert ledger is not None
            try:
                result.export = refresh_export(export_dir, store_root=store.root, ledger=ledger)
            except TxrayError as exc:  # recorded without its text: the status holds no paths
                result.export = {"failed": True, "error": exc.code, "message": str(exc)}
        return _finish(store, state, result, out_dir, url, branch, observed_at, observed=observed)

    repo = store.open_mirror(url)
    try:
        fetch(url, repo.git_dir, allowlist)
    except GitError as exc:
        events.append(Event(
            OBSERVATION_FAILED, ATTENTION, None,
            f"the guarded fetch failed ({exc}); pins and the accepted commit are kept, and "
            "no claim of 'no changes' is made because nothing was observed",
        ))
        return finish(observed=False)

    refs = upstream_refs(repo)
    head = refs.get(f"refs/heads/{branch}")
    if head is None:
        events.append(Event(
            BRANCH_MISSING, ATTENTION, None,
            f"the upstream has no branch {branch!r} after the fetch (deleted, renamed or the "
            "repository changed); pins are kept and nothing is compared",
        ))
        return finish(observed=True)
    result.head = head

    for pin in store.list_pins():
        if pin.upstream_url != url:
            continue
        pinned_before.add(pin.commit)
        if repo.resolve_commit(pin.commit) != pin.commit:
            events.append(Event(
                COMMIT_MISSING, ATTENTION, pin.commit,
                "a pinned commit's objects are missing from the local mirror; its manifest is "
                "kept, but its spans cannot be re-read until the objects are restored",
            ))
        elif not refs_containing(repo, pin.commit, ["refs/heads/", "refs/tags/"]):
            events.append(Event(
                COMMIT_UNREACHABLE, ATTENTION, pin.commit,
                "a pinned commit is no longer reachable from any upstream branch or tag "
                "(deleted or rewritten upstream); the pin is kept and stays citable as history",
            ))

    previous = _previous(store, state, url, head, since)
    result.previous = previous
    if previous is None:
        store.pin(head, url, allowlist=allowlist)
        state["accepted"] = head
        result.outcome, result.accepted = BASELINE, head
        refresh(head)
        return finish(observed=True)
    if previous == head:
        events.append(Event(NO_NEW_COMMIT, INFO, head,
                            "no new public commit observed on the branch; this does not "
                            "establish that production stopped changing"))
        if since is not None and state.get("accepted") != head:
            store.pin(head, url, allowlist=allowlist)
            state["accepted"] = head  # an explicit maintainer decision
            state["quarantined"] = [c for c in state.get("quarantined", []) if c != head]
        result.outcome = NO_CHANGE
        refresh(head)  # already pinned: only what was never checked at this head
        return finish(observed=True)
    if repo.resolve_commit(previous) != previous:
        if not any(e.kind == COMMIT_MISSING and e.commit == previous for e in events):
            events.append(Event(COMMIT_MISSING, ATTENTION, previous,
                                "the previous accepted commit is missing from the local mirror"))
        result.outcome = FAILED
        return finish(observed=True)

    store.pin(head, url, allowlist=allowlist)
    refresh(head)  # before the digest reads the ledger
    builder = DigestBuilder(store, allowlist=allowlist,
                            findings=findings if findings is not None else NullFindingsProvider(),
                            symbols=symbols)
    if not is_ancestor(repo, previous, head):
        events.append(Event(
            HISTORY_REWRITTEN, ATTENTION, previous,
            f"the previous accepted commit is not an ancestor of the new head {head} of "
            f"{branch!r}: upstream history was rewritten (force-push). The new head is pinned "
            "and quarantined, the accepted commit is unchanged, and a maintainer decision is "
            "required; the digest compares the two trees only",
        ))
        if head not in state.setdefault("quarantined", []):
            state["quarantined"].append(head)
        document = builder.build(previous, head, events=events)
        result.written = _write_digest(out_dir, document, formats)
        result.outcome = NEEDS_ATTENTION
        return finish(observed=True)

    document = builder.build(previous, head, events=events)
    license_items = [item for item in document["items"] if item["class"] == "license"]
    if license_items:
        paths = sorted({item["new_path"] or item["old_path"] for item in license_items})
        event = Event(
            LICENSE_CHANGED, ATTENTION, head,
            "license or notice text changed (" + ", ".join(paths) + "); promotion of the new "
            "head pauses until the change is reviewed (then run: txray update --since "
            f"{head[:12]})",
        )
        events.append(event)
        document["events"].append(event.to_dict())
        if head not in state.setdefault("quarantined", []):
            state["quarantined"].append(head)
        result.written = _write_digest(out_dir, document, formats)
        result.outcome = NEEDS_ATTENTION
        return finish(observed=True)
    result.written = _write_digest(out_dir, document, formats)
    state["accepted"] = head
    state["quarantined"] = [c for c in state.get("quarantined", []) if c != head]
    result.accepted = head
    result.outcome = UPDATED
    return finish(observed=True)


def _previous(
    store: SnapshotStore, state: dict[str, Any], url: str, head: str, since: str | None
) -> str | None:
    if since is not None:
        return store.get_pin(validate_commit_input(since)).commit
    if state.get("accepted"):
        return str(state["accepted"])
    # Without an accepted commit, the newest pin *older than the head*: a pin of the head
    # itself is what an earlier run left behind when it pinned the head and then failed
    # before the digest was written (the accepted commit is only recorded after the digest),
    # and comparing the head with itself would report "no change" for a change never reported.
    pins = [pin for pin in store.list_pins() if pin.upstream_url == url and pin.commit != head]
    if not pins:
        return None
    newest = max(pins, key=lambda pin: (pin.committer_time, pin.commit))
    return newest.commit


def _write_digest(out_dir: Path, document: dict[str, Any], formats: tuple[str, ...]) -> list[str]:
    out_dir.mkdir(exist_ok=True)
    written = []
    for fmt in formats:
        name = digest_filename(document, fmt)
        text = render_markdown(document) if fmt == "md" else digest_json(document)
        atomic_write(out_dir / name, text.encode("utf-8"))
        written.append(name)
    return written


def _finish(
    store: SnapshotStore,
    state: dict[str, Any],
    result: UpdateResult,
    out_dir: Path,
    url: str,
    branch: str,
    observed_at: str,
    *,
    observed: bool,
) -> UpdateResult:
    attention = any(event.severity == ATTENTION for event in result.events)
    new_items = bool(result.refresh is not None and result.refresh.added)
    export_failed = bool(result.export and result.export.get("failed"))
    result.exit_code = 1 if (attention or result.outcome == FAILED or new_items
                             or export_failed) else 0
    result.accepted = state.get("accepted")
    state["branch"] = branch
    state["last_attempt"] = {"at": observed_at, "outcome": result.outcome}
    if observed:
        state["last_successful_observation_at"] = observed_at
        state["last_observed_head"] = result.head
    _save_state(store, state)
    upstream = upstream_info(url)
    result.status = {
        "schema": STATUS_SCHEMA,
        "outcome": result.outcome,
        "observed_at": observed_at,
        "observation_succeeded": observed,
        "upstream": upstream,
        "branch": branch,
        "previous": result.previous,
        "head": result.head,
        "accepted": result.accepted,
        "quarantined": list(state.get("quarantined", [])),
        "events": [event.to_dict() for event in result.events],
        "digests": list(result.written),
        "ledger_refresh": result.refresh.to_dict() if result.refresh is not None else None,
        "export": ({key: value for key, value in result.export.items() if key != "message"}
                   if result.export is not None else None),
        "notes": [
            "Nothing was posted anywhere; outputs are local files.",
            "A schedule is not a freshness guarantee: check observed_at and "
            "observation_succeeded.",
            "ledger_refresh.review_queue.added lists the review items that appeared in this "
            "run (exit code 1): run txray findings queue.",
        ],
    }
    out_dir.mkdir(exist_ok=True)
    text = json.dumps(result.status, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    atomic_write(out_dir / STATUS_FILE, text.encode("ascii"))
    return result
