"""The append-only event log of the findings memory, chained with SHA-256.

Layout of a ledger directory::

    events.jsonl   one event per line, canonical JSON (sorted keys, no spaces, ASCII)
    HEAD           {"hash": ..., "seq": ...} of the last event, rewritten atomically
    .lock          advisory lock taken by every writer

Every event has ``seq`` (1, 2, 3, ...), ``prev`` (the previous event's ``hash``; 64 zeros
for the first event) and ``hash``: the SHA-256 of the canonical JSON of the event without
its ``hash`` field. :func:`verify_log` recomputes every hash and link and compares the last
event with ``HEAD``, which detects a changed byte (hash), a reordered, removed or inserted
event (``seq`` and ``prev``), a truncated tail (``HEAD``) and a torn final write (a line
without its newline).

A hash chain is not a signature: someone able to rewrite the whole file and ``HEAD``
consistently is not detected. Compare the head with an independently kept copy
(``txray findings verify-log --expect-head HASH``) when that matters.

Recovery: a crash between the event append and the ``HEAD`` replacement leaves an intact
log with a ``HEAD`` behind it (``head_mismatch``). :meth:`Ledger.repair_head`
(``txray findings verify-log --repair-head``) rewrites ``HEAD`` from the verified log in
exactly that case and refuses every other problem.

Writers take an exclusive ``flock`` on ``.lock``, re-verify the whole chain, optionally
check an expected head (compare-and-swap), append all events of one operation in a single
write followed by ``fsync``, and only then replace ``HEAD``. An operation either appends all
its events or none.

The event log records when each action happened (``time``, UTC). It is a log of actions,
not a derived artifact, so it is the one place in TimelineXray where wall-clock time is
part of hashed content.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import IntegrityError, InvalidInput, Refused
from ..fsutil import atomic_write
from .model import EVENT_TYPES, Actor, canonical_json, check_finding_id, sha256_hex

EVENT_SCHEMA = "timelinexray/findings-event/v1"
GENESIS = "0" * 64
EVENTS_FILE = "events.jsonl"
HEAD_FILE = "HEAD"
LOCK_FILE = ".lock"
_EVENT_KEYS = frozenset(
    {"schema", "seq", "type", "time", "actor", "finding_id", "payload", "prev", "hash"}
)
#: Problems ``repair_head`` may fix: a ``HEAD`` that is missing, malformed or behind an
#: intact log. Every other problem is in the log itself and is never repaired.
REPAIRABLE_CODES = frozenset({"head_mismatch", "head_missing", "head_malformed"})


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    type: str
    time: str
    actor: Actor
    finding_id: str
    payload: dict[str, Any]
    prev: str
    hash: str

    def body(self) -> dict[str, Any]:
        return {
            "schema": EVENT_SCHEMA,
            "seq": self.seq,
            "type": self.type,
            "time": self.time,
            "actor": self.actor.to_dict(),
            "finding_id": self.finding_id,
            "payload": self.payload,
            "prev": self.prev,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "hash": self.hash}

    def line(self) -> bytes:
        return canonical_json(self.to_dict()) + b"\n"


def compute_hash(body: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(body))


@dataclass
class LogReport:
    """Result of :func:`verify_log`."""

    events: list[Event] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)
    head: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def last_hash(self) -> str:
        return self.events[-1].hash if self.events else GENESIS

    def add(self, code: str, message: str, line: int | None = None) -> None:
        self.problems.append({"code": code, "line": line, "message": message})

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "events": len(self.events),
            "last": {"seq": len(self.events), "hash": self.last_hash},
            "head": self.head,
            "problems": self.problems,
        }


def _parse_event(obj: Any) -> Event:
    if not isinstance(obj, dict) or set(obj) != _EVENT_KEYS:
        raise ValueError("an event must have exactly the keys " + ", ".join(sorted(_EVENT_KEYS)))
    if obj["schema"] != EVENT_SCHEMA:
        raise ValueError(f"unknown event schema {obj['schema']!r}")
    seq = obj["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise ValueError("seq must be a positive integer")
    if obj["type"] not in EVENT_TYPES:
        raise ValueError(f"unknown event type {obj['type']!r}")
    for key in ("time", "prev", "hash"):
        if not isinstance(obj[key], str):
            raise ValueError(f"{key} must be a string")
    if not isinstance(obj["payload"], dict):
        raise ValueError("payload must be an object")
    try:
        actor = Actor.from_dict(obj["actor"])
        finding_id = check_finding_id(obj["finding_id"])
    except InvalidInput as exc:
        raise ValueError(str(exc)) from None
    return Event(seq, obj["type"], obj["time"], actor, finding_id, obj["payload"],
                 obj["prev"], obj["hash"])


def verify_log(directory: Path, *, expect_head: str | None = None) -> LogReport:
    """Check every hash and link of a ledger directory (see the module documentation)."""
    report = LogReport()
    events_path = directory / EVENTS_FILE
    head_path = directory / HEAD_FILE
    data = events_path.read_bytes() if events_path.exists() else b""
    if data and not data.endswith(b"\n"):
        report.add("torn_write", "the last line has no newline: a write was interrupted or "
                   "the file was cut", line=data.count(b"\n") + 1)
    prev = GENESIS
    for number, raw in enumerate(data.split(b"\n"), start=1):
        if not raw:
            if number <= data.count(b"\n"):
                report.add("blank_line", "blank line in the event log", line=number)
            continue
        try:
            obj = json.loads(raw)
            event = _parse_event(obj)
        except (UnicodeDecodeError, ValueError) as exc:
            report.add("malformed", f"not a valid event: {exc}", line=number)
            continue
        expected_seq = len(report.events) + 1
        if raw != canonical_json(obj):
            report.add("not_canonical", "the line is not in canonical JSON form", line=number)
        if compute_hash(event.body()) != event.hash:
            report.add("hash_mismatch", f"event {event.seq}: its content does not hash to its "
                       "recorded hash (the event was changed)", line=number)
        if event.seq != expected_seq:
            report.add("seq_mismatch", f"expected event {expected_seq} here, found {event.seq} "
                       "(events were removed, inserted or reordered)", line=number)
        if event.prev != prev:
            report.add("chain_broken", f"event {event.seq}: prev does not match the hash of the "
                       "event before it (events were removed, inserted or reordered)",
                       line=number)
        prev = event.hash
        report.events.append(event)
    head: dict[str, Any] | None = None
    if head_path.exists():
        try:
            head = json.loads(head_path.read_bytes())
            if not (isinstance(head, dict) and set(head) == {"hash", "seq"}):
                raise ValueError
        except (UnicodeDecodeError, ValueError):
            report.add("head_malformed", f"{HEAD_FILE} is not a valid head record")
            head = None
    report.head = head
    last = {"seq": len(report.events), "hash": report.last_hash}
    if head is None and report.events and not any(p["code"] == "head_malformed"
                                                   for p in report.problems):
        report.add("head_missing", f"{HEAD_FILE} is missing although the log has events")
    elif head is not None and head != last:
        if isinstance(head.get("seq"), int) and head["seq"] > last["seq"]:
            report.add("truncated", f"{HEAD_FILE} records {head['seq']} events but the log holds "
                       f"{last['seq']}: events were removed from the end")
        else:
            report.add("head_mismatch", f"{HEAD_FILE} ({head.get('seq')}, {head.get('hash')}) "
                       f"does not match the last event ({last['seq']}, {last['hash']})")
    if expect_head is not None and expect_head != report.last_hash:
        report.add("unexpected_head", f"the last event hash is {report.last_hash}, "
                   f"expected {expect_head}")
    return report


class Ledger:
    """A findings ledger directory."""

    def __init__(self, directory: Path | str, *, clock: Callable[[], str] = utc_now) -> None:
        self.directory = Path(directory)
        self.clock = clock

    @property
    def events_path(self) -> Path:
        return self.directory / EVENTS_FILE

    def verify(self, *, expect_head: str | None = None) -> LogReport:
        return verify_log(self.directory, expect_head=expect_head)

    def events(self) -> list[Event]:
        """All events, after a full chain check; refuses to read a damaged log."""
        report = self.verify()
        if not report.ok:
            first = report.problems[0]
            raise IntegrityError(
                f"the findings ledger {self.directory} fails verification "
                f"({len(report.problems)} problem(s); first: {first['message']}); "
                "run: txray findings verify-log"
            )
        return report.events

    def read_events(self) -> list[Event]:
        """:meth:`events` for readers that must not write anything (digests, reports).

        The log and ``HEAD`` must be regular files (a FIFO would block the reader). When the
        ledger's lock file exists, the chain is read under a shared ``flock`` so a writer's
        append is never seen half-done; the lock file is never created, and nothing in the
        directory is modified.
        """
        for name in (EVENTS_FILE, HEAD_FILE, LOCK_FILE):
            path = self.directory / name
            try:
                info = os.stat(path)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise Refused(f"the ledger's {name} is not a regular file")
        try:
            handle = open(self.directory / LOCK_FILE, "rb")
        except FileNotFoundError:
            return self.events()
        with handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return self.events()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def writer(self, *, expect_head: str | None = None) -> Iterator["LedgerWriter"]:
        """Lock, verify, yield a writer, then append its events atomically on success."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.directory / LOCK_FILE, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                events = self.events()
                head = events[-1].hash if events else GENESIS
                if expect_head is not None and expect_head != head:
                    raise Refused(
                        f"the ledger head is {head}, not the expected {expect_head}: another "
                        "write happened first; reload and retry"
                    )
                writer = LedgerWriter(events, self.clock)
                yield writer
                if writer.pending:
                    self._append(writer.pending)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _append(self, events: list[Event]) -> None:
        data = b"".join(event.line() for event in events)
        with open(self.events_path, "ab") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        self._write_head(events[-1])

    def _write_head(self, last: Event) -> None:
        head = (json.dumps({"hash": last.hash, "seq": last.seq}, sort_keys=True) + "\n").encode()
        # the same mode as the events file (an existing HEAD keeps its bits, a new one gets the
        # umask default): a mkstemp file would be 0600 next to a 0644 log
        atomic_write(self.directory / HEAD_FILE, head)

    def repair_head(self) -> dict[str, Any]:
        """Rewrite ``HEAD`` from an intact log whose ``HEAD`` fell behind it.

        This is the recovery after a crash between the event append and the ``HEAD``
        replacement (the only moment at which a writer leaves the two out of step). It is
        allowed only when every event verifies (hashes, ``seq``, ``prev``, canonical lines,
        no torn write) and ``HEAD`` is missing, malformed or names fewer events than the log
        holds. It is refused when the log itself has a problem, when ``HEAD`` names more
        events than the log (``truncated``: events were removed, which nothing can repair)
        or when ``HEAD`` names the same number of events under another hash (the last event
        may have been replaced; compare with an independently kept head instead). The
        writer lock is held, so no writer runs meanwhile.
        """
        if not self.directory.is_dir():
            raise Refused(f"the findings ledger {self.directory} does not exist")
        with open(self.directory / LOCK_FILE, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                report = self.verify()
                codes = {problem["code"] for problem in report.problems}
                last = {"seq": len(report.events), "hash": report.last_hash}
                if not codes:
                    return {"repaired": False, "reason": "HEAD already matches the log",
                            "previous": report.head, "head": last}
                blocking = sorted(codes - REPAIRABLE_CODES)
                if blocking:
                    raise Refused(
                        f"refused: the event log itself fails verification "
                        f"({', '.join(blocking)}); only a HEAD that fell behind an intact log "
                        "can be repaired. Restore the log from a backup or an independently "
                        "kept copy"
                    )
                if not report.events:
                    raise Refused("refused: the log holds no events; nothing to repair")
                head = report.head
                if isinstance(head, dict) and head.get("seq") == last["seq"]:
                    raise Refused(
                        "refused: HEAD names the same number of events as the log under "
                        "another hash, so the last event may have been replaced; verify "
                        "against an independently kept head (--expect-head) instead"
                    )
                self._write_head(report.events[-1])
                return {"repaired": True, "reason": "HEAD rewritten from the verified log",
                        "previous": head, "head": last}
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class LedgerWriter:
    """Collects the events of one operation; they are written when the context exits."""

    def __init__(self, events: list[Event], clock: Callable[[], str]) -> None:
        self.existing = events
        self.pending: list[Event] = []
        self.clock = clock

    @property
    def all_events(self) -> list[Event]:
        return self.existing + self.pending

    def append(self, type_: str, actor: Actor, finding_id: str, payload: dict[str, Any]) -> Event:
        if type_ not in EVENT_TYPES:
            raise InvalidInput(f"unknown event type {type_!r}")
        chain = self.all_events
        prev = chain[-1].hash if chain else GENESIS
        body = {
            "schema": EVENT_SCHEMA,
            "seq": len(chain) + 1,
            "type": type_,
            "time": self.clock(),
            "actor": actor.to_dict(),
            "finding_id": check_finding_id(finding_id),
            "payload": json.loads(canonical_json(payload)),
            "prev": prev,
        }
        event = _parse_event({**body, "hash": compute_hash(body)})
        self.pending.append(event)
        return event
