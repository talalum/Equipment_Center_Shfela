"""Parsing the issuance email — against the real format."""
from __future__ import annotations

import unittest

from tests.base import SAMPLE_EMAIL

from app.parsing.issuance_parser import center_matches, parse


class ParseRealEmail(unittest.TestCase):
    def setUp(self) -> None:
        self.parsed = parse(SAMPLE_EMAIL)

    def test_no_errors(self) -> None:
        self.assertEqual(self.parsed.errors, [])
        self.assertTrue(self.parsed.ok)

    def test_header_fields(self) -> None:
        self.assertEqual(self.parsed.recipient, "עם הנצח")
        self.assertEqual(self.parsed.issuer, "יואב חמאם")
        self.assertEqual(self.parsed.center, "מרכז ציוד שפלה")
        self.assertTrue(center_matches(self.parsed))

    def test_all_seven_lines(self) -> None:
        self.assertEqual(
            [line.normalized_sku for line in self.parsed.lines],
            ["1111", "3702", "1102", "1108", "1103", "1104", "1105"],
        )
        self.assertEqual([line.qty for line in self.parsed.lines], [2, 1, 1, 1, 1, 5, 5])

    def test_raw_names_kept_for_the_record(self) -> None:
        # The name from the email is kept verbatim — it differs from the name in
        # the file on three of the lines.
        names = [line.raw_name for line in self.parsed.lines]
        self.assertIn("פלסטרים", names)
        self.assertIn("גליל אגד 5 ס\"מ", names)
        self.assertIn("לויקופלסט / דבק", names)


class ParseEdgeCases(unittest.TestCase):
    def test_other_center_still_parses(self) -> None:
        parsed = parse(SAMPLE_EMAIL.replace("מרכז ציוד שפלה", "מרכז ציוד ירושלים"))
        self.assertEqual(parsed.errors, [])
        self.assertFalse(center_matches(parsed))

    def test_unrecognised_line_is_an_error(self) -> None:
        parsed = parse(SAMPLE_EMAIL.replace("פלסטרים, מקט: 1102 - כמות: 1", "שורה חופשית כלשהי"))
        self.assertTrue(any("שורה לא מזוהה" in e for e in parsed.errors))
        self.assertFalse(parsed.ok)

    def test_missing_anchor_is_an_error(self) -> None:
        parsed = parse(SAMPLE_EMAIL.replace("המוצרים שהונפקו:", ""))
        self.assertTrue(any("לא נמצא העוגן" in e for e in parsed.errors))

    def test_truncated_email_is_an_error(self) -> None:
        cut = SAMPLE_EMAIL.split("שם המנפיק:")[0]
        parsed = parse(cut)
        self.assertTrue(any("לא נמצא העוגן" in e for e in parsed.errors))

    def test_empty_body(self) -> None:
        self.assertTrue(parse("   ").errors)

    def test_zero_quantity_rejected(self) -> None:
        parsed = parse(SAMPLE_EMAIL.replace("תחבושת אישית, מקט: 1111 - כמות: 2", "תחבושת אישית, מקט: 1111 - כמות: 0"))
        self.assertTrue(any("כמות לא חוקית" in e for e in parsed.errors))

    def test_same_sku_twice_is_merged(self) -> None:
        doubled = SAMPLE_EMAIL.replace(
            "תחבושת אישית, מקט: 1111 - כמות: 2",
            "תחבושת אישית, מקט: 1111 - כמות: 2\n\nתחבושת אישית, מקט: 1111 - כמות: 3",
        )
        parsed = parse(doubled)
        self.assertEqual(parsed.errors, [])
        matching = [line for line in parsed.lines if line.normalized_sku == "1111"]
        self.assertEqual(len(matching), 1, "the same SKU twice must merge into a single line")
        self.assertEqual(matching[0].qty, 5, "the quantities must be summed and not lost")

    def test_tolerates_quote_and_dash_variants(self) -> None:
        variant = SAMPLE_EMAIL.replace(
            'תחבושת אישית, מקט: 1111 - כמות: 2', 'תחבושת אישית, מק"ט: 1111 – כמות: 2'
        )
        parsed = parse(variant)
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.lines[0].qty, 2)


if __name__ == "__main__":
    unittest.main()
