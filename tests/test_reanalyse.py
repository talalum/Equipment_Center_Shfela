"""
Re-analysing an issuance already in the database.

The need showed up in the field: a real email was taken in by a version that
could not read emphasis asterisks, and was marked ignored. After the parser was
fixed it stayed stuck — deduplication by Message-ID prevents a re-fetch, so the
improvement in the code had no effect on it at all.
"""
from __future__ import annotations

import unittest

from tests.base import SAMPLE_EMAIL, DBTestCase
from tests.test_forwarded import FORWARDED

from app import ingest, inventory, repo

STUCK_NOTE = 'ההנפקה שייכת ל"מרכז לא ידוע" ולא ל"מרכז ציוד שפלה" — לא נכנסה למלאי.'


class ReanalyseStuckIssuance(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        # Exactly the state the old version produced: stored with no lines, no
        # center, as ignored.
        self.issuance_id = repo.insert_issuance(
            message_id="<stuck@mail>",
            email_date="2026-09-01T20:10:00+00:00",
            recipient=None,
            issuer=None,
            center=None,
            raw_text=FORWARDED,
            status=ingest.IGNORED,
            source="email",
            review_note=STUCK_NOTE,
            lines=[],
        )

    def shortages(self) -> dict[str, int]:
        return {
            s.item.sku: s.shortage
            for s in inventory.status_for_all(repo.list_items())
            if s.in_shortage
        }

    def test_refetching_would_skip_it(self) -> None:
        """Confirms the premise: a re-fetch will never rescue a stuck email."""
        self.assertIsNotNone(repo.find_issuance_by_message_id("<stuck@mail>"))

    def test_reanalysis_rescues_it(self) -> None:
        changed, _ = ingest.reanalyse_issuance(self.issuance_id)
        self.assertTrue(changed)
        issuance = repo.get_issuance(self.issuance_id)
        self.assertEqual(issuance.status, ingest.APPLIED)
        self.assertIsNone(issuance.review_note)
        self.assertEqual(len(issuance.lines), 7)

    def test_details_are_filled_in(self) -> None:
        ingest.reanalyse_issuance(self.issuance_id)
        issuance = repo.get_issuance(self.issuance_id)
        self.assertEqual(issuance.center, "מרכז ציוד שפלה")
        self.assertEqual(issuance.issuer, "יואב חמאם")
        self.assertEqual(issuance.recipient, "אביהו ששון")

    def test_stock_is_updated(self) -> None:
        self.assertEqual(self.shortages(), {})
        ingest.reanalyse_issuance(self.issuance_id)
        self.assertEqual(
            self.shortages(),
            {"1111": 2, "3702": 1, "1102": 1, "1108": 1, "1103": 1, "1104": 5, "1105": 5},
        )

    def test_reanalysis_is_idempotent(self) -> None:
        """Running it again neither duplicates lines nor double counts."""
        ingest.reanalyse_issuance(self.issuance_id)
        first = self.shortages()
        for _ in range(3):
            ingest.reanalyse_issuance(self.issuance_id)
        self.assertEqual(self.shortages(), first)
        self.assertEqual(len(repo.get_issuance(self.issuance_id).lines), 7)

    def test_missing_issuance_is_reported(self) -> None:
        ok, message = ingest.reanalyse_issuance(9999)
        self.assertFalse(ok)
        self.assertIn("לא נמצאה", message)


class ReanalyseInBulk(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self.stuck = repo.insert_issuance(
            message_id="<stuck@mail>", email_date="2026-09-01T20:10:00+00:00",
            recipient=None, issuer=None, center=None, raw_text=FORWARDED,
            status=ingest.IGNORED, source="email", review_note=STUCK_NOTE, lines=[],
        )
        self.other_centre = ingest.ingest_issuance(
            SAMPLE_EMAIL.replace("מרכז ציוד שפלה", "מרכז ציוד ירושלים"), "<other@x>"
        ).issuance_id
        self.applied = ingest.ingest_issuance(SAMPLE_EMAIL, "<good@x>").issuance_id

    def test_only_unapplied_are_touched(self) -> None:
        counts = ingest.reanalyse_unapplied()
        self.assertEqual(counts["total"], 2, "the already-applied issuance must not be included")
        self.assertEqual(counts["applied"], 1)
        self.assertEqual(counts["ignored"], 1)

    def test_a_real_other_centre_stays_ignored(self) -> None:
        ingest.reanalyse_unapplied()
        self.assertEqual(repo.get_issuance(self.other_centre).status, ingest.IGNORED)

    def test_already_applied_issuance_is_unchanged(self) -> None:
        before = repo.get_issuance(self.applied)
        ingest.reanalyse_unapplied()
        after = repo.get_issuance(self.applied)
        self.assertEqual((after.status, len(after.lines)), (before.status, len(before.lines)))

    def test_stock_counts_each_issuance_once(self) -> None:
        ingest.reanalyse_unapplied()
        # The healthy issuance (2) + the freed stuck one (2) = 4 on SKU 1111.
        self.assertEqual(inventory.status_for_item(repo.find_item_by_sku("1111")).shortage, 4)

    def test_nothing_to_do_reports_zero(self) -> None:
        ingest.reanalyse_unapplied()
        ingest.reanalyse_unapplied()
        self.assertEqual(inventory.status_for_item(repo.find_item_by_sku("1111")).shortage, 4)


class ReanalyseOverHttp(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        from tests.test_web import WSGIClient

        self.client = WSGIClient()
        self.issuance_id = repo.insert_issuance(
            message_id="<stuck@mail>", email_date="2026-09-01T20:10:00+00:00",
            recipient=None, issuer=None, center=None, raw_text=FORWARDED,
            status=ingest.IGNORED, source="email", review_note=STUCK_NOTE, lines=[],
        )

    def test_button_appears_for_ignored_issuances(self) -> None:
        _, _, body = self.client.get("/issuances")
        self.assertIn(f"/issuances/{self.issuance_id}/reanalyse", body)

    def test_single_reanalyse_endpoint(self) -> None:
        status, _, _ = self.client.post(f"/issuances/{self.issuance_id}/reanalyse")
        self.assertEqual(status, 303)
        self.assertEqual(repo.get_issuance(self.issuance_id).status, ingest.APPLIED)

    def test_bulk_endpoint_updates_the_dashboard(self) -> None:
        self.client.post("/reanalyse-all")
        _, _, body = self.client.get("/")
        self.assertIn('<span class="num">7</span>', body)


if __name__ == "__main__":
    unittest.main()
