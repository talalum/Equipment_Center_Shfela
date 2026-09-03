"""
Shared test infrastructure — a clean database for every test.

By default it runs against SQLite in a temporary file, with no external
dependency at all. If DATABASE_URL_TEST is set, those very same tests run
against a real Postgres:

    DATABASE_URL_TEST=postgresql://... py -m unittest discover -s tests -t .

That way one test suite verifies both engines, and there is no risk of the
behaviour diverging between them without anyone noticing.
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

#: Address of the Postgres database used for testing. Empty = run against SQLite.
POSTGRES_TEST_URL = os.environ.get("DATABASE_URL_TEST", "")

#: The reverse of the dependency order, so that TRUNCATE does not trip over
#: foreign keys.
_ALL_TABLES = "issuance_lines, adjustments, import_runs, issuances, items"


class DBTestCase(unittest.TestCase):
    """Every test gets an empty database, so nothing leaks between tests."""

    def setUp(self) -> None:
        from app import config, db

        # The default is a site with no password. A class testing authentication
        # sets that up for itself.
        self._auth = (config.APP_PASSWORD_HASH, config.SESSION_SECRET)
        config.APP_PASSWORD_HASH = ""
        config.SESSION_SECRET = "unit-test-secret"

        self._tmp = None
        self._prev_url = config.DATABASE_URL

        if POSTGRES_TEST_URL:
            config.DATABASE_URL = POSTGRES_TEST_URL
            db.reset_for_tests()
            db.init_db()
            # A fast wipe instead of rebuilding the schema for every test.
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
