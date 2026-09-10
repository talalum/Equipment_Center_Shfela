"""
An issuance to an organizational unit — an email that arrives with no
recipient name.

Such an email is a real issuance and must be counted, but it does not look
like the familiar one: there is no name after the greeting, and the item list
does not always come through in the expected shape. Identification therefore
rests on the sentence "ההזמנה שלך מוכנה", which every issuance email contains,
and not on the greeting.

What must never happen again: an issuance email quietly marked ignored. Stock
that is short and nobody knows it is exactly what this system exists to
prevent, so an email carrying that sentence always ends up in front of a
person — applied if it parsed, waiting for review if it did not.
"""
from __future__ import annotations

import unittest

from tests.base import SAMPLE_EMAIL, DBTestCase

from app import ingest, inventory, repo
from app.parsing.issuance_parser import parse

#: The same issuance, to an organizational unit: the greeting has no name.
NO_NAME = SAMPLE_EMAIL.replace("שלום עם הנצח", "שלום")

#: An issuance whose item list could not be read — the heading is missing.
NO_ITEM_LIST = SAMPLE_EMAIL.replace("המוצרים שהונפקו:", "")

#: An ordinary email that is not an issuance at all.
NOT_AN_ISSUANCE = "שלום רב\n\nתזכורת: ישיבת צוות מחר ב-10:00.\n\nבברכה"


class ParseWithoutARecipient(unittest.TestCase):
    def test_recognised_as_an_issuance(self) -> None:
        parsed = parse(NO_NAME)
        self.assertTrue(parsed.has_marker)
        self.assertTrue(parsed.looks_like_issuance)

    def test_parses_in_full_even_with_no_name(self) -> None:
        parsed = parse(NO_NAME)
        self.assertEqual(parsed.errors, [])
        self.assertTrue(parsed.ok)
        self.assertIsNone(parsed.recipient, "there is no name — and none should be invented")
        self.assertEqual(len(parsed.lines), 7)

    def test_the_marker_alone_is_enough(self) -> None:
        """No item list, and still an issuance — it will not be dropped."""
        parsed = parse(NO_ITEM_LIST)
        self.assertFalse(parsed.has_items_section)
        self.assertTrue(parsed.looks_like_issuance)

    def test_an_ordinary_email_is_not_an_issuance(self) -> None:
        self.assertFalse(parse(NOT_AN_ISSUANCE).looks_like_issuance)


class IngestWithoutARecipient(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()

    def shortage(self, sku: str) -> int:
        item = repo.find_item_by_sku(sku)
        return inventory.status_for_item(item).shortage

    def test_a_nameless_issuance_enters_stock(self) -> None:
        result = ingest.ingest_issuance(NO_NAME, "<org-1@mail>")
        self.assertEqual(result.status, ingest.APPLIED)
        self.assertEqual(self.shortage("1111"), 2)

    def test_an_unreadable_issuance_waits_instead_of_vanishing(self) -> None:
        result = ingest.ingest_issuance(NO_ITEM_LIST, "<org-2@mail>")
        self.assertEqual(result.status, ingest.NEEDS_REVIEW)
        note = repo.get_issuance(result.issuance_id).review_note
        self.assertIn("רשימת המוצרים", note)

    def test_an_ordinary_email_is_still_ignored(self) -> None:
        result = ingest.ingest_issuance(NOT_AN_ISSUANCE, "<news-1@mail>")
        self.assertEqual(result.status, ingest.IGNORED)

    def test_two_nameless_issuances_are_not_counted_twice(self) -> None:
        """Deduplication has no name to lean on here, so the second one waits."""
        ingest.ingest_issuance(NO_NAME, "<org-1@mail>")
        second = ingest.ingest_issuance(NO_NAME, "<org-forwarded-again@mail>")
        self.assertEqual(second.status, ingest.NEEDS_REVIEW)
        self.assertEqual(self.shortage("1111"), 2)


if __name__ == "__main__":
    unittest.main()
