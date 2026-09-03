"""
תשתית משותפת לטסטים — מסד נקי לכל בדיקה.

כברירת מחדל רץ מול SQLite בקובץ זמני, בלי שום תלות חיצונית. אם מוגדר
DATABASE_URL_TEST אותן בדיקות בדיוק ירוצו מול Postgres אמיתי:

    DATABASE_URL_TEST=postgresql://... py -m unittest discover -s tests -t .

כך אותה חבילת בדיקות מאמתת את שני המנועים, ואין סיכון שהתנהגות תתפצל
ביניהם בלי שנדע.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "emails"
SAMPLE_EMAIL = (FIXTURES / "sample_issuance.txt").read_text(encoding="utf-8")
REAL_CSV = Path(__file__).resolve().parent.parent / "data" / "Inventory_Report.csv"

#: כתובת מסד Postgres לבדיקות. ריק = לרוץ מול SQLite.
POSTGRES_TEST_URL = os.environ.get("DATABASE_URL_TEST", "")

#: סדר הפוך לסדר התלויות, כדי ש-TRUNCATE לא ייפול על מפתחות זרים.
_ALL_TABLES = "issuance_lines, adjustments, import_runs, issuances, items"


class DBTestCase(unittest.TestCase):
    """כל בדיקה מקבלת מסד ריק, כך שאין דליפה בין בדיקות."""

    def setUp(self) -> None:
        from app import config, db

        # ברירת המחדל היא אתר בלי סיסמה. מחלקה שבודקת אימות מגדירה זאת בעצמה.
        self._auth = (config.APP_PASSWORD_HASH, config.SESSION_SECRET)
        config.APP_PASSWORD_HASH = ""
        config.SESSION_SECRET = "unit-test-secret"

        self._tmp = None
        self._prev_url = config.DATABASE_URL

        if POSTGRES_TEST_URL:
            config.DATABASE_URL = POSTGRES_TEST_URL
            db.reset_for_tests()
            db.init_db()
            # ריקון מהיר במקום בנייה מחדש של הסכימה לכל בדיקה.
            db.connect().execute(f"TRUNCATE {_ALL_TABLES} RESTART IDENTITY CASCADE")
        else:
            config.DATABASE_URL = ""
            self._tmp = tempfile.TemporaryDirectory()
            os.environ["DB_PATH"] = str(Path(self._tmp.name) / "test.db")
            config.DB_PATH = os.environ["DB_PATH"]
            db.reset_for_tests()
            db.init_db()

    def tearDown(self) -> None:
        from app import config, db

        config.APP_PASSWORD_HASH, config.SESSION_SECRET = self._auth
        db.reset_for_tests()
        config.DATABASE_URL = self._prev_url
        if self._tmp is not None:
            self._tmp.cleanup()

    def load_real_items(self) -> None:
        from app import importer

        importer.import_items(REAL_CSV.read_bytes(), REAL_CSV.name)
