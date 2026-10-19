"""
An issuance to an organizational unit — an email that arrives with no
recipient name.

Such an email is a real issuance and must be counted, but it does not look
like the familiar one: there is no name after the greeting, and the item list
does not always come through in the expected shape. Identification therefore
rests on the sentence "ההזמנה ... מוכנה", which every issuance email contains,
and not on the greeting.

What must never happen again: an issuance email quietly marked ignored. Stock
that is short and nobody knows it is exactly what this system exists to
prevent, so an email carrying that sentence always ends up in front of a
person — applied if it parsed, waiting for review if it did not.
"""
from __future__ import annotations

from dataclasses import replace
import unittest
from unittest import mock

from tests.base import SAMPLE_EMAIL, DBTestCase

from app import ingest, inventory, repo
from app.parsing import issuance_parser
from app.parsing.issuance_parser import parse

#: The same issuance, to an organizational unit: the greeting has no name.
NO_NAME = SAMPLE_EMAIL.replace("שלום עם הנצח", "שלום")

#: An issuance whose item list could not be read — the heading is missing.
NO_ITEM_LIST = SAMPLE_EMAIL.replace("המוצרים שהונפקו:", "")

#: An ordinary email that is not an issuance at all.
NOT_AN_ISSUANCE = "שלום רב\n\nתזכורת: ישיבת צוות מחר ב-10:00.\n\nבברכה"

#: The organizational vehicle template has no issuer or equipment-center line.
VEHICLE = """שלום וברכה

ההזמנה על אמבולנס 92 מוכנה, הנך יכול לבוא לאסוף.

המוצרים שהונפקו:

מסכת הגנה N-95, מקט: 1604 - כמות: 10

בברכה,

אגף תפעול,

חטיבת מתנדבים ותפעול"""


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


class ParseOrganizationalVehicle(unittest.TestCase):
    def test_parses_the_real_vehicle_template(self) -> None:
        parsed = parse(VEHICLE)

        self.assertEqual(parsed.errors, [])
        self.assertTrue(parsed.ok)
        self.assertTrue(parsed.has_marker)
        self.assertTrue(parsed.is_organizational)
        self.assertEqual(parsed.unit, "אמבולנס 92")
        self.assertEqual(parsed.recipient, "אמבולנס 92")
        self.assertIsNone(parsed.center)
        self.assertEqual(
            [(line.normalized_sku, line.qty) for line in parsed.lines],
            [("1604", 10)],
        )

    def test_regular_issuance_is_not_organizational(self) -> None:
        parsed = parse(SAMPLE_EMAIL)
        self.assertFalse(parsed.is_organizational)
        self.assertIsNone(parsed.unit)

    def test_greeting_is_not_used_as_a_recipient(self) -> None:
        parsed = parse(SAMPLE_EMAIL.replace("שלום עם הנצח", "שלום וברכה"))
        self.assertIsNone(parsed.recipient)


class IngestOrganizationalVehicle(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()

    def shortage(self, sku: str) -> int:
        item = repo.find_item_by_sku(sku)
        return inventory.status_for_item(item).shortage

    def test_valid_vehicle_issuance_is_applied_without_a_center(self) -> None:
        result = ingest.ingest_issuance(VEHICLE, "<vehicle-1@mail>")

        self.assertEqual(result.status, ingest.APPLIED)
        self.assertEqual(self.shortage("1604"), 10)

    def test_different_vehicles_are_not_treated_as_duplicates(self) -> None:
        first = ingest.ingest_issuance(VEHICLE, "<vehicle-1@mail>")
        second = ingest.ingest_issuance(
            VEHICLE.replace("אמבולנס 92", "אמבולנס 93"),
            "<vehicle-2@mail>",
        )

        self.assertEqual(first.status, ingest.APPLIED)
        self.assertEqual(second.status, ingest.APPLIED)
        self.assertEqual(self.shortage("1604"), 20)

    def test_same_vehicle_and_items_still_trigger_duplicate_review(self) -> None:
        ingest.ingest_issuance(VEHICLE, "<vehicle-1@mail>")
        repeated = ingest.ingest_issuance(VEHICLE, "<forwarded-vehicle@mail>")

        self.assertEqual(repeated.status, ingest.NEEDS_REVIEW)
        self.assertEqual(self.shortage("1604"), 10)

    def test_explicit_other_center_is_still_ignored(self) -> None:
        other_center = VEHICLE.replace(
            "בברכה,", "מרכז ציוד: מרכז ציוד ירושלים\n\nבברכה,"
        )
        result = ingest.ingest_issuance(other_center, "<vehicle-other-center@mail>")

        self.assertEqual(result.status, ingest.IGNORED)
        self.assertEqual(self.shortage("1604"), 0)

    def test_feature_flag_can_require_manual_review(self) -> None:
        fmt = replace(issuance_parser.load_format(), organizational_is_ours=False)
        with mock.patch.object(issuance_parser, "load_format", return_value=fmt):
            result = ingest.ingest_issuance(VEHICLE, "<vehicle-review@mail>")

        self.assertEqual(result.status, ingest.NEEDS_REVIEW)
        self.assertEqual(self.shortage("1604"), 0)


if __name__ == "__main__":
    unittest.main()
