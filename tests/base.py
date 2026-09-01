"""תשתית משותפת לטסטים — מסד נקי בזיכרון לכל בדיקה."""
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


class DBTestCase(unittest.TestCase):
    """כל בדיקה מקבלת קובץ מסד חדש, כך שאין דליפה בין בדיקות."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["DB_PATH"] = str(Path(self._tmp.name) / "test.db")

        from app import config, db

        config.DB_PATH = os.environ["DB_PATH"]
        # ברירת המחדל היא אתר בלי סיסמה. מחלקה שבודקת אימות מגדירה זאת בעצמה.
        self._auth = (config.APP_PASSWORD_HASH, config.SESSION_SECRET)
        config.APP_PASSWORD_HASH = ""
        config.SESSION_SECRET = "unit-test-secret"
        db.reset_for_tests()
        db.init_db()

    def tearDown(self) -> None:
        from app import config, db

        config.APP_PASSWORD_HASH, config.SESSION_SECRET = self._auth
        db.reset_for_tests()
        self._tmp.cleanup()

    def load_real_items(self) -> None:
        from app import importer

        importer.import_items(REAL_CSV.read_bytes(), REAL_CSV.name)
