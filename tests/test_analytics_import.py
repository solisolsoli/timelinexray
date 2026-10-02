"""The P6 §2.2 import contract with synthetic English and Turkish exports."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from timelinexray.analytics import schema
from timelinexray.analytics.common import count_decreases
from timelinexray.analytics.dataset import (
    BLANK,
    DATASET_FILE,
    MISSING_COLUMN,
    OBSERVED,
    SNAPSHOTS_FILE,
    DatasetError,
    ExportRejected,
    import_file,
    load_dataset,
    read_export,
    read_header_row,
)
from timelinexray.analytics.parsing import (
    AMBIGUOUS,
    NONEXISTENT,
    PARSED,
    CellError,
    load_zone,
    parse_count,
    parse_created_at,
    parse_post_id,
)
from timelinexray.errors import IntegrityError, InvalidInput
from timelinexray.findings.model import check_finding_id
from tests.analytics_support import (
    CANONICAL,
    EN_HEADERS,
    TR_HEADER_ROW,
    TR_HEADER_ROW_SHA256,
    TR_HEADERS,
    post,
    settings,
    write_export,
)


class _TempDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="txray-analytics-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def export(self, rows, name="export.csv", **kwargs) -> Path:
        return write_export(self.tmp / name, rows, **kwargs)


class HeaderAliasTest(_TempDir):
    def test_turkish_row_is_the_transcribed_row(self) -> None:
        self.assertEqual(hashlib.sha256(TR_HEADER_ROW.encode("utf-8")).hexdigest(),
                         TR_HEADER_ROW_SHA256)
        self.assertEqual(len(TR_HEADERS), 17)

    def test_turkish_row_maps_to_canonical_fields_in_order(self) -> None:
        resolution = schema.resolve_headers(TR_HEADERS, "x-post-v1", "tr")
        self.assertEqual(tuple(c.field for c in resolution.columns), CANONICAL)
        self.assertEqual(tuple(c.header for c in resolution.columns), TR_HEADERS)
        table = schema.ALIASES[("x-post-v1", "tr")].headers
        self.assertEqual(tuple(table[f] for f in CANONICAL), TR_HEADERS)

    def test_english_row_maps_to_canonical_fields(self) -> None:
        resolution = schema.resolve_headers(EN_HEADERS, "x-post-v1", "en")
        self.assertEqual(tuple(c.field for c in resolution.columns), CANONICAL)
        self.assertEqual(schema.ALIASES[("x-post-v1", "en")].status, "EXTERNAL_RECHECK")

    def test_a_supported_table_names_what_confirmed_it(self) -> None:
        """An alias table's status changes only through a recorded finding id or the hash
        pin of a transcribed row (audit section 6, item 11): the English table stays
        EXTERNAL_RECHECK until a finding made from a real export or X's documentation
        names it in ``confirmed_by``."""
        finding_id = re.compile(r"[A-Z][A-Za-z0-9]*-[A-Za-z0-9_.-]+")
        for key, table in schema.ALIASES.items():
            with self.subTest(table=key):
                self.assertIn(table.status, ("SUPPORTED", "EXTERNAL_RECHECK"))
                if table.status == "SUPPORTED":
                    self.assertIsNotNone(table.confirmed_by)
                    check_finding_id(table.confirmed_by)  # citable in a ledger event
                    self.assertTrue(re.fullmatch("sha256:[0-9a-f]{64}", table.confirmed_by)
                                    or finding_id.fullmatch(table.confirmed_by),
                                    table.confirmed_by)
                else:
                    self.assertIsNone(table.confirmed_by)
        english = schema.ALIASES[("x-post-v1", "en")]
        self.assertTrue(english.status == "EXTERNAL_RECHECK"
                        or finding_id.fullmatch(english.confirmed_by or ""),
                        "the English table becomes SUPPORTED only through a recorded finding")
        turkish = schema.ALIASES[("x-post-v1", "tr")]
        self.assertEqual(turkish.confirmed_by, "sha256:" + TR_HEADER_ROW_SHA256)

    def test_header_row_is_read_without_the_data_rows(self) -> None:
        path = self.tmp / "export.csv"
        path.write_bytes(b"\xef\xbb\xbf" + TR_HEADER_ROW.encode("utf-8") + b"\r\n"
                         + b'"broken,row\n')
        self.assertEqual(read_header_row(path), ("export.csv", list(TR_HEADERS)))
        # a data row that is not UTF-8 is not decoded either (a full import rejects it)
        path.write_bytes(TR_HEADER_ROW.encode("utf-8") + b"\n1,\xff\xfe\n")
        self.assertEqual(read_header_row(path), ("export.csv", list(TR_HEADERS)))
        path.write_bytes(b'"a\r\nb",c\r\n1,\xff\n')
        self.assertEqual(read_header_row(path), ("export.csv", ["a\r\nb", "c"]))
        path.write_bytes(b"x,\xc3\n")
        with self.assertRaisesRegex(ExportRejected, r"not UTF-8 text \(invalid byte at offset 2\)"):
            read_header_row(path)
        path.write_bytes(b"")
        with self.assertRaisesRegex(ExportRejected, "must be the header row"):
            read_header_row(path)
        path.write_bytes(b"\xff\xfe,x\n1,2\n")
        with self.assertRaisesRegex(ExportRejected, "not UTF-8"):
            read_header_row(path)
        path.write_bytes(b'"a,b\n1,2\n')
        with self.assertRaisesRegex(ExportRejected, "malformed CSV"):
            read_header_row(path)
        with self.assertRaisesRegex(ExportRejected, "cannot read"):
            read_header_row(self.tmp / "missing.csv")

    def test_canonical_field_list(self) -> None:
        self.assertEqual(schema.FIELD_NAMES, CANONICAL)
        self.assertEqual(schema.COUNT_FIELDS, CANONICAL[4:])

    def test_case_whitespace_and_turkish_dotted_i(self) -> None:
        upper = [h.replace("i", "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}")
                  .replace("\N{LATIN SMALL LETTER DOTLESS I}", "I").upper() for h in TR_HEADERS]
        spaced = [f"  {h.replace(' ', '   ')} " for h in TR_HEADERS]
        for variant in (upper, spaced):
            resolution = schema.resolve_headers(variant, "x-post-v1", "tr")
            self.assertEqual(tuple(c.field for c in resolution.columns), CANONICAL)
            self.assertEqual([c.header for c in resolution.columns], variant)  # kept verbatim

    def test_no_cross_language_translation(self) -> None:
        self.assertTrue(all(c.field is None for c in
                            schema.resolve_headers(TR_HEADERS, "x-post-v1", "en").columns))
        self.assertTrue(all(c.field is None for c in
                            schema.resolve_headers(EN_HEADERS, "x-post-v1", "tr").columns))
        # "Views" is not assumed to mean impressions and "Shares" never means reposts.
        resolution = schema.resolve_headers(["Post id", "Views", "Shares"], "x-post-v1", "en")
        self.assertEqual([c.field for c in resolution.columns], ["post_id", None, "shares"])


class ParserTest(unittest.TestCase):
    def test_counts_follow_the_declared_locale(self) -> None:
        self.assertEqual(parse_count("1234", "en"), 1234)
        self.assertEqual(parse_count("1234", "tr"), 1234)
        self.assertEqual(parse_count("1,234,567", "en"), 1234567)
        self.assertEqual(parse_count("1.234.567", "tr"), 1234567)
        self.assertEqual(parse_count(" 0 ", "en"), 0)
        for raw, locale in (("1.234", "en"), ("1,234", "tr"), ("12,5", "tr"), ("12.5", "en"),
                            ("-3", "en"), ("+3", "en"), ("1,5K", "tr"), ("1,23", "en"),
                            ("1 234", "en"), ("", "en")):
            with self.subTest(raw=raw, locale=locale):
                with self.assertRaises(CellError):
                    parse_count(raw, locale)

    def test_post_ids_stay_exact_strings(self) -> None:
        big = "1970000000000000001"
        self.assertEqual(parse_post_id(f" {big} "), big)
        self.assertNotEqual(str(int(float(big))), big)  # what float rounding would do
        with self.assertRaisesRegex(CellError, "scientific notation"):
            parse_post_id("1.97E+18")
        for raw in ("12a", "", "1.5", "-1"):
            with self.subTest(raw=raw), self.assertRaises(CellError):
                parse_post_id(raw)

    def test_times_and_daylight_saving(self) -> None:
        berlin = load_zone("Europe/Berlin")
        utc = load_zone("UTC")
        parsed = parse_created_at("2026-07-01 12:00", berlin)
        self.assertEqual((parsed.status, parsed.offset_source), (PARSED, "declared-zone"))
        self.assertEqual(parsed.utc, dt.datetime(2026, 7, 1, 10, 0, tzinfo=dt.timezone.utc))
        gap = parse_created_at("2026-03-29T02:30:00", berlin)
        self.assertEqual((gap.status, gap.utc), (NONEXISTENT, None))
        fold = parse_created_at("2026-10-25T02:30:00", berlin)
        self.assertEqual((fold.status, fold.utc), (AMBIGUOUS, None))
        explicit = parse_created_at("2026-10-25T02:30:00+01:00", berlin)
        self.assertEqual((explicit.status, explicit.offset_source), (PARSED, "explicit"))
        date_only = parse_created_at("2026-09-28", utc)
        self.assertEqual((date_only.precision, date_only.utc), ("date", None))
        declared = parse_created_at("Mon, Sep 28, 2026 14:05", utc, "%a, %b %d, %Y %H:%M")
        self.assertEqual(declared.utc, dt.datetime(2026, 9, 28, 14, 5, tzinfo=dt.timezone.utc))
        self.assertEqual(parse_created_at("28/09/2026", utc).status, "unparsed")
        self.assertEqual(parse_created_at("  ", utc).status, "blank")


class ImportContractTest(_TempDir):
    def test_turkish_export_with_turkish_numbers(self) -> None:
        path = self.export([post("1000000000000000001", impressions="12.345", likes="1.200",
                                 replies=3, reposts=0)], lang="tr")
        record, snapshots = read_export(path.read_bytes(), path.name,
                                        settings(language="tr", number_locale="tr"))
        self.assertEqual(record["header"]["original"], list(TR_HEADERS))
        self.assertEqual(record["header"]["alias_table"], "x-post-v1/tr/v1")
        self.assertEqual(record["header"]["missing_fields"], [])
        (snapshot,) = snapshots
        self.assertEqual(snapshot.cells["impressions"].value, 12345)
        self.assertEqual(snapshot.cells["likes"].value, 1200)
        self.assertEqual(snapshot.cells["reposts"].to_dict(), {"status": OBSERVED, "value": 0})
        self.assertEqual(snapshot.cells["bookmarks"].status, BLANK)

    def test_same_bytes_under_the_wrong_locale_are_rejected(self) -> None:
        path = self.export([post("1", impressions="1.234", likes=1, replies=0, reposts=0)])
        with self.assertRaisesRegex(ExportRejected, r"'1\.234' is not a whole non-negative count"):
            import_file(path, self.tmp / "ds", settings(number_locale="en"))
        self.assertFalse((self.tmp / "ds").exists())
        summary = import_file(path, self.tmp / "ds", settings(number_locale="tr",
                                                             number_locale_source="declared"))
        self.assertEqual(summary["import"]["settings"]["number_locale_source"], "declared")
        (snapshot,) = load_dataset(self.tmp / "ds").snapshots
        self.assertEqual(snapshot.cells["impressions"].value, 1234)

    def test_missing_column_blank_cell_and_zero_are_distinct(self) -> None:
        fields = [f for f in CANONICAL if f != "replies"]
        path = self.export([post("5", impressions=100, likes="", reposts=0)], fields=fields)
        record, (snapshot,) = read_export(path.read_bytes(), path.name, settings())
        self.assertEqual(record["header"]["missing_fields"], ["replies"])
        self.assertEqual(snapshot.cells["replies"].status, MISSING_COLUMN)
        self.assertEqual(snapshot.cells["likes"].status, BLANK)
        self.assertEqual(snapshot.cells["reposts"].to_dict(), {"status": OBSERVED, "value": 0})
        self.assertEqual(record["blank_cells"]["likes"], 1)

    def test_post_ids_are_never_numbers(self) -> None:
        path = self.export([post("0000000000000000042", impressions=1)])
        _, (snapshot,) = read_export(path.read_bytes(), path.name, settings())
        self.assertEqual(snapshot.post_id, "0000000000000000042")
        rounded = self.export([post("1.97E+18", impressions=1)], name="rounded.csv")
        with self.assertRaisesRegex(ExportRejected, "scientific notation"):
            read_export(rounded.read_bytes(), rounded.name, settings())

    def test_wrong_language_and_account_grain_exports_are_rejected(self) -> None:
        path = self.export([post("1", impressions=1)], lang="tr")
        with self.assertRaisesRegex(ExportRejected, "try --lang tr"):
            read_export(path.read_bytes(), path.name, settings(language="en"))
        daily = self.tmp / "daily.csv"
        daily.write_text("Date,Impressions,Unfollows\n2026-09-01,100,2\n", "utf-8")
        with self.assertRaisesRegex(ExportRejected, "post-grain schema; daily account-level"):
            read_export(daily.read_bytes(), daily.name, settings())

    def test_rows_and_encoding_are_checked(self) -> None:
        bom = self.tmp / "bom.csv"
        bom.write_bytes(b"\xef\xbb\xbf" + self.export([post("1", impressions=1)]).read_bytes())
        _, snapshots = read_export(bom.read_bytes(), bom.name, settings())
        self.assertEqual(len(snapshots), 1)
        latin = self.tmp / "latin.csv"
        latin.write_bytes("Post id,Impressions\n1,caf\xe9\n".encode("latin-1"))
        with self.assertRaisesRegex(ExportRejected, "not UTF-8"):
            read_export(latin.read_bytes(), latin.name, settings())
        short = self.tmp / "short.csv"
        short.write_text("Post id,Impressions,Likes\n1,2\n", "utf-8")
        with self.assertRaisesRegex(ExportRejected, "2 fields, but the header has 3"):
            read_export(short.read_bytes(), short.name, settings())
        empty = self.tmp / "empty.csv"
        empty.write_text("Post id,Impressions\n\n", "utf-8")
        with self.assertRaisesRegex(ExportRejected, "no post rows"):
            read_export(empty.read_bytes(), empty.name, settings())

    def test_duplicate_rows_within_one_export(self) -> None:
        row = post("7", impressions=10, likes=1)
        path = self.export([row, row])
        record, snapshots = read_export(path.read_bytes(), path.name, settings())
        self.assertEqual((len(snapshots), record["identical_duplicate_rows"]), (1, 1))
        clash = self.export([row, post("7", impressions=11, likes=1)], name="clash.csv")
        with self.assertRaisesRegex(ExportRejected, "appears again with different values"):
            read_export(clash.read_bytes(), clash.name, settings())

    def test_publication_after_capture_is_rejected(self) -> None:
        path = self.export([post("1", created="2026-10-01T00:00:00Z", impressions=1)])
        with self.assertRaisesRegex(ExportRejected, "after the declared capture time"):
            read_export(path.read_bytes(), path.name, settings())

    def test_daylight_saving_issues_are_recorded_not_resolved(self) -> None:
        path = self.export([post("1", created="2026-03-29 02:30", impressions=1),
                            post("2", created="2026-10-25 02:30", impressions=1),
                            post("3", created="2026-09-01 10:00", impressions=1)])
        record, snapshots = read_export(path.read_bytes(), path.name,
                                        settings(source_timezone="Europe/Berlin"))
        self.assertEqual(record["created_at_issues"],
                         {"ambiguous-local-time": 1, "nonexistent-local-time": 1})
        by_id = {s.post_id: s.created_at for s in snapshots}
        self.assertEqual(by_id["1"].raw, "2026-03-29 02:30")
        self.assertIsNone(by_id["2"].utc)
        self.assertEqual(by_id["3"].utc, dt.datetime(2026, 9, 1, 8, tzinfo=dt.timezone.utc))

    def test_text_and_links_are_not_stored(self) -> None:
        rows = [post("11", impressions=1),
                {**post("12", impressions=1),
                 "url": "https://example.invalid/someone/status/99"}]
        path = self.export(rows)
        summary = import_file(path, self.tmp / "ds", settings())
        stored = b"".join((self.tmp / "ds" / name).read_bytes()
                          for name in (DATASET_FILE, SNAPSHOTS_FILE))
        self.assertNotIn(b"synthetic post", stored)
        self.assertNotIn(b"example.invalid", stored)
        self.assertEqual(summary["import"]["links"], {"consistent": 1, "inconsistent": 1})
        snapshot = load_dataset(self.tmp / "ds").snapshots[0]
        self.assertEqual(snapshot.text_sha256,
                         hashlib.sha256(b"synthetic post 11").hexdigest())


class ZoneDatabaseTest(unittest.TestCase):
    """UTC needs no time zone database; any other zone says when the database is missing."""

    def no_database(self) -> None:
        import sys
        import zoneinfo

        previous = list(zoneinfo.TZPATH)
        zoneinfo.reset_tzpath(to=[])
        zoneinfo.ZoneInfo.clear_cache()
        hidden = {name: None for name in (*sys.modules, "tzdata") if name.split(".")[0] == "tzdata"}
        patched = mock.patch.dict(sys.modules, hidden)  # None: "import tzdata..." fails
        patched.start()

        def restore() -> None:
            patched.stop()
            zoneinfo.reset_tzpath(to=previous)
            zoneinfo.ZoneInfo.clear_cache()

        self.addCleanup(restore)

    def test_utc_works_without_a_database(self) -> None:
        self.no_database()
        for name in ("UTC", "Etc/UTC"):
            with self.subTest(name=name):
                zone = load_zone(name)
                parsed = parse_created_at("2026-09-28T14:05:00", zone)
                self.assertEqual(parsed.utc, dt.datetime(2026, 9, 28, 14, 5, tzinfo=dt.timezone.utc))
                self.assertEqual(parsed.status, PARSED)

    def test_another_zone_names_the_missing_database(self) -> None:
        self.no_database()
        with self.assertRaises(InvalidInput) as raised:
            load_zone("Europe/Berlin")
        self.assertIn("no IANA time zone database found (install the tzdata package", str(raised.exception))
        self.assertNotIn("unknown IANA time zone", str(raised.exception))

    def test_an_unknown_name_with_a_database_is_still_called_unknown(self) -> None:
        with self.assertRaisesRegex(InvalidInput, "unknown IANA time zone 'Mars/Olympus'"):
            load_zone("Mars/Olympus")
        self.assertEqual(str(load_zone("Europe/Berlin")), "Europe/Berlin")


class DatasetTest(_TempDir):
    def test_files_stay_private_whatever_the_umask(self) -> None:
        """Dataset files do not take the ordinary umask-based mode of ``atomic_write``."""
        previous = os.umask(0)
        self.addCleanup(os.umask, previous)
        path = self.export([post("1", impressions=10, likes=1, replies=0, reposts=0)])
        import_file(path, self.tmp / "ds0", settings())
        root = self.tmp / "ds0"
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        for name in (DATASET_FILE, SNAPSHOTS_FILE):
            self.assertEqual(stat.S_IMODE((root / name).stat().st_mode), 0o600)

    def test_files_are_private_and_integrity_checked(self) -> None:
        path = self.export([post("1", impressions=10, likes=1, replies=0, reposts=0)])
        import_file(path, self.tmp / "ds", settings())
        root = self.tmp / "ds"
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        for name in (DATASET_FILE, SNAPSHOTS_FILE):
            self.assertEqual(stat.S_IMODE((root / name).stat().st_mode), 0o600)
        meta = json.loads((root / DATASET_FILE).read_text("utf-8"))
        self.assertEqual(meta["schema"]["grain"], "post")
        self.assertEqual(meta["schema"]["canonical_fields"], list(CANONICAL))
        (root / SNAPSHOTS_FILE).write_bytes((root / SNAPSHOTS_FILE).read_bytes()
                                            .replace(b'"value":10', b'"value":11'))
        with self.assertRaises(IntegrityError):
            load_dataset(root)

    def test_reimport_is_a_no_op(self) -> None:
        path = self.export([post("1", impressions=10)])
        first = import_file(path, self.tmp / "ds", settings())
        before = {n: (self.tmp / "ds" / n).read_bytes() for n in (DATASET_FILE, SNAPSHOTS_FILE)}
        again = import_file(path, self.tmp / "ds", settings())
        self.assertFalse(first["already_imported"])
        self.assertTrue(again["already_imported"])
        self.assertEqual(before, {n: (self.tmp / "ds" / n).read_bytes() for n in before})

    def test_cumulative_snapshots_are_kept_apart_never_summed(self) -> None:
        early = self.export([post("1", impressions=1000, likes=100, replies=1, reposts=1)],
                            name="early.csv")
        late = self.export([post("1", impressions=4000, likes=150, replies=2, reposts=1)],
                           name="late.csv")
        import_file(early, self.tmp / "ds", settings(captured_at=dt.datetime(
            2026, 9, 28, 12, tzinfo=dt.timezone.utc)))
        import_file(late, self.tmp / "ds", settings())
        dataset = load_dataset(self.tmp / "ds")
        self.assertEqual(len(dataset.snapshots), 2)
        self.assertEqual([s.cells["likes"].value for s in dataset.snapshots], [100, 150])
        self.assertEqual(dataset.meta["post_count"], 1)

    def test_conflicting_snapshot_is_refused(self) -> None:
        a = self.export([post("1", impressions=10)], name="a.csv")
        b = self.export([post("1", impressions=12)], name="b.csv")
        import_file(a, self.tmp / "ds", settings())
        before = (self.tmp / "ds" / SNAPSHOTS_FILE).read_bytes()
        with self.assertRaisesRegex(DatasetError, "never summed or overwritten"):
            import_file(b, self.tmp / "ds", settings())
        self.assertEqual((self.tmp / "ds" / SNAPSHOTS_FILE).read_bytes(), before)

    def test_same_snapshot_from_another_file_is_counted_as_present(self) -> None:
        a = self.export([post("1", impressions=10)], name="a.csv")
        b = self.export([post("1", impressions=10), post("2", impressions=3)], name="b.csv")
        import_file(a, self.tmp / "ds", settings())
        summary = import_file(b, self.tmp / "ds", settings())
        self.assertEqual((summary["snapshots_added"], summary["snapshots_already_present"]),
                         (1, 1))

    def test_count_decreases_are_reported_not_clipped(self) -> None:
        a = self.export([post("1", impressions=500, likes=9)], name="a.csv")
        b = self.export([post("1", impressions=480, likes=9)], name="b.csv")
        import_file(a, self.tmp / "ds", settings(captured_at=dt.datetime(
            2026, 9, 29, tzinfo=dt.timezone.utc)))
        import_file(b, self.tmp / "ds", settings())
        dataset = load_dataset(self.tmp / "ds")
        (decrease,) = count_decreases(dataset.snapshots, "combined")
        self.assertEqual((decrease["field"], decrease["earlier"]["value"],
                          decrease["later"]["value"]), ("impressions", 500, 480))
        self.assertEqual(dataset.snapshots[-1].cells["impressions"].value, 480)

    def test_output_directory_rules(self) -> None:
        path = self.export([post("1", impressions=1)])
        busy = self.tmp / "busy"
        busy.mkdir()
        (busy / "other.txt").write_text("x", "utf-8")
        with self.assertRaisesRegex(DatasetError, "not an analytics dataset"):
            import_file(path, busy, settings())
        with self.assertRaisesRegex(DatasetError, "parent directory"):
            import_file(path, self.tmp / "missing" / "ds", settings())
        with self.assertRaisesRegex(DatasetError, "not an analytics dataset"):
            load_dataset(busy)

    def test_settings_are_recorded(self) -> None:
        path = self.export([post("1", impressions=1)])
        chosen = settings(scope="organic", counts="window", source_timezone="Asia/Tokyo",
                          date_format=None)
        summary = import_file(path, self.tmp / "ds", chosen)
        recorded = summary["import"]["settings"]
        self.assertEqual(recorded["captured_at"], "2026-09-30T12:00:00Z")
        self.assertEqual({k: recorded[k] for k in ("scope", "counts", "source_timezone")},
                         {"scope": "organic", "counts": "window",
                          "source_timezone": "Asia/Tokyo"})
        self.assertEqual(sorted(recorded), sorted(
            f.name if f.name != "language" else "header_language"
            for f in dataclasses.fields(chosen)))
        self.assertEqual(summary["import"]["source"]["file_name"], "export.csv")
        self.assertNotIn(os.sep, summary["import"]["source"]["file_name"])


if __name__ == "__main__":
    unittest.main()
