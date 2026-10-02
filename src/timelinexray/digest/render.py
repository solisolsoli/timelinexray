"""Markdown rendering of a digest document (see :mod:`timelinexray.digest.build`).

The Markdown is a pure function of the JSON document: same document, same bytes. Values
from upstream code are shown in code spans, cut to :data:`VALUE_MAX` characters (the JSON
document and the cited span hold the full text), and always labelled public defaults.
"""

from __future__ import annotations

from typing import Any

from ..diff.rules import CLASS_TITLES, CLASSES, PARAMETER_DEFAULT, REGISTRATION, UNKNOWN
from ..textsafe import visible

VALUE_MAX = 120
SHORT = 12


def _short(commit: str | None) -> str:
    return commit[:SHORT] if commit else "-"


def _cell(text: str) -> str:
    return visible(" ".join(str(text).split())).replace("|", "\\|")


def _code(text: Any, limit: int | None = VALUE_MAX) -> str:
    value = visible(" ".join(str(text).split()))
    if limit is not None and len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    fence = "``" if "`" in value else "`"
    pad = " " if fence == "``" else ""
    return f"{fence}{pad}{value}{pad}{fence}".replace("|", "\\|")


def cite(citation: dict[str, Any], *, with_path: bool = False) -> str:
    """One citation as Markdown: commit, lines and the span SHA-256 (or why there is none)."""
    commit = f"`{_short(citation['commit'])}`"
    role = citation["role"]
    path = f" `{_cell(citation['path'])}`" if with_path and citation.get("path") else ""
    if role in ("span", "context"):
        a, b = citation["start_line"], citation["end_line"]
        lines = f"L{a}" if a == b else f"L{a}-L{b}"
        text = f"{commit}{path} {lines} sha256 `{citation['span_sha256']}`"
        if role == "context":
            text += " (context: " + _cell(citation.get("note") or "") + ")"
        return text
    if role == "absent":
        return f"{commit} absent"
    if role == "empty":
        return f"{commit}{path} empty file"
    return f"{commit}{path} " + _cell(citation.get("note") or role)


def _value_cell(row: dict[str, Any]) -> str:
    old, new = row.get("old_value"), row.get("new_value")
    if row["change"] == "value-changed":
        return f"public default {_code(old)} → {_code(new)}"
    if row["change"] == "added":
        return f"public default {_code(new)} (declared)"
    return f"public default was {_code(old)} (removed)"


def _change_word(kind: str) -> str:
    return {"value-changed": "value changed", "added": "declared", "removed": "removed",
            "list-added": "list added", "list-removed": "list removed",
            "entries-changed": "entries changed"}.get(kind, kind)


def render_markdown(doc: dict[str, Any]) -> str:
    out: list[str] = []
    rng = doc["range"]
    old, new = rng["old"], rng["new"]
    out.append(f"# Change digest {_short(old['commit'])}..{_short(new['commit'])}")
    out.append("")
    for statement in doc["statements"]:
        out.append(f"- {statement}")
    out.append("")

    # -- range ---------------------------------------------------------------------------
    out += ["## Range", "",
            "| | Commit | Committed (UTC) | Manifest SHA-256 |", "|---|---|---|---|",
            f"| Old | `{old['commit']}` | {old['committer_time']} | `{old['manifest_sha256']}` |",
            f"| New | `{new['commit']}` | {new['committer_time']} | `{new['manifest_sha256']}` |",
            ""]
    relationship = {
        "ancestor": "the old commit is an ancestor of the new commit",
        "same": "the two commits are the same",
        "reversed": "the old commit is a descendant of the new commit (reversed range)",
        "diverged": "the old commit is not an ancestor of the new commit",
        "unrelated-mirrors": "the commits come from different upstreams",
    }[rng["relationship"]]
    steps = len(rng["first_parent_chain"]) - 1
    out.append(f"- Relationship: {relationship}.")
    if rng["relationship"] == "ancestor":
        merged = len(rng["reached_through_merges"])
        out.append(f"- History: {rng['commits_in_range']} commit{'s' if rng['commits_in_range'] != 1 else ''} in the range; {steps} "
                   f"first-parent step{'s' if steps != 1 else ''} diffed one by one"
                   + (f"; {merged} commit{'s' if merged != 1 else ''} reached only through "
                      "merges (their changes appear in the merge step)" if merged else "") + ".")
    else:
        out.append("- History: not available for this range; only the two trees are compared.")
    upstream = doc["upstream"]
    if upstream["repository"]:
        out.append(f"- Upstream: {upstream['repository']} ({upstream['url']}).")
    else:
        out.append(f"- Upstream: {upstream['note']}.")
    tool = doc["tool"]
    out.append(f"- Tool: {tool['name']} {tool['version']}, classifier v{tool['classifier_version']}, "
               f"symbol backends {', '.join(tool['symbol_backends']) or 'none'}; digest input "
               f"SHA-256 `{doc['inputs_sha256']}`.")
    out.append("")

    # -- events --------------------------------------------------------------------------
    out += ["## Exceptional events", ""]
    if doc["events"]:
        out += ["| Event | Severity | Commit | Message |", "|---|---|---|---|"]
        for event in doc["events"]:
            out.append(f"| `{event['kind']}` | {event['severity']} | "
                       f"{('`' + _short(event['commit']) + '`') if event['commit'] else '-'} | "
                       f"{_cell(event['message'])} |")
    else:
        out.append("No exceptional upstream event was detected for this range.")
    out.append("")

    # -- summary -------------------------------------------------------------------------
    summary = doc["summary"]
    net = summary["net"]
    out += ["## Summary by class", "",
            f"| Class | Net items | Items across {summary['steps']} step{'s' if summary['steps'] != 1 else ''} | What the rule saw |",
            "|---|---:|---:|---|"]
    evidence = {row["class"]: row["evidence"] for row in doc["classes"]}
    for name in CLASSES:
        out.append(f"| {CLASS_TITLES[name]} (`{name}`) | {net['items_by_class'][name]} | "
                   f"{summary['items_by_class_across_steps'][name]} | {_cell(evidence[name])} |")
    files = net["files_by_status"]
    out += ["", f"Net files: {files['added']} added, {files['removed']} removed, "
            f"{files['modified']} modified, {files['renamed']} renamed; "
            f"+{net['lines_added']} -{net['lines_removed']} lines.", ""]

    # -- parameters ----------------------------------------------------------------------
    params = doc["parameters"]
    out += ["## Parameter default changes", "",
            "Values are public defaults at the cited commit, not production values.", ""]
    if params["net"]:
        out += ["| Parameter | Path | Declaration | Change | Value | Old citation | New citation |",
                "|---|---|---|---|---|---|---|"]
        for row in params["net"]:
            out.append(f"| `{_cell(row['name'])}` | `{_cell(row['path'])}` | {row['declaration']} | "
                       f"{_change_word(row['change'])} | {_value_cell(row)} | {cite(row['old'])} | "
                       f"{cite(row['new'])} |")
    else:
        out.append("No parameter default changed between the two commits.")
    out.append("")
    if summary["steps"] > 1:
        out += ["### Parameter values at intermediate commits", ""]
        if params["history"]:
            out += ["| Parameter | Path | Public default by commit |", "|---|---|---|"]
            for row in params["history"]:
                out.append(f"| `{_cell(row['name'])}` | `{_cell(row['path'])}` | "
                           + " → ".join(_point(point) for point in row["points"]) + " |")
        else:
            out.append("No parameter value changed at an intermediate commit.")
        out.append("")
    out += ["### Intermediate reversions", ""]
    if params["reversions"]:
        out += ["A value that changed and later returned to an earlier value within the range.", "",
                "| Parameter | Path | Public default by commit |", "|---|---|---|"]
        for row in params["reversions"]:
            out.append(f"| `{_cell(row['name'])}` | `{_cell(row['path'])}` | "
                       + " → ".join(_point(point) for point in row["points"]) + " |")
    else:
        out.append("No parameter value returned to an earlier value within the range.")
    out.append("")

    # -- registrations -------------------------------------------------------------------
    regs = doc["registrations"]
    out += ["## Registration changes", ""]
    if regs["net"]:
        out += ["| List | Path | Change | Added | Removed | Reordered | Old citation | New citation |",
                "|---|---|---|---|---|---|---|---|"]
        for row in regs["net"]:
            out.append(_registration_line(row))
    else:
        out.append("No component registration list changed between the two commits.")
    out.append("")
    if summary["steps"] > 1 and regs["history"]:
        out += ["### Registration changes at intermediate commits", "",
                "| Commit | List | Path | Change | Added | Removed | Reordered | Old citation | New citation |",
                "|---|---|---|---|---|---|---|---|---|"]
        for row in regs["history"]:
            out.append(f"| `{_short(row['new_commit'])}` " + _registration_line(row))
        out.append("")
    if regs["reversions"]:
        out += ["### Registration reversions", "",
                "| List | Path | Entry | Events |", "|---|---|---|---|"]
        for row in regs["reversions"]:
            events = ", ".join(f"{e['event']} at `{_short(e['commit'])}`" for e in row["events"])
            out.append(f"| {_cell(row['list'])} | `{_cell(row['path'])}` | {_code(row['entry'])} | {events} |")
        out.append("")

    # -- affected findings -----------------------------------------------------------------
    findings = doc["affected_findings"]
    out += ["## Affected findings", ""]
    if not findings["available"]:
        out.append(f"{findings['note']} ({findings['regions']} changed regions were offered to "
                   f"the `{findings['provider']}` provider.)")
    elif findings["findings"]:
        out += ["Evidence status and freshness are separate fields: the status is what the "
                "ledger records (with its basis), the freshness is the finding's latest "
                "recorded check (at the new commit when one exists). Being listed here "
                "changes neither; re-anchor and review the finding.", "",
                "| Finding | Via | Citation | Status | Freshness | Reason | Items |",
                "|---|---|---|---|---|---|---|"]
        for row in findings["findings"]:
            c = row["citation"]
            basis = f" ({row['status_basis']})" if row.get("status_basis") else ""
            against = row.get("freshness_checked_against")
            checked = (f" at `{_short(against)}`" if against and against != "cited"
                       else " at the cited commits" if against == "cited" else "")
            out.append(f"| `{_cell(row['finding_id'])}` | {_cell(row.get('via', 'citation'))} | "
                       f"`{_short(c['commit'])}` `{_cell(c['path'])}` "
                       f"L{c['start_line']}-L{c['end_line']} | {_cell(str(row['status']))}{basis} | "
                       f"{_cell(str(row['freshness']))}{checked} | {_cell(row['reason'])} | "
                       f"{', '.join(row['item_ids'])} |")
    else:
        out.append(f"The `{findings['provider']}` provider reported no finding whose citation "
                   f"touches the {findings['regions']} changed regions.")
    candidates = findings.get("relocation_candidates") or []
    if findings["available"] and candidates:
        out += ["", "Relocation candidates (not affected): these findings cite a file that "
                "moved in this range without a byte changing. `txray findings reanchor` "
                "relocates the citation (outcome `relocated`, freshness `CURRENT`); there is "
                "nothing to review.", ""]
        for row in candidates:
            c = row["citation"]
            out.append(f"- `{_cell(row['finding_id'])}` {_cell(row.get('via', 'citation'))} "
                       f"`{_short(c['commit'])}` `{_cell(c['path'])}` "
                       f"L{c['start_line']}-L{c['end_line']}: {_cell(row['reason'])} "
                       f"({', '.join(row['item_ids'])})")
    coverage = findings.get("coverage")
    if findings["available"] and coverage:
        skipped = coverage.get("skipped", 0)
        out += ["", f"Checked {coverage['active_findings']} active findings with "
                f"{coverage['spans']} cited or dependency spans: {coverage['placed']} placed on "
                f"the old or new commit, {coverage['unplaced']} not placeable on either (their "
                "bytes are not found exactly once there), so they were not checked against "
                f"this range, and {skipped} not placed because their path exists at both "
                "commits and no changed region touches it (they cannot be affected)."]
        for row in coverage.get("unplaced_spans", []):
            out.append(f"- `{_cell(row['finding_id'])}` {_cell(row['via'])} "
                       f"`{_cell(row['citation'])}`: old {row['old']}, new {row['new']}")
    out.append("")

    # -- other classes -------------------------------------------------------------------
    items = doc["items"]
    out += ["## Other classified changes", ""]
    others = [name for name in CLASSES if name not in (PARAMETER_DEFAULT, REGISTRATION, UNKNOWN)]
    for name in others:
        chosen = [item for item in items if item["class"] == name]
        out.append(f"### {CLASS_TITLES[name]} (`{name}`): {len(chosen)}")
        out.append("")
        if not chosen:
            out.append("None.")
        for item in chosen:
            out.append(_item_line(item))
        out.append("")

    out += ["## Unresolved and unknown changes", "",
            "These changes were not classified by any rule and need review; nothing is "
            "concluded about them.", ""]
    unknown = [item for item in items if item["class"] == UNKNOWN]
    if not unknown:
        out.append("None.")
    for item in unknown:
        out.append(_item_line(item))
    out.append("")

    # -- steps ---------------------------------------------------------------------------
    out += ["## Intermediate history", ""]
    if summary["steps"] > 1:
        out += ["| # | Commit | Committed (UTC) | Files | Lines | Items by class |",
                "|---:|---|---|---:|---|---|"]
        for number, step in enumerate(doc["steps"], start=1):
            counts = ", ".join(f"{k} {v}" for k, v in step["items_by_class"].items()) or "none"
            out.append(f"| {number} | `{_short(step['new'])}` | {step['committer_time']} | "
                       f"{step['files']} | +{step['lines_added']} -{step['lines_removed']} | {counts} |")
    elif summary["steps"] == 1:
        out.append("The range is a single step; the net diff above is that step.")
    else:
        out.append("No intermediate history is available for this range.")
    out.append("")

    health = doc["health"]
    coverage = ", ".join(f"{k} {v}" for k, v in health["parse_coverage"].items()) or "none"
    out += ["## Health and limitations", "",
            f"- History complete: {'yes' if health['history_complete'] else 'no'}.",
            f"- Rename detection: {', '.join(health['rename_detection'])}.",
            f"- Symbol extraction per changed file side (all diffs): {coverage}.",
            "- Hunks come from a line matcher on blob bytes and can differ from git's; "
            "applying them to the old lines reproduces the new lines exactly.",
            "- Parameter, registration and logic classes are heuristics over names, masked "
            "source and Milestone 2 symbols; a class never implies that a change is active in "
            "production or affects any particular account.",
            ""]
    return "\n".join(out)


def _point(point: dict[str, Any]) -> str:
    c = point["citation"]
    where = f"`{_short(point['commit'])}`"
    if c["role"] == "span":
        a, b = c["start_line"], c["end_line"]
        where += f" L{a}" if a == b else f" L{a}-L{b}"
    if point["value"] is None:
        return f"absent ({where})"
    return f"{_code(point['value'], 60)} ({where})"


def _registration_line(row: dict[str, Any]) -> str:
    def names(values: list[str]) -> str:
        return ", ".join(_code(v, 80) for v in values) or "-"

    return (f"| {_cell(row['list'])} | `{_cell(row['path'])}` | {_change_word(row['change'])} | "
            f"{names(row['added'])} | {names(row['removed'])} | {names(row['reordered'])} | "
            f"{cite(row['old'])} | {cite(row['new'])} |")


def _item_line(item: dict[str, Any]) -> str:
    path = item["new_path"] or item["old_path"]
    summary = _cell(item["summary"])
    if len(summary) > 240:
        summary = summary[:239].rstrip() + "…"
    old_path = item["old_path"] if item["old_path"] != path else None
    return (f"- `{_cell(path)}`: {summary}. Old: {cite(item['old'], with_path=bool(old_path))}. "
            f"New: {cite(item['new'])}.")
