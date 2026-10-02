"""Reference citations (``goldens/citations.json``) and their runner.

A golden is a citation only - commit, path, 1-based inclusive line range, anchor and the
SHA-256 of the exact span bytes - with the verdicts the verifier must return for it:
integrity (``INTACT``/``CHANGED``/``MISSING``) and anchor (``FOUND``/``FOUND_MULTIPLE``/
``MISSING``). History goldens also give, for later commits, the expected re-anchoring
outcome, freshness and line range (for a current span) or proposed line range (for a
changed span). Goldens hold no claims or report prose.

Goldens are immutable: changing an expected result requires an independently reviewed
source change or a documented correction of the golden itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import InvalidInput, NotFound
from ..verify import FRESHNESS, OUTCOMES, VERDICTS, Citation, Verifier

GOLDENS_SCHEMA = "timelinexray/goldens/v1"
ENTRY_KEYS = frozenset({"id", "source", "commit", "path", "start_line", "end_line", "anchor",
                        "span_sha256", "expected", "history"})
ANCHOR_VERDICTS = ("FOUND", "FOUND_MULTIPLE", "MISSING")


@dataclass(frozen=True, slots=True)
class Golden:
    id: str
    source: str
    citation: Citation
    expected_verdict: str
    expected_anchor: str
    history: tuple[dict[str, Any], ...]


def load(path: Path) -> list[Golden]:
    data = json.loads(Path(path).read_text("utf-8"))
    if not isinstance(data, dict) or data.get("schema") != GOLDENS_SCHEMA:
        raise InvalidInput(f"{path} is not a {GOLDENS_SCHEMA} file")
    goldens = []
    seen = set()
    for entry in data.get("goldens", []):
        if not isinstance(entry, dict) or not set(entry) <= ENTRY_KEYS:
            raise InvalidInput(f"golden entry has unexpected keys: {entry!r:.120}")
        if entry["id"] in seen:
            raise InvalidInput(f"duplicate golden id {entry['id']}")
        seen.add(entry["id"])
        citation = Citation.from_dict({**entry, "kind": "code"})
        if len(citation.commit) != 40 or citation.span_sha256 is None:
            raise InvalidInput(f"golden {entry['id']} needs a full commit id and a span_sha256")
        expected = entry["expected"]
        if expected.get("verdict") not in VERDICTS or expected.get("anchor") not in ANCHOR_VERDICTS:
            raise InvalidInput(f"golden {entry['id']} has an invalid expected verdict")
        history = tuple(entry.get("history", ()))
        for item in history:
            if item.get("freshness") not in FRESHNESS or item.get("outcome") not in OUTCOMES \
                    or len(item.get("target", "")) != 40:
                raise InvalidInput(f"golden {entry['id']} has an invalid history entry")
        goldens.append(Golden(entry["id"], entry.get("source", ""), citation,
                              expected["verdict"], expected["anchor"], history))
    return goldens


def commits(goldens: list[Golden]) -> list[str]:
    """Every commit the goldens need pinned (cited and history targets)."""
    needed = {golden.citation.commit for golden in goldens}
    needed |= {item["target"] for golden in goldens for item in golden.history}
    return sorted(needed)


def run(verifier: Verifier, goldens: list[Golden]) -> list[dict[str, Any]]:
    """Run every golden; each result lists what was expected, what was observed and passed."""
    results = []
    for golden in goldens:
        check = verifier.check(golden.citation)
        observed = {"verdict": check.verdict, "anchor": check.anchor_verdict}
        expected = {"verdict": golden.expected_verdict, "anchor": golden.expected_anchor}
        history = []
        for item in golden.history:
            try:
                relocation = verifier.relocate(golden.citation, item["target"])
            except NotFound as exc:
                history.append({"target": item["target"], "expected": item,
                                "observed": {"error": str(exc)}, "passed": False})
                continue
            seen: dict[str, Any] = {"freshness": relocation.freshness,
                                    "outcome": relocation.outcome}
            if relocation.current is not None:
                seen["lines"] = [relocation.current.start_line, relocation.current.end_line]
            if relocation.proposed is not None:
                seen["proposed_lines"] = [relocation.proposed["start_line"],
                                          relocation.proposed["end_line"]]
            wanted = {key: item[key] for key in ("freshness", "outcome", "lines", "proposed_lines")
                      if key in item}
            passed = all(seen.get(key) == value for key, value in wanted.items())
            history.append({"target": item["target"], "expected": wanted, "observed": seen,
                            "passed": passed})
        results.append({
            "id": golden.id,
            "source": golden.source,
            "citation": golden.citation.label(),
            "expected": expected,
            "observed": observed,
            "passed": observed == expected and all(item["passed"] for item in history),
            "history": history,
        })
    return results
