"""
זיהוי העברה חוזרת של אותה הנפקה.

Message-ID מזהה *מייל*, לא הנפקה. בזרימת העבודה האמיתית מיילי ההנפקה
מועברים לתיבה, וכל העברה מקבלת Message-ID חדש — ולכן אותה הנפקה בדיוק
נספרה פעמיים והמלאי יצא שגוי.

הפתרון הוא טביעת אצבע של התוכן, אבל *לא* דחייה אוטומטית: אי אפשר לדעת
מהמייל אם זו העברה חוזרת או באמת הנפקה שנייה של אותו ציוד לאותו אדם,
ולכן ההכרעה עוברת למשתמשת.
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
        """הכותרות של ההעברה אינן חלק מההנפקה ולכן לא אמורות להשפיע."""
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
        swapped = "\n".join(lines)  # אותו תוכן
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
        self.assertEqual(self.issued(), 2, "אותה הנפקה לא אמורה להיספר פעמיים")

    def test_the_note_explains_both_options(self) -> None:
        second = ingest.ingest_issuance(FORWARDED, "<again@mail>")
        note = repo.get_issuance(second.issuance_id).review_note
        self.assertIn("הועבר שוב", note)
        self.assertIn("לאשר", note)
        self.assertIn("להתעלם", note)

    def test_it_is_not_dropped_it_is_decidable(self) -> None:
        """המשתמשת יכולה לאשר — כשזו באמת הנפקה נוספת."""
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
        """הנפקה בודדת לא אמורה לסמן את עצמה ככפילות של עצמה."""
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
        הבדיקה כאן בונה מחדש טבלה. ב-Postgres המסד משותף בין הבדיקות,
        ולכן מפילים את הסכימה כדי שה-setUp הבא ייצור אותה נקייה.
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
        המסד של המשתמשת נוצר לפני שהעמודה הזו הייתה קיימת. עלייה חייבת
        להוסיף אותה בלי לאבד נתונים ובלי להיכשל.
        """
        from app import db
        from app.db import _existing_columns, _migrate, connect, init_db

        conn = connect()
        # הטבלה נבנית כאן בתחביר של המנוע הפעיל, כדי שהבדיקה תתאר מסד ישן
        # אמיתי בשני המצבים ולא רק ב-SQLite.
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

        init_db()  # כולל את המיגרציה
        self.assertIn("content_key", _existing_columns(conn, "issuances"))
        self.assertIsNotNone(repo.find_issuance_by_message_id("<old@mail>"), "הנתונים נשמרו")

        _migrate(conn)  # בטוח להרצה חוזרת
        self.assertIn("content_key", _existing_columns(conn, "issuances"))


if __name__ == "__main__":
    unittest.main()
