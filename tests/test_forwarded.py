"""
מייל הנפקה אמיתי שהועבר דרך ג'ימייל.

המייל הזה נכשל בגרסה הראשונה משלוש סיבות שהתגלו רק מול דואר אמיתי:
  1. ג'ימייל מציג מודגשים בטקסט כ-*כוכביות*, וזה שבר את כל העוגנים.
  2. גוף המייל עטוף בשתי כותרות העברה.
  3. באג שלי: המייל לא נפרס, ולכן "מרכז ציוד" יצא ריק, והקוד סימן אותו
     כהנפקה של מרכז אחר — כלומר בלע אותו בשקט עם הודעה מטעה.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from tests.base import SAMPLE_EMAIL, DBTestCase

from app import ingest, inventory, repo
from app.parsing.issuance_parser import center_matches, parse

FORWARDED = (Path(__file__).resolve().parent / "fixtures" / "emails" / "forwarded_with_bold.txt").read_text(
    encoding="utf-8"
)


class ParsesTheRealForwardedEmail(unittest.TestCase):
    def setUp(self) -> None:
        self.parsed = parse(FORWARDED)

    def test_no_errors(self) -> None:
        self.assertEqual(self.parsed.errors, [])
        self.assertTrue(self.parsed.ok)

    def test_center_is_read_through_the_bold_markers(self) -> None:
        """השורה במקור: '*מרכז ציוד: *מרכז ציוד שפלה'"""
        self.assertEqual(self.parsed.center, "מרכז ציוד שפלה")
        self.assertTrue(center_matches(self.parsed))

    def test_issuer_and_recipient(self) -> None:
        self.assertEqual(self.parsed.issuer, "יואב חמאם")
        self.assertEqual(self.parsed.recipient, "אביהו ששון")

    def test_all_seven_lines(self) -> None:
        self.assertEqual(
            [line.normalized_sku for line in self.parsed.lines],
            ["1111", "3702", "1102", "1108", "1103", "1104", "1105"],
        )
        self.assertEqual([line.qty for line in self.parsed.lines], [2, 1, 1, 1, 1, 5, 5])

    def test_asterisks_inside_item_names_are_preserved(self) -> None:
        """הסרת ההדגשות לא תפגע ב'פד גזה סטרילי 10*10'."""
        names = [line.raw_name for line in self.parsed.lines]
        self.assertIn("פד גזה סטרילי 10*10", names)
        self.assertIn("פד גזה סטרילי 5*5", names)

    def test_forwarding_headers_do_not_become_items(self) -> None:
        self.assertEqual(len(self.parsed.lines), 7)
        for line in self.parsed.lines:
            self.assertNotIn("@", line.raw_name)


class EmphasisAndQuoting(unittest.TestCase):
    def test_bold_around_the_items_anchor(self) -> None:
        self.assertTrue(parse(SAMPLE_EMAIL.replace("המוצרים שהונפקו:", "*המוצרים שהונפקו*:")).ok)

    def test_bold_around_a_whole_item_line(self) -> None:
        parsed = parse(
            SAMPLE_EMAIL.replace(
                "תחבושת אישית, מקט: 1111 - כמות: 2", "*תחבושת אישית, מקט: 1111 - כמות: 2*"
            )
        )
        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.lines[0].qty, 2)

    def test_outlook_style_quote_prefix(self) -> None:
        quoted = "\n".join("> " + line for line in SAMPLE_EMAIL.split("\n"))
        parsed = parse(quoted)
        self.assertEqual(parsed.errors, [])
        self.assertEqual(len(parsed.lines), 7)

    def test_quantity_with_asterisk_in_name_still_parses(self) -> None:
        parsed = parse(SAMPLE_EMAIL)
        by_sku = {line.normalized_sku: line for line in parsed.lines}
        self.assertEqual(by_sku["1104"].raw_name, "פד גזה סטרילי 10*10")


class IngestTheForwardedEmail(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()

    def test_it_is_applied_and_updates_stock(self) -> None:
        result = ingest.ingest_issuance(FORWARDED, "fwd-1", source="email")
        self.assertEqual(result.status, ingest.APPLIED, result.message)
        shortages = {
            s.item.sku: s.shortage
            for s in inventory.status_for_all(repo.list_items())
            if s.in_shortage
        }
        self.assertEqual(
            shortages, {"1111": 2, "3702": 1, "1102": 1, "1108": 1, "1103": 1, "1104": 5, "1105": 5}
        )

    def test_recipient_and_issuer_are_recorded(self) -> None:
        result = ingest.ingest_issuance(FORWARDED, "fwd-2", source="email")
        issuance = repo.get_issuance(result.issuance_id)
        self.assertEqual(issuance.issuer, "יואב חמאם")
        self.assertEqual(issuance.recipient, "אביהו ששון")


class UnparsableEmailIsNeverSilentlyIgnored(DBTestCase):
    """הרגרסיה של הבאג: מייל שנכשל בפירוק חייב להגיע לביקורת, לא להיעלם."""

    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()

    def test_issuance_without_a_centre_line_goes_to_review(self) -> None:
        without_centre = SAMPLE_EMAIL.replace("מרכז ציוד: מרכז ציוד שפלה", "")
        result = ingest.ingest_issuance(without_centre, "no-centre", source="email")
        self.assertEqual(result.status, ingest.NEEDS_REVIEW)
        self.assertIn("מרכז ציוד", repo.get_issuance(result.issuance_id).review_note)

    def test_broken_line_and_missing_centre_still_goes_to_review(self) -> None:
        broken = SAMPLE_EMAIL.replace("מרכז ציוד: מרכז ציוד שפלה", "").replace(
            "פלסטרים, מקט: 1102 - כמות: 1", "טקסט חופשי"
        )
        result = ingest.ingest_issuance(broken, "broken-no-centre", source="email")
        self.assertEqual(result.status, ingest.NEEDS_REVIEW)

    def test_a_named_other_centre_is_still_ignored(self) -> None:
        other = SAMPLE_EMAIL.replace("מרכז ציוד שפלה", "מרכז ציוד ירושלים")
        result = ingest.ingest_issuance(other, "other-centre", source="email")
        self.assertEqual(result.status, ingest.IGNORED)
        self.assertIn("ירושלים", repo.get_issuance(result.issuance_id).review_note)

    def test_non_issuance_email_is_ignored_quietly(self) -> None:
        result = ingest.ingest_issuance("שלום, זהו ניוזלטר שיווקי.", "newsletter", source="email")
        self.assertEqual(result.status, ingest.IGNORED)
        self.assertIn("אינו נראה", repo.get_issuance(result.issuance_id).review_note)

    def test_none_of_these_touch_the_stock(self) -> None:
        for index, body in enumerate(
            [
                SAMPLE_EMAIL.replace("מרכז ציוד: מרכז ציוד שפלה", ""),
                SAMPLE_EMAIL.replace("מרכז ציוד שפלה", "מרכז ציוד ירושלים"),
                "ניוזלטר",
            ]
        ):
            ingest.ingest_issuance(body, f"m-{index}", source="email")
        in_shortage = [s for s in inventory.status_for_all(repo.list_items()) if s.in_shortage]
        self.assertEqual(in_shortage, [])


if __name__ == "__main__":
    unittest.main()
