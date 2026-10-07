"""Markdown rendering of a digest document (see :mod:`timelinexray.digest.build`).

Two files, both pure functions of the JSON document (same document, same bytes):

* :func:`render_markdown`, the **main digest**: a summary first (what changed, what to
  check next), then the statements, the range, events, the summary by class, parameter
  defaults, registrations, affected findings and the ``scoring-logic``
  items with their citations; every other class (visibility-rule, model-config, test-only,
  build-dependency, data-type, observability, access-modifier, cosmetic, docs, license,
  generated, unknown) is counted by area. Every
  section has a row budget; what does not fit is said, with its count, and is in the
  appendix. The main file is bounded on any range.
* :func:`render_appendix`: every classified item that the main file does not list, grouped
  by class and file, with both citations, plus the table rows the main file had to cut.

Values from upstream code are shown in code spans, cut to :data:`VALUE_MAX` characters (the
JSON document and the cited span hold the full text), and always labelled public defaults.
"""

from __future__ import annotations

from typing import Any

from ..diff.rules import (
    CLASS_TITLES,
    CLASSES,
    PARAMETER_DEFAULT,
    REGISTRATION,
    SCORING_LOGIC,
    UNKNOWN,
)
from ..textsafe import visible
from .build import MAIN_CLASSES, area_of

VALUE_MAX = 120
SHORT = 12

#: Row budgets of the main file (the appendix and the JSON document hold everything).
MAIN_PARAMETER_ROWS = 60
MAIN_TIMELINE_ROWS = 30
MAIN_REGISTRATION_ROWS = 30
MAIN_FINDING_ROWS = 40
MAIN_STEP_ROWS = 40
#: ``scoring-logic`` items listed one by one in the main file; above
#: the budget a class is listed by file (with counts, no citations) and itemised in the
#: appendix.
MAIN_LOGIC_ITEMS = 80
MAIN_FILE_ROWS = 25
#: Areas named per class in the long-tail table, and rows of the unknown-areas table.
AREAS_PER_CLASS = 6
MAIN_UNKNOWN_AREAS = 20

#: The command a reader runs to see review work after re-anchoring.
REVIEW_COMMAND = "txray findings stale"

UNKNOWN_REASONS = {
    "no-rule": "a parsed language, and no rule matched",
    "not-parsed": "a language without symbol extraction (C, C++, CUDA, shell, ...): only path "
    "and token rules could apply",
    "not-text": "not compared as text (binary, oversize, symlink or submodule)",
    "mode-only": "only the file mode changed",
}


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


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count:,} {word if count == 1 else (plural or word + 's')}"


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


def cite_compact(citation: dict[str, Any], implied: str | None = None) -> str:
    """A citation in the main digest: commit, lines and span SHA-256, without labels (the
    main file says once how to read it); ``(context)`` marks the line an insertion or
    deletion applies at. The commit is left out only when it is ``implied``, the commit the
    enclosing section names for that side."""
    role = citation["role"]
    parts = [] if implied is not None and citation["commit"] == implied \
        else [f"`{_short(citation['commit'])}`"]
    if role in ("span", "context"):
        a, b = citation["start_line"], citation["end_line"]
        parts += [f"L{a}" if a == b else f"L{a}-L{b}", f"`{citation['span_sha256']}`"]
        if role == "context":
            parts.append("(context)")
    elif role == "absent":
        parts.append("absent")
    elif role == "empty":
        parts.append("empty file")
    else:
        parts.append(_cell(citation.get("note") or role))
    return " ".join(parts)


CITATION_KEY = ("A citation reads `commit` L<first>-L<last> `span SHA-256` (old side, then new "
                "side); where a section names the old and new commit once, its citations leave "
                "the commit out. `(context)` marks the line where an insertion or deletion "
                "applies on a side where nothing changed. Reproduce one with "
                "`txray show COMMIT PATH --lines A-B`.")


def _changed_parts(old: str, new: str, limit: int = 60, context: int = 16) -> tuple[str, str]:
    """Two long values reduced to the part that differs (with ``context`` characters around
    it and ``…`` where text was left out), so that two values with a long common beginning
    do not render as the same cut prefix. Short values are returned whole."""
    old = " ".join(str(old).split())
    new = " ".join(str(new).split())
    if len(old) <= limit and len(new) <= limit:
        return old, new
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < min(len(old), len(new)) - prefix
           and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]):
        suffix += 1
    start = max(prefix - context, 0)

    def part(text: str) -> str:
        end = min(len(text) - suffix + context, len(text))
        return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")

    return part(old), part(new)


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


def digest_names(doc: dict[str, Any]) -> tuple[str, str, str]:
    """File names of the main digest, its appendix and the JSON document."""
    rng = doc["range"]
    stem = f"digest-{rng['old']['commit'][:12]}-{rng['new']['commit'][:12]}"
    return f"{stem}.md", f"{stem}-appendix.md", f"{stem}.json"


def _class_rows(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The document's per-class overview (recomputed for documents built without one)."""
    overview = doc.get("overview")
    if overview is None:
        from .build import overview as compute

        overview = compute(doc["items"])
    return {row["class"]: row for row in overview["classes"]}


def _items_of(doc: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [item for item in doc["items"] if item["class"] == name]


def _path(item: dict[str, Any]) -> str:
    return item["new_path"] or item["old_path"]


def _by_file(items: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:  # items are in class, path, line order already
        groups.setdefault(_path(item), []).append(item)
    return list(groups.items())


def _lines_changed(items: list[dict[str, Any]]) -> tuple[int, int]:
    return (sum(h["new_count"] for item in items for h in item["hunks"]),
            sum(h["old_count"] for item in items for h in item["hunks"]))


def _cut(rows: list[Any], limit: int) -> tuple[list[Any], int]:
    return rows[:limit], max(len(rows) - limit, 0)


# -- the main digest --------------------------------------------------------------------------


def render_markdown(doc: dict[str, Any]) -> str:
    """The main digest: summary first, bounded size, long tail counted (see the module)."""
    out: list[str] = []
    rng = doc["range"]
    old, new = rng["old"], rng["new"]
    main_name, appendix_name, json_name = digest_names(doc)
    rows = _class_rows(doc)
    cuts: list[str] = []  # what the row budgets moved to the appendix, said at the top
    sections: list[list[str]] = []

    # the sections are rendered first: their row budgets decide what the summary says was cut
    sections.append(_parameters_section(doc, cuts, appendix_name))
    sections.append(_registrations_section(doc, cuts, appendix_name))
    sections.append(_logic_section(doc, SCORING_LOGIC, rows[SCORING_LOGIC], cuts, appendix_name))
    sections.append(_findings_section(doc, cuts, appendix_name))
    sections.append(_summary_section(doc, rows, appendix_name))
    sections.append(_long_tail_section(doc, rows, appendix_name))
    history = _history_section(doc, cuts, appendix_name)

    out.append(f"# Change digest {_short(old['commit'])}..{_short(new['commit'])}")
    out.append("")
    out += _at_a_glance(doc, rows, cuts, main_name, appendix_name, json_name)
    out += _next_steps(doc, rows)
    out += ["## Statements", ""]
    for statement in doc["statements"]:
        out.append(f"- {statement}")
    out += ["", CITATION_KEY, ""]
    for section in sections:
        out += section
    out += _range_section(doc)
    out += _events_section(doc)
    out += history
    out += _health_section(doc)
    return "\n".join(out)


def _at_a_glance(doc: dict[str, Any], rows: dict[str, dict[str, Any]], cuts: list[str],
                 main_name: str, appendix_name: str, json_name: str) -> list[str]:
    rng = doc["range"]
    net = doc["summary"]["net"]
    files = net["files_by_status"]
    steps = doc["summary"]["steps"]
    out = ["## At a glance", ""]
    if rng["relationship"] == "ancestor":
        history = (f"{_plural(rng['commits_in_range'], 'commit')} "
                   f"({_plural(steps, 'first-parent step')})")
    else:
        history = f"two trees compared ({rng['relationship']}; no intermediate history)"
    out.append(f"- Range: `{_short(rng['old']['commit'])}` → `{_short(rng['new']['commit'])}`, "
               f"{history}.")
    out.append(f"- Changed: {_plural(net['files'], 'file')} ({files['added']:,} added, "
               f"{files['removed']:,} removed, {files['modified']:,} modified, "
               f"{files['renamed']:,} renamed), +{net['lines_added']:,} -{net['lines_removed']:,} "
               f"lines, {_plural(net['items'], 'classified item')}.")
    params, regs = doc["parameters"]["net"], doc["registrations"]["net"]
    out.append(f"- Parameter defaults: {_plural(len(params), 'change')} (public defaults at the "
               f"cited commits, not production values). Registrations: "
               f"{_plural(len(regs), 'list change')}.")
    for name in (SCORING_LOGIC,):
        row = rows[name]
        decided = row.get("decided_by", {})
        how = ", ".join(f"{rule} {count:,}" for rule, count in decided.items())
        out.append(f"- {CLASS_TITLES[name]} (`{name}`): {_plural(row['items'], 'item')} in "
                   f"{_plural(row['files'], 'file')}" + (f", decided by a name rule on the "
                                                          f"{how}" if how else "") + ".")
    unknown = rows[UNKNOWN]
    reasons = ", ".join(f"{reason} {count:,}" for reason, count in unknown.get("reasons", {}).items())
    share = (100 * unknown["items"] / net["items"]) if net["items"] else 0.0
    out.append(f"- Unknown (not classified by any rule, needs review): "
               f"{_plural(unknown['items'], 'item')} ({share:.0f} % of items)"
               + (f": {reasons}" if reasons else "") + ".")
    events = doc["events"]
    out.append("- Exceptional events: " + (", ".join(f"`{e['kind']}` ({e['severity']})"
                                                     for e in events) if events else "none detected") + ".")
    findings = doc["affected_findings"]
    if not findings["available"]:
        out.append("- Affected findings: not computed (no findings ledger could be read); "
                   "this is not evidence that no finding is affected.")
    else:
        count = len({row["finding_id"] for row in findings["findings"]})
        moved = len({row["finding_id"] for row in findings.get("relocation_candidates") or []})
        out.append(f"- Affected findings: {_plural(count, 'finding')} touch a changed region"
                   + (f"; {_plural(moved, 'finding')} only moved with identical bytes" if moved else "")
                   + ".")
    tail = [name for name in CLASSES if name not in MAIN_CLASSES and rows[name]["items"]]
    tail_text = ", ".join(f"{name} {rows[name]['items']:,}" for name in tail) or "none"
    out.append(f"- In this file: parameter defaults, registrations, affected findings and the "
               f"`scoring-logic` items, with citations. Counted here by area "
               f"and listed item by item in `{appendix_name}`: {tail_text}. `{json_name}` holds "
               "every item and citation.")
    if cuts:
        out.append("- Cut from this file for size (all in the appendix): " + "; ".join(cuts) + ".")
    out.append("")
    return out


def _next_steps(doc: dict[str, Any], rows: dict[str, dict[str, Any]]) -> list[str]:
    new = _short(doc["range"]["new"]["commit"])
    out = ["## What to check next", ""]
    steps = []
    if doc["parameters"]["net"]:
        steps.append("Parameter defaults (table below): `txray param-history NAME` shows the "
                     "public default at every pinned commit; `txray show COMMIT PATH --lines A-B` "
                     "reproduces a cited span and its SHA-256.")
    if rows[SCORING_LOGIC]["items"]:
        steps.append(f"Scoring items: read the new side with `txray show {new} PATH "
                     "--lines A-B`; an item is a mechanical classification by a name rule, not a "
                     "finding: review it before citing it.")
    if doc["registrations"]["net"]:
        steps.append("Registration changes: an added or removed entry is not evidence that a "
                     "component is active for any request.")
    findings = doc["affected_findings"]
    if findings["available"]:
        steps.append(f"Findings: `txray findings reanchor --latest`, then `{REVIEW_COMMAND}`; "
                     "listed findings are not current until re-anchored and reviewed.")
    else:
        steps.append(f"Findings: with a ledger, `txray findings reanchor --latest` and then "
                     f"`{REVIEW_COMMAND}` (or `txray update --reanchor`).")
    if rows[UNKNOWN]["items"]:
        steps.append("Unknown items: listed with their reason in the appendix; nothing is "
                     "concluded about them.")
    out += [f"{number}. {text}" for number, text in enumerate(steps, start=1)]
    out.append("")
    return out


def _range_section(doc: dict[str, Any]) -> list[str]:
    rng = doc["range"]
    old, new = rng["old"], rng["new"]
    out = ["## Range", "",
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
    return out


def _events_section(doc: dict[str, Any]) -> list[str]:
    out = ["## Exceptional events", ""]
    if doc["events"]:
        out += ["| Event | Severity | Commit | Message |", "|---|---|---|---|"]
        for event in doc["events"]:
            out.append(f"| `{event['kind']}` | {event['severity']} | "
                       f"{('`' + _short(event['commit']) + '`') if event['commit'] else '-'} | "
                       f"{_cell(event['message'])} |")
    else:
        out.append("No exceptional upstream event was detected for this range.")
    out.append("")
    return out


def _summary_section(doc: dict[str, Any], rows: dict[str, dict[str, Any]],
                     appendix_name: str) -> list[str]:
    summary = doc["summary"]
    net = summary["net"]
    steps = summary["steps"]
    out = ["## Summary by class", "",
           "What each rule saw is stated with its section here or in the appendix, and in "
           "the JSON document (`classes`).", "",
           f"| Class | Net items | Files | Items across {steps} step{'s' if steps != 1 else ''} | "
           "Listed in | Must not be read as |",
           "|---|---:|---:|---:|---|---|"]
    meta = {row["class"]: row for row in doc["classes"]}
    for name in CLASSES:
        row = rows[name]
        where = "this file" if name in MAIN_CLASSES else "appendix"
        out.append(f"| {CLASS_TITLES[name]} (`{name}`) | {net['items_by_class'][name]:,} | "
                   f"{row['files']:,} | {summary['items_by_class_across_steps'][name]:,} | {where} | "
                   f"{_cell(meta[name].get('must_not_be_read_as', ''))} |")
    files = net["files_by_status"]
    out += ["", f"Net files: {files['added']} added, {files['removed']} removed, "
            f"{files['modified']} modified, {files['renamed']} renamed; "
            f"+{net['lines_added']} -{net['lines_removed']} lines. Appendix: `{appendix_name}`.", ""]
    return out


def _parameters_section(doc: dict[str, Any], cuts: list[str], appendix_name: str) -> list[str]:
    params = doc["parameters"]
    steps = doc["summary"]["steps"]
    out = ["## Parameter default changes", "",
           "Values are public defaults at the cited commit, not production values.", ""]
    if params["net"]:
        shown, rest = _cut(params["net"], MAIN_PARAMETER_ROWS)
        out += _parameter_table(shown, compact=True, commits=(doc["range"]["old"]["commit"],
                                                              doc["range"]["new"]["commit"]))
        if rest:
            cuts.append(f"{rest} of {len(params['net'])} parameter default rows")
            out += ["", f"{rest} more parameter default changes are in `{appendix_name}` "
                    "(Parameter default changes, continued)."]
    else:
        out.append("No parameter default changed between the two commits.")
    out.append("")
    if steps > 1:
        out += ["### Parameter values at intermediate commits", ""]
        if params["history"]:
            shown, rest = _cut(params["history"], MAIN_TIMELINE_ROWS)
            out += _timeline_table(shown)
            if rest:
                cuts.append(f"{rest} of {len(params['history'])} parameter timelines")
                out += ["", f"{rest} more timelines are in `{appendix_name}`."]
        else:
            out.append("No parameter value changed at an intermediate commit.")
        out.append("")
    out += ["### Intermediate reversions", ""]
    if params["reversions"]:
        shown, rest = _cut(params["reversions"], MAIN_TIMELINE_ROWS)
        out += ["A value that changed and later returned to an earlier value within the range.", ""]
        out += _timeline_table(shown)
        if rest:
            cuts.append(f"{rest} of {len(params['reversions'])} parameter reversions")
            out += ["", f"{rest} more reversions are in `{appendix_name}`."]
    else:
        out.append("No parameter value returned to an earlier value within the range.")
    out.append("")
    return out


def _parameter_table(rows: list[dict[str, Any]], *, compact: bool,
                     commits: tuple[str, str] | None = None) -> list[str]:
    """``commits`` (old, new) are the commits the compact table names in its header."""
    if compact:
        old_commit, new_commit = commits or (None, None)
        out = [f"| Parameter | Path | Change | Old citation"
               f"{f' (`{_short(old_commit)}`)' if old_commit else ''} | New citation"
               f"{f' (`{_short(new_commit)}`)' if new_commit else ''} |",
               "|---|---|---|---|---|"]
        for row in rows:
            old, new = row.get("old_value"), row.get("new_value")
            if row["change"] == "value-changed":
                old, new = _changed_parts(old, new, limit=VALUE_MAX // 2)
            value = {"value-changed": f"public default {_code(old)} → {_code(new)} "
                                      f"({row['declaration']})",
                     "added": f"public default {_code(new)} (declared, {row['declaration']})"}.get(
                row["change"], f"public default was {_code(old)} (removed, {row['declaration']})")
            out.append(f"| `{_cell(row['name'])}` | `{_cell(row['path'])}` | {value} | "
                       f"{cite_compact(row['old'], old_commit)} | "
                       f"{cite_compact(row['new'], new_commit)} |")
        return out
    out = ["| Parameter | Path | Declaration | Change | Value | Old citation | New citation |",
           "|---|---|---|---|---|---|---|"]
    for row in rows:
        out.append(f"| `{_cell(row['name'])}` | `{_cell(row['path'])}` | {row['declaration']} | "
                   f"{_change_word(row['change'])} | {_value_cell(row)} | {cite(row['old'])} | "
                   f"{cite(row['new'])} |")
    return out


def _timeline_table(rows: list[dict[str, Any]]) -> list[str]:
    out = ["| Parameter | Path | Public default by commit |", "|---|---|---|"]
    for row in rows:
        points = row["points"]
        shown = []
        for index, point in enumerate(points):
            value = point["value"]
            if value is not None:  # a long value shows the part that differs from a neighbour
                neighbour = next((p["value"] for p in (points[index - 1:index] if index else points[1:2])
                                  if p["value"] is not None), None)
                if neighbour is not None:
                    value = _changed_parts(value, neighbour)[0]
            shown.append(_point(point, value))
        out.append(f"| `{_cell(row['name'])}` | `{_cell(row['path'])}` | " + " → ".join(shown) + " |")
    return out


def _registrations_section(doc: dict[str, Any], cuts: list[str], appendix_name: str) -> list[str]:
    regs = doc["registrations"]
    steps = doc["summary"]["steps"]
    out = ["## Registration changes", ""]
    if regs["net"]:
        shown, rest = _cut(regs["net"], MAIN_REGISTRATION_ROWS)
        out += _registration_table(shown, with_commit=False, compact=True)
        if rest:
            cuts.append(f"{rest} of {len(regs['net'])} registration rows")
            out += ["", f"{rest} more registration changes are in `{appendix_name}`."]
    else:
        out.append("No component registration list changed between the two commits.")
    out.append("")
    if steps > 1 and regs["history"]:
        shown, rest = _cut(regs["history"], MAIN_REGISTRATION_ROWS)
        out += ["### Registration changes at intermediate commits", "",
                f"The commit at which each list changed; both citations of every intermediate "
                f"change are in `{appendix_name}`.", "",
                "| Commit | List | Path | Change | Added | Removed | Reordered |",
                "|---|---|---|---|---|---|---|"]
        for row in shown:
            out.append(f"| `{_short(row['new_commit'])}` " + _registration_line(row, None))
        if rest:
            cuts.append(f"{rest} of {len(regs['history'])} intermediate registration rows")
            out += ["", f"{rest} more intermediate registration changes are in `{appendix_name}`."]
        out.append("")
    if regs["reversions"]:
        shown, rest = _cut(regs["reversions"], MAIN_REGISTRATION_ROWS)
        out += ["### Registration reversions", ""] + _registration_reversions(shown)
        if rest:
            cuts.append(f"{rest} of {len(regs['reversions'])} registration reversions")
            out += ["", f"{rest} more registration reversions are in `{appendix_name}`."]
        out.append("")
    return out


def _registration_reversions(rows: list[dict[str, Any]]) -> list[str]:
    out = ["| List | Path | Entry | Events |", "|---|---|---|---|"]
    for row in rows:
        events = ", ".join(f"{e['event']} at `{_short(e['commit'])}`" for e in row["events"])
        out.append(f"| {_cell(row['list'])} | `{_cell(row['path'])}` | {_code(row['entry'])} | {events} |")
    return out


def _registration_table(rows: list[dict[str, Any]], *, with_commit: bool,
                        compact: bool) -> list[str]:
    if with_commit:
        out = ["| Commit | List | Path | Change | Added | Removed | Reordered | Old citation | New citation |",
               "|---|---|---|---|---|---|---|---|---|"]
        return out + [f"| `{_short(row['new_commit'])}` " + _registration_line(row, compact)
                      for row in rows]
    out = ["| List | Path | Change | Added | Removed | Reordered | Old citation | New citation |",
           "|---|---|---|---|---|---|---|---|"]
    return out + [_registration_line(row, compact) for row in rows]


def _findings_section(doc: dict[str, Any], cuts: list[str], appendix_name: str) -> list[str]:
    findings = doc["affected_findings"]
    out = ["## Affected findings", ""]
    if not findings["available"]:
        out.append(f"{findings['note']} ({findings['regions']} changed regions were offered to "
                   f"the `{findings['provider']}` provider.)")
    elif findings["findings"]:
        shown, rest = _cut(findings["findings"], MAIN_FINDING_ROWS)
        out += ["Evidence status and freshness are separate fields: the status is what the "
                "ledger records (with its basis), the freshness is the finding's latest "
                "recorded check (at the new commit when one exists). Being listed here "
                "changes neither; re-anchor and review the finding.", ""]
        out += _findings_table(shown)
        if rest:
            cuts.append(f"{rest} of {len(findings['findings'])} affected-finding rows")
            out += ["", f"{rest} more affected-finding rows are in `{appendix_name}`."]
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
    return out


def _findings_table(rows: list[dict[str, Any]]) -> list[str]:
    out = ["| Finding | Via | Citation | Status | Freshness | Reason | Items |",
           "|---|---|---|---|---|---|---|"]
    for row in rows:
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
    return out


def _logic_section(doc: dict[str, Any], name: str, row: dict[str, Any], cuts: list[str],
                   appendix_name: str) -> list[str]:
    items = _items_of(doc, name)
    meta = {entry["class"]: entry for entry in doc["classes"]}[name]
    out = [f"## {CLASS_TITLES[name]} (`{name}`): {_plural(len(items), 'item')} in "
           f"{_plural(row['files'], 'file')}", ""]
    if not items:
        out += ["None.", ""]
        return out
    decided = ", ".join(f"{rule} {count:,}" for rule, count in row.get("decided_by", {}).items())
    old, new = doc["range"]["old"]["commit"], doc["range"]["new"]["commit"]
    out += [f"Rule: {_cell(meta['evidence'])}" + (f"; decided on the {decided}" if decided else "")
            + f". Must not be read as {_cell(meta.get('must_not_be_read_as', ''))}. No item has "
            "been reviewed.", ""]
    groups = _by_file(items)
    if len(items) <= MAIN_LOGIC_ITEMS:
        out += [f"Per item: hunks, lines, the first enclosing symbol, then the old citation (at "
                f"`{_short(old)}`) and the new citation (at `{_short(new)}`).", ""]
        for path, chosen in groups:
            added, removed = _lines_changed(chosen)
            out.append(f"- `{_cell(path)}` ({_plural(len(chosen), 'item')}, +{added} -{removed}):")
            for item in chosen:
                out.append("  - " + _item_compact(item, old, new))
        out.append("")
        return out
    cuts.append(f"the citations of {len(items):,} `{name}` items (counted by file here)")
    ranked = sorted(groups, key=lambda group: (-len(group[1]), group[0].encode("utf-8", "surrogateescape")))
    shown, rest = _cut(ranked, MAIN_FILE_ROWS)
    out += [f"{len(items):,} items are more than this file lists one by one: here they are "
            f"counted by file, most items first; every item with its citations is in "
            f"`{appendix_name}`.", "",
            "| File | Items | Lines | Enclosing symbols (first) |", "|---|---:|---|---|"]
    for path, chosen in shown:
        added, removed = _lines_changed(chosen)
        symbols = list(dict.fromkeys(s for item in chosen
                                     for s in item["symbols"]["new"] + item["symbols"]["old"]))
        names = ", ".join(_code(s, 50) for s in symbols[:2]) + (", …" if len(symbols) > 2 else "")
        out.append(f"| `{_cell(path)}` | {len(chosen)} | +{added} -{removed} | {names or '-'} |")
    if rest:
        others = ranked[MAIN_FILE_ROWS:]
        out += ["", f"{rest} more files ({sum(len(group[1]) for group in others):,} items) are "
                f"listed in `{appendix_name}`."]
    out.append("")
    return out


def _long_tail_section(doc: dict[str, Any], rows: dict[str, dict[str, Any]],
                       appendix_name: str) -> list[str]:
    out = ["## Other classes, counted by area", "",
           f"Every item of these classes is listed with both citations in `{appendix_name}`. "
           "An area is the first two directories of a path.", "",
           "| Class | Items | Files | Lines | Areas (items) |", "|---|---:|---:|---|---|"]
    for name in CLASSES:
        if name in MAIN_CLASSES or name == UNKNOWN:
            continue
        row = rows[name]
        if not row["items"]:
            continue
        out.append(f"| {CLASS_TITLES[name]} (`{name}`) | {row['items']:,} | {row['files']:,} | "
                   f"+{row['lines_added']:,} -{row['lines_removed']:,} | {_areas(row)} |")
    out.append("")
    unknown = rows[UNKNOWN]
    out += ["## Unresolved and unknown changes", "",
            "These changes were not classified by any rule and need review; nothing is "
            "concluded about them. Each is listed, with its reason and citations, in "
            f"`{appendix_name}`.", ""]
    if not unknown["items"]:
        out += ["None.", ""]
        return out
    out.append(f"{_plural(unknown['items'], 'item')} in {_plural(unknown['files'], 'file')}, "
               f"+{unknown['lines_added']:,} -{unknown['lines_removed']:,} lines.")
    reasons = unknown.get("reasons", {})
    if reasons:
        out.append("")
        for reason, count in reasons.items():
            out.append(f"- `{reason}` {count:,}: {UNKNOWN_REASONS.get(reason, reason)}.")
    shown, rest = _cut(unknown["areas"], MAIN_UNKNOWN_AREAS)
    out += ["", "| Area | Items | Files |", "|---|---:|---:|"]
    for area in shown:
        out.append(f"| {_code(area['area'], 80)} | {area['items']:,} | {area['files']:,} |")
    if rest:
        others = unknown["areas"][MAIN_UNKNOWN_AREAS:]
        out.append(f"| {rest} more areas | {sum(a['items'] for a in others):,} | "
                   f"{sum(a['files'] for a in others):,} |")
    out.append("")
    return out


def _areas(row: dict[str, Any]) -> str:
    shown, rest = _cut(row["areas"], AREAS_PER_CLASS)
    text = ", ".join(f"{_code(area['area'], 80)} {area['items']:,}" for area in shown)
    if rest:
        text += f", {rest} more areas {sum(a['items'] for a in row['areas'][AREAS_PER_CLASS:]):,}"
    return text


def _steps_table(steps: list[dict[str, Any]], *, first: int = 1) -> list[str]:
    out = ["| # | Commit | Committed (UTC) | Files | Lines | Items by class |",
           "|---:|---|---|---:|---|---|"]
    for number, step in enumerate(steps, start=first):
        counts = ", ".join(f"{k} {v}" for k, v in step["items_by_class"].items()) or "none"
        out.append(f"| {number} | `{_short(step['new'])}` | {step['committer_time']} | "
                   f"{step['files']} | +{step['lines_added']} -{step['lines_removed']} | {counts} |")
    return out


def _history_section(doc: dict[str, Any], cuts: list[str], appendix_name: str) -> list[str]:
    summary = doc["summary"]
    out = ["## Intermediate history", ""]
    if summary["steps"] > 1:
        shown, rest = _cut(doc["steps"], MAIN_STEP_ROWS)
        out += _steps_table(shown)
        if rest:
            cuts.append(f"{rest} of {len(doc['steps'])} intermediate steps")
            out += ["", f"{rest} more steps are in `{appendix_name}`."]
    elif summary["steps"] == 1:
        out.append("The range is a single step; the net diff above is that step.")
    else:
        out.append("No intermediate history is available for this range.")
    out.append("")
    return out


def _health_section(doc: dict[str, Any]) -> list[str]:
    health = doc["health"]
    coverage = ", ".join(f"{k} {v}" for k, v in health["parse_coverage"].items()) or "none"
    return ["## Health and limitations", "",
            f"- History complete: {'yes' if health['history_complete'] else 'no'}.",
            f"- Rename detection: {', '.join(health['rename_detection'])}.",
            f"- Symbol extraction per changed file side (all diffs): {coverage}.",
            "- Hunks come from a line matcher on blob bytes and can differ from git's; "
            "applying them to the old lines reproduces the new lines exactly.",
            "- Parameter, registration and logic classes are heuristics over names, masked "
            "source and Milestone 2 symbols; a class never implies that a change is active in "
            "production or affects any particular account.",
            ""]


# -- the appendix -----------------------------------------------------------------------------


def render_appendix(doc: dict[str, Any]) -> str:
    """Every item the main digest does not list one by one, with both citations, and every
    table row the main file cut (see the module documentation)."""
    rng = doc["range"]
    main_name, appendix_name, json_name = digest_names(doc)
    rows = _class_rows(doc)
    out = [f"# Change digest appendix {_short(rng['old']['commit'])}..{_short(rng['new']['commit'])}",
           "",
           f"Companion of `{main_name}` (summary, parameter defaults, registrations, findings) "
           f"and `{json_name}` (the complete document). This file lists every classified item "
           "that the main file counts but does not cite, and any table rows the main file had "
           "to cut for size. Same commits, same citations, same digest input SHA-256 "
           f"`{doc['inputs_sha256']}`.", ""]
    for statement in doc["statements"]:
        out.append(f"- {statement}")
    out.append("")

    params, regs, findings = doc["parameters"], doc["registrations"], doc["affected_findings"]
    continued: list[tuple[str, list[str]]] = []
    if len(params["net"]) > MAIN_PARAMETER_ROWS:
        continued.append(("Parameter default changes, continued",
                          ["Values are public defaults at the cited commit, not production values.",
                           ""] + _parameter_table(params["net"][MAIN_PARAMETER_ROWS:], compact=False)))
    if doc["summary"]["steps"] > 1 and len(params["history"]) > MAIN_TIMELINE_ROWS:
        continued.append(("Parameter values at intermediate commits, continued",
                          _timeline_table(params["history"][MAIN_TIMELINE_ROWS:])))
    if len(params["reversions"]) > MAIN_TIMELINE_ROWS:
        continued.append(("Intermediate reversions, continued",
                          _timeline_table(params["reversions"][MAIN_TIMELINE_ROWS:])))
    if len(regs["net"]) > MAIN_REGISTRATION_ROWS:
        continued.append(("Registration changes, continued",
                          _registration_table(regs["net"][MAIN_REGISTRATION_ROWS:], with_commit=False,
                                              compact=False)))
    if doc["summary"]["steps"] > 1 and regs["history"]:  # the main file shows no citations here
        continued.append(("Registration changes at intermediate commits, with citations",
                          _registration_table(regs["history"], with_commit=True, compact=False)))
    if len(regs["reversions"]) > MAIN_REGISTRATION_ROWS:
        continued.append(("Registration reversions, continued",
                          _registration_reversions(regs["reversions"][MAIN_REGISTRATION_ROWS:])))
    if len(doc["steps"]) > MAIN_STEP_ROWS:
        continued.append(("Intermediate history, continued",
                          _steps_table(doc["steps"][MAIN_STEP_ROWS:], first=MAIN_STEP_ROWS + 1)))
    if findings["available"] and len(findings["findings"]) > MAIN_FINDING_ROWS:
        continued.append(("Affected findings, continued",
                          _findings_table(findings["findings"][MAIN_FINDING_ROWS:])))
    for title, lines in continued:
        out += [f"## {title}", ""] + lines + [""]

    listed = [name for name in CLASSES if name not in (PARAMETER_DEFAULT, REGISTRATION)
              and (name not in MAIN_CLASSES or len(_items_of(doc, name)) > MAIN_LOGIC_ITEMS)]
    out += ["## Contents", ""]
    for name in listed:
        row = rows[name]
        out.append(f"- {CLASS_TITLES[name]} (`{name}`): {_plural(row['items'], 'item')} in "
                   f"{_plural(row['files'], 'file')}")
    out.append("")
    for name in listed:
        out += _class_listing(doc, name, rows[name])
    return "\n".join(out)


def _class_listing(doc: dict[str, Any], name: str, row: dict[str, Any]) -> list[str]:
    items = _items_of(doc, name)
    meta = {entry["class"]: entry for entry in doc["classes"]}[name]
    out = [f"## {CLASS_TITLES[name]} (`{name}`): {_plural(len(items), 'item')} in "
           f"{_plural(row['files'], 'file')}", "",
           f"Rule: {_cell(meta['evidence'])}. Must not be read as "
           f"{_cell(meta.get('must_not_be_read_as', ''))}.", ""]
    if not items:
        return out + ["None.", ""]
    current_area: str | None = None
    groups = sorted(_by_file(items), key=lambda group: (area_of(group[0]).encode("utf-8", "surrogateescape"),
                                                        group[0].encode("utf-8", "surrogateescape")))
    for path, chosen in groups:
        area = area_of(path)
        if area != current_area:
            out += [f"### `{_cell(area)}`", ""]
            current_area = area
        added, removed = _lines_changed(chosen)
        out.append(f"- `{_cell(path)}` ({_plural(len(chosen), 'item')}, +{added} -{removed}):")
        for item in chosen:
            out.append("  - " + _item_body(item, include_path=False))
    out.append("")
    return out


# -- shared item lines ------------------------------------------------------------------------


def _point(point: dict[str, Any], value: Any = None) -> str:
    c = point["citation"]
    where = f"`{_short(point['commit'])}`"
    if c["role"] == "span":
        a, b = c["start_line"], c["end_line"]
        where += f" L{a}" if a == b else f" L{a}-L{b}"
    if point["value"] is None:
        return f"absent ({where})"
    return f"{_code(point['value'] if value is None else value, 60)} ({where})"


def _registration_line(row: dict[str, Any], compact: bool | None) -> str:
    """A registration row; ``compact`` ``None`` leaves the citation columns out."""
    def names(values: list[str]) -> str:
        return ", ".join(_code(v, 80) for v in values) or "-"

    text = (f"| {_cell(row['list'])} | `{_cell(row['path'])}` | {_change_word(row['change'])} | "
            f"{names(row['added'])} | {names(row['removed'])} | {names(row['reordered'])} |")
    if compact is None:
        return text
    show = cite_compact if compact else cite
    return text + f" {show(row['old'])} | {show(row['new'])} |"


def _item_compact(item: dict[str, Any], old: str, new: str) -> str:
    """One logic item in the main digest: hunks, lines, enclosing symbols, the deciding rule
    when it is not the file's own path, and both citations (:func:`cite_compact`)."""
    hunks = item["hunks"]
    added, removed = _lines_changed([item])
    symbols = list(dict.fromkeys(item["symbols"]["new"] + item["symbols"]["old"]))
    text = f"{_plural(len(hunks), 'hunk')}, -{removed} +{added}"
    if symbols:
        text += " in " + _code(symbols[0], 60)
        if len(symbols) > 1:
            text += f" +{len(symbols) - 1}"
    if item["status"] in ("added", "removed"):
        text += f" (file {item['status']})"
    elif item["status"] == "renamed":
        text += f" (renamed from `{_cell(item['old_path'])}`)"
    matched = (item["detail"].get("matched_by") or [{}])[0]
    if matched.get("rule") == "symbol":  # a path match is the file's own path, shown above
        text += (" [symbol rule]" if symbols and matched["name"] == symbols[0]
                 else f" [symbol rule: {_code(matched['name'], 60)}]")
    elif matched.get("rule") == "path" and matched["name"] != _path(item):
        text += f" [path rule: `{_cell(matched['name'])}`]"
    return f"{text}: {cite_compact(item['old'], old)}; {cite_compact(item['new'], new)}"


def _item_body(item: dict[str, Any], *, include_path: bool) -> str:
    """One item: what changed (summary, cut to 240 characters), its unknown reason or the
    name rule that decided it, and both citations (commit, lines, span SHA-256)."""
    path = _path(item)
    summary = _cell(item["summary"])
    if len(summary) > 240:
        summary = summary[:239].rstrip() + "…"
    notes = []
    reason = item["detail"].get("unknown_reason")
    if reason:
        notes.append(f"reason `{reason}`")
    matched = item["detail"].get("matched_by")
    if matched:
        notes.append("by " + ", ".join(f"{m['rule']} {_code(m['name'], 80)}" for m in matched[:2]))
    note = f" [{'; '.join(notes)}]" if notes else ""
    old_path = item["old_path"] if item["old_path"] != path else None
    head = f"`{_cell(path)}`: " if include_path else ""
    return (f"{head}{summary}{note}. Old: {cite(item['old'], with_path=bool(old_path))}. "
            f"New: {cite(item['new'])}.")
