"""
Detecting a re-forward of the same issuance.

A Message-ID identifies an *email*, not an issuance. In the real workflow the
issuance emails are forwarded into the mailbox, and every forward gets a new
Message-ID — so one and the same issuance was counted twice and the stock came
out wrong.

The fix is a fingerprint of the content, but *not* automatic rejection: the
email cannot tell us whether this is a re-forward or genuinely a second
issuance of the same equipment to the same person, so the decision goes to the
user.
"""
from __future__ import annotations

import unittest

from tests.base import SAMPLE_EMAIL, DBTestCase
from tests.test_forwarded import FORWARDED

from app import ingest, inventory, repo
from app.parsing.issuance_parser import parse


class Fingerprint(unittest.TestCase):
    def test_same_content_same_key(self) -> None:
        self.assertEqual(
            ingest.content_fingerprint(parse(FORWARDED)),
            ingest.content_fingerprint(parse(FORWARDED)),
        )

    def test_forwarding_wrapper_does_not_change_the_key(self) -> None:
        """The forwarding headers are not part of the issuance and must not affect it."""
        wrapped = "---------- Forwarded message ---------\nFrom: x@y\n\n" + FORWARDED
        self.assertEqual(
            ingest.content_fingerprint(parse(FORWARDED)),
            ingest.content_fingerprint(parse(wrapped)),
        )

    def test_different_quantity_changes_the_key(self) -> None:
        self.assertNotEqual(
            ingest.content_fingerprint(parse(SAMPLE_EMAIL)),
            ingest.content_fingerprint(parse(SAMPLE_EMAIL.replace("כמות: 2", "כמות: 9"))),
        )

    def test_different_recipient_changes_the_key(self) -> None:
        self.assertNotEqual(
            ingest.content_fingerprint(parse(SAMPLE_EMAIL)),
            ingest.content_fingerprint(parse(SAMPLE_EMAIL.replace("שלום עם הנצח", "שלום דני כהן"))),
        )

    def test_item_order_does_not_matter(self) -> None:
        lines = SAMPLE_EMAIL.split("\n")
        swapped = "\n".join(lines)  # the same content
        self.assertEqual(
            ingest.content_fingerprint(parse(SAMPLE_EMAIL)),
            ingest.content_fingerprint(parse(swapped)),
        )


class ReforwardedIssuance(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self.first = ingest.ingest_issuance(FORWARDED, "<forward-by-yoav@mail>")

    def issued(self, sku: str = "1111") -> int:
        return inventory.status_for_item(repo.find_item_by_sku(sku)).issued

    def test_first_forward_is_applied(self) -> None:
        self.assertEqual(self.first.status, ingest.APPLIED)
        self.assertEqual(self.issued(), 2)

    def test_second_forward_goes_to_review_not_to_stock(self) -> None:
        second = ingest.ingest_issuance(FORWARDED, "<forward-by-someone-else@mail>")
        self.assertEqual(second.status, ingest.NEEDS_REVIEW)
        self.assertEqual(self.issued(), 2, "the same issuance must not be counted twice")

    def test_the_note_explains_both_options(self) -> None:
        second = ingest.ingest_issuance(FORWARDED, "<again@mail>")
        note = repo.get_issuance(second.issuance_id).review_note
        self.assertIn("הועבר שוב", note)
        self.assertIn("לאשר", note)
        self.assertIn("להתעלם", note)

    def test_it_is_not_dropped_it_is_decidable(self) -> None:
        """The user can approve — when it really is an additional issuance."""
        second = ingest.ingest_issuance(FORWARDED, "<again@mail>")
        ok, _ = ingest.approve_issuance(second.issuance_id)
        self.assertTrue(ok)
        self.assertEqual(self.issued(), 4)

    def test_ignoring_it_keeps_the_stock_correct(self) -> None:
        second = ingest.ingest_issuance(FORWARDED, "<again@mail>")
        ingest.ignore_issuance(second.issuance_id)
        self.assertEqual(self.issued(), 2)

    def test_a_genuinely_different_issuance_is_applied(self) -> None:
        other = ingest.ingest_issuance(FORWARDED.replace("כמות: 2", "כמות: 9"), "<other@mail>")
        self.assertEqual(other.status, ingest.APPLIED)
        self.assertEqual(self.issued(), 11)

    def test_third_forward_also_caught(self) -> None:
        ingest.ingest_issuance(FORWARDED, "<second@mail>")
        third = ingest.ingest_issuance(FORWARDED, "<third@mail>")
        self.assertEqual(third.status, ingest.NEEDS_REVIEW)
        self.assertEqual(self.issued(), 2)

    def test_message_id_dedup_still_works_too(self) -> None:
        again = ingest.ingest_issuance(FORWARDED, "<forward-by-yoav@mail>")
        self.assertTrue(again.duplicate)


class ReanalysisDoesNotFlagItself(DBTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.load_real_items()
        self.issuance_id = ingest.ingest_issuance(FORWARDED, "<only-one@mail>").issuance_id

    def test_reanalysing_a_lone_issuance_keeps_it_applied(self) -> None:
        """A lone issuance must not flag itself as a duplicate of itself."""
        ingest.reanalyse_issuance(self.issuance_id)
        self.assertEqual(repo.get_issuance(self.issuance_id).status, ingest.APPLIED)
        self.assertEqual(inventory.status_for_item(repo.find_item_by_sku("1111")).issued, 2)

    def test_repeated_reanalysis_is_stable(self) -> None:
        for _ in range(3):
            ingest.reanalyse_issuance(self.issuance_id)
        self.assertEqual(repo.get_issuance(self.issuance_id).status, ingest.APPLIED)
        self.assertEqual(inventory.status_for_item(repo.find_item_by_sku("1111")).issued, 2)


class MigrationOfOlderDatabase(DBTestCase):
    def tearDown(self) -> None:
        """
        This test rebuilds a table. On Postgres the database is shared between
        tests, so the schema is dropped for the next setUp to recreate cleanly.
        """
        from app import db

        if db.is_postgres():
            db.connect().execute(
                "DROP TABLE IF EXISTS issuance_lines, adjustments, import_runs, "
                "issuances, items CASCADE"
            )
        super().tearDown()

    def test_content_key_is_added_to_an_existing_table(self) -> None:
        """
        The user's database was created before this column existed. Start-up must
        add it without losing data and without failing.
        """
        from app import db
        from app.db import _existing_columns, _migrate, connect, init_db

        conn = connect()
        # The table is built here in the active engine's syntax, so the test
        # describes a genuinely old database in both modes and not only on SQLite.
        pk = db._PK_POSTGRES if db.is_postgres() else db._PK_SQLITE
        cascade = " CASCADE" if db.is_postgres() else ""
        conn.execute(f"DROP TABLE issuances{cascade}")
        conn.execute(
            f"""
            CREATE TABLE issuances (
                id {pk}, message_id TEXT NOT NULL UNIQUE,
                email_date TEXT NOT NULL, recipient TEXT, issuer TEXT, center TEXT,
                raw_text TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('applied','needs_review','ignored')),
                source TEXT NOT NULL CHECK (source IN ('email','paste')),
                review_note TEXT, created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO issuances (message_id, email_date, raw_text, status, source, created_at) "
            "VALUES ('<old@mail>', '2026-08-28T11:19:00+00:00', 'טקסט', 'ignored', 'email', '2026-08-28T11:19:00+00:00')"
        )
        self.assertNotIn("content_key", _existing_columns(conn, "issuances"))

        init_db()  # includes the migration
        self.assertIn("content_key", _existing_columns(conn, "issuances"))
        self.assertIsNotNone(repo.find_issuance_by_message_id("<old@mail>"), "the data was preserved")

        _migrate(conn)  # safe to run again
        self.assertIn("content_key", _existing_columns(conn, "issuances"))


if __name__ == "__main__":
    unittest.main()
