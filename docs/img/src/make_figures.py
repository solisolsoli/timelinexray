#!/usr/bin/env python3
"""Generate the documentation figures in ``docs/img/`` (light and dark variants).

Not part of the ``timelinexray`` package and never imported by it. Standard library only,
deterministic output (same bytes on every run). Every figure is plain SVG: shapes and real
text, no script, no external reference, no embedded image, no logo.

    python docs/img/src/make_figures.py          # write docs/img/*.svg
    python docs/img/src/make_figures.py --check  # exit 1 when a committed file differs

The values shown in the figures (commits, lines, hashes, public defaults) were read with
``txray show`` and ``txray findings`` at the commits named in them; they are public defaults
at those commits, not production values.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent.parent

SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"

# Two palettes with one meaning per colour: neutral (source), accent (exact evidence),
# ok (current / reviewed), warn (stale), bad (cannot be verified).
PALETTES = {
    "light": {
        "fg": "#1f2328", "muted": "#59636e", "line": "#818b98", "box": "#f6f8fa",
        "border": "#d1d9e0", "accent": "#0969da", "accent_bg": "#ddf4ff",
        "ok": "#1a7f37", "ok_bg": "#dafbe1", "warn": "#9a6700", "warn_bg": "#fff8c5",
        "bad": "#cf222e", "bad_bg": "#ffebe9", "code_bg": "#eff2f5",
    },
    "dark": {
        "fg": "#f0f6fc", "muted": "#9198a1", "line": "#7d8590", "box": "#151b23",
        "border": "#3d444d", "accent": "#4493f8", "accent_bg": "#121d2f",
        "ok": "#3fb950", "ok_bg": "#12261e", "warn": "#d29922", "warn_bg": "#272115",
        "bad": "#f85149", "bad_bg": "#25171c", "code_bg": "#1f2630",
    },
}

#: Approximate advance width per character, as a fraction of the font size; used only to
#: refuse a layout whose text would overflow its box (a generous upper bound).
SANS_ADVANCE = 0.52
MONO_ADVANCE = 0.61


class Figure:
    def __init__(self, width: int, height: int, title: str, desc: str, theme: str) -> None:
        self.width, self.height, self.theme = width, height, theme
        self.p = PALETTES[theme]
        self.parts: list[str] = []
        self.title, self.desc = title, desc

    # -- primitives ------------------------------------------------------------------
    def rect(self, x: float, y: float, w: float, h: float, kind: str = "box",
             rx: float = 10, dashed: bool = False) -> None:
        fill, stroke = self._kind(kind)
        dash = ' stroke-dasharray="5 4"' if dashed else ""
        self.parts.append(
            f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{rx:g}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dash}/>')

    def text(self, x: float, y: float, s: str, *, size: float = 15, weight: int = 400,
             color: str = "fg", anchor: str = "start", mono: bool = False,
             max_width: float | None = None) -> None:
        if max_width is not None:
            advance = (MONO_ADVANCE if mono else SANS_ADVANCE) * size
            if weight >= 600:
                advance *= 1.06
            needed = advance * len(s)
            if needed > max_width:
                raise ValueError(f"text overflows ({needed:.0f} > {max_width:.0f}): {s!r}")
        family = MONO if mono else SANS
        fill = self.p.get(color, color)
        self.parts.append(
            f'<text x="{x:g}" y="{y:g}" font-family="{family}" font-size="{size:g}" '
            f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{escape(s)}</text>')

    def arrow(self, points: list[tuple[float, float]], *, color: str = "line",
              dashed: bool = False) -> None:
        stroke = self.p[color]
        d = " ".join(f"{x:g},{y:g}" for x, y in points)
        dash = ' stroke-dasharray="6 5"' if dashed else ""
        self.parts.append(
            f'<polyline points="{d}" fill="none" stroke="{stroke}" stroke-width="1.8" '
            f'stroke-linejoin="round"{dash} marker-end="url(#head-{color})"/>')

    def card(self, x: float, y: float, w: float, h: float, kind: str, title: str,
             lines: list[tuple[str, str]], *, step: str | None = None,
             title_size: float = 16, line_size: float = 14, gap: float = 21) -> None:
        """A box with a title and body lines; each line is (style, text), where style is
        'text', 'mono', 'muted' or 'strong'."""
        self.rect(x, y, w, h, kind)
        _, stroke = self._kind(kind)
        tx = x + 16
        if step is not None:
            dot = self.p["muted"] if kind == "box" else stroke
            self.parts.append(f'<circle cx="{x + 27:g}" cy="{y + 27:g}" r="12" fill="{dot}"/>')
            bg = "#ffffff" if self.theme == "light" else "#0d1117"
            self.text(x + 27, y + 32, step, size=13, weight=700, color=bg, anchor="middle")
            tx = x + 48
        self.text(tx, y + 33, title, size=title_size, weight=600,
                  max_width=w - (tx - x) - 12)
        cy = y + 33 + gap + 6
        for style, s in lines:
            mono = style == "mono"
            color = "muted" if style == "muted" else "fg"
            weight = 600 if style == "strong" else 400
            size = line_size - (1 if style == "muted" else 0)
            self.text(x + 16, cy, s, size=size, mono=mono, color=color, weight=weight,
                      max_width=w - 28)
            cy += gap
        if cy - gap + 14 > y + h:
            raise ValueError(f"card {title!r} is too short for its lines")

    def _kind(self, kind: str) -> tuple[str, str]:
        p = self.p
        return {
            "box": (p["box"], p["border"]),
            "accent": (p["accent_bg"], p["accent"]),
            "ok": (p["ok_bg"], p["ok"]),
            "warn": (p["warn_bg"], p["warn"]),
            "bad": (p["bad_bg"], p["bad"]),
            "plain": ("none", p["border"]),
        }[kind]

    # -- output ----------------------------------------------------------------------
    def render(self) -> str:
        markers = []
        for color in ("line", "accent", "ok", "warn", "bad", "muted"):
            markers.append(
                f'<marker id="head-{color}" viewBox="0 0 10 10" refX="9" refY="5" '
                f'markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
                f'<path d="M0,0 L10,5 L0,10 z" fill="{self.p[color]}"/></marker>')
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" '
            f'height="{self.height}" viewBox="0 0 {self.width} {self.height}" '
            f'role="img" aria-labelledby="t d">\n'
            f'<title id="t">{escape(self.title)}</title>\n'
            f'<desc id="d">{escape(self.desc)}</desc>\n'
            f'<defs>{"".join(markers)}</defs>\n')
        return head + "\n".join(self.parts) + "\n</svg>\n"


# ---------------------------------------------------------------------------------------
# Figures. Values come from real runs (see the module docstring).
# ---------------------------------------------------------------------------------------

OVERVIEW_DESC = ("Four steps: the published repository is pinned at commit 92f0918; an exact "
                 "span (home-mixer/params/param.rs line 347, with its SHA-256 and anchor) is "
                 "read from the git blob; a finding cites that span and is reviewed by a "
                 "different reviewer; at the newer commit ed78e06 the cited bytes are gone, "
                 "so the finding becomes STALE and goes back to review.")
OVERVIEW_TITLE = "From published code to a finding that notices change"
OVERVIEW_CARDS = [
    ("box", "Published code", [
        ("text", "xai-org/x-algorithm"),
        ("text", "pinned at one commit:"),
        ("mono", "92f0918f6cb5"),
        ("muted", "a commit id, never a branch"),
        ("muted", "fetched, never executed"),
    ]),
    ("accent", "Exact span", [
        ("mono", "home-mixer/params/"),
        ("mono", "param.rs  line 347"),
        ("mono", "sha256 4b5c535d..."),
        ("text", "anchor ClickWeight"),
        ("strong", "FOUND in the span"),
        ("muted", "read from the git blob"),
    ]),
    ("ok", "Finding", [
        ("text", "\"At 92f0918 the public"),
        ("text", "default of ClickWeight"),
        ("text", "is 0.3.\""),
        ("strong", "SUPPORTED, reviewed"),
        ("muted", "by a different reviewer;"),
        ("muted", "self-approval is refused"),
    ]),
    ("warn", "Newer commit", [
        ("mono", "ed78e0645cfa"),
        ("text", "the cited bytes are gone"),
        ("text", "from param.rs; line diff"),
        ("text", "proposes line 492 (0.274)"),
        ("strong", "STALE: re-review"),
        ("muted", "never shown as current"),
    ]),
]
OVERVIEW_LABELS = ["pin", "cite", "re-check"]
OVERVIEW_FOOTER = "Every number is a public default at a named commit, not a production value."


def overview(theme: str) -> Figure:
    f = Figure(1044, 330, OVERVIEW_TITLE, OVERVIEW_DESC, theme)
    w, h, y, gap = 216, 190, 10, 56
    xs = [6 + i * (w + gap) for i in range(4)]
    for i, (kind, title, lines) in enumerate(OVERVIEW_CARDS):
        f.card(xs[i], y, w, h, kind, title, lines, step=str(i + 1))
    for i in range(3):
        x1, x2 = xs[i] + w + 2, xs[i + 1] - 4
        f.arrow([(x1, y + h / 2), (x2, y + h / 2)])
        f.text((x1 + x2) / 2, y + h / 2 - 9, OVERVIEW_LABELS[i], size=12, color="muted",
               anchor="middle")
    # the loop back from 4 to 3
    by = y + h + 36
    f.arrow([(xs[3] + w / 2, y + h + 2), (xs[3] + w / 2, by), (xs[2] + w / 2, by),
             (xs[2] + w / 2, y + h + 6)], color="warn", dashed=True)
    f.text((xs[2] + xs[3] + w) / 2, by + 24,
           "re-review: supersede with new spans, review as is, or retract",
           size=14, color="muted", anchor="middle")
    f.text(6, by + 70, OVERVIEW_FOOTER, size=14, color="muted")
    return f


def overview_narrow(theme: str) -> Figure:
    """The same four steps in one column, for narrow screens."""
    w, h, gap = 300, 190, 46
    f = Figure(380, 10 + 4 * h + 3 * gap + 70, OVERVIEW_TITLE, OVERVIEW_DESC, theme)
    ys = [10 + i * (h + gap) for i in range(4)]
    for i, (kind, title, lines) in enumerate(OVERVIEW_CARDS):
        f.card(10, ys[i], w, h, kind, title, lines, step=str(i + 1))
    for i in range(3):
        y1, y2 = ys[i] + h + 2, ys[i + 1] - 4
        f.arrow([(80, y1), (80, y2)])
        f.text(92, (y1 + y2) / 2 + 5, OVERVIEW_LABELS[i], size=13, color="muted")
    # the loop back from 4 to 3, on the right
    f.arrow([(312, ys[3] + h / 2), (350, ys[3] + h / 2), (350, ys[2] + h / 2),
             (314, ys[2] + h / 2)], color="warn", dashed=True)
    f.text(10, ys[3] + h + 30, "re-review: supersede, review as is, or retract",
           size=13, color="muted", max_width=360)
    f.text(10, ys[3] + h + 52, "Numbers are public defaults at a named commit.",
           size=13, color="muted", max_width=360)
    return f


def pipeline(theme: str) -> Figure:
    f = Figure(1000, 560, "How TimelineXray is built",
               "Four tiers. Source: the public upstream repository is fetched only from an "
               "allowlisted URL into a local snapshot store of pinned commits. Evidence: a "
               "manifest of every path, an exact span reader and a code index with "
               "parameters. Knowledge: a hash-chained findings ledger and classified diffs "
               "and digests. Access: the txray command line and a read-only MCP server.",
               theme)
    tiers = [("SOURCE", 20), ("EVIDENCE", 160), ("KNOWLEDGE", 300), ("ACCESS", 440)]
    for name, ty in tiers:
        f.text(10, ty + 18, name, size=12, weight=700, color="muted")
    bx = 110
    # source tier
    f.card(bx, 20, 300, 100, "box", "xai-org/x-algorithm", [
        ("muted", "public GitHub repository"),
        ("muted", "read as data, never run"),
    ], title_size=15, line_size=14, gap=20)
    f.card(bx + 420, 20, 450, 100, "box", "Snapshot store (outside your project)", [
        ("muted", "bare mirror + one pin per commit id"),
        ("muted", "a pin survives force-pushes upstream"),
    ], title_size=15, line_size=14, gap=20)
    f.arrow([(bx + 302, 70), (bx + 416, 70)], color="accent")
    f.text(bx + 359, 60, "git fetch", size=12, color="muted", anchor="middle")
    f.text(bx + 359, 88, "allowlisted", size=12, color="muted", anchor="middle")
    # evidence tier
    ew, eg = 280, 15
    ex = [bx + i * (ew + eg) for i in range(3)]
    f.card(ex[0], 160, ew, 100, "accent", "Manifest", [
        ("muted", "every path, classified"),
        ("muted", "and hashed; licence files"),
    ], title_size=15, gap=20)
    f.card(ex[1], 160, ew, 100, "accent", "Exact spans", [
        ("muted", "line ranges cut from blob"),
        ("muted", "bytes; SHA-256 + anchor"),
    ], title_size=15, gap=20)
    f.card(ex[2], 160, ew, 100, "accent", "Code index + parameters", [
        ("muted", "SQLite FTS5, symbols, calls;"),
        ("muted", "public defaults and history"),
    ], title_size=15, gap=20)
    for x in ex:
        f.arrow([(bx + 420 + 225, 122), (bx + 420 + 225, 140), (x + ew / 2, 140),
                 (x + ew / 2, 156)])
    # knowledge tier
    kw = 428
    f.card(bx, 300, kw, 100, "ok", "Findings ledger", [
        ("muted", "claims with cited spans; hash-chained events;"),
        ("muted", "re-anchored on new commits: CURRENT / STALE"),
    ], title_size=15, gap=20)
    f.card(bx + kw + 26, 300, kw, 100, "warn", "Diffs and digests", [
        ("muted", "classified changes between two pins, cited"),
        ("muted", "on both sides; never from commit messages"),
    ], title_size=15, gap=20)
    f.arrow([(ex[1] + ew / 2, 262), (ex[1] + ew / 2, 280), (bx + kw / 2, 280), (bx + kw / 2, 296)])
    f.arrow([(ex[1] + ew / 2, 280), (bx + kw + 26 + kw / 2, 280), (bx + kw + 26 + kw / 2, 296)])
    # access tier
    f.card(bx, 440, kw, 100, "box", "txray command line", [
        ("muted", "pin, show, search, param, findings, diff,"),
        ("muted", "digest, update; JSON output with --json"),
    ], title_size=15, gap=20)
    f.card(bx + kw + 26, 440, kw, 100, "box", "Read-only MCP server (stdio)", [
        ("muted", "13 tools for AI agents; never fetches,"),
        ("muted", "never writes; every result schema-checked"),
    ], title_size=15, gap=20)
    f.arrow([(bx + kw / 2, 402), (bx + kw / 2, 436)])
    f.arrow([(bx + kw + 26 + kw / 2, 402), (bx + kw + 26 + kw / 2, 436)])
    return f


def pipeline_narrow(theme: str) -> Figure:
    """The four tiers in one column, for narrow screens."""
    tiers = [
        ("SOURCE", [("box", "xai-org/x-algorithm", "public repository; read as data, never run"),
                    ("box", "Snapshot store", "bare mirror + one pin per commit id")]),
        ("EVIDENCE", [("accent", "Manifest", "every path classified and hashed"),
                      ("accent", "Exact spans", "blob bytes, SHA-256 and anchor"),
                      ("accent", "Code index + parameters", "search, symbols, public defaults")]),
        ("KNOWLEDGE", [("ok", "Findings ledger", "hash-chained claims; CURRENT / STALE"),
                       ("warn", "Diffs and digests", "classified changes, cited both sides")]),
        ("ACCESS", [("box", "txray command line", "every operation; JSON with --json"),
                    ("box", "Read-only MCP server", "13 tools for AI agents; never writes")]),
    ]
    h, gap, head, between = 76, 10, 26, 40
    height = 10 + sum(head + len(cards) * (h + gap) for _, cards in tiers) + between * 3
    f = Figure(380, height, "How TimelineXray is built",
               "Four tiers in one column. Source: the public upstream repository and a local "
               "snapshot store of pinned commits. Evidence: manifest, exact spans, code index "
               "with parameters. Knowledge: findings ledger, diffs and digests. Access: the "
               "txray command line and a read-only MCP server.", theme)
    y = 10
    for t, (name, cards) in enumerate(tiers):
        f.text(10, y + 16, name, size=12, weight=700, color="muted")
        y += head
        for kind, title, line in cards:
            f.card(20, y, 340, h, kind, title, [("muted", line)], title_size=15, gap=20)
            y += h + gap
        if t < len(tiers) - 1:
            f.arrow([(190, y - gap + 2), (190, y + between - 6)])
            y += between
    return f


def lifecycle(theme: str) -> Figure:
    f = Figure(1000, 520, "The life cycle of a finding",
               "A finding is added through a write gate that reads every cited span, starts "
               "as a draft, is verified at its cited commit and reviewed by a different "
               "reviewer. When a newer commit is pinned it is re-anchored there: CURRENT if "
               "the cited bytes are still found once, STALE if they changed, UNVERIFIABLE if "
               "they are ambiguous or missing. A stale finding goes to the re-review "
               "worklist: supersede it with new spans, review it as is at the target, or "
               "retract it.", theme)
    w, h = 220, 122
    xs = [10, 270, 530, 770]
    f.card(xs[0], 20, w, h, "box", "add", [
        ("muted", "the write gate reads every"),
        ("muted", "cited span; anchor must"),
        ("muted", "be inside the cited lines"),
    ], gap=20)
    f.card(xs[1], 20, w, h, "box", "draft", [
        ("mono", "NOT_CHECKED"),
        ("muted", "status is only proposed"),
    ], gap=20)
    f.card(xs[2], 20, w, h, "accent", "verify", [
        ("mono", "CURRENT at cited"),
        ("muted", "span bytes and hashes"),
        ("muted", "match the record"),
    ], gap=20)
    f.card(xs[3], 20, w, h, "ok", "review", [
        ("mono", "SUPPORTED (reviewed)"),
        ("muted", "only by a different actor"),
        ("muted", "with a reviewer role"),
    ], gap=20)
    for i in range(3):
        f.arrow([(xs[i] + w + 2, 81), (xs[i + 1] - 4, 81)])
    # re-anchor
    f.rect(270, 172, 720, 56, "plain", dashed=True)
    f.text(290, 206, "a newer commit is pinned", size=15, weight=600)
    f.text(510, 206, "txray findings reanchor --latest", size=14, mono=True, color="accent")
    f.arrow([(xs[3] + w / 2, 144), (xs[3] + w / 2, 168)])
    ow = 220
    ox = [270, 520, 770]
    f.card(ox[0], 268, ow, 122, "ok", "CURRENT", [
        ("muted", "the exact bytes are found"),
        ("muted", "once: identical, unchanged"),
        ("muted", "or relocated"),
    ], gap=20)
    f.card(ox[1], 268, ow, 122, "warn", "STALE", [
        ("muted", "path present, bytes gone;"),
        ("muted", "or a dependency changed;"),
        ("muted", "or a negative search hits"),
    ], gap=20)
    f.card(ox[2], 268, ow, 122, "bad", "UNVERIFIABLE", [
        ("muted", "bytes found several times,"),
        ("muted", "nowhere, or the citation"),
        ("muted", "was not usable"),
    ], gap=20)
    for x in ox:
        f.arrow([(630, 230), (630, 248), (x + ow / 2, 248), (x + ow / 2, 264)])
    # worklist and decisions
    for i, s in enumerate(("Three separate fields:", "status: what is claimed;",
                           "freshness: does it hold", "at a given commit;",
                           "workflow: review state.")):
        f.text(10, 290 + 20 * i, s, size=13, color="fg" if i == 0 else "muted",
               weight=600 if i == 0 else 400, max_width=240)
    f.rect(270, 426, 720, 80, "warn", dashed=True)
    f.text(290, 456, "txray findings stale: old span, aligned candidate span, exact commands",
           size=14, weight=600, max_width=690)
    f.text(290, 484, "decide:  supersede with new spans  |  review as is at the target  |  retract",
           size=14, color="muted", max_width=690)
    for x in ox[1:]:
        f.arrow([(x + ow / 2, 392), (x + ow / 2, 422)], color="warn", dashed=True)
    f.text(10, 440, "Freshness is not truth:", size=13, color="muted")
    f.text(10, 460, "only a review sets a status;", size=13, color="muted")
    f.text(10, 480, "a matching hash is text", size=13, color="muted")
    f.text(10, 500, "identity, not proof.", size=13, color="muted")
    return f


def reanchor(theme: str) -> Figure:
    f = Figure(1000, 470, "Re-anchoring one cited span on a newer commit",
               "Decision tree for one citation. If the file has the same blob, the outcome is "
               "identical. If the path still exists, the exact span bytes are searched in that "
               "file: found once means unchanged or relocated (CURRENT), several times means "
               "ambiguous (UNVERIFIABLE), not at all means changed (STALE, with a line-diff "
               "proposal). If the path is gone, other files are searched by identical blob, "
               "then through the code index: one match is relocated, none is missing "
               "(UNVERIFIABLE). The finding takes the worst outcome of its parts.", theme)
    q = lambda x, y, w, s: (f.rect(x, y, w, 44, "box", rx=22),
                            f.text(x + w / 2, y + 28, s, size=14, weight=600, anchor="middle",
                                   max_width=w - 16))
    q(330, 10, 340, "cited span, read at its own commit")
    q(330, 100, 340, "same path, same blob at the target?")
    f.arrow([(500, 56), (500, 96)])
    # yes branch: identical
    f.card(760, 84, 230, 76, "ok", "identical", [("mono", "CURRENT")], gap=18, line_size=13)
    f.arrow([(672, 122), (756, 122)], color="ok")
    f.text(714, 114, "yes", size=12, color="muted", anchor="middle")
    q(330, 200, 340, "does the path still exist?")
    f.arrow([(500, 146), (500, 196)])
    f.text(512, 178, "no", size=12, color="muted")
    # path exists
    f.rect(10, 290, 470, 44, "box", rx=22)
    f.text(245, 318, "exact span bytes in that file (whole lines)", size=14, weight=600,
           anchor="middle")
    f.arrow([(400, 246), (400, 266), (245, 266), (245, 286)])
    f.text(300, 262, "yes", size=12, color="muted")
    cw = 150
    f.card(10, 370, cw, 92, "ok", "once", [("muted", "unchanged or"), ("muted", "relocated")],
           gap=18, title_size=15, line_size=13)
    f.card(170, 370, cw, 92, "bad", "several", [("muted", "ambiguous:"),
                                                 ("muted", "none is picked")],
           gap=18, title_size=15, line_size=13)
    f.card(330, 370, cw, 92, "warn", "none", [("muted", "changed: STALE"),
                                              ("muted", "+ line-diff idea")],
           gap=18, title_size=15, line_size=13)
    for x in (10, 170, 330):
        f.arrow([(245, 336), (245, 352), (x + cw / 2, 352), (x + cw / 2, 366)])
    # path gone
    f.rect(520, 290, 470, 44, "box", rx=22)
    f.text(755, 318, "other files: same blob id, then index search", size=14, weight=600,
           anchor="middle")
    f.arrow([(600, 246), (600, 266), (755, 266), (755, 286)])
    f.text(612, 262, "no", size=12, color="muted")
    f.card(520, 370, cw, 92, "ok", "one", [("muted", "relocated:"), ("muted", "CURRENT")],
           gap=18, title_size=15, line_size=13)
    f.card(680, 370, cw, 92, "bad", "several", [("muted", "ambiguous:"),
                                                 ("muted", "UNVERIFIABLE")],
           gap=18, title_size=15, line_size=13)
    f.card(840, 370, cw, 92, "bad", "none", [("muted", "missing:"),
                                              ("muted", "UNVERIFIABLE")],
           gap=18, title_size=15, line_size=13)
    for x in (520, 680, 840):
        f.arrow([(755, 336), (755, 352), (x + cw / 2, 352), (x + cw / 2, 366)])
    f.text(990, 210, "A finding takes the worst of its parts:", size=13, color="muted",
           anchor="end")
    f.text(990, 230, "STALE, then UNVERIFIABLE, then CURRENT.", size=13, color="muted",
           anchor="end")
    f.text(10, 210, "Not usable at its own commit:", size=13, color="muted")
    f.text(10, 230, "unusable, UNVERIFIABLE.", size=13, color="muted")
    return f


def citation(theme: str) -> Figure:
    f = Figure(1000, 330, "Anatomy of a citation",
               "A citation names a full commit, a path and a 1-based inclusive line range. "
               "TimelineXray cuts those lines from the git blob as bytes and records the byte "
               "range, the blob id, the SHA-256 of the blob and of the span, and whether the "
               "anchor text occurs inside the span. Example: commit 92f0918, "
               "home-mixer/params/param.rs line 347, span SHA-256 4b5c535d, anchor ClickWeight "
               "FOUND.", theme)
    # the span as it appears in the blob
    f.rect(10, 20, 980, 118, "box")
    f.text(30, 50, "home-mixer/params/param.rs at 92f0918f6cb52837cb53a8c790c68f906b2c0db1",
           size=14, mono=True, color="muted", max_width=950)
    f.rect(24, 64, 952, 30, "accent", rx=4)
    f.text(36, 85, "347", size=14, mono=True, color="muted")
    f.text(90, 85, 'param!(ClickWeight, f64, "rust_home_mixer_click_weight", 0.3);',
           size=14, mono=True, max_width=860)
    f.text(36, 122, "346 / 348 are outside the span; only LF ends a line; nothing is decoded "
           "or normalised", size=13, color="muted", max_width=940)
    rows = [
        ("commit", "92f0918f6cb52837cb53a8c790c68f906b2c0db1", "a full id; branches are refused"),
        ("lines", "347-347 of 1120 (bytes 8255-8318)", "1-based, inclusive, never clamped"),
        ("blob", "a99841a766c8242f89e68c70c13f5e1590d678e9", "the git object the bytes come from"),
        ("span sha256", "4b5c535dbf7b2b6ac766b2405034e203...", "hash of exactly these bytes"),
        ("anchor", "ClickWeight: FOUND at line 347", "must occur inside the cited lines"),
    ]
    y = 176
    for key, value, note in rows:
        f.text(30, y, key, size=14, weight=600)
        f.text(160, y, value, size=14, mono=True, color="accent", max_width=440)
        f.text(620, y, note, size=13, color="muted", max_width=370)
        y += 30
    return f


FIGURES = {
    "overview": overview,
    "overview-narrow": overview_narrow,
    "pipeline": pipeline,
    "pipeline-narrow": pipeline_narrow,
    "finding-lifecycle": lifecycle,
    "reanchor": reanchor,
    "citation": citation,
}


def render_all() -> dict[str, str]:
    out = {}
    for name, build in FIGURES.items():
        for theme in PALETTES:
            out[f"{name}-{theme}.svg"] = build(theme).render()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="compare with the committed files instead of writing")
    args = parser.parse_args(argv)
    files = render_all()
    if args.check:
        bad = [name for name, data in files.items()
               if not (OUT / name).is_file() or (OUT / name).read_text("utf-8") != data]
        for name in bad:
            print(f"differs: docs/img/{name}")
        return 1 if bad else 0
    for name, data in files.items():
        (OUT / name).write_text(data, "utf-8", newline="\n")
    print(f"wrote {len(files)} files to docs/img/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
