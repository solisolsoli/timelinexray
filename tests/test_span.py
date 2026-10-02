"""The byte-to-line contract of timelinexray.span, tested without git."""

from __future__ import annotations

import hashlib
import random
import unittest

from timelinexray.errors import InvalidInput, SpanRangeError
from timelinexray.span import (
    CONFIRMING,
    FOUND_MULTIPLE,
    FOUND,
    MISSING,
    LineMap,
    find_occurrences,
    parse_line_range,
    read_span,
)
from tests.support import oracle_lines

EDGE_BLOBS: dict[str, bytes] = {
    "empty": b"",
    "one line, no newline": b"abc",
    "only newline": b"\n",
    "lf": b"a\nb\nc\n",
    "lf, no final newline": b"a\nb\nc",
    "crlf": b"alpha\r\nbeta\r\ngamma\r\n",
    "crlf, no final newline": b"object A\r\nobject B",
    "mixed endings": b"fn a() {}\r\nfn b() {}\nfn c() {}\r\n",
    "lone cr": b"one\rtwo\nthree\n",
    "trailing cr": b"one\r",
    "cr before crlf": b"x\r\r\ny\n",
    "blank lines": b"\n\n\nx = 1\n\n",
    "bom": b"\xef\xbb\xbfprint('bom')\r\n",
    "non-utf8": b"caf\xe9\nna\xefve\n",
    "nul bytes": b"\x00\n\x00\x00",
}


def _span(blob: bytes, start: int, end: int, anchor: str | None = None):
    return read_span(blob, start, end, commit="c" * 40, path="f.txt", blob_oid="b" * 40, anchor=anchor)


class LineMapTest(unittest.TestCase):
    def test_line_count_formula(self) -> None:
        for name, blob in EDGE_BLOBS.items():
            with self.subTest(name):
                expected = blob.count(b"\n") + (1 if blob and not blob.endswith(b"\n") else 0)
                self.assertEqual(LineMap.of(blob).line_count, expected)
                self.assertEqual(LineMap.of(blob).line_count, len(oracle_lines(blob)))

    def test_known_starts(self) -> None:
        self.assertEqual(LineMap.of(b"").starts, ())
        self.assertEqual(LineMap.of(b"\n").starts, (0,))
        self.assertEqual(LineMap.of(b"a\r\nb").starts, (0, 3))
        self.assertEqual(LineMap.of(b"one\rtwo\nthree\n").starts, (0, 8))

    def test_line_of_offset(self) -> None:
        lines = LineMap.of(b"ab\ncd\n")
        self.assertEqual([lines.line_of_offset(i) for i in range(6)], [1, 1, 1, 2, 2, 2])


class LineMapPropertyTest(unittest.TestCase):
    """Seeded property test (audit section 6, item 12): on random blobs built from line
    terminators, lone CRs, NULs, invalid UTF-8 and a BOM, every ``LineMap`` answer equals
    the independent regular-expression oracle. A failure names the seed and the case."""

    SEED = 20261001
    CASES = 400
    PIECES = (b"\n", b"\n", b"\r\n", b"\r", b"a", b"bc", b" ", b"\x00", b"\xff",
              b"\xef\xbb\xbf", b"\xc3\xa9", b"\n\n")

    def blobs(self):
        rng = random.Random(self.SEED)
        for case in range(self.CASES):
            blob = b"".join(rng.choice(self.PIECES) for _ in range(rng.randrange(0, 40)))
            yield case, rng, blob

    def test_line_map_equals_the_oracle(self) -> None:
        for case, rng, blob in self.blobs():
            with self.subTest(seed=self.SEED, case=case, blob=blob):
                lines = oracle_lines(blob)
                lmap = LineMap.of(blob)
                starts, offset = [], 0
                for line in lines:
                    starts.append(offset)
                    offset += len(line)
                self.assertEqual(offset, len(blob))
                self.assertEqual((lmap.starts, lmap.line_count, lmap.size),
                                 (tuple(starts), len(lines), len(blob)))
                for number, line in enumerate(lines, 1):
                    self.assertEqual(blob[lmap.starts[number - 1]:lmap.line_end(number)], line)
                owner = [number for number, line in enumerate(lines, 1) for _ in line]
                self.assertEqual([lmap.line_of_offset(o) for o in range(len(blob))], owner)
                count = len(lines)
                for _ in range(6):
                    start = rng.randrange(1, count + 3)
                    end = rng.randrange(1, count + 3)
                    if 1 <= start <= end <= count:
                        first, last = lmap.byte_range(start, end)
                        self.assertEqual(blob[first:last], b"".join(lines[start - 1:end]))
                        span = read_span(blob, start, end, commit="c" * 40, path="f",
                                         blob_oid="b" * 40)
                        self.assertEqual(span.sha256,
                                         hashlib.sha256(blob[first:last]).hexdigest())
                    else:
                        with self.assertRaises(SpanRangeError):
                            lmap.byte_range(start, end)
                with self.assertRaises(SpanRangeError):
                    lmap.byte_range(0, max(count, 1))

    def test_find_occurrences_equals_a_naive_scan(self) -> None:
        for case, rng, blob in self.blobs():
            if not blob:
                continue
            start = rng.randrange(len(blob))
            needle = blob[start:start + rng.randrange(1, 4)]
            with self.subTest(seed=self.SEED, case=case, blob=blob, needle=needle):
                self.assertEqual(find_occurrences(blob, needle),
                                 [i for i in range(len(blob)) if blob.startswith(needle, i)])


class ReadSpanExhaustiveTest(unittest.TestCase):
    def test_every_range_matches_the_oracle(self) -> None:
        for name, blob in EDGE_BLOBS.items():
            lines = oracle_lines(blob)
            count = len(lines)
            for start in range(1, count + 1):
                for end in range(start, count + 1):
                    with self.subTest(name, start=start, end=end):
                        span = _span(blob, start, end)
                        expected = b"".join(lines[start - 1 : end])
                        self.assertEqual(span.data, expected)
                        self.assertEqual(blob[span.start_byte : span.end_byte], expected)
                        self.assertEqual(span.sha256, hashlib.sha256(expected).hexdigest())
                        self.assertEqual(span.blob_sha256, hashlib.sha256(blob).hexdigest())
                        self.assertEqual(span.line_count, count)

    def test_adjacent_spans_concatenate_to_the_blob(self) -> None:
        for name, blob in EDGE_BLOBS.items():
            count = LineMap.of(blob).line_count
            for k in range(1, count):
                with self.subTest(name, k=k):
                    head = _span(blob, 1, k).data
                    tail = _span(blob, k + 1, count).data
                    self.assertEqual(head + tail, blob)
            if count:
                self.assertEqual(_span(blob, 1, count).data, blob)

    def test_line_endings_are_preserved_and_counted(self) -> None:
        span = _span(EDGE_BLOBS["crlf"], 1, 3)
        self.assertEqual(span.data, b"alpha\r\nbeta\r\ngamma\r\n")
        self.assertEqual((span.lf_lines, span.crlf_lines, span.unterminated_lines), (0, 3, 0))
        span = _span(EDGE_BLOBS["mixed endings"], 1, 3)
        self.assertEqual((span.lf_lines, span.crlf_lines, span.unterminated_lines), (1, 2, 0))
        span = _span(EDGE_BLOBS["crlf, no final newline"], 2, 2)
        self.assertEqual(span.data, b"object B")
        self.assertEqual((span.lf_lines, span.crlf_lines, span.unterminated_lines), (0, 0, 1))
        span = _span(EDGE_BLOBS["lone cr"], 1, 1)
        self.assertEqual(span.data, b"one\rtwo\n")  # a lone CR does not end a line

    def test_bom_stays_in_line_one(self) -> None:
        self.assertTrue(_span(EDGE_BLOBS["bom"], 1, 1).data.startswith(b"\xef\xbb\xbf"))


class RangeErrorTest(unittest.TestCase):
    def test_range_past_end_of_file(self) -> None:
        with self.assertRaises(SpanRangeError) as ctx:
            _span(b"a\nb\nc\n", 2, 4)
        self.assertEqual(
            str(ctx.exception), "line range 2-4 is past end of file: f.txt has 3 lines"
        )
        self.assertEqual(ctx.exception.exit_code, 1)

    def test_range_past_end_of_one_line_file(self) -> None:
        with self.assertRaisesRegex(SpanRangeError, r"f\.txt has 1 line$"):
            _span(b"abc", 2, 2)

    def test_empty_file_has_no_lines(self) -> None:
        with self.assertRaises(SpanRangeError) as ctx:
            _span(b"", 1, 1)
        self.assertEqual(
            str(ctx.exception), "line range 1-1 is past end of file: f.txt is empty (0 lines)"
        )

    def test_start_must_not_follow_end(self) -> None:
        with self.assertRaisesRegex(SpanRangeError, "start line is after end line"):
            _span(b"a\nb\n", 2, 1)

    def test_lines_start_at_one(self) -> None:
        with self.assertRaisesRegex(SpanRangeError, "line numbers start at 1"):
            _span(b"a\n", 0, 1)

    def test_no_final_newline_last_line_is_readable(self) -> None:
        self.assertEqual(_span(b"a\nb", 2, 2).data, b"b")
        with self.assertRaisesRegex(SpanRangeError, "has 2 lines"):
            _span(b"a\nb", 3, 3)

    def test_parse_line_range(self) -> None:
        self.assertEqual(parse_line_range("5-9"), (5, 9))
        self.assertEqual(parse_line_range("7"), (7, 7))
        self.assertEqual(parse_line_range("0-3"), (0, 3))  # rejected later, with a range error
        for bad in ("", "a-b", "5-", "-5", "5--9", "5:9", "1.5", "5 - 9"):
            with self.subTest(bad):
                with self.assertRaises(InvalidInput) as ctx:
                    parse_line_range(bad)
                self.assertEqual(ctx.exception.exit_code, 2)


class AnchorTest(unittest.TestCase):
    BLOB = b"WEIGHT = 0.5\nWEIGHT = 0.5\n\ndef score(x):\n    return x * WEIGHT\n"

    def test_found_once(self) -> None:
        check = _span(self.BLOB, 4, 5, anchor="def score").anchor
        self.assertEqual((check.verdict, check.lines, check.count), (FOUND, (4,), 1))

    def test_several_occurrences_are_all_reported_and_confirm_the_span(self) -> None:
        check = _span(self.BLOB, 1, 5, anchor="WEIGHT").anchor
        self.assertEqual(check.verdict, FOUND_MULTIPLE)
        self.assertEqual(check.lines, (1, 2, 5))  # every occurrence; none is picked
        self.assertEqual(check.count, 3)
        self.assertIn(FOUND_MULTIPLE, CONFIRMING)
        self.assertIn(FOUND, CONFIRMING)
        self.assertNotIn(MISSING, CONFIRMING)

    def test_search_is_restricted_to_the_span(self) -> None:
        check = _span(self.BLOB, 3, 4, anchor="WEIGHT").anchor
        self.assertEqual((check.verdict, check.lines), (MISSING, ()))
        self.assertEqual(_span(self.BLOB, 2, 2, anchor="WEIGHT").anchor.verdict, FOUND)

    def test_occurrence_crossing_the_span_edge_is_not_seen(self) -> None:
        blob = b"abc\ndef\n"
        self.assertEqual(_span(blob, 1, 1, anchor="c\nd").anchor.verdict, MISSING)
        self.assertEqual(_span(blob, 1, 2, anchor="c\nd").anchor.verdict, FOUND)

    def test_exact_bytes_no_normalisation(self) -> None:
        blob = b"alpha\r\nbeta\r\n"
        self.assertEqual(_span(blob, 1, 2, anchor="alpha\nbeta").anchor.verdict, MISSING)
        self.assertEqual(_span(blob, 1, 2, anchor="alpha\r\nbeta").anchor.verdict, FOUND)
        self.assertEqual(_span(blob, 1, 2, anchor="ALPHA").anchor.verdict, MISSING)

    def test_overlapping_occurrences_count(self) -> None:
        self.assertEqual(find_occurrences(b"aaa", b"aa"), [0, 1])
        self.assertEqual(_span(b"aaa\n", 1, 1, anchor="aa").anchor.verdict, FOUND_MULTIPLE)

    def test_utf8_anchor(self) -> None:
        blob = "naïve — café\n".encode("utf-8")
        check = _span(blob, 1, 1, anchor="café").anchor
        self.assertEqual((check.verdict, check.byte_offsets), (FOUND, (blob.index(b"caf"),)))

    def test_empty_anchor_is_invalid(self) -> None:
        with self.assertRaises(InvalidInput):
            _span(b"a\n", 1, 1, anchor="")


class SerialisationTest(unittest.TestCase):
    def test_utf8_span_round_trips_through_text(self) -> None:
        data = _span(EDGE_BLOBS["crlf"], 1, 2).to_dict()
        self.assertEqual(data["text"].encode("utf-8"), b"alpha\r\nbeta\r\n")
        self.assertNotIn("base64", data)
        self.assertEqual(data["line_terminators"], {"lf": 0, "crlf": 2, "none": 0})

    def test_non_utf8_span_uses_base64(self) -> None:
        import base64

        data = _span(EDGE_BLOBS["non-utf8"], 1, 2).to_dict()
        self.assertIsNone(data["text"])
        self.assertIsNone(data["encoding"])
        self.assertEqual(base64.b64decode(data["base64"]), EDGE_BLOBS["non-utf8"])


if __name__ == "__main__":
    unittest.main()
