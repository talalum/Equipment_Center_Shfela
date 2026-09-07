"""
An issuance already applied cannot be taken back out of the count.

The bug this guards against was found in production. `remaining` is recomputed
every time from the issuances applied right now, while a "reset to standard"
movement stores a delta computed against the issuances applied at that moment.
Dropping an applied issuance from the count therefore leaves its reset movement
behind as a credit against a shortage that no longer exists — and stock climbs
above the standard with nothing on screen to say so.

Two routes did exactly that, with no check at all: marking an applied issuance
as irrelevant, and re-analysing it.
"""
from __future__ import annotations

import unittest

from tests.base import SAMPLE_EMAIL, DBTestCase

from app import ingest, inventory, repo


class AppliedIssuanceIsFinal(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self.issuance_id = ingest.ingest_issuance(SAMPLE_EMAIL, "m-1", source="paste").issuance_id
        self.assertEqual(repo.get_issuance(self.issuance_id).status, ingest.APPLIED)

    def status(self, sku: str) -> inventory.ItemStatus:
        return inventory.status_for_item(repo.find_item_by_sku(sku))

    def test_it_cannot_be_marked_irrelevant(self) -> None:
        ok, message = ingest.ignore_issuance(self.issuance_id)
        self.assertFalse(ok)
        self.assertIn("כבר נקלטה", message)
        self.assertEqual(repo.get_issuance(self.issuance_id).status, ingest.APPLIED)

    def test_it_cannot_be_reanalysed(self) -> None:
        changed, message = ingest.reanalyse_issuance(self.issuance_id)
        self.assertFalse(changed)
        self.assertIn("כבר נקלטה", message)
        self.assertEqual(repo.get_issuance(self.issuance_id).status, ingest.APPLIED)

    def test_the_quantities_stay_in_the_count(self) -> None:
        before = self.status("1111").issued
        self.assertGreater(before, 0)
        ingest.ignore_issuance(self.issuance_id)
        ingest.reanalyse_issuance(self.issuance_id)
        self.assertEqual(self.status("1111").issued, before)

    def test_a_missing_issuance_is_still_reported(self) -> None:
        ok, message = ingest.ignore_issuance(9999)
        self.assertFalse(ok)
        self.assertIn("לא נמצאה", message)


class ResetSurvivesTheAttempt(DBTestCase):
    """
    The regression itself, end to end: reset to standard, then try to take the
    issuance back out. Before the guard, stock ended up *above* the standard.
    """

    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self.issuance_id = ingest.ingest_issuance(SAMPLE_EMAIL, "m-1", source="paste").issuance_id
        ingest.reset_all_shortages()

    def over_standard(self) -> list[str]:
        return [
            s.item.sku
            for s in inventory.status_for_all(repo.list_items())
            if s.remaining > s.item.standard_qty
        ]

    def test_reset_leaves_every_item_exactly_at_standard(self) -> None:
        self.assertEqual(self.over_standard(), [])
        self.assertEqual([s.item.sku for s in inventory.status_for_all(repo.list_items()) if s.in_shortage], [])

    def test_ignoring_afterwards_cannot_inflate_stock(self) -> None:
        ingest.ignore_issuance(self.issuance_id)
        self.assertEqual(self.over_standard(), [])

    def test_reanalysing_afterwards_cannot_inflate_stock(self) -> None:
        ingest.reanalyse_issuance(self.issuance_id)
        self.assertEqual(self.over_standard(), [])


class UnappliedIssuancesAreUntouched(DBTestCase):
    """The guard must not get in the way of the flows it does not concern."""

    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()

    def test_a_needs_review_issuance_can_still_be_ignored(self) -> None:
        issuance_id = repo.insert_issuance(
            message_id="<pending@mail>",
            email_date="2026-09-01T20:10:00+00:00",
            recipient=None,
            issuer=None,
            center=None,
            raw_text=SAMPLE_EMAIL,
            status=ingest.NEEDS_REVIEW,
            source="email",
            review_note="ממתין",
            lines=[],
        )
        ok, _ = ingest.ignore_issuance(issuance_id)
        self.assertTrue(ok)
        self.assertEqual(repo.get_issuance(issuance_id).status, ingest.IGNORED)

    def test_an_ignored_issuance_can_still_be_reanalysed_into_stock(self) -> None:
        issuance_id = repo.insert_issuance(
            message_id="<stuck@mail>",
            email_date="2026-09-01T20:10:00+00:00",
            recipient=None,
            issuer=None,
            center=None,
            raw_text=SAMPLE_EMAIL,
            status=ingest.IGNORED,
            source="email",
            review_note="נתקע",
            lines=[],
        )
        changed, _ = ingest.reanalyse_issuance(issuance_id)
        self.assertTrue(changed)
        self.assertEqual(repo.get_issuance(issuance_id).status, ingest.APPLIED)


if __name__ == "__main__":
    unittest.main()
