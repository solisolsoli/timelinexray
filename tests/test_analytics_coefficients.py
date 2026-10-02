"""The coefficient table, and its check against the pinned upstream source.

The upstream checks pin commit 77d431a from a local clone through a ``file://`` URL that is
allowed via ``TXRAY_ALLOW_FILE_URLS`` (never from the network) and read every cited span
with the snapshot span reader. Without a local clone they are skipped, like the Milestone 1
acceptance tests.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from timelinexray.analytics import checks
from timelinexray.analytics import coefficients as co
from timelinexray.analytics import rwe
from timelinexray.netguard import ENV_ALLOW_FILE_URLS
from timelinexray.snapshot import SnapshotStore
from timelinexray.span import FOUND
from tests.support import UPSTREAM_COMMIT, file_url, run_cli, upstream_git_dir

UPSTREAM = upstream_git_dir()

_PARAM = re.compile(r'\s*param!\(\s*(\w+),\s*f64,\s*"([^"]+)",\s*(-?[0-9]+\.[0-9]+)\s*\);\s*')


def apply_calls(text: str) -> list[tuple[str, str]]:
    """``(head, weight expression)`` of every ``apply(scores.<head>, <expr>)`` call."""
    calls = []
    start = text.find("apply(")
    while start != -1:
        depth, end = 0, start + len("apply")
        for end in range(start + len("apply"), len(text)):
            if text[end] == "(":
                depth += 1
            elif text[end] == ")":
                depth -= 1
                if depth == 0:
                    break
        head, expr = text[start + len("apply("):end].split(",", 1)
        calls.append((" ".join(head.split()).removeprefix("scores."),
                      " ".join(expr.split()).rstrip(",").strip()))
        start = text.find("apply(", end)
    return calls


class CoefficientTableTest(unittest.TestCase):
    def test_table_identity(self) -> None:
        self.assertEqual(co.UPSTREAM_COMMIT, UPSTREAM_COMMIT)
        self.assertEqual(co.VALUE_LABEL, "public default")
        info = co.table_info()
        self.assertEqual((info["commit"], info["label"], info["version"]),
                         (UPSTREAM_COMMIT, "public default", 1))

    def test_twenty_five_terms_with_exact_values(self) -> None:
        heads = [c.head for c in co.COEFFICIENTS]
        self.assertEqual(len(heads), 25)
        self.assertEqual(len(set(heads)), 25)
        self.assertEqual({c.head for c in co.COEFFICIENTS if c.negative},
                         {"not_interested_score", "block_author_score", "mute_author_score",
                          "report_score", "not_dwelled_score"})
        values = {c.head: c.value for c in co.COEFFICIENTS}
        self.assertEqual((values["favorite_score"], values["reply_score"],
                          values["retweet_score"], values["open_link_score"],
                          values["follow_author_score"], values["click_score"]),
                         (Fraction(1, 2), 5, 1, Fraction(1, 5), 4, Fraction(3, 10)))
        for c in co.COEFFICIENTS + co.BOOSTS:
            with self.subTest(param=c.param):
                self.assertEqual(c.value, Fraction(Decimal(c.literal)))
                self.assertEqual(c.to_dict()["label"], "public default")
                self.assertEqual(c.to_dict()["source"]["commit"], UPSTREAM_COMMIT)
        self.assertEqual(set(rwe.EXPORT_MAPPING), set(heads))

    def test_variants_use_only_table_heads(self) -> None:
        for variant in rwe.VARIANTS.values():
            for term in variant.terms:
                self.assertIn(term.head, co.BY_HEAD)


@unittest.skipIf(UPSTREAM is None, "no local x-algorithm clone with 77d431a "
                 "(set TXRAY_TEST_UPSTREAM to a local clone)")
class CoefficientSourceTest(unittest.TestCase):
    """Every entry of the table, read back from the pinned upstream source."""

    @classmethod
    def setUpClass(cls) -> None:
        assert UPSTREAM is not None
        cls._tmp = tempfile.TemporaryDirectory(prefix="txray-coefficients-")
        store_dir = Path(cls._tmp.name) / "store"
        url = file_url(UPSTREAM.parent if UPSTREAM.name == ".git" else UPSTREAM)
        code, _, err = run_cli(["pin", UPSTREAM_COMMIT, "--upstream", url,
                                "--store", str(store_dir)], {ENV_ALLOW_FILE_URLS: url})
        assert code == 0, err.decode()
        cls.store = SnapshotStore(store_dir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def span_text(self, span: co.SourceSpan, anchor: str | None = None) -> str:
        read = self.store.read_span(UPSTREAM_COMMIT, span.path, span.start, span.end,
                                    anchor=anchor)
        if anchor is not None:
            self.assertEqual(read.anchor.verdict, FOUND, f"{span} {anchor!r}")
        return read.data.decode("utf-8")

    def test_parameter_declarations(self) -> None:
        for c in co.COEFFICIENTS + co.BOOSTS:
            with self.subTest(param=c.param):
                text = self.span_text(c.source(), f'"{c.switch}"')
                match = _PARAM.fullmatch(text)
                self.assertIsNotNone(match, text)
                self.assertEqual(match.groups(), (c.param, c.switch, c.literal))

    def test_sum_order_and_weight_expressions(self) -> None:
        calls = apply_calls(self.span_text(co.SUM_SOURCE, "post_unexplored_score"))
        self.assertEqual(calls, [(c.head, c.weight_expr) for c in co.COEFFICIENTS])

    def test_adapter_fills_each_field_from_its_parameter(self) -> None:
        adapter = dict(re.findall(r"(\w+): params\.get\((\w+)\)",
                                  self.span_text(co.ADAPTER_SOURCE)))
        conditions = self.span_text(co.CONDITION_SOURCE)
        local = {name: field for name, field in re.findall(
            r"let (\w+) = if [^{]+\{\s*weights\.(\w+)", conditions)}
        self.assertEqual(local, {"vqv_weight": "vqv", "quoted_vqv_weight": "quoted_vqv",
                                 "post_unexplored_weight": "post_unexplored"})
        functions = self.span_text(co.BOOST_FUNCTION_SOURCE)
        for c in co.COEFFICIENTS + co.BOOSTS:
            with self.subTest(head=c.head, param=c.param):
                self.assertEqual(adapter[c.field], c.param)
                expr = c.weight_expr
                if expr in local:
                    self.assertEqual(local[expr], c.field)
                elif expr.endswith("_weight_for(candidate)"):
                    base = expr.removeprefix("weights.").removesuffix("_weight_for(candidate)")
                    self.assertIn(f"pub fn {base}_weight_for(", functions)
                    if c in co.BOOSTS:
                        self.assertIn(f"return self.{base} + self.{c.field};", functions)
                    else:
                        self.assertEqual(c.field, base)
                else:
                    self.assertEqual(expr, f"weights.{c.field}")
        self.span_text(co.BOOST_ELIGIBILITY_SOURCE,
                       "!self.is_reply && !self.is_retweet && self.is_mutual_follow_author")

    def test_sync_annotation_and_readme_statements(self) -> None:
        line = co.SourceSpan(co.PARAM_SOURCE, co.SYNC_ANNOTATION_LINE, co.SYNC_ANNOTATION_LINE)
        self.assertEqual(self.span_text(line), co.SYNC_ANNOTATION + "\n")
        for span, anchor in co.README_STATEMENTS:
            with self.subTest(span=span):
                self.span_text(span, anchor)

    def test_p6_example_with_coefficients_read_from_source(self) -> None:
        weights = {}
        for head in ("favorite_score", "reply_score", "retweet_score"):
            c = co.BY_HEAD[head]
            match = _PARAM.fullmatch(self.span_text(c.source(), f'"{c.switch}"'))
            weights[head] = Fraction(Decimal(match.group(3)))
        rwe_core = Fraction(1000, 10000) * (weights["favorite_score"] * 200
                                            + weights["reply_score"] * 10
                                            + weights["retweet_score"] * 15)
        self.assertEqual(rwe_core, Fraction(33, 2))  # 16.5

    def test_m3_and_m4_citations_are_intact_spans(self) -> None:
        citations = [c for item in checks.M3_CHECKLIST for c in item.citations if c.path]
        citations += [c for *_, cited in checks.M4_EVIDENCE for c in cited]
        self.assertEqual(len(citations), 6)
        for cite in citations:
            with self.subTest(path=cite.path, lines=cite.lines):
                start, end = (int(x) for x in cite.lines.split("-"))
                self.assertEqual(cite.commit, UPSTREAM_COMMIT)
                self.span_text(co.SourceSpan(cite.path, start, end), cite.anchor)


if __name__ == "__main__":
    unittest.main()
