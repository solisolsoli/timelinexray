#!/usr/bin/env python3
"""Deterministic semantic-gate harness: the question set checked through the MCP server.

No model is involved. For every expected citation of an *answer* item the harness calls
``read_span`` at the pinned commit and checks the span (outcome ``OK``, span SHA-256 equal
to the oracle hash, anchor ``FOUND``, expected value inside the span text) and then the
lookup the item names (``get_param``: a declaration with that value whose citation overlaps
the expected span; ``find_symbols``: a symbol whose span overlaps it). For every *abstain*
item it runs the item's probes through ``search_code`` and requires zero hits, so that no
supporting span exists in the index. It exits 0 only when every check passes, and its
report carries every denominator.

    python eval/run_gate.py --store DIR [--questions eval/questions.json] [--json PATH]

The store must hold the question set's pinned commits (``commits.pinned``) with the code
index built for ``commits.indexed`` (``txray pin`` / ``txray index``). The harness is a
developer tool: it is not part of the ``timelinexray`` package and talks to
``txray mcp serve`` over stdio like any other MCP client.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUESTIONS = REPO_ROOT / "eval" / "questions.json"
SCHEMA = "timelinexray/eval-questions/v1"
KINDS = ("answer", "abstain")
EVIDENCE_CLASSES = ("CODE", "PARAM_DEFAULT", "REPO_DOC", "OFFICIAL", "THIRD_PARTY",
                    "EMPIRICAL", "INFERENCE")
LOOKUP_TOOLS = ("get_param", "find_symbols")
ITEM_KEYS = {"id", "kind", "source", "evidence_class", "commit", "question", "expected"}
CITATION_KEYS = {"commit", "path", "start_line", "end_line", "anchor", "span_sha256", "lookup"}
ANSWER_EXPECTED_KEYS = {"answer_regexes", "citations"}
ABSTAIN_EXPECTED_KEYS = {"abstention_reason", "probes"}
PROBE_KEYS = {"query", "literal"}
SOURCE_RE = re.compile(r"^P[0-9]b?-[0-9]{3}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MIN_ITEMS, MAX_ITEMS = 25, 40
MAX_QUESTION_CHARS = 400
MAX_ANCHOR_CHARS = 80

PROTOCOL = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "txray-eval-gate", "version": "1"},
}


# -- MCP stdio client ---------------------------------------------------------------------


class GateError(RuntimeError):
    pass


class StdioClient:
    """``txray mcp serve`` in a child process, one JSON-RPC line at a time."""

    def __init__(self, store: Path | str, *, python: str = sys.executable,
                 timeout: float = 120.0, env: Mapping[str, str] | None = None) -> None:
        self.timeout = timeout
        self._id = 0
        self.proc = subprocess.Popen(
            [python, "-m", "timelinexray", "mcp", "serve", "--store", str(store)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=child_env(env),
        )
        self._lines: queue.Queue[bytes] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(b"")

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        assert self.proc.stdin is not None
        self._id += 1
        body = {**dict(params or {}), "_meta": dict(META)}
        message = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": body}
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()
        try:
            line = self._lines.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise GateError(f"no reply from the MCP server within {self.timeout} s") from exc
        if not line:
            raise GateError("the MCP server closed its output: " + self.stderr_tail())
        reply = json.loads(line)
        if "error" in reply:
            raise GateError(f"{method}: JSON-RPC error {reply['error']}")
        return reply["result"]

    def tool(self, tool_name: str, /, **arguments: Any) -> dict[str, Any]:
        """Call a tool; return its result envelope (``structuredContent``)."""
        result = self.call("tools/call", {"name": tool_name, "arguments": arguments})
        envelope = result.get("structuredContent")
        if not isinstance(envelope, dict):
            raise GateError(f"{tool_name}: no structuredContent in the result")
        return envelope

    def stderr_tail(self) -> str:
        if self.proc.stderr is None:
            return ""
        try:
            return self.proc.stderr.read().decode("utf-8", "replace")[-2000:]
        except (OSError, ValueError):
            return ""

    def close(self) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()

    def __enter__(self) -> StdioClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    src = str(REPO_ROOT / "src")
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not current else f"{src}{os.pathsep}{current}"
    env.pop("TXRAY_STORE", None)
    env.pop("TXRAY_FINDINGS", None)
    env.update(extra or {})
    return env


# -- the question file --------------------------------------------------------------------


def load_questions(path: Path | str = DEFAULT_QUESTIONS) -> dict[str, Any]:
    data = json.loads(Path(path).read_text("utf-8"))
    problems = validate(data)
    if problems:
        raise GateError("invalid question set:\n  " + "\n  ".join(problems))
    return data


def validate(data: Any) -> list[str]:
    """Structural problems of a question set (empty when it is well-formed)."""
    problems: list[str] = []
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return [f"schema must be {SCHEMA}"]
    for key in ("upstream", "status", "description", "commits", "items"):
        if key not in data:
            problems.append(f"missing top-level member {key!r}")
    commits = data.get("commits") or {}
    pinned = set(commits.get("pinned") or [])
    indexed = set(commits.get("indexed") or [])
    for commit in pinned | indexed:
        if not COMMIT_RE.match(str(commit)):
            problems.append(f"commits: {commit!r} is not a full commit id")
    if not indexed <= pinned:
        problems.append("commits.indexed must be a subset of commits.pinned")
    items = data.get("items")
    if not isinstance(items, list):
        return problems + ["items must be a list"]
    if not MIN_ITEMS <= len(items) <= MAX_ITEMS:
        problems.append(f"{len(items)} items; expected {MIN_ITEMS}-{MAX_ITEMS}")
    seen: set[str] = set()
    for item in items:
        problems.extend(_validate_item(item, seen, pinned, indexed))
    return problems


def _validate_item(item: Any, seen: set[str], pinned: set[str], indexed: set[str]) -> list[str]:
    if not isinstance(item, dict):
        return ["item is not an object"]
    label = str(item.get("id", "?"))
    problems: list[str] = []
    extra = set(item) - ITEM_KEYS
    if extra:
        problems.append(f"{label}: unexpected members {sorted(extra)}")
    for key in ITEM_KEYS:
        if key not in item:
            problems.append(f"{label}: missing {key!r}")
    if label in seen:
        problems.append(f"{label}: duplicate id")
    seen.add(label)
    if item.get("kind") not in KINDS:
        problems.append(f"{label}: kind must be one of {KINDS}")
    source = item.get("source")
    if source is not None and not SOURCE_RE.match(str(source)):
        problems.append(f"{label}: source {source!r} is not a finding id")
    if item.get("kind") == "answer" and source is None:
        problems.append(f"{label}: an answer item needs a source finding")
    if item.get("evidence_class") not in EVIDENCE_CLASSES:
        problems.append(f"{label}: evidence_class must be one of {EVIDENCE_CLASSES}")
    if item.get("commit") not in indexed:
        problems.append(f"{label}: commit must be one of commits.indexed")
    question = item.get("question")
    if not isinstance(question, str) or not question.strip():
        problems.append(f"{label}: question must be a non-empty string")
    elif len(question) > MAX_QUESTION_CHARS:
        problems.append(f"{label}: question longer than {MAX_QUESTION_CHARS} characters")
    expected = item.get("expected")
    if not isinstance(expected, dict):
        return problems + [f"{label}: expected must be an object"]
    if item.get("kind") == "answer":
        if set(expected) != ANSWER_EXPECTED_KEYS:
            problems.append(f"{label}: expected must have exactly {sorted(ANSWER_EXPECTED_KEYS)}")
        regexes = expected.get("answer_regexes")
        if not isinstance(regexes, list) or not regexes:
            problems.append(f"{label}: answer_regexes must be a non-empty list")
        else:
            for pattern in regexes:
                try:
                    re.compile(pattern, re.IGNORECASE | re.DOTALL)
                except (re.error, TypeError) as exc:
                    problems.append(f"{label}: bad regex {pattern!r}: {exc}")
        citations = expected.get("citations")
        if not isinstance(citations, list) or not citations:
            problems.append(f"{label}: citations must be a non-empty list")
        else:
            for citation in citations:
                problems.extend(_validate_citation(label, citation, pinned))
    elif item.get("kind") == "abstain":
        if set(expected) != ABSTAIN_EXPECTED_KEYS:
            problems.append(f"{label}: expected must have exactly {sorted(ABSTAIN_EXPECTED_KEYS)}")
        reason = expected.get("abstention_reason")
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"{label}: abstention_reason must be a non-empty string")
        probes = expected.get("probes")
        if not isinstance(probes, list) or not probes:
            problems.append(f"{label}: probes must be a non-empty list")
        else:
            for probe in probes:
                if not isinstance(probe, dict) or not isinstance(probe.get("query"), str):
                    problems.append(f"{label}: each probe needs a query string")
                elif set(probe) - PROBE_KEYS:
                    problems.append(f"{label}: probe has unexpected members")
    return problems


def _validate_citation(label: str, citation: Any, pinned: set[str]) -> list[str]:
    if not isinstance(citation, dict):
        return [f"{label}: citation is not an object"]
    problems = []
    extra = set(citation) - CITATION_KEYS
    if extra:
        problems.append(f"{label}: citation has unexpected members {sorted(extra)}")
    for key in ("commit", "path", "start_line", "end_line", "anchor", "span_sha256"):
        if key not in citation:
            problems.append(f"{label}: citation misses {key!r}")
    if citation.get("commit") not in pinned:
        problems.append(f"{label}: citation commit must be one of commits.pinned")
    if not SHA256_RE.match(str(citation.get("span_sha256", ""))):
        problems.append(f"{label}: span_sha256 is not a SHA-256")
    start, end = citation.get("start_line"), citation.get("end_line")
    if not (isinstance(start, int) and isinstance(end, int) and 1 <= start <= end):
        problems.append(f"{label}: bad line range {start!r}-{end!r}")
    anchor = citation.get("anchor")
    if not isinstance(anchor, str) or not anchor or len(anchor) > MAX_ANCHOR_CHARS:
        problems.append(f"{label}: anchor must be 1-{MAX_ANCHOR_CHARS} characters")
    lookup = citation.get("lookup")
    if lookup is not None:
        if not isinstance(lookup, dict) or lookup.get("tool") not in LOOKUP_TOOLS:
            problems.append(f"{label}: lookup.tool must be one of {LOOKUP_TOOLS}")
        elif not isinstance(lookup.get("name"), str):
            problems.append(f"{label}: lookup needs a name")
        elif lookup["tool"] == "get_param" and not isinstance(lookup.get("value"), str):
            problems.append(f"{label}: a get_param lookup needs the expected value")
    return problems


# -- checks -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    item: str
    kind: str      # span | hash | anchor | value | get_param | find_symbols | probe
    target: str
    ok: bool
    detail: str = ""


def overlaps(path_a: str, start_a: int, end_a: int, path_b: str, start_b: int, end_b: int) -> bool:
    return path_a == path_b and start_a <= end_b and start_b <= end_a


def same_value(found: Any, expected: str) -> bool:
    if found is None:
        return False
    text = str(found).strip()
    if text == expected:
        return True
    try:
        return float(text) == float(expected)
    except ValueError:
        return False


def check_citation(client: StdioClient, item: dict[str, Any], citation: dict[str, Any]) -> list[Check]:
    label = item["id"]
    target = f"{citation['commit'][:7]} {citation['path']}:{citation['start_line']}-{citation['end_line']}"
    checks: list[Check] = []
    envelope = client.tool("read_span", commit=citation["commit"], path=citation["path"],
                           start_line=citation["start_line"], end_line=citation["end_line"],
                           anchor=citation["anchor"])
    if envelope["outcome"] != "OK" or not envelope.get("data"):
        error = envelope.get("error") or {}
        return [Check(label, "span", target, False,
                      f"read_span {envelope['outcome']}: {error.get('message', '')}")]
    data = envelope["data"]
    checks.append(Check(label, "span", target, True))
    found_hash = data["citation"]["span_sha256"]
    checks.append(Check(label, "hash", target, found_hash == citation["span_sha256"],
                        "" if found_hash == citation["span_sha256"] else f"span_sha256 {found_hash}"))
    verdict = (data.get("anchor") or {}).get("verdict")
    checks.append(Check(label, "anchor", target, verdict == "FOUND", f"anchor {verdict}"))
    lookup = citation.get("lookup")
    text = data.get("text") or ""
    if lookup and lookup["tool"] == "get_param":
        value = lookup["value"]
        checks.append(Check(label, "value", target, value in text,
                            "" if value in text else f"{value!r} not in the span text"))
        checks.append(check_get_param(client, item, citation, lookup))
    elif lookup and lookup["tool"] == "find_symbols":
        checks.append(check_find_symbols(client, item, citation, lookup))
    return checks


def check_get_param(client: StdioClient, item: dict[str, Any], citation: dict[str, Any],
                    lookup: dict[str, Any]) -> Check:
    label = item["id"]
    target = f"{lookup['name']} in {citation['path']}"
    envelope = client.tool("get_param", commit=citation["commit"], name=lookup["name"])
    if envelope["outcome"] != "OK" or not envelope.get("data"):
        return Check(label, "get_param", target, False, f"outcome {envelope['outcome']}")
    for declaration in envelope["data"]["declarations"]:
        cited = declaration["citation"]
        if overlaps(cited["path"], cited["start_line"], cited["end_line"],
                    citation["path"], citation["start_line"], citation["end_line"]):
            if same_value(declaration.get("value"), lookup["value"]):
                return Check(label, "get_param", target, True)
            return Check(label, "get_param", target, False,
                         f"value {declaration.get('value')!r} != {lookup['value']!r}")
    return Check(label, "get_param", target, False,
                 f"{len(envelope['data']['declarations'])} declaration(s), none at the cited span")


def check_find_symbols(client: StdioClient, item: dict[str, Any], citation: dict[str, Any],
                       lookup: dict[str, Any]) -> Check:
    label = item["id"]
    target = f"{lookup['name']} in {citation['path']}"
    arguments: dict[str, Any] = {"commit": citation["commit"], "name": lookup["name"], "limit": 50}
    if lookup.get("kind"):
        arguments["kind"] = lookup["kind"]
    if lookup.get("path_prefix"):
        arguments["path_prefix"] = lookup["path_prefix"]
    envelope = client.tool("find_symbols", **arguments)
    if envelope["outcome"] not in ("OK", "INCOMPLETE") or not envelope.get("data"):
        return Check(label, "find_symbols", target, False, f"outcome {envelope['outcome']}")
    for symbol in envelope["data"]["symbols"]:
        cited = symbol["citation"]
        if overlaps(cited["path"], cited["start_line"], cited["end_line"],
                    citation["path"], citation["start_line"], citation["end_line"]):
            return Check(label, "find_symbols", target, True)
    return Check(label, "find_symbols", target, False,
                 f"{envelope['data']['total']} symbol(s), none overlapping the cited span")


def check_probe(client: StdioClient, item: dict[str, Any], probe: dict[str, Any]) -> Check:
    label = item["id"]
    target = probe["query"] + (" (literal)" if probe.get("literal") else "")
    envelope = client.tool("search_code", commit=item["commit"], query=probe["query"],
                           literal=bool(probe.get("literal", False)), limit=5)
    if envelope["outcome"] not in ("OK", "INCOMPLETE") or not envelope.get("data"):
        return Check(label, "probe", target, False, f"outcome {envelope['outcome']}")
    data = envelope["data"]
    total = data.get("total", 0)
    if total:
        first = data["hits"][0]["citation"] if data.get("hits") else {}
        return Check(label, "probe", target, False,
                     f"{total} hit(s), first {first.get('path')}:{first.get('start_line')}")
    # Zero hits over every lexically indexed line. ``coverage_complete`` is false whenever
    # the manifest has unsearchable paths (binary, generated, symlink, ...); the scope is
    # reported with the result rather than treated as a failure.
    return Check(label, "probe", target, True, str(data.get("search_scope", "")))


# -- the run --------------------------------------------------------------------------------


def run(store: Path | str, questions: Path | str = DEFAULT_QUESTIONS, *,
        python: str = sys.executable) -> dict[str, Any]:
    data = load_questions(questions)
    checks: list[Check] = []
    with StdioClient(store, python=python) as client:
        for commit in data["commits"]["pinned"]:
            envelope = client.tool("resolve_commit", commit=commit)
            checks.append(Check("commits", "pinned", commit[:7], envelope["outcome"] == "OK",
                                "" if envelope["outcome"] == "OK" else f"outcome {envelope['outcome']}"))
        for item in data["items"]:
            if item["kind"] == "answer":
                for citation in item["expected"]["citations"]:
                    checks.extend(check_citation(client, item, citation))
            else:
                for probe in item["expected"]["probes"]:
                    checks.append(check_probe(client, item, probe))
    return summarize(data, checks)


def summarize(data: dict[str, Any], checks: list[Check]) -> dict[str, Any]:
    by_kind: dict[str, dict[str, int]] = {}
    for check in checks:
        row = by_kind.setdefault(check.kind, {"passed": 0, "total": 0})
        row["total"] += 1
        row["passed"] += int(check.ok)
    items = data["items"]
    scopes = sorted({check.detail for check in checks if check.kind == "probe" and check.ok and check.detail})
    return {
        "search_scope": scopes,
        "schema": "timelinexray/eval-gate-report/v1",
        "questions": {
            "total": len(items),
            "answer": sum(item["kind"] == "answer" for item in items),
            "abstain": sum(item["kind"] == "abstain" for item in items),
            "citations": sum(len(item["expected"].get("citations", [])) for item in items),
            "probes": sum(len(item["expected"].get("probes", [])) for item in items),
            "status": data.get("status"),
        },
        "checks": {kind: dict(row) for kind, row in sorted(by_kind.items())},
        "passed": sum(check.ok for check in checks),
        "total": len(checks),
        "ok": all(check.ok for check in checks),
        "failures": [asdict(check) for check in checks if not check.ok],
    }


def format_report(report: dict[str, Any]) -> str:
    questions = report["questions"]
    lines = [
        f"question set: {questions['total']} items ({questions['answer']} answer, "
        f"{questions['abstain']} abstain), {questions['citations']} expected citations, "
        f"{questions['probes']} abstention probes; status: {questions['status']}",
    ]
    for kind, row in report["checks"].items():
        lines.append(f"  {kind:<13} {row['passed']} / {row['total']}")
    for scope in report.get("search_scope", []):
        lines.append(f"  probe scope: {scope}")
    for failure in report["failures"]:
        lines.append(f"  FAIL {failure['item']} {failure['kind']} {failure['target']}: {failure['detail']}")
    lines.append(f"semantic gate (deterministic): {'PASS' if report['ok'] else 'FAIL'} "
                 f"{report['passed']} / {report['total']} checks")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--store", required=True, help="snapshot store with the pinned commits")
    parser.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    parser.add_argument("--json", help="write the report to this path")
    parser.add_argument("--python", default=sys.executable, help="interpreter for the server")
    args = parser.parse_args(argv)
    try:
        report = run(args.store, args.questions, python=args.python)
    except GateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", "utf-8")
    print(format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
