"""M1 (RWE) and M2 (relative reach) on synthetic datasets, including the P6 worked example."""

from __future__ import annotations

import datetime as dt
import math
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from fractions import Fraction
from pathlib import Path

from timelinexray.analytics import coefficients as co
from timelinexray.analytics import reach, rwe
from timelinexray.analytics.common import NOTICE
from timelinexray.analytics.dataset import Dataset, import_file, load_dataset
from timelinexray.errors import InvalidInput
from tests.analytics_support import CANONICAL, post, settings, write_export

UTC = dt.timezone.utc
H = dt.timedelta(hours=1)


def when(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text).astimezone(UTC)


class _Datasets(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-metrics-")
        self.tmp = Path(self._tmp.name)
        self.count = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def dataset(self, captures: Sequence[tuple[dt.datetime, Sequence[Mapping[str, str]]]],
                *, fields: Sequence[str] = CANONICAL, name: str = "ds", **overrides: object,
                ) -> Dataset:
        """Import one synthetic export per capture time into a new dataset."""
        for captured, rows in captures:
            self.count += 1
            path = write_export(self.tmp / f"export-{self.count}.csv", rows, fields=fields)
            import_file(path, self.tmp / name, settings(captured_at=captured, **overrides))
        return load_dataset(self.tmp / name)

    def single(self, rows: Sequence[Mapping[str, str]], **kwargs: object) -> Dataset:
        return self.dataset([(when("2026-09-30T12:00:00+00:00"), rows)], **kwargs)


def _rows(result: Mapping[str, object]) -> dict[str, dict]:
    return {row["post_id"]: row for row in result["posts"]}  # type: ignore[index,union-attr]


class RweTest(_Datasets):
    def test_p6_worked_example(self) -> None:
        dataset = self.single([post("1", impressions=10000, likes=200, replies=10, reposts=15)])
        result = rwe.compute(dataset, rwe.variant("core"))
        row = _rows(result)["1"]
        self.assertEqual((row["status"], row["rwe"]), ("defined", 16.5))
        self.assertEqual(
            [(c["field"], c["coefficient"], c["rate_per_1000"], c["contribution"],
              c["one_event_changes_rwe_by"]) for c in row["components"]],
            [("likes", 0.5, 20.0, 10.0, 0.05), ("replies", 5.0, 1.0, 5.0, 0.5),
             ("reposts", 1.0, 1.5, 1.5, 0.1)],
        )
        self.assertEqual(Fraction(1000) * (Fraction(1, 2) * 200 + 5 * 10 + 15) / 10000,
                         Fraction(33, 2))
        self.assertEqual(result["metric"]["unit"],
                         "reference-weighted observed events per 1,000 recorded impressions")
        self.assertEqual(result["notice"], NOTICE)
        self.assertTrue(NOTICE.startswith("This is not a reach prediction."))
        table = result["metric"]["coefficient_table"]
        self.assertEqual((table["commit"], table["label"]), (co.UPSTREAM_COMMIT, "public default"))
        self.assertEqual(row["negative_feedback"], "unknown")

    def test_exact_arithmetic_and_rounding(self) -> None:
        dataset = self.single([post("1", impressions=1234, likes=12, replies=3, reposts=2)])
        row = _rows(rwe.compute(dataset, rwe.variant("core")))["1"]
        self.assertEqual(row["rwe"], float(round(Fraction(1000 * (6 + 15 + 2), 1234), 6)))
        self.assertEqual(row["outcomes"]["likes"]["rate_per_1000"],
                         float(round(Fraction(12000, 1234), 6)))

    def test_zero_blank_and_missing_are_undefined_not_zero(self) -> None:
        dataset = self.single([
            post("1", impressions=0, likes=0, replies=0, reposts=0),
            post("2", impressions="", likes=3, replies=0, reposts=0),
            post("3", impressions=500, likes=3, replies="", reposts=0),
        ])
        rows = _rows(rwe.compute(dataset, rwe.variant("core")))
        self.assertEqual({p: (r["rwe"], r["reason"]) for p, r in rows.items()}, {
            "1": (None, "zero-impressions"),
            "2": (None, "impressions-blank"),
            "3": (None, "component-blank:replies"),
        })
        self.assertIsNone(rows["1"]["components"][0]["rate_per_1000"])
        self.assertIsNone(rows["1"]["outcomes"]["bookmarks"]["rate_per_1000"])
        likes = rows["3"]["components"][0]
        self.assertEqual((likes["status"], likes["contribution"]), ("observed", 3.0))
        self.assertEqual(rows["3"]["components"][1]["status"], "unknown")

    def test_missing_column_makes_rwe_undefined(self) -> None:
        fields = [f for f in CANONICAL if f != "reposts"]
        dataset = self.single([post("1", impressions=100, likes=1, replies=1)], fields=fields)
        row = _rows(rwe.compute(dataset, rwe.variant("core")))["1"]
        self.assertEqual((row["rwe"], row["reason"]),
                         (None, "component-missing-column:reposts"))

    def test_pooled_and_mean_rates_differ(self) -> None:
        dataset = self.single([post("1", impressions=1000, likes=100, replies=0, reposts=0),
                               post("2", impressions=9000, likes=0, replies=0, reposts=0)])
        aggregate = rwe.compute(dataset, rwe.variant("core"))["aggregate"]
        self.assertEqual((aggregate["mean_post_rwe"], aggregate["pooled_rwe"]), (25.0, 5.0))
        self.assertEqual(aggregate["median_post_rwe"], 25.0)

    def test_variants(self) -> None:
        row = post("1", impressions=1000, likes=10, replies=1, reposts=1, url_clicks=5,
                   new_follows=2, detail_expands=30, shares=40, engagements=90, bookmarks=7)
        dataset = self.single([row])
        with self.assertRaisesRegex(InvalidInput, "--attribution-validated"):
            rwe.variant("extended")
        with self.assertRaises(InvalidInput):
            rwe.variant("everything")
        expected = {"core": 11.0, "extended": 11.0 + 1.0 + 8.0, "sensitivity": 11.0 + 9.0}
        for name, value in expected.items():
            with self.subTest(variant=name):
                result = rwe.compute(dataset, rwe.variant(name, attribution_validated=True))
                self.assertEqual(_rows(result)["1"]["rwe"], value)
                used = {t["field"] for t in result["metric"]["action_set"]}
                self.assertFalse(used & {"shares", "engagements", "bookmarks", "impressions"})
                omitted = {t["head"]: t for t in result["omitted_terms"]}
                self.assertEqual(len(omitted) + len(used), 25)
                for head in ("not_interested_score", "block_author_score", "mute_author_score",
                             "report_score", "not_dwelled_score"):
                    self.assertEqual(omitted[head]["contribution"], "unknown")
                    self.assertIn("never zero", omitted[head]["reason"])
                self.assertEqual(result["metric"]["attribution"] is not None, name == "extended")

    def test_latest_snapshot_or_common_horizon(self) -> None:
        published = when("2026-09-20T10:00:00+00:00")
        row_early = post("1", created="2026-09-20T10:00:00Z", impressions=1000, likes=10,
                         replies=0, reposts=0)
        row_late = post("1", created="2026-09-20T10:00:00Z", impressions=4000, likes=20,
                        replies=0, reposts=0)
        dataset = self.dataset([(published + 25 * H, [row_early]),
                                (published + 73 * H, [row_late])])
        latest = _rows(rwe.compute(dataset, rwe.variant("core")))["1"]
        self.assertEqual((latest["rwe"], latest["snapshot"]["observation_age_hours"]), (2.5, 73.0))
        at_day = _rows(rwe.compute(dataset, rwe.variant("core"), horizon=24 * H,
                                   tolerance=2 * H))["1"]
        self.assertEqual((at_day["rwe"], at_day["snapshot"]["observation_age_hours"]), (5.0, 25.0))
        too_tight = _rows(rwe.compute(dataset, rwe.variant("core"), horizon=24 * H,
                                      tolerance=0 * H))["1"]
        self.assertEqual(too_tight["reason"], "no-snapshot-in-horizon-window")

    def test_post_selection_and_scope(self) -> None:
        dataset = self.single([post("1", impressions=10, likes=1, replies=0, reposts=0),
                               post("2", impressions=10, likes=2, replies=0, reposts=0)])
        result = rwe.compute(dataset, rwe.variant("core"), posts=["2"])
        self.assertEqual(list(_rows(result)), ["2"])
        with self.assertRaisesRegex(InvalidInput, "not in the dataset"):
            rwe.compute(dataset, rwe.variant("core"), posts=["3"])
        with self.assertRaisesRegex(InvalidInput, "scope 'organic' is not in this dataset"):
            rwe.compute(dataset, rwe.variant("core"), scope="organic")


class RelativeReachTest(_Datasets):
    HORIZON = 24 * H
    TOLERANCE = 2 * H

    def build(self, posts: Sequence[tuple[str, str, object]], *, age: dt.timedelta = 25 * H,
              name: str = "ds", **overrides: object) -> Dataset:
        """One export per post, captured ``age`` after its publication."""
        captures = []
        for post_id, created, impressions in posts:
            captured = when(created) + age
            captures.append((captured, [post(post_id, created=created.replace("+00:00", "Z"),
                                             impressions=impressions)]))
        return self.dataset(captures, name=name, **overrides)

    def compute(self, dataset: Dataset, **kwargs: object) -> dict[str, dict]:
        options = {"horizon": self.HORIZON, "tolerance": self.TOLERANCE, "min_parent": 1,
                   **kwargs}
        return _rows(reach.compute(dataset, reach.Settings(**options)))  # type: ignore[arg-type]

    def test_shrinkage_heuristic_by_hand(self) -> None:
        dataset = self.build([
            ("1", "2026-09-07T10:00:00+00:00", 99),     # Monday 06-12: same bucket as target
            ("2", "2026-09-08T10:00:00+00:00", 999),    # Tuesday
            ("3", "2026-09-09T20:00:00+00:00", 9999),   # Wednesday
            ("9", "2026-09-14T11:00:00+00:00", 631),    # target, Monday 06-12
        ])
        row = self.compute(dataset, kappa=1.0, min_parent=3)["9"]
        m_p, m_b = math.log(1000), math.log(100)
        shrunk = 0.5 * m_b + 0.5 * m_p
        baseline = math.exp(shrunk) - 1
        self.assertEqual(row["bucket"], {"weekday": "Mon", "band": "06:00-12:00"})
        b = row["baseline"]
        self.assertEqual((b["n_parent"], b["n_bucket"], b["lambda"]), (3, 1, 0.5))
        self.assertAlmostEqual(b["log_median_parent"], m_p, places=6)
        self.assertAlmostEqual(b["log_median_bucket"], m_b, places=6)
        self.assertAlmostEqual(b["value"], baseline, places=5)
        self.assertAlmostEqual(row["rr"], 631 / baseline, places=5)
        self.assertAlmostEqual(row["rr_plus"], 632 / (1 + baseline), places=5)
        self.assertAlmostEqual(row["lr"], math.log(632) - shrunk, places=5)
        self.assertNotAlmostEqual(row["rr"], row["rr_plus"], places=3)
        self.assertEqual(b["window"]["first_published"], "2026-09-07T10:00:00Z")
        self.assertEqual(b["window"]["last_published"], "2026-09-09T20:00:00Z")

    def test_kappa_limits(self) -> None:
        dataset = self.build([("1", "2026-09-07T10:00:00+00:00", 99),
                              ("2", "2026-09-08T10:00:00+00:00", 9999),
                              ("9", "2026-09-14T11:00:00+00:00", 5)])
        only_bucket = self.compute(dataset, kappa=0.0)["9"]["baseline"]
        self.assertAlmostEqual(only_bucket["value"], 99, places=5)
        parent_heavy = self.compute(dataset, kappa=1e9)["9"]["baseline"]
        self.assertAlmostEqual(parent_heavy["value"], math.sqrt(100 * 10000) - 1, places=3)

    def test_empty_bucket_uses_the_parent(self) -> None:
        dataset = self.build([("1", "2026-09-08T10:00:00+00:00", 99),
                              ("9", "2026-09-14T11:00:00+00:00", 5)])
        baseline = self.compute(dataset)["9"]["baseline"]
        self.assertEqual((baseline["n_bucket"], baseline["lambda"]), (0, 0.0))
        self.assertIsNone(baseline["log_median_bucket"])
        self.assertAlmostEqual(baseline["value"], 99, places=5)

    def test_zero_baseline_leaves_rr_undefined_and_rr_plus_defined(self) -> None:
        dataset = self.build([("1", "2026-09-07T10:00:00+00:00", 0),
                              ("2", "2026-09-08T10:00:00+00:00", 0),
                              ("9", "2026-09-14T11:00:00+00:00", 40)])
        row = self.compute(dataset)["9"]
        self.assertEqual(row["baseline"]["value"], 0.0)
        self.assertEqual((row["rr"], row["rr_reason"]), (None, "baseline-zero"))
        self.assertEqual(row["rr_plus"], 41.0)
        self.assertEqual(row["status"], "rr-undefined")

    def test_zero_impressions_with_positive_baseline(self) -> None:
        dataset = self.build([("1", "2026-09-07T10:00:00+00:00", 99),
                              ("9", "2026-09-14T11:00:00+00:00", 0)])
        row = self.compute(dataset)["9"]
        self.assertEqual(row["rr"], 0.0)
        self.assertAlmostEqual(row["rr_plus"], 1 / 100, places=6)

    def test_only_pre_publication_observations(self) -> None:
        # Post 1 is published before the target, but its 24 h snapshot is captured after the
        # target's publication, so it must not enter the target's baseline.
        dataset = self.build([("1", "2026-09-14T01:00:00+00:00", 500),
                              ("9", "2026-09-14T11:00:00+00:00", 50)])
        rows = self.compute(dataset)
        self.assertEqual((rows["9"]["rr"], rows["9"]["rr_reason"]),
                         (None, "no-pre-publication-baseline"))
        self.assertEqual(rows["1"]["rr_reason"], "no-pre-publication-baseline")
        control = self.build([("1", "2026-09-13T01:00:00+00:00", 500),
                              ("9", "2026-09-14T11:00:00+00:00", 50)], name="control")
        self.assertEqual(self.compute(control)["9"]["baseline"]["n_parent"], 1)

    def test_minimum_parent_size(self) -> None:
        dataset = self.build([("1", "2026-09-07T10:00:00+00:00", 99),
                              ("9", "2026-09-14T11:00:00+00:00", 5)])
        row = self.compute(dataset, min_parent=2)["9"]
        self.assertEqual((row["baseline"], row["rr_reason"], row["rr_plus_reason"]),
                         (None, "baseline-below-min-parent:1<2", "baseline-below-min-parent:1<2"))

    def test_horizon_window_and_count_kind(self) -> None:
        late = self.build([("1", "2026-09-07T10:00:00+00:00", 99)], age=50 * H)
        self.assertEqual(self.compute(late)["1"]["rr_reason"], "no-snapshot-in-horizon-window")
        window = self.build([("1", "2026-09-07T10:00:00+00:00", 99)], name="window",
                            counts="window")
        self.assertEqual(self.compute(window)["1"]["rr_reason"], "counts-not-declared-cumulative")

    def test_publication_time_problems(self) -> None:
        dataset = self.single([post("1", created="2026-09-20", impressions=5),
                               post("2", created="", impressions=5)])
        rows = self.compute(dataset)
        self.assertEqual(rows["1"]["rr_reason"], "publication-time-date-only")
        self.assertEqual(rows["2"]["rr_reason"], "publication-time-blank")

    def test_blank_impressions_at_horizon(self) -> None:
        dataset = self.build([("1", "2026-09-07T10:00:00+00:00", 99),
                              ("9", "2026-09-14T11:00:00+00:00", "")])
        row = self.compute(dataset)["9"]
        self.assertIsNotNone(row["baseline"])
        self.assertEqual((row["rr"], row["rr_reason"]), (None, "impressions-blank"))

    def test_buckets_follow_the_analysis_time_zone(self) -> None:
        dataset = self.build([("1", "2026-09-14T23:30:00+00:00", 10)])
        self.assertEqual(self.compute(dataset)["1"]["bucket"],
                         {"weekday": "Mon", "band": "18:00-24:00"})
        local = self.compute(dataset, timezone="Asia/Tokyo")["1"]
        self.assertEqual(local["bucket"], {"weekday": "Tue", "band": "06:00-12:00"})
        self.assertTrue(local["published_local"].endswith("+09:00"))

    def test_scopes_are_never_combined(self) -> None:
        self.build([("1", "2026-09-07T10:00:00+00:00", 99)], scope="organic")
        dataset = self.build([("2", "2026-09-08T10:00:00+00:00", 99)], scope="promoted")
        with self.assertRaisesRegex(InvalidInput, "choose one with --scope"):
            reach.compute(dataset, reach.Settings(self.HORIZON, self.TOLERANCE))
        chosen = reach.compute(dataset, reach.Settings(self.HORIZON, self.TOLERANCE),
                               scope="organic")
        self.assertEqual([r["post_id"] for r in chosen["posts"]], ["1"])

    def test_settings_are_validated(self) -> None:
        dataset = self.build([("1", "2026-09-07T10:00:00+00:00", 99)])
        for bad in ({"band_hours": 5}, {"kappa": -1.0}, {"kappa": math.inf}, {"min_parent": 0}):
            with self.subTest(**bad), self.assertRaises(InvalidInput):
                self.compute(dataset, **bad)
        result = reach.compute(dataset, reach.Settings(self.HORIZON, self.TOLERANCE))
        self.assertEqual(result["metric"]["action_coefficients"],
                         "not applicable: M2 uses no action weights")
        self.assertIn("P6-018", " ".join(result["caveats"]))


if __name__ == "__main__":
    unittest.main()
